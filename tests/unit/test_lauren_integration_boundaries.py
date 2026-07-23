"""Regression tests for lauren-ai schema and transport boundaries."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from lauren_ai._agents import agent, use_tools
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools._schema import generate_tool_schema
from lauren_ai._transport import Completion, Message, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.testing.recording_transport import RecordingTransport
from agenthicc.runners.tool_populator import populate_agent_tools
from agenthicc.tools.fs.agent_tools import batch_copy, batch_move, batch_write, read_file
from agenthicc.plugins.registry import build_registry
from agenthicc.runners.tui_session import _make_session_tools
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.workflows.code_plan.phase_tools import make_questions_tool

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
        schemas = {tool["name"]: tool for tool in (generate_tool_schema(t)[2] for t in registry.tools)}

    assert not [
        record for record in caplog.records if "unrecognised type annotation" in record.message
    ]
    assert len(schemas) == 56
    assert schemas["ask_user"]["input_schema"]["properties"]["questions"]["items"]["type"] == "object"
    assert schemas["spawn_subagents"]["input_schema"]["properties"]["tasks"]["items"]["type"] == "object"


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
