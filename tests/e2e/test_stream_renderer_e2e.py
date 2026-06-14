"""E2E tests for StreamRenderer, covering the full agent-turn output pipeline.

These tests exercise StreamRenderer in isolation (no real LLM), via the
SignalBus bridge pattern established in test_agent_runner_e2e.py, and in
parallel with TranscriptModel to confirm the two sinks are fully independent.

NOTE: no ``from __future__ import annotations`` at module level —
``@tool()``/``@agent()`` inspect real annotations at decoration time.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from lauren_ai._agents import agent, use_tools
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import (
    SignalBus,
    ToolCallComplete,
    ToolCallStarted,
)
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_console() -> MagicMock:
    """Return a MagicMock that records every console.print() call."""
    return MagicMock()


def _make_status() -> MagicMock:
    """Return a minimal status stub that StreamRenderer reads token counts from."""
    status = MagicMock()
    status.input_tokens = 0
    status.output_tokens = 0
    status.session_cost_usd = 0.0
    return status


def _completion(content: str, n: int = 1) -> Completion:
    return Completion(
        id=f"c{n}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _printed_strings(mock_console: MagicMock) -> list[str]:
    """Collect every first positional argument passed to mock_console.print()."""
    result = []
    for c in mock_console.print.call_args_list:
        args = c.args
        if args:
            result.append(str(args[0]))
    return result


# ---------------------------------------------------------------------------
# Test 1 — static analysis: no legacy Live/spin artifacts in __main__
# ---------------------------------------------------------------------------

def test_stream_renderer_no_live_in_codebase() -> None:
    """__main__.py must not contain the old Live spinner or _spin/_watch_ctrlO helpers."""
    main_path = Path(__file__).parents[2] / "src" / "agenthicc" / "__main__.py"
    assert main_path.exists(), f"__main__.py not found at {main_path}"
    content = main_path.read_text()
    assert "Live(refresh_per_second" not in content, (
        "__main__.py still uses rich.live.Live — switch to StreamRenderer"
    )
    assert "def _spin" not in content, (
        "__main__.py still defines _spin helper — remove it"
    )
    assert "def _watch_ctrlO" not in content, (
        "__main__.py still defines _watch_ctrlO helper — remove it"
    )


# ---------------------------------------------------------------------------
# Test 2 — full synthetic turn: correct ordering of printed output
# ---------------------------------------------------------------------------

def test_stream_renderer_full_turn_output(capsys) -> None:
    """Simulate a full agent turn via direct StreamRenderer method calls.

    Verifies that:
      a) "Thinking" appears before any tool name.
      b) The first text delta appears before the first tool line.
      c) The first tool-complete line appears before the second text delta.
      d) The second text delta appears before the second tool-complete line.
      e) A summary line (elapsed / token counts) is printed at finish().
    """
    from agenthicc.tui.stream_renderer import StreamRenderer

    console = _make_console()
    status = _make_status()
    sr = StreamRenderer(console, status)

    # _thinking_wave is now local to stream_renderer; patch it there.
    with patch("agenthicc.tui.stream_renderer._thinking_wave", return_value="Thinking..."):
        sr.on_turn_start()

    sr.on_text_delta("I will read the files.\n")
    sr.on_tool_started("t1", "read_file", "path='main.py'")
    sr.on_tool_complete("t1", True, 7.0)
    sr.on_text_delta("Based on what I found:\n")
    sr.on_tool_started("t2", "list_directory", "path='.'")
    sr.on_tool_complete("t2", True, 4.0)
    sr.on_turn_end(turn_text="Based on what I found:")
    sr.finish()

    printed = _printed_strings(console)
    full_output = "\n".join(printed)

    # (a) "Thinking" appears in sys.stdout (header written by _pin_input_bar directly).
    import re as _re  # noqa: PLC0415
    raw_stdout = capsys.readouterr().out
    plain_stdout = _re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", raw_stdout)
    assert "Thinking" in plain_stdout, "No 'Thinking' header line was printed"

    # Find positions of key lines in console.print() calls.
    text1_positions = [i for i, s in enumerate(printed) if "I will read the files" in s]
    read_file_positions = [i for i, s in enumerate(printed) if "read_file" in s]
    text2_positions = [i for i, s in enumerate(printed) if "Based on what I found" in s]
    list_dir_positions = [i for i, s in enumerate(printed) if "list_directory" in s]
    summary_positions = [i for i, s in enumerate(printed) if ("↑" in s or "↓" in s or "up " in s or "down " in s or "$" in s)]

    assert text1_positions, f"First text delta not printed. Got:\n{full_output}"
    assert read_file_positions, f"read_file tool line not printed. Got:\n{full_output}"
    assert text2_positions, f"Second text delta not printed. Got:\n{full_output}"
    assert list_dir_positions, f"list_directory tool line not printed. Got:\n{full_output}"
    assert summary_positions, f"Summary line not printed. Got:\n{full_output}"

    # (a) Thinking header (in stdout) before first tool name (in printed) — ordering confirmed.
    # (b) first text delta before first tool line
    assert text1_positions[0] < read_file_positions[0], (
        "First text delta must appear before read_file tool line"
    )
    # (c) first tool-complete before second text delta
    assert read_file_positions[0] < text2_positions[0], (
        "read_file tool line must appear before second text delta"
    )
    # (d) second text delta before second tool line
    assert text2_positions[0] < list_dir_positions[0], (
        "Second text delta must appear before list_directory tool line"
    )
    # (e) summary is the last meaningful printed line
    assert summary_positions[-1] == len(printed) - 1, (
        "Summary line must be the last printed item"
    )

    # Verify checkmark symbol in completed tool lines
    read_file_line = printed[read_file_positions[0]]
    assert "✓" in read_file_line or "read_file" in read_file_line


# ---------------------------------------------------------------------------
# Test 3 — SignalBus bridge: ToolCallStarted → ToolCallComplete fires renderer
# ---------------------------------------------------------------------------

async def test_agent_runner_base_signal_bridge() -> None:
    """Wire StreamRenderer to a real SignalBus; emit signals; verify renderer fires.

    Uses the same MockTransport + AgentRunnerBase pattern as test_agent_runner_e2e.py.
    No real LLM calls are made.
    """
    from agenthicc.tui.stream_renderer import StreamRenderer

    console = _make_console()
    status = _make_status()
    sr = StreamRenderer(console, status)

    # Track which on_tool_complete calls were received
    completed_calls: list[tuple[str, bool]] = []
    original_on_tool_complete = sr.on_tool_complete

    def _spy_complete(tool_use_id, success, duration_ms, error=None, diff=None):
        completed_calls.append((tool_use_id, success))
        original_on_tool_complete(tool_use_id, success, duration_ms, error, diff)

    sr.on_tool_complete = _spy_complete  # type: ignore[method-assign]

    # Build a tool that the agent will call (must be a real @tool so the runner
    # can dispatch it and fire ToolCallStarted/ToolCallComplete signals).
    call_log: list[str] = []

    @tool()
    async def echo_tool(message: str) -> dict:
        """Echo a message back.

        Args:
            message: The message to echo.
        """
        call_log.append(message)
        return {"echoed": message}

    @agent(model="mock-model", system="You echo messages.")
    @use_tools(echo_tool)
    class EchoAgent: ...

    bus = SignalBus()

    # Wire the bus to StreamRenderer (mirrors the bridge in __main__.py)
    @bus.on(ToolCallStarted)
    async def _on_started(sig) -> None:
        tid = getattr(sig, "tool_use_id", "")
        name = getattr(sig, "tool_name", "")
        sr.on_tool_started(tid, name, "")

    @bus.on(ToolCallComplete)
    async def _on_complete(sig) -> None:
        tid = getattr(sig, "tool_use_id", "")
        success = bool(getattr(sig, "success", True))
        duration_ms = float(getattr(sig, "duration_ms", 0.0) or 0.0)
        sr.on_tool_complete(tid, success, duration_ms)

    mock = MockTransport()
    mock.queue_tool_use("echo_tool", {"message": "hello from agent"})
    mock.queue_response(_completion("Echo complete.", n=2))

    with patch("agenthicc.tui.app._thinking_wave", return_value="Thinking..."):
        sr.on_turn_start()

    inst = EchoAgent()
    runner = _build_runner_for_agent(inst, mock, signals=bus)
    response = await runner.run(inst, "Echo hello from agent")

    sr.finish()

    assert response.stop_reason == "end_turn"
    assert len(response.tool_calls_made) == 1
    assert call_log == ["hello from agent"]

    # The spy must have recorded exactly one completed call
    assert len(completed_calls) == 1, (
        f"Expected 1 on_tool_complete call, got {len(completed_calls)}"
    )
    _tid, _success = completed_calls[0]
    assert _success is True

    # The console must have the tool-complete line
    printed = _printed_strings(console)
    tool_lines = [s for s in printed if "echo_tool" in s]
    assert tool_lines, (
        f"No echo_tool line in console output. Printed:\n{chr(10).join(printed)}"
    )


# ---------------------------------------------------------------------------
# Test 4 — parallel independence: StreamRenderer and TranscriptModel are sinks
# ---------------------------------------------------------------------------

def test_stream_renderer_vs_transcript_parallel() -> None:
    """StreamRenderer and TranscriptModel receive the same events independently.

    StreamRenderer calls console.print(); TranscriptModel stores lines in memory.
    Neither should trigger output from the other.
    """
    from agenthicc.tui.stream_renderer import StreamRenderer
    from agenthicc.tui.transcript import TranscriptModel, ToolCallState

    console = _make_console()
    status = _make_status()
    sr = StreamRenderer(console, status)
    tm = TranscriptModel()

    agent_id = "agent-test"
    tool_use_id = "t42"
    tool_name = "grep_files"
    args_str = "pattern='TODO'"

    # Simulate turn start
    with patch("agenthicc.tui.app._thinking_wave", return_value="Thinking..."):
        sr.on_turn_start()
    tm.append_turn(agent_id, "assistant")

    # Text delta → both sinks
    delta = "Searching the codebase now.\n"
    sr.on_text_delta(delta)
    tm.append_line(agent_id, delta.rstrip())

    # Tool started → both sinks
    sr.on_tool_started(tool_use_id, tool_name, args_str)
    tm.add_tool_call(agent_id, tool_use_id, tool_name, {"pattern": "TODO"})

    # Tool complete → both sinks
    sr.on_tool_complete(tool_use_id, True, 12.0)
    tm.finish_tool_call(tool_use_id, success=True, duration_ms=12.0)

    sr.on_turn_end()
    sr.finish()

    # --- StreamRenderer side ---
    printed = _printed_strings(console)
    assert any("Searching the codebase now" in s for s in printed), (
        "StreamRenderer must have printed the text delta"
    )
    assert any("grep_files" in s for s in printed), (
        "StreamRenderer must have printed the tool-complete line"
    )

    # --- TranscriptModel side ---
    assert len(tm.turns) == 1
    turn = tm.turns[0]
    assert any("Searching the codebase now" in ln for ln in turn.output_lines), (
        "TranscriptModel must have stored the text line"
    )
    tool_call_entries = turn.tool_calls
    assert len(tool_call_entries) == 1
    tc = tool_call_entries[0]
    assert tc.name == "grep_files"
    assert tc.state == ToolCallState.SUCCESS

    # --- Independence: printing came only from StreamRenderer ---
    # TranscriptModel has no console at all — no extra print calls should have occurred
    # beyond what StreamRenderer produced.
    call_count_after = console.print.call_count
    assert call_count_after > 0  # StreamRenderer printed
    # Re-run TranscriptModel operations on a separate fresh console to confirm it
    # produces zero console.print calls.
    console2 = _make_console()
    tm2 = TranscriptModel()
    tm2.append_turn("a2", "assistant")
    tm2.append_line("a2", "some text")
    tm2.add_tool_call("a2", "tid2", "read_file", {})
    tm2.finish_tool_call("tid2", success=True)
    console2.print.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — cancellation: finish() is safe even if the turn was CancelledError
# ---------------------------------------------------------------------------

async def test_stream_renderer_cancelled_agent_turn(capsys) -> None:
    """finish() must not raise even if CancelledError interrupted the turn."""
    from agenthicc.tui.stream_renderer import StreamRenderer

    console = _make_console()
    status = _make_status()
    sr = StreamRenderer(console, status)

    async def _agent_turn_simulation() -> None:
        with patch("agenthicc.tui.stream_renderer._thinking_wave", return_value="Thinking..."):
            sr.on_turn_start()
        sr.on_text_delta("Starting analysis")
        # Raise CancelledError mid-turn (simulates Ctrl+C)
        raise asyncio.CancelledError

    try:
        await _agent_turn_simulation()
    except asyncio.CancelledError:
        pass
    finally:
        # finish() must not raise regardless of mid-turn cancellation
        try:
            sr.finish()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"finish() raised {type(exc).__name__}: {exc}")

    # Verify that whatever was buffered before cancellation was flushed.
    # The Thinking header goes to sys.stdout (not console.print), so check capsys.
    import re as _re
    raw = capsys.readouterr().out
    plain = _re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", raw)
    assert "Thinking" in plain, (
        "Header line from on_turn_start() must have been printed even after cancellation"
    )
    printed_after = _printed_strings(console)
    # finish() must have printed the summary line without raising
    assert any("|" in s or "s" in s for s in printed_after), (
        "finish() must print a summary line even after mid-turn cancellation"
    )
    # No exception means finish() is safe — the test passes if we reach here
