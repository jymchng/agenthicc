"""Unit tests for workflow/registry.py and workflow/loader.py (PRD-87, PRD-116)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agenthicc.workflows.loader import load_python_workflows
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin
from agenthicc.workflows.registry import WorkflowRegistry, build_workflow_registry

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_plugin(
    name: str,
    description: str = "",
    mode_bindings: tuple[str, ...] = (),
    phases: tuple[PhaseSpec, ...] = (),
    **_kwargs,
) -> type[WorkflowPlugin]:
    _phases = list(phases)
    _mode_bindings = list(mode_bindings)
    _description = description

    class _Plugin(WorkflowPlugin):
        pass

    _Plugin.name = name
    _Plugin.description = _description
    _Plugin.mode_bindings = _mode_bindings
    _Plugin.phases = _phases
    return _Plugin


def _write_py(tmp_path: Path, content: str, filename: str = "test_wf.py") -> Path:
    path = tmp_path / filename
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


# ── TestWorkflowRegistry ──────────────────────────────────────────────────────


class TestWorkflowRegistry:
    def test_register_and_get(self):
        registry = WorkflowRegistry()
        plugin_cls = _make_plugin("my_workflow")
        registry.register(plugin_cls)
        assert registry.get("my_workflow") is plugin_cls

    def test_get_missing_returns_none(self):
        assert WorkflowRegistry().get("does_not_exist") is None

    def test_all_returns_all(self):
        registry = WorkflowRegistry()
        for i in range(3):
            registry.register(_make_plugin(f"wf_{i}"))
        assert len(registry.all()) == 3

    def test_later_registration_wins(self):
        registry = WorkflowRegistry()
        registry.register(_make_plugin("my_wf", description="first"))
        registry.register(_make_plugin("my_wf", description="second"))
        result = registry.get("my_wf")
        assert result is not None
        assert result.description == "second"

    def test_names(self):
        registry = WorkflowRegistry()
        for n in ("alpha", "beta", "gamma"):
            registry.register(_make_plugin(n))
        assert set(registry.names()) == {"alpha", "beta", "gamma"}

    def test_mode_default_map(self):
        registry = WorkflowRegistry()
        registry.register(_make_plugin("wf_a", mode_bindings=("Plan",)))
        registry.register(_make_plugin("wf_b", mode_bindings=("Plan", "Review")))
        m = registry.mode_default_map()
        # first-registered wins for Plan
        assert m["Plan"] == "wf_a"
        assert m["Review"] == "wf_b"

    def test_mode_available_map(self):
        registry = WorkflowRegistry()
        registry.register(_make_plugin("wf_a", mode_bindings=("Plan",)))
        registry.register(_make_plugin("wf_b", mode_bindings=("Plan", "Review")))
        m = registry.mode_available_map()
        assert set(m["Plan"]) == {"wf_a", "wf_b"}
        assert m["Review"] == ["wf_b"]


# ── TestBuildWorkflowRegistry ─────────────────────────────────────────────────


class TestBuildWorkflowRegistry:
    def test_code_plan_mode_bindings(self):
        plugin_cls = build_workflow_registry().get("code_plan")
        assert plugin_cls is not None
        assert "Plan" in plugin_cls.mode_bindings

    def test_code_plan_has_four_phases(self):
        # Single-agent workflow: plan / execute / review / summarize.
        # explore phase removed — exploration happens during plan phase.
        plugin_cls = build_workflow_registry().get("code_plan")
        assert plugin_cls is not None
        assert len(plugin_cls.phases) == 4
        assert set(plugin_cls.phase_names()) == {"plan", "execute", "review", "summarize"}

    def test_code_plan_all_phases_use_auto_agent(self):
        plugin_cls = build_workflow_registry().get("code_plan")
        assert plugin_cls is not None
        assert all(p.agent_type == "auto" for p in plugin_cls.phases)

    def test_code_plan_phases_have_system_prompt_override(self):
        plugin_cls = build_workflow_registry().get("code_plan")
        assert plugin_cls is not None
        assert all(p.system_prompt_override for p in plugin_cls.phases)

    def test_mode_default_map_has_plan(self):
        m = build_workflow_registry().mode_default_map()
        assert "Plan" in m
        assert m["Plan"] == "code_plan"

    def test_user_dir_missing_does_not_fail(self, tmp_path):
        registry = build_workflow_registry(user_dir=tmp_path / "nonexistent")
        assert isinstance(registry, WorkflowRegistry)

    def test_project_dir_missing_does_not_fail(self, tmp_path):
        registry = build_workflow_registry(project_dir=tmp_path / "no_project")
        assert isinstance(registry, WorkflowRegistry)


# ── TestPythonLoader ──────────────────────────────────────────────────────────


class TestPythonLoader:
    def test_load_python_plugin(self, tmp_path):
        content = """
            from agenthicc.workflows.plugin import WorkflowPlugin, PhaseSpec, PhaseRole

            class MyWorkflow(WorkflowPlugin):
                name = "my_plugin_workflow"
                description = "A test plugin workflow."
                phases = [
                    PhaseSpec(name="plan", agent_type=PhaseRole.PLANNER),
                    PhaseSpec(name="execute", agent_type=PhaseRole.EXECUTOR),
                ]
        """
        path = _write_py(tmp_path, content)
        results = load_python_workflows(path, "user")
        assert len(results) == 1
        plugin_cls = results[0]
        assert plugin_cls.name == "my_plugin_workflow"
        assert len(plugin_cls.phases) == 2

    def test_load_python_source_stored(self, tmp_path):
        content = """
            from agenthicc.workflows.plugin import WorkflowPlugin, PhaseSpec

            class P(WorkflowPlugin):
                name = "sourced"
                phases = [PhaseSpec(name="p")]
        """
        registry = WorkflowRegistry()
        for plugin_cls in load_python_workflows(_write_py(tmp_path, content), "project"):
            registry.register(plugin_cls, source="project")
        entry = registry.get_entry("sourced")
        assert entry is not None
        assert entry.source == "project"

    def test_load_python_multiple_plugins(self, tmp_path):
        content = """
            from agenthicc.workflows.plugin import WorkflowPlugin, PhaseSpec

            class Alpha(WorkflowPlugin):
                name = "alpha_wf"
                phases = [PhaseSpec(name="p")]

            class Beta(WorkflowPlugin):
                name = "beta_wf"
                phases = [PhaseSpec(name="q")]
        """
        results = load_python_workflows(_write_py(tmp_path, content), "user")
        assert len(results) == 2
        assert {cls.name for cls in results} == {"alpha_wf", "beta_wf"}

    def test_load_python_base_class_not_included(self, tmp_path):
        content = """
            from agenthicc.workflows.plugin import WorkflowPlugin, PhaseSpec

            class Real(WorkflowPlugin):
                name = "real"
                phases = [PhaseSpec(name="p")]
        """
        results = load_python_workflows(_write_py(tmp_path, content), "user")
        assert len(results) == 1
        assert results[0].name == "real"

    def test_load_python_invalid_returns_empty(self, tmp_path):
        path = _write_py(tmp_path, "def broken syntax {{ {{ }}")
        assert load_python_workflows(path, "user") == []

    def test_load_python_no_subclasses_returns_empty(self, tmp_path):
        content = """
            class SomethingElse:
                pass
        """
        assert load_python_workflows(_write_py(tmp_path, content), "user") == []

    def test_load_python_unnamed_skipped(self, tmp_path):
        content = """
            from agenthicc.workflows.plugin import WorkflowPlugin, PhaseSpec

            class Unnamed(WorkflowPlugin):
                name = ""
                phases = [PhaseSpec(name="p")]
        """
        assert load_python_workflows(_write_py(tmp_path, content), "user") == []

    def test_load_python_mode_bindings_preserved(self, tmp_path):
        content = """
            from agenthicc.workflows.plugin import WorkflowPlugin, PhaseSpec

            class Bound(WorkflowPlugin):
                name = "bound"
                mode_bindings = ["Plan", "Review"]
                phases = [PhaseSpec(name="p")]
        """
        results = load_python_workflows(_write_py(tmp_path, content), "user")
        assert "Plan" in results[0].mode_bindings
        assert "Review" in results[0].mode_bindings

    def test_load_python_nonexistent_file_returns_empty(self, tmp_path):
        assert load_python_workflows(tmp_path / "missing.py", "user") == []
