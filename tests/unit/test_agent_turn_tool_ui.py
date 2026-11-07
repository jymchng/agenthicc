"""Regression tests for tool-result previews emitted by AgentTurnRunner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lauren_ai._signals import ToolCallComplete

from agenthicc.runners.agent_turn import (
    AgentTurnRunner,
    _capture_tool_result,
    _ToolCapture,
    _ToolOutputCaptureHook,
)
from agenthicc.tui.tool_summary import OutputProjection, ToolSummary
from agenthicc.tools.hooks import AfterToolHookDecision, ToolCallContext
from agenthicc.config import ExecutionSettings

pytestmark = pytest.mark.unit


def test_tool_arguments_are_kept_whole_for_inspection_module_names() -> None:
    from agenthicc.tui.tool_summary import bounded_args

    args = bounded_args({"module": "agenthicc.workflows.code_plan.definition", "max_chars": 10_000})

    assert args == {"module": "agenthicc.workflows.code_plan.definition", "max_chars": 10_000}


def test_phase_allowlist_builds_agent_with_write_file_but_not_shell() -> None:
    from lauren_ai._agents import AGENT_META

    from agenthicc.runners.agent_turn_context import AgentTurnContext

    context = AgentTurnContext(
        text="write the workflow",
        runner=SimpleNamespace(_transport=MagicMock(), _signals=None),
        processor=MagicMock(),
        exec_cfg=ExecutionSettings(),
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
    assert "Do not call run_bash" in meta.system
    assert "path and complete content" in meta.system


def test_default_turn_prompt_mentions_injected_spawn_subagents(tmp_path: Path) -> None:
    """The parent prompt and provider schema expose the session-bound tool."""
    from lauren_ai._agents import AGENT_META

    from agenthicc.runners.agent_turn_context import AgentTurnContext

    context = AgentTurnContext(
        text="delegate repository research",
        runner=SimpleNamespace(_transport=MagicMock(), _signals=None),
        processor=MagicMock(),
        exec_cfg=ExecutionSettings(),
        active_agent="default",
        project_plugin_tools=[],
        mcp_registry=None,
        conversation_id="parent-session",
        workspace_access=SimpleNamespace(
            scope=SimpleNamespace(primary_root=tmp_path),
        ),
    )
    runner = AgentTurnRunner(context)
    runner._model_id = "mock-model"

    agent, _active_runner = runner._build_agent()
    meta = getattr(type(agent), AGENT_META)

    assert "spawn_subagents" in meta.tools
    assert "spawn_subagents" in meta.system
    assert "spawn_worker_agents" in meta.tools
    assert "spawn_worker_agents" in meta.system


def test_tool_output_capture_prefers_file_content_and_counts_every_line() -> None:
    capture = _capture_tool_result(
        "read_file",
        {"path": "/w/README.md"},
        {"content": "one\ntwo\nthree\nfour\nfive"},
        Path("/w"),
        None,
    )

    assert capture.output == OutputProjection(("one", "two", "three", "four", "five"), 5, 5)
    assert capture.summary == ToolSummary("README.md")


@pytest.mark.asyncio
async def test_output_capture_hook_records_native_tool_result() -> None:
    outputs: dict[str, _ToolCapture] = {}
    hook = _ToolOutputCaptureHook(outputs, root=Path("/w"))
    ctx = ToolCallContext(
        agent_context=None,
        tool_use_id="call-2",
        turn=0,
        tool_name="run_bash",
        tool_input={"command": "cd /w/app && npm test"},
    )

    decision = await hook.after_tool_call(
        {"ok": False, "state": "failed", "returncode": 1, "stdout": "a\nb", "stderr": "boom"},
        ctx,
    )

    assert isinstance(decision, AfterToolHookDecision)
    capture = outputs["call-2"]
    assert capture.output.lines == ("a", "b", "boom")
    assert capture.summary == ToolSummary("npm test", "in app/")
    assert capture.exit_code == 1


@pytest.mark.asyncio
async def test_tool_completion_event_is_structured_data_without_markup() -> None:
    conv_store = MagicMock()
    ctx = MagicMock()
    ctx.conv_store = conv_store
    ctx.workspace_access = None
    runner = AgentTurnRunner(ctx)
    runner._tool_names["call-1"] = "read_file"
    runner._tool_args["call-1"] = {"path": "README.md"}
    runner._tool_outputs["call-1"] = _ToolCapture(
        OutputProjection(("line one", "line two", "line nine"), 2, 9), ToolSummary("README.md")
    )

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
    assert payload["summary"] == "README.md"
    assert payload["args"] == {"path": "README.md"}
    assert payload["duration_ms"] == 3
    assert payload["output_lines"] == ["line one", "line two", "line nine"]
    assert (payload["output_head"], payload["output_total"]) == (2, 9)
    assert (payload["exit_code"], payload["error_code"]) == (None, None)
    assert set(payload) == {
        "tool_use_id",
        "name",
        "success",
        "state",
        "ready",
        "summary",
        "summary_detail",
        "args",
        "duration_ms",
        "output_lines",
        "output_head",
        "output_total",
        "exit_code",
        "error_code",
    }
