"""Integration coverage for the workflow-compatible executor subagent role."""

from __future__ import annotations

from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
import pytest

from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.integration


async def test_spawn_executor_runs_through_real_pool_and_agent_runner() -> None:
    """The workflow ``executor`` role is accepted by the real tool-to-pool path."""
    transport = MockTransport()
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
    spawn = make_spawn_subagents_tool(
        parent_runner,
        "mock-model",
        [],
        conv_store=conversation,
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
    assert len(transport.calls) == 1
