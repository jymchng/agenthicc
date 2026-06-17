"""Unit tests for PRD-101 graph types: EdgeGate, EdgeSpec, PhaseNode,
WorkflowGraph, DataBus, NodeResult, and the make_completion_tool factory."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.workflow.plugin import (
    DataBus,
    EdgeGate,
    EdgeSpec,
    NodeResult,
    PhaseNode,
    WorkflowGraph,
)

pytestmark = pytest.mark.unit


# ── EdgeGate ──────────────────────────────────────────────────────────────────

class TestEdgeGate:
    def test_defaults(self):
        gate = EdgeGate()
        assert gate.kind  == "plan_review"
        assert gate.title == ""

    def test_custom_kind(self):
        gate = EdgeGate(kind="tool_approval", title="Confirm write")
        assert gate.kind  == "tool_approval"
        assert gate.title == "Confirm write"

    def test_immutable(self):
        gate = EdgeGate()
        with pytest.raises((AttributeError, TypeError)):
            gate.kind = "other"  # type: ignore[misc]


# ── EdgeSpec ─────────────────────────────────────────────────────────────────

class TestEdgeSpec:
    def test_minimal(self):
        e = EdgeSpec(target="review", label="complete")
        assert e.target == "review"
        assert e.label  == "complete"
        assert e.gate   is None

    def test_terminal_edge(self):
        e = EdgeSpec(target=None, label="done")
        assert e.target is None

    def test_with_gate(self):
        gate = EdgeGate(kind="plan_review")
        e    = EdgeSpec(target="execute", label="approve", gate=gate)
        assert e.gate is gate
        assert e.gate.kind == "plan_review"

    def test_immutable(self):
        e = EdgeSpec(target="next", label="ok")
        with pytest.raises((AttributeError, TypeError)):
            e.label = "other"  # type: ignore[misc]


# ── PhaseNode ─────────────────────────────────────────────────────────────────

class TestPhaseNode:
    def test_minimal(self):
        node = PhaseNode(name="plan")
        assert node.name              == "plan"
        assert node.agent_config      is None
        assert node.llm_config        is None
        assert node.agent_type        == "auto"
        assert node.edges             == ()
        assert node.allowed_capabilities is None
        assert node.mode_override     is None
        assert node.max_continuations == 10
        assert node.parallel_with     == ()

    def test_with_edges(self):
        node = PhaseNode(
            name  = "review",
            edges = (
                EdgeSpec("summarize", "approve"),
                EdgeSpec("execute",   "reject"),
            ),
        )
        assert len(node.edges) == 2
        assert node.edges[0].label == "approve"
        assert node.edges[1].target == "execute"

    def test_terminal_node(self):
        node = PhaseNode(name="summarize", edges=())
        assert node.edges == ()

    def test_immutable(self):
        node = PhaseNode(name="plan")
        with pytest.raises((AttributeError, TypeError)):
            node.name = "other"  # type: ignore[misc]


# ── WorkflowGraph ─────────────────────────────────────────────────────────────

def _make_graph(**extra) -> WorkflowGraph:
    nodes = {
        "plan":    PhaseNode(name="plan",    edges=(EdgeSpec("execute", "approve"),)),
        "execute": PhaseNode(name="execute", edges=(EdgeSpec("review",  "complete"),)),
        "review":  PhaseNode(name="review",  edges=(EdgeSpec("summarize", "approve"), EdgeSpec("execute", "reject"))),
        "summarize": PhaseNode(name="summarize"),
    }
    return WorkflowGraph(name="test", entry="plan", nodes=nodes, **extra)


class TestWorkflowGraph:
    def test_basic(self):
        g = _make_graph()
        assert g.name  == "test"
        assert g.entry == "plan"
        assert len(g.nodes) == 4

    def test_get_node(self):
        g = _make_graph()
        assert g.get_node("plan") is not None
        assert g.get_node("unknown") is None

    def test_node_index(self):
        g = _make_graph()
        assert g.node_index("plan")      == 0
        assert g.node_index("execute")   == 1
        assert g.node_index("review")    == 2
        assert g.node_index("summarize") == 3

    def test_node_names(self):
        g = _make_graph()
        assert g.node_names() == ["plan", "execute", "review", "summarize"]

    def test_default_max_total_phase_runs(self):
        g = _make_graph()
        assert g.max_total_phase_runs == 0   # unlimited by default

    def test_opt_in_cap(self):
        g = _make_graph(max_total_phase_runs=5)
        assert g.max_total_phase_runs == 5

    def test_immutable(self):
        g = _make_graph()
        with pytest.raises((AttributeError, TypeError)):
            g.name = "other"  # type: ignore[misc]


# ── DataBus ───────────────────────────────────────────────────────────────────

class TestDataBus:
    def test_empty_context_block(self):
        bus = DataBus(intent="do it", run_id="r1")
        block = bus.as_context_block()
        assert "do it" in block
        assert "plan" not in block

    def test_set_and_get(self):
        bus = DataBus(intent="x", run_id="r1")
        bus.set("plan", {"approach": "JWT auth", "files": ["auth.py"]})
        assert bus.get("plan") == {"approach": "JWT auth", "files": ["auth.py"]}

    def test_missing_get(self):
        bus = DataBus(intent="x", run_id="r1")
        assert bus.get("nonexistent") is None

    def test_edge_history(self):
        bus = DataBus(intent="x", run_id="r1")
        bus.record_edge("plan", "approve")
        assert bus.edge_history["plan"] == "approve"

    def test_context_block_with_outputs(self):
        bus = DataBus(intent="add auth", run_id="r1")
        bus.set("plan", {"approach": "JWT", "files": ["a.py", "b.py"]})
        bus.set("execute", {"modified": ["a.py"], "tests_pass": True})
        block = bus.as_context_block()
        assert "add auth" in block
        assert "plan:"    in block
        assert "approach" in block
        assert "JWT"      in block
        assert "execute:" in block

    def test_context_block_skips_internal_keys(self):
        bus = DataBus(intent="x", run_id="r1")
        bus.set("plan", {"_edge_label": "approve", "real_key": "real_value"})
        block = bus.as_context_block()
        assert "_edge_label" not in block
        assert "real_value"  in block

    def test_long_value_truncated(self):
        bus = DataBus(intent="x", run_id="r1")
        long_val = "x" * 1000
        bus.set("plan", {"text": long_val})
        block = bus.as_context_block()
        assert "…" in block   # truncation marker


# ── NodeResult ────────────────────────────────────────────────────────────────

class TestNodeResult:
    def test_basic(self):
        r = NodeResult(node_name="plan", edge_label="approve", output={"plan": "step 1"})
        assert r.node_name  == "plan"
        assert r.edge_label == "approve"
        assert r.output     == {"plan": "step 1"}
        assert r.duration_s == 0.0

    def test_terminal(self):
        r = NodeResult(node_name="summarize", edge_label=None, output={})
        assert r.edge_label is None

    def test_failed(self):
        r = NodeResult(node_name="execute", edge_label=None, output={}, duration_s=5.0)
        assert r.duration_s == 5.0


# ── make_transition_tools ────────────────────────────────────────────────────

def _get_tool(tools: list, name: str):
    """Find a tool from the list by its __name__."""
    from lauren_ai._tools import TOOL_META
    for t in tools:
        meta = getattr(t, TOOL_META, None)
        if meta and getattr(meta, "name", None) == name:
            return t
        if getattr(t, "__name__", None) == name:
            return t
    raise KeyError(f"No tool named {name!r} in {[getattr(t,'__name__','?') for t in tools]}")


class TestMakeTransitionTools:
    def _make_tools(self, node, approval_svc=None):
        from agenthicc.workflow.phase_tools import make_transition_tools
        from agenthicc.workflow.plugin import DataBus
        data_bus         = DataBus(intent="x", run_id="r")
        transition_event = asyncio.Event()
        transition_data: dict = {}
        tools = make_transition_tools(
            node, data_bus, transition_event, transition_data, approval_svc
        )
        return tools, data_bus, transition_event, transition_data

    async def test_terminal_node_returns_one_finish_tool(self):
        node = PhaseNode(name="summarize", edges=())
        tools, bus, ev, td = self._make_tools(node)
        assert len(tools) == 1
        finish = tools[0]
        result = await finish(output={"summary": "done"})
        assert result["ok"]   is True
        assert ev.is_set()
        assert td["edge_label"] is None
        assert td["output"]     == {"summary": "done"}
        assert bus.get("summarize") == {"summary": "done"}

    async def test_terminal_tool_name_is_finish(self):
        node  = PhaseNode(name="summarize", edges=())
        tools, _, _, _ = self._make_tools(node)
        assert tools[0].__name__ == "finish"

    async def test_single_edge_creates_one_named_tool(self):
        node = PhaseNode(name="plan", edges=(EdgeSpec("execute", "approve"),))
        tools, bus, ev, td = self._make_tools(node)
        assert len(tools) == 1
        assert tools[0].__name__ == "approve"
        result = await tools[0](output={"plan": "step 1"})
        assert result["ok"]   is True
        assert ev.is_set()
        assert td["edge_label"] == "approve"
        assert bus.get("plan")   == {"plan": "step 1"}

    async def test_two_edges_create_two_named_tools(self):
        node = PhaseNode(
            name  = "review",
            edges = (EdgeSpec("summarize", "approve"), EdgeSpec("execute", "reject")),
        )
        tools, _, ev, td = self._make_tools(node)
        assert len(tools) == 2
        names = {t.__name__ for t in tools}
        assert names == {"approve", "reject"}

    async def test_reject_tool_fires(self):
        node = PhaseNode(
            name  = "review",
            edges = (EdgeSpec("summarize", "approve"), EdgeSpec("execute", "reject")),
        )
        tools, _, ev, td = self._make_tools(node)
        reject = _get_tool(tools, "reject")
        result = await reject(output={"issues": ["test failure"]})
        assert result["ok"]     is True
        assert td["edge_label"] == "reject"
        assert ev.is_set()

    async def test_gated_edge_allowed(self):
        approval_svc = MagicMock()
        from agenthicc.tools.approval import ApprovalResponse
        approval_svc.request_approval = AsyncMock(
            return_value=ApprovalResponse(allowed=True, message="Looks good!")
        )
        node = PhaseNode(
            name  = "plan",
            edges = (EdgeSpec("execute", "approve", gate=EdgeGate(kind="plan_review")),),
        )
        tools, bus, ev, td = self._make_tools(node, approval_svc=approval_svc)
        approve = _get_tool(tools, "approve")
        result  = await approve(output={"plan": "do stuff"})
        assert result["ok"] is True
        assert ev.is_set()
        assert bus.get("plan").get("_user_instructions") == "Looks good!"

    async def test_gated_edge_denied(self):
        approval_svc = MagicMock()
        from agenthicc.tools.approval import ApprovalResponse
        approval_svc.request_approval = AsyncMock(
            return_value=ApprovalResponse(allowed=False, message="Not ready yet")
        )
        node = PhaseNode(
            name  = "plan",
            edges = (EdgeSpec("execute", "approve", gate=EdgeGate(kind="plan_review")),),
        )
        tools, _, ev, _ = self._make_tools(node, approval_svc=approval_svc)
        approve = _get_tool(tools, "approve")
        result  = await approve(output={"plan": "draft"})
        assert result.get("approved") is False
        assert "Not ready yet"        in result.get("feedback", "")
        assert not ev.is_set()

    async def test_headless_no_approval_svc(self):
        """Gate present but approval_svc=None → transition committed immediately."""
        node = PhaseNode(
            name  = "plan",
            edges = (EdgeSpec("execute", "approve", gate=EdgeGate(kind="plan_review")),),
        )
        tools, _, ev, td = self._make_tools(node, approval_svc=None)
        approve = _get_tool(tools, "approve")
        result  = await approve(output={"plan": "draft"})
        assert result["ok"] is True
        assert ev.is_set()
        assert td["edge_label"] == "approve"
