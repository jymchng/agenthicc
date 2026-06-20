"""Tests for workflow runner dispatch via factory method (PRD-110)."""
from __future__ import annotations

import pytest

from agenthicc.workflows.plugin import PhaseSpec, WorkflowDefinition, WorkflowPlugin
from agenthicc.workflows.builtins import CodePlan, PlanOnly, ReviewOnly
from agenthicc.workflows.loader import load_builtin_workflows


# ── WorkflowPlugin.runner_factory default ────────────────────────────────────

@pytest.mark.unit
def test_plugin_default_runner_factory_returns_workflow_runner() -> None:
    """The default runner_factory produces a WorkflowRunner."""
    from agenthicc.workflows.runner import WorkflowRunner
    from unittest.mock import MagicMock

    class MyWorkflow(WorkflowPlugin):
        name  = "my_workflow"
        phases = [PhaseSpec(name="phase1")]

    defn = MyWorkflow().to_definition()
    assert defn.runner_factory is not None
    runner = defn.build_runner(MagicMock(), None)
    assert isinstance(runner, WorkflowRunner)


@pytest.mark.unit
def test_code_plan_runner_factory_is_overridden() -> None:
    """CodePlan.runner_factory underlying function differs from the default."""
    # Python classmethods create new wrappers on each access; compare __func__.
    assert CodePlan.runner_factory.__func__ is not WorkflowPlugin.runner_factory.__func__


@pytest.mark.unit
def test_plan_only_uses_default_runner_factory() -> None:
    """PlanOnly uses the inherited generic runner factory."""
    assert PlanOnly.runner_factory.__func__ is WorkflowPlugin.runner_factory.__func__


@pytest.mark.unit
def test_review_only_uses_default_runner_factory() -> None:
    """ReviewOnly uses the inherited generic runner factory."""
    assert ReviewOnly.runner_factory.__func__ is WorkflowPlugin.runner_factory.__func__


# ── to_definition() carries factory ──────────────────────────────────────────

@pytest.mark.unit
def test_to_definition_carries_runner_factory() -> None:
    """to_definition() stores a non-None runner_factory on the definition."""
    class CustomWorkflow(WorkflowPlugin):
        name   = "custom"
        phases = [PhaseSpec(name="p")]

    defn = CustomWorkflow().to_definition()
    assert defn.runner_factory is not None
    # Factory's underlying function matches the class's classmethod.
    assert defn.runner_factory.__func__ is CustomWorkflow.runner_factory.__func__


@pytest.mark.unit
def test_code_plan_definition_carries_override_factory() -> None:
    defn = CodePlan().to_definition(source="builtin")
    assert defn.runner_factory is not None
    assert defn.runner_factory.__func__ is CodePlan.runner_factory.__func__
    assert defn.runner_factory.__func__ is not WorkflowPlugin.runner_factory.__func__


@pytest.mark.unit
def test_plan_only_definition_carries_default_factory() -> None:
    defn = PlanOnly().to_definition(source="builtin")
    assert defn.runner_factory is not None
    assert defn.runner_factory.__func__ is WorkflowPlugin.runner_factory.__func__


# ── WorkflowDefinition.build_runner() ────────────────────────────────────────

@pytest.mark.unit
def test_build_runner_none_factory_returns_workflow_runner() -> None:
    """When runner_factory is None, build_runner() falls back to WorkflowRunner."""
    from agenthicc.workflows.runner import WorkflowRunner
    from unittest.mock import MagicMock

    defn = WorkflowDefinition(
        name="test",
        phases=(PhaseSpec(name="p"),),
        runner_factory=None,
    )
    mock_config = MagicMock()
    runner = defn.build_runner(mock_config, None)
    assert isinstance(runner, WorkflowRunner)


@pytest.mark.unit
def test_build_runner_custom_factory_is_called() -> None:
    """build_runner() delegates to the stored factory."""
    sentinel = object()
    calls: list = []

    def my_factory(defn, config, mode_manager):
        calls.append((defn, config, mode_manager))
        return sentinel  # type: ignore[return-value]

    defn = WorkflowDefinition(
        name="test",
        phases=(PhaseSpec(name="p"),),
        runner_factory=my_factory,
    )
    result = defn.build_runner("cfg", "mm")  # type: ignore[arg-type]
    assert result is sentinel
    assert calls == [(defn, "cfg", "mm")]


@pytest.mark.unit
def test_build_runner_passes_defn_as_first_arg() -> None:
    """The factory receives the WorkflowDefinition as its first argument."""
    received_defn: list = []

    def capture(defn, config, mode_manager):
        received_defn.append(defn)
        from agenthicc.workflows.runner import WorkflowRunner
        from unittest.mock import MagicMock
        return WorkflowRunner(defn, MagicMock(), mode_manager)

    defn = WorkflowDefinition(
        name="test",
        phases=(PhaseSpec(name="p"),),
        runner_factory=capture,
    )
    from unittest.mock import MagicMock
    defn.build_runner(MagicMock(), None)
    assert received_defn[0] is defn


# ── CodePlan factory builds CodePlanRunner ───────────────────────────────────

@pytest.mark.unit
def test_code_plan_factory_builds_code_plan_runner() -> None:
    """CodePlan.runner_factory returns a CodePlanRunner instance."""
    from agenthicc.workflows.code_plan import CodePlanRunner
    from unittest.mock import MagicMock

    defn = CodePlan().to_definition(source="builtin")
    mock_config = MagicMock()
    runner = defn.build_runner(mock_config, None)
    assert isinstance(runner, CodePlanRunner)


@pytest.mark.unit
def test_plan_only_factory_builds_workflow_runner() -> None:
    """PlanOnly.build_runner() returns a WorkflowRunner instance."""
    from agenthicc.workflows.runner import WorkflowRunner
    from unittest.mock import MagicMock

    defn = PlanOnly().to_definition(source="builtin")
    runner = defn.build_runner(MagicMock(), None)
    assert isinstance(runner, WorkflowRunner)


# ── load_builtin_workflows integration ───────────────────────────────────────

@pytest.mark.unit
def test_load_builtin_workflows_all_have_factories() -> None:
    """All builtin WorkflowDefinitions carry a non-None runner_factory."""
    for defn in load_builtin_workflows():
        assert defn.runner_factory is not None, (
            f"Workflow {defn.name!r} has no runner_factory"
        )


@pytest.mark.unit
def test_load_builtin_workflows_code_plan_factory_differs() -> None:
    """The code_plan builtin definition carries the CodePlan-specific factory."""
    defs = {d.name: d for d in load_builtin_workflows()}
    code_plan_defn = defs["code_plan"]
    plan_only_defn = defs["plan_only"]

    assert code_plan_defn.runner_factory is not plan_only_defn.runner_factory


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
