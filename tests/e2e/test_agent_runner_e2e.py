"""E2E tests: real lauren-ai AgentRunnerBase driving the agenthicc kernel.

These tests exercise the full stack the PRDs describe:

* an ``@agent()``-decorated class run by ``AgentRunnerBase`` with a
  ``MockTransport`` providing scripted completions;
* lauren-ai ``SignalBus`` lifecycle signals bridged into the agenthicc
  ``EventProcessor`` as kernel events;
* tool-only communication — the agent affects ``AppState`` exclusively by
  calling a tool that emits kernel events (``AgentSpawnRequest``), never by
  touching state directly.

NOTE: no ``from __future__ import annotations`` here — ``@tool()`` inspects
real annotations at decoration time.
"""

import asyncio

import pytest

from lauren_ai._agents import agent, use_tools
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import (
    AgentRunComplete,
    ModelCallComplete,
    ModelCallStarted,
    SignalBus,
    ToolCallComplete,
    ToolCallStarted,
)
from lauren_ai._tools import tool
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent

from agenthicc.kernel import (
    AppState,
    Event,
    EventProcessor,
    SecurityPolicy,
    SystemSettings,
)

pytestmark = pytest.mark.e2e


def _completion(content: str, n: int = 1) -> Completion:
    return Completion(
        id=f"c{n}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _bridge_signals(bus: SignalBus, processor: EventProcessor) -> None:
    """Bridge lauren-ai lifecycle signals into agenthicc kernel events."""

    def _make_handler(event_type: str, fields: list[str]):
        async def handler(signal) -> None:
            payload = {f: getattr(signal, f, None) for f in fields}
            # usage objects aren't JSON-serializable; flatten them
            usage = payload.pop("usage", None)
            if usage is not None:
                payload["input_tokens"] = usage.input_tokens
                payload["output_tokens"] = usage.output_tokens
            await processor.emit(Event.create(event_type, payload))

        return handler

    bus.on(ModelCallStarted)(_make_handler("ModelCallStarted", ["model", "agent_id", "agent_name"]))
    bus.on(ModelCallComplete)(
        _make_handler("ModelCallComplete", ["model", "agent_id", "usage", "duration_ms"])
    )
    bus.on(ToolCallStarted)(
        _make_handler("ToolCallStarted", ["tool_name", "tool_use_id", "agent_id"])
    )
    bus.on(ToolCallComplete)(
        _make_handler(
            "ToolCallComplete", ["tool_name", "tool_use_id", "agent_id", "duration_ms", "success"]
        )
    )
    bus.on(AgentRunComplete)(
        _make_handler("AgentRunComplete", ["agent_id", "agent_name", "turns", "stop_reason"])
    )


@pytest.fixture
def kernel(tmp_path):
    state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    return EventProcessor(initial_state=state, persist=False)


async def test_agent_run_signals_land_in_kernel_event_log(kernel):
    """A plain agent run produces ModelCall* and AgentRunComplete kernel events."""

    @agent(model="mock-model", system="You are a test agent.")
    class PlainAgent: ...

    proc_task = asyncio.create_task(kernel.run())
    bus = SignalBus()
    _bridge_signals(bus, kernel)

    mock = MockTransport()
    mock.queue_response(_completion("Hello from the agent."))
    runner = AgentRunnerBase(transport=mock, signals=bus)

    response = await runner.run(PlainAgent(), "Say hello")
    assert response.content == "Hello from the agent."
    assert response.stop_reason == "end_turn"

    await kernel.drain()
    event_types = [e.event_type for e in kernel.event_log]
    assert "ModelCallStarted" in event_types
    assert "ModelCallComplete" in event_types
    assert "AgentRunComplete" in event_types

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)


