"""Regression tests for tool-result previews emitted by AgentTurnRunner."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from lauren_ai._signals import ToolCallComplete

from agenthicc.runners.agent_turn import (
    AgentTurnRunner,
    _ToolOutputCaptureHook,
    _tool_output_preview,
)
from agenthicc.tools.hooks import AfterToolHookDecision, ToolCallContext

pytestmark = pytest.mark.unit


def test_tool_output_preview_prefers_file_content_and_counts_omitted_lines() -> None:
    preview, omitted = _tool_output_preview({"content": "one\ntwo\nthree\nfour\nfive"})

    assert preview == ["one", "two", "three", "four"]
    assert omitted == 1


@pytest.mark.asyncio
async def test_output_capture_hook_records_native_tool_result() -> None:
    outputs: dict[str, tuple[list[str], int]] = {}
    hook = _ToolOutputCaptureHook(outputs)
    ctx = ToolCallContext(agent_context=None, tool_use_id="call-2", turn=0)

    decision = await hook.after_tool_call({"content": "one\ntwo\nthree\nfour\nfive"}, ctx)

    assert isinstance(decision, AfterToolHookDecision)
    assert outputs["call-2"] == (["one", "two", "three", "four"], 1)


@pytest.mark.asyncio
async def test_tool_completion_event_contains_result_preview() -> None:
    conv_store = MagicMock()
    ctx = MagicMock()
    ctx.conv_store = conv_store
    runner = AgentTurnRunner(ctx)
    runner._tool_names["call-1"] = "read_file"
    runner._tool_args["call-1"] = {"path": "README.md"}
    runner._tool_outputs["call-1"] = (["line one", "line two"], 4)

    await runner._handle_tool_complete(
        ToolCallComplete(
            tool_name="read_file",
            tool_use_id="call-1",
            duration_ms=3.0,
            success=True,
        )
    )

    payload = conv_store.append_event.call_args.args[1]
    assert payload["name"] == "read_file"
    assert payload["output_lines"] == ["line one", "line two"]
    assert payload["output_more"] == 4
