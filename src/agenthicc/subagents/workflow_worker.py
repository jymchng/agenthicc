"""Run a delegated workflow in a worker's private conversation and checkpoint tree."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agenthicc.runners.authored import AuthoredWorkflowOutcome
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_execution import run_workflow_core
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_stack import WorkflowFrame, workflow_return
from agenthicc.tools.surface import ToolSurfaceHolder
from agenthicc.workflows.plugin import WorkflowContext
from agenthicc.workflows.stack_tools import bind_workflow_tools

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from agenthicc.kernel.events import Event
    from agenthicc.plugins.authored_types import AuthoredRun
    from agenthicc.runners.workflow_execution import WorkflowWorkerRuntime
    from agenthicc.runners.workflow_stack import WorkflowReturn
    from agenthicc.subagents.policy import SubagentExecutionPolicy
    from agenthicc.subagents.workflow_task import WorkflowTaskSelection
    from agenthicc.tools.approval import ApprovalBroker
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.surface import ToolSurface
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.workflows.stack_tools import WorkflowToolScope


@runtime_checkable
class _WorkflowFailure(Protocol):
    @property
    def state(self) -> Enum: ...

    @property
    def fail_reason(self) -> str: ...


@dataclass(frozen=True, slots=True)
class WorkflowWorkerReceipt:
    result: WorkflowReturn
    phases: tuple[str, ...]
    run: AuthoredRun | None
    reason: str = ""


async def execute_worker_workflow(
    runtime: WorkflowWorkerRuntime,
    selected: WorkflowTaskSelection,
    *,
    run_id: str,
    intent: str,
    root: Path,
    workflow_scope: WorkflowToolScope,
    runner: AgentRunnerBase,
    model: str,
    memory_tokens: int,
    state: AppState,
    policy: SubagentExecutionPolicy,
    tools: ToolSurface,
    approval: ApprovalBroker | None,
    workspace: WorkspaceAccessPolicy | None,
    observe: Callable[[str, Mapping[str, object], object], None],
    control_tools: tuple[ToolLike, ...],
) -> WorkflowWorkerReceipt:
    """Use the same runner as headless workflow execution with a worker-owned handle."""
    definition = selected.definition
    base = runtime.config
    project = workspace.scope.primary_root if workspace is not None else None
    checkpoints = WorkflowCheckpointStore(run_id, root=root)
    conversation = SessionConversation.open(
        run_id, max_tokens=memory_tokens, journal_path=root / run_id / "conversation.jsonl"
    )
    handle = WorkflowRunHandle.create(
        run_id=run_id,
        workflow=definition,
        conversation=conversation,
        intent=intent,
        checkpoint_store=checkpoints,
        workspace_root=str(project) if project is not None else "",
    )
    names = tuple(phase.name for phase in definition.phases)
    context = WorkflowContext(
        intent=intent,
        run_id=run_id,
        workflow_name=definition.name,
        current_phase=names[0] if names else None,
    )
    handle.attach_bootstrap_context(context)
    if names:
        handle.update_phase(names[0], 0, 0)
    config = replace(
        base,
        conv_store=state.conversation,
        app_state=state,
        agent_runner=runner,
        cfg=replace(base.cfg, execution=replace(base.cfg.execution, model=model)),
        params=selected.params,
        approval_svc=approval,
        session_memory=conversation.memory,
        conversation_id=run_id,
        workflow_handle=handle,
        tool_surface=ToolSurfaceHolder(tools),
        workspace_scope=workspace.scope if workspace is not None else None,
        workspace_access=workspace,
        next_queued_message=None,
        browser_tools=(),
        browser_manager=None,
    )
    phases: list[str] = []

    def record(event: Event) -> None:
        if event.event_type == "WorkflowPhaseCompleted" and event.payload.get("run_id") == run_id:
            name = event.payload.get("phase_name")
            if isinstance(name, str):
                phases.append(name)

    unsubscribe = base.processor.add_sink(record)
    frame = WorkflowFrame(
        run_id,
        definition.name,
        run_id,
        None,
        "agent",
        intent,
        0,
        policy,
        "running",
        definition=definition,
        params=selected.params,
        model_arguments=selected.arguments,
    )
    try:
        with bind_workflow_tools(workflow_scope.for_worker(run_id, definition.name)):
            outcome = await run_workflow_core(
                definition,
                config,
                None,
                intent,
                policy=policy,
                model_request=frame.model_request(),
                observe_tool=observe,
                worker_tools=control_tools,
            )
        await base.processor.drain()
        failure = ""
        if (
            isinstance(outcome, _WorkflowFailure)
            and isinstance(outcome.state, Enum)
            and outcome.state.name.casefold()
            in {
                "failure",
                "failed",
            }
        ):
            failure = outcome.fail_reason or "The workflow reported failure."
        elif handle.lifecycle in {"failed", "paused"}:
            failure = handle.last_error or "The workflow did not finish."
        handle.mark_terminal("failed" if failure else "complete", error=failure)
        if handle.checkpoint_supported:
            handle.save_checkpoint(reason=handle.lifecycle)
        returned = workflow_return(
            frame, outcome, state.workflow_run(), status="failed" if failure else "completed"
        )
        run = (
            outcome.authored_run
            if not failure and isinstance(outcome, AuthoredWorkflowOutcome)
            else None
        )
        if run is not None:
            returned = replace(returned, artifacts=tuple(item.artifact for item in run.candidates))
        return WorkflowWorkerReceipt(returned, tuple(phases), run, failure)
    finally:
        unsubscribe()
        conversation.close()