async def test_tool_only_communication_spawns_agent_in_appstate(kernel):
    """The agent mutates AppState exclusively through a tool call.

    The ``spawn_helper`` tool emits an ``AgentSpawnRequest`` kernel event; the
    reducer creates the ``AgentInstance``. The agent itself never touches
    state.
    """
    proc_task = asyncio.create_task(kernel.run())

    # Communication tool: closure over the kernel processor.
    processor = kernel

    @tool()
    async def spawn_helper(agent_type: str) -> dict:
        """Spawn a helper agent of the given type.

        Args:
            agent_type: The type of agent to spawn.
        """
        from uuid import uuid4

        new_id = uuid4().hex
        await processor.emit(
            Event.create(
                "AgentSpawnRequest",
                {"agent_id": new_id, "agent_type": agent_type, "config": {}},
            )
        )
        return {"agent_id": new_id}

    @agent(model="mock-model", system="You delegate work by spawning helpers.")
    @use_tools(spawn_helper)
    class OrchestratorAgent: ...

    bus = SignalBus()
    _bridge_signals(bus, kernel)

    mock = MockTransport()
    mock.queue_tool_use("spawn_helper", {"agent_type": "DebuggerAgent"})
    mock.queue_response(_completion("Spawned a debugger to handle the failure.", n=2))

    orchestrator = OrchestratorAgent()
    runner = _build_runner_for_agent(orchestrator, mock, signals=bus)
    response = await runner.run(orchestrator, "The tests are failing, get help")

    assert response.turns == 2
    assert len(response.tool_calls_made) == 1
    assert response.tool_calls_made[0].name == "spawn_helper"

    await kernel.drain()

    # The reducer created the agent from the tool-emitted event.
    state = kernel.get_state()
    spawned = [a for a in state.agents.values() if a.agent_type == "DebuggerAgent"]
    assert len(spawned) == 1
    assert spawned[0].status.value == "idle"

    # Tool lifecycle was also captured via the signal bridge.
    event_types = [e.event_type for e in kernel.event_log]
    assert "ToolCallStarted" in event_types
    assert "ToolCallComplete" in event_types
    assert "AgentSpawnRequest" in event_types

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)


async def test_queued_message_is_sent_after_tool_results_before_next_model_call(kernel):
    """A queued follow-up becomes the next user message after a tool batch."""
    proc_task = asyncio.create_task(kernel.run())
    calls: list[str] = []

    @tool()
    async def inspect_item(item: str) -> dict:
        """Inspect an item before answering the user."""
        calls.append(item)
        return {"item": item, "status": "ready"}

    mock = MockTransport()
    mock.queue_tool_use("inspect_item", {"item": "first"})
    mock.queue_response(_completion("I handled the follow-up.", n=2))
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())
    queued = iter(("follow-up",))

    from agenthicc.runners.agent_turn import _run_agent_turn

    await _run_agent_turn(
        "initial request",
        runner,
        kernel,
        project_plugin_tools=[inspect_item],
        next_queued_message=lambda: next(queued, None),
    )

    assert calls == ["first"]
    assert len(mock.calls) == 2
    second_messages = mock.calls[1].messages
    assert any(
        (
            (message.get("role") if isinstance(message, dict) else getattr(message, "role", None))
            == "user"
            and (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            == "follow-up"
        )
        for message in second_messages
    )

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)


async def test_agent_turn_repairs_non_adjacent_history_before_provider_call(kernel):
    """A live turn repairs a late result before lauren-ai serializes memory."""
    proc_task = asyncio.create_task(kernel.run())
    memory = ShortTermMemory()
    memory._messages = [
        {"role": "user", "content": "inspect the asset"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "late-1", "name": "Read", "input": {}}],
        },
        {"role": "user", "content": "continue from where you stopped"},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "late-1", "content": "done"}],
        },
    ]

    mock = MockTransport()
    mock.queue_response(_completion("Recovered safely.", n=2))
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())

    from agenthicc.runners.agent_turn import _run_agent_turn

    await _run_agent_turn(
        "resume the interrupted request",
        runner,
        kernel,
        session_memory=memory,
    )

    assert memory.validate_tool_history().ok
    assert memory._messages[2]["content"][0]["tool_use_id"] == "late-1"
    assert memory._messages[3]["content"] == "continue from where you stopped"
    assert len(mock.calls) == 1

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)


