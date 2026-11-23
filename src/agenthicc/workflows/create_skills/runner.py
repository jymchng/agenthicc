"""Design, write, and validate a set of project skills before publication."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from lauren_ai import tool
from lauren_ai._tools import TOOL_META, ToolMeta

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.plugins.authored import AuthoredArtifactStore, _reserved_names
from agenthicc.plugins.authored_types import AuthoredRun, PublishedArtifact, StagedArtifact
from agenthicc.runners.workflow_execution_context import (
    current_workflow_policy,
    effective_workflow_mode,
)
from agenthicc.tools.base import tool_name
from agenthicc.tools.capabilities import get_tool_capabilities, tool_control
from agenthicc.tools.errors import ToolErrorCode, tool_error
from agenthicc.workflows.authoring.budget import RepairBudget, RepairBudgetExhausted
from agenthicc.workflows.authoring.scoped_tools import (
    make_scoped_reader_tools,
    make_scoped_writer_tools,
)
from agenthicc.workflows.authoring.staged_runner import drive_staged_run, run_authoring_phase
from agenthicc.workflows.authoring.verdict import Finding, ValidationReport, decide
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.create_skills.plan import (
    MAX_FILE_BYTES,
    MAX_SKILLS,
    MAX_SUPPORT_FILES,
    SkillPlan,
    SkillPlanError,
    SkillPlanInput,
    plan_skills,
)
from agenthicc.workflows.create_skills.validation import validate_skill_set

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lauren_ai._memory import ShortTermMemory

    from agenthicc.tools.base import ToolLike
    from agenthicc.workflows.config import WorkflowConfig


_CACHE_CONTRACT = (
    "Author coherent project skills in the assigned stage. The runtime validates "
    "all files, caps repair rounds, and owns publication. Skill instructions "
    "can narrow the caller's tool access. Keep candidate text in phase context."
)


class CreateSkillsState(StrEnum):
    DESIGN = "design"
    WRITE = "write"
    VALIDATE = "validate"
    FINALIZE = "finalize"
    COMPLETE = "complete"
    FAILED = "failed"


PHASES = tuple(
    state.value
    for state in CreateSkillsState
    if state not in {CreateSkillsState.COMPLETE, CreateSkillsState.FAILED}
)


@dataclass(slots=True)
class CreateSkillsContext:
    intent: str
    run_id: str
    repair: RepairBudget
    state: CreateSkillsState = CreateSkillsState.DESIGN
    phase_iteration: int = 0
    plans: tuple[SkillPlan, ...] = ()
    predecessors: dict[str, str] = field(default_factory=dict)
    reports: dict[str, ValidationReport] = field(default_factory=dict)
    files: dict[str, tuple[str, ...]] = field(default_factory=dict)
    fail_reason: str = ""
    authored_run: AuthoredRun | None = None
    shared_memory: ShortTermMemory | None = field(default=None, repr=False)

    @property
    def summary(self) -> str:
        if self.state is CreateSkillsState.FAILED:
            return self.fail_reason
        return f"Validated {len(self.plans)} skills for publication: {', '.join(plan.slug for plan in self.plans)}."

    @property
    def return_outputs(self) -> dict[str, str]:
        return {"skills": ",".join(plan.slug for plan in self.plans)}


class CreateSkillsRunner(BaseWorkflowRunner):
    """Ensure the session publishes the approved Markdown set together."""

    def __init__(self, config: WorkflowConfig) -> None:
        self._cfg = config

    async def run(self, intent: str) -> CreateSkillsContext:
        handle = self._cfg.workflow_handle
        return await self.resume(
            CreateSkillsContext(
                intent,
                handle.run_id if handle is not None else uuid.uuid4().hex,
                RepairBudget.from_settings(self._cfg.cfg.workflows.get("authoring", {})),
            )
        )

    async def resume(self, context: object) -> CreateSkillsContext:
        if not isinstance(context, CreateSkillsContext):
            raise TypeError("create_skills requires a CreateSkillsContext.")
        if context.state is CreateSkillsState.COMPLETE and context.authored_run is None:
            self._finalize(context)
        return await drive_staged_run(
            self._cfg, context, PHASES, self._advance, workflow_name="create_skills"
        )

    async def _advance(self, ctx: CreateSkillsContext, store: AuthoredArtifactStore) -> None:
        match ctx.state:
            case CreateSkillsState.DESIGN:
                await self._design(ctx, store)
            case CreateSkillsState.WRITE:
                await self._write(ctx, store)
            case CreateSkillsState.VALIDATE:
                await self._validate(ctx, store)
            case CreateSkillsState.FINALIZE:
                self._finalize(ctx)

    def _scope_tool_names(self) -> frozenset[str]:
        tools = [*AGENT_TOOLS, *self._cfg.extension_tools()]
        if self._cfg.mcp_registry is not None:
            tools.extend(self._cfg.mcp_registry.all_tools())
        blocked = effective_workflow_mode(self._cfg.app_state.active_mode()).blocked_capabilities
        available = frozenset(
            tool_name(tool) for tool in tools if not get_tool_capabilities(tool) & blocked
        )
        policy = current_workflow_policy()
        if policy is None:
            return available
        if policy.visible_tool_names:
            available &= policy.visible_tool_names
        return frozenset(
            name for name in available if policy.capability_scope.is_tool_allowed(name)
        )

    async def _design(self, ctx: CreateSkillsContext, store: AuthoredArtifactStore) -> None:
        live = self._cfg.skills.current().value
        owned = {
            item.artifact.name: item.artifact.digest
            for item in await asyncio.to_thread(store.records)
            if item.artifact.kind == "skill"
        }
        reserved = _reserved_names("skill") | {
            name
            for slug, skill in live.items()
            if slug not in owned
            for name in skill.command_names
        }
        available = self._scope_tool_names()

        @tool_control
        @tool()
        async def submit_skills_plan(skills: list[SkillPlanInput]) -> dict[str, object]:
            """Submit the complete skill set before writing any files.

            Args:
                skills: Each plan includes slug, name, description, and suggested
                    topics in when_to_use. Provide optional aliases, tools,
                    disabled_tools, argument_hints, Markdown support files, and
                    update for explicit changes to an authored skill. Explain
                    any operator-activation-only model override in model_reason.
            """
            if ctx.plans:
                return tool_error(
                    ToolErrorCode.ALREADY_EXISTS, "This run already has an accepted plan."
                )
            try:
                plans = plan_skills(
                    skills,
                    reserved=reserved,
                    owned=frozenset(owned),
                    allowed_tools=available,
                )
            except SkillPlanError as exc:
                return tool_error(ToolErrorCode.INVALID_ARGUMENT, str(exc), reason=exc.code)
            ctx.plans = plans
            ctx.predecessors = {plan.slug: owned[plan.slug] for plan in plans if plan.update}
            return {"ok": True, "skills": [plan.slug for plan in plans]}

        metadata = cast("ToolMeta", vars(submit_skills_plan)[TOOL_META])
        metadata.parameters["input_schema"]["properties"]["skills"]["items"]["required"] = [
            "slug",
            "name",
            "description",
            "when_to_use",
        ]
        inventory = "\n".join(
            f"${slug} · {skill.description}" for slug, skill in sorted(live.items())
        )
        await self._phase(
            ctx,
            f"{ctx.intent}\n\nExisting skills:\n{inventory}\n\nAvailable tool names: {', '.join(sorted(available))}",
            "Design the whole skill set and call submit_skills_plan. Use distinct slugs "
            "and name only tools shown in the caller's inventory.",
            [submit_skills_plan],
        )
        if ctx.plans:
            ctx.state = CreateSkillsState.WRITE
        else:
            ctx.fail_reason = "The design phase did not submit a skill plan."
            ctx.state = CreateSkillsState.FAILED

    async def _write(self, ctx: CreateSkillsContext, store: AuthoredArtifactStore) -> None:
        root = await asyncio.to_thread(store.stage_directory, ctx.run_id, "skill")
        finished = False

        @tool_control
        @tool()
        async def confirm_skills_written() -> dict[str, object]:
            """Confirm the planned Markdown files are present in staging."""
            nonlocal finished
            finished = True
            return {"ok": True}

        await self._phase(
            ctx,
            f"Skills: {[plan.to_dict() for plan in ctx.plans]}\nPrevious findings: {ctx.fail_reason or 'none'}",
            "Write <slug>/SKILL.md and the planned Markdown support files through the "
            "scoped writer. Set the plan's fields in YAML frontmatter, including "
            "suggestedTopics, tools, disabledTools, aliases, and argumentHint when used. "
            "Keep the behavior in the Markdown body. Call confirm_skills_written.",
            [
                *make_scoped_writer_tools(
                    root,
                    suffixes=frozenset({".md"}),
                    max_file_bytes=MAX_FILE_BYTES,
                    max_files=MAX_SKILLS * (1 + MAX_SUPPORT_FILES),
                ),
                confirm_skills_written,
            ],
        )
        if finished:
            ctx.state = CreateSkillsState.VALIDATE
        else:
            self._repair(ctx, "The write phase did not confirm its Markdown files.")

    async def _validate(self, ctx: CreateSkillsContext, store: AuthoredArtifactStore) -> None:
        root = await asyncio.to_thread(store.stage_directory, ctx.run_id, "skill")
        ctx.reports, candidates, ctx.files = await asyncio.to_thread(
            validate_skill_set, root, ctx.plans
        )
        if self._cfg.reload_service is not None and not any(
            report.errors for report in ctx.reports.values()
        ):
            preview = await self._cfg.reload_service.preview(
                frozenset({"skills", "commands"}), staged_skills=candidates
            )
            for slug, candidate in candidates.items():
                relevant = [
                    item
                    for item in preview.diagnostics
                    if item.code == "name_conflict"
                    and Path(item.path).is_relative_to(candidate.path)
                ]
                if relevant:
                    original = ctx.reports[slug]
                    ctx.reports[slug] = ValidationReport(
                        original.digest,
                        (
                            *original.findings,
                            Finding(
                                "skill.name_collision", "error", slug, None, relevant[0].message
                            ),
                        ),
                    )
        findings = "\n".join(
            f"{item.path}: {item.code}: {item.message}"
            for report in ctx.reports.values()
            for item in report.findings
        )
        if any(decide(report, "approve") != "approve" for report in ctx.reports.values()):
            self._repair(ctx, findings)
            return

        accepted = False
        rejection = ""

        @tool_control
        @tool()
        async def approve_skills() -> dict[str, object]:
            """Approve the behavior of the validated skill set."""
            nonlocal accepted
            accepted = True
            return {"ok": True}

        @tool_control
        @tool()
        async def reject_skills(reason: str) -> dict[str, object]:
            """Send the skill set back to the writer with the reason.

            Args:
                reason: Describe the change needed.
            """
            nonlocal rejection
            if not reason.strip():
                return tool_error(ToolErrorCode.INVALID_ARGUMENT, "Explain the required change.")
            rejection = reason.strip()
            return {"ok": True}

        await self._phase(
            ctx,
            findings or "Deterministic validation passed.",
            "Read the validated skill files, then approve_skills or reject_skills.",
            [
                *make_scoped_reader_tools(
                    root, suffixes=frozenset({".md"}), max_file_bytes=MAX_FILE_BYTES
                ),
                approve_skills,
                reject_skills,
            ],
        )
        if accepted and not rejection:
            ctx.state = CreateSkillsState.FINALIZE
            ctx.fail_reason = ""
        else:
            self._repair(ctx, rejection or "The validate phase did not approve the skills.")

    def _finalize(self, ctx: CreateSkillsContext) -> None:
        candidates: list[StagedArtifact] = []
        for plan in ctx.plans:
            report = ctx.reports[plan.slug]
            artifact = PublishedArtifact(
                kind="skill",
                name=plan.slug,
                files=ctx.files[plan.slug],
                digest=report.digest,
                state="staged",
                advisories=tuple(
                    item.code for item in report.findings if item.severity == "warning"
                ),
                supersedes=ctx.predecessors.get(plan.slug),
            )
            candidates.append(StagedArtifact(artifact, report))
        ctx.authored_run = AuthoredRun(
            ctx.run_id, self._cfg.conversation_id, self._cfg.cfg.execution.model, tuple(candidates)
        )
        ctx.state = CreateSkillsState.COMPLETE

    def _repair(self, ctx: CreateSkillsContext, reason: str) -> None:
        try:
            ctx.repair = ctx.repair.charge()
        except RepairBudgetExhausted as exc:
            ctx.fail_reason = f"{exc}. {reason}"
            ctx.state = CreateSkillsState.FAILED
        else:
            ctx.fail_reason = reason
            ctx.state = CreateSkillsState.WRITE

    async def _phase(
        self,
        ctx: CreateSkillsContext,
        text: str,
        instructions: str,
        tools: Sequence[ToolLike],
    ) -> None:
        await run_authoring_phase(
            self._cfg,
            ctx,
            workflow_name="create_skills",
            cache_contract=_CACHE_CONTRACT,
            text=text,
            instructions=instructions,
            tools=tools,
        )
