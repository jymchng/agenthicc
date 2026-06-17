"""Integration tests for WorkflowRunner with WorkflowGraph (PRD-101).

Uses real EventProcessor + mocked _run_node / _graph_run_node to test
the runner's graph traversal, edge following, UI updates, and kernel events
without live LLM calls.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflow.config import WorkflowConfig
from agenthicc.workflow.plugin import (
    DataBus, EdgeGate, EdgeSpec, NodeResult, PhaseNode, WorkflowGraph,
)
from agenthicc.workflow.runner import WorkflowRunner

pytestmark = pytest.mark.integration


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def app_state():
    return TUIAppState.create()


@pytest.fixture
async def processor(tmp_path):
    k_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=k_state, persist=False)
    t = asyncio.create_task(proc.run())
    yield proc
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)


def _make_runner(graph: WorkflowGraph, app_state, processor) -> WorkflowRunner:
    agent_runner = MagicMock()
    agent_runner._transport = MagicMock()
    agent_runner._signals   = None
    cfg = WorkflowConfig(
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,
        agent_runner=agent_runner,
        approval_svc=None,
        cfg=MagicMock(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=MagicMock(),
    )
    return WorkflowRunner(graph, cfg)


def _patch_run_node(runner: WorkflowRunner, results: dict[str, NodeResult]) -> None:
    """Patch _run_node to return canned results by node name."""
    async def _fake(node, intent, data_bus):
        res = results.get(node.name) or NodeResult(
            node_name=node.name, edge_label=None, output={"auto": True},
        )
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res
    runner._run_node = _fake  # type: ignore[assignment]


def _two_node_graph(**extra) -> WorkflowGraph:
    return WorkflowGraph(
        name  = "test",
        entry = "plan",
        nodes = {
            "plan":    PhaseNode(name="plan",    edges=(EdgeSpec("execute", "complete"),)),
            "execute": PhaseNode(name="execute", edges=()),
        },
        **extra,
    )


def _four_node_graph() -> WorkflowGraph:
    return WorkflowGraph(
        name  = "code_plan",
        entry = "plan",
        nodes = {
            "plan":      PhaseNode(name="plan",    edges=(EdgeSpec("execute",  "approve"), EdgeSpec("plan", "revise"))),
            "execute":   PhaseNode(name="execute", edges=(EdgeSpec("review",   "complete"),)),
            "review":    PhaseNode(name="review",  edges=(EdgeSpec("summarize","approve"), EdgeSpec("execute", "reject"))),
            "summarize": PhaseNode(name="summarize"),
        },
    )


# ── basic traversal ───────────────────────────────────────────────────────────

async def test_two_node_linear_completes(app_state, processor):
    graph  = _two_node_graph()
    runner = _make_runner(graph, app_state, processor)
    _patch_run_node(runner, {
        "plan":    NodeResult("plan",    "complete", {"files": ["a.py"]}),
        "execute": NodeResult("execute", None,       {"done": True}),
    })
    await runner.run("Fix the bug")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert len(wf_run.phase_history) == 2
    assert [r.phase_name for r in wf_run.phase_history] == ["plan", "execute"]


async def test_four_node_happy_path(app_state, processor):
    graph  = _four_node_graph()
    runner = _make_runner(graph, app_state, processor)
    _patch_run_node(runner, {
        "plan":      NodeResult("plan",      "approve", {"plan_text": "step 1"}),
        "execute":   NodeResult("execute",   "complete", {"modified": ["a.py"]}),
        "review":    NodeResult("review",    "approve", {"verdict": "ok"}),
        "summarize": NodeResult("summarize", None,       {"summary": "done"}),
    })
    await runner.run("Enhance the repo")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    names = [r.phase_name for r in wf_run.phase_history]
    assert names == ["plan", "execute", "review", "summarize"]


async def test_review_rejects_back_to_execute(app_state, processor):
    call_count: dict[str, int] = {}
    graph  = _four_node_graph()
    runner = _make_runner(graph, app_state, processor)

    async def _fake(node, intent, data_bus):
        call_count[node.name] = call_count.get(node.name, 0) + 1
        if node.name == "review" and call_count["review"] == 1:
            edge_label = "reject"   # first review: reject
        elif node.name in ("summarize",):
            edge_label = None
        else:
            edge_label = {"plan": "approve", "execute": "complete", "review": "approve"}.get(node.name)
        res = NodeResult(node.name, edge_label, {})
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res
    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("Enhance")

    assert call_count["plan"]      == 1
    assert call_count["execute"]   == 2   # once initially + once after review rejection
    assert call_count["review"]    == 2
    assert call_count["summarize"] == 1
    assert app_state.workflow_run().status == "complete"


async def test_plan_self_loop_revise(app_state, processor):
    call_count: dict[str, int] = {}
    graph  = _four_node_graph()
    runner = _make_runner(graph, app_state, processor)

    async def _fake(node, intent, data_bus):
        call_count[node.name] = call_count.get(node.name, 0) + 1
        # Plan revises once before approving
        if node.name == "plan":
            edge = "approve" if call_count["plan"] >= 2 else "revise"
        elif node.name == "summarize":
            edge = None
        else:
            edge = {"execute": "complete", "review": "approve"}.get(node.name)
        res = NodeResult(node.name, edge, {})
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res
    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("Enhance")

    assert call_count["plan"] == 2
    assert app_state.workflow_run().status == "complete"


# ── edge routing ──────────────────────────────────────────────────────────────

async def test_unknown_node_fails_workflow(app_state, processor):
    # If _follow_edge returns a node name not in the graph, workflow fails.
    graph = WorkflowGraph(
        name  = "test",
        entry = "a",
        nodes = {
            "a": PhaseNode(name="a", edges=(EdgeSpec("nonexistent_node", "go"),)),
        },
    )
    runner = _make_runner(graph, app_state, processor)
    _patch_run_node(runner, {
        "a": NodeResult("a", "go", {}),
    })
    await runner.run("test")
    assert app_state.workflow_run().status == "failed"


async def test_terminal_edge_ends_workflow(app_state, processor):
    graph = WorkflowGraph(
        name  = "test",
        entry = "only",
        nodes = {"only": PhaseNode(name="only", edges=())},
    )
    runner = _make_runner(graph, app_state, processor)
    _patch_run_node(runner, {"only": NodeResult("only", None, {"done": True})})
    await runner.run("test")
    assert app_state.workflow_run().status == "complete"


# ── DataBus accumulation ──────────────────────────────────────────────────────

async def test_data_bus_carries_forward(app_state, processor):
    """Each node can read outputs from all prior nodes via DataBus."""
    observed: dict[str, dict] = {}

    graph  = _two_node_graph()
    runner = _make_runner(graph, app_state, processor)

    async def _fake(node, intent, data_bus):
        observed[node.name] = dict(data_bus.outputs)
        edge = "complete" if node.name == "plan" else None
        output = {f"{node.name}_out": True}
        res = NodeResult(node.name, edge, output)
        data_bus.set(node.name, output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res
    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("test")

    assert "plan" not in observed["plan"]       # plan sees empty bus
    assert "plan" in observed["execute"]         # execute sees plan output


# ── UI state ─────────────────────────────────────────────────────────────────

async def test_phase_index_updates_correctly(app_state, processor):
    """current_phase_index reflects definition position, not cumulative count."""
    observed_indices: list[int] = []

    graph  = _four_node_graph()
    runner = _make_runner(graph, app_state, processor)
    original_run_node = runner._run_node

    async def _spy(node, intent, data_bus):
        observed_indices.append(app_state.workflow_run().current_phase_index)
        edge = {"plan": "approve", "execute": "complete", "review": "approve"}.get(node.name)
        res = NodeResult(node.name, edge, {})
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res
    runner._run_node = _spy  # type: ignore[assignment]
    await runner.run("test")

    assert observed_indices == [0, 1, 2, 3]   # plan=0, execute=1, review=2, summarize=3


# ── global cap ───────────────────────────────────────────────────────────────

async def test_global_cap_stops_loop(app_state, processor):
    graph  = _two_node_graph(max_total_phase_runs=1)
    runner = _make_runner(graph, app_state, processor)
    _patch_run_node(runner, {
        "plan":    NodeResult("plan",    "complete", {}),
        "execute": NodeResult("execute", None,       {}),
    })
    await runner.run("test")
    # cap=1: after plan runs (total_runs=1 < cap? cap fires at total_runs > cap=1 → after 1st run)
    # Actually cap fires when total_runs > max_total_phase_runs
    # plan runs (total_runs=1), cap fires (1 > 1 is False)... let me check
    # The cap: if _max_total > 0 and total_runs > _max_total → with cap=1, fires after 2nd run
    # With cap=1: plan runs (total_runs=1, 1>1 False), execute runs (total_runs=2, 2>1 True → stop)
    wf = app_state.workflow_run()
    assert wf.status == "failed"
    assert len(wf.phase_history) <= 2


async def test_no_cap_by_default(app_state, processor):
    graph  = _two_node_graph()   # max_total_phase_runs=0 (unlimited)
    runner = _make_runner(graph, app_state, processor)
    _patch_run_node(runner, {
        "plan":    NodeResult("plan",    "complete", {}),
        "execute": NodeResult("execute", None,       {}),
    })
    await runner.run("test")
    assert app_state.workflow_run().status == "complete"


# ── Ctrl+C cancellation ───────────────────────────────────────────────────────

async def test_cancellation_marks_failed(app_state, processor):
    graph  = _two_node_graph()
    runner = _make_runner(graph, app_state, processor)

    async def _fake(node, intent, data_bus):
        raise asyncio.CancelledError()
    runner._run_node = _fake  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await runner.run("test")

    assert app_state.workflow_run().status == "failed"


# ── kernel events ─────────────────────────────────────────────────────────────

async def test_kernel_events_emitted(app_state, processor):
    graph  = _two_node_graph()
    runner = _make_runner(graph, app_state, processor)
    _patch_run_node(runner, {
        "plan":    NodeResult("plan",    "complete", {}),
        "execute": NodeResult("execute", None,       {}),
    })
    await runner.run("test")
    await processor.drain()

    event_types = [e.event_type for e in processor.event_log]
    assert "WorkflowRunStarted"    in event_types
    assert "WorkflowPhaseStarted"  in event_types
    assert "WorkflowPhaseCompleted" in event_types
    assert "WorkflowRunCompleted"  in event_types


# ── _follow_edge ─────────────────────────────────────────────────────────────

class TestFollowEdge:
    def _runner(self, graph):
        cfg = MagicMock()
        return WorkflowRunner(graph, cfg)

    def test_follows_matching_label(self):
        graph  = _two_node_graph()
        runner = self._runner(graph)
        node   = graph.get_node("plan")
        assert runner._follow_edge(node, "complete") == "execute"

    def test_returns_none_for_unknown_label(self):
        graph  = _two_node_graph()
        runner = self._runner(graph)
        node   = graph.get_node("plan")
        assert runner._follow_edge(node, "nonexistent") is None

    def test_terminal_node_returns_none(self):
        graph  = _two_node_graph()
        runner = self._runner(graph)
        node   = graph.get_node("execute")   # no edges
        assert runner._follow_edge(node, None) is None

    def test_terminal_edge_target_none(self):
        graph = WorkflowGraph(
            name="t", entry="a",
            nodes={"a": PhaseNode(name="a", edges=(EdgeSpec(None, "done"),))},
        )
        runner = self._runner(graph)
        node   = graph.get_node("a")
        assert runner._follow_edge(node, "done") is None


# ── _graph_find_resume_node ──────────────────────────────────────────────────

class TestFindResumeNode:
    def _runner(self, graph):
        cfg = MagicMock()
        return WorkflowRunner(graph, cfg)

    def test_empty_bus_returns_entry(self):
        graph  = _four_node_graph()
        runner = self._runner(graph)
        bus    = DataBus(intent="x", run_id="r")
        assert runner._find_resume_node(bus) == "plan"

    def test_returns_next_incomplete_node(self):
        graph  = _four_node_graph()
        runner = self._runner(graph)
        bus    = DataBus(intent="x", run_id="r")
        bus.set("plan", {"plan_text": "step 1"})
        bus.record_edge("plan", "approve")
        assert runner._find_resume_node(bus) == "execute"

    def test_all_complete_returns_none(self):
        graph  = _four_node_graph()
        runner = self._runner(graph)
        bus    = DataBus(intent="x", run_id="r")
        for name in ["plan", "execute", "review", "summarize"]:
            bus.set(name, {})
        bus.record_edge("plan",    "approve")
        bus.record_edge("execute", "complete")
        bus.record_edge("review",  "approve")
        assert runner._find_resume_node(bus) is None

    def test_resume_after_rejection_loop(self):
        graph  = _four_node_graph()
        runner = self._runner(graph)
        bus    = DataBus(intent="x", run_id="r")
        # plan approved, execute completed, review rejected (→ execute again)
        bus.set("plan",    {})
        bus.set("execute", {})
        bus.set("review",  {})
        bus.record_edge("plan",    "approve")
        bus.record_edge("execute", "complete")
        bus.record_edge("review",  "reject")
        # Should resume at execute (the reject target)
        assert runner._find_resume_node(bus) == "execute"
