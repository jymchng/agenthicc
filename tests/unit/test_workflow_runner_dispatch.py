"""Tests for workflow runner dispatch via build_runner classmethod (PRD-110, PRD-116)."""
from __future__ import annotations

import pytest

from agenthicc.workflows.code_plan.definition import CodePlan
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin
from agenthicc.workflows.loader import load_builtin_workflows


# ── WorkflowPlugin.build_runner default ──────────────────────────────────────

@pytest.mark.unit
def test_plugin_default_build_runner_returns_workflow_runner() -> None:
    """The default build_runner() produces a WorkflowRunner."""
    from agenthicc.workflows import WorkflowRunner
    from unittest.mock import MagicMock

    class MyWorkflow(WorkflowPlugin):
        name  = "my_workflow"
        phases = [PhaseSpec(name="phase1")]

    runner = MyWorkflow.build_runner(MagicMock(), None)
    assert isinstance(runner, WorkflowRunner)


@pytest.mark.unit
def test_code_plan_build_runner_is_overridden() -> None:
    """CodePlan.build_runner is overridden and differs from the default."""
    assert CodePlan.build_runner.__func__ is not WorkflowPlugin.build_runner.__func__


# ── build_runner() produces correct runner types ─────────────────────────────

@pytest.mark.unit
def test_custom_workflow_build_runner_returns_workflow_runner() -> None:
    """A custom WorkflowPlugin subclass with no override returns a WorkflowRunner."""
    from agenthicc.workflows import WorkflowRunner
    from unittest.mock import MagicMock

    class CustomWorkflow(WorkflowPlugin):
        name   = "custom"
        phases = [PhaseSpec(name="p")]

    runner = CustomWorkflow.build_runner(MagicMock(), None)
    assert isinstance(runner, WorkflowRunner)


@pytest.mark.unit
def test_code_plan_build_runner_builds_code_plan_runner() -> None:
    """CodePlan.build_runner() returns a CodePlanRunner instance."""
    from agenthicc.workflows.code_plan import CodePlanRunner
    from unittest.mock import MagicMock

    runner = CodePlan.build_runner(MagicMock(), None)
    assert isinstance(runner, CodePlanRunner)


# ── load_builtin_workflows integration ───────────────────────────────────────

@pytest.mark.unit
def test_load_builtin_workflows_all_have_build_runner() -> None:
    """All builtin WorkflowPlugin classes expose a build_runner classmethod."""
    for plugin_cls in load_builtin_workflows():
        assert callable(getattr(plugin_cls, "build_runner", None)), (
            f"Workflow {plugin_cls.name!r} has no build_runner"
        )


# ── No name-based dispatch in tui_session ────────────────────────────────────

@pytest.mark.unit
def test_tui_session_has_no_code_plan_name_branch() -> None:
    """tui_session.py must not contain hardcoded 'code_plan' name comparisons
    for runner selection."""
    import pathlib
    src = pathlib.Path(
        __file__
    ).parent.parent.parent / "src/agenthicc/runners/tui_session.py"
    text = src.read_text(encoding="utf-8")
    # The old pattern used string comparison for dispatch.
    # A docstring reference is allowed; an equality test is not.
    import re
    bad = re.findall(r'\.name\s*==\s*["\']code_plan["\']', text)
    assert not bad, f"Found name-based dispatch in tui_session.py: {bad}"


@pytest.mark.unit
def test_tui_session_does_not_import_code_plan_runner_for_dispatch() -> None:
    """tui_session.py must not import CodePlanRunner at module level or in
    the two dispatch methods (run_turn / _resume_workflow_task)."""
    import pathlib, ast
    src = pathlib.Path(
        __file__
    ).parent.parent.parent / "src/agenthicc/runners/tui_session.py"
    text = src.read_text(encoding="utf-8")
    # CodePlanRunner should not appear at all (the dispatch is gone)
    assert "CodePlanRunner" not in text, (
        "tui_session.py still references CodePlanRunner"
    )
