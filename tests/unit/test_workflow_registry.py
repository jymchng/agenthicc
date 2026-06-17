"""Unit tests for workflow/registry.py and workflow/loader.py (PRD-101)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agenthicc.workflow.loader import load_python_workflows
from agenthicc.workflow.plugin import EdgeSpec, PhaseNode, PhaseRole, WorkflowGraph, WorkflowPlugin
from agenthicc.workflow.registry import WorkflowRegistry, build_workflow_registry

pytestmark = pytest.mark.unit


def _make_graph(name: str, source: str = "builtin", **kwargs) -> WorkflowGraph:
    return WorkflowGraph(
        name=name, entry="p",
        nodes={"p": PhaseNode(name="p")},
        source=source, **kwargs,
    )


def _write_py(tmp_path: Path, content: str, filename: str = "test_wf.py") -> Path:
    path = tmp_path / filename
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


class TestWorkflowRegistry:
    def test_register_and_get(self):
        registry = WorkflowRegistry()
        defn = _make_graph("my_workflow")
        registry.register(defn)
        assert registry.get("my_workflow") is defn

    def test_get_missing_returns_none(self):
        assert WorkflowRegistry().get("does_not_exist") is None

    def test_all_returns_all(self):
        registry = WorkflowRegistry()
        for i in range(3):
            registry.register(_make_graph(f"wf_{i}"))
        assert len(registry.all()) == 3

    def test_later_registration_wins(self):
        registry = WorkflowRegistry()
        registry.register(_make_graph("my_wf", description="first"))
        registry.register(_make_graph("my_wf", description="second"))
        assert registry.get("my_wf").description == "second"

    def test_names(self):
        registry = WorkflowRegistry()
        for n in ("alpha", "beta", "gamma"):
            registry.register(_make_graph(n))
        assert set(registry.names()) == {"alpha", "beta", "gamma"}

    def test_mode_default_map(self):
        registry = WorkflowRegistry()
        registry.register(_make_graph("wf_a", mode_bindings=("Plan",)))
        registry.register(_make_graph("wf_b", mode_bindings=("Plan", "Review")))
        m = registry.mode_default_map()
        assert m["Plan"]   == "wf_a"
        assert m["Review"] == "wf_b"

    def test_mode_available_map(self):
        registry = WorkflowRegistry()
        registry.register(_make_graph("wf_a", mode_bindings=("Plan",)))
        registry.register(_make_graph("wf_b", mode_bindings=("Plan", "Review")))
        m = registry.mode_available_map()
        assert set(m["Plan"]) == {"wf_a", "wf_b"}
        assert m["Review"]    == ["wf_b"]


class TestBuildWorkflowRegistry:
    def test_includes_builtins(self):
        registry = build_workflow_registry()
        assert "plan_only"  in registry.names()
        assert "supervised" in registry.names()
        assert "architect"  in registry.names()
        assert "code_plan"  in registry.names()

    def test_plan_only_has_one_node(self):
        defn = build_workflow_registry().get("plan_only")
        assert defn is not None
        assert len(defn.nodes) == 1
        assert "plan" in defn.nodes

    def test_supervised_has_three_nodes(self):
        defn = build_workflow_registry().get("supervised")
        assert len(defn.nodes) == 3

    def test_architect_has_four_nodes(self):
        defn = build_workflow_registry().get("architect")
        assert len(defn.nodes) == 4
        assert set(defn.node_names()) == {"explore", "plan", "execute", "verify"}

    def test_plan_only_source_is_builtin(self):
        assert build_workflow_registry().get("plan_only").source == "builtin"

    def test_plan_only_mode_bindings(self):
        assert "Review" in build_workflow_registry().get("plan_only").mode_bindings

    def test_code_plan_mode_bindings(self):
        defn = build_workflow_registry().get("code_plan")
        assert defn is not None
        assert "Plan" in defn.mode_bindings

    def test_code_plan_has_four_nodes(self):
        defn = build_workflow_registry().get("code_plan")
        assert len(defn.nodes) == 4
        assert set(defn.node_names()) == {"plan", "execute", "review", "summarize"}

    def test_code_plan_plan_node_has_approval_gate(self):
        defn  = build_workflow_registry().get("code_plan")
        plan  = defn.get_node("plan")
        gates = [e.gate for e in plan.edges if e.gate is not None]
        assert len(gates) == 1
        assert gates[0].kind == "plan_review"

    def test_mode_default_map_has_plan(self):
        m = build_workflow_registry().mode_default_map()
        assert "Plan" in m and m["Plan"] == "code_plan"

    def test_user_dir_missing_does_not_fail(self, tmp_path):
        assert isinstance(
            build_workflow_registry(user_dir=tmp_path / "nonexistent"),
            WorkflowRegistry,
        )


class TestPythonLoader:
    def test_load_python_plugin(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, WorkflowGraph, PhaseNode, PhaseRole

            class MyWorkflow(WorkflowPlugin):
                name = "my_plugin_workflow"
                description = "A test plugin workflow."
                graph = WorkflowGraph(
                    name="my_plugin_workflow", entry="plan",
                    nodes={
                        "plan":    PhaseNode(name="plan",    agent_type=PhaseRole.PLANNER,
                                            edges=()),
                        "execute": PhaseNode(name="execute", agent_type=PhaseRole.EXECUTOR),
                    },
                )
        """
        results = load_python_workflows(_write_py(tmp_path, content), "user")
        assert len(results) == 1
        defn = results[0]
        assert defn.name == "my_plugin_workflow"
        assert len(defn.nodes) == 2

    def test_load_python_source_stored(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, WorkflowGraph, PhaseNode

            class P(WorkflowPlugin):
                name = "sourced"
                graph = WorkflowGraph(name="sourced", entry="p", nodes={"p": PhaseNode(name="p")})
        """
        results = load_python_workflows(_write_py(tmp_path, content), "project")
        assert results[0].source == "project"

    def test_load_python_multiple_plugins(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, WorkflowGraph, PhaseNode

            class Alpha(WorkflowPlugin):
                name = "alpha_wf"
                graph = WorkflowGraph(name="alpha_wf", entry="p", nodes={"p": PhaseNode(name="p")})

            class Beta(WorkflowPlugin):
                name = "beta_wf"
                graph = WorkflowGraph(name="beta_wf", entry="q", nodes={"q": PhaseNode(name="q")})
        """
        results = load_python_workflows(_write_py(tmp_path, content), "user")
        assert len(results) == 2
        assert {d.name for d in results} == {"alpha_wf", "beta_wf"}

    def test_load_python_invalid_returns_empty(self, tmp_path):
        assert load_python_workflows(_write_py(tmp_path, "def broken {{ {{"), "user") == []

    def test_load_python_no_subclasses_returns_empty(self, tmp_path):
        assert load_python_workflows(_write_py(tmp_path, "class SomethingElse: pass"), "user") == []

    def test_load_python_unnamed_skipped(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, WorkflowGraph, PhaseNode

            class Unnamed(WorkflowPlugin):
                name = ""
                graph = WorkflowGraph(name="", entry="p", nodes={"p": PhaseNode(name="p")})
        """
        assert load_python_workflows(_write_py(tmp_path, content), "user") == []

    def test_load_python_mode_bindings_preserved(self, tmp_path):
        content = """
            from agenthicc.workflow.plugin import WorkflowPlugin, WorkflowGraph, PhaseNode

            class Bound(WorkflowPlugin):
                name = "bound"
                mode_bindings = ["Plan", "Review"]
                graph = WorkflowGraph(name="bound", entry="p", nodes={"p": PhaseNode(name="p")})
        """
        results = load_python_workflows(_write_py(tmp_path, content), "user")
        assert "Plan"   in results[0].mode_bindings
        assert "Review" in results[0].mode_bindings
