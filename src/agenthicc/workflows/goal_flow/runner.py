"""goal_flow — clarify intent into goals, implement and verify each goal, then summarize.

The runner below is a state machine: the agent first clarifies the intent by
asking focused questions, then decides an ordered list of Goals. Each goal
becomes its own implement -> verify cycle; verification loops back to
implementation (unbounded retries) until the goal is satisfied, and only then
does the cursor move to the next goal. When every goal is satisfied the agent
writes a concise summary of what was done and which files were affected.

Transitions happen only because a phase tool was called — the state methods
check an ``asyncio.Event`` after the turn and never parse the agent's prose.
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

#: Bounded tool-wait ceiling per phase invocation — never loop forever waiting
#: for a transition tool call. Goal verification itself retries without a
#: ceiling across phase invocations, but each invocation is still bounded.
_MAX_ATTEMPTS = 5

# Stable workflow policy.  Keep phase-specific instructions and current
# artifacts (clarification notes, goals, verification evidence) in the dynamic
# ``system_prompt`` / ``text`` arguments to ``run_phase``.
CACHE_CONTRACT = """\
[WORKFLOW EXECUTION CONTRACT]
Keep the original user goal and the complete prior conversation in mind.
Phase state, artifacts, questions, answers, and transition details are dynamic
context; do not treat them as permanent workflow policy.

[REQUIREMENTS CLARIFICATION POLICY]
Ask the user a focused clarifying question through the existing `ask_user` tool
whenever required information is missing, ambiguous, or would materially change
the result. Wait for the answer and do not guess over a material ambiguity.
The question policy is stable; each actual question and answer remains dynamic.

[CACHE SAFETY POLICY]
Keep stable instructions and stable tool schemas deterministic. Do not insert
messages near the beginning of conversation history, rewrite old messages, or
put a rolling summary into the stable system prompt. Prompt caching never
replaces capability filtering, approval, or tool authorization.

