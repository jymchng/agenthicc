"""Read-only inspection tools for the create_workflow design phase.

These give the authoring agent *ground truth* about the workflow-plugin API —
the ``PhaseSpec`` field list is read live from the dataclass and the capability
and role lists are read live from their enums, so the guidance can never drift
from the real contract.  A canonical, known-valid example workflow is included
so the agent has a correct template to adapt rather than one it hallucinates.

All tools here are read-only: they take no session state and cause no side
effects, so they are safe to inject into any phase.
"""

from __future__ import annotations

from collections.abc import Callable

# Curated one-line purposes for PhaseSpec fields.  The field *names* come from
# the live dataclass (see describe_phasespec); this mapping only supplies human
# purpose text, and a field with no entry simply reports an empty purpose.
_PHASESPEC_PURPOSE: dict[str, str] = {
    "name": "Unique phase id within the workflow; referenced by next / on_reject.",
    "agent_type": (
        "Registry key selecting the default prompt and allowed capabilities: "
        "auto, planner, executor, reviewer, explorer, verifier, human, custom."
    ),
    "system_prompt_override": "Replaces the role's default system prompt entirely for this phase.",
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
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

#: Bounded retries per phase — never loop forever waiting for a tool call.
_MAX_ATTEMPTS = 5


class ReleaseState(Enum):
    """Every state this workflow can be in."""

    PLAN = auto()
    VERIFY = auto()
    REPORT = auto()
    COMPLETE = auto()  # terminal
    FAILED = auto()  # terminal

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
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: ShortTermMemory | None = dataclasses.field(default=None, repr=False)


def _make_plan_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the plan phase."""
    from lauren_ai._tools import tool

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
    total_phases = 3

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
            if handle is not None:
                handle.attach_context(ctx)
                handle.update_phase(
                    state.name.lower(), self._phase_index(state), ctx.phase_iteration
                )
            match state:
                case ReleaseState.PLAN:
                    state = await self._plan(ctx, memory)
                case ReleaseState.VERIFY:
                    state = await self._verify(ctx, memory)
                case ReleaseState.REPORT:
                    state = await self._report(ctx, memory)
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
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
        state = context.state
        while not state.is_terminal:
            context.state = state
            context.phase_iteration += 1
            if handle is not None:
                handle.attach_context(context)
                handle.update_phase(
                    state.name.lower(), self._phase_index(state), context.phase_iteration
                )
            match state:
                case ReleaseState.PLAN:
                    state = await self._plan(context, memory)
                case ReleaseState.VERIFY:
                    state = await self._verify(context, memory)
                case ReleaseState.REPORT:
                    state = await self._report(context, memory)
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
                system_prompt=(
                    "You are in the PLAN phase of release_check. List the checks that must "
                    "pass before this release, then call submit_release_plan(plan). Do not "
                    "run the checks yet."
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
                system_prompt=(
                    "You are in the VERIFY phase of release_check. Run the planned checks "
                    "with the shell tools, then call release_passed(summary) or "
                    "release_blocked(blocker). You MUST call one of them."
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
            system_prompt=(
                "You are in the REPORT phase of release_check. Summarise what was checked "
                "and whether the release is clear to ship."
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
    # Declarative metadata for the registry and the TUI phase counter; the runner
    # above is what actually executes, and it follows exactly this graph.
    phases = [
        PhaseSpec(
            name="plan",
            max_turns=10,
            next="verify",
            system_prompt_override="You are in the PLAN phase of release_check.",
        ),
        PhaseSpec(
            name="verify",
            max_turns=20,
            next="report",
            mode_override="Yolo",
            system_prompt_override="You are in the VERIFY phase of release_check.",
        ),
        PhaseSpec(
            name="report",
            max_turns=4,
            output_schema="free_text",
            system_prompt_override="You are in the REPORT phase of release_check.",
        ),
    ]

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
                "using the write tools, then briefly state what you wrote and where."
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
                "whether it satisfies the request, noting any gaps."
            ),
        ),
    ]
'''


def make_inspection_tools() -> list[Callable[..., object]]:
    """Return the read-only authoring-surface inspection tools.

    ``[describe_phasespec, list_tool_capabilities, list_agent_roles, show_example_workflow]``
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_read  # noqa: PLC0415

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
        return {"phasespec_fields": fields}

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
        return {"capabilities": caps}

    @tool_read
    @_tool()
    async def list_agent_roles() -> dict[str, object]:
        """List the agent_type values a phase may use.

        Returns the PhaseRole constants read live from the code.  Most phases use
        'auto'; the others map to role-specific default prompts and capabilities.
        """
        from agenthicc.workflows.plugin import PhaseRole  # noqa: PLC0415

        roles = [
            value
            for name, value in vars(PhaseRole).items()
            if not name.startswith("_") and isinstance(value, str)
        ]
        return {"agent_types": roles}

    @tool_read
    @_tool()
    async def show_example_workflow(style: str = "runner") -> dict[str, object]:
        """Return a complete, known-valid example workflow file to adapt.

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
                "path": ".agenthicc/workflows/doc_review.py",
                "source": _DECLARATIVE_EXAMPLE,
                "note": (
                    "No runner: every phase is one unconditional agent turn. If any phase "
                    "needs a retry, a branch, a loop, or its own transition tool, use "
                    "show_example_workflow('runner') instead."
                ),
            }
        return {
            "style": "runner",
            "path": ".agenthicc/workflows/release_check.py",
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
                "resume(context): restore and re-enter the same dispatch path",
                "checkpoint_context_to_payload(context) and "
                "checkpoint_context_from_payload(payload, memory=None) on the plugin; "
                "omit session memory from JSON and reattach the supplied memory on restore",
                "phase tool factories that set an asyncio.Event; the state method checks "
                "the event after the turn returns and never parses the agent's prose",
                "build_runner() on the plugin returning the runner class",
            ],
            "turn_api": (
                "Subclass CodePlanRunner and call the public "
                "run_phase(intent=, text=, system_prompt=, mode=, max_turns=, "
                "shared_memory=, tools=) once per phase. Use the injected "
                "WorkflowConfig.session_memory for every call (with a local fallback only "
                "when no session memory exists), and pass one object to every phase. "
                "Attach the typed context to config.workflow_handle and update its phase "
                "cursor. Never call super().run() — that would execute code_plan's own "
                "phases."
            ),
            "reference_implementations": [
                "agenthicc.workflows.code_plan.runner:CodePlanRunner",
                "agenthicc.workflows.create_workflow.runner:CreateWorkflowRunner",
            ],
            "example": "show_example_workflow('runner')",
        }

    return [
        describe_phasespec,
        list_tool_capabilities,
        list_agent_roles,
        describe_runner_pattern,
        show_example_workflow,
    ]
