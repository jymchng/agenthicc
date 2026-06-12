"""Unit tests for the CommunicationTools catalog (PRD-03)."""

from __future__ import annotations

import pytest

from agenthicc.kernel import AgentStatus, Event, NodeStatus
from agenthicc.runtime import AgentPool, CommunicationTools

pytestmark = pytest.mark.unit


@pytest.fixture
def pool() -> AgentPool:
    return AgentPool()


@pytest.fixture
def comm(harness, pool) -> CommunicationTools:
    return CommunicationTools(processor=harness.processor, pool=pool)


async def make_workflow(harness, workflow_id: str = "wf-1") -> str:
    await harness.processor.emit(
        Event.create("WorkflowCreated", {"workflow_id": workflow_id, "intent_id": "i-1"})
    )
    await harness.processor.drain()
    return workflow_id


# ── agent_spawn ──────────────────────────────────────────────────────────


async def test_agent_spawn_emits_event_and_creates_agent(harness, comm, pool):
    result = await comm.agent_spawn("researcher", config={"model": "mock"})
    await harness.processor.drain()

    assert result["agent_id"]
    events = harness.events_of_type("AgentSpawnRequest")
    assert len(events) == 1
    assert events[0].payload["agent_type"] == "researcher"
    assert events[0].payload["config"] == {"model": "mock"}

    state = harness.processor.get_state()
    agent = state.agents[result["agent_id"]]
    assert agent.agent_type == "researcher"
    assert agent.status == AgentStatus.idle

    # the runtime pool tracked the new agent as idle
    assert pool.idle_count == 1
    assert pool.get(result["agent_id"]) is not None


async def test_agent_spawn_records_parent(harness, comm):
    parent = (await comm.agent_spawn("lead"))["agent_id"]
    child = (await comm.agent_spawn("worker", parent_agent_id=parent))["agent_id"]
    await harness.processor.drain()

    state = harness.processor.get_state()
    assert state.agents[child].parent_agent_id == parent


# ── agent_send_message ───────────────────────────────────────────────────


async def test_agent_send_message_emits_event_without_bus(harness, comm):
    result = await comm.agent_send_message("a-2", {"hello": "world"}, from_agent_id="a-1")
    await harness.processor.drain()

    events = harness.events_of_type("AgentMessageSent")
    assert len(events) == 1
    assert events[0].payload["from_agent_id"] == "a-1"
    assert events[0].payload["to_agent_id"] == "a-2"
    assert events[0].payload["message"] == {"hello": "world"}
    assert result["delivered"] is False  # no bus configured


async def test_agent_send_message_wraps_plain_string(harness, comm):
    await comm.agent_send_message("a-2", "ping")
    await harness.processor.drain()
    event = harness.events_of_type("AgentMessageSent")[0]
    assert event.payload["message"] == {"text": "ping"}


# ── task_create / task_assign ────────────────────────────────────────────


async def test_task_create_emits_node_added_and_task_created(harness, comm):
    wf = await make_workflow(harness)
    result = await comm.task_create("write tests", workflow_id=wf)
    await harness.processor.drain()

    assert len(harness.events_of_type("WorkflowNodeAdded")) == 1
    created = harness.events_of_type("TaskCreated")
    assert len(created) == 1
    assert created[0].payload["task_id"] == result["task_id"]

    state = harness.processor.get_state()
    task = state.tasks[result["task_id"]]
    assert task.description == "write tests"
    assert task.status == NodeStatus.pending
    assert result["node_id"] in state.workflows[wf].nodes


async def test_task_assign_emits_event_and_marks_running(harness, comm):
    wf = await make_workflow(harness)
    task_id = (await comm.task_create("do work", workflow_id=wf))["task_id"]
    await comm.task_assign(task_id, "agent-9")
    await harness.processor.drain()

    events = harness.events_of_type("TaskAssigned")
    assert len(events) == 1
    assert events[0].payload == {"task_id": task_id, "agent_id": "agent-9"}

    task = harness.processor.get_state().tasks[task_id]
    assert task.status == NodeStatus.running
    assert task.assigned_agent_id == "agent-9"


# ── workflow_modify ──────────────────────────────────────────────────────


async def test_workflow_modify_add_node(harness, comm):
    wf = await make_workflow(harness)
    result = await comm.workflow_modify(wf, "add_node", "n1", label="step one")
    await harness.processor.drain()

    assert result["applied"] is True
    assert len(harness.events_of_type("WorkflowNodeAdded")) == 1
    node = harness.processor.get_state().workflows[wf].nodes["n1"]
    assert node.label == "step one"


