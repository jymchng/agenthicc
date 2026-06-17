"""Pure reducers: (AppState, Event) -> (AppState, list[Effect])  (PRD-01)."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from .events import Effect, EffectType, Event
from .state import (
    AgentInstance,
    AgentStatus,
    AppState,
    Intent,
    IntentStatus,
    NodeStatus,
    Task,
    ToolRegistration,
    Workflow,
    WorkflowNode,
)

__all__ = ["ReducerFn", "root_reducer"]

ReducerFn = Callable[[AppState, Event], tuple[AppState, list[Effect]]]


def root_reducer(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    handler = _HANDLERS.get(event.event_type)
    if handler is None:
        return state, []
    return handler(state, event)


# ── Intent ───────────────────────────────────────────────────────────────


def _intent_created(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    intent = Intent(
        intent_id=event.payload["intent_id"],
        raw_text=event.payload["raw_text"],
        status=IntentStatus.pending,
        workflow_id=None,
        created_at=event.timestamp,
        metadata=event.payload.get("metadata", {}),
    )
    return state.with_intent(intent), [
        Effect(EffectType.emit_signal, {"signal": "IntentCreated", "intent_id": intent.intent_id}),
        Effect(EffectType.update_tui, {"type": "intent_added", "intent_id": intent.intent_id}),
    ]


def _intent_status_changed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    old = state.intents.get(event.payload["intent_id"])
    if old is None:
        return state, []
    updated = replace(
        old,
        status=IntentStatus(event.payload["status"]),
        workflow_id=event.payload.get("workflow_id", old.workflow_id),
        error=event.payload.get("error", old.error),
    )
    return state.with_intent(updated), [
        Effect(EffectType.update_tui, {"type": "intent_updated", "intent_id": old.intent_id}),
    ]


# ── Agents ───────────────────────────────────────────────────────────────


def _agent_spawn_request(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    agent = AgentInstance(
        agent_id=event.payload["agent_id"],
        agent_type=event.payload["agent_type"],
        status=AgentStatus.idle,
        current_task_id=None,
        parent_agent_id=event.payload.get("parent_agent_id"),
        created_at=event.timestamp,
        metadata=event.payload.get("metadata", {}),
    )
    return state.with_agent(agent), [
        Effect(EffectType.spawn_agent, {
            "agent_id": agent.agent_id,
            "agent_type": agent.agent_type,
            "config": event.payload.get("config", {}),
        }),
        Effect(EffectType.update_tui, {"type": "agent_spawned", "agent_id": agent.agent_id}),
    ]


def _agent_status_changed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    old = state.agents.get(event.payload["agent_id"])
    if old is None:
        return state, []
    updated = replace(
        old,
        status=AgentStatus(event.payload["status"]),
        current_task_id=event.payload.get("current_task_id", old.current_task_id),
    )
    return state.with_agent(updated), []


# ── Workflows ────────────────────────────────────────────────────────────


def _workflow_created(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    wf = Workflow(
        workflow_id=event.payload["workflow_id"],
        intent_id=event.payload["intent_id"],
        nodes={},
        status=NodeStatus.pending,
        created_at=event.timestamp,
    )
    return state.with_workflow(wf), []


def _workflow_node_added(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    wf = state.workflows.get(event.payload["workflow_id"])
    if wf is None:
        return state, []
    node = WorkflowNode(
        node_id=event.payload["node_id"],
        task_id=event.payload["task_id"],
        label=event.payload.get("label", ""),
        dependencies=frozenset(event.payload.get("dependencies", [])),
        status=NodeStatus.pending,
    )
    new_wf = replace(wf, nodes={**wf.nodes, node.node_id: node})
    return state.with_workflow(new_wf), [
        Effect(EffectType.start_workflow_node, {"workflow_id": wf.workflow_id}),
    ]


def _workflow_node_removed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    wf = state.workflows.get(event.payload["workflow_id"])
    if wf is None:
        return state, []
    node_id = event.payload["node_id"]
    if node_id not in wf.nodes:
        return state, []
    new_nodes = {k: v for k, v in wf.nodes.items() if k != node_id}
    return state.with_workflow(replace(wf, nodes=new_nodes)), []


def _workflow_node_status_changed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    wf = state.workflows.get(event.payload["workflow_id"])
    if wf is None:
        return state, []
    old_node = wf.nodes.get(event.payload["node_id"])
    if old_node is None:
        return state, []
    new_node = replace(
        old_node,
        status=NodeStatus(event.payload["status"]),
        agent_id=event.payload.get("agent_id", old_node.agent_id),
        result=event.payload.get("result", old_node.result),
        error=event.payload.get("error", old_node.error),
    )
    new_wf = replace(wf, nodes={**wf.nodes, new_node.node_id: new_node})

    # Workflow completes when every node is terminal.
    terminal = {NodeStatus.complete, NodeStatus.failed, NodeStatus.skipped}
    if all(n.status in terminal for n in new_wf.nodes.values()):
        wf_status = (
            NodeStatus.complete
            if all(n.status != NodeStatus.failed for n in new_wf.nodes.values())
            else NodeStatus.failed
        )
        new_wf = replace(new_wf, status=wf_status)

    effects: list[Effect] = []
    if new_node.status in (NodeStatus.complete, NodeStatus.failed):
        effects.append(Effect(EffectType.start_workflow_node, {"workflow_id": wf.workflow_id}))
    effects.append(Effect(EffectType.update_tui, {
        "type": "node_updated",
        "workflow_id": wf.workflow_id,
        "node_id": new_node.node_id,
        "status": new_node.status.value,
    }))
    return state.with_workflow(new_wf), effects


# ── WorkflowRun (live-session events, PRD-94) ────────────────────────────


def _workflow_run_started(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Creates a Workflow entry when a WorkflowRunner.run() begins."""
    wf = Workflow(
        workflow_id=event.payload["run_id"],
        intent_id="",
        nodes={},
        status=NodeStatus.pending,
        created_at=event.timestamp,
        name=event.payload.get("workflow_name", ""),
        intent_text=event.payload.get("intent", ""),
    )
    return state.with_workflow(wf), []


