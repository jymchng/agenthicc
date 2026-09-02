"""Inspection tools for the create_workflow design and generation phases.

These give the authoring agent *ground truth* about the workflow-plugin API —
the ``PhaseSpec`` field list is read live from the dataclass and the capability
and role lists are read live from their enums, so the guidance can never drift
from the real contract.  A canonical, known-valid example workflow is included
so the agent has a correct template to adapt rather than one it hallucinates.

Most tools here are read-only and take no session state.  The cache-contract
validator is intentionally different: it imports the target workflow through
the same loader used at runtime, so its target must be a trusted generated path
and its execution capability is gated separately.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agenthicc.tools.base import ToolLike
    from agenthicc.workflows.create_workflow.catalog import AuthoringSnapshot

# Curated one-line purposes for PhaseSpec fields.  The field *names* come from
# the live dataclass (see describe_phasespec); this mapping only supplies human
# purpose text, and a field with no entry simply reports an empty purpose.
_PHASESPEC_PURPOSE: dict[str, str] = {
    "name": "Unique phase id within the workflow; referenced by next / on_reject.",
    "agent_type": (
        "Registry key selecting the default prompt and allowed capabilities: "
        "auto, planner, executor, reviewer, explorer, verifier, human, custom."
    ),
    "system_prompt_override": (
        "Generic WorkflowRunner only: replaces the selected role prompt for this phase; "
        "the base/framework policies remain active and the text stays dynamic for caching. "
        "Custom runners must pass their explicit system_prompt to run_phase() themselves; "
        "human phases do not invoke an agent turn."
    ),
    "mode_override": "RuntimeMode to activate for this phase, e.g. 'Yolo' to unlock write tools.",
    "allowed_capabilities": "Optional capability allowlist for this phase (None = role default).",
    "allowed_capabilities_override": "Explicit per-phase capability override; wins over the field and role default.",
    "max_turns": "Maximum LLM sub-turns (tool-call → response cycles) within one phase run.",
    "output_schema": "How to parse the phase output: 'plan', 'review_result', or 'free_text'.",
    "next": "Phase to run next on success; None ends the workflow.",
    "on_reject": "Phase to jump to when this phase's output is approved=False (retry loops).",
    "on_error": "Phase to run when this phase raises (reserved).",
    "max_iterations": "Retry ceiling for re-entering this phase; -1 = unlimited.",
    "require_explicit_completion": "Loop until the phase's completion tool is called.",
    "require_plan_finalization": "Loop until finalize_plan() is called.",
    "require_explicit_review": "Require approve/reject tool calls instead of an XML review tag.",
    "parallel_with": "Sibling phase names to run concurrently via asyncio.gather.",
    "terminal_wait_policy": "Terminal default: 'foreground' or 'background'.",
    "command_lifecycle": "Command lifecycle: 'oneshot' or 'service'.",
    "require_successful_commands": "Gate the phase transition on successful command outcomes.",
    "require_readiness": "Require a successful service-readiness result before transition.",
}

# The recommended shape: a workflow that ships its own state-machine runner.
# Known-valid — it parses and loads cleanly through the workflow loader.
_RUNNER_EXAMPLE = '''\
"""release_check — plan, verify, then report (example custom-runner workflow).

The runner below is the shape to copy: a typed state enum, a typed context, one
bounded async method per non-terminal state, an explicit
``while not state.is_terminal`` / ``match`` driver, and transitions that happen
only because a phase tool was called.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Callable
from enum import Enum, auto
from typing import TYPE_CHECKING

from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.phase_lifecycle import (
    PhaseAnnotation,
    PhaseBoundaryError,
    checkpoint_phase_boundary,
    publish_phase_annotation,
    reconcile_phase_cursor,
)
from agenthicc.workflows.checkpoint import topology_from_phase_specs
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

#: Bounded retries per phase — never loop forever waiting for a tool call.
_MAX_ATTEMPTS = 5

# Stable workflow policy.  Keep phase-specific instructions and current
# artifacts in the dynamic ``system_prompt`` argument to ``run_phase``.
CACHE_CONTRACT = """
[CACHE-STABLE WORKFLOW POLICY]
Keep this workflow contract unchanged across phases. Ask the user a focused
clarifying question through the existing `ask_user` tool whenever required
information is missing, ambiguous, or would materially change the result.
Wait for the answer; do not guess. Use the parent session's
`WorkflowConfig.workspace_access` policy for every filesystem, mention, Git,
and command-working-directory access; never construct a second workspace
scope, allow-list, or unrestricted sandbox inside this workflow. Actual
questions and answers, phase state, and artifacts are dynamic context and do
not belong here. Use the parent session's `WorkflowConfig.conversation_id` and
injected session memory unchanged across every phase, retry, and resume; never
create a second conversation or replace the session memory.
""".strip()


class ReleaseState(Enum):
    """Every state this workflow can be in."""

    PLAN = auto()
    VERIFY = auto()
    REPORT = auto()
    COMPLETE = auto()  # terminal
    # Terminal for this local state machine; the session owner can still
    # checkpoint a valid typed context for resume.
    FAILED = auto()

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (ReleaseState.COMPLETE, ReleaseState.FAILED)


@dataclasses.dataclass
class ReleaseContext:
    """Data carried across every phase of one run."""

    intent: str
    plan: str = ""
    verdict: str = ""
    blocker: str = ""
    fail_reason: str = ""
    artifacts: dict[str, str] = dataclasses.field(default_factory=dict)
    run_id: str = ""
    state: ReleaseState = ReleaseState.PLAN
    phase_iteration: int = 0
    plan_version: str = "release_check.v1"
    phase_attempts: dict[str, int] = dataclasses.field(default_factory=dict)
    completed_phases: list[str] = dataclasses.field(default_factory=list)
    phase_history: list[dict[str, object]] = dataclasses.field(default_factory=list)
    last_boundary: dict[str, object] = dataclasses.field(default_factory=dict)
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: ShortTermMemory | None = dataclasses.field(default=None, repr=False)


def _make_plan_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the plan phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_release_plan(plan: str) -> dict[str, object]:
        """Record the ordered release checks and advance to the verify phase.

        Args:
            plan: The checks to run before release, in order.
        """
        if not plan.strip():
            return {
                "ok": False,
                "error": "The plan was rejected: it must not be empty.",
                "fix": "Call submit_release_plan(plan) with the ordered checks.",
            }
        data["plan"] = plan.strip()
        event.set()
        return {"ok": True, "message": "Plan recorded. The verify phase starts next."}

    return [submit_release_plan]


