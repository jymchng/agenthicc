"""Regression tests for tool-result previews emitted by AgentTurnRunner."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lauren_ai._signals import ToolCallComplete

from agenthicc.runners.agent_turn import (
    AgentTurnRunner,
    _ToolOutputCaptureHook,
    _fmt_args,
    _tool_output_preview,
)
from agenthicc.tools.hooks import AfterToolHookDecision, ToolCallContext

pytestmark = pytest.mark.unit


def test_tool_argument_preview_keeps_inspection_module_names() -> None:
    rendered = _fmt_args(
        {
            "module": "agenthicc.workflows.code_plan.definition",
            "max_chars": 10_000,
        }
    )

    assert "agenthicc.workflows.code_plan.definition" in rendered
    assert "max_chars=10000" in rendered


def test_phase_allowlist_builds_agent_with_write_file_but_not_shell() -> None:
    from lauren_ai._agents import AGENT_META

    from agenthicc.runners.agent_turn_context import AgentTurnContext

    context = AgentTurnContext(
        text="write the workflow",
        runner=SimpleNamespace(_transport=MagicMock(), _signals=None),
        processor=MagicMock(),
        exec_cfg=SimpleNamespace(base_system_prompt=""),
        active_agent="executor",
        project_plugin_tools=[],
        mcp_registry=None,
        allowed_tool_names=frozenset({"write_file"}),
    )
    runner = AgentTurnRunner(context)
    runner._model_id = "mock-model"

    agent, _active_runner = runner._build_agent()
    meta = getattr(type(agent), AGENT_META)

    assert set(meta.tools) == {"write_file"}
    assert "shell" not in meta.tools
    assert "run_bash" not in meta.tools
    assert "Do not call shell" in meta.system
    assert "path and complete content" in meta.system


def test_default_turn_prompt_mentions_injected_spawn_subagents() -> None:
    """The parent prompt and provider schema expose the session-bound tool."""
    from lauren_ai._agents import AGENT_META

    from agenthicc.runners.agent_turn_context import AgentTurnContext

    context = AgentTurnContext(
        text="delegate repository research",
        runner=SimpleNamespace(_transport=MagicMock(), _signals=None),
        processor=MagicMock(),
        exec_cfg=SimpleNamespace(base_system_prompt=""),
        active_agent="default",
        project_plugin_tools=[],
        mcp_registry=None,
    )
    runner = AgentTurnRunner(context)
    runner._model_id = "mock-model"

    agent, _active_runner = runner._build_agent()
    meta = getattr(type(agent), AGENT_META)

    assert "spawn_subagents" in meta.tools
    assert "spawn_subagents" in meta.system


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
