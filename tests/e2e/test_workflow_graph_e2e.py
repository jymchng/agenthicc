"""E2E tests for WorkflowGraph with real AgentsRegistry + MockTransport (PRD-101).

NOTE: no ``from __future__ import annotations`` — @agent() inspects annotations
at decoration time.
"""
import asyncio

import pytest

from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.agents.registry import build_agents_registry
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflow.config import WorkflowConfig
from agenthicc.workflow.plugin import (
    DataBus,
    EdgeGate,
    EdgeSpec,
    PhaseNode,
    WorkflowGraph,
)
from agenthicc.workflow.runner import WorkflowRunner

pytestmark = pytest.mark.e2e


# ── helpers ───────────────────────────────────────────────────────────────────

def _completion(content: str, n: int = 1) -> Completion:
    return Completion(
        id=f"c{n}", model="mock-model", content=content,
        tool_calls=[], stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


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


def _make_runner(graph, app_state, processor, mock_transport) -> WorkflowRunner:
    from unittest.mock import MagicMock
    agent_runner = AgentRunnerBase(transport=mock_transport, signals=SignalBus())
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
        agents_registry=build_agents_registry(),
    )
    return WorkflowRunner(graph, cfg)


# ── single-node terminal ──────────────────────────────────────────────────────

async def test_e2e_terminal_node_complete_phase(app_state, processor):
    """Single terminal node: agent calls complete_phase(output=…) → workflow ends."""
    mock = MockTransport()
    # The agent will call complete_phase() as a tool call, then respond.
    # MockTransport returns text completions; the agent's tool call is simulated
    # by having the LLM output JSON that matches the tool schema.
    # For simplicity we test via the WorkflowRunner with patched _run_node.

    graph = WorkflowGraph(
        name  = "single",
        entry = "do_it",
        nodes = {"do_it": PhaseNode(name="do_it", edges=())},
    )
    runner = _make_runner(graph, app_state, processor, mock)

    # Patch _run_node to simulate a successful terminal completion.
    from agenthicc.workflow.plugin import NodeResult

    async def _fake(node, intent, data_bus):
        data_bus.set(node.name, {"result": "completed"})
        return NodeResult(node_name=node.name, edge_label=None, output={"result": "completed"})

    runner._run_node = _fake  # type: ignore[assignment]

    await runner.run("Do the thing")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert len(wf_run.phase_history) == 1


# ── full graph path ───────────────────────────────────────────────────────────

async def test_e2e_full_graph_linear(app_state, processor):
    """Four-node graph: plan→execute→review→summarize — all approve, happy path."""
    mock  = MockTransport()
    graph = WorkflowGraph(
        name  = "code_plan_test",
        entry = "plan",
        nodes = {
            "plan":      PhaseNode(name="plan",    edges=(EdgeSpec("execute",  "approve"),)),
            "execute":   PhaseNode(name="execute", edges=(EdgeSpec("review",   "complete"),)),
            "review":    PhaseNode(name="review",  edges=(EdgeSpec("summarize","approve"), EdgeSpec("execute","reject"))),
            "summarize": PhaseNode(name="summarize"),
        },
    )
    runner = _make_runner(graph, app_state, processor, mock)

    from agenthicc.workflow.plugin import NodeResult

    edge_map = {"plan": "approve", "execute": "complete", "review": "approve", "summarize": None}

    async def _fake(node, intent, data_bus):
        edge = edge_map[node.name]
        out  = {f"{node.name}_done": True}
        data_bus.set(node.name, out)
        if edge:
            data_bus.record_edge(node.name, edge)
        return NodeResult(node.name, edge, out)

    runner._run_node = _fake  # type: ignore[assignment]

    await runner.run("Enhance the project")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    phase_names = [r.phase_name for r in wf_run.phase_history]
    assert phase_names == ["plan", "execute", "review", "summarize"]


# ── DataBus content between nodes ─────────────────────────────────────────────

async def test_e2e_data_bus_content_visible_downstream(app_state, processor):
    """Execute node can see plan's structured output via DataBus."""
    mock  = MockTransport()
    graph = WorkflowGraph(
        name  = "test",
        entry = "plan",
        nodes = {
            "plan":    PhaseNode(name="plan",    edges=(EdgeSpec("execute","complete"),)),
            "execute": PhaseNode(name="execute", edges=()),
        },
    )
    runner = _make_runner(graph, app_state, processor, mock)

    seen_in_execute: dict = {}

    from agenthicc.workflow.plugin import NodeResult

    async def _fake(node, intent, data_bus):
        if node.name == "execute":
            seen_in_execute.update(data_bus.outputs)
        edge = "complete" if node.name == "plan" else None
        out  = {"files": ["auth.py"]} if node.name == "plan" else {}
        data_bus.set(node.name, out)
        if edge:
            data_bus.record_edge(node.name, edge)
        return NodeResult(node.name, edge, out)

    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("add auth")

    assert "plan" in seen_in_execute
    assert seen_in_execute["plan"] == {"files": ["auth.py"]}


# ── phase index display ───────────────────────────────────────────────────────