def _make_verify_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return the pass/block decision tools for the verify phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def release_passed(summary: str) -> dict[str, object]:
        """Signal that every check passed.

        Args:
            summary: What was verified.
        """
        data["action"] = "pass"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True}

    @tool_control
    @tool()
    async def release_blocked(blocker: str) -> dict[str, object]:
        """Signal that a check failed and the release is blocked.

        Args:
            blocker: The concrete failure that must be fixed.
        """
        data["action"] = "block"
        data["blocker"] = blocker.strip()
        event.set()
        return {"ok": True}

    return [release_passed, release_blocked]


class ReleaseCheckRunner(CodePlanRunner):
    """State-machine runner for release_check.

    Subclasses ``CodePlanRunner`` purely to inherit its session wiring and the
    public ``run_phase()`` helper. ``super().run()`` is never called, so none of
    code_plan's own phases execute — this runner owns the whole flow.
    """

    workflow_name = "release_check"
    @property
    def total_phases(self) -> int:
        """Derive progress from the plugin's PhaseSpec declaration."""
        return len(self._phase_names())

    @staticmethod
    def _phase_names() -> tuple[str, ...]:
        return tuple(spec.name for spec in ReleaseCheckWorkflow.phases)

    def _publish_phase(self, ctx: ReleaseContext, state: ReleaseState) -> None:
        """Project the active PhaseSpec through the shared lifecycle helper."""
        phase_name = state.name.lower()
        phase_index = self._phase_names().index(phase_name)
        ctx.phase_attempts[phase_name] = ctx.phase_attempts.get(phase_name, 0) + 1
        publish_phase_annotation(
            self._cfg,
            PhaseAnnotation(
                workflow_name=self.workflow_name,
                phase_name=phase_name,
                phase_index=phase_index,
                total_phases=self.total_phases,
                run_id=ctx.run_id,
                intent=ctx.intent,
                model_id=str(getattr(self, "_model_id", "")),
                phase_iteration=ctx.phase_iteration,
                phase_attempt=ctx.phase_attempts[phase_name],
                plan_version=ctx.plan_version,
            ),
            ctx,
        )

    def _checkpoint_boundary(
        self,
        ctx: ReleaseContext,
        completed_phase: str,
        next_state: ReleaseState,
    ) -> None:
        """Persist the selected next state before another provider turn."""
        next_phase = None if next_state.is_terminal else next_state.name.lower()
        names = self._phase_names()
        next_index = names.index(next_phase) if next_phase is not None else names.index(completed_phase)
        outcome = "failed" if next_state is ReleaseState.FAILED else "completed"
        if next_state.is_terminal and next_state is not ReleaseState.FAILED:
            outcome = "terminal"
        boundary_key = "|".join(
            (
                completed_phase,
                next_phase or "",
                str(next_index),
                str(ctx.phase_iteration),
                outcome,
            )
        )
        if (
            ctx.last_boundary.get("boundary_key") == boundary_key
            and ctx.last_boundary.get("durable") is True
        ):
            return
        ctx.last_boundary = {
            "completed_phase": completed_phase,
            "next_state": next_state.name,
            "next_phase": next_phase,
            "outcome": outcome,
            "plan_version": ctx.plan_version,
            "boundary_key": boundary_key,
            "durable": False,
        }
        if completed_phase not in ctx.completed_phases:
            ctx.completed_phases.append(completed_phase)
        ctx.phase_history.append(dict(ctx.last_boundary))
        try:
            checkpoint_phase_boundary(
                self._cfg,
                ctx,
                completed_phase=completed_phase,
                next_phase=next_phase,
                phase_index=next_index,
                phase_iteration=ctx.phase_iteration,
                outcome=outcome,
            )
        except PhaseBoundaryError:
            raise

    def _reconcile_resume(self, ctx: ReleaseContext) -> None:
        """Resolve the saved cursor before constructing the first resume prompt."""
        resolution = reconcile_phase_cursor(
            self._phase_names(),
            ctx.state.name.lower(),
            completed_phases=ctx.completed_phases,
            terminal_phase="complete",
        )
        if resolution.phase_name == "complete":
            ctx.state = ReleaseState.COMPLETE
        else:
            ctx.state = ReleaseState[resolution.phase_name.upper()]
        if self._cfg.workflow_handle is not None and resolution.reconciled:
            self._cfg.workflow_handle.attach_context(ctx)
            self._cfg.workflow_handle.update_phase(
                resolution.phase_name,
                resolution.phase_index,
                ctx.phase_iteration,
                persist=False,
            )
            self._cfg.workflow_handle.save_checkpoint(reason="resume_reconciled")

    async def run(self, intent: str) -> ReleaseContext:
        """Drive plan → verify → report."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        ctx = ReleaseContext(
            intent=intent,
            run_id=run_id,
            state=ReleaseState.PLAN,
            shared_memory=memory,
        )
        if handle is not None:
            handle.attach_context(ctx)
        state = ctx.state

        while not state.is_terminal:
            ctx.state = state
            ctx.phase_iteration += 1
            completed_phase = state.name.lower()
            self._publish_phase(ctx, state)
            match state:
                case ReleaseState.PLAN:
                    state = await self._plan(ctx, memory)
                case ReleaseState.VERIFY:
                    state = await self._verify(ctx, memory)
                case ReleaseState.REPORT:
                    state = await self._report(ctx, memory)
            ctx.state = state
            self._checkpoint_boundary(ctx, completed_phase, state)
            log.info("release_check → %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> ReleaseContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, ReleaseContext):
            raise TypeError("release_check resume requires ReleaseContext")
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else context.shared_memory
        )
        if memory is None:
            memory = ShortTermMemory(
                max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        context.shared_memory = memory
        self._reconcile_resume(context)
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
        state = context.state
        while not state.is_terminal:
            context.state = state
            context.phase_iteration += 1
            completed_phase = state.name.lower()
            self._publish_phase(context, state)
            match state:
                case ReleaseState.PLAN:
                    state = await self._plan(context, memory)
                case ReleaseState.VERIFY:
                    state = await self._verify(context, memory)
                case ReleaseState.REPORT:
                    state = await self._report(context, memory)
            context.state = state
            self._checkpoint_boundary(context, completed_phase, state)
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: ReleaseState) -> int:
        return {ReleaseState.PLAN: 0, ReleaseState.VERIFY: 1, ReleaseState.REPORT: 2}.get(
            state, 0
        )

    async def _plan(self, ctx: ReleaseContext, memory: object) -> ReleaseState:
        """Loop until submit_release_plan fires; return VERIFY or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=ctx.intent if attempt == 1 else "Call submit_release_plan(plan) now.",
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the PLAN phase of release_check. List the checks that must "
                    "pass before this release, then call submit_release_plan(plan). Only a "
                    "successful submit_release_plan(plan) call changes phase; prose such "
                    "as 'done' never advances the workflow. Do not run the checks yet."
                ),
                max_turns=10,
                shared_memory=memory,
                tools=_make_plan_tools(event, data),
            )
            if event.is_set():
                ctx.plan = data["plan"]
                ctx.artifacts["plan"] = ctx.plan
                return ReleaseState.VERIFY

        ctx.fail_reason = "plan phase never called submit_release_plan()"
        return ReleaseState.FAILED

    async def _verify(self, ctx: ReleaseContext, memory: object) -> ReleaseState:
        """Loop until a verdict tool fires; return REPORT or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Run these checks:\\n{ctx.plan}"
                    if attempt == 1
                    else "Call release_passed(summary) or release_blocked(blocker) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY phase of release_check. Run the planned checks "
                    "with the shell tools, then call release_passed(summary) or "
                    "release_blocked(blocker). Only a successful call to one of these "
                    "transition tools changes phase; prose never advances the workflow. "
                    "You MUST call one of them."
                ),
                mode="Yolo",  # unlock write / execute tools for this phase
                max_turns=20,
                shared_memory=memory,
                tools=_make_verify_tools(event, data),
            )
            if event.is_set():
                if data.get("action") == "pass":
                    ctx.verdict = data.get("summary", "")
                else:
                    ctx.blocker = data.get("blocker", "")
                ctx.artifacts["verify"] = ctx.verdict or ctx.blocker
                return ReleaseState.REPORT

        ctx.fail_reason = "verify phase never reported a verdict"
        return ReleaseState.FAILED

    async def _report(self, ctx: ReleaseContext, memory: object) -> ReleaseState:
        """Single turn; always returns COMPLETE."""
        await self.run_phase(
            intent=ctx.intent,
            text=f"Plan:\\n{ctx.plan}\\n\\nResult: {ctx.verdict or ctx.blocker}",
            stable_system_prompt=CACHE_CONTRACT,
            system_prompt=(
                "You are in the REPORT phase of release_check. Summarise what was checked "
                "and whether the release is clear to ship. No phase-transition tool is "
                "available in this terminal phase; the runner owns completion."
            ),
            max_turns=4,
            shared_memory=memory,
        )
        return ReleaseState.COMPLETE


@dataclasses.dataclass
class ReleaseCheckParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.release_check]."""

    plan_model: str = ""
    verify_model: str = ""
    report_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "plan": self.plan_model,
            "verify": self.verify_model,
            "report": self.report_model,
        }


