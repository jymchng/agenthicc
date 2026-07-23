"""CodePlanRunner — explicit state-machine runner for the code_plan workflow.

The state machine mirrors the pattern:

    state = CodePlanState.PLAN
    while not state.is_terminal:
        match state:
            case PLAN:      state = await self._plan(ctx)
            case EXECUTE:   state = await self._execute(ctx)
            case REVIEW:    state = await self._review(ctx)
            case SUMMARIZE: state = await self._summarize(ctx)

Each phase method owns its own retry loop and returns the next state.
All routing is explicit Python — no string lookups, no tristate `approved`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from typing import TYPE_CHECKING

from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.tools.base import ToolLike

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.plugin import WorkflowRun
    from agenthicc.tui.runtime.mode_manager import ModeManager

log = logging.getLogger(__name__)

# ── type alias for tool objects (no formal base class in lauren-ai yet) ────────
# Gap (lauren-ai): tools are @_tool()-decorated callables with no public ABC.
# list[object] is the most precise non-Any annotation available.
_ToolList = list[ToolLike]

# ── default retry caps ────────────────────────────────────────────────────────

_MAX_PLAN_ATTEMPTS: int = 10
_MAX_EXECUTE_ATTEMPTS: int = 10
_MAX_REVIEW_ATTEMPTS: int = 10

# ── system prompts ────────────────────────────────────────────────────────────

_PLAN_PROMPT: str = (
    "You are in the PLANNING phase. The original user intent is provided below. "
    "Explore only the parts of the codebase directly relevant to it — do not "
    "do a general repository survey. Produce a focused implementation plan that "
    "addresses exactly what was asked, nothing more. Use request_plan_approval() "
    "to present the plan for human review, and finalize_plan() once it is approved.\n\n"
    "If the user's message is a question, an explanation request, a read-only "
    "query, or any intent that cannot be turned into concrete implementation steps, "
    "call exit_code_plan(suggestion) immediately — do not attempt to produce a plan. "
    "Reserve planning for tasks that genuinely require file changes, commands, or "
    "multi-step implementation."
)
_PLAN_REMINDER: str = (
    "You have not yet finalized the plan. The original user intent is in your "
    "system prompt. Return to that task, develop or refine your implementation "
    "plan, present it with request_plan_approval(plan), and once approved call "
    "finalize_plan(plan)."
)

_EXECUTE_PROMPT: str = (
    "You are in the EXECUTION phase. The original user intent and the approved "
    "plan are provided below — implement the plan step by step using tools. "
    "Do NOT re-explore or re-plan. When ALL tasks are complete, call "
    "mark_execute_complete() with a brief summary. Do not stop without calling it."
)
_EXECUTE_REMINDER: str = (
    "Continue implementing — you have not yet called mark_execute_complete(). "
    "The original user intent and approved plan are in your system prompt. "
    "Resume from where you left off and complete all remaining tasks. "
    "Call mark_execute_complete() once everything is done."
)

_REVIEW_PROMPT: str = (
    "You are in the REVIEW phase. The EXECUTE phase has just completed. "
    "The original user intent, approved plan, and execution summary are provided "
    "below. Inspect the changes that were made and run the tests. Call "
    "approve_review(summary) if all tests pass and the code is correct, or "
    "reject_review(reason) if there are issues that need fixing. "
    "You MUST call one of these two tools."
)
_REVIEW_REMINDER: str = (
    "You have not yet called approve_review() or reject_review(). "
    "The original user intent, approved plan, and execution summary are in your "
    "system prompt. Review the implementation now and call approve_review(summary) "
    "if everything is correct, or reject_review(reason) if there are issues."
)

_SUMMARIZE_PROMPT: str = (
    "You are in the SUMMARY phase. The original user intent is provided below. "
    "Write a concise summary of what was planned, implemented, and verified."
)

_PHASE_INDEX: dict[str, int] = {
    "plan": 0,
    "execute": 1,
    "review": 2,
    "summarize": 3,
}


class CodePlanRunner(BaseWorkflowRunner):
    """State-machine runner for the code_plan workflow.

    Subclasses override ``workflow_name`` and ``total_phases`` so that
    ``app_state.workflow_run`` always reflects the actual running workflow name
    rather than the hardcoded ``"code_plan"`` string.

    Parameters
    ----------
    config:
        WorkflowConfig holding all session-scoped singletons.
    mode_manager:
        ModeManager for per-phase mode overrides (None = headless).
    """

    #: Workflow name written to app_state.workflow_run.  Subclasses must
    #: override this to match their WorkflowPlugin.name.
    workflow_name: str = "code_plan"
    #: Total number of phases shown in the "N/M" status-bar counter.
    total_phases: int = 4

    # Per-phase model overrides (PRD-115).  Empty string = use global execution.model.
    # Override as class attributes in subclasses, or set via TOML:
    #   [workflows.code_plan]
    #   plan_model = "deepseek-v4-pro"
    plan_model: str = ""
    execute_model: str = ""
    review_model: str = ""
    summary_model: str = ""

    def __init__(
        self,
        config: WorkflowConfig,
        mode_manager: ModeManager | None = None,
    ) -> None:
        self._cfg: WorkflowConfig = config
        self._mode_manager: ModeManager | None = mode_manager
        self._run_id: str = ""

        # Gap (lauren-ai): no public model_id accessor on AgentRunnerBase.
        _transport_cfg = getattr(getattr(config.agent_runner, "_transport", None), "_config", None)
        self._model_id: str = (
            getattr(_transport_cfg, "model", None) or config.cfg.execution.effective_model()
        )

    # ── public entry points ───────────────────────────────────────────────────

    async def run(self, intent: str) -> CodePlanContext:
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
        from agenthicc.kernel import Event  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowRun  # noqa: PLC0415

        run_id: str = uuid.uuid4().hex
        self._run_id = run_id

        ctx: CodePlanContext = CodePlanContext(
            intent=intent,
            run_id=run_id,
            shared_memory=ShortTermMemory(
                max_tokens=self._cfg.cfg.execution.effective_usable_budget()
            ),
        )

        wf_run: WorkflowRun = WorkflowRun(
            run_id=run_id,
            workflow_name=self.workflow_name,
            intent=intent,
            current_phase="plan".title(),
            total_phases=self.total_phases,
        )
        self._cfg.app_state.workflow_run.set(wf_run)

        await self._cfg.processor.emit(
            Event.create(
                "WorkflowRunStarted",
                {
                    "run_id": run_id,
                    "workflow_name": self.workflow_name,
                    "intent": intent,
                    "phase_names": ["plan", "execute", "review", "summarize"],
                },
            )
        )

        state: CodePlanState = CodePlanState.PLAN

        try:
            while not state.is_terminal:
                phase_name: str = state.name.lower()
                wf_run = dataclasses.replace(
                    wf_run,
                    current_phase=phase_name,
                    current_phase_index=_PHASE_INDEX.get(phase_name, 0),
                )
                self._cfg.app_state.workflow_run.set(wf_run)

                await self._cfg.processor.emit(
                    Event.create(
                        "WorkflowPhaseStarted",
                        {
                            "run_id": run_id,
                            "phase_name": phase_name,
                            "workflow_name": self.workflow_name,
                        },
                    )
                )

                match state:
                    case CodePlanState.PLAN:
                        state = await self._plan(ctx)
                    case CodePlanState.EXECUTE:
                        state = await self._execute(ctx)
                    case CodePlanState.REVIEW:
                        state = await self._review(ctx)
                    case CodePlanState.SUMMARIZE:
                        state = await self._summarize(ctx)

                next_label: str | None = state.name.lower() if not state.is_terminal else None
                await self._cfg.processor.emit(
                    Event.create(
                        "WorkflowPhaseCompleted",
                        {
                            "run_id": run_id,
                            "phase_name": phase_name,
                            "role": "auto",
                            "full_text": "",
                            "approved": None,
                            "structured": {},
                            "edge_label": next_label,
                        },
                    )
                )
                self._cfg.app_state.workflow_run.set(wf_run)
                log.info("code_plan: %s → %s", phase_name, state.name)

            if state == CodePlanState.COMPLETE:
                final_status = "complete"
            elif state == CodePlanState.EXITED:
                final_status = "exited"
            else:
                final_status = "failed"

            wf_run = dataclasses.replace(wf_run, status=final_status, current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)

            if state == CodePlanState.FAILED and ctx.fail_reason:
                self._cfg.conv_store.append_event(
                    "error", {"message": f"code_plan failed: {ctx.fail_reason}"}
                )

            await self._cfg.processor.emit(
                Event.create(
                    "WorkflowRunCompleted",
                    {
                        "run_id": run_id,
                        "workflow_name": self.workflow_name,
                        "phases_run": len(wf_run.phase_history),
                        "status": final_status,
                    },
                )
            )

        except (asyncio.CancelledError, KeyboardInterrupt):
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            raise
        except Exception as exc:
            log.error("CodePlanRunner error: %s", exc, exc_info=True)
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            self._cfg.conv_store.append_event("error", {"message": str(exc)})

        return ctx  # PRD-114: subclasses receive typed context via super().run()

    async def resume(self, context: object) -> None:
        """Resume from a WorkflowContext (legacy --resume path)."""
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowContext, WorkflowRun  # noqa: PLC0415

        if not isinstance(context, WorkflowContext):
            raise TypeError("workflow resume requires a WorkflowContext")

        completed: set[str] = (
            set(context.phase_outputs.keys()) if hasattr(context, "phase_outputs") else set()
        )

        ctx: CodePlanContext = CodePlanContext(
            intent=context.intent,
            run_id=context.run_id,
            shared_memory=ShortTermMemory(
                max_tokens=self._cfg.cfg.execution.effective_usable_budget()
            ),
        )

        if "plan" in completed:
            plan_output = context.phase_outputs.get("plan")
            if plan_output is not None:
                ctx.plan = plan_output.full_text

        resume_map: dict[frozenset[str], CodePlanState] = {
            frozenset(): CodePlanState.PLAN,
            frozenset({"plan"}): CodePlanState.EXECUTE,
            frozenset({"plan", "execute"}): CodePlanState.REVIEW,
            frozenset({"plan", "execute", "review"}): CodePlanState.SUMMARIZE,
        }
        state: CodePlanState = resume_map.get(frozenset(completed), CodePlanState.PLAN)

        if not completed:
            await self.run(context.intent)
            return

        self._run_id = context.run_id
        wf_run: WorkflowRun = WorkflowRun(
            run_id=context.run_id,
            workflow_name=self.workflow_name,
            intent=context.intent,
            current_phase=state.name.lower(),
            total_phases=self.total_phases,
        )
        self._cfg.app_state.workflow_run.set(wf_run)

        while not state.is_terminal:
            match state:
                case CodePlanState.PLAN:
                    state = await self._plan(ctx)
                case CodePlanState.EXECUTE:
                    state = await self._execute(ctx)
                case CodePlanState.REVIEW:
                    state = await self._review(ctx)
                case CodePlanState.SUMMARIZE:
                    state = await self._summarize(ctx)

        if state == CodePlanState.COMPLETE:
            final: str = "complete"
        elif state == CodePlanState.EXITED:
            final = "exited"
        else:
            final = "failed"
        wf_run = dataclasses.replace(wf_run, status=final, current_phase=None)
        self._cfg.app_state.workflow_run.set(wf_run)

    # ── phase methods ─────────────────────────────────────────────────────────

    async def _plan(self, ctx: CodePlanContext) -> CodePlanState:
        """Loop until finalize_plan() or exit_code_plan() fires; return EXECUTE, EXITED, or FAILED."""
        from agenthicc.workflows.code_plan.phase_tools import make_planner_tools  # noqa: PLC0415

        from agenthicc.workflows.code_plan.phase_tools import make_questions_tool  # noqa: PLC0415

        self._set_phase("plan", 0, ctx)
        exit_event: asyncio.Event = asyncio.Event()

        for attempt in range(1, _MAX_PLAN_ATTEMPTS + 1):
            plan_event: asyncio.Event = asyncio.Event()
            plan_data: dict[str, object] = {}

            tools: _ToolList = list(self._base_tools())
            tools.extend(
                make_planner_tools(
                    self._cfg.approval_svc,
                    plan_event,
                    plan_data,
                    exit_event=exit_event,
                )
            )
            tools.extend(make_questions_tool(self._cfg.approval_svc))

            text: str = ctx.intent if attempt == 1 else _PLAN_REMINDER

            try:
                await self._run_turn(
                    text,
                    tools=tools,
                    mode=None,
                    system_prompt=_PLAN_PROMPT + f"\n\n[USER INTENT]\n{ctx.intent}",
                    max_turns=20,
                    ctx=ctx,
                    model_override=self._phase_model("plan"),
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                # PRD-117: only permanent errors reach here — _stream() swallows
                # transient errors and returns normally.  Exit immediately with a
                # clear diagnostic instead of the generic "exhausted N attempts".
                ctx.fail_reason = f"{type(exc).__name__}: {exc}"
                log.error("_plan permanent error on attempt %d: %s", attempt, exc)
                return CodePlanState.FAILED

            # Exit takes priority — check before plan finalization.
            if exit_event.is_set():
                return CodePlanState.EXITED

            if plan_event.is_set() and "plan" in plan_data:
                plan = plan_data["plan"]
                if isinstance(plan, str):
                    ctx.plan = plan
                return CodePlanState.EXECUTE

        ctx.fail_reason = (
            f"Plan phase exhausted {_MAX_PLAN_ATTEMPTS} attempts without finalization."
        )
        return CodePlanState.FAILED

    async def _execute(self, ctx: CodePlanContext) -> CodePlanState:
        """Loop until mark_execute_complete() fires; return REVIEW or FAILED."""
        from agenthicc.workflows.code_plan.phase_tools import make_executor_tools  # noqa: PLC0415

        self._set_phase("execute", 1, ctx)

        # Embed intent + approved plan in the system prompt so every retry turn
        # has full context, independent of conversation-history trimming.
        system_prompt: str = (
            _EXECUTE_PROMPT
            + f"\n\n[USER INTENT]\n{ctx.intent}"
            + f"\n\n[APPROVED PLAN]\n{ctx.plan}"
        )

        for attempt in range(1, _MAX_EXECUTE_ATTEMPTS + 1):
            execute_event: asyncio.Event = asyncio.Event()
            execute_data: dict[str, object] = {}

            tools: _ToolList = list(self._base_tools()) + list(
                make_executor_tools(execute_event, execute_data)
            )
            text: str = (
                f"[PLAN]\n{ctx.plan}\n\nTask: {ctx.intent}" if attempt == 1 else _EXECUTE_REMINDER
            )

            try:
                await self._run_turn(
                    text,
                    tools=tools,
                    mode="Auto",
                    system_prompt=system_prompt,
                    max_turns=40,
                    ctx=ctx,
                    model_override=self._phase_model("execute"),
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                # PRD-117: permanent error — exit immediately with clear reason.
                ctx.fail_reason = f"{type(exc).__name__}: {exc}"
                log.error("_execute permanent error on attempt %d: %s", attempt, exc)
                return CodePlanState.FAILED

            if execute_event.is_set():
                summary = execute_data.get("summary", "")
                ctx.execute_summary = summary if isinstance(summary, str) else ""
                return CodePlanState.REVIEW

        ctx.fail_reason = f"Execute phase exhausted {_MAX_EXECUTE_ATTEMPTS} attempts."
        return CodePlanState.FAILED

    async def _review(self, ctx: CodePlanContext) -> CodePlanState:
        """Loop until approve_review() or reject_review() fires; return SUMMARIZE, EXECUTE, or FAILED."""
        from agenthicc.workflows.code_plan.phase_tools import make_reviewer_tools  # noqa: PLC0415

        self._set_phase("review", 2, ctx)

        # Embed accumulated context in the system prompt so every retry turn
        # has the full picture, independent of conversation-history trimming.
        system_prompt: str = _REVIEW_PROMPT + f"\n\n[USER INTENT]\n{ctx.intent}"
        if ctx.plan:
            system_prompt += f"\n\n[APPROVED PLAN]\n{ctx.plan}"
        if ctx.execute_summary:
            system_prompt += f"\n\n[EXECUTION SUMMARY]\n{ctx.execute_summary}"

        for attempt in range(1, _MAX_REVIEW_ATTEMPTS + 1):
            review_event: asyncio.Event = asyncio.Event()
            review_data: dict[str, object] = {}

            tools: _ToolList = list(self._base_tools()) + list(
                make_reviewer_tools(review_event, review_data)
            )
            text: str = (
                (
                    f"Execution complete. Review the implementation for: {ctx.intent}"
                    + (
                        f"\n\nExecution summary: {ctx.execute_summary}"
                        if ctx.execute_summary
                        else ""
                    )
                )
                if attempt == 1
                else _REVIEW_REMINDER
            )

            try:
                await self._run_turn(
                    text,
                    tools=tools,
                    mode=None,
                    system_prompt=system_prompt,
                    max_turns=8,
                    ctx=ctx,
                    model_override=self._phase_model("review"),
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                # PRD-117: permanent error — exit immediately with clear reason.
                ctx.fail_reason = f"{type(exc).__name__}: {exc}"
                log.error("_review permanent error on attempt %d: %s", attempt, exc)
                return CodePlanState.FAILED

            if review_event.is_set():
                action_value = review_data.get("action", "reject")
                action: str = action_value if isinstance(action_value, str) else "reject"
                if action == "approve":
                    summary = review_data.get("summary", "")
                    ctx.review_summary = summary if isinstance(summary, str) else ""
                    return CodePlanState.SUMMARIZE
                reason = review_data.get("reason", "")
                ctx.rejection_reason = reason if isinstance(reason, str) else ""
                return CodePlanState.EXECUTE

        ctx.fail_reason = f"Review phase exhausted {_MAX_REVIEW_ATTEMPTS} attempts."
        return CodePlanState.FAILED

    async def _summarize(self, ctx: CodePlanContext) -> CodePlanState:
        """Single turn; always returns COMPLETE."""
        self._set_phase("summarize", 3, ctx)
        text: str = (
            f"Task: {ctx.intent}\n\n"
            f"What was implemented: {ctx.execute_summary or '(see conversation)'}\n"
            f"Review verdict: {ctx.review_summary or 'approved'}"
        )
        try:
            await self._run_turn(
                text,
                tools=self._base_tools(),
                mode=None,
                system_prompt=_SUMMARIZE_PROMPT + f"\n\n[USER INTENT]\n{ctx.intent}",
                max_turns=4,
                ctx=ctx,
                model_override=self._phase_model("summarize"),
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            log.error("_summarize error: %s", exc)
        return CodePlanState.COMPLETE

    # ── public extension API (PRD-114) ────────────────────────────────────────

    async def run_phase(
        self,
        *,
        intent: str,
        text: str,
        system_prompt: str,
        mode: str | None = None,
        max_turns: int = 10,
        shared_memory: "ShortTermMemory | None" = None,
    ) -> None:
        """Execute one additional agent phase using this runner's tool set.

        This is the **stable public surface** for composite workflow authors.
        It delegates to the private ``_run_turn()`` + ``_base_tools()`` so
        that internal implementation details remain free to change.

        Parameters
        ----------
        intent:
            The original user intent string — included in the system prompt so
            the agent keeps the original goal in context.
        text:
            The user-turn text sent to the LLM for this phase.  Typically
            includes key outputs from prior phases (plan, execute summary, …).
        system_prompt:
            Full system-prompt for this phase.  Replaces any role default.
        mode:
            Optional mode name (e.g. ``"Auto"``) to switch for this phase.
            Restored automatically on exit.
        max_turns:
            Maximum LLM sub-turns (tool-call → response cycles).
        shared_memory:
            ``ShortTermMemory`` instance to use.  Pass ``ctx.shared_memory``
            to carry the full prior conversation context into this phase.
            ``None`` creates an isolated memory for this phase only.
        """
        from lauren_ai._memory import ShortTermMemory as _STM  # noqa: PLC0415
        from agenthicc.workflows.code_plan.state import CodePlanContext  # noqa: PLC0415

        # Build a minimal CodePlanContext so the turn has a shared_memory.
        _sm = shared_memory or _STM(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        _ctx = CodePlanContext(
            intent=intent,
            run_id=self._run_id or "extension",
            shared_memory=_sm,
        )
        # PRD-126: composite-workflow phases get transport retry too.
        await self._run_turn(
            text,
            tools=self._base_tools(),
            mode=mode,
            system_prompt=system_prompt,
            max_turns=max_turns,
            ctx=_ctx,
        )

    # ── phase helpers (PRD-115) ───────────────────────────────────────────────

    def _phase_model(self, phase_name: str) -> str:
        """Return the model override for *phase_name*, or '' for global default.

        Priority:
        1. ``WorkflowParams.model_for_phase()`` — populated from TOML / CLI.
        2. Class attribute ``{phase_name}_model`` — static subclass default.
        3. Empty string — caller falls back to global ``execution.model``.
        """
        if self._cfg.params is not None:
            m = self._cfg.params.model_for_phase(phase_name, "")
            if m:
                return m
        return getattr(self, f"{phase_name}_model", "") or ""

    def _set_phase(self, phase_name: str, phase_index: int, ctx: CodePlanContext) -> None:
        """Update all workflow TUI state for the current phase in one call."""
        self._cfg.app_state.update_workflow_phase(
            workflow_name=self.workflow_name,
            phase_name=phase_name,
            phase_index=phase_index,
            total_phases=self.total_phases,
            run_id=ctx.run_id,
            intent=ctx.intent,
            model_id=self._phase_model(phase_name) or self._model_id,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _run_turn(
        self,
        text: str,
        *,
        tools: _ToolList,
        mode: str | None,
        system_prompt: str,
        max_turns: int,
        ctx: CodePlanContext,
        model_override: str = "",
    ) -> None:
        """Run one agent turn, optionally switching mode for its duration.

        When *model_override* is non-empty, a modified copy of ``exec_cfg`` is
        built with ``model=model_override`` so that ``AgentTurnRunner._resolve_model()``
        picks up the per-phase model (PRD-115).
        """
        from agenthicc.runners.agent_turn import _run_agent_turn  # noqa: PLC0415

        original_mode = self._cfg.app_state.active_mode()
        if mode is not None and self._mode_manager is not None:
            if self._mode_manager.set_by_name(mode) is None:
                log.warning("_run_turn: mode %r not found — keeping current mode", mode)

        # Build exec_cfg — replace model when a per-phase override is requested.
        _base_exec = self._cfg.cfg.execution
        exec_cfg = (
            dataclasses.replace(_base_exec, model=model_override)
            if model_override and dataclasses.is_dataclass(_base_exec)
            else _base_exec
        )

        if self._cfg.approval_svc is not None and ctx.shared_memory is not None:
            ctx.shared_memory.ensure_valid()

        try:
            await _run_agent_turn(
                text,
                runner=self._cfg.agent_runner,
                processor=self._cfg.processor,
                session_memory=ctx.shared_memory,
                max_agent_turns=max_turns,
                conv_store=self._cfg.conv_store,
                app_state=self._cfg.app_state,
                exec_cfg=exec_cfg,
                skills=self._cfg.skills,
                skill_permissions=self._cfg.cfg.agents.skill_permissions_for("auto"),
                mention_cache=self._cfg.mention_cache,
                project_plugin_tools=tools,
                mcp_registry=self._cfg.mcp_registry,
                active_agent="auto",
                completed_turns=self._cfg.completed_turns,
                approval_svc=self._cfg.approval_svc,
                output_collector=[],
                system_prompt_suffix=system_prompt,
                memory_router=self._cfg.memory_router,
                semantic_index=self._cfg.semantic_index,
            )
        finally:
            if mode is not None and self._mode_manager is not None:
                self._cfg.app_state.active_mode.set(original_mode)

    def _base_tools(self) -> _ToolList:
        """Return capability-filtered project tools for the current mode."""
        from agenthicc.tools.capabilities import get_tool_capabilities  # noqa: PLC0415
        from agenthicc.workflows.memory_tools import make_memory_tools  # noqa: PLC0415

        mode_blocked = self._cfg.app_state.active_mode().blocked_capabilities
        all_tools: _ToolList = list(self._cfg.all_plugin_tools())
        if self._cfg.mcp_registry is not None:
            try:
                all_tools = all_tools + list(self._cfg.mcp_registry.all_tools())
            except Exception:  # noqa: BLE001
                pass

        filtered: _ToolList = [
            tool for tool in all_tools if not (get_tool_capabilities(tool) & mode_blocked)
        ]
        # Memory tools carry no capability restrictions — always available.
        filtered = filtered + make_memory_tools(self._cfg.memory_router, self._cfg.semantic_index)
        return filtered