[WORKSPACE POLICY]
Use the parent session's `WorkflowConfig.workspace_scope` and
`WorkflowConfig.workspace_access` unchanged for every filesystem, mention, Git,
and command-working-directory access. Never construct a second workspace scope,
allow-list, or unrestricted sandbox inside this workflow, and never use raw
filesystem I/O to bypass the workspace policy.
""".strip()


class GoalState(Enum):
    """Every state this workflow can be in."""

    CLARIFY = auto()
    DECIDE_GOALS = auto()
    IMPLEMENT_GOAL = auto()
    VERIFY_GOAL = auto()
    SUMMARIZE = auto()
    COMPLETE = auto()  # terminal
    FAILED = auto()  # terminal

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (GoalState.COMPLETE, GoalState.FAILED)


@dataclasses.dataclass
class GoalContext:
    """Data carried across every phase of one run."""

    intent: str
    run_id: str = ""
    state: GoalState = GoalState.CLARIFY
    phase_iteration: int = 0
    clarification_notes: str = ""
    goals: list[str] = dataclasses.field(default_factory=list)
    goal_index: int = 0
    # Per-goal records, index-aligned with ``goals``.
    goal_attempts: list[int] = dataclasses.field(default_factory=list)
    goal_evidence: list[str] = dataclasses.field(default_factory=list)
    goal_files: list[list[str]] = dataclasses.field(default_factory=list)
    # Goal completion is a stronger durability boundary than ordinary phase
    # entry: implementation and verification have both succeeded, so the
    # next resume must never return to that goal.  These lists are persisted in
    # the custom checkpoint payload as an auditable, index-aligned record.
    completed_goal_indices: list[int] = dataclasses.field(default_factory=list)
    goal_checkpoint_revisions: list[int] = dataclasses.field(default_factory=list)
    summary: str = ""
    files_affected: list[str] = dataclasses.field(default_factory=list)
    fail_reason: str = ""
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: "ShortTermMemory | None" = dataclasses.field(default=None, repr=False)


def _make_clarify_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the clarify phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def complete_clarification(notes: str) -> dict[str, object]:
        """Record the clarification notes and advance to goal decision.

        Args:
            notes: A summary of the questions asked and the answers received.
        """
        if not notes.strip():
            return {
                "ok": False,
                "error": "The notes were rejected: they must not be empty.",
                "fix": "Call complete_clarification(notes) with a summary of your questions and answers.",
            }
        data["notes"] = notes.strip()
        event.set()
        return {
            "ok": True,
            "message": "Clarification recorded. The goal-decision phase starts next.",
        }

    return [complete_clarification]


def _make_decide_goals_tools(
    event: asyncio.Event,
    data: dict[str, list[str]],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the decide-goals phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def finalize_goals(goals: list[str]) -> dict[str, object]:
        """Record the ordered goals and advance to implementing goal #0.

        Args:
            goals: The concrete, testable goals to satisfy, in order.
        """
        cleaned = [g.strip() for g in goals if g and g.strip()]
        if not cleaned:
            return {
                "ok": False,
                "error": "The goals were rejected: at least one non-empty goal is required.",
                "fix": "Call finalize_goals(goals) with one or more concrete goals.",
            }
        data["goals"] = cleaned
        event.set()
        return {
            "ok": True,
            "message": f"{len(cleaned)} goals recorded. Implementation starts with goal #0.",
        }

    return [finalize_goals]


def _make_implement_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the implement-goal phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def goal_implemented(summary: str, files: list[str]) -> dict[str, object]:
        """Signal that the current goal has been implemented.

        Args:
            summary: What was implemented for the current goal.
            files: The files created or modified for this goal.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "The summary was rejected: it must not be empty.",
                "fix": "Call goal_implemented(summary, files) describing what you implemented.",
            }
        data["summary"] = summary.strip()
        data["files"] = [f.strip() for f in files if f and f.strip()]
        event.set()
        return {"ok": True, "message": "Implementation recorded. Verification starts next."}

    return [goal_implemented]


def _make_verify_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the pass/retry decision tools for the verify-goal phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def verify_goal(satisfied: bool, evidence: str) -> dict[str, object]:
        """Report whether the current goal is satisfied.

        Args:
            satisfied: True when the goal is fully satisfied, False to retry.
            evidence: The checks, test results, or inspection that support the verdict.
        """
        if not evidence.strip():
            return {
                "ok": False,
                "error": "The evidence was rejected: it must not be empty.",
                "fix": "Call verify_goal(satisfied, evidence) with concrete evidence.",
            }
        data["satisfied"] = bool(satisfied)
        data["evidence"] = evidence.strip()
        event.set()
        return {"ok": True, "message": "Verdict recorded."}

    return [verify_goal]


