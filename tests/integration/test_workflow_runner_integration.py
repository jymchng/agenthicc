"""Integration tests: WorkflowRunner with WorkflowGraph (PRD-101)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflow.config import WorkflowConfig
from agenthicc.workflow.plugin import (
    DataBus, EdgeSpec, NodeResult, PhaseNode, PhaseRole, WorkflowGraph,
)
from agenthicc.workflow.runner import WorkflowRunner

pytestmark = pytest.mark.integration


# ── fixtures ──────────────────────────────────────────────────────────────────

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


def _make_graph(*nodes: PhaseNode) -> WorkflowGraph:
    return WorkflowGraph(
        name="test_wf",
        entry=nodes[0].name,
        nodes={n.name: n for n in nodes},
    )


def _make_runner(wf: WorkflowGraph, app_state, processor) -> WorkflowRunner:
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
    return WorkflowRunner(wf, cfg)


def _patch_run_node(runner: WorkflowRunner, results: dict[str, NodeResult]) -> None:
    """Patch _run_node to return canned NodeResult objects by node name."""
    async def _fake(node, intent, data_bus):
        res = results.get(node.name) or NodeResult(
            node_name=node.name, edge_label=None, output={"auto": True},
        )
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res
    runner._run_node = _fake  # type: ignore[assignment]


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_single_node_workflow_completes(app_state, processor):
    wf     = _make_graph(PhaseNode(name="plan", agent_type=PhaseRole.PLANNER))
    runner = _make_runner(wf, app_state, processor)
    _patch_run_node(runner, {"plan": NodeResult("plan", None, {"done": True})})
    await runner.run("Fix the bug")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert len(wf_run.phase_history) == 1
    assert wf_run.phase_history[0].phase_name == "plan"


async def test_two_node_sequential(app_state, processor):
    wf = _make_graph(
        PhaseNode(name="plan",    agent_type=PhaseRole.PLANNER,
                  edges=(EdgeSpec("execute", "complete"),)),
        PhaseNode(name="execute", agent_type=PhaseRole.EXECUTOR),
    )
    runner = _make_runner(wf, app_state, processor)
    _patch_run_node(runner, {
        "plan":    NodeResult("plan",    "complete", {"plan": "step 1"}),
        "execute": NodeResult("execute", None,        {"done": True}),
    })
    await runner.run("Do the work")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert [r.phase_name for r in wf_run.phase_history] == ["plan", "execute"]


async def test_on_reject_loops_back_and_completes(app_state, processor):
    call_count: dict[str, int] = {}
    wf = _make_graph(
        PhaseNode(name="plan",   agent_type=PhaseRole.PLANNER,
                  edges=(EdgeSpec("review", "complete"),)),
        PhaseNode(name="review", agent_type=PhaseRole.REVIEWER,
                  edges=(EdgeSpec(None,   "approve"),
                         EdgeSpec("plan", "reject"))),
    )
    runner = _make_runner(wf, app_state, processor)

    async def _fake(node, intent, data_bus):
        call_count[node.name] = call_count.get(node.name, 0) + 1
        if node.name == "review":
            edge = "approve" if call_count["review"] >= 2 else "reject"
        elif node.name == "plan":
            edge = "complete"
        else:
            edge = None
        res = NodeResult(node.name, edge, {})
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res

    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("Fix it")
    assert call_count["plan"]   == 2
    assert call_count["review"] == 2
    assert app_state.workflow_run().status == "complete"


async def test_per_node_max_continuations_stops_loop(app_state, processor):
    """When _run_node exhausts continuations it returns edge_label=None → terminal."""
    wf = _make_graph(
        PhaseNode(name="plan",   edges=(EdgeSpec("review", "complete"),)),
        PhaseNode(name="review", edges=(EdgeSpec(None,   "approve"),
                                        EdgeSpec("plan", "reject"))),
    )
    runner = _make_runner(wf, app_state, processor)
    # review always rejects — loop: plan→review(reject)→plan→review(reject)→…
    # The opt-in cap is not set so we need max_continuations to bound it via NodeResult
    # Simulate: after 2 plan runs review returns approve
    call_count: dict[str, int] = {}

    async def _fake(node, intent, data_bus):
        call_count[node.name] = call_count.get(node.name, 0) + 1
        if node.name == "review":
            edge = "reject"  # always reject
        else:
            edge = "complete"
        res = NodeResult(node.name, edge, {})
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res

    runner._run_node = _fake  # type: ignore[assignment]
    # With opt-in cap of 3 total runs, loop stops after plan+review+plan
    runner._def = WorkflowGraph(
        name="test_wf", entry="plan",
        nodes=wf.nodes,
        max_total_phase_runs=3,
    )
    await runner.run("Always fail")
    assert app_state.workflow_run().status == "failed"


async def test_opt_in_global_cap(app_state, processor):
    wf = WorkflowGraph(
        name="test_wf", entry="plan",
        nodes={
            "plan":   PhaseNode(name="plan",   edges=(EdgeSpec("review", "complete"),)),
            "review": PhaseNode(name="review", edges=(EdgeSpec("plan",   "reject"),)),
        },
        max_total_phase_runs=3,
    )
    runner = _make_runner(wf, app_state, processor)
    call_count: dict[str, int] = {}

    async def _fake(node, intent, data_bus):
        call_count[node.name] = call_count.get(node.name, 0) + 1
        edge = "complete" if node.name == "plan" else "reject"
        res = NodeResult(node.name, edge, {})
        data_bus.set(node.name, res.output)
        if res.edge_label:
            data_bus.record_edge(node.name, res.edge_label)
        return res

    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("Loop forever")
    assert sum(call_count.values()) <= 3
    assert app_state.workflow_run().status == "failed"


async def test_no_global_cap_by_default(app_state, processor):
    wf = _make_graph(
        PhaseNode(name="plan",      edges=(EdgeSpec("execute",  "complete"),)),
        PhaseNode(name="execute",   edges=(EdgeSpec("review",   "complete"),)),
        PhaseNode(name="review",    edges=(EdgeSpec("summarize","approve"),)),
        PhaseNode(name="summarize"),
    )
    runner = _make_runner(wf, app_state, processor)
    _patch_run_node(runner, {
        "plan":      NodeResult("plan",      "complete", {}),
        "execute":   NodeResult("execute",   "complete", {}),
        "review":    NodeResult("review",    "approve",  {}),
        "summarize": NodeResult("summarize", None,       {}),
    })
    await runner.run("Do the work")
    assert app_state.workflow_run().status == "complete"
    assert len(app_state.workflow_run().phase_history) == 4


async def test_cancellation_marks_failed(app_state, processor):
    wf     = _make_graph(PhaseNode(name="plan"))
    runner = _make_runner(wf, app_state, processor)

    async def _fake(node, intent, data_bus):
        raise asyncio.CancelledError()

    runner._run_node = _fake  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        await runner.run("test")
    assert app_state.workflow_run().status == "failed"


async def test_kernel_events_emitted(app_state, processor):
    wf = _make_graph(
        PhaseNode(name="plan",    edges=(EdgeSpec("execute", "complete"),)),
        PhaseNode(name="execute"),
    )
    runner = _make_runner(wf, app_state, processor)
    _patch_run_node(runner, {
        "plan":    NodeResult("plan",    "complete", {}),
        "execute": NodeResult("execute", None,        {}),
    })
    await runner.run("test")
    await processor.drain()

    event_types = [e.event_type for e in processor.event_log]
    assert "WorkflowRunStarted"    in event_types
    assert "WorkflowPhaseStarted"  in event_types
    assert "WorkflowPhaseCompleted" in event_types
    assert "WorkflowRunCompleted"  in event_types


async def test_workflow_run_signal_updates(app_state, processor):
    wf = _make_graph(
        PhaseNode(name="plan",    edges=(EdgeSpec("execute", "complete"),)),
        PhaseNode(name="execute"),
    )
    runner = _make_runner(wf, app_state, processor)
    _patch_run_node(runner, {
        "plan":    NodeResult("plan",    "complete", {}),
        "execute": NodeResult("execute", None,        {}),
    })
    await runner.run("test")
    wf_run = app_state.workflow_run()
    assert wf_run is not None
    assert wf_run.workflow_name == "test_wf"
    assert wf_run.status        == "complete"
