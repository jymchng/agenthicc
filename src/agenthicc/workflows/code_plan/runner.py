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

from agenthicc.workflows.base import BaseWorkflowRunner
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState

if TYPE_CHECKING:
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.plugin import WorkflowContext, WorkflowRun
    from agenthicc.tui.runtime.mode_manager import ModeManager

log = logging.getLogger(__name__)

# ── type alias for tool objects (no formal base class in lauren-ai yet) ────────
# Gap (lauren-ai): tools are @_tool()-decorated callables with no public ABC.
# list[object] is the most precise non-Any annotation available.
_ToolList = list[object]

# ── default retry caps ────────────────────────────────────────────────────────

_MAX_PLAN_ATTEMPTS:    int = 10
_MAX_EXECUTE_ATTEMPTS: int = 10
_MAX_REVIEW_ATTEMPTS:  int = 10

# ── system prompts ────────────────────────────────────────────────────────────

_PLAN_PROMPT: str = (
    "You are in the PLANNING phase. First explore the repository to understand "
    "the codebase. Then produce a detailed implementation plan. Use "
    "request_plan_approval() to present the plan for human review, and "
    "finalize_plan() once it is approved."
)
_PLAN_REMINDER: str = (
    "You have not yet finalized the plan. Return to the task described above, "
    "develop or refine your implementation plan, present it with "
    "request_plan_approval(plan), and once approved call finalize_plan(plan)."
)

_EXECUTE_PROMPT: str = (
    "You are in the EXECUTION phase. You already explored and planned in the "
    "previous phase — do NOT re-explore. Implement the approved plan step by "
    "step using tools. When ALL tasks are complete, call "
    "mark_execute_complete() with a brief summary. Do not stop without calling it."
)
_EXECUTE_REMINDER: str = (
    "Continue implementing — you have not yet called mark_execute_complete(). "
    "Resume from where you left off and complete all remaining tasks. Call "
    "mark_execute_complete() once everything is done."
)

_REVIEW_PROMPT: str = (
    "You are in the REVIEW phase. Inspect the changes you just made and run "
    "the tests. Call approve_review(summary) if all tests pass and the code is "
    "correct, or reject_review(reason) if there are issues that need fixing. "
    "You MUST call one of these two tools."
)
_REVIEW_REMINDER: str = (
    "You have not yet called approve_review() or reject_review(). Review the "
    "implementation now and call one of these tools: approve_review(summary) "
    "if all tests pass and the code is correct, or reject_review(reason) if "
    "there are issues that need fixing."
)

_SUMMARIZE_PROMPT: str = (
    "You are in the SUMMARY phase. Write a concise summary of what was "
    "planned, implemented, and verified in this session."
)

_PHASE_INDEX: dict[str, int] = {
    "plan": 0, "execute": 1, "review": 2, "summarize": 3,
}


