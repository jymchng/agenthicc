"""Unit tests for WorkflowRunner.resume() and _find_resume_node() (PRD-101)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.workflow.config import WorkflowConfig
from agenthicc.workflow.plugin import (
    DataBus,
    EdgeSpec,
    PhaseNode,
    WorkflowGraph,
)
from agenthicc.workflow.runner import WorkflowRunner

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_graph(nodes: dict[str, PhaseNode]) -> WorkflowGraph:
    entry = next(iter(nodes))
    return WorkflowGraph(name="test_wf", entry=entry, nodes=nodes)


def _make_runner(graph: WorkflowGraph) -> WorkflowRunner:
    app_state = MagicMock()
    app_state.active_mode.return_value.blocked_capabilities = frozenset()
    cfg = WorkflowConfig(
        conv_store=MagicMock(),
        app_state=app_state,
        processor=MagicMock(),
        agent_runner=MagicMock(),
        approval_svc=None,
        cfg=MagicMock(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=MagicMock(),
    )
    runner = WorkflowRunner(graph, cfg)
    runner._cfg.processor.emit = AsyncMock()
    return runner


def _bus(outputs: dict[str, dict], edges: dict[str, str] | None = None) -> DataBus:
    bus = DataBus(intent="x", run_id="r")
    for name, data in outputs.items():
        bus.set(name, data)
    for name, label in (edges or {}).items():
        bus.record_edge(name, label)
    return bus


# ── _find_resume_node ─────────────────────────────────────────────────────────

class TestFindResumeNode:
    def test_empty_bus_returns_entry(self):
        graph  = _make_graph({"plan": PhaseNode(name="plan", edges=(EdgeSpec("execute", "complete"),)),
                               "execute": PhaseNode(name="execute")})
        runner = _make_runner(graph)
        assert runner._find_resume_node(_bus({})) == "plan"

    def test_first_done_returns_second(self):
        graph  = _make_graph({"plan": PhaseNode(name="plan", edges=(EdgeSpec("execute", "complete"),)),
                               "execute": PhaseNode(name="execute")})
        runner = _make_runner(graph)
        bus    = _bus({"plan": {"done": True}}, {"plan": "complete"})
        assert runner._find_resume_node(bus) == "execute"

    def test_all_done_returns_none(self):
        graph  = _make_graph({"plan": PhaseNode(name="plan", edges=(EdgeSpec("execute", "complete"),)),
                               "execute": PhaseNode(name="execute")})
        runner = _make_runner(graph)
        bus    = _bus({"plan": {}, "execute": {}}, {"plan": "complete"})
        assert runner._find_resume_node(bus) is None

    def test_approval_edge_followed(self):
        graph  = _make_graph({
            "plan":    PhaseNode(name="plan",    edges=(EdgeSpec("execute", "approve"),
                                                        EdgeSpec("plan",    "revise"))),
            "execute": PhaseNode(name="execute"),
        })
        runner = _make_runner(graph)
        bus    = _bus({"plan": {}}, {"plan": "approve"})
        assert runner._find_resume_node(bus) == "execute"

    def test_no_phases_returns_none(self):
        graph  = WorkflowGraph(name="t", entry="only",
                               nodes={"only": PhaseNode(name="only")})
        runner = _make_runner(graph)
        bus    = _bus({"only": {}})
        assert runner._find_resume_node(bus) is None

    def test_cycle_guard_returns_revisited_node(self):
        graph  = _make_graph({
            "execute": PhaseNode(name="execute", edges=(EdgeSpec("review",  "complete"),)),
            "review":  PhaseNode(name="review",  edges=(EdgeSpec("execute", "reject"),)),
        })
        runner = _make_runner(graph)
        # execute completed → review rejected → execute needs re-execution
        bus    = _bus({"execute": {}, "review": {}},
                      {"execute": "complete", "review": "reject"})
        assert runner._find_resume_node(bus) == "execute"


# ── resume() ─────────────────────────────────────────────────────────────────

class TestWorkflowRunnerResume:
    async def test_resume_all_done_marks_complete(self):
        graph  = _make_graph({"plan": PhaseNode(name="plan")})
        runner = _make_runner(graph)
        bus    = _bus({"plan": {"summary": "done"}})

        await runner.resume(bus)

        wf_run = runner._cfg.app_state.workflow_run.set.call_args_list[-1][0][0]
        assert wf_run.status == "complete"
        assert runner._run_id == "r"

    async def test_resume_missing_node_runs_it(self):
        graph  = _make_graph({
            "plan":    PhaseNode(name="plan",    edges=(EdgeSpec("execute", "complete"),)),
            "execute": PhaseNode(name="execute"),
        })
        runner = _make_runner(graph)
        bus    = _bus({"plan": {}}, {"plan": "complete"})

        called_start: list[str] = []

        async def _fake_loop(intent, data_bus, wf_run, run_id, start_node):
            called_start.append(start_node)

        runner._run_graph = _fake_loop  # type: ignore[method-assign]
        await runner.resume(bus)

        assert called_start == ["execute"]

    async def test_resume_sets_run_id(self):
        graph  = _make_graph({"plan": PhaseNode(name="plan")})
        runner = _make_runner(graph)
        bus    = DataBus(intent="x", run_id="my-run-id")
        bus.set("plan", {})
        await runner.resume(bus)
        assert runner._run_id == "my-run-id"

    async def test_resume_initialises_shared_memory(self):
        graph  = _make_graph({"plan": PhaseNode(name="plan")})
        runner = _make_runner(graph)
        bus    = _bus({"plan": {}})
        assert runner._shared_memory is None
        await runner.resume(bus)
        assert runner._shared_memory is not None


# ── restore_from_log integration ──────────────────────────────────────────────

class TestRestoreFromLog:
    async def test_restore_produces_workflow_entry(self, tmp_path):
        import json
        from agenthicc.kernel import AppState, Event, SecurityPolicy, SystemSettings
        from agenthicc.kernel.processor import restore_from_log
        from agenthicc.kernel.state import NodeStatus

        log_path = str(tmp_path / "events.jsonl")
        events = [
            Event.create("WorkflowRunStarted",    {"run_id": "r1", "workflow_name": "code_plan",
                                                   "intent": "add auth", "phase_names": ["plan", "execute"]}),
            Event.create("WorkflowPhaseCompleted", {"run_id": "r1", "phase_name": "plan",
                                                   "role": "planner", "full_text": "Here is the plan.",
                                                   "approved": None, "structured": {},
                                                   "edge_label": "approve"}),
            Event.create("WorkflowRunCompleted",   {"run_id": "r1", "status": "complete"}),
        ]
        with open(log_path, "w") as f:
            for e in events:
                f.write(json.dumps(e.to_dict()) + "\n")

        initial  = AppState.create(
            settings=SystemSettings(event_log_path=log_path), policy=SecurityPolicy(),
        )
        restored = await restore_from_log(log_path, initial)

        assert "r1" in restored.workflows
        wf = restored.workflows["r1"]
        assert wf.name       == "code_plan"
        assert wf.intent_text == "add auth"
        assert wf.status     == NodeStatus.complete
        assert "plan" in wf.nodes
        assert wf.nodes["plan"].result["full_text"]  == "Here is the plan."
        assert wf.nodes["plan"].result["edge_label"] == "approve"