def _make_summarize_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the summarize phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def complete_workflow(summary: str, files: list[str]) -> dict[str, object]:
        """Record the final summary and finish the workflow.

        Args:
            summary: A concise summary of everything that was done.
            files: The complete list of files affected by the run.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "The summary was rejected: it must not be empty.",
                "fix": "Call complete_workflow(summary, files) with the final summary.",
            }
        data["summary"] = summary.strip()
        data["files"] = [f.strip() for f in files if f and f.strip()]
        event.set()
        return {"ok": True, "message": "Workflow complete."}

    return [complete_workflow]


class GoalFlowRunner(CodePlanRunner):
    """State-machine runner for goal_flow.

    Subclasses ``CodePlanRunner`` purely to inherit its session wiring and the
    public ``run_phase()`` helper. ``super().run()`` is never called, so none of
    code_plan's own phases execute — this runner owns the whole flow.
    """

    workflow_name = "goal_flow"
    total_phases = 5

    # ------------------------------------------------------------------ driver
    async def run(self, intent: str) -> GoalContext:
        """Drive clarify -> decide -> per-goal implement/verify -> summarize."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        ctx = GoalContext(
            intent=intent,
            run_id=run_id,
            state=GoalState.CLARIFY,
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
                case GoalState.CLARIFY:
                    state = await self._clarify(ctx, memory)
                case GoalState.DECIDE_GOALS:
                    state = await self._decide_goals(ctx, memory)
                case GoalState.IMPLEMENT_GOAL:
                    state = await self._implement_goal(ctx, memory)
                case GoalState.VERIFY_GOAL:
                    state = await self._verify_goal(ctx, memory)
                case GoalState.SUMMARIZE:
                    state = await self._summarize(ctx, memory)
            log.info("goal_flow \u2192 %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> GoalContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, GoalContext):
            raise TypeError("goal_flow resume requires GoalContext")
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else context.shared_memory
        )
        if memory is None:
            memory = ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
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
                case GoalState.CLARIFY:
                    state = await self._clarify(context, memory)
                case GoalState.DECIDE_GOALS:
                    state = await self._decide_goals(context, memory)
                case GoalState.IMPLEMENT_GOAL:
                    state = await self._implement_goal(context, memory)
                case GoalState.VERIFY_GOAL:
                    state = await self._verify_goal(context, memory)
                case GoalState.SUMMARIZE:
                    state = await self._summarize(context, memory)
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: GoalState) -> int:
        return {
            GoalState.CLARIFY: 0,
            GoalState.DECIDE_GOALS: 1,
            GoalState.IMPLEMENT_GOAL: 2,
            GoalState.VERIFY_GOAL: 3,
            GoalState.SUMMARIZE: 4,
        }.get(state, 0)

    def _checkpoint_completed_goal(
        self,
        ctx: GoalContext,
        goal_index: int,
        next_state: GoalState,
    ) -> None:
        """Persist the exact boundary after one goal is implemented and verified.

        The state and handle cursor are moved to ``next_state`` before the
        checkpoint is serialized.  If the process disappears immediately
        after verification, recovery therefore starts with the next goal (or
        summary) and cannot replay the completed goal's side effects.
        """
        if goal_index in ctx.completed_goal_indices:
            return

        ctx.completed_goal_indices.append(goal_index)
        handle = self._cfg.workflow_handle
        if handle is None:
            # Headless callers without a session-owned handle have no durable
            # checkpoint store.  The typed context still records completion so
            # a caller that supplies a handle later cannot lose the boundary.
            ctx.state = next_state
            return
        if not handle.checkpoint_supported:
            raise RuntimeError(
                "goal_flow cannot checkpoint a completed goal because its "
                "workflow context codec is unavailable"
            )

        ctx.state = next_state
        handle.attach_context(ctx)
        # ``update_phase(..., persist=False)`` selects the continuation cursor
        # without creating an intermediate checkpoint. The goal-completion
        # save below is the single durable boundary for this transition.
        handle.update_phase(
            next_state.name.lower(),
            self._phase_index(next_state),
            ctx.phase_iteration,
            persist=False,
        )
        expected_revision = handle.checkpoint_revision + 1
        ctx.goal_checkpoint_revisions.append(expected_revision)
        checkpoint = handle.save_checkpoint(reason=f"goal_{goal_index + 1}_completed")
        if checkpoint.revision != expected_revision:  # pragma: no cover - defensive invariant
            raise RuntimeError(
                "goal_flow checkpoint revision advanced unexpectedly while "
                f"completing goal {goal_index + 1}"
            )

    # ------------------------------------------------------------- state steps
    async def _clarify(self, ctx: GoalContext, memory: object) -> GoalState:
        """Loop until complete_clarification fires; return DECIDE_GOALS or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=ctx.intent if attempt == 1 else "Call complete_clarification(notes) now.",
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the CLARIFY phase of goal_flow. Ask as many focused "
                    "clarifying questions as needed through the existing `ask_user` tool "
                    "until the intent is unambiguous; wait for each answer and do not "
                    "guess. When you have enough detail, call "
                    "complete_clarification(notes). Only a successful "
                    "complete_clarification(notes) call changes phase; prose such as "
                    "'done' never advances the workflow. You MUST call it."
                ),
                max_turns=10,
                shared_memory=memory,
                tools=_make_clarify_tools(event, data),
            )
            if event.is_set():
                ctx.clarification_notes = data["notes"]
                return GoalState.DECIDE_GOALS

        ctx.fail_reason = "clarify phase never called complete_clarification()"
        return GoalState.FAILED

    async def _decide_goals(self, ctx: GoalContext, memory: object) -> GoalState:
        """Loop until finalize_goals fires; return IMPLEMENT_GOAL or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, list[str]] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Clarification notes:\n{ctx.clarification_notes}"
                    if attempt == 1
                    else "Call finalize_goals(goals) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the DECIDE_GOALS phase of goal_flow. Turn the intent and "
                    "clarification notes into an ordered list of concrete, testable "
                    "goals, then call finalize_goals(goals). Only a successful "
                    "finalize_goals(goals) call changes phase; prose such as 'done' "
                    "never advances the workflow. You MUST call it."
                ),
                max_turns=6,
                shared_memory=memory,
                tools=_make_decide_goals_tools(event, data),
            )
            if event.is_set():
                ctx.goals = data["goals"]
                ctx.goal_index = 0
                ctx.goal_attempts = [0] * len(ctx.goals)
                ctx.goal_evidence = [""] * len(ctx.goals)
                ctx.goal_files = [[] for _ in ctx.goals]
                return GoalState.IMPLEMENT_GOAL

        ctx.fail_reason = "decide-goals phase never called finalize_goals()"
        return GoalState.FAILED

    async def _implement_goal(self, ctx: GoalContext, memory: object) -> GoalState:
        """Loop until goal_implemented fires; return VERIFY_GOAL or FAILED."""
        idx = ctx.goal_index
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            current_goal = ctx.goals[idx] if idx < len(ctx.goals) else ""
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Goal #{idx + 1} of {len(ctx.goals)}:\n{current_goal}\n\n"
                    f"Prior attempts for this goal: {ctx.goal_attempts[idx]}"
                    if attempt == 1
                    else "Call goal_implemented(summary, files) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the IMPLEMENT_GOAL phase of goal_flow. Work toward the "
                    "current goal using the file, shell, and git tools: read relevant "
                    "code, write or modify files, and run commands as needed. When the "
                    "goal is implemented, call goal_implemented(summary, files). Only a "
                    "successful goal_implemented(summary, files) call changes phase; "
                    "prose such as 'done' never advances the workflow. You MUST call it."
                ),
                mode="Yolo",  # unlock write / execute tools for this phase
                max_turns=25,
                shared_memory=memory,
                tools=_make_implement_tools(event, data),
            )
            if event.is_set():
                ctx.goal_attempts[idx] += 1
                if idx < len(ctx.goal_evidence):
                    ctx.goal_evidence[idx] = str(data.get("summary", ""))
                files = data.get("files", [])
                if isinstance(files, list):
                    if idx < len(ctx.goal_files):
                        ctx.goal_files[idx] = [str(f) for f in files]
                return GoalState.VERIFY_GOAL

        ctx.fail_reason = "implement phase never called goal_implemented()"
        return GoalState.FAILED

    async def _verify_goal(self, ctx: GoalContext, memory: object) -> GoalState:
        """Loop until verify_goal fires; branch to next goal, retry, or FAILED."""
        idx = ctx.goal_index
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            current_goal = ctx.goals[idx] if idx < len(ctx.goals) else ""
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Goal #{idx + 1} of {len(ctx.goals)}:\n{current_goal}\n\n"
                    f"Implementation summary:\n{ctx.goal_evidence[idx]}"
                    if attempt == 1
                    else "Call verify_goal(satisfied, evidence) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_GOAL phase of goal_flow. Check that the "
                    "current goal is actually satisfied: inspect the code, run tests or "
                    "commands, and gather concrete evidence. Then call "
                    "verify_goal(satisfied, evidence). Only a successful "
                    "verify_goal(satisfied, evidence) call changes phase; prose such as "
                    "'done' never advances the workflow. You MUST call it."
                ),
                max_turns=12,
                shared_memory=memory,
                tools=_make_verify_tools(event, data),
            )
            if event.is_set():
                satisfied = bool(data.get("satisfied", False))
                evidence = str(data.get("evidence", ""))
                if idx < len(ctx.goal_evidence):
                    ctx.goal_evidence[idx] = evidence
                if not satisfied:
                    # Loop back to the same goal's implementation phase (unbounded).
                    return GoalState.IMPLEMENT_GOAL
                if idx + 1 >= len(ctx.goals):
                    next_state = GoalState.SUMMARIZE
                else:
                    ctx.goal_index = idx + 1
                    next_state = GoalState.IMPLEMENT_GOAL
                self._checkpoint_completed_goal(ctx, idx, next_state)
                return next_state

        ctx.fail_reason = "verify phase never called verify_goal()"
        return GoalState.FAILED

    async def _summarize(self, ctx: GoalContext, memory: object) -> GoalState:
        """Loop until complete_workflow fires; return COMPLETE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            goals_block = "\n".join(
                f"- {g}  [attempts={ctx.goal_attempts[i]}]" for i, g in enumerate(ctx.goals)
            )
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"All goals satisfied:\n{goals_block}\n\n"
                    f"Files affected so far: {ctx.files_affected}"
                    if attempt == 1
                    else "Call complete_workflow(summary, files) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the SUMMARIZE phase of goal_flow. Write a concise final "
                    "summary of everything that was done and every file affected, then "
                    "call complete_workflow(summary, files). Only a successful "
                    "complete_workflow(summary, files) call changes phase; prose such as "
                    "'done' never advances the workflow. You MUST call it."
                ),
                max_turns=4,
                shared_memory=memory,
                tools=_make_summarize_tools(event, data),
            )
            if event.is_set():
                ctx.summary = str(data.get("summary", ""))
                files = data.get("files", [])
                if isinstance(files, list):
                    ctx.files_affected = [str(f) for f in files]
                return GoalState.COMPLETE

        ctx.fail_reason = "summarize phase never called complete_workflow()"
        return GoalState.FAILED


@dataclasses.dataclass
class GoalFlowParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.goal_flow]."""

    clarify_model: str = ""
    decide_goals_model: str = ""
    implement_model: str = ""
    verify_model: str = ""
    summarize_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "clarify": self.clarify_model,
            "decide_goals": self.decide_goals_model,
            "implement_goal": self.implement_model,
            "verify_goal": self.verify_model,
            "summarize": self.summarize_model,
        }