async def test_workflow_modify_rejects_self_cycle(harness, comm):
    wf = await make_workflow(harness)
    with pytest.raises(ValueError, match="cycle"):
        await comm.workflow_modify(wf, "add_node", "n1", dependencies=["n1"])
    assert harness.events_of_type("WorkflowNodeAdded") == []


async def test_workflow_modify_rejects_two_node_cycle(harness, comm):
    wf = await make_workflow(harness)
    # n1 depends on (not yet existing) n2; adding n2 -> n1 closes the loop
    await comm.workflow_modify(wf, "add_node", "n1", dependencies=["n2"])
    with pytest.raises(ValueError, match="cycle"):
        await comm.workflow_modify(wf, "add_node", "n2", dependencies=["n1"])


async def test_workflow_modify_remove_node(harness, comm):
    wf = await make_workflow(harness)
    await comm.workflow_modify(wf, "add_node", "n1")
    result = await comm.workflow_modify(wf, "remove_node", "n1")
    await harness.processor.drain()

    assert result["applied"] is True
    assert "n1" not in harness.processor.get_state().workflows[wf].nodes


async def test_workflow_modify_refuses_removing_running_node(harness, comm):
    wf = await make_workflow(harness)
    await comm.workflow_modify(wf, "add_node", "n1")
    await harness.processor.emit(
        Event.create(
            "WorkflowNodeStatusChanged",
            {"workflow_id": wf, "node_id": "n1", "status": "running"},
        )
    )
    await harness.processor.drain()

    with pytest.raises(ValueError, match="running"):
        await comm.workflow_modify(wf, "remove_node", "n1")
    assert harness.events_of_type("WorkflowNodeRemoved") == []


async def test_workflow_modify_unknown_workflow_or_action(harness, comm):
    with pytest.raises(ValueError, match="unknown workflow"):
        await comm.workflow_modify("nope", "add_node", "n1")
    wf = await make_workflow(harness)
    with pytest.raises(ValueError, match="unsupported"):
        await comm.workflow_modify(wf, "explode", "n1")


# ── application_log / application_ui_update ──────────────────────────────


async def test_application_log_emits_event(harness, comm):
    result = await comm.application_log("info", "hello", data={"k": 1})
    await harness.processor.drain()

    events = harness.events_of_type("ApplicationLog")
    assert len(events) == 1
    assert events[0].payload == {"level": "INFO", "message": "hello", "data": {"k": 1}}
    assert result["accepted"] is True


async def test_application_log_rejects_bad_level(harness, comm):
    with pytest.raises(ValueError, match="level"):
        await comm.application_log("LOUD", "boom")


async def test_application_ui_update_emits_event(harness, comm):
    result = await comm.application_ui_update("progress: 50%", ui_type="progress")
    await harness.processor.drain()

    events = harness.events_of_type("UIUpdate")
    assert len(events) == 1
    assert events[0].payload == {"ui_type": "progress", "content": "progress: 50%"}
    assert result["queued"] is True


# ── tool_define / hook_register ──────────────────────────────────────────


async def test_tool_define_registers_tool(harness, comm):
    source = "async def execute(params, context):\n    return {'ok': True}\n"
    result = await comm.tool_define(
        "my_tool", "does things", source, {"type": "object", "properties": {}}
    )
    await harness.processor.drain()

    events = harness.events_of_type("ToolRegistered")
    assert len(events) == 1
    assert events[0].payload["name"] == "my_tool"
    assert result["registered"] is True

    state = harness.processor.get_state()
    assert state.tools["my_tool"].description == "does things"


async def test_tool_define_rejects_invalid_source(harness, comm):
    with pytest.raises(ValueError, match="compile"):
        await comm.tool_define("bad_tool", "broken", "def f(:\n    pass", {})
    assert harness.events_of_type("ToolRegistered") == []


async def test_tool_define_rejects_invalid_name(harness, comm):
    with pytest.raises(ValueError, match="identifier"):
        await comm.tool_define("not a name", "x", "x = 1", {})


async def test_hook_register_emits_event_and_updates_state(harness, comm):
    result = await comm.hook_register("task", "on_complete", "myapp.hooks.notify")
    await harness.processor.drain()

    events = harness.events_of_type("HookRegistered")
    assert len(events) == 1

    state = harness.processor.get_state()
    assert state.hooks[result["hook_id"]] == {
        "entity_type": "task",
        "stage": "on_complete",
        "handler_dotpath": "myapp.hooks.notify",
    }
