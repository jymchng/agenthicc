"""Tests for per-phase model override and phase-aware TUI updates (PRD-115)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.code_plan.state import CodePlanContext


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_runner(plan_model="", execute_model="", review_model="", summary_model="", params=None):
    mock_cfg = MagicMock()
    mock_cfg.params = params
    mock_cfg.app_state.workflow_run.return_value = None
    mock_cfg.app_state.update_workflow_phase = MagicMock()
    mock_cfg.app_state.active_mode.return_value = MagicMock(blocked_capabilities=frozenset())
    mock_cfg.plugin_tools = []
    mock_cfg.mcp_registry = None
    mock_cfg.approval_svc = None
    mock_cfg.memory_router = None
    mock_cfg.semantic_index = None
    mock_cfg.cfg.execution = MagicMock(model="global-model")
    mock_cfg.agent_runner = MagicMock()
    mock_cfg.cfg.execution.__class__ = type(
        "ExecutionSettings", (), {"model": "global-model", "__dataclass_fields__": {}}
    )

    runner = CodePlanRunner(mock_cfg, None)
    runner._model_id = "global-model"
    runner.plan_model = plan_model
    runner.execute_model = execute_model
    runner.review_model = review_model
    runner.summary_model = summary_model
    return runner


# ── _resolve_model() fix ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_model_uses_exec_cfg_model_when_set() -> None:
    """exec_cfg.model takes priority over the transport config (PRD-115 fix)."""
    from agenthicc.runners.agent_turn import AgentTurnRunner
    from agenthicc.runners.agent_turn_context import AgentTurnContext

    exec_cfg = MagicMock()
    exec_cfg.model = "per-phase-model"

    ctx = AgentTurnContext(
        text="test",
        runner=MagicMock(),  # transport would say "transport-model"
        processor=MagicMock(),
        session_memory=None,
        max_agent_turns=1,
        conv_store=None,
        app_state=None,
        exec_cfg=exec_cfg,
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=None,
        system_prompt_suffix="",
    )
    runner = AgentTurnRunner(ctx)
    runner._resolve_model()

    assert runner._model_id == "per-phase-model"
    assert runner._model_short == "per-phase-model"


@pytest.mark.unit
def test_resolve_model_falls_back_to_transport_when_exec_cfg_empty() -> None:
    """When exec_cfg.model is empty, fall back to transport config."""
    from agenthicc.runners.agent_turn import AgentTurnRunner
    from agenthicc.runners.agent_turn_context import AgentTurnContext

    exec_cfg = MagicMock()
    exec_cfg.model = ""  # empty → use transport

    transport_cfg = MagicMock()
    transport_cfg.model = "transport-model"
    transport = MagicMock()
    transport._config = transport_cfg
    mock_runner = MagicMock()
    mock_runner._transport = transport

    ctx = AgentTurnContext(
        text="test",
        runner=mock_runner,
        processor=MagicMock(),
        session_memory=None,
        max_agent_turns=1,
        conv_store=None,
        app_state=None,
        exec_cfg=exec_cfg,
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=None,
        system_prompt_suffix="",
    )
    runner = AgentTurnRunner(ctx)
    runner._resolve_model()

    assert runner._model_id == "transport-model"


@pytest.mark.unit
def test_resolve_model_falls_back_to_transport_when_exec_cfg_is_none() -> None:
    """When exec_cfg is None, fall back to transport config."""
    from agenthicc.runners.agent_turn import AgentTurnRunner
    from agenthicc.runners.agent_turn_context import AgentTurnContext

    transport_cfg = MagicMock()
    transport_cfg.model = "transport-model"
    transport = MagicMock()
    transport._config = transport_cfg
    mock_runner = MagicMock()
    mock_runner._transport = transport

    ctx = AgentTurnContext(
        text="test",
        runner=mock_runner,
        processor=MagicMock(),
        session_memory=None,
        max_agent_turns=1,
        conv_store=None,
        app_state=None,
        exec_cfg=None,
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=None,
        system_prompt_suffix="",
    )
    runner = AgentTurnRunner(ctx)
    runner._resolve_model()

    assert runner._model_id == "transport-model"


# ── AppState.update_workflow_phase() ──────────────────────────────────────────


@pytest.mark.unit
def test_update_workflow_phase_creates_run_when_none() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.workflows.plugin import WorkflowRun

    state = AppState()
    assert state.workflow_run() is None

    state.update_workflow_phase(
        workflow_name="code_plan_docs",
        phase_name="update_docs",
        phase_index=4,
        total_phases=5,
        run_id="abc123",
        intent="do stuff",
    )

    wf = state.workflow_run()
    assert isinstance(wf, WorkflowRun)
    assert wf.workflow_name == "code_plan_docs"
    assert wf.current_phase == "update_docs"
    assert wf.current_phase_index == 4
    assert wf.total_phases == 5
    assert wf.status == "running"


@pytest.mark.unit
def test_update_workflow_phase_replaces_existing() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.workflows.plugin import WorkflowRun

    state = AppState()
    initial = WorkflowRun(
        run_id="r1",
        workflow_name="code_plan",
        intent="intent",
        current_phase="plan",
        current_phase_index=0,
        total_phases=4,
    )
    state.workflow_run.set(initial)

    state.update_workflow_phase(
        workflow_name="code_plan",
        phase_name="execute",
        phase_index=1,
        total_phases=4,
        run_id="r1",
        intent="intent",
    )

    wf = state.workflow_run()
    assert wf.current_phase == "execute"
    assert wf.current_phase_index == 1
    assert wf.run_id == "r1"  # preserved from existing


# ── CodePlanRunner class attributes ──────────────────────────────────────────


@pytest.mark.unit
def test_code_plan_runner_default_phase_models_are_empty() -> None:
    assert CodePlanRunner.plan_model == ""
    assert CodePlanRunner.execute_model == ""
    assert CodePlanRunner.review_model == ""
    assert CodePlanRunner.summary_model == ""


@pytest.mark.unit
def test_subclass_can_override_phase_models() -> None:
    class MyRunner(CodePlanRunner):
        plan_model = "flagship"
        execute_model = "cheap"

    assert MyRunner.plan_model == "flagship"
    assert MyRunner.execute_model == "cheap"
    assert MyRunner.review_model == ""  # inherited default


# ── _phase_model() ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_phase_model_returns_empty_when_no_override() -> None:
    runner = _make_runner()
    assert runner._phase_model("plan") == ""
    assert runner._phase_model("execute") == ""


@pytest.mark.unit
def test_phase_model_uses_class_attribute() -> None:
    runner = _make_runner(plan_model="flagship", execute_model="cheap")
    assert runner._phase_model("plan") == "flagship"
    assert runner._phase_model("execute") == "cheap"
    assert runner._phase_model("review") == ""


@pytest.mark.unit
def test_phase_model_prefers_workflow_params_over_class_attr() -> None:
    """WorkflowParams (TOML/CLI) wins over class attribute."""
    mock_params = MagicMock()
    mock_params.model_for_phase.side_effect = lambda phase, default: (
        "toml-model" if phase == "plan" else default
    )

    runner = _make_runner(plan_model="class-model", params=mock_params)
    assert runner._phase_model("plan") == "toml-model"  # TOML wins
    assert runner._phase_model("execute") == ""  # no TOML → empty


@pytest.mark.unit
def test_phase_model_falls_back_to_class_attr_when_params_returns_empty() -> None:
    mock_params = MagicMock()
    mock_params.model_for_phase.return_value = ""  # TOML has no override

    runner = _make_runner(execute_model="class-fallback", params=mock_params)
    assert runner._phase_model("execute") == "class-fallback"


# ── _run_turn model_override ──────────────────────────────────────────────────


@pytest.mark.unit
async def test_run_turn_passes_replaced_exec_cfg_when_model_override_set() -> None:
    """_run_turn with model_override calls _run_agent_turn with modified exec_cfg."""
    from agenthicc.workflows.code_plan.state import CodePlanContext

    runner = _make_runner()

    # Make exec_cfg a real dataclass so dataclasses.replace works
    from agenthicc.config import ExecutionSettings

    real_exec = ExecutionSettings(model="global")
    runner._cfg.cfg.execution = real_exec

    ctx = CodePlanContext(intent="i", run_id="r", shared_memory=MagicMock())

    captured_exec_cfg: list = []

    async def fake_run_agent_turn(text, **kwargs):
        captured_exec_cfg.append(kwargs.get("exec_cfg"))

    with patch("agenthicc.runners.agent_turn._run_agent_turn", fake_run_agent_turn):
        await runner._run_turn(
            "text",
            tools=[],
            mode=None,
            system_prompt="sp",
            max_turns=1,
            ctx=ctx,
            model_override="per-phase-model",
        )

    assert len(captured_exec_cfg) == 1
    assert captured_exec_cfg[0].model == "per-phase-model"


@pytest.mark.unit
async def test_run_turn_passes_original_exec_cfg_when_no_override() -> None:
    """_run_turn without model_override passes exec_cfg unchanged."""
    from agenthicc.config import ExecutionSettings
    from agenthicc.workflows.code_plan.state import CodePlanContext

    runner = _make_runner()
    real_exec = ExecutionSettings(model="global")
    runner._cfg.cfg.execution = real_exec

    ctx = CodePlanContext(intent="i", run_id="r", shared_memory=MagicMock())
    captured: list = []

    async def fake_run_agent_turn(text, **kwargs):
        captured.append(kwargs.get("exec_cfg"))

    with patch("agenthicc.runners.agent_turn._run_agent_turn", fake_run_agent_turn):
        await runner._run_turn(
            "text",
            tools=[],
            mode=None,
            system_prompt="sp",
            max_turns=1,
            ctx=ctx,
            # no model_override
        )

    assert captured[0] is real_exec  # exact same object, not replaced


# ── _set_phase() ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_set_phase_calls_update_workflow_phase() -> None:
    runner = _make_runner()
    runner.workflow_name = "code_plan_docs"
    runner.total_phases = 5
    runner._model_id = "global"

    ctx = CodePlanContext(intent="do stuff", run_id="run-abc", shared_memory=MagicMock())
    runner._set_phase("update_docs", 4, ctx)

    runner._cfg.app_state.update_workflow_phase.assert_called_once_with(
        workflow_name="code_plan_docs",
        phase_name="update_docs",
        phase_index=4,
        total_phases=5,
        run_id="run-abc",
        intent="do stuff",
        model_id="global",  # no override → falls back to self._model_id
    )


@pytest.mark.unit
def test_set_phase_includes_phase_model_in_model_id() -> None:
    runner = _make_runner(plan_model="flagship")
    runner._model_id = "global"
    ctx = CodePlanContext(intent="intent", run_id="r", shared_memory=MagicMock())
    runner._set_phase("plan", 0, ctx)

    call_kwargs = runner._cfg.app_state.update_workflow_phase.call_args[1]
    assert call_kwargs["model_id"] == "flagship"