def _workflow_phase_completed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Records a completed phase as a WorkflowNode inside the Workflow."""
    run_id     = event.payload["run_id"]
    phase_name = event.payload["phase_name"]
    wf = state.workflows.get(run_id)
    if wf is None:
        return state, []
    node = WorkflowNode(
        node_id=phase_name,
        task_id=phase_name,
        label=phase_name,
        dependencies=frozenset(),
        status=NodeStatus.complete,
        result={
            "role":        event.payload.get("role", ""),
            "full_text":   event.payload.get("full_text", ""),
            "approved":    event.payload.get("approved"),
            "structured":  event.payload.get("structured", {}),
            "edge_label":  event.payload.get("edge_label"),
        },
    )
    new_wf = replace(wf, nodes={**wf.nodes, node.node_id: node})
    return state.with_workflow(new_wf), []


def _workflow_run_completed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Marks a Workflow as complete or failed when the runner finishes."""
    run_id = event.payload["run_id"]
    wf = state.workflows.get(run_id)
    if wf is None:
        return state, []
    status_str = event.payload.get("status", "complete")
    new_status = NodeStatus.complete if status_str == "complete" else NodeStatus.failed
    return state.with_workflow(replace(wf, status=new_status)), []


# ── Tasks ────────────────────────────────────────────────────────────────


def _task_created(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    task = Task(
        task_id=event.payload["task_id"],
        workflow_id=event.payload["workflow_id"],
        node_id=event.payload["node_id"],
        description=event.payload["description"],
        status=NodeStatus.pending,
        assigned_agent_id=None,
        created_at=event.timestamp,
    )
    return state.with_task(task), []


def _task_assigned(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    old = state.tasks.get(event.payload["task_id"])
    if old is None:
        return state, []
    updated = replace(
        old,
        status=NodeStatus.running,
        assigned_agent_id=event.payload["agent_id"],
    )
    return state.with_task(updated), [
        Effect(EffectType.assign_task, {
            "task_id": old.task_id,
            "agent_id": event.payload["agent_id"],
        }),
    ]


# ── Tools & hooks ────────────────────────────────────────────────────────


def _tool_registered(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    reg = ToolRegistration(
        tool_id=event.payload["tool_id"],
        name=event.payload["name"],
        description=event.payload.get("description", ""),
        parameters_schema=event.payload.get("parameters_schema", {}),
        is_builtin=event.payload.get("is_builtin", False),
        source_agent_id=event.payload.get("source_agent_id"),
    )
    return state.with_tool(reg), []


def _hook_registered(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    hook_id = event.payload["hook_id"]
    return state.with_hook(hook_id, {
        "entity_type": event.payload.get("entity_type", ""),
        "stage": event.payload.get("stage", ""),
        "handler_dotpath": event.payload.get("handler_dotpath", ""),
    }), []


# ── Interrupt / cancel ───────────────────────────────────────────────────


def _intent_cancelled(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Mark all active intents as failed with error='cancelled by user'."""
    updated = dict(state.intents)
    for intent_id, intent in state.intents.items():
        if intent.status in (
            IntentStatus.running,
            IntentStatus.planning,
            IntentStatus.validating,
            IntentStatus.pending,
        ):
            updated[intent_id] = replace(
                intent,
                status=IntentStatus.failed,
                error="cancelled by user",
            )
    new_state = replace(state, intents=updated)
    return new_state, [Effect(EffectType.update_tui, {"type": "intent_cancelled"})]


_HANDLERS: dict[str, ReducerFn] = {
    "IntentCreated": _intent_created,
    "IntentStatusChanged": _intent_status_changed,
    "IntentCancelled": _intent_cancelled,
    "AgentSpawnRequest": _agent_spawn_request,
    "AgentStatusChanged": _agent_status_changed,
    "WorkflowCreated": _workflow_created,
    "WorkflowNodeAdded": _workflow_node_added,
    "WorkflowNodeRemoved": _workflow_node_removed,
    "WorkflowNodeStatusChanged": _workflow_node_status_changed,
    # PRD-94: live-session workflow run tracking
    "WorkflowRunStarted": _workflow_run_started,
    "WorkflowPhaseCompleted": _workflow_phase_completed,
    "WorkflowRunCompleted": _workflow_run_completed,
    "TaskCreated": _task_created,
    "TaskAssigned": _task_assigned,
    "ToolRegistered": _tool_registered,
    "HookRegistered": _hook_registered,
}