async def test_e2e_phase_index_shows_definition_position(app_state, processor):
    """Phase N/M always reflects definition position, not cumulative run count."""
    mock  = MockTransport()
    graph = WorkflowGraph(
        name  = "test",
        entry = "plan",
        nodes = {
            "plan":    PhaseNode(name="plan",    edges=(EdgeSpec("plan","revise"), EdgeSpec("execute","approve"))),
            "execute": PhaseNode(name="execute", edges=()),
        },
    )
    runner = _make_runner(graph, app_state, processor, mock)

    from agenthicc.workflow.plugin import NodeResult

    call_count: dict[str, int] = {}
    seen_indices: dict[str, list[int]] = {"plan": [], "execute": []}

    async def _fake(node, intent, data_bus):
        call_count[node.name] = call_count.get(node.name, 0) + 1
        seen_indices[node.name].append(app_state.workflow_run().current_phase_index)
        # Plan revises once then approves
        if node.name == "plan":
            edge = "approve" if call_count["plan"] >= 2 else "revise"
        else:
            edge = None
        data_bus.set(node.name, {})
        if edge:
            data_bus.record_edge(node.name, edge)
        return NodeResult(node.name, edge, {})

    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("test")

    # Plan is always at index 0, execute at index 1 — regardless of plan running twice.
    assert all(i == 0 for i in seen_indices["plan"])
    assert all(i == 1 for i in seen_indices["execute"])


# ── _follow_edge semantics ────────────────────────────────────────────────────

async def test_e2e_reject_edge_returns_to_execute(app_state, processor):
    """review reject edge routes back to execute; second review approves."""
    mock  = MockTransport()
    graph = WorkflowGraph(
        name  = "test",
        entry = "execute",
        nodes = {
            "execute":   PhaseNode(name="execute", edges=(EdgeSpec("review","complete"),)),
            "review":    PhaseNode(name="review",  edges=(EdgeSpec("summarize","approve"), EdgeSpec("execute","reject"))),
            "summarize": PhaseNode(name="summarize"),
        },
    )
    runner = _make_runner(graph, app_state, processor, mock)

    from agenthicc.workflow.plugin import NodeResult

    run_count: dict[str, int] = {}

    async def _fake(node, intent, data_bus):
        run_count[node.name] = run_count.get(node.name, 0) + 1
        if node.name == "review":
            edge = "approve" if run_count["review"] >= 2 else "reject"
        elif node.name == "summarize":
            edge = None
        else:
            edge = "complete"
        data_bus.set(node.name, {})
        if edge:
            data_bus.record_edge(node.name, edge)
        return NodeResult(node.name, edge, {})

    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("test")

    assert run_count["execute"]   == 2
    assert run_count["review"]    == 2
    assert run_count["summarize"] == 1
    assert app_state.workflow_run().status == "complete"


# ── kernel events ─────────────────────────────────────────────────────────────

async def test_e2e_kernel_events_contain_edge_label(app_state, processor):
    """WorkflowPhaseCompleted kernel event carries edge_label from NodeResult."""
    mock  = MockTransport()
    graph = WorkflowGraph(
        name  = "test",
        entry = "a",
        nodes = {
            "a": PhaseNode(name="a", edges=(EdgeSpec("b","go"),)),
            "b": PhaseNode(name="b"),
        },
    )
    runner = _make_runner(graph, app_state, processor, mock)

    from agenthicc.workflow.plugin import NodeResult

    async def _fake(node, intent, data_bus):
        edge = "go" if node.name == "a" else None
        data_bus.set(node.name, {"x": 1})
        if edge:
            data_bus.record_edge(node.name, edge)
        return NodeResult(node.name, edge, {"x": 1})

    runner._run_node = _fake  # type: ignore[assignment]
    await runner.run("test")
    await processor.drain()

    completed = [
        e for e in processor.event_log
        if e.event_type == "WorkflowPhaseCompleted"
    ]
    assert len(completed) == 2
    a_event = next(e for e in completed if e.payload["phase_name"] == "a")
    assert a_event.payload["edge_label"] == "go"
    b_event = next(e for e in completed if e.payload["phase_name"] == "b")
    assert b_event.payload["edge_label"] is None


# ── is_graph detection ────────────────────────────────────────────────────────

async def test_e2e_is_graph_true_for_workflow_graph(app_state, processor):
    mock  = MockTransport()
    graph = WorkflowGraph(name="t", entry="a", nodes={"a": PhaseNode(name="a")})
    runner = _make_runner(graph, app_state, processor, mock)
    assert runner._is_graph is True


async def test_e2e_is_graph_false_for_workflow_definition(app_state, processor):
    from unittest.mock import MagicMock
    from agenthicc.workflow.plugin import PhaseSpec, WorkflowDefinition
    defn = WorkflowDefinition(name="t", phases=(PhaseSpec(name="a"),))
    mock = MockTransport()
    agent_runner = AgentRunnerBase(transport=mock, signals=SignalBus())
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
        agents_registry=build_agents_registry(),
    )
    runner = WorkflowRunner(defn, cfg)
    assert runner._is_graph is False
