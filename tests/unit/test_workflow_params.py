"""Tests for per-workflow tunable parameters (PRD-111, PRD-116)."""
from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin
from agenthicc.workflows.loader import load_builtin_workflows
from agenthicc.config import AgenthiccConfig
from agenthicc.workflows.code_plan import CodePlanParams, CodePlan


# ── WorkflowParams base ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_workflow_params_default_no_overrides() -> None:
    p = WorkflowParams()
    assert p.get_phase_models() == {}


@pytest.mark.unit
def test_model_for_phase_returns_fallback_when_empty() -> None:
    p = WorkflowParams()
    assert p.model_for_phase("execute", "claude-opus") == "claude-opus"


@pytest.mark.unit
def test_model_for_phase_returns_fallback_for_unknown_phase() -> None:
    p = WorkflowParams()
    assert p.model_for_phase("nonexistent", "default-model") == "default-model"


@pytest.mark.unit
def test_model_for_phase_empty_string_falls_back() -> None:
    """An empty string in the map means 'use global model'."""
    class MyParams(WorkflowParams):
        def get_phase_models(self):
            return {"execute": ""}

    p = MyParams()
    assert p.model_for_phase("execute", "global-model") == "global-model"


@pytest.mark.unit
def test_model_for_phase_non_empty_overrides() -> None:
    class MyParams(WorkflowParams):
        def get_phase_models(self):
            return {"execute": "claude-haiku-4-5"}

    p = MyParams()
    assert p.model_for_phase("execute", "claude-opus") == "claude-haiku-4-5"
    assert p.model_for_phase("plan", "claude-opus") == "claude-opus"


# ── CodePlanParams ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_code_plan_params_defaults_are_empty() -> None:
    p = CodePlanParams()
    assert p.execute_model == ""
    assert p.plan_model == ""
    assert p.review_model == ""
    assert p.summary_model == ""


@pytest.mark.unit
def test_code_plan_params_execute_model_overrides() -> None:
    p = CodePlanParams(execute_model="claude-haiku-4-5")
    assert p.model_for_phase("execute", "claude-opus") == "claude-haiku-4-5"
    assert p.model_for_phase("plan", "claude-opus") == "claude-opus"


@pytest.mark.unit
def test_code_plan_params_get_phase_models_maps_all_phases() -> None:
    p = CodePlanParams(plan_model="m1", execute_model="m2",
                       review_model="m3", summary_model="m4")
    pm = p.get_phase_models()
    assert pm["plan"]      == "m1"
    assert pm["execute"]   == "m2"
    assert pm["review"]    == "m3"
    assert pm["summarize"] == "m4"


@pytest.mark.unit
def test_code_plan_params_is_workflow_params_subclass() -> None:
    assert issubclass(CodePlanParams, WorkflowParams)


# ── WorkflowPlugin.build_params default ──────────────────────────────────────

@pytest.mark.unit
def test_default_build_params_returns_base_workflow_params() -> None:
    class MyWorkflow(WorkflowPlugin):
        name   = "my"
        phases = [PhaseSpec(name="p")]

    result = MyWorkflow.build_params({})
    assert type(result) is WorkflowParams


@pytest.mark.unit
def test_default_build_params_ignores_source() -> None:
    result = WorkflowPlugin.build_params({"anything": "value"})
    assert type(result) is WorkflowParams
    assert result.get_phase_models() == {}


@pytest.mark.unit
def test_code_plan_build_params_constructs_code_plan_params() -> None:
    result = CodePlan.build_params({"execute_model": "claude-haiku-4-5"})
    assert isinstance(result, CodePlanParams)
    assert result.execute_model == "claude-haiku-4-5"


@pytest.mark.unit
def test_code_plan_build_params_filters_unknown_keys() -> None:
    result = CodePlan.build_params({"execute_model": "X", "unknown": "Y"})
    assert isinstance(result, CodePlanParams)
    assert result.execute_model == "X"
    assert not hasattr(result, "unknown")


@pytest.mark.unit
def test_code_plan_build_params_empty_source_uses_defaults() -> None:
    result = CodePlan.build_params({})
    assert isinstance(result, CodePlanParams)
    assert result.execute_model == ""