class ReleaseCheckWorkflow(WorkflowPlugin):
    """Plan the release checks, run them, then report."""

    name = "release_check"
    description = "Plan release checks, run them, then report."
    mode_bindings = []  # manual only — invoke with /workflow release_check
    # Optional integrations are explicit metadata.  This example needs none;
    # generated workflows must declare browser/MCP requirements here.
    required_integrations: tuple[str, ...] = ()
    optional_integrations: tuple[str, ...] = ()
    integration_fallbacks: dict[str, str] = {}
    # Declarative metadata for the registry and the TUI phase counter; the runner
    # above is what actually executes, and it follows exactly this graph.
    phases = [
        PhaseSpec(
            name="plan",
            max_turns=10,
            next="verify",
            system_prompt_override=(
                "You are in the PLAN phase of release_check. Call "
                "submit_release_plan(plan); only a successful transition-tool call "
                "changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="verify",
            max_turns=20,
            next="report",
            mode_override="Yolo",
            system_prompt_override=(
                "You are in the VERIFY phase of release_check. Call "
                "release_passed(summary) or release_blocked(blocker); only a successful "
                "transition-tool call changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="report",
            max_turns=4,
            output_schema="free_text",
            system_prompt_override=(
                "You are in the REPORT phase of release_check. No phase-transition tool "
                "is available; the runner owns completion."
            ),
        ),
    ]

    @classmethod
    def resolve_checkpoint_topology(cls, context_payload):
        """Return the same ordered graph used by the runner after restart."""
        del context_payload
        return topology_from_phase_specs(
            cls.name, tuple(cls.phases), topology_version="release_check.v1"
        )

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, ReleaseContext):
            raise TypeError("release_check checkpoint requires ReleaseContext")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "plan": context.plan,
            "verdict": context.verdict,
            "blocker": context.blocker,
            "fail_reason": context.fail_reason,
            "artifacts": context.artifacts,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
            "plan_version": context.plan_version,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> ReleaseContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", ReleaseState.PLAN.name))
        try:
            state = ReleaseState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown release_check state: {raw_state}") from exc
        raw_artifacts = payload.get("artifacts", {})
        artifacts = (
            {str(key): str(value) for key, value in raw_artifacts.items()}
            if isinstance(raw_artifacts, dict)
            else {}
        )
        return ReleaseContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            plan=str(payload.get("plan", "")),
            verdict=str(payload.get("verdict", "")),
            blocker=str(payload.get("blocker", "")),
            fail_reason=str(payload.get("fail_reason", "")),
            artifacts=artifacts,
            state=state,
            phase_iteration=int(payload.get("phase_iteration", 0)),
            plan_version=str(payload.get("plan_version", "release_check.v1")),
            phase_attempts={
                str(key): int(value)
                for key, value in payload.get("phase_attempts", {}).items()
            } if isinstance(payload.get("phase_attempts", {}), dict) else {},
            completed_phases=(
                [str(item) for item in payload.get("completed_phases", [])]
                if isinstance(payload.get("completed_phases", []), list)
                else []
            ),
            phase_history=(
                [dict(item) for item in payload.get("phase_history", []) if isinstance(item, dict)]
                if isinstance(payload.get("phase_history", []), list)
                else []
            ),
            last_boundary=(
                {str(key): item for key, item in payload.get("last_boundary", {}).items()}
                if isinstance(payload.get("last_boundary", {}), dict)
                else {}
            ),
            shared_memory=memory,
        )

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> ReleaseCheckRunner:
        """Return this workflow's own state-machine runner."""
        return ReleaseCheckRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.release_check]."""
        return ReleaseCheckParams(
            plan_model=str(source.get("plan_model", "") or ""),
            verify_model=str(source.get("verify_model", "") or ""),
            report_model=str(source.get("report_model", "") or ""),
        )
'''

# The fallback shape: a purely declarative graph, correct only when no phase
# needs conditional routing, retries, or its own transition tools.
_DECLARATIVE_EXAMPLE = '''\
"""doc_review — draft a document, then review it (declarative example)."""

from __future__ import annotations

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DocReviewWorkflow(WorkflowPlugin):
    name = "doc_review"
    description = "Draft a document, then review it."
    mode_bindings = []  # manual only — invoke with /workflow doc_review
    phases = [
        PhaseSpec(
            name="draft",
            agent_type="auto",
            max_turns=20,
            next="review",
            mode_override="Yolo",  # unlock write tools for this phase
            system_prompt_override=(
                "You are in the DRAFT phase. Write the requested document to disk "
                "using the write tools, then briefly state what you wrote and where. No "
                "phase-transition tool is available; the declarative runner applies the "
                "declared next phase after this turn."
            ),
        ),
        PhaseSpec(
            name="review",
            agent_type="auto",
            max_turns=8,
            next=None,  # None ends the workflow
            output_schema="free_text",
            system_prompt_override=(
                "You are in the REVIEW phase. Read the drafted document and summarise "
                "whether it satisfies the request, noting any gaps. No phase-transition "
                "tool is available in this terminal phase; the runner owns completion."
            ),
        ),
    ]
'''


def make_inspection_tools(
    snapshot: "AuthoringSnapshot | None" = None,
) -> list[Callable[..., object]]:
    """Return the authoring-surface inspection tools.

    The returned callables are the complete inspection surface used by the
    design phase.  Keep this list in one place so the authoring prompt and its
    tests cannot accidentally describe a stale subset of the tools.
    """
    from lauren_ai._tools import set_metadata, tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import (  # noqa: PLC0415
        CAPABILITIES_KEY,
        ToolCapability,
        tool_read,
    )

    # Validation reads a report but imports trusted Python and therefore also
    # remains EXECUTE-gated. Keeping both labels preserves the read-only
    # inspection contract without weakening the mode gate.
    tool_read_execute = set_metadata(
        CAPABILITIES_KEY,
        frozenset({ToolCapability.READ, ToolCapability.EXECUTE}),
    )

    def live_browser_schemas(
        factory: Callable[..., Iterable[object]],
    ) -> list[dict[str, object]]:
        """Extract schemas from browser tool factories without starting a browser."""
        from types import SimpleNamespace  # noqa: PLC0415

        from agenthicc.workflows.create_workflow.catalog import (  # noqa: PLC0415
            build_tool_catalog,
        )

        # The factories only close over the manager; construction performs no
        # launch, navigation, network access, or health probe.
        fake_manager = SimpleNamespace(settings=SimpleNamespace(enabled=True))
        try:
            entries = build_tool_catalog(cast(Iterable["ToolLike"], factory(fake_manager)))
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"schema extraction failed: {type(exc).__name__}"}]
        return [entry.to_dict() for entry in entries]

    @tool_read
    @_tool()
    async def describe_phasespec() -> dict[str, object]:
        """Describe every PhaseSpec field: name, type, default, and purpose.

        Returns the authoritative field reference for the PhaseSpec dataclass,
        read live from the running code so it never drifts from the real API.
        Use it to decide which fields each phase of your new workflow needs.
        """
        import dataclasses  # noqa: PLC0415

        from agenthicc.workflows.plugin import PhaseSpec  # noqa: PLC0415

        fields: list[dict[str, str]] = []
        for spec_field in dataclasses.fields(PhaseSpec):
            if spec_field.default is not dataclasses.MISSING:
                default_repr = repr(spec_field.default)
            elif spec_field.default_factory is not dataclasses.MISSING:
                default_repr = repr(spec_field.default_factory())
            else:
                default_repr = "(required)"
            fields.append(
                {
                    "name": spec_field.name,
                    "type": str(spec_field.type),
                    "default": default_repr,
                    "purpose": _PHASESPEC_PURPOSE.get(spec_field.name, ""),
                }
            )
        return {
            "schema_version": "agenthicc.phasespec.v1",
            "phasespec_fields": fields,
        }

    @tool_read
    @_tool()
    async def list_tool_capabilities() -> dict[str, object]:
        """List the tool capabilities a phase can allow or a mode can block.

        Returns each ToolCapability value with its description, read live from the
        enum.  Use these when reasoning about mode_override (which unlocks write /
        execute / network tools) and allowed_capabilities.
        """
        from agenthicc.tools.capabilities import ToolCapability  # noqa: PLC0415

        descriptions: dict[str, str] = {
            "read": "Reads files / data — no persistent side effects.",
            "write": "Creates, modifies, or deletes files / data.",
            "execute": "Runs shell commands or arbitrary code.",
            "git_read": "Reads git history, diffs, status, blame.",
            "git_write": "Modifies git state (add, commit, checkout, stash).",
            "network": "Makes outbound network calls.",
            "search": "Searches content without state changes.",
            "control": "Advances an internal workflow or session state machine.",
            "undeclared": "No capability metadata was declared; Safe requires approval and Plan blocks it.",
        }
        caps = [
            {"value": cap.value, "description": descriptions.get(cap.value, "")}
            for cap in ToolCapability
        ]
        return {
            "schema_version": "agenthicc.tool-capabilities.v1",
            "capabilities": caps,
        }

    @tool_read
    @_tool()
    async def list_agent_roles() -> dict[str, object]:
        """List the agent_type values a phase may use.

        Returns the PhaseRole constants read live from the code.  Most phases use
        'auto'; the others map to role-specific default prompts and capabilities.
        """
        from agenthicc.agents.plugin import ROLE_DEFAULT_ALLOWED  # noqa: PLC0415
        from agenthicc.workflows.plugin import PhaseRole  # noqa: PLC0415

        roles = [
            value
            for name, value in vars(PhaseRole).items()
            if not name.startswith("_") and isinstance(value, str)
        ]
        defaults: dict[str, list[str] | None] = {}
        for role in roles:
            role_capabilities = ROLE_DEFAULT_ALLOWED.get(role)
            defaults[role] = (
                None
                if role_capabilities is None
                else sorted(
                    str(item.value if hasattr(item, "value") else item)
                    for item in role_capabilities
                )
            )
        return {
            "schema_version": "agenthicc.phase-roles.v1",
            "agent_types": roles,
            "defaults": defaults,
        }

    @tool_read
    @_tool()
    async def describe_cloakbrowser_tools() -> dict[str, object]:
        """Describe the optional, session-scoped browser tool contract."""
        from agenthicc.config import CloakBrowserSettings, ToolSettings  # noqa: PLC0415
        from agenthicc.tools.cloakbrowser.agent_tools import (  # noqa: PLC0415
            CLOAKBROWSER_AGENT_TOOLS,
            make_cloakbrowser_tools,
        )

        settings = CloakBrowserSettings()
        return {
            "optional_extra": "cloakbrowser",
            # This is the configuration flag, not a claim that the optional
            # package or its browser runtime is installed.
            "enabled_by_default": settings.enabled,
            "selected_by_default": ToolSettings().browser_backend == "cloakbrowser",
            "dependency_optional": True,
            "dependency_installed_by_default": False,
            "configuration": "[tools.cloakbrowser]",
            "tool_names": list(CLOAKBROWSER_AGENT_TOOLS),
            "schema_version": "agenthicc.authoring-catalog.v1",
            "tools": live_browser_schemas(make_cloakbrowser_tools),
            "constraints": {
                "url": "absolute http:// or https:// URL; bounded by the session network policy",
                "page_id": "opaque page identifier returned by the open operation",
                "selector": "one bounded CSS/text selector; sensitive fields are rejected",
                "operation_id": "optional bounded id; reuse it for safe mutation retries",
                "artifacts": "screenshots are written below the session workspace artifact directory",
            },
            "effective_session": dict(snapshot.browser) if snapshot is not None else {},
            "availability": (
                "The integration flag and allow-all policy are enabled by default for the "
                "local VPS/sandbox profile: localhost, private addresses, arbitrary HTTP(S) "
                "hosts, and all ports are reachable. Set allow_all_domains = false and provide "
                "an allow-list for a narrower boundary. Install the optional cloakbrowser "
                "extra; without it, status reports dependency_missing."
            ),
            "phase_guidance": (
                "Declare NETWORK plus READ for observation phases and NETWORK plus WRITE "
                "for interactions. Keep browser tools out of design/validation phases "
                "unless the workflow has a documented, intentional reason."
            ),
            "phase_spec_example": (
                "from agenthicc.tools.capabilities import ToolCapability\n"
                "PhaseSpec(name='inspect_site', allowed_capabilities=frozenset({"
                "ToolCapability.READ, ToolCapability.NETWORK}), max_turns=8, next='report')"
            ),
            "security": [
                "navigation is unrestricted by default or restricted to configured domains when "
                "allow_all_domains = false; every DNS answer is checked",
                "the default local/VPS profile permits loopback and private addresses; set "
                "allow_all_domains = false to restore those protections",
                "sensitive form fields, raw JavaScript, arbitrary CDP, cookies, and proxy settings are unavailable",
                "snapshots and screenshots are bounded and screenshots are workspace artifacts",
                "browser objects are not serialized into workflow checkpoints",
            ],
            "operation_id": (
                "Every browser operation tool except *_status accepts an optional bounded "
                "operation_id. Reuse the same id when safely retrying a mutation; the session "
                "manager returns the cached result instead of repeating it."
            ),
        }

    @tool_read
    @_tool()
    async def describe_playwright_tools() -> dict[str, object]:
        """Describe the optional, session-scoped Playwright browser contract."""
        from agenthicc.config import PlaywrightSettings, ToolSettings  # noqa: PLC0415
        from agenthicc.tools.playwright.agent_tools import (  # noqa: PLC0415
            PLAYWRIGHT_AGENT_TOOLS,
            make_playwright_tools,
        )

        settings = PlaywrightSettings()
        return {
            "optional_extra": "playwright",
            # Playwright's setting is enabled by default, but CloakBrowser is
            # the selected backend unless the operator opts into this one.
            "enabled_by_default": settings.enabled,
            "selected_by_default": ToolSettings().browser_backend == "playwright",
            "dependency_optional": True,
            "dependency_installed_by_default": False,
            "backend_selection": "Set [tools].browser_backend = 'playwright'.",
            "configuration": "[tools.playwright]",
            "tool_names": list(PLAYWRIGHT_AGENT_TOOLS),
            "schema_version": "agenthicc.authoring-catalog.v1",
            "tools": live_browser_schemas(make_playwright_tools),
            "constraints": {
                "url": "absolute http:// or https:// URL; bounded by the session network policy",
                "page_id": "opaque page identifier returned by the open operation",
                "selector": "one bounded CSS/text selector; sensitive fields are rejected",
                "operation_id": "optional bounded id; reuse it for safe mutation retries",
                "artifacts": "screenshots are written below the session workspace artifact directory",
            },
            "effective_session": dict(snapshot.browser) if snapshot is not None else {},
            "browser_types": ["chromium", "firefox", "webkit"],
            "availability": (
                "The backend setting and allow-all policy are enabled by default but the backend "
                "is not selected by default. Select it explicitly and install the optional "
                "playwright extra and its browser runtime. Set allow_all_domains = false and "
                "configure an allow-list when a narrower boundary is required."
            ),
            "phase_guidance": (
                "Declare NETWORK plus READ for observation phases and NETWORK plus WRITE "
                "for interactions. Keep browser tools out of design/validation phases "
                "unless the workflow has a documented, intentional reason."
            ),
            "security": [
                "navigation and subresource requests are unrestricted by default or restricted "
                "to configured domains when allow_all_domains = false; DNS rebinding is checked",
                "the default profile permits loopback and private addresses; set "
                "allow_all_domains = false to restore private-address protection",
                "sensitive form fields, raw JavaScript, arbitrary CDP, cookies, and proxy settings are unavailable",
                "snapshots and screenshots are bounded and screenshots are workspace artifacts",
                "browser objects are not serialized into workflow checkpoints",
            ],
            "operation_id": (
                "Every browser operation tool except *_status accepts an optional bounded "
                "operation_id. Reuse the same id when safely retrying a mutation; the session "
                "manager returns the cached result instead of repeating it."
            ),
        }

    @tool_read
    @_tool()
    async def show_example_workflow(style: str = "runner") -> dict[str, object]:
        """Return a complete, known-valid workflow package entry point to adapt.

        Default style="runner" returns the shape you should almost always copy: a
        workflow that ships its OWN state-machine runner — typed state enum, typed
        dataclass context, one bounded async method per state, an explicit
        `while not state.is_terminal` / `match` driver, `resume()`, per-phase
        transition tools passed to `run_phase(..., tools=...)`, checkpoint codec
        hooks, and `build_runner()` returning it. That is how `code_plan` and
        `create_workflow` themselves are built.

        style="declarative" returns the fallback: a bare PhaseSpec graph with no
        runner. Only correct when every phase is one unconditional agent turn.

        Read the real implementations for more depth with
        inspect_agenthicc_source('agenthicc.workflows.code_plan.runner:CodePlanRunner').

        Args:
            style: "runner" (recommended, default) or "declarative".
        """
        if style.strip().lower() == "declarative":
            return {
                "style": "declarative",
                "path": ".agenthicc/workflows/doc_review",
                "entry_point": ".agenthicc/workflows/doc_review/runner.py",
                "source": _DECLARATIVE_EXAMPLE,
                "note": (
                    "No runner: every phase is one unconditional agent turn. If any phase "
                    "needs a retry, a branch, a loop, or its own transition tool, use "
                    "show_example_workflow('runner') instead."
                ),
            }
        return {
            "style": "runner",
            "path": ".agenthicc/workflows/release_check",
            "entry_point": ".agenthicc/workflows/release_check/runner.py",
            "source": _RUNNER_EXAMPLE,
            "note": (
                "Recommended. The runner owns the control flow and every transition is a "
                "tool call. Subclassing CodePlanRunner provides the session wiring and the "
                "public run_phase() helper; super().run() is never called, so none of "
                "code_plan's phases execute."
            ),
        }

    @tool_read
    @_tool()
    async def describe_runner_pattern() -> dict[str, object]:
        """Return the checklist a generated custom workflow runner must satisfy.

        Use this while designing so the design states each element explicitly, and
        again while generating so nothing is missed. A workflow whose phases need
        retries, conditional routing, loops, accumulated context, or phase-local
        transition tools MUST ship a runner — a bare PhaseSpec graph cannot
        express any of those.
        """
        return {
            "when_required": [
                "any phase that must retry until a specific tool is called",
                "any conditional or looping transition (approve → next, reject → back)",
                "context accumulated across phases and injected into later prompts",
                "phase-local tools that do not belong in the session tool set",
                "a human approval gate that blocks the handoff",
                "bounded failure handling with an explicit terminal state",
            ],
            "required_elements": [
                "a typed State(Enum) with every non-terminal and terminal state, "
                "and an is_terminal property",
                "a typed @dataclass context carrying the intent, run id, current state, "
                "phase iteration, per-phase outputs, and failure reason",
                "one bounded async method per non-terminal state, returning the next state",
                "run(intent): build the context, then "
                "'while not state.is_terminal' + 'match state' dispatch",
                "resume(context): restore and re-enter the same dispatch path; never call "
                "run(context.intent) for a recoverable checkpoint",
                "checkpoint_context_to_payload(context) and "
                "checkpoint_context_from_payload(payload, memory=None) on the plugin; "
                "omit session memory from JSON and reattach the supplied memory on restore",
                "fixed PhaseSpec lists may inherit the checkpoint topology resolver; if the "
                "runner filters, skips, generates, or reorders phases, implement a pure "
                "resolve_checkpoint_topology(context_payload) method and persist the "
                "selected phase names, profile, and plan version",
                "derive phase_index from the active topology for annotations, phase entry, "
                "boundaries, failures, and resume; never use a full-registry index for a "
                "profile-filtered graph",
                "phase tool factories that set an asyncio.Event; the state method checks "
                "the event after the turn returns and never parses the agent's prose",
                "each phase prompt names its @tool_control transition tool(s) and says "
                "that only a successful transition-tool call changes phase; prose never "
                "advances the workflow",
                "build_runner() on the plugin returning the runner class",
                "inherit WorkflowConfig.workspace_scope and WorkflowConfig.workspace_access; "
                "never construct a second scope or bypass the parent policy in a custom tool",
                "declare only truly required deferred dependencies in the plugin's "
                "required_startup_phases tuple; use config.wait_for_startup() for a "
                "phase-local gate and keep optional integrations on a real fallback path",
                "attach the typed context to config.workflow_handle before the first provider "
                "or tool call; the framework persists setup failures as diagnostic-only and "
                "typed phase errors as error-paused checkpoints",
                "construct PhaseAnnotation from the authoritative PhaseSpec plan and call "
                "publish_phase_annotation() before every phase turn, retry, and resume",
                "after each valid transition, call checkpoint_phase_boundary() with the "
                "committed next state before publishing or invoking the next phase",
                "propagate PhaseBoundaryError to the framework finalizer; never continue "
                "after a checkpoint serialization or storage failure",
                "reconcile checkpoint, verified receipts, and journal state before any "
                "resume prompt; transcript summaries are advisory only",
                "do not swallow phase exceptions or mark a failed run complete; let the "
                "framework failure finalizer own error disposition and persistence; every "
                "ordinary exception type uses the same resumability path when typed "
                "context and checkpoint storage are available",
                "do not pass recoverable=False or otherwise disable resumability for an "
                "ordinary workflow exception; failure_kind is diagnostic only",
            ],
            "turn_api": (
                "Subclass CodePlanRunner and call the public "
                "run_phase(intent=, text=, system_prompt=, mode=, max_turns=, "
                "shared_memory=, tools=) once per phase. Use the injected "
                "WorkflowConfig.session_memory for every call (with a local fallback only "
                "when no session memory exists), and pass one object to every phase. "
                "Attach the typed context to config.workflow_handle and update its phase "
                "cursor through PhaseAnnotation/publish_phase_annotation; call "
                "checkpoint_phase_boundary after each transition. Never call super().run() — "
                "that would execute code_plan's own "
                "phases."
            ),
            "reference_implementations": [
                "agenthicc.workflows.code_plan.runner:CodePlanRunner",
                "agenthicc.workflows.create_workflow.runner:CreateWorkflowRunner",
            ],
            "example": "show_example_workflow('runner')",
        }

    @tool_read
    @_tool()
    async def describe_phase_lifecycle() -> dict[str, object]:
        """Return the exact runtime annotation and checkpoint contract.

        This intentionally exposes the ordering and failure semantics as
        structured data so an authoring agent can implement the lifecycle
        without copying a built-in workflow's private business phases.
        """
        return {
            "schema_version": "agenthicc.phase-lifecycle.v1",
            "required_imports": (
                "from agenthicc.workflows.phase_lifecycle import "
                "PhaseAnnotation, PhaseBoundaryError, checkpoint_phase_boundary, "
                "publish_phase_annotation, reconcile_phase_cursor"
            ),
            "annotation_fields": [
                "workflow_name",
                "phase_name",
                "phase_index",
                "total_phases",
                "run_id",
                "intent",
                "model_id",
                "phase_iteration",
                "phase_attempt",
                "status",
                "plan_version",
            ],
            "entry_order": [
                "set typed state and increment phase iteration/attempt",
                "resolve and attach the active checkpoint topology before deriving the index",
                "attach typed context to config.workflow_handle",
                "publish_phase_annotation(config, annotation, context)",
                "run the bounded inner agent-turn loop",
            ],
            "boundary_order": [
                "validate successful transition-tool result and next state",
                "commit output, artifacts, history, retry/rejection data, and next state",
                "checkpoint_phase_boundary(config, context, completed_phase, next_phase, outcome)",
                "publish the next phase annotation only after checkpoint success",
                "allow the next provider turn",
            ],
            "failure_rule": (
                "PhaseBoundaryError must propagate to the framework failure finalizer. "
                "Never swallow save_checkpoint errors or call the next provider turn. "
                "All ordinary exception classes use this same finalizer; exception "
                "classification must not turn a valid typed run into a fresh INIT run."
            ),
            "resume_rule": (
                "resume(context) reattaches the supplied session memory and conversation_id, "
                "reconciles durable state before constructing a phase prompt, then enters "
                "the same outer dispatch loop; it never calls run(intent)."
            ),
            "prompt_cache_rule": (
                "Keep CACHE_CONTRACT, schemas, and lifecycle policy stable. Put phase name, "
                "iteration, artifacts, questions, and transition data in dynamic context."
            ),
        }

    @tool_read
    @_tool()
    async def show_phase_lifecycle_template() -> dict[str, object]:
        """Return a minimal implementation pattern for a custom runner."""
        return {
            "schema_version": "agenthicc.phase-lifecycle-template.v1",
            "source": (
                "from agenthicc.workflows.phase_lifecycle import (\n"
                "    PhaseAnnotation, PhaseBoundaryError, checkpoint_phase_boundary,\n"
                "    publish_phase_annotation, reconcile_phase_cursor,\n"
                ")\n\n"
                "def publish_current(self, ctx, phase_name, phase_index):\n"
                "    publish_phase_annotation(\n"
                "        self._cfg,\n"
                "        PhaseAnnotation(\n"
                "            workflow_name=self.workflow_name, phase_name=phase_name,\n"
                "            phase_index=phase_index, total_phases=len(self._phase_names()),\n"
                "            run_id=ctx.run_id, intent=ctx.intent, model_id=self._model_id,\n"
                "            phase_iteration=ctx.phase_iteration,\n"
                "            phase_attempt=ctx.phase_attempts.get(phase_name, 0),\n"
                "            plan_version=ctx.plan_version,\n"
                "        ), ctx,\n"
                "    )\n\n"
                "def checkpoint_boundary(self, ctx, completed_phase, next_phase, index, outcome):\n"
                "    try:\n"
                "        return checkpoint_phase_boundary(\n"
                "            self._cfg, ctx, completed_phase=completed_phase,\n"
                "            next_phase=next_phase, phase_index=index,\n"
                "            phase_iteration=ctx.phase_iteration, outcome=outcome,\n"
                "        )\n"
                "    except PhaseBoundaryError:\n"
                "        raise  # the framework failure finalizer owns recovery"
            ),
            "topology_source": (
                "from agenthicc.workflows.checkpoint import topology_from_phase_specs\n\n"
                "@classmethod\n"
                "def resolve_checkpoint_topology(cls, context_payload):\n"
                "    fields = context_payload.get('fields', context_payload)\n"
                "    selected = tuple(fields.get('active_phase_names', cls.phase_names()))\n"
                "    # Rebuild the exact ordered active graph; do not use the full registry index.\n"
                "    phases = tuple(spec for spec in cls.phases if spec.name in selected)\n"
                "    return topology_from_phase_specs(\n"
                "        cls.name, phases, topology_version='workflow.v1',\n"
                "        profile=str(fields.get('profile', 'default')),\n"
                "    )"
            ),
            "do_not": [
                "do not put runtime annotation values into CACHE_CONTRACT",
                "do not call another phase method directly",
                "do not use agent prose as a transition",
                "do not replace supplied session memory or conversation_id",
                "do not silently continue after a boundary checkpoint error",
            ],
        }

    @tool_read
    @_tool()
    async def describe_transition_tool_pattern() -> dict[str, object]:
        """Return the canonical import/decorator pattern for phase handoffs.

        This is intentionally separate from the broad runner checklist because
        a local import error inside a tool factory is invisible to module-load
        validation until that phase actually starts.
        """
        return {
            "canonical_import": (
                "from lauren_ai._tools import tool\n"
                "from agenthicc.tools.capabilities import tool_control"
            ),
            "canonical_decorators": "@tool_control\n@tool()\nasync def transition(...): ...",
            "decorator_rules": [
                "tool_control is a bare decorator; never write @tool_control().",
                "tool_control must come from agenthicc.tools.capabilities, not lauren_ai._tools.",
                "Put @tool_control above @tool() so the final callable carries CONTROL metadata.",
                "Apply it to every callable that can advance, reject, retry, or branch a phase.",
            ],
            "validation_boundary": (
                "Factory-local imports are checked statically because validation must not "
                "execute arbitrary generated tool factories. Import success alone does not "
                "prove local factory imports work; use this contract and the generated "
                "template before calling a phase."
            ),
            "failure_prevented": [
                "ImportError: cannot import name 'tool_control' from lauren_ai._tools",
                "AttributeError caused by invoking the bare decorator as @tool_control()",
                "phase transitions not being classified with ToolCapability.CONTROL",
            ],
        }

    @tool_read
    @_tool()
    async def describe_prompt_cache_contract() -> dict[str, object]:
        """Describe the cache-stable prompt and user-questioning contract."""

        from agenthicc.runners.prompt_contract import (  # noqa: PLC0415
            CACHE_CONTRACT_VERSION,
            DEFAULT_WORKFLOW_CACHE_POLICY,
        )

        return {
            "contract_version": CACHE_CONTRACT_VERSION,
            "regions": [
                {
                    "name": "stable_system_prefix",
                    "purpose": "Immutable workflow policy, safety, capability, and question policy.",
                    "must_not_contain": [
                        "phase state",
                        "user/artifact/validation content",
                        "rolling summaries",
                        "individual question answers",
                    ],
                },
                {
                    "name": "dynamic_context",
                    "purpose": "Phase instructions, current intent, artifacts, questions, answers, and transitions.",
                    "transport": "append after the stable prefix as the current turn context",
                },
                {
                    "name": "stable_tools",
                    "purpose": "Tools whose schema and authorization remain valid across the workflow epoch.",
                },
                {
                    "name": "phase_tools",
                    "purpose": "Phase-local write, validation, and transition tools; ordered after stable tools.",
                },
            ],
            "required_policy": DEFAULT_WORKFLOW_CACHE_POLICY,
            "authoring_rules": [
                "Declare a literal CACHE_CONTRACT in runner.py.",
                "Pass CACHE_CONTRACT as stable_system_prompt=... to every run_phase() call.",
                "Use the existing ask_user tool for material clarification and wait for its answer.",
                "Keep actual questions, answers, phase state, and artifacts dynamic.",
                "Use PhaseAnnotation/publish_phase_annotation for the runtime phase projection "
                "and checkpoint_phase_boundary after every transition; do not put either "
                "dynamic value into CACHE_CONTRACT.",
                "For filtered or dynamic phases, persist the selector and resolve the same "
                "active topology during recovery; the full plugin registry is not the "
                "checkpoint index coordinate system.",
                "Reconcile durable checkpoint/receipt/journal state before any resume prompt; "
                "transcript summaries are advisory only.",
                "Inherit WorkflowConfig.workspace_scope/workspace_access for every phase and "
                "custom path-aware tool; never construct a second scope or bypass authorization.",
                "Never insert messages into the beginning of shared conversation history.",
                "Never call _run_agent_turn directly from generated code.",
            ],
            "invalidation_reasons": [
                "phase_context_changed",
                "question_appended",
                "summary_updated",
                "history_compacted",
                "stable_contract_changed",
                "connection_changed",
                "provider_expired",
            ],
            "invalidation_reason_ownership": {
                "tracked_by_workflow_handle": [
                    "initial",
                    "phase_context_changed",
                    "stable_contract_changed",
                    "connection_changed",
                ],
                "not_tracked_by_workflow_handle": [
                    "question_appended",
                    "summary_updated",
                    "history_compacted",
                    "provider_expired",
                ],
            },
            "provider_behavior": {
                "anthropic": "explicit stable system/tool prefix when supported",
                "openai-compatible": "deterministic prefix for automatic provider caching",
                "modal": "OpenAI-compatible automatic caching semantics when the endpoint supports them",
                "ollama/litellm": "logical contract preserved; provider cache may be unsupported",
            },
        }

    @tool_read
    @_tool()
    async def show_workflow_template() -> dict[str, object]:
        """Return the cache-stable custom-runner template."""

        return {
            "style": "cache-stable-runner",
            "path": ".agenthicc/workflows/release_check/runner.py",
            "entry_point": ".agenthicc/workflows/release_check/runner.py",
            "source": _RUNNER_EXAMPLE,
            "required_call": "run_phase(..., stable_system_prompt=CACHE_CONTRACT, system_prompt=phase_prompt)",
            "note": (
                "Copy the stable CACHE_CONTRACT literally, keep phase-specific data dynamic, "
                "use the existing ask_user tool for missing or ambiguous requirements, and "
                "inherit WorkflowConfig.workspace_scope/workspace_access instead of creating "
                "a second scope or bypassing authorization."
            ),
        }

    @tool_read_execute
    @_tool()
    async def validate_workflow_cache_contract(path: str) -> dict[str, object]:
        """Validate a trusted generated workflow's cache/question contract.

        Validation imports the target package exactly as the workflow loader
        does.  Top-level code in that package therefore executes; the tool is
        execute-gated even though its intended result is a read-only report.
        """

        from pathlib import Path  # noqa: PLC0415
        from agenthicc.workflows.create_workflow.validation import validate_workflow_file  # noqa: PLC0415

        report = validate_workflow_file(
            path,
            root=Path.cwd(),
            strict_cache_contract=True,
        )
        return {
            "ok": report.ok,
            "path": report.path,
            "cache_contract": report.cache_contract,
            "errors": list(report.errors),
            "warnings": list(report.warnings),
            "phase_names": list(report.phase_names),
            "categories": dict(report.categories),
            "evidence": dict(report.evidence),
            "skipped_checks": list(report.skipped_checks),
            "execution_note": (
                "The target was imported through the workflow loader; call this only for a "
                "trusted generated path because module-level code executes during validation."
            ),
        }

    tools: list[Callable[..., object]] = [
        describe_phasespec,
        list_tool_capabilities,
        list_agent_roles,
        describe_cloakbrowser_tools,
        describe_playwright_tools,
        describe_runner_pattern,
        describe_phase_lifecycle,
        show_phase_lifecycle_template,
        describe_transition_tool_pattern,
        show_example_workflow,
        describe_prompt_cache_contract,
        show_workflow_template,
        validate_workflow_cache_contract,
    ]

    if snapshot is not None:
        from agenthicc.workflows.create_workflow.catalog import explain_tool_access as _explain  # noqa: PLC0415

        @tool_read
        @_tool()
        async def describe_authoring_session() -> dict[str, object]:
            """Describe the bounded effective tool/session contract for this run."""
            return snapshot.to_dict()

        @tool_read
        @_tool()
        async def explain_authoring_tool_access(tool_name: str) -> dict[str, object]:
            """Explain the mode/phase decision for one tool without executing it."""
            entry = next((item for item in snapshot.tools if item.name == tool_name), None)
            if entry is None:
                return {
                    "ok": False,
                    "error": f"tool {tool_name!r} is not in this authoring snapshot",
                    "available": False,
                }
            policy_constraints = [
                f"workspace policy: {snapshot.workspace.get('policy', 'unknown')}",
                "network/browser/MCP policy is session-owned and cannot be widened by the workflow",
            ]
            if entry.source == "browser":
                policy_constraints.append(
                    f"browser status: {snapshot.browser.get('dependency_status', 'unknown')}"
                )
            elif entry.source == "mcp":
                policy_constraints.append("MCP server availability is session-owned")
            decision = _explain(
                entry,
                active_mode=snapshot,
                phase_capabilities=snapshot.phase_capabilities,
                policy_constraints=policy_constraints,
            )
            return {"ok": True, **decision.to_dict()}

        tools.extend([describe_authoring_session, explain_authoring_tool_access])

    return tools
