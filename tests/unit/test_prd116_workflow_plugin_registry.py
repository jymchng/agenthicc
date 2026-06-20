"""Tests for PRD-116: WorkflowPlugin as registry artifact, WorkflowDefinition removed."""
from __future__ import annotations

import pytest

from agenthicc.workflows.plugin import (
    PhaseSpec,
    WorkflowEntry,
    WorkflowParams,
    WorkflowPlugin,
)
from agenthicc.workflows.registry import WorkflowRegistry
from agenthicc.workflows.loader import load_builtin_workflows


# ── WorkflowPlugin class attributes ──────────────────────────────────────────

@pytest.mark.unit
def test_plugin_has_max_total_phase_runs_attribute() -> None:
    assert hasattr(WorkflowPlugin, "max_total_phase_runs")
    assert WorkflowPlugin.max_total_phase_runs == 0


@pytest.mark.unit
def test_plugin_subclass_can_override_max_total_phase_runs() -> None:
    class Capped(WorkflowPlugin):
        name = "capped"
        phases = [PhaseSpec(name="p")]
        max_total_phase_runs = 7

    assert Capped.max_total_phase_runs == 7


# ── WorkflowPlugin classmethods ───────────────────────────────────────────────

@pytest.mark.unit
def test_first_phase_returns_none_for_empty_phases() -> None:
    class Empty(WorkflowPlugin):
        name = "empty"
        phases = []

    assert Empty.first_phase() is None


@pytest.mark.unit
def test_first_phase_returns_first_spec() -> None:
    class Wf(WorkflowPlugin):
        name = "wf"
        phases = [PhaseSpec(name="a"), PhaseSpec(name="b")]

    assert Wf.first_phase().name == "a"


@pytest.mark.unit
def test_get_phase_returns_matching_spec() -> None:
    class Wf(WorkflowPlugin):
        name = "wf"
        phases = [PhaseSpec(name="plan"), PhaseSpec(name="execute")]

    assert Wf.get_phase("execute").name == "execute"


@pytest.mark.unit
def test_get_phase_returns_none_for_unknown() -> None:
    class Wf(WorkflowPlugin):
        name = "wf"
        phases = [PhaseSpec(name="plan")]

    assert Wf.get_phase("unknown") is None


@pytest.mark.unit
def test_phase_names_returns_ordered_list() -> None:
    class Wf(WorkflowPlugin):
        name = "wf"
        phases = [PhaseSpec(name="a"), PhaseSpec(name="b"), PhaseSpec(name="c")]

    assert Wf.phase_names() == ["a", "b", "c"]


# ── WorkflowPlugin factory methods ───────────────────────────────────────────

@pytest.mark.unit
def test_build_params_default_returns_base_params() -> None:
    class Wf(WorkflowPlugin):
        name = "wf"

    result = Wf.build_params({"anything": "value"})
    assert isinstance(result, WorkflowParams)


@pytest.mark.unit
def test_build_params_override_returns_custom_params() -> None:
    class MyParams(WorkflowParams):
        pass

    class Wf(WorkflowPlugin):
        name = "wf"

        @classmethod
        def build_params(cls, source: dict) -> WorkflowParams:
            return MyParams()

    result = Wf.build_params({})
    assert isinstance(result, MyParams)


@pytest.mark.unit
def test_build_runner_default_returns_workflow_runner() -> None:
    from unittest.mock import MagicMock
    from agenthicc.workflows.default.runner import WorkflowRunner

    class Wf(WorkflowPlugin):
        name = "wf"
        phases = [PhaseSpec(name="p")]

    mock_cfg = MagicMock()
    runner = Wf.build_runner(mock_cfg, None)
    assert isinstance(runner, WorkflowRunner)
    assert runner._plugin is Wf


@pytest.mark.unit
def test_build_runner_override_returns_custom_runner() -> None:
    from unittest.mock import MagicMock
    from agenthicc.workflows.base_runner import BaseWorkflowRunner

    class MyRunner(BaseWorkflowRunner):
        async def run(self, intent: str) -> None: ...
        async def resume(self, context) -> None: ...

    class Wf(WorkflowPlugin):
        name = "wf"

        @classmethod
        def build_runner(cls, config, mode_manager):
            return MyRunner()

    runner = Wf.build_runner(MagicMock(), None)
    assert isinstance(runner, MyRunner)


