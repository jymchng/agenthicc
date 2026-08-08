"""Regression tests for lauren-ai schema and transport boundaries."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lauren_ai._agents import agent, use_tools
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools._schema import generate_tool_schema
from lauren_ai._transport import Completion, Message, RequestOptions, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.testing.recording_transport import RecordingTransport
from agenthicc.runners.tool_populator import populate_agent_tools
from agenthicc.tools.fs.agent_tools import batch_copy, batch_move, batch_write, read_file
from agenthicc.plugins.registry import build_registry
from agenthicc.runners.tui_session import _make_session_tools
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.tools.mcp import AgenthiccMcpTool, McpToolSchema

pytestmark = pytest.mark.unit


def test_batch_filesystem_inputs_generate_structured_schemas(caplog):
    """Batch input objects do not fall back to ``object`` JSON schemas."""
    with caplog.at_level(logging.WARNING, logger="lauren_ai._tools._schema"):
        schemas = [generate_tool_schema(tool)[2] for tool in (batch_write, batch_move, batch_copy)]

    assert not [
        record for record in caplog.records if "unrecognised type annotation" in record.message
    ]
    for schema in schemas:
        items = schema["input_schema"]["properties"]
        item_schema = next(iter(items.values()))
        assert item_schema["type"] == "array"
        assert item_schema["items"]["type"] == "object"


def test_dynamic_session_tools_generate_warning_free_schemas(caplog):
    """The actual session registry generates no unrecognised-type warnings."""
    with caplog.at_level(logging.WARNING, logger="lauren_ai._tools._schema"):
        session_tools = _make_session_tools(None)
        session_tools.append(make_spawn_subagents_tool(None, "mock", []))
        registry = build_registry(project_plugin_tools=session_tools)
        schemas = {
            tool["name"]: tool for tool in (generate_tool_schema(t)[2] for t in registry.tools)
        }

    assert not [
        record for record in caplog.records if "unrecognised type annotation" in record.message
    ]
    assert len(schemas) == 65
    assert (
        schemas["ask_user"]["input_schema"]["properties"]["questions"]["items"]["type"] == "object"
    )
    # The agenthicc self-inspection tools produce clean schemas too.
    assert schemas["inspect_agenthicc_source"]["input_schema"]["required"] == ["target"]
    assert (
        schemas["read_agenthicc_doc"]["input_schema"]["properties"]["start_line"]["type"]
        == "integer"
    )
    assert (
        schemas["spawn_subagents"]["input_schema"]["properties"]["tasks"]["items"]["type"]
        == "object"
    )
    # The model-facing metadata is the schema actually sent by the decorated
    # tool.  Context is supported but optional; lauren-ai's standalone schema
    # helper currently regenerates TypedDict fields and does not preserve that
    # optionality, so assert the decorated contract directly.
    spawn_tool = next(tool for tool in session_tools if tool.__name__ == "spawn_subagents")
    spawn_input = spawn_tool.__lauren_ai_tool__.parameters["input_schema"]
    task_schema = spawn_input["properties"]["tasks"]["items"]
    assert "context" in task_schema["properties"]
    assert task_schema["required"] == ["type", "task"]


async def test_recording_transport_accepts_lauren_dict_tool_schemas(tmp_path: Path):
    """Recording a call works with lauren-ai's current TypedDict schema."""
    inner = MockTransport()
    inner.queue_response(
        Completion(
            id="c1",
            model="mock",
            content="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    recorder = RecordingTransport(inner, tmp_path / "cassette.jsonl")

    result = await recorder.complete(
        [Message.user("hello")],
        model="mock",
        tools=[
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {"type": "object"},
            }
        ],
    )

    assert result.content == "done"
    assert '"tool_names_available": ["read_file"]' in (tmp_path / "cassette.jsonl").read_text()


async def test_recording_transport_forwards_profile_options_without_recording_secrets(
    tmp_path: Path,
):
    inner = MockTransport()
    inner.queue_response(
        Completion(
            id="c-profile",
            model="mock",
            content="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    recorder = RecordingTransport(inner, tmp_path / "cassette.jsonl")
    await recorder.complete(
        [Message.user("hello")],
        model="mock",
        temperature=0.3,
        top_p=0.95,
        max_completion_tokens=0,
        request_options=RequestOptions(
            extra_headers={"Authorization": "secret-value"},
            extra_body={"vendor_trace": True},
        ),
    )
    call = inner.calls[0]
    assert call.temperature == 0.3
    assert call.top_p == 0.95
    assert call.max_completion_tokens == 0
    assert call.request_options is not None
    assert call.request_options.extra_body["vendor_trace"] is True
    assert "secret-value" not in (tmp_path / "cassette.jsonl").read_text()


async def test_streamed_agent_turn_with_read_file_does_not_crash(tmp_path: Path):
    """The reported prompt survives a real lauren streaming tool turn."""
    (tmp_path / "README.md").write_text("hello from README\n", encoding="utf-8")
    previous_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:

        @agent(model="deepseek-v4-flash")
        @use_tools(read_file)
        class TestAgent: ...

        agent_instance = TestAgent()
        populate_agent_tools(agent_instance, [read_file])

        inner = MockTransport()
        inner.queue_tool_use("read_file", {"path": "README.md?"})
        inner.queue_response(
            Completion(
                id="c2",
                model="deepseek-v4-flash",
                content="done",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        )
        recorder = RecordingTransport(inner, tmp_path / "cassette.jsonl")
        runner = AgentRunnerBase(transport=recorder)

        stream = await runner.run_stream(agent_instance, "what is @README.md?")
        chunks = [chunk async for chunk in stream]

        assert "".join(chunk.delta for chunk in chunks) == "done"
        assert len(inner.calls) == 2
    finally:
        os.chdir(previous_cwd)


async def test_mcp_execute_object_becomes_callable_agent_tool(tmp_path: Path):
    """A discovered MCP tool is exposed and dispatched as a Lauren callable."""
    bridge = SimpleNamespace(
        server_name="demo",
        call_tool=AsyncMock(return_value={"content": "pong"}),
    )
    mcp_tool = AgenthiccMcpTool(
        bridge,
        McpToolSchema(
            name="ping",
            description="Ping the MCP server.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
    )
    registry = build_registry(project_plugin_tools=[mcp_tool])
    exposed = next(
        tool for tool in registry.tools if getattr(tool, "__name__", "") == mcp_tool.provider_name
    )

    assert callable(exposed)
    assert getattr(exposed, "__lauren_ai_tool__").name == mcp_tool.provider_name
    assert getattr(exposed, "__lauren_ai_tool__").parameters["input_schema"] == mcp_tool.parameters

    @agent(model="deepseek-v4-flash")
    @use_tools(exposed)
    class TestMcpAgent: ...

    agent_instance = TestMcpAgent()
    populate_agent_tools(agent_instance, [exposed])

    inner = MockTransport()
    inner.queue_tool_use(mcp_tool.provider_name, {"value": "hello"})
    inner.queue_response(
        Completion(
            id="mcp-c2",
            model="deepseek-v4-flash",
            content="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    recorder = RecordingTransport(inner, tmp_path / "mcp-cassette.jsonl")
    runner = AgentRunnerBase(transport=recorder)

    stream = await runner.run_stream(agent_instance, "ping")
    chunks = [chunk async for chunk in stream]

    assert "".join(chunk.delta for chunk in chunks) == "done"
    bridge.call_tool.assert_awaited_once()
    call_args = bridge.call_tool.await_args
    assert call_args.args == ("ping", {"value": "hello"})
    assert isinstance(call_args.kwargs.get("tool_call_id"), str)
    assert call_args.kwargs["tool_call_id"]
