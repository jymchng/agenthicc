"""Tests for per-phase model display in the status bar (PRD-118)."""
from __future__ import annotations

import dataclasses

import pytest

from agenthicc.workflows.plugin import WorkflowRun
from agenthicc.tui.conversation_store import AppState


# ── WorkflowRun.current_phase_model field ────────────────────────────────────

@pytest.mark.unit
def test_workflow_run_has_current_phase_model_field() -> None:
    wf = WorkflowRun(
        run_id="r1", workflow_name="code_plan",
        intent="do stuff", current_phase="plan",
    )
    assert hasattr(wf, "current_phase_model")
    assert wf.current_phase_model == ""


@pytest.mark.unit
def test_workflow_run_current_phase_model_set_explicitly() -> None:
    wf = WorkflowRun(
        run_id="r1", workflow_name="code_plan",
        intent="do stuff", current_phase="execute",
        current_phase_model="deepseek-v4-flash",
    )
    assert wf.current_phase_model == "deepseek-v4-flash"


@pytest.mark.unit
def test_workflow_run_is_typed_str() -> None:
    fields = {f.name: f for f in dataclasses.fields(WorkflowRun)}
    assert "current_phase_model" in fields
    assert fields["current_phase_model"].default == ""


# ── update_workflow_phase wires model_id ─────────────────────────────────────

@pytest.mark.unit
def test_update_workflow_phase_stores_model_id() -> None:
    state = AppState()
    state.update_workflow_phase(
        workflow_name="code_plan",
        phase_name="execute",
        phase_index=1,
        total_phases=4,
        run_id="r1",
        intent="test",
        model_id="deepseek-v4-flash",
    )
    wf = state.workflow_run()
    assert wf is not None
    assert wf.current_phase_model == "deepseek-v4-flash"


@pytest.mark.unit
def test_update_workflow_phase_empty_model_id_stores_empty_string() -> None:
    state = AppState()
    state.update_workflow_phase(
        workflow_name="code_plan",
        phase_name="plan",
        phase_index=0,
        total_phases=4,
        run_id="r1",
        intent="test",
        # model_id not provided — defaults to ""
    )
    wf = state.workflow_run()
    assert wf is not None
    assert wf.current_phase_model == ""


@pytest.mark.unit
def test_update_workflow_phase_replaces_model_on_phase_change() -> None:
    """Second call (new phase) replaces current_phase_model correctly."""
    state = AppState()
    state.update_workflow_phase(
        workflow_name="code_plan", phase_name="plan",
        phase_index=0, total_phases=4, run_id="r1", intent="test",
        model_id="deepseek-v4-pro",
    )
    state.update_workflow_phase(
        workflow_name="code_plan", phase_name="execute",
        phase_index=1, total_phases=4, run_id="r1", intent="test",
        model_id="deepseek-v4-flash",
    )
    wf = state.workflow_run()
    assert wf is not None
    assert wf.current_phase_model == "deepseek-v4-flash"


@pytest.mark.unit
def test_update_workflow_phase_clears_model_when_empty() -> None:
    """A phase with no override (model_id='') stores empty string."""
    state = AppState()
    state.update_workflow_phase(
        workflow_name="code_plan", phase_name="execute",
        phase_index=1, total_phases=4, run_id="r1", intent="test",
        model_id="deepseek-v4-flash",
    )
    state.update_workflow_phase(
        workflow_name="code_plan", phase_name="review",
        phase_index=2, total_phases=4, run_id="r1", intent="test",
        model_id="",   # review uses global model
    )
    wf = state.workflow_run()
    assert wf is not None
    assert wf.current_phase_model == ""


# ── StatusComponent.render() model selection ─────────────────────────────────

@pytest.mark.unit
def test_status_component_uses_phase_model_when_set() -> None:
    """When current_phase_model is non-empty and status==running, line 2 shows it."""
    from unittest.mock import MagicMock
    from agenthicc.tui.workspace.components import StatusComponent

    state = MagicMock()
    state.conversation.model_name.return_value = "openai/claude-opus"
    state.conversation.session_id.return_value = "sess-1"
    state.conversation.turn_count.return_value = 1
    state.conversation.cost_usd.return_value = 0.0
    state.conversation.tokens_in.return_value = 100
    state.conversation.tokens_out.return_value = 50
    state.active_mode.return_value = MagicMock(badge="⏵⏵")

    wf = WorkflowRun(
        run_id="r1", workflow_name="code_plan",
        intent="do stuff", current_phase="execute",
        status="running", current_phase_model="deepseek-v4-flash",
    )
    state.workflow_run.return_value = wf
    state.conversation.agent_state.return_value = MagicMock(name="IDLE")
    state.conversation.active_tool.return_value = ""
    state.conversation.elapsed_s = 0.0
    state.conversation.notification.return_value = None
    state.conversation.workflow_override.return_value = None
    state.conversation.frame.return_value = 0
    state.conversation.compaction_active.return_value = False

    from rich.console import Console
    comp = StatusComponent(state)
    console = Console(highlight=False, markup=False, no_color=True, width=120)
    with console.capture() as cap:
        console.print(comp.render())
    rendered_str = cap.get()

    # The phase model should appear, not the global model
    assert "deepseek-v4-flash" in rendered_str
    assert "claude-opus" not in rendered_str


