"""Unit tests: workflow/plugin.py — PhaseNode, WorkflowGraph, DataBus (PRD-101)."""
from __future__ import annotations

import pytest

from agenthicc.agents.plugin import READ_CAPS
from agenthicc.tools.capabilities import ToolCapability
from agenthicc.workflow.plugin import (
    DataBus, EdgeGate, EdgeSpec, NodeResult,
    PhaseNode, PhaseRole, WorkflowGraph, WorkflowPlugin,
)

pytestmark = pytest.mark.unit


class TestPhaseRole:
    def test_planner_value(self):
        assert PhaseRole.PLANNER == "planner"

    def test_executor_value(self):
        assert PhaseRole.EXECUTOR == "executor"

    def test_is_string(self):
        assert isinstance(PhaseRole.PLANNER, str)

    def test_usable_as_agent_type(self):
        node = PhaseNode(name="p", agent_type=PhaseRole.PLANNER)
        assert node.agent_type == "planner"


class TestPhaseNode:
    def test_defaults(self):
        node = PhaseNode(name="p")
        assert node.agent_type        == "auto"
        assert node.allowed_capabilities is None
        assert node.max_continuations == 10
        assert node.edges             == ()
        assert node.mode_override     is None
        assert node.agent_config      is None
        assert node.llm_config        is None

    def test_with_edges(self):
        node = PhaseNode(
            name  = "review",
            edges = (EdgeSpec("summarize", "approve"), EdgeSpec("execute", "reject")),
        )
        assert len(node.edges) == 2
        assert node.edges[0].label  == "approve"
        assert node.edges[1].target == "execute"

    def test_terminal(self):
        assert PhaseNode(name="summarize").edges == ()

    def test_frozen(self):
        node = PhaseNode(name="p")
        with pytest.raises((AttributeError, TypeError)):
            node.name = "other"  # type: ignore[misc]


class TestWorkflowGraph:
    def _wf(self, *names):
        nodes = {n: PhaseNode(name=n) for n in names}
        return WorkflowGraph(name="wf", entry=names[0] if names else "", nodes=nodes)

    def test_get_node_found(self):
        assert self._wf("plan", "execute").get_node("plan").name == "plan"

    def test_get_node_missing(self):
        assert self._wf("plan").get_node("x") is None

    def test_node_index(self):
        wf = self._wf("plan", "execute", "review", "summarize")
        assert wf.node_index("plan")      == 0
        assert wf.node_index("execute")   == 1
        assert wf.node_index("review")    == 2
        assert wf.node_index("summarize") == 3

    def test_node_names(self):
        assert self._wf("a", "b", "c").node_names() == ["a", "b", "c"]

    def test_default_max_total_phase_runs(self):
        assert self._wf("p").max_total_phase_runs == 0

    def test_frozen(self):
        wf = self._wf("p")
        with pytest.raises((AttributeError, TypeError)):
            wf.name = "other"  # type: ignore[misc]


class TestDataBus:
    def test_empty_context_block(self):
        bus = DataBus(intent="Fix bug", run_id="r1")
        block = bus.as_context_block()
        assert "Original intent: Fix bug" in block
        assert "plan" not in block

    def test_set_and_get(self):
        bus = DataBus(intent="x", run_id="r1")
        bus.set("plan", {"approach": "JWT", "files": ["auth.py"]})
        assert bus.get("plan") == {"approach": "JWT", "files": ["auth.py"]}

    def test_get_missing(self):
        assert DataBus(intent="x", run_id="r").get("nope") is None

    def test_edge_history(self):
        bus = DataBus(intent="x", run_id="r")
        bus.record_edge("plan", "approve")
        assert bus.edge_history["plan"] == "approve"

    def test_context_block_with_outputs(self):
        bus = DataBus(intent="add auth", run_id="r")
        bus.set("plan", {"approach": "JWT", "files": ["a.py"]})
        bus.set("execute", {"modified": ["a.py"], "tests_pass": True})
        block = bus.as_context_block()
        assert "add auth" in block
        assert "plan:"    in block
        assert "JWT"      in block
        assert "execute:" in block

    def test_internal_keys_skipped(self):
        bus = DataBus(intent="x", run_id="r")
        bus.set("plan", {"_edge_label": "approve", "real_key": "real_value"})
        assert "_edge_label" not in bus.as_context_block()
        assert "real_value"  in bus.as_context_block()

    def test_long_values_truncated(self):
        bus = DataBus(intent="x", run_id="r")
        bus.set("plan", {"text": "x" * 1000})
        assert "…" in bus.as_context_block()


class TestNodeResult:
    def test_basic(self):
        r = NodeResult(node_name="plan", edge_label="approve", output={"plan": "step 1"})
        assert r.node_name  == "plan"
        assert r.edge_label == "approve"
        assert r.duration_s == 0.0

    def test_terminal(self):
        r = NodeResult(node_name="summarize", edge_label=None, output={})
        assert r.edge_label is None


class TestWorkflowPlugin:
    def test_to_definition_from_graph(self):
        class MyWf(WorkflowPlugin):
            name          = "my_wf"
            description   = "d"
            mode_bindings = ["Plan"]
            graph = WorkflowGraph(
                name="my_wf", entry="p",
                nodes={"p": PhaseNode(name="p", agent_type=PhaseRole.PLANNER)},
            )

        defn = MyWf().to_definition(source="user", path="/tmp/x.py")
        assert defn.name   == "my_wf"
        assert "Plan"      in defn.mode_bindings
        assert defn.source == "user"
        assert defn.path   == "/tmp/x.py"

    def test_to_definition_no_graph_raises(self):
        class NoGraph(WorkflowPlugin):
            name = "broken"

        with pytest.raises(NotImplementedError):
            NoGraph().to_definition()
