"""E2E tests: real lauren-ai AgentRunnerBase driving the agenthicc kernel.

These tests exercise the full stack the PRDs describe:

* an ``@agent()``-decorated class run by ``AgentRunnerBase`` with a
  ``MockTransport`` providing scripted completions;
* tool-only communication — the agent affects ``AppState`` exclusively by
  calling a tool that emits declared kernel events (``IntentCreated``,
  ``WorkflowPhaseCompleted``), never by touching state directly.

NOTE: no ``from __future__ import annotations`` here — ``@tool()`` inspects
real annotations at decoration time.
"""

import asyncio
import json

import pytest

from lauren_ai._agents import agent, use_tools
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
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
from agenthicc.memory.journal import ConversationJournal, fold_path
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.subagents.tool import make_spawn_subagents_tool

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


async def test_parent_provider_history_contains_complete_subagent_aggregate(tmp_path):
    """The full worker artefact survives the parent tool-exchange commit."""
    long_output = "CHAPTER-ARTEFACT\n" + ("rewritten prose\n" * 400)
    journal_path = tmp_path / "conversation-journal.jsonl"
    journal = ConversationJournal(journal_path)
    memory = JournaledShortTermMemory(journal, max_tokens=40_000)

    mock = MockTransport()
    mock.queue_tool_use(
        "spawn_subagents",
        {
            "tasks": [
                {
                    "type": "executor",
                    "task": "Return the complete rewritten chapter content.",
                }
            ]
        },
        tool_use_id="spawn-1",
    )
    # The worker consumes this response; the parent consumes the following one.
    mock.queue_response(_completion(long_output, n=2))
    mock.queue_response(_completion("The chapter was returned and persisted.", n=3))
    parent_runner = AgentRunnerBase(transport=mock)
    spawn = make_spawn_subagents_tool(
        parent_runner,
        "mock-model",
        [],
        conversation_journal=journal,
    )

    @agent(model="mock-model", system="Delegate the chapter task.")
    @use_tools(spawn)
    class ParentAgent: ...

    from agenthicc.runners.tool_populator import populate_agent_tools

    parent = ParentAgent()
    populate_agent_tools(parent, [spawn])
    response = await parent_runner.run(
        parent,
        "Delegate this chapter rewrite.",
        memory=memory,
        run_id="parent-1",
    )

    assert response.content == "The chapter was returned and persisted."
    messages, _ = fold_path(journal_path)
    serialized = str(messages)
    assert "CHAPTER-ARTEFACT" in serialized
    aggregate = str(journal.fold_subagent_pool_results()[0]["text"])
    assert long_output in aggregate
    tool_message = messages[2]
    tool_content = tool_message["content"][0]["content"]
    assert isinstance(tool_content, str)
    assert json.loads(tool_content)["results"] == aggregate
    journal.close()


async def test_tool_only_communication_changes_appstate_through_kernel_events(kernel):
    """The agent mutates AppState exclusively through a tool call.

    The ``record_intent`` tool emits an ``IntentCreated`` kernel event; the
    reducer creates the intent. The agent itself never touches state.
    """
    proc_task = asyncio.create_task(kernel.run())
    processor = kernel
    seen: list[str] = []
    processor.add_sink(lambda event: seen.append(event.event_type))

    @tool()
    async def record_intent(text: str) -> dict:
        """Record a follow-up intent for the orchestrator.

        Args:
            text: What needs to be done.
        """
        from uuid import uuid4

        intent_id = uuid4().hex
        await processor.emit(
            Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text})
        )
        return {"intent_id": intent_id}

    @agent(model="mock-model", system="You delegate work by recording intents.")
    @use_tools(record_intent)
    class OrchestratorAgent: ...

    mock = MockTransport()
    mock.queue_tool_use("record_intent", {"text": "Debug the failing tests"})
    mock.queue_response(_completion("Recorded a debugging intent.", n=2))

    orchestrator = OrchestratorAgent()
    runner = _build_runner_for_agent(orchestrator, mock, signals=SignalBus())
    response = await runner.run(orchestrator, "The tests are failing, get help")

    assert response.turns == 2
    assert len(response.tool_calls_made) == 1
    assert response.tool_calls_made[0].name == "record_intent"

    await kernel.drain()

    # The reducer created the intent from the tool-emitted event.
    intents = list(kernel.get_state().intents.values())
    assert [intent.raw_text for intent in intents] == ["Debug the failing tests"]
    assert intents[0].status.value == "pending"
    assert seen == ["IntentCreated"]
    assert kernel.rejected_count == 0

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
    """Agent advances a workflow run through tool calls across two turns."""
    proc_task = asyncio.create_task(kernel.run())
    processor = kernel

    await processor.emit(
        Event.create(
            "WorkflowRunStarted",
            {"run_id": "wf1", "workflow_name": "refactor", "intent": "Refactor auth"},
        )
    )
    await processor.drain()

    @tool()
    async def complete_phase(run_id: str, phase_name: str, summary: str) -> dict:
        """Record a completed workflow phase and finish the run.

        Args:
            run_id: The workflow run identifier.
            phase_name: The phase that finished.
            summary: Result summary.
        """
        await processor.emit(
            Event.create(
                "WorkflowPhaseCompleted",
                {"run_id": run_id, "phase_name": phase_name, "full_text": summary},
            )
        )
        await processor.emit(
            Event.create("WorkflowRunCompleted", {"run_id": run_id, "status": "complete"})
        )
        return {"ok": True}

    @agent(model="mock-model")
    @use_tools(complete_phase)
    class WorkerAgent: ...

    mock = MockTransport()
    mock.queue_tool_use(
        "complete_phase",
        {"run_id": "wf1", "phase_name": "execute", "summary": "Argon2 refactor done"},
    )
    mock.queue_response(_completion("Phase complete.", n=2))

    worker = WorkerAgent()
    runner = _build_runner_for_agent(worker, mock, signals=SignalBus())
    await runner.run(worker, "Complete your assigned phase")

    await kernel.drain()
    wf = kernel.get_state().workflows["wf1"]
    assert wf.nodes["execute"].status.value == "complete"
    assert isinstance(wf.nodes["execute"].result, dict)
    assert wf.nodes["execute"].result["full_text"] == "Argon2 refactor done"
    assert wf.status.value == "complete"

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)
