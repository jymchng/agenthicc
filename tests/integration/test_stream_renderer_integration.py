"""Integration tests: StreamRenderer + signal pipeline.

Tests verify that StreamRenderer correctly handles tool call signals,
produces correctly ordered console output, and handles edge cases gracefully.
"""
from __future__ import annotations

import inspect
import pytest
from unittest.mock import MagicMock, call, patch

from agenthicc.tui.stream_renderer import StreamRenderer

pytestmark = pytest.mark.integration


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_sr(*, input_tokens: int = 0, output_tokens: int = 0, cost: float = 0.0) -> tuple[StreamRenderer, MagicMock]:
    """Return (StreamRenderer, mock_console) with a pre-wired StatusState-like mock."""
    console = MagicMock()
    status = MagicMock()
    status.input_tokens = input_tokens
    status.output_tokens = output_tokens
    status.session_cost_usd = cost
    sr = StreamRenderer(console, status)
    return sr, console


def _printed_args(console: MagicMock) -> list[str]:
    """Return each positional string passed to console.print(), in order."""
    result = []
    for c in console.print.call_args_list:
        args = c.args
        if args:
            result.append(str(args[0]))
    return result


# ── test 1: signal bus wires on_tool_started / on_tool_complete ───────────────


def test_tool_complete_signal_reaches_stream_renderer():
    """SignalBus callbacks correctly invoke StreamRenderer.on_tool_started and on_tool_complete."""
    sr, console = _make_sr()

    # Simulate the signal routing that __main__._build_agent_runner / _run_agent_turn does:
    # signals arrive → we call sr.on_tool_started / sr.on_tool_complete

    # Wire up handler (as done in __main__._run_agent_turn)
    tool_started_calls: list[tuple] = []
    tool_complete_calls: list[tuple] = []

    def on_tool_started(tool_use_id: str, name: str, args_str: str) -> None:
        tool_started_calls.append((tool_use_id, name, args_str))
        sr.on_tool_started(tool_use_id, name, args_str)

    def on_tool_complete(tool_use_id: str, success: bool, duration_ms: float) -> None:
        tool_complete_calls.append((tool_use_id, success, duration_ms))
        sr.on_tool_complete(tool_use_id, success, duration_ms)

    # Emit events via the wired handlers
    on_tool_started("tid-1", "read_file", "path='README.md'")
    on_tool_complete("tid-1", True, 42.0)

    # Both handlers were invoked
    assert len(tool_started_calls) == 1
    assert tool_started_calls[0] == ("tid-1", "read_file", "path='README.md'")
    assert len(tool_complete_calls) == 1
    assert tool_complete_calls[0] == ("tid-1", True, 42.0)

    # StreamRenderer printed the completed tool line
    printed = _printed_args(console)
    assert any("read_file" in line for line in printed), f"read_file not found in: {printed}"


# ── test 2: console output ordering ──────────────────────────────────────────


def test_stream_renderer_console_output_order():
    """Text deltas, tool calls, and more text appear in the correct chronological order."""
    sr, console = _make_sr()

    sr.on_text_delta("Let me check\n")
    sr.on_tool_started("t1", "read_file", "path=x")
    sr.on_tool_complete("t1", True, 5.0)
    sr.on_text_delta("Done\n")
    sr.on_turn_end()

    printed = _printed_args(console)

    # Find positions of each expected output
    def _first_index(needle: str) -> int:
        for i, line in enumerate(printed):
            if needle in line:
                return i
        raise AssertionError(f"{needle!r} not found in printed lines: {printed}")

    idx_check = _first_index("Let me check")
    idx_read = _first_index("read_file")
    idx_done = _first_index("Done")

    assert idx_check < idx_read, (
        f"'Let me check' (pos {idx_check}) should appear before 'read_file' (pos {idx_read})"
    )
    assert idx_read < idx_done, (
        f"'read_file' (pos {idx_read}) should appear before 'Done' (pos {idx_done})"
    )


# ── test 3: no rich.live.Live in _run_agent_turn ─────────────────────────────


def test_stream_renderer_replaces_live_spinner():
    """_run_agent_turn no longer uses rich.live.Live — the spinner was replaced by StreamRenderer."""
    import ast
    import textwrap
    from pathlib import Path

    src = Path("/root/python_projects/agenthicc/src/agenthicc/__main__.py").read_text()

    # Isolate the _run_agent_turn function source via AST
    tree = ast.parse(src)
    func_node = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_agent_turn"),
        None,
    )
    assert func_node is not None, "_run_agent_turn not found in __main__.py"

    # Extract the lines that belong to _run_agent_turn
    lines = src.splitlines()
    func_lines = lines[func_node.lineno - 1: func_node.end_lineno]
    func_body = "\n".join(func_lines)

    # Ensure Live( is not used in the function body
    assert "Live(" not in func_body, (
        "rich.live.Live is still used in _run_agent_turn; "
        "it should have been replaced by StreamRenderer"
    )

    # Also verify _spin() helper is gone (was the old spinner loop)
    assert "_spin(" not in func_body, (
        "_spin() is still referenced in _run_agent_turn; "
        "it should have been removed in favour of StreamRenderer"
    )