async def test_two_parallel_reads_are_serialized_as_one_valid_exchange(kernel):
    """The reported two-Read journey never sends a partial result batch."""
    proc_task = asyncio.create_task(kernel.run())
    reads: list[str] = []

    @tool()
    async def Read(path: str) -> dict:
        """Read one named asset."""
        reads.append(path)
        return {"path": path, "content": f"contents of {path}"}

    @agent(model="mock-model")
    @use_tools(Read)
    class AssetAgent: ...

    mock = MockTransport()
    mock.queue_response(
        Completion(
            id="parallel-read",
            model="mock-model",
            content="",
            tool_calls=[
                ToolCall(tool_use_id="read-cover", name="Read", input={"path": "cover.png"}),
                ToolCall(tool_use_id="read-back", name="Read", input={"path": "back.png"}),
            ],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
    )
    mock.queue_response(_completion("Both assets were read.", n=2))
    memory = ShortTermMemory()
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())

    from agenthicc.runners.agent_turn import _run_agent_turn

    await _run_agent_turn(
        "Use both image assets",
        runner,
        kernel,
        session_memory=memory,
        project_plugin_tools=[Read],
    )

    assert reads == ["cover.png", "back.png"]
    assert memory.validate_tool_history().ok
    second_request = mock.calls[1].messages

    def _role(message: object) -> str:
        return (
            str(message.get("role", ""))
            if isinstance(message, dict)
            else str(getattr(message, "role", ""))
        )

    def _tool_calls(message: object) -> list[object]:
        if isinstance(message, dict):
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                return calls
            content = message.get("content")
        else:
            calls = getattr(message, "tool_calls", None)
            if isinstance(calls, list):
                return calls
            content = getattr(message, "content", None)
        return [
            block
            for block in content or []
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

    assistant_index = next(
        index
        for index, message in enumerate(second_request)
        if _role(message) == "assistant" and _tool_calls(message)
    )
    result_ids: set[str] = set()
    for message in second_request[assistant_index + 1 :]:
        if _role(message) == "tool":
            result_ids.add(
                message.get("tool_call_id")
                if isinstance(message, dict)
                else getattr(message, "tool_call_id", "")
            )
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result_ids.add(str(block.get("tool_use_id", "")))
    assert result_ids == {"read-cover", "read-back"}

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)


async def test_multi_turn_workflow_progress_via_tools(kernel):
    """Agent advances a workflow node through tool calls across two turns."""
    proc_task = asyncio.create_task(kernel.run())
    processor = kernel

    # Seed a workflow with one node.
    await processor.emit(Event.create("WorkflowCreated", {"workflow_id": "wf1", "intent_id": "i1"}))
    await processor.emit(
        Event.create(
            "WorkflowNodeAdded",
            {
                "workflow_id": "wf1",
                "node_id": "n1",
                "task_id": "t1",
                "label": "Refactor auth",
                "dependencies": [],
            },
        )
    )
    await processor.drain()

    @tool()
    async def complete_node(workflow_id: str, node_id: str, result: str) -> dict:
        """Mark a workflow node as complete.

        Args:
            workflow_id: The workflow identifier.
            node_id: The node to complete.
            result: Result summary.
        """
        await processor.emit(
            Event.create(
                "WorkflowNodeStatusChanged",
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "status": "complete",
                    "result": result,
                },
            )
        )
        return {"ok": True}

    @agent(model="mock-model")
    @use_tools(complete_node)
    class WorkerAgent: ...

    mock = MockTransport()
    mock.queue_tool_use(
        "complete_node",
        {
            "workflow_id": "wf1",
            "node_id": "n1",
            "result": "Argon2 refactor done",
        },
    )
    mock.queue_response(_completion("Node complete.", n=2))

    worker = WorkerAgent()
    runner = _build_runner_for_agent(worker, mock, signals=SignalBus())
    await runner.run(worker, "Complete your assigned node")

    await kernel.drain()
    wf = kernel.get_state().workflows["wf1"]
    assert wf.nodes["n1"].status.value == "complete"
    assert wf.nodes["n1"].result == "Argon2 refactor done"
    assert wf.status.value == "complete"  # single node => workflow complete

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)