# ── plugin_cls.build_params() ─────────────────────────────────────────────────

@pytest.mark.unit
def test_build_params_base_plugin_returns_base() -> None:
    class _TestWf(WorkflowPlugin):
        name = "test"
        phases = [PhaseSpec(name="p")]

    result = _TestWf.build_params({"anything": "x"})
    assert type(result) is WorkflowParams


@pytest.mark.unit
def test_code_plan_build_params_differs_from_default() -> None:
    from agenthicc.workflows import CodePlan
    assert CodePlan.build_params.__func__ is not WorkflowPlugin.build_params.__func__


@pytest.mark.unit
def test_all_builtins_have_build_params() -> None:
    for plugin_cls in load_builtin_workflows():
        assert callable(getattr(plugin_cls, "build_params", None)), (
            f"Workflow {plugin_cls.name!r} has no build_params"
        )


# ── AgenthiccConfig.workflows ─────────────────────────────────────────────────

@pytest.mark.unit
def test_agenthicc_config_workflows_defaults_to_empty() -> None:
    cfg = AgenthiccConfig()
    assert cfg.workflows == {}


@pytest.mark.unit
def test_agenthicc_config_workflows_stored() -> None:
    cfg = AgenthiccConfig(workflows={"code_plan": {"execute_model": "claude-haiku-4-5"}})
    assert cfg.workflows["code_plan"]["execute_model"] == "claude-haiku-4-5"


@pytest.mark.unit
def test_load_config_parses_workflows_section(tmp_path) -> None:
    toml = tmp_path / "agenthicc.toml"
    toml.write_text(
        '[workflows.code_plan]\nexecute_model = "claude-haiku-4-5"\n',
        encoding="utf-8",
    )
    from agenthicc.config import load_config
    cfg = load_config(project_path=toml)
    assert cfg.workflows.get("code_plan", {}).get("execute_model") == "claude-haiku-4-5"


# ── model_for_phase integration ───────────────────────────────────────────────

@pytest.mark.unit
def test_end_to_end_phase_model_resolution() -> None:
    """Simulate the full path: TOML → build_params → model_for_phase."""
    cfg_workflows = {"execute_model": "claude-haiku-4-5", "plan_model": ""}
    params = CodePlan.build_params(cfg_workflows)

    # Execute phase uses cheap model
    assert params.model_for_phase("execute", "claude-opus") == "claude-haiku-4-5"
    # Plan phase falls back to global (empty string in config)
    assert params.model_for_phase("plan", "claude-opus") == "claude-opus"
    # Unknown phase falls back to global
    assert params.model_for_phase("review", "claude-opus") == "claude-opus"


@pytest.mark.unit
def test_workflow_config_carries_params() -> None:
    """WorkflowConfig.params field exists and accepts WorkflowParams."""
    from agenthicc.workflows.config import WorkflowConfig
    mock = MagicMock()
    params = CodePlanParams(execute_model="claude-haiku-4-5")
    cfg = WorkflowConfig(
        conv_store=mock, app_state=mock, processor=mock,
        agent_runner=mock, approval_svc=None, cfg=mock,
        skills={}, plugin_tools=mock, mcp_registry=None,
        mention_cache=mock, agents_registry=mock,
        params=params,
    )
    assert cfg.params is params
    assert cfg.params.model_for_phase("execute", "fallback") == "claude-haiku-4-5"


@pytest.mark.unit
def test_workflow_config_replace_updates_params() -> None:
    """dataclasses.replace() can update params independently of completed_turns."""
    from agenthicc.workflows.config import WorkflowConfig
    mock = MagicMock()
    base_cfg = WorkflowConfig(
        conv_store=mock, app_state=mock, processor=mock,
        agent_runner=mock, approval_svc=None, cfg=mock,
        skills={}, plugin_tools=mock, mcp_registry=None,
        mention_cache=mock, agents_registry=mock,
    )
    assert base_cfg.params is None

    params = CodePlanParams(execute_model="claude-haiku-4-5")
    updated = dataclasses.replace(base_cfg, params=params, completed_turns=3)
    assert updated.params is params
    assert updated.completed_turns == 3
    assert base_cfg.params is None  # original unchanged
