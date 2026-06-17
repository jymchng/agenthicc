"""Integration tests: DAGExecutor drives workflows via kernel events (PRD-02)."""
from __future__ import annotations
import asyncio
import time
import pytest
from agenthicc.kernel import Event, EventProcessor, NodeStatus, SecurityPolicy, SystemSettings, AppState
from agenthicc.workflows.executor import DAGExecutor

pytestmark = pytest.mark.integration

@pytest.fixture
async def proc(tmp_path):
    state = AppState.create(settings=SystemSettings(event_log_path=str(tmp_path/"ev.jsonl"), snapshot_path=str(tmp_path/"s.json")), policy=SecurityPolicy())
    p = EventProcessor(initial_state=state, persist=False)
    t = asyncio.create_task(p.run())
    yield p
    t.cancel(); await asyncio.gather(t, return_exceptions=True)

async def _seed_workflow(proc, wf_id, nodes):
    await proc.emit(Event.create("WorkflowCreated", {"workflow_id": wf_id, "intent_id": "i1"}))
    for nid, label, deps in nodes:
        await proc.emit(Event.create("WorkflowNodeAdded", {"workflow_id": wf_id, "node_id": nid, "task_id": f"t-{nid}", "label": label, "dependencies": deps}))
    await proc.drain()

async def test_linear_workflow_completes(proc):
    await _seed_workflow(proc, "wf1", [("a", "Step A", []), ("b", "Step B", ["a"])])
    exe = DAGExecutor(proc, lambda node, wf_id: asyncio.sleep(0, result=f"done {node.node_id}"))
    wf = await exe.run_workflow("wf1", timeout=5.0)
    assert wf.status == NodeStatus.complete
    assert wf.nodes["a"].status == NodeStatus.complete
    assert wf.nodes["b"].status == NodeStatus.complete

async def test_diamond_branches_run_concurrently(proc):
    times: dict[str, float] = {}
    async def runner(node, wf_id):
        times[node.node_id] = time.monotonic()
        await asyncio.sleep(0.02)
        return "ok"
    await _seed_workflow(proc, "wf2", [("root", "Root", []), ("left", "Left", ["root"]), ("right", "Right", ["root"]), ("merge", "Merge", ["left", "right"])])
    exe = DAGExecutor(proc, runner, max_parallel_tasks=4)
    wf = await exe.run_workflow("wf2", timeout=5.0)
    assert wf.status == NodeStatus.complete
    # Both branches should start within 50ms of each other (concurrent)
    if "left" in times and "right" in times:
        assert abs(times["left"] - times["right"]) < 0.05

async def test_failed_node_marks_workflow_failed(proc):
    async def failing(node, wf_id):
        raise RuntimeError("intentional failure")
    await _seed_workflow(proc, "wf3", [("a", "Will fail", [])])
    exe = DAGExecutor(proc, failing)
    wf = await exe.run_workflow("wf3", timeout=5.0)
    assert wf.status == NodeStatus.failed
    assert wf.nodes["a"].status == NodeStatus.failed

async def test_downstream_skipped_after_failure(proc):
    async def runner(node, wf_id):
        if node.node_id == "a": raise RuntimeError("fail")
        return "ok"
    await _seed_workflow(proc, "wf4", [("a", "Fail", []), ("b", "Downstream", ["a"])])
    exe = DAGExecutor(proc, runner)
    wf = await exe.run_workflow("wf4", timeout=5.0)
    assert wf.nodes["b"].status == NodeStatus.skipped

async def test_max_parallel_tasks_throttle(proc):
    running: list[int] = [0]; peak: list[int] = [0]
    async def runner(node, wf_id):
        running[0] += 1; peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.02); running[0] -= 1; return "ok"
    await _seed_workflow(proc, "wf5", [(str(i), f"Node {i}", []) for i in range(5)])
    exe = DAGExecutor(proc, runner, max_parallel_tasks=2)
    wf = await exe.run_workflow("wf5", timeout=10.0)
    assert wf.status == NodeStatus.complete
    assert peak[0] <= 2