# ── WorkflowDefinition is gone ────────────────────────────────────────────────

@pytest.mark.unit
def test_workflow_definition_does_not_exist() -> None:
    import agenthicc.workflows.plugin as mod
    assert not hasattr(mod, "WorkflowDefinition"), (
        "WorkflowDefinition was deleted in PRD-116 but still exists"
    )


@pytest.mark.unit
def test_workflow_plugin_has_no_to_definition() -> None:
    assert not hasattr(WorkflowPlugin, "to_definition"), (
        "to_definition() was deleted in PRD-116"
    )


@pytest.mark.unit
def test_workflow_plugin_has_no_runner_factory_attribute() -> None:
    assert not hasattr(WorkflowPlugin, "runner_factory"), (
        "runner_factory classmethod was deleted in PRD-116; use build_runner()"
    )


@pytest.mark.unit
def test_workflow_plugin_has_no_params_factory_attribute() -> None:
    assert not hasattr(WorkflowPlugin, "params_factory"), (
        "params_factory classmethod was deleted in PRD-116; use build_params()"
    )


# ── WorkflowEntry ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_workflow_entry_is_importable() -> None:
    from agenthicc.workflows.plugin import WorkflowEntry
    assert WorkflowEntry is not None


@pytest.mark.unit
def test_workflow_entry_stores_plugin_class() -> None:
    class Wf(WorkflowPlugin):
        name = "wf"
        phases = [PhaseSpec(name="p")]
        mode_bindings = ["Auto"]

    entry = WorkflowEntry(plugin_cls=Wf, source="user", path="/some/path.py")
    assert entry.plugin_cls is Wf
    assert entry.source == "user"
    assert entry.path == "/some/path.py"


# ── WorkflowRegistry ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_registry_register_and_get_plugin_class() -> None:
    class Wf(WorkflowPlugin):
        name = "my_wf"
        phases = [PhaseSpec(name="p")]

    reg = WorkflowRegistry()
    reg.register(Wf, source="user")
    assert reg.get("my_wf") is Wf


@pytest.mark.unit
def test_registry_get_returns_none_for_unknown() -> None:
    reg = WorkflowRegistry()
    assert reg.get("nonexistent") is None


@pytest.mark.unit
def test_registry_get_entry_returns_workflow_entry() -> None:
    class Wf(WorkflowPlugin):
        name = "wf"

    reg = WorkflowRegistry()
    reg.register(Wf, source="project", path="/p.py")
    entry = reg.get_entry("wf")
    assert isinstance(entry, WorkflowEntry)
    assert entry.plugin_cls is Wf
    assert entry.source == "project"
    assert entry.path == "/p.py"


@pytest.mark.unit
def test_registry_mode_default_map() -> None:
    class A(WorkflowPlugin):
        name = "a"; mode_bindings = ["Plan"]
    class B(WorkflowPlugin):
        name = "b"; mode_bindings = ["Plan", "Auto"]

    reg = WorkflowRegistry()
    reg.register(A)
    reg.register(B)
    dm = reg.mode_default_map()
    assert dm["Plan"] == "a"   # first-registered wins
    assert dm["Auto"] == "b"


@pytest.mark.unit
def test_registry_mode_available_map() -> None:
    class A(WorkflowPlugin):
        name = "a"; mode_bindings = ["Plan"]
    class B(WorkflowPlugin):
        name = "b"; mode_bindings = ["Plan"]

    reg = WorkflowRegistry()
    reg.register(A)
    reg.register(B)
    am = reg.mode_available_map()
    assert set(am["Plan"]) == {"a", "b"}


# ── loader ────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_load_builtin_workflows_returns_plugin_classes() -> None:
    plugins = load_builtin_workflows()
    assert all(issubclass(p, WorkflowPlugin) for p in plugins)
    names = {p.name for p in plugins}
    assert "code_plan" in names


@pytest.mark.unit
def test_load_builtin_workflows_no_to_definition_call() -> None:
    """load_builtin_workflows() returns classes, not instances or definitions."""
    plugins = load_builtin_workflows()
    for p in plugins:
        assert isinstance(p, type), f"{p!r} is not a class"
        assert issubclass(p, WorkflowPlugin)
