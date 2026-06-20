"""Tests for workflow package structure reorganisation (PRD-112)."""
from __future__ import annotations

import pytest


# ── Canonical import paths ────────────────────────────────────────────────────

@pytest.mark.unit
def test_code_plan_importable_from_code_plan_package() -> None:
    from agenthicc.workflows.code_plan import CodePlan
    assert CodePlan.name == "code_plan"


@pytest.mark.unit
def test_code_plan_params_importable_from_code_plan_package() -> None:
    from agenthicc.workflows.code_plan import CodePlanParams
    p = CodePlanParams(execute_model="claude-haiku-4-5")
    assert p.execute_model == "claude-haiku-4-5"


@pytest.mark.unit
def test_code_plan_defined_in_definition_module() -> None:
    from agenthicc.workflows.code_plan.definition import CodePlan
    assert CodePlan.__module__ == "agenthicc.workflows.code_plan.definition"


@pytest.mark.unit
def test_code_plan_params_defined_in_definition_module() -> None:
    from agenthicc.workflows.code_plan.definition import CodePlanParams
    assert CodePlanParams.__module__ == "agenthicc.workflows.code_plan.definition"


@pytest.mark.unit
def test_workflow_runner_importable_from_default_package() -> None:
    from agenthicc.workflows.default import WorkflowRunner
    assert WorkflowRunner is not None


@pytest.mark.unit
def test_workflow_runner_defined_in_default_runner() -> None:
    from agenthicc.workflows.default.runner import WorkflowRunner
    assert WorkflowRunner.__module__ == "agenthicc.workflows.default.runner"


@pytest.mark.unit
def test_generic_workflows_in_default_definition() -> None:
    from agenthicc.workflows.default.definition import (
        Architect, PlanOnly, ReviewOnly, Supervised,
    )
    assert PlanOnly.name == "plan_only"
    assert ReviewOnly.name == "review_only"
    assert Supervised.name == "supervised"
    assert Architect.name == "architect"


@pytest.mark.unit
def test_generic_workflows_importable_from_default_package() -> None:
    from agenthicc.workflows.default import (
        Architect, PlanOnly, ReviewOnly, Supervised,
    )
    for cls in (PlanOnly, ReviewOnly, Supervised, Architect):
        assert issubclass(cls, __import__(
            "agenthicc.workflows.plugin", fromlist=["WorkflowPlugin"]
        ).WorkflowPlugin)


# ── Backward-compat shims still work ─────────────────────────────────────────

@pytest.mark.unit
def test_old_runner_path_still_works() -> None:
    from agenthicc.workflows.runner import WorkflowRunner
    from agenthicc.workflows.default.runner import WorkflowRunner as WR2
    assert WorkflowRunner is WR2


@pytest.mark.unit
def test_old_builtins_code_plan_still_works() -> None:
    from agenthicc.workflows.builtins import CodePlan
    from agenthicc.workflows.code_plan.definition import CodePlan as CP2
    assert CodePlan is CP2


@pytest.mark.unit
def test_old_builtins_generic_workflows_still_work() -> None:
    from agenthicc.workflows.builtins import PlanOnly, ReviewOnly, Supervised, Architect
    from agenthicc.workflows.default.definition import (
        PlanOnly as PO2, ReviewOnly as RO2, Supervised as S2, Architect as A2,
    )
    assert PlanOnly is PO2
    assert ReviewOnly is RO2
    assert Supervised is S2
    assert Architect is A2


# ── Top-level __init__ exports all symbols ────────────────────────────────────

@pytest.mark.unit
def test_workflows_init_exports_workflow_runner() -> None:
    from agenthicc.workflows import WorkflowRunner
    assert WorkflowRunner is not None


@pytest.mark.unit
def test_workflows_init_exports_code_plan() -> None:
    from agenthicc.workflows import CodePlan, CodePlanParams
    assert CodePlan.name == "code_plan"
    assert issubclass(CodePlanParams, __import__(
        "agenthicc.workflows.plugin", fromlist=["WorkflowParams"]
    ).WorkflowParams)


@pytest.mark.unit
def test_workflows_init_exports_generic_workflows() -> None:
    from agenthicc.workflows import Architect, PlanOnly, ReviewOnly, Supervised
    for cls in (PlanOnly, ReviewOnly, Supervised, Architect):
        assert cls.name  # all have non-empty names


# ── loader uses canonical paths ───────────────────────────────────────────────

@pytest.mark.unit
def test_load_builtin_workflows_returns_all_five() -> None:
    from agenthicc.workflows.loader import load_builtin_workflows
    defs = load_builtin_workflows()
    names = {d.name for d in defs}
    assert "code_plan" in names
    assert "plan_only" in names
    assert "review_only" in names
    assert "supervised" in names
    assert "architect" in names


@pytest.mark.unit
def test_code_plan_definition_has_runner_factory() -> None:
    from agenthicc.workflows.loader import load_builtin_workflows
    from agenthicc.workflows.code_plan import CodePlanRunner
    from unittest.mock import MagicMock
    defs = {d.name: d for d in load_builtin_workflows()}
    runner = defs["code_plan"].build_runner(MagicMock(), None)
    assert isinstance(runner, CodePlanRunner)
