"""Author a complete tool set inside a run-owned staging directory."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from lauren_ai import tool
from lauren_ai._tools import TOOL_META, ToolMeta

from agenthicc.plugins.authored import AuthoredArtifactStore, _reserved_names
from agenthicc.plugins.authored_types import AuthoredRun, PublishedArtifact, StagedArtifact
from agenthicc.tools.base import tool_name
from agenthicc.tools.capabilities import tool_control
from agenthicc.tools.errors import ToolErrorCode, tool_error
from agenthicc.workflows.authoring.budget import RepairBudget, RepairBudgetExhausted
from agenthicc.workflows.authoring.scoped_tools import (
    make_scoped_reader_tools,
    make_scoped_writer_tools,
)
from agenthicc.workflows.authoring.staged_runner import drive_staged_run, run_authoring_phase
from agenthicc.workflows.authoring.verdict import ValidationReport, decide
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.create_tools.plan import (
    MAX_MODULE_BYTES,
    MAX_MODULES,
    ToolPlan,
    ToolPlanError,
    ToolPlanInput,
    plan_set,
)
from agenthicc.workflows.create_tools.validation import validate_modules

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lauren_ai._memory import ShortTermMemory

    from agenthicc.tools.base import ToolLike
    from agenthicc.workflows.config import WorkflowConfig


CACHE_CONTRACT = (
    "Create the complete planned set of agenthicc tools. Use only the scope granted "
    "for this phase. The runtime validates exact bytes and owns publication. "
    "Tool parameters, source paths, and validation findings belong in phase context."
)


class CreateToolsState(StrEnum):
    ANALYZE = "analyze"
    GENERATE = "generate"
    VALIDATE = "validate"
    FINALIZE = "finalize"
    COMPLETE = "complete"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (self.COMPLETE, self.FAILED)


PHASES = tuple(state.value for state in CreateToolsState if not state.is_terminal)


@dataclass(slots=True)
class CreateToolsContext:
    intent: str
    run_id: str
    repair: RepairBudget
    state: CreateToolsState = CreateToolsState.ANALYZE
    phase_iteration: int = 0
    plans: tuple[ToolPlan, ...] = ()
    predecessors: dict[str, str] = field(default_factory=dict)
    reports: dict[str, ValidationReport] = field(default_factory=dict)
    static_only: bool = False
    fail_reason: str = ""
    authored_run: AuthoredRun | None = None
    shared_memory: ShortTermMemory | None = field(default=None, repr=False)

    @property
    def return_outputs(self) -> dict[str, str]:
        return {"tools": ",".join(item.name for item in self.plans)}

    @property
    def summary(self) -> str:
        if self.state is CreateToolsState.FAILED:
            return self.fail_reason
        names = ", ".join(item.name for item in self.plans)
        return f"Validated {len(self.plans)} tools for publication: {names}."


class CreateToolsRunner(BaseWorkflowRunner):
    """Run one bounded plan, generation, validation, and staging loop."""

    def __init__(self, config: WorkflowConfig) -> None:
        self._cfg = config

    async def run(self, intent: str) -> CreateToolsContext:
        handle = self._cfg.workflow_handle
        return await self.resume(
            CreateToolsContext(
                intent=intent,
                run_id=handle.run_id if handle is not None else uuid.uuid4().hex,
                repair=RepairBudget.from_settings(self._cfg.cfg.workflows.get("authoring", {})),
            )
        )

    async def resume(self, context: object) -> CreateToolsContext:
        if not isinstance(context, CreateToolsContext):
            raise TypeError("create_tools requires a CreateToolsContext.")
        if context.state is CreateToolsState.COMPLETE and context.authored_run is None:
            self._finalize(context)
        return await drive_staged_run(
            self._cfg, context, PHASES, self._advance, workflow_name="create_tools"
        )

    async def _advance(self, ctx: CreateToolsContext, store: AuthoredArtifactStore) -> None:
        match ctx.state:
            case CreateToolsState.ANALYZE:
                await self._analyze(ctx, store)
            case CreateToolsState.GENERATE:
                await self._generate(ctx, store)
            case CreateToolsState.VALIDATE:
                await self._validate(ctx, store)
            case CreateToolsState.FINALIZE:
                self._finalize(ctx)

    async def _analyze(self, ctx: CreateToolsContext, store: AuthoredArtifactStore) -> None:
        existing = await asyncio.to_thread(store.records)
        prior = {
            record.artifact.name: (record.artifact.files[0], record.artifact.digest)
            for record in existing
            if record.artifact.kind == "tool" and len(record.artifact.files) == 1
        }
        live = frozenset(map(tool_name, self._cfg.tool_surface.current().tools)) - prior.keys()
        mcp = self._cfg.mcp_registry
        reserved = _reserved_names("tool") | live
        if mcp is not None:
            reserved |= frozenset(map(tool_name, mcp.all_tools()))

        @tool_control
        @tool()
        async def submit_tools_plan(tools: list[ToolPlanInput]) -> dict[str, object]:
            """Submit plans for the entire tool set before writing any files.

            Args:
                tools: Each plan names a tool, its description and capabilities.
                    Optional module defaults to the tool name. Tools can share a
                    module. Parameters use name, type_hint, default (Python
                    literal or empty for required), and description. Provide
                    dependency names, network hosts, and any confirmation,
                    class form, or deliberate update requirements.
            """
            if ctx.plans:
                return tool_error(
                    ToolErrorCode.ALREADY_EXISTS, "This run already has an accepted plan."
                )
            try:
                plans = plan_set(tools, reserved, prior)
            except ToolPlanError as exc:
                return tool_error(ToolErrorCode.INVALID_ARGUMENT, str(exc), reason=exc.code)
            for plan in plans:
                path = store.workspace / ".agenthicc" / "tools" / f"{plan.module}.py"
                if path.exists() and not any(
                    filename == path.relative_to(store.workspace).as_posix()
                    for filename, _ in prior.values()
                ):
                    return tool_error(
                        ToolErrorCode.ALREADY_EXISTS,
                        f"{path.name} is not an authored module.",
                        reason="name_collision",
                    )
            ctx.plans = plans
            ctx.predecessors = {plan.name: prior[plan.name][1] for plan in plans if plan.update}
            return {"ok": True, "tools": [plan.name for plan in plans]}

        metadata = cast("ToolMeta", vars(submit_tools_plan)[TOOL_META])
        schema = metadata.parameters["input_schema"]["properties"]["tools"]["items"]
        schema["required"] = ["name", "description", "capabilities"]
        await self._phase(
            ctx,
            ctx.intent,
            "Plan all requested tools. Give each tool capabilities and a concise description, "
            "then submit the complete set with submit_tools_plan.",
            [submit_tools_plan],
        )
        if ctx.plans:
            ctx.state = CreateToolsState.GENERATE
        else:
            self._fail(ctx, "The analyze phase did not submit a tool plan.")

    async def _generate(self, ctx: CreateToolsContext, store: AuthoredArtifactStore) -> None:
        root = await asyncio.to_thread(store.stage_directory, ctx.run_id, "tool")
        completed = False

        @tool_control
        @tool()
        async def confirm_tools_generated() -> dict[str, object]:
            """Confirm that the planned Python modules have been written."""
            nonlocal completed
            completed = True
            return {"ok": True}

        tools = make_scoped_writer_tools(
            root,
            suffixes=frozenset({".py"}),
            max_file_bytes=MAX_MODULE_BYTES,
            max_files=MAX_MODULES,
        )
        await self._phase(
            ctx,
            f"Plans: {[plan.to_dict() for plan in ctx.plans]}\n"
            f"Previous findings: {ctx.fail_reason or 'none'}",
            "Write the exact module names as relative .py paths through write_file. "
            "Export the complete planned set with TOOLS. Use @tool() and the matching "
            "capability decorator or one set_metadata declaration with a frozenset of "
            "ToolCapability members. Use annotated JSON object results, Args docstrings, "
            "literal DEPENDENCIES, WorkspaceView for paths, and the shared guarded HTTP client. "
            "Call confirm_tools_generated once all modules are ready.",
            [
                *(tool for tool in tools if tool_name(tool) in {"read_file", "write_file"}),
                confirm_tools_generated,
            ],
        )
        if completed:
            ctx.state = CreateToolsState.VALIDATE
        else:
            self._repair(ctx, "The generate phase did not confirm its modules.")

    async def _validate(self, ctx: CreateToolsContext, store: AuthoredArtifactStore) -> None:
        root = await asyncio.to_thread(store.stage_directory, ctx.run_id, "tool")
        ctx.reports, ctx.static_only = await asyncio.to_thread(
            validate_modules, store.workspace, root, ctx.plans
        )
        findings = [
            f"{item.path}: {item.code}: {item.message}"
            for report in ctx.reports.values()
            for item in report.findings
        ]
        errors = any(decide(report, "approve") != "approve" for report in ctx.reports.values())
        if errors:
            self._repair(ctx, "\n".join(findings))
            return

        accepted = False
        issue = ""

        @tool_control
        @tool()
        async def approve_tools() -> dict[str, object]:
            """Approve the intent and planned behavior of the validated set."""
            nonlocal accepted
            accepted = True
            return {"ok": True}

        @tool_control
        @tool()
        async def reject_tools(reason: str) -> dict[str, object]:
            """Request a repair of the set.

            Args:
                reason: Explain the specific change needed.
            """
            nonlocal issue
            issue = reason.strip()
            return {"ok": bool(issue)}

        await self._phase(
            ctx,
            f"Review these plans and the validation findings.\n"
            f"{[asdict(plan) for plan in ctx.plans]}\n" + "\n".join(findings),
            "Review the generated source through read_file. Call approve_tools or reject_tools.",
            [
                *make_scoped_reader_tools(
                    root, suffixes=frozenset({".py"}), max_file_bytes=MAX_MODULE_BYTES
                ),
                approve_tools,
                reject_tools,
            ],
        )
        if accepted and not issue:
            ctx.fail_reason = ""
            ctx.state = CreateToolsState.FINALIZE
        else:
            self._repair(ctx, issue or "The validate phase did not approve the set.")

    def _finalize(self, ctx: CreateToolsContext) -> None:
        candidates = []
        for plan in ctx.plans:
            report = ctx.reports[plan.module]
            artifact = PublishedArtifact(
                kind="tool",
                name=plan.name,
                files=(f".agenthicc/tools/{plan.module}.py",),
                digest=report.digest,
                state="staged",
                declared_capabilities=plan.capabilities,
                network_hosts=plan.network_hosts,
                dependencies=plan.dependencies,
                advisories=tuple(
                    item.code for item in report.findings if item.severity == "warning"
                ),
                validation="static_only" if ctx.static_only else "passed",
                supersedes=ctx.predecessors.get(plan.name),
            )
            candidates.append(StagedArtifact(artifact, report))
        execution = self._cfg.cfg.execution
        ctx.authored_run = AuthoredRun(
            ctx.run_id, self._cfg.conversation_id, execution.model, tuple(candidates)
        )
        ctx.state = CreateToolsState.COMPLETE

    @staticmethod
    def _fail(ctx: CreateToolsContext, reason: str) -> None:
        ctx.fail_reason = reason
        ctx.state = CreateToolsState.FAILED

    def _repair(self, ctx: CreateToolsContext, reason: str) -> None:
        try:
            ctx.repair = ctx.repair.charge()
        except RepairBudgetExhausted as exc:
            self._fail(ctx, f"{exc}. {reason}")
        else:
            ctx.fail_reason = reason
            ctx.state = CreateToolsState.GENERATE

    async def _phase(
        self,
        ctx: CreateToolsContext,
        text: str,
        instructions: str,
        tools: Sequence[ToolLike],
    ) -> None:
        await run_authoring_phase(
            self._cfg,
            ctx,
            workflow_name="create_tools",
            cache_contract=CACHE_CONTRACT,
            text=text,
            instructions=instructions,
            tools=tools,
        )
