"""Unit tests for workflow/registry.py and workflow/loader.py (PRD-87)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agenthicc.workflow.loader import load_python_workflows
from agenthicc.workflow.plugin import PhaseRole, WorkflowDefinition
from agenthicc.workflow.registry import WorkflowRegistry, build_workflow_registry

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_defn(name: str, source: str = "builtin", **kwargs) -> WorkflowDefinition:
    return WorkflowDefinition(name=name, source=source, **kwargs)


def _write_py(tmp_path: Path, content: str, filename: str = "test_wf.py") -> Path:
    path = tmp_path / filename
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


# ── TestWorkflowRegistry ──────────────────────────────────────────────────────

class TestWorkflowRegistry:
    def test_register_and_get(self):
        registry = WorkflowRegistry()
        defn = _make_defn("my_workflow")
        registry.register(defn)
        assert registry.get("my_workflow") is defn

    def test_get_missing_returns_none(self):
        assert WorkflowRegistry().get("does_not_exist") is None

    def test_all_returns_all(self):
        registry = WorkflowRegistry()
        for i in range(3):
            registry.register(_make_defn(f"wf_{i}"))
        assert len(registry.all()) == 3

    def test_later_registration_wins(self):
        registry = WorkflowRegistry()
        registry.register(_make_defn("my_wf", description="first"))
        registry.register(_make_defn("my_wf", description="second"))
        assert registry.get("my_wf").description == "second"

    def test_names(self):
        registry = WorkflowRegistry()
        for n in ("alpha", "beta", "gamma"):
            registry.register(_make_defn(n))
        assert set(registry.names()) == {"alpha", "beta", "gamma"}

    def test_mode_default_map(self):
        registry = WorkflowRegistry()
        registry.register(_make_defn("wf_a", mode_bindings=("Plan",)))
        registry.register(_make_defn("wf_b", mode_bindings=("Plan", "Review")))
        m = registry.mode_default_map()
        # first-registered wins for Plan
        assert m["Plan"] == "wf_a"
        assert m["Review"] == "wf_b"

    def test_mode_available_map(self):
        registry = WorkflowRegistry()
        registry.register(_make_defn("wf_a", mode_bindings=("Plan",)))
        registry.register(_make_defn("wf_b", mode_bindings=("Plan", "Review")))
        m = registry.mode_available_map()
        assert set(m["Plan"]) == {"wf_a", "wf_b"}
        assert m["Review"] == ["wf_b"]


# ── TestBuildWorkflowRegistry ─────────────────────────────────────────────────

class TestBuildWorkflowRegistry:
    def test_includes_builtins(self):
        registry = build_workflow_registry()
        assert "plan_only" in registry.names()
        assert "supervised" in registry.names()
        assert "architect" in registry.names()

    def test_plan_only_has_one_phase(self):
        defn = build_workflow_registry().get("plan_only")
        assert defn is not None
        assert len(defn.phases) == 1
        assert defn.phases[0].name == "plan"

    def test_supervised_has_three_phases(self):
        defn = build_workflow_registry().get("supervised")
        assert len(defn.phases) == 3

    def test_architect_has_four_phases(self):
        defn = build_workflow_registry().get("architect")
        assert len(defn.phases) == 4

    def test_plan_only_source_is_builtin(self):
        assert build_workflow_registry().get("plan_only").source == "builtin"

    def test_plan_only_mode_bindings(self):
        defn = build_workflow_registry().get("plan_only")
        # plan_only now binds Review; Plan mode is served by code_plan
        assert "Review" in defn.mode_bindings

    def test_code_plan_mode_bindings(self):
        defn = build_workflow_registry().get("code_plan")
        assert defn is not None
        assert "Plan" in defn.mode_bindings

    def test_code_plan_has_four_phases(self):
        # Single-agent workflow: plan / execute / review / summarize.
        # explore phase removed — exploration happens during plan phase.
        defn = build_workflow_registry().get("code_plan")
        assert len(defn.phases) == 4
        assert set(defn.phase_names()) == {"plan", "execute", "review", "summarize"}

    def test_code_plan_all_phases_use_auto_agent(self):
        defn = build_workflow_registry().get("code_plan")
        assert all(p.agent_type == "auto" for p in defn.phases)

    def test_code_plan_phases_have_system_prompt_override(self):
        defn = build_workflow_registry().get("code_plan")
        assert all(p.system_prompt_override for p in defn.phases)

    def test_architect_phase_names(self):
        names = build_workflow_registry().get("architect").phase_names()
        assert set(names) == {"explore", "plan", "execute", "verify"}

    def test_mode_default_map_has_plan(self):
        m = build_workflow_registry().mode_default_map()
        assert "Plan" in m
        assert m["Plan"] == "code_plan"

    def test_mode_available_map_has_review(self):
        m = build_workflow_registry().mode_available_map()
        assert "Review" in m
        assert "plan_only" in m["Review"]

    def test_user_dir_missing_does_not_fail(self, tmp_path):
        registry = build_workflow_registry(user_dir=tmp_path / "nonexistent")
        assert isinstance(registry, WorkflowRegistry)

    def test_project_dir_missing_does_not_fail(self, tmp_path):
        registry = build_workflow_registry(project_dir=tmp_path / "no_project")
        assert isinstance(registry, WorkflowRegistry)

    def test_plan_only_uses_planner_agent_type(self):
        defn = build_workflow_registry().get("plan_only")
        assert defn.phases[0].agent_type == PhaseRole.PLANNER


# ── TestPythonLoader ──────────────────────────────────────────────────────────

class TestPythonLoader:
    def test_load_python_plugin(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, PhaseSpec, PhaseRole

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
        defn = results[0]
        assert defn.name == "my_plugin_workflow"
        assert len(defn.phases) == 2

    def test_load_python_source_stored(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, PhaseSpec

            class P(WorkflowPlugin):
                name = "sourced"
                phases = [PhaseSpec(name="p")]
        """
        results = load_python_workflows(_write_py(tmp_path, content), "project")
        assert results[0].source == "project"

    def test_load_python_multiple_plugins(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, PhaseSpec

            class Alpha(WorkflowPlugin):
                name = "alpha_wf"
                phases = [PhaseSpec(name="p")]

            class Beta(WorkflowPlugin):
                name = "beta_wf"
                phases = [PhaseSpec(name="q")]
        """
        results = load_python_workflows(_write_py(tmp_path, content), "user")
        assert len(results) == 2
        assert {d.name for d in results} == {"alpha_wf", "beta_wf"}

    def test_load_python_base_class_not_included(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, PhaseSpec

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
            from agenthicc.workflow.plugin import WorkflowPlugin, PhaseSpec

            class Unnamed(WorkflowPlugin):
                name = ""
                phases = [PhaseSpec(name="p")]
        """
        assert load_python_workflows(_write_py(tmp_path, content), "user") == []

    def test_load_python_mode_bindings_preserved(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, PhaseSpec

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
