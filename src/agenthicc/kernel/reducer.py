"""Pure reducers: (AppState, Event) -> (AppState, list[Effect])  (PRD-01)."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from .events import (
    PROJECTION_EVENT_TYPES,
    Effect,
    EffectType,
    Event,
    UnknownEventError,
    _payload_bool,
    _payload_mapping,
    _payload_optional_str,
    _payload_str,
)
from .state import (
    AppState,
    Intent,
    IntentStatus,
    NodeStatus,
    ToolRegistration,
    Workflow,
    WorkflowNode,
)

__all__ = ["REDUCED_EVENT_TYPES", "ReducerFn", "root_reducer"]

ReducerFn = Callable[[AppState, Event], tuple[AppState, list[Effect]]]


def root_reducer(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Apply *event* to *state*.

    Projection-only events leave the state unchanged. Any other event type
    without a handler raises :class:`UnknownEventError`; the processor turns
    that into a ``KernelEventRejected`` event instead of dropping it silently.
    """
    handler = _HANDLERS.get(event.event_type)
    if handler is not None:
        return handler(state, event)
    if event.event_type in PROJECTION_EVENT_TYPES:
        return state, []
    raise UnknownEventError(event.event_type)


# ── Intent ───────────────────────────────────────────────────────────────


def _intent_created(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    intent = Intent(
        intent_id=_payload_str(event.payload, "intent_id"),
        raw_text=_payload_str(event.payload, "raw_text"),
        status=IntentStatus.pending,
        workflow_id=None,
        created_at=event.timestamp,
        metadata=_payload_mapping(event.payload, "metadata"),
    )
    return state.with_intent(intent), [
        Effect(EffectType.emit_signal, {"signal": "IntentCreated", "intent_id": intent.intent_id}),
        Effect(EffectType.update_tui, {"type": "intent_added", "intent_id": intent.intent_id}),
    ]


def _intent_status_changed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    old = state.intents.get(_payload_str(event.payload, "intent_id"))
    if old is None:
        return state, []
    updated = replace(
        old,
        status=IntentStatus(_payload_str(event.payload, "status")),
        workflow_id=_payload_optional_str(event.payload, "workflow_id", default=old.workflow_id),
        error=_payload_optional_str(event.payload, "error", default=old.error),
    )
    return state.with_intent(updated), [
        Effect(EffectType.update_tui, {"type": "intent_updated", "intent_id": old.intent_id}),
    ]


# ── WorkflowRun (live-session events, PRD-94) ────────────────────────────


def _workflow_run_started(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Creates a Workflow entry when a WorkflowRunner.run() begins."""
    wf = Workflow(
        workflow_id=_payload_str(event.payload, "run_id"),
        intent_id="",
        nodes={},
        status=NodeStatus.pending,
        created_at=event.timestamp,
        name=_payload_str(event.payload, "workflow_name", default=""),
        intent_text=_payload_str(event.payload, "intent", default=""),
    )
    return state.with_workflow(wf), []


def _workflow_phase_completed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Records a completed phase as a WorkflowNode inside the Workflow."""
    run_id = _payload_str(event.payload, "run_id")
    phase_name = _payload_str(event.payload, "phase_name")
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
            "role": _payload_str(event.payload, "role", default=""),
            "full_text": _payload_str(event.payload, "full_text", default=""),
            "approved": event.payload.get("approved"),
            "structured": _payload_mapping(event.payload, "structured"),
            "metadata": _payload_mapping(event.payload, "metadata"),
        },
    )
    new_wf = replace(wf, nodes={**wf.nodes, node.node_id: node})
    return state.with_workflow(new_wf), []


def _workflow_run_completed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Marks a Workflow as complete or failed when the runner finishes."""
    run_id = _payload_str(event.payload, "run_id")
    wf = state.workflows.get(run_id)
    if wf is None:
        return state, []
    status_str = _payload_str(event.payload, "status", default="complete")
    if status_str == "complete" or status_str == "exited":
        new_status = NodeStatus.complete
    elif status_str == "paused":
        new_status = NodeStatus.paused
    elif status_str == "discarded":
        new_status = NodeStatus.discarded
    else:
        new_status = NodeStatus.failed
    return state.with_workflow(replace(wf, status=new_status)), []


def _workflow_run_paused(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Mark a live workflow paused without losing its run identity."""
    run_id = _payload_str(event.payload, "run_id")
    wf = state.workflows.get(run_id)
    if wf is None:
        return state, []
    return state.with_workflow(replace(wf, status=NodeStatus.paused)), []


def _workflow_run_resumed(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Return a paused workflow to running state."""
    run_id = _payload_str(event.payload, "run_id")
    wf = state.workflows.get(run_id)
    if wf is None:
        return state, []
    return state.with_workflow(replace(wf, status=NodeStatus.running)), []


def _workflow_run_discarded(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Record an explicit user reset as a terminal discarded run."""
    run_id = _payload_str(event.payload, "run_id")
    wf = state.workflows.get(run_id)
    if wf is None:
        return state, []
    return state.with_workflow(replace(wf, status=NodeStatus.discarded)), []


# ── Tools ──────────────────────────────────────────────────────────────────


def _tool_registered(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    reg = ToolRegistration(
        tool_id=_payload_str(event.payload, "tool_id"),
        name=_payload_str(event.payload, "name"),
        description=_payload_str(event.payload, "description", default=""),
        parameters_schema=_payload_mapping(event.payload, "parameters_schema"),
        is_builtin=_payload_bool(event.payload, "is_builtin"),
        source_agent_id=_payload_optional_str(event.payload, "source_agent_id"),
    )
    return state.with_tool(reg), []


_HANDLERS: dict[str, ReducerFn] = {
    "IntentCreated": _intent_created,
    "IntentStatusChanged": _intent_status_changed,
    # PRD-94: live-session workflow run tracking
    "WorkflowRunStarted": _workflow_run_started,
    "WorkflowPhaseCompleted": _workflow_phase_completed,
    "WorkflowRunCompleted": _workflow_run_completed,
    "WorkflowRunPaused": _workflow_run_paused,
    "WorkflowRunResumed": _workflow_run_resumed,
    "WorkflowRunDiscarded": _workflow_run_discarded,
    "ToolRegistered": _tool_registered,
}

#: Event types that change kernel state, each through its handler above.
REDUCED_EVENT_TYPES: frozenset[str] = frozenset(_HANDLERS)
