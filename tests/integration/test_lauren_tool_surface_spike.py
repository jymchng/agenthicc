"""WFC-08 spike using the real Lauren runner with a scripted provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from lauren_ai._agents import agent
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.runners.tool_populator import populate_agent_tools
from agenthicc.testing.recording_transport import RecordingTransport

if TYPE_CHECKING:
    from lauren_ai._agents import AgentMeta

pytestmark = pytest.mark.integration


class _RefreshableRunner(AgentRunnerBase):
    def _get_tool_schemas(self, meta: AgentMeta) -> list[dict[str, object]]:
        schemas: list[dict[str, object]] = super()._get_tool_schemas(meta)
        self.schemas = schemas
        self.meta = meta
        return schemas

    def refresh_schemas(self) -> None:
        self.schemas[:] = super()._get_tool_schemas(self.meta)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("refresh", [False, True], ids=["stock", "refresh"])
async def test_lauren_tool_schema_refresh_between_requests(
    tmp_path: Path, streaming: bool, refresh: bool
) -> None:
    @agent(model="mock-model")
    class SpikeAgent: ...

    instance = SpikeAgent()
    installed = False
    called = False

    @tool()
    def newly_registered() -> str:
        """Return a known result."""
        nonlocal called
        called = True
        return "available"

    @tool()
    def install_tool() -> str:
        """Replace the available tools."""
        nonlocal installed
        assert not populate_agent_tools(instance, [newly_registered])
        if refresh:
            runner.refresh_schemas()
        installed = True
        return "installed"

    assert not populate_agent_tools(instance, [install_tool])
    transport = MockTransport()
    transport.queue_tool_use("install_tool", {})
    if refresh:
        transport.queue_tool_use("newly_registered", {})
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
    cassette = tmp_path / "requests.jsonl"
    runner = _RefreshableRunner(transport=RecordingTransport(transport, cassette))
    for prompt in ("Install a tool", "New run"):
        if streaming:
            stream = await runner.run_stream(instance, prompt)
            async for _ in stream:
                pass
        else:
            await runner.run(instance, prompt)

    assert installed
    assert called == refresh
    names = [
        json.loads(line)["tool_names_available"]
        for line in cassette.read_text(encoding="utf-8").splitlines()
    ]
    if refresh:
        assert names == [
            ["install_tool"],
            ["newly_registered"],
            ["newly_registered"],
            ["newly_registered"],
        ]
    else:
        assert names == [["install_tool"], ["install_tool"], ["newly_registered"]]