class GoalFlowWorkflow(WorkflowPlugin):
    """Clarify intent into goals, implement and verify each goal, then summarize."""

    name = "goal_flow"
    description = "Clarify intent into goals, implement and verify each goal, then summarize."
    mode_bindings: list[str] = []  # manual only — invoke with /workflow goal_flow
    # Declarative metadata for the registry and the TUI phase counter; the runner
    # above is what actually executes, and it follows exactly this graph (the
    # runner additionally loops implement -> verify per goal at runtime).
    phases = [
        PhaseSpec(
            name="clarify",
            max_turns=10,
            next="decide_goals",
            system_prompt_override=(
                "You are in the CLARIFY phase of goal_flow. Ask clarifying questions "
                "through the existing `ask_user` tool as needed, then call "
                "complete_clarification(notes); only a successful transition-tool call "
                "changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="decide_goals",
            max_turns=6,
            next="implement_goal",
            system_prompt_override=(
                "You are in the DECIDE_GOALS phase of goal_flow. Decide the ordered "
                "goals and call finalize_goals(goals); only a successful "
                "transition-tool call changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="implement_goal",
            max_turns=25,
            next="verify_goal",
            mode_override="Yolo",
            system_prompt_override=(
                "You are in the IMPLEMENT_GOAL phase of goal_flow. Implement the "
                "current goal, then call goal_implemented(summary, files); only a "
                "successful transition-tool call changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="verify_goal",
            max_turns=12,
            next="implement_goal",
            system_prompt_override=(
                "You are in the VERIFY_GOAL phase of goal_flow. Verify the current "
                "goal is satisfied, then call verify_goal(satisfied, evidence); only a "
                "successful transition-tool call changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="summarize",
            max_turns=4,
            output_schema="free_text",
            system_prompt_override=(
                "You are in the SUMMARIZE phase of goal_flow. Write the final summary "
                "and call complete_workflow(summary, files); only a successful "
                "transition-tool call changes phase, never prose."
            ),
        ),
    ]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, GoalContext):
            raise TypeError("goal_flow checkpoint requires GoalContext")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
            "clarification_notes": context.clarification_notes,
            "goals": context.goals,
            "goal_index": context.goal_index,
            "goal_attempts": context.goal_attempts,
            "goal_evidence": context.goal_evidence,
            "goal_files": context.goal_files,
            "completed_goal_indices": context.completed_goal_indices,
            "goal_checkpoint_revisions": context.goal_checkpoint_revisions,
            "summary": context.summary,
            "files_affected": context.files_affected,
            "fail_reason": context.fail_reason,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> GoalContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", GoalState.CLARIFY.name))
        try:
            state = GoalState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown goal_flow state: {raw_state}") from exc

        def _str_list(value: object) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(item) for item in value]

        def _int_value(value: object, default: int = 0) -> int:
            """Decode a scalar checkpoint value without trusting JSON types."""
            if isinstance(value, bool):
                return default
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    return default
            return default

        raw_files = payload.get("goal_files", [])
        goal_files: list[list[str]] = []
        if isinstance(raw_files, list):
            for entry in raw_files:
                goal_files.append(_str_list(entry) if isinstance(entry, list) else [])

        return GoalContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            state=state,
            phase_iteration=_int_value(payload.get("phase_iteration", 0)),
            clarification_notes=str(payload.get("clarification_notes", "")),
            goals=_str_list(payload.get("goals")),
            goal_index=_int_value(payload.get("goal_index", 0)),
            goal_attempts=[_int_value(v) for v in _str_list(payload.get("goal_attempts"))]
            if isinstance(payload.get("goal_attempts"), list)
            else [],
            goal_evidence=_str_list(payload.get("goal_evidence")),
            goal_files=goal_files,
            completed_goal_indices=[
                _int_value(value) for value in _str_list(payload.get("completed_goal_indices"))
            ],
            goal_checkpoint_revisions=[
                _int_value(value) for value in _str_list(payload.get("goal_checkpoint_revisions"))
            ],
            summary=str(payload.get("summary", "")),
            files_affected=_str_list(payload.get("files_affected")),
            fail_reason=str(payload.get("fail_reason", "")),
            shared_memory=memory,
        )

    @classmethod
    def build_runner(
        cls,
        config: "WorkflowConfig",
        mode_manager: "ModeManager | None",
    ) -> GoalFlowRunner:
        """Return this workflow's own state-machine runner."""
        return GoalFlowRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.goal_flow]."""
        return GoalFlowParams(
            clarify_model=str(source.get("clarify_model", "") or ""),
            decide_goals_model=str(source.get("decide_goals_model", "") or ""),
            implement_model=str(source.get("implement_model", "") or ""),
            verify_model=str(source.get("verify_model", "") or ""),
            summarize_model=str(source.get("summarize_model", "") or ""),
        )