class CodePlanRunner(BaseWorkflowRunner):
    """State-machine runner for the code_plan workflow.

    Parameters
    ----------
    config:
        WorkflowConfig holding all session-scoped singletons.
    mode_manager:
        ModeManager for per-phase mode overrides (None = headless).
    """

    def __init__(
        self,
        config:       WorkflowConfig,
        mode_manager: ModeManager | None = None,
    ) -> None:
        self._cfg:          WorkflowConfig     = config
        self._mode_manager: ModeManager | None = mode_manager
        self._run_id:       str                = ""

        # Gap (lauren-ai): no public model_id accessor on AgentRunnerBase.
        _transport_cfg  = getattr(
            getattr(config.agent_runner, "_transport", None), "_config", None
        )
        self._model_id: str = (
            getattr(_transport_cfg, "model", None)
            or config.cfg.execution.effective_model()
        )

    # ── public entry points ───────────────────────────────────────────────────

    async def run(self, intent: str) -> None:
        from lauren_ai._memory import ShortTermMemory       # noqa: PLC0415
        from agenthicc.kernel import Event                  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowRun  # noqa: PLC0415

        run_id:       str           = uuid.uuid4().hex
        self._run_id: str           = run_id

        ctx: CodePlanContext = CodePlanContext(
            intent=intent,
            run_id=run_id,
            shared_memory=ShortTermMemory(max_tokens=32_000),
        )

        wf_run: WorkflowRun = WorkflowRun(
            run_id=run_id,
            workflow_name="code_plan",
            intent=intent,
            current_phase="plan".title(),
            total_phases=4,
        )
        self._cfg.app_state.workflow_run.set(wf_run)

        await self._cfg.processor.emit(Event.create("WorkflowRunStarted", {
            "run_id":        run_id,
            "workflow_name": "code_plan",
            "intent":        intent,
            "phase_names":   ["plan", "execute", "review", "summarize"],
        }))

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

                await self._cfg.processor.emit(Event.create("WorkflowPhaseStarted", {
                    "run_id":        run_id,
                    "phase_name":    phase_name,
                    "workflow_name": "code_plan",
                }))

                match state:
                    case CodePlanState.PLAN:
                        state = await self._plan(ctx)
                    case CodePlanState.EXECUTE:
                        state = await self._execute(ctx)
                    case CodePlanState.REVIEW:
                        state = await self._review(ctx)
                    case CodePlanState.SUMMARIZE:
                        state = await self._summarize(ctx)

                next_label: str | None = (
                    state.name.lower() if not state.is_terminal else None
                )
                await self._cfg.processor.emit(Event.create("WorkflowPhaseCompleted", {
                    "run_id":      run_id,
                    "phase_name":  phase_name,
                    "role":        "auto",
                    "full_text":   "",
                    "approved":    None,
                    "structured":  {},
                    "edge_label":  next_label,
                }))
                self._cfg.app_state.workflow_run.set(wf_run)
                log.info("code_plan: %s → %s", phase_name, state.name)

            final_status: str = (
                "complete" if state == CodePlanState.COMPLETE else "failed"
            )
            wf_run = dataclasses.replace(wf_run, status=final_status, current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)

            if state == CodePlanState.FAILED and ctx.fail_reason:
                self._cfg.conv_store.append_event("error", {
                    "message": f"code_plan failed: {ctx.fail_reason}"
                })

            await self._cfg.processor.emit(Event.create("WorkflowRunCompleted", {
                "run_id":        run_id,
                "workflow_name": "code_plan",
                "phases_run":    len(wf_run.phase_history),
                "status":        final_status,
            }))

        except (asyncio.CancelledError, KeyboardInterrupt):
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            raise
        except Exception as exc:
            log.error("CodePlanRunner error: %s", exc, exc_info=True)
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            self._cfg.conv_store.append_event("error", {"message": str(exc)})

    async def resume(self, context: WorkflowContext) -> None:
        """Resume from a WorkflowContext (legacy --resume path)."""
        from lauren_ai._memory import ShortTermMemory       # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowRun  # noqa: PLC0415

        completed: set[str] = (
            set(context.phase_outputs.keys())
            if hasattr(context, "phase_outputs")
            else set()
        )

        ctx: CodePlanContext = CodePlanContext(
            intent=context.intent,
            run_id=context.run_id,
            shared_memory=ShortTermMemory(max_tokens=32_000),
        )

        if "plan" in completed:
            plan_output = context.phase_outputs.get("plan")
            if plan_output is not None:
                ctx.plan = plan_output.full_text

        resume_map: dict[frozenset[str], CodePlanState] = {
            frozenset():                              CodePlanState.PLAN,
            frozenset({"plan"}):                      CodePlanState.EXECUTE,
            frozenset({"plan", "execute"}):           CodePlanState.REVIEW,
            frozenset({"plan", "execute", "review"}): CodePlanState.SUMMARIZE,
        }
        state: CodePlanState = resume_map.get(
            frozenset(completed), CodePlanState.PLAN
        )

        if not completed:
            await self.run(context.intent)
            return

        self._run_id = context.run_id
        wf_run: WorkflowRun = WorkflowRun(
            run_id=context.run_id,
            workflow_name="code_plan",
            intent=context.intent,
            current_phase=state.name.lower(),
            total_phases=4,
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

        final: str = "complete" if state == CodePlanState.COMPLETE else "failed"
        wf_run = dataclasses.replace(wf_run, status=final, current_phase=None)
        self._cfg.app_state.workflow_run.set(wf_run)

    # ── phase methods ─────────────────────────────────────────────────────────

    async def _plan(self, ctx: CodePlanContext) -> CodePlanState:
        """Loop until finalize_plan() fires; return EXECUTE or FAILED."""
        from agenthicc.workflows.phase_tools import make_planner_tools  # noqa: PLC0415

        for attempt in range(1, _MAX_PLAN_ATTEMPTS + 1):
            plan_event: asyncio.Event     = asyncio.Event()
            plan_data:  dict[str, str]    = {}

            tools: _ToolList = list(self._base_tools())
            if self._cfg.approval_svc is not None:
                tools = tools + list(make_planner_tools(
                    self._cfg.approval_svc, plan_event, plan_data,
                ))

            text: str = ctx.intent if attempt == 1 else _PLAN_REMINDER

            try:
                await self._run_turn(
                    text, tools=tools, mode=None,
                    system_prompt=_PLAN_PROMPT, max_turns=20, ctx=ctx,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                log.error("_plan attempt %d error: %s", attempt, exc)
                break

            if plan_event.is_set() and "plan" in plan_data:
                ctx.plan = plan_data["plan"]
                return CodePlanState.EXECUTE

        ctx.fail_reason = (
            f"Plan phase exhausted {_MAX_PLAN_ATTEMPTS} attempts without finalization."
        )
        return CodePlanState.FAILED

    async def _execute(self, ctx: CodePlanContext) -> CodePlanState:
        """Loop until mark_execute_complete() fires; return REVIEW or FAILED."""
        from agenthicc.workflows.phase_tools import make_executor_tools  # noqa: PLC0415

        for attempt in range(1, _MAX_EXECUTE_ATTEMPTS + 1):
            execute_event: asyncio.Event  = asyncio.Event()
            execute_data:  dict[str, str] = {}

            tools: _ToolList = (
                list(self._base_tools())
                + list(make_executor_tools(execute_event, execute_data))
            )
            text: str = (
                f"[PLAN]\n{ctx.plan}\n\nTask: {ctx.intent}"
                if attempt == 1
                else _EXECUTE_REMINDER
            )

            try:
                await self._run_turn(
                    text, tools=tools, mode="Auto",
                    system_prompt=_EXECUTE_PROMPT, max_turns=40, ctx=ctx,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                log.error("_execute attempt %d error: %s", attempt, exc)
                break

            if execute_event.is_set():
                ctx.execute_summary = execute_data.get("summary", "")
                return CodePlanState.REVIEW

        ctx.fail_reason = (
            f"Execute phase exhausted {_MAX_EXECUTE_ATTEMPTS} attempts."
        )
        return CodePlanState.FAILED

    async def _review(self, ctx: CodePlanContext) -> CodePlanState:
        """Loop until approve_review() or reject_review() fires; return SUMMARIZE, EXECUTE, or FAILED."""
        from agenthicc.workflows.phase_tools import make_reviewer_tools  # noqa: PLC0415

        for attempt in range(1, _MAX_REVIEW_ATTEMPTS + 1):
            review_event: asyncio.Event  = asyncio.Event()
            review_data:  dict[str, str] = {}

            tools: _ToolList = (
                list(self._base_tools())
                + list(make_reviewer_tools(review_event, review_data))
            )
            text: str = ctx.intent if attempt == 1 else _REVIEW_REMINDER

            try:
                await self._run_turn(
                    text, tools=tools, mode=None,
                    system_prompt=_REVIEW_PROMPT, max_turns=8, ctx=ctx,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                log.error("_review attempt %d error: %s", attempt, exc)
                break

            if review_event.is_set():
                action: str = review_data.get("action", "reject")
                if action == "approve":
                    ctx.review_summary = review_data.get("summary", "")
                    return CodePlanState.SUMMARIZE
                ctx.rejection_reason = review_data.get("reason", "")
                return CodePlanState.EXECUTE

        ctx.fail_reason = (
            f"Review phase exhausted {_MAX_REVIEW_ATTEMPTS} attempts."
        )
        return CodePlanState.FAILED

    async def _summarize(self, ctx: CodePlanContext) -> CodePlanState:
        """Single turn; always returns COMPLETE."""
        text: str = (
            f"Task: {ctx.intent}\n\n"
            f"What was implemented: {ctx.execute_summary or '(see conversation)'}\n"
            f"Review verdict: {ctx.review_summary or 'approved'}"
        )
        try:
            await self._run_turn(
                text, tools=self._base_tools(), mode=None,
                system_prompt=_SUMMARIZE_PROMPT, max_turns=4, ctx=ctx,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            log.error("_summarize error: %s", exc)
        return CodePlanState.COMPLETE

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _run_turn(
        self,
        text:          str,
        *,
        tools:         _ToolList,
        mode:          str | None,
        system_prompt: str,
        max_turns:     int,
        ctx:           CodePlanContext,
    ) -> None:
        """Run one agent turn, optionally switching mode for its duration."""
        from agenthicc.runners.agent_turn import _run_agent_turn  # noqa: PLC0415

        original_mode = self._cfg.app_state.active_mode()
        if mode is not None and self._mode_manager is not None:
            if self._mode_manager.set_by_name(mode) is None:
                log.warning("_run_turn: mode %r not found — keeping current mode", mode)

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
                exec_cfg=self._cfg.cfg.execution,
                skills=self._cfg.skills,
                mention_cache=self._cfg.mention_cache,
                project_plugin_tools=tools,
                mcp_registry=self._cfg.mcp_registry,
                active_agent="auto",
                completed_turns=self._cfg.completed_turns,
                approval_svc=self._cfg.approval_svc,
                output_collector=[],
                system_prompt_suffix=system_prompt,
            )
        finally:
            if mode is not None and self._mode_manager is not None:
                self._cfg.app_state.active_mode.set(original_mode)

    def _base_tools(self) -> _ToolList:
        """Return capability-filtered project tools for the current mode."""
        from agenthicc.tools.capabilities import get_tool_capabilities  # noqa: PLC0415

        mode_blocked        = self._cfg.app_state.active_mode().blocked_capabilities
        all_tools: _ToolList = list(self._cfg.plugin_tools)
        if self._cfg.mcp_registry is not None:
            try:
                all_tools = all_tools + list(self._cfg.mcp_registry.all_tools())
            except Exception:  # noqa: BLE001
                pass

        return [
            tool for tool in all_tools
            if not (get_tool_capabilities(tool) & mode_blocked)
        ]