@pytest.mark.unit
def test_status_component_uses_session_model_when_no_phase_override() -> None:
    """When current_phase_model is empty, line 2 shows the session model."""
    from unittest.mock import MagicMock
    from agenthicc.tui.workspace.components import StatusComponent

    state = MagicMock()
    state.conversation.model_name.return_value = "openai/claude-opus"
    state.conversation.session_id.return_value = "sess-1"
    state.conversation.turn_count.return_value = 1
    state.conversation.cost_usd.return_value = 0.0
    state.conversation.tokens_in.return_value = 100
    state.conversation.tokens_out.return_value = 50
    state.active_mode.return_value = MagicMock(badge="⏵⏵")

    wf = WorkflowRun(
        run_id="r1", workflow_name="code_plan",
        intent="do stuff", current_phase="plan",
        status="running", current_phase_model="",   # no override
    )
    state.workflow_run.return_value = wf
    state.conversation.agent_state.return_value = MagicMock(name="IDLE")
    state.conversation.active_tool.return_value = ""
    state.conversation.elapsed_s = 0.0
    state.conversation.notification.return_value = None
    state.conversation.workflow_override.return_value = None
    state.conversation.frame.return_value = 0
    state.conversation.compaction_active.return_value = False

    from rich.console import Console
    comp = StatusComponent(state)
    console = Console(highlight=False, markup=False, no_color=True, width=120)
    with console.capture() as cap:
        console.print(comp.render())
    rendered_str = cap.get()

    assert "claude-opus" in rendered_str


@pytest.mark.unit
def test_status_component_reverts_to_session_model_after_workflow_ends() -> None:
    """When workflow_run is None, line 2 shows the session model again."""
    from unittest.mock import MagicMock
    from agenthicc.tui.workspace.components import StatusComponent

    state = MagicMock()
    state.conversation.model_name.return_value = "openai/claude-opus"
    state.conversation.session_id.return_value = "sess-1"
    state.conversation.turn_count.return_value = 1
    state.conversation.cost_usd.return_value = 0.0
    state.conversation.tokens_in.return_value = 100
    state.conversation.tokens_out.return_value = 50
    state.active_mode.return_value = MagicMock(badge="⏵⏵")
    state.workflow_run.return_value = None   # no active workflow
    state.conversation.agent_state.return_value = MagicMock(name="IDLE")
    state.conversation.active_tool.return_value = ""
    state.conversation.elapsed_s = 0.0
    state.conversation.notification.return_value = None
    state.conversation.workflow_override.return_value = None
    state.conversation.frame.return_value = 0
    state.conversation.compaction_active.return_value = False

    from rich.console import Console
    comp = StatusComponent(state)
    console = Console(highlight=False, markup=False, no_color=True, width=120)
    with console.capture() as cap:
        console.print(comp.render())
    rendered_str = cap.get()

    assert "claude-opus" in rendered_str


@pytest.mark.unit
def test_status_component_uses_session_model_when_workflow_complete() -> None:
    """When status=='complete', current_phase_model is ignored."""
    from unittest.mock import MagicMock
    from agenthicc.tui.workspace.components import StatusComponent

    state = MagicMock()
    state.conversation.model_name.return_value = "openai/claude-opus"
    state.conversation.session_id.return_value = "sess-1"
    state.conversation.turn_count.return_value = 1
    state.conversation.cost_usd.return_value = 0.0
    state.conversation.tokens_in.return_value = 100
    state.conversation.tokens_out.return_value = 50
    state.active_mode.return_value = MagicMock(badge="⏵⏵")

    wf = WorkflowRun(
        run_id="r1", workflow_name="code_plan",
        intent="do stuff", current_phase=None,
        status="complete", current_phase_model="deepseek-v4-flash",
    )
    state.workflow_run.return_value = wf
    state.conversation.agent_state.return_value = MagicMock(name="IDLE")
    state.conversation.active_tool.return_value = ""
    state.conversation.elapsed_s = 0.0
    state.conversation.notification.return_value = None
    state.conversation.workflow_override.return_value = None
    state.conversation.frame.return_value = 0
    state.conversation.compaction_active.return_value = False

    from rich.console import Console
    comp = StatusComponent(state)
    console = Console(highlight=False, markup=False, no_color=True, width=120)
    with console.capture() as cap:
        console.print(comp.render())
    rendered_str = cap.get()

    # Workflow complete — session model shown, not phase model
    assert "claude-opus" in rendered_str
    assert "deepseek-v4-flash" not in rendered_str
