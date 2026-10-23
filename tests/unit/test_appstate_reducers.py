"""Unit tests for kernel reducers (PRD-01)."""

from __future__ import annotations

import time

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from agenthicc.kernel import (
    PROJECTION_EVENT_TYPES,
    AppState,
    Event,
    IntentStatus,
    NodeStatus,
    SecurityPolicy,
    SystemSettings,
    UnknownEventError,
    root_reducer,
)

pytestmark = pytest.mark.unit


def base_state() -> AppState:
    return AppState.create(settings=SystemSettings(), policy=SecurityPolicy())


def ev(event_type: str, payload: dict) -> Event:
    return Event(event_id="t", event_type=event_type, timestamp=time.time(), payload=payload)


class TestIntentReducers:
    def test_creates_intent_pending(self):
        s, _ = root_reducer(base_state(), ev("IntentCreated", {"intent_id": "i1", "raw_text": "x"}))
        assert s.intents["i1"].status == IntentStatus.pending

    def test_original_not_mutated(self):
        state = base_state()
        root_reducer(state, ev("IntentCreated", {"intent_id": "i1", "raw_text": "x"}))
        assert "i1" not in state.intents

    def test_produces_tui_effect(self):
        _, effects = root_reducer(
            base_state(), ev("IntentCreated", {"intent_id": "i1", "raw_text": "x"})
        )
        assert any(e.effect_type.value == "update_tui" for e in effects)

    def test_status_change(self):
        s0 = base_state()
        s1, _ = root_reducer(s0, ev("IntentCreated", {"intent_id": "i1", "raw_text": "x"}))
        s2, _ = root_reducer(
            s1, ev("IntentStatusChanged", {"intent_id": "i1", "status": "running"})
        )
        assert s2.intents["i1"].status == IntentStatus.running

    def test_status_change_unknown_intent_noop(self):
        state = base_state()
        s, effects = root_reducer(
            state, ev("IntentStatusChanged", {"intent_id": "nope", "status": "running"})
        )
        assert s is state
        assert effects == []

    def test_unknown_event_is_rejected(self):
        with pytest.raises(UnknownEventError, match="UnknownXYZ"):
            root_reducer(base_state(), ev("UnknownXYZ", {}))

    @pytest.mark.parametrize("event_type", sorted(PROJECTION_EVENT_TYPES))
    def test_projection_events_leave_state_unchanged(self, event_type: str):
        state = base_state()
        s, effects = root_reducer(state, ev(event_type, {"anything": 1}))
        assert s is state
        assert effects == []


