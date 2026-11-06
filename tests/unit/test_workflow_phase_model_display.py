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
        run_id="r1",
        workflow_name="code_plan",
        intent="do stuff",
        current_phase="plan",
    )
    assert hasattr(wf, "current_phase_model")
    assert wf.current_phase_model == ""


@pytest.mark.unit
def test_workflow_run_current_phase_model_set_explicitly() -> None:
    wf = WorkflowRun(
        run_id="r1",
        workflow_name="code_plan",
        intent="do stuff",
        current_phase="execute",
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
        workflow_name="code_plan",
        phase_name="plan",
        phase_index=0,
        total_phases=4,
        run_id="r1",
        intent="test",
        model_id="deepseek-v4-pro",
    )
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
def test_update_workflow_phase_clears_model_when_empty() -> None:
    """A phase with no override (model_id='') stores empty string."""
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
    state.update_workflow_phase(
        workflow_name="code_plan",
        phase_name="review",
        phase_index=2,
        total_phases=4,
        run_id="r1",
        intent="test",
        model_id="",  # review uses global model
    )
    wf = state.workflow_run()
    assert wf is not None
    assert wf.current_phase_model == ""


# ── StatusLine model segment (TUX-02) ───────────────────────────────────────


def _status_text(workflow: WorkflowRun | None) -> str:
    from rich.text import Text

    from agenthicc.tui.workspace.components import StatusLine

    state = AppState.create()
    state.conversation.model_name.set("openai/claude-opus")
    state.workflow_run.set(workflow)
    return Text.from_markup(StatusLine(state).markup(160)).plain


def _run(status: str, phase_model: str, phase: str | None = "execute") -> WorkflowRun:
    return WorkflowRun(
        run_id="r1",
        workflow_name="code_plan",
        intent="do stuff",
        current_phase=phase,
        status=status,
        current_phase_model=phase_model,
    )


@pytest.mark.unit
def test_status_line_uses_phase_model_when_set() -> None:
    text = _status_text(_run("running", "deepseek-v4-flash"))
    assert "deepseek-v4-flash" in text
    assert "claude-opus" not in text


@pytest.mark.unit
def test_status_line_uses_session_model_when_no_phase_override() -> None:
    assert "openai/opus" in _status_text(_run("running", "", "plan"))


@pytest.mark.unit
def test_status_line_reverts_to_session_model_after_workflow_ends() -> None:
    assert "openai/opus" in _status_text(None)


@pytest.mark.unit
def test_status_line_uses_session_model_when_workflow_complete() -> None:
    text = _status_text(_run("complete", "deepseek-v4-flash", None))
    assert "openai/opus" in text
    assert "deepseek-v4-flash" not in text
