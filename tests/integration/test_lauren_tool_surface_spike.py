"""WFC-08 spike using the real Lauren runner with a scripted provider."""

from __future__ import annotations

import pytest
from lauren_ai._agents import agent
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.runners.tool_populator import populate_agent_tools

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("streaming", [False, True])
async def test_lauren_snapshots_tool_schemas_at_start_of_run(streaming: bool) -> None:
    @agent(model="mock-model")
    class SpikeAgent: ...

    instance = SpikeAgent()
    installed = False

    @tool()
    def newly_registered() -> str:
        """Return a known result."""
        return "available"

    @tool()
    def install_tool() -> str:
        """Replace the available tools."""
        nonlocal installed
        assert not populate_agent_tools(instance, [newly_registered])
        installed = True
        return "installed"

    assert not populate_agent_tools(instance, [install_tool])
    transport = MockTransport()
    transport.queue_tool_use("install_tool", {})
    for completion_id in ("after-install", "next-run"):
        transport.queue_response(
            Completion(
                id=completion_id,
                model="mock-model",
                content="done",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        )
    runner = AgentRunnerBase(transport=transport)
    for prompt in ("Install a tool", "New run"):
        if streaming:
            stream = await runner.run_stream(instance, prompt)
            async for _ in stream:
                pass
        else:
            await runner.run(instance, prompt)

    assert installed
    names = [
        [schema["name"] if isinstance(schema, dict) else schema.name for schema in call.tools or []]
        for call in transport.calls
    ]
    assert names == [["install_tool"], ["install_tool"], ["newly_registered"]]