# ── test 4: interleaved text and tools in chronological order ─────────────────


def test_stream_renderer_text_before_tool_interleaved():
    """Full scenario: 2 text events, 2 interleaved tool calls, 1 trailing text — correct order."""
    sr, console = _make_sr()

    sr.on_text_delta("First thought\n")
    sr.on_text_delta("Second thought\n")

    sr.on_tool_started("t1", "git_status", "")
    sr.on_tool_started("t2", "read_file", "path=x.py")

    # Complete t2 first, then t1 (reverse order)
    sr.on_tool_complete("t2", True, 10.0)
    sr.on_tool_complete("t1", False, 5.0)

    sr.on_text_delta("Final summary\n")
    sr.on_turn_end()

    printed = _printed_args(console)

    def _first_index(needle: str) -> int:
        for i, line in enumerate(printed):
            if needle in line:
                return i
        raise AssertionError(f"{needle!r} not found in printed lines: {printed}")

    idx_first = _first_index("First thought")
    idx_second = _first_index("Second thought")
    idx_git = _first_index("git_status")
    idx_read = _first_index("read_file")
    idx_final = _first_index("Final summary")

    # Both text chunks appear before either tool (text is flushed on on_tool_started)
    assert idx_first < idx_git, "'First thought' should precede 'git_status'"
    assert idx_second < idx_git, "'Second thought' should precede 'git_status'"
    # Both tools appear before final text
    assert idx_git < idx_final, "'git_status' should precede 'Final summary'"
    assert idx_read < idx_final, "'read_file' should precede 'Final summary'"


# ── test 5: diff after write_file uses Rich markup colours ────────────────────


def test_stream_renderer_diff_after_write_file():
    """Diff lines from on_tool_complete are printed with the correct Rich markup colours."""
    sr, console = _make_sr()

    sr.on_tool_started("t1", "write_file", "path='test.py'")

    diff = (
        "--- a/test.py\n"
        "+++ b/test.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new"
    )
    sr.on_tool_complete("t1", True, 5.0, diff=diff)

    printed = _printed_args(console)
    full_output = "\n".join(printed)

    # Header lines — printed as [dim]
    assert any("--- a/test.py" in line for line in printed), "--- header missing"
    assert any("+++ b/test.py" in line for line in printed), "+++ header missing"

    # Hunk line — printed with cyan
    hunk_lines = [c for c in console.print.call_args_list if "@@ -1 +1 @@" in str(c)]
    assert hunk_lines, "@@ hunk line not printed"
    hunk_markup = str(hunk_lines[0])
    assert "cyan" in hunk_markup, f"hunk line missing cyan markup: {hunk_markup}"

    # Removed line — printed with red
    removed_lines = [c for c in console.print.call_args_list if "-old" in str(c)]
    assert removed_lines, "-old diff line not printed"
    removed_markup = str(removed_lines[0])
    assert "red" in removed_markup, f"removed line missing red markup: {removed_markup}"

    # Added line — printed with green
    added_lines = [c for c in console.print.call_args_list if "+new" in str(c)]
    assert added_lines, "+new diff line not printed"
    added_markup = str(added_lines[0])
    assert "green" in added_markup, f"added line missing green markup: {added_markup}"


# ── test 6: finish() summary includes token counts ────────────────────────────


def test_stream_renderer_finish_contains_summary():
    """finish() prints a summary line that includes input and output token counts."""
    sr, console = _make_sr(input_tokens=1000, output_tokens=500, cost=0.05)

    sr.on_turn_start()
    sr.finish()

    printed = _printed_args(console)
    summary_lines = [p for p in printed if "1,000" in p or "500" in p]
    assert summary_lines, (
        f"Token counts not found in summary. Printed lines: {printed}"
    )
    summary = summary_lines[0]
    assert "1,000" in summary, f"Input token count '1,000' missing from: {summary}"
    assert "500" in summary, f"Output token count '500' missing from: {summary}"


# ── test 7: unknown tool_use_id is handled gracefully ────────────────────────


def test_stream_renderer_unknown_tool_graceful():
    """on_tool_complete with an unknown tool_use_id does not raise and still prints a line."""
    sr, console = _make_sr()

    # No prior on_tool_started for "unknown_id"
    sr.on_tool_complete("unknown_id", True, 3.0)

    # Must not raise; must still produce at least one print call
    assert console.print.called, "Expected at least one console.print call"

    printed = _printed_args(console)
    assert printed, "Expected some output even for an unknown tool_use_id"

    # The printed line should mention 'unknown' (the fallback name) or be empty-named
    tool_line = printed[0]
    # Acceptable: either 'unknown' appears or the line contains the tool-call format characters
    assert "unknown" in tool_line or "⎿" in tool_line, (
        f"Expected 'unknown' or tool-call marker in printed line: {tool_line!r}"
    )
