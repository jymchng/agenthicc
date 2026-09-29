"""Runner for the durable parallel code-plan workflow."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agenthicc.background import BackgroundStore, BackgroundSupervisor
from agenthicc.tools.base import ToolLike
from agenthicc.worktrees.coordinator import ParallelCoordinator
from agenthicc.worktrees.store import ManifestStore
from agenthicc.workflows.code_plan.phase_tools import make_planner_tools, make_questions_tool
from .phase_tools import (
    make_collect_tools,
    make_complete_tool,
    make_dispatch_tools,
    make_integrate_tools,
    make_task_graph_tools,
)
from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.code_plan.state import CodePlanContext
from agenthicc.workflows.base_runner import BaseWorkflowRunner

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig


@dataclasses.dataclass
class ParallelCodePlanContext(CodePlanContext):
    plan: str = ""
    orchestration_id: str = ""
    phase: str = "plan"
    summary: str = ""


class ParallelCodePlanRunner(BaseWorkflowRunner):
    """A coordinator agent that delegates independent tasks to Git workers."""

    workflow_name = "parallel_code_plan"
    total_phases = 7

    def __init__(self, config: WorkflowConfig, mode_manager: ModeManager | None = None) -> None:
        self._cfg = config
        self._delegate = CodePlanRunner(config, mode_manager)

    def _coordinator(self, ctx: ParallelCodePlanContext) -> ParallelCoordinator:
        return ParallelCoordinator(
            self._cfg.workspace_scope.primary_root
            if self._cfg.workspace_scope is not None
            else ".",
            parent_session_id=self._cfg.conversation_id or ctx.run_id,
            store=ManifestStore(),
            supervisor=BackgroundSupervisor(
                BackgroundStore(),
                max_workers=self._cfg.cfg.execution.max_parallel_tasks,
                max_workers_per_project=self._cfg.cfg.execution.max_parallel_tasks,
            ),
            max_parallel_tasks=self._cfg.cfg.execution.max_parallel_tasks,
        )

    def _phase_model(self, phase_name: str) -> str:
        return self._delegate._phase_model(phase_name)

    def _set_phase(self, name: str, index: int, ctx: ParallelCodePlanContext) -> None:
        ctx.phase = name
        ctx.phase_iteration += 1
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(ctx)
            handle.update_phase(name, index, ctx.phase_iteration)
        self._cfg.app_state.update_workflow_phase(
            workflow_name=self.workflow_name,
            phase_name=name,
            phase_index=index,
            total_phases=self.total_phases,
            run_id=ctx.run_id,
            intent=ctx.intent,
            model_id=self._phase_model(name) or self._delegate._model_id,
        )

    async def run(self, intent: str) -> ParallelCodePlanContext:
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415

        memory = self._cfg.session_memory or ShortTermMemory(
            max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        ctx = ParallelCodePlanContext(intent=intent, run_id=run_id, shared_memory=memory)
        if handle is not None:
            handle.attach_context(ctx)
            handle.update_phase("plan", 0, 0)
        phases = (
            self._plan,
            self._decompose,
            self._dispatch,
            self._collect,
            self._integrate,
            self._verify,
            self._summarize,
        )
        try:
            for index, phase in enumerate(phases):
                await phase(ctx, index)
            if handle is not None:
                handle.mark_terminal("complete")
                if handle.checkpoint_supported:
                    handle.save_checkpoint(reason="complete")
            return ctx
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            ctx.fail_reason = f"{type(exc).__name__}: {exc}"
            if handle is not None:
                handle.attach_context(ctx)
                handle.finalize_failure(ctx.fail_reason)
            raise

    async def resume(self, context: object) -> ParallelCodePlanContext:
        if not isinstance(context, ParallelCodePlanContext):
            raise TypeError("parallel_code_plan resume requires ParallelCodePlanContext")
        phases = {
            "plan": self._plan,
            "decompose": self._decompose,
            "dispatch": self._dispatch,
            "collect": self._collect,
            "integrate": self._integrate,
            "verify": self._verify,
            "summarize": self._summarize,
        }
        if context.phase == "complete":
            return context
        order = tuple(phases)
        start = order.index(context.phase) if context.phase in phases else 0
        for index in range(start, len(order)):
            await phases[order[index]](context, index)
        return context

    async def _turn(
        self,
        ctx: ParallelCodePlanContext,
        *,
        phase: str,
        index: int,
        text: str,
        prompt: str,
        tools: Sequence[ToolLike],
        event: asyncio.Event,
        max_turns: int = 12,
    ) -> None:
        self._set_phase(phase, index, ctx)
        phase_tools = list(tools)
        phase_tools.extend(make_questions_tool(self._cfg.approval_svc))
        await self._delegate._run_turn(
            text,
            tools=phase_tools,
            mode=None,
            system_prompt=prompt,
            stable_system_prompt=(
                "[PARALLEL CODE PLAN CACHE CONTRACT]\n"
                "The coordinator owns the task graph, worker identity, Git branches, "
                "integration, conflict preservation, and checkpoint boundaries. "
                "Dynamic phase state, task descriptions, worker results, and user "
                "feedback belong in the dynamic prompt region. Never claim a phase "
                "advanced in prose; call its control tool and stop after success."
            ),
            max_turns=max_turns,
            ctx=ctx,
            phase_name=phase,
            model_override=self._phase_model(phase),
        )
        if not event.is_set():
            raise RuntimeError(f"{phase} phase ended without its transition tool")

    async def _plan(self, ctx: ParallelCodePlanContext, index: int) -> None:
        approval_event = asyncio.Event()
        data: dict[str, object] = {}
        tools = make_planner_tools(self._cfg.approval_svc, approval_event, data)
        await self._turn(
            ctx,
            phase="plan",
            index=index,
            text=ctx.intent,
            prompt=(
                "Plan the requested implementation as parallelizable work. "
                "Identify independent boundaries and dependencies, then request "
                "human approval and finalize the plan."
            ),
            tools=tools,
            event=approval_event,
        )
        ctx.plan = str(data.get("plan", ctx.intent))
        coordinator = self._coordinator(ctx)
        manifest = await asyncio.to_thread(coordinator.create)
        ctx.orchestration_id = manifest.orchestration_id
        self._checkpoint(ctx, "decompose")

    async def _decompose(self, ctx: ParallelCodePlanContext, index: int) -> None:
        coordinator = self._coordinator(ctx)
        event = asyncio.Event()
        data: dict[str, object] = {}
        await self._turn(
            ctx,
            phase="decompose",
            index=index,
            text=f"Approved plan:\n{ctx.plan}",
            prompt=(
                "Decompose the approved plan into independently implementable worker "
                "tasks. Call add_parallel_task once for every task, including exact "
                "scope and dependencies. Then call finalize_task_graph(summary)."
            ),
            tools=make_task_graph_tools(coordinator, ctx.orchestration_id, event, data),
            event=event,
        )
        self._checkpoint(ctx, "dispatch")

    async def _dispatch(self, ctx: ParallelCodePlanContext, index: int) -> None:
        coordinator = self._coordinator(ctx)
        event = asyncio.Event()
        data: dict[str, object] = {}
        await self._turn(
            ctx,
            phase="dispatch",
            index=index,
            text="Dispatch the ready tasks from the durable graph.",
            prompt=(
                "Dispatch all currently ready worker tasks by calling "
                "dispatch_parallel_workers(summary). Each worker receives one "
                "isolated worktree and must commit its result."
            ),
            tools=make_dispatch_tools(coordinator, ctx.orchestration_id, event, data),
            event=event,
        )
        self._checkpoint(ctx, "collect")

    async def _collect(self, ctx: ParallelCodePlanContext, index: int) -> None:
        coordinator = self._coordinator(ctx)
        event = asyncio.Event()
        data: dict[str, object] = {}
        await self._turn(
            ctx,
            phase="collect",
            index=index,
            text="Collect worker completion evidence.",
            prompt=(
                "Refresh each dispatched worker using collect_parallel_workers(summary). "
                "The tool checks Git commits and clean state, not worker prose."
            ),
            tools=make_collect_tools(coordinator, ctx.orchestration_id, event, data),
            event=event,
            max_turns=6,
        )
        self._checkpoint(ctx, "integrate")

    async def _integrate(self, ctx: ParallelCodePlanContext, index: int) -> None:
        coordinator = self._coordinator(ctx)
        event = asyncio.Event()
        data: dict[str, object] = {}
        await self._turn(
            ctx,
            phase="integrate",
            index=index,
            text="Review worker results and integrate them one at a time.",
            prompt=(
                "Review machine-derived worker evidence. Call "
                "integrate_parallel_worker(task_id, summary) for every completed "
                "task. If a conflict is returned, do not delete the worker."
            ),
            tools=make_integrate_tools(coordinator, ctx.orchestration_id, event, data),
            event=event,
            max_turns=20,
        )
        self._checkpoint(ctx, "verify")

    async def _verify(self, ctx: ParallelCodePlanContext, index: int) -> None:
        coordinator = self._coordinator(ctx)
        event = asyncio.Event()
        data: dict[str, object] = {}
        await self._turn(
            ctx,
            phase="verify",
            index=index,
            text="Run relevant integration verification in the coordinator worktree.",
            prompt=(
                "Run relevant tests and checks using the normal tools. Confirm "
                "the integrated worktree is correct, then call "
                "mark_parallel_complete(summary) with verification evidence."
            ),
            tools=make_complete_tool(coordinator, ctx.orchestration_id, event, data),
            event=event,
            max_turns=12,
        )
        self._checkpoint(ctx, "summarize")

    async def _summarize(self, ctx: ParallelCodePlanContext, index: int) -> None:
        self._set_phase("summarize", index, ctx)
        ctx.summary = (
            f"Parallel orchestration {ctx.orchestration_id} completed. "
            "Worker branches were reviewed, integrated, and verified."
        )
        self._checkpoint(ctx, "complete")

    def _checkpoint(self, ctx: ParallelCodePlanContext, next_phase: str) -> None:
        ctx.phase = next_phase
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(ctx)
            handle.persist_checkpoint(reason=f"parallel_phase:{ctx.phase}")
