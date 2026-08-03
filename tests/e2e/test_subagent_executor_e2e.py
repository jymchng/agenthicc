"""End-to-end delegation coverage for workflow-compatible subagent roles."""

from __future__ import annotations

from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.subagents.tool import make_spawn_subagents_tool

import pytest

pytestmark = pytest.mark.e2e


async def test_executor_delegation_returns_a_successful_aggregate() -> None:
    """A user-facing spawn request can use the same role as workflow phases."""
    transport = MockTransport()
    transport.queue_response(
        Completion(
            id="e2e-executor",
            model="mock-model",
            content="artifact ready",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=2),
        )
    )
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(runner, "mock-model", [])

    result = await spawn(tasks=[{"type": "executor", "task": "Build the artifact."}])

    assert set(result) == {"ok", "pool_id", "total", "succeeded", "failed", "error", "results"}
    assert result["ok"] is True
    assert isinstance(result["pool_id"], str)
    assert result["total"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert result["error"] == ""
    assert "artifact ready" in result["results"]
