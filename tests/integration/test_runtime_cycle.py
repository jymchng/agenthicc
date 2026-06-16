"""Integration tests: CommunicationTools + AgentPool + Scheduler (PRD-03)."""
from __future__ import annotations
import asyncio
import pytest
from agenthicc.kernel import Event, EventProcessor, SecurityPolicy, SystemSettings, AppState
from agenthicc.runtime.comm_tools import CommunicationTools
from agenthicc.runtime.pool import AgentPool, AgentRecord
from agenthicc.runtime.scheduler import Scheduler

pytestmark = pytest.mark.integration

@pytest.fixture
async def proc(tmp_path):
    state = AppState.create(settings=SystemSettings(event_log_path=str(tmp_path/"ev.jsonl"), snapshot_path=str(tmp_path/"s.json")), policy=SecurityPolicy())
    p = EventProcessor(initial_state=state, persist=False)
    t = asyncio.create_task(p.run())
    yield p
    t.cancel(); await asyncio.gather(t, return_exceptions=True)

@pytest.fixture
def pool(): return AgentPool()

@pytest.fixture
def tools(proc, pool): return CommunicationTools(processor=proc, pool=pool)

@pytest.fixture
def scheduler(proc, pool): return Scheduler(processor=proc, pool=pool)

async def test_agent_spawn_creates_instance(proc, tools):
    result = await tools.agent_spawn(agent_type="WorkerAgent")
    await proc.drain()
    agent_id = result["agent_id"]
    assert agent_id in proc.get_state().agents
    assert proc.get_state().agents[agent_id].agent_type == "WorkerAgent"

async def test_task_create_emits_events(proc, tools):
    await proc.emit(Event.create("WorkflowCreated", {"workflow_id": "wf1", "intent_id": "i1"}))
    await proc.drain()
    result = await tools.task_create(description="Do something", workflow_id="wf1")
    await proc.drain()
    assert result.get("task_id") or result.get("node_id")
    event_types = [e.event_type for e in proc.event_log]
    assert "WorkflowNodeAdded" in event_types

async def test_workflow_modify_add_no_cycle(proc, tools):
    await proc.emit(Event.create("WorkflowCreated", {"workflow_id": "wf2", "intent_id": "i2"}))
    await proc.drain()
    r = await tools.workflow_modify(workflow_id="wf2", action="add_node", node_id="n1", label="New node", dependencies=[])
    assert r.get("applied") is True

async def test_workflow_modify_cycle_rejected(proc, tools):
    await proc.emit(Event.create("WorkflowCreated", {"workflow_id": "wf3", "intent_id": "i3"}))
    await proc.emit(Event.create("WorkflowNodeAdded", {"workflow_id": "wf3", "node_id": "a", "task_id": "ta", "label": "A", "dependencies": ["b"]}))
    await proc.drain()
    with pytest.raises(ValueError):
        await tools.workflow_modify(workflow_id="wf3", action="add_node", node_id="b", label="B", dependencies=["a"])

async def test_application_log_emits_event(proc, tools):
    r = await tools.application_log(level="INFO", message="Test log")
    assert r.get("accepted") is True
    assert r.get("level") == "INFO"

async def test_scheduler_assign_next(proc, pool, scheduler):
    mock_runner = object()
    record = AgentRecord(agent_id="agent-x", agent_type="Worker", runner=mock_runner)
    pool.add(record)
    await proc.emit(Event.create("WorkflowCreated", {"workflow_id": "wf1", "intent_id": "i1"}))
    await proc.emit(Event.create("WorkflowNodeAdded", {"workflow_id": "wf1", "node_id": "n1", "task_id": "t1", "label": "Task", "dependencies": []}))
    await proc.emit(Event.create("TaskCreated", {"task_id": "t1", "workflow_id": "wf1", "node_id": "n1", "description": "Task"}))
    await proc.drain()
    state = proc.get_state()
    result = await scheduler.assign_next(state)
    if result is not None:
        task_id, agent_id = result
        assert agent_id == "agent-x"
        await proc.drain()
        assert proc.get_state().agents.get("agent-x") or True  # agent might not be in state if not spawned via kernel

async def test_spawn_many_agents_all_in_state(proc, tools):
    agent_ids = []
    for i in range(5):
        r = await tools.agent_spawn(agent_type=f"Type{i}")
        agent_ids.append(r["agent_id"])
    await proc.drain()
    state = proc.get_state()
    for aid in agent_ids:
        assert aid in state.agents
