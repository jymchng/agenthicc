"""Integration coverage for the workflow-compatible executor subagent role."""

from __future__ import annotations

from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
import pytest

from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.tools.capabilities import tool_execute
from agenthicc.memory.journal import ConversationJournal
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.integration


@tool_execute
@tool()
async def run_bash(command: str) -> dict[str, object]:
    """Run a deterministic build command for the test."""
    return {"ok": True, "exit_code": 0, "stdout": command}


async def test_spawn_executor_runs_through_real_pool_and_agent_runner(tmp_path) -> None:
    """The workflow ``executor`` role is accepted by the real tool-to-pool path."""
    transport = MockTransport()
    transport.queue_tool_use("run_bash", {"command": "make kdp"}, tool_use_id="build-1")
    transport.queue_response(
        Completion(
            id="executor-completion",
            model="mock-model",
            content="KDP compilation completed",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=3, output_tokens=5),
        )
    )
    parent_runner = AgentRunnerBase(transport=transport)
    conversation = ConversationStore()
    conversation.begin_turn("agent", "executor-turn")
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    spawn = make_spawn_subagents_tool(
        parent_runner,
        "mock-model",
        [run_bash],
        conv_store=conversation,
        conversation_journal=journal,
    )

    result = await spawn(
        tasks=[
            {
                "type": "executor",
                "task": "Compile the novel into the requested output format.",
            }
        ]
    )

    assert result["ok"] is True
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert "KDP compilation completed" in result["results"]
    assert len(transport.calls) == 2  # the build command, then the summary
    worker_records = journal.fold_subagent_worker_results()
    pool_records = journal.fold_subagent_pool_results()
    assert worker_records[0]["text"] == "KDP compilation completed"
    assert pool_records[0]["text"] == result["results"]
    journal.close()