class TestWorkflowRunReducers:
    """PRD-94: live-session workflow run event handlers."""

    def test_workflow_run_started_creates_workflow(self):
        s, effects = root_reducer(
            base_state(),
            ev(
                "WorkflowRunStarted",
                {
                    "run_id": "run1",
                    "workflow_name": "code_plan",
                    "intent": "refactor auth",
                    "phase_names": ["plan", "execute"],
                },
            ),
        )
        assert "run1" in s.workflows
        wf = s.workflows["run1"]
        assert wf.name == "code_plan"
        assert wf.intent_text == "refactor auth"
        assert wf.status == NodeStatus.pending
        assert wf.nodes == {}
        assert effects == []

    def test_workflow_run_started_unknown_fields_ignored(self):
        s, _ = root_reducer(
            base_state(),
            ev(
                "WorkflowRunStarted",
                {
                    "run_id": "r2",
                    "workflow_name": "wf",
                    "intent": "x",
                },
            ),
        )
        assert "r2" in s.workflows

    def test_workflow_phase_completed_adds_node(self):
        s0, _ = root_reducer(
            base_state(),
            ev(
                "WorkflowRunStarted",
                {
                    "run_id": "run1",
                    "workflow_name": "wf",
                    "intent": "do it",
                },
            ),
        )
        s1, effects = root_reducer(
            s0,
            ev(
                "WorkflowPhaseCompleted",
                {
                    "run_id": "run1",
                    "phase_name": "plan",
                    "role": "planner",
                    "full_text": "Here is the plan.",
                    "approved": True,
                    "structured": {"plan_text": "step 1"},
                },
            ),
        )
        assert "plan" in s1.workflows["run1"].nodes
        node = s1.workflows["run1"].nodes["plan"]
        assert node.status == NodeStatus.complete
        assert node.result["full_text"] == "Here is the plan."
        assert node.result["approved"] is True
        assert node.result["role"] == "planner"
        assert node.result["structured"] == {"plan_text": "step 1"}
        assert effects == []

    def test_workflow_phase_completed_unknown_run_noop(self):
        s = base_state()
        s2, effects = root_reducer(
            s,
            ev(
                "WorkflowPhaseCompleted",
                {
                    "run_id": "nope",
                    "phase_name": "p",
                    "role": "r",
                    "full_text": "",
                    "approved": None,
                    "structured": {},
                },
            ),
        )
        assert s2 is s
        assert effects == []

    def test_workflow_run_completed_marks_complete(self):
        s0, _ = root_reducer(
            base_state(),
            ev(
                "WorkflowRunStarted",
                {
                    "run_id": "run1",
                    "workflow_name": "wf",
                    "intent": "x",
                },
            ),
        )
        s1, effects = root_reducer(
            s0,
            ev(
                "WorkflowRunCompleted",
                {
                    "run_id": "run1",
                    "status": "complete",
                },
            ),
        )
        assert s1.workflows["run1"].status == NodeStatus.complete
        assert effects == []

    def test_workflow_run_completed_marks_failed(self):
        s0, _ = root_reducer(
            base_state(),
            ev(
                "WorkflowRunStarted",
                {
                    "run_id": "run1",
                    "workflow_name": "wf",
                    "intent": "x",
                },
            ),
        )
        s1, _ = root_reducer(
            s0,
            ev(
                "WorkflowRunCompleted",
                {
                    "run_id": "run1",
                    "status": "failed",
                },
            ),
        )
        assert s1.workflows["run1"].status == NodeStatus.failed

    def test_workflow_run_completed_unknown_run_noop(self):
        s = base_state()
        s2, _ = root_reducer(
            s, ev("WorkflowRunCompleted", {"run_id": "nope", "status": "complete"})
        )
        assert s2 is s

    def test_full_sequence_restores_via_replay(self):
        """Replaying WorkflowRunStarted + WorkflowPhaseCompleted × N + WorkflowRunCompleted
        produces a Workflow with all phases as complete nodes."""
        s = base_state()
        s, _ = root_reducer(
            s,
            ev(
                "WorkflowRunStarted",
                {
                    "run_id": "rx",
                    "workflow_name": "wf",
                    "intent": "fix bugs",
                    "phase_names": ["plan", "execute"],
                },
            ),
        )
        s, _ = root_reducer(
            s,
            ev(
                "WorkflowPhaseCompleted",
                {
                    "run_id": "rx",
                    "phase_name": "plan",
                    "role": "planner",
                    "full_text": "plan text",
                    "approved": True,
                    "structured": {},
                    "metadata": {"execute_mode": "Yolo"},
                },
            ),
        )
        s, _ = root_reducer(
            s,
            ev(
                "WorkflowPhaseCompleted",
                {
                    "run_id": "rx",
                    "phase_name": "execute",
                    "role": "executor",
                    "full_text": "exec text",
                    "approved": None,
                    "structured": {},
                },
            ),
        )
        s, _ = root_reducer(s, ev("WorkflowRunCompleted", {"run_id": "rx", "status": "complete"}))
        wf = s.workflows["rx"]
        assert wf.status == NodeStatus.complete
        assert set(wf.nodes.keys()) == {"plan", "execute"}
        assert wf.nodes["plan"].result["full_text"] == "plan text"
        assert wf.nodes["plan"].result["metadata"]["execute_mode"] == "Yolo"
        assert wf.nodes["execute"].result["role"] == "executor"


class TestToolReducers:
    def test_tool_registered(self):
        s, _ = root_reducer(
            base_state(),
            ev(
                "ToolRegistered",
                {
                    "tool_id": "t1",
                    "name": "my_tool",
                    "description": "d",
                    "parameters_schema": {},
                },
            ),
        )
        assert "my_tool" in s.tools


class TestReducerProperties:
    @given(
        intent_id=st.text(
            min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"))
        ),
        raw_text=st.text(min_size=1, max_size=200),
    )
    @h_settings(max_examples=50, deadline=None)
    def test_deterministic(self, intent_id: str, raw_text: str):
        state = base_state()
        event = ev("IntentCreated", {"intent_id": intent_id, "raw_text": raw_text})
        s1, efx1 = root_reducer(state, event)
        s2, efx2 = root_reducer(state, event)
        assert s1.intents[intent_id].raw_text == s2.intents[intent_id].raw_text
        assert len(efx1) == len(efx2)

    @given(st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll",))))
    @h_settings(max_examples=50, deadline=None)
    def test_never_mutates_input(self, intent_id: str):
        state = base_state()
        before = dict(state.intents)
        root_reducer(state, ev("IntentCreated", {"intent_id": intent_id, "raw_text": "t"}))
        assert state.intents == before
