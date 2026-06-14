"""Unit tests for StreamRenderer (agenthicc.tui.stream_renderer)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agenthicc.tui.stream_renderer import StreamRenderer

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


class MockStatus:
    input_tokens: int = 0
    output_tokens: int = 0
    session_cost_usd: float = 0.0
    spinner_frame: int = 0
    active: bool = False
    completed_agents: int = 0


def _make_sr() -> tuple[StreamRenderer, MagicMock]:
    """Return (StreamRenderer, mock_console) wired together."""
    console = MagicMock()
    sr = StreamRenderer(console, MockStatus())
    return sr, console


def _calls(mock_console: MagicMock) -> list[str]:
    """Flatten all console.print positional args into a list of strings."""
    return [str(call) for call in mock_console.print.call_args_list]


def _on_turn_start(sr: StreamRenderer) -> None:
    """Call on_turn_start with _thinking_wave patched to avoid app.py import issues."""
    # on_turn_start does `from agenthicc.tui.app import _thinking_wave` lazily.
    # We patch the name in the agenthicc.tui.app namespace so the lazy import
    # picks up the mock regardless of whether app.py has syntax issues.
    with patch("agenthicc.tui.app._thinking_wave", return_value="Thinking..."):
        sr.on_turn_start()


# ── test_on_turn_start_prints_header ─────────────────────────────────────────


def test_on_turn_start_prints_header(capsys):
    sr, console = _make_sr()
    _on_turn_start(sr)
    # The animated Thinking header is written directly to sys.stdout via
    # _pin_input_bar() (not through Rich's console.print), so check capsys.
    raw = capsys.readouterr().out
    # Strip ANSI codes for a plain-text assertion
    import re as _re
    plain = _re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", raw)
    assert "Thinking" in plain


# ── test_on_turn_start_resets_state ──────────────────────────────────────────


def test_on_turn_start_resets_state():
    sr, console = _make_sr()
    # Dirty the state first
    sr.on_tool_started("t0", "dummy", "")
    sr._text_buf.append("some text")
    _on_turn_start(sr)
    assert sr._pending == {}
    assert sr._text_buf == []


# ── test_tool_complete_printed_immediately ────────────────────────────────────


def test_tool_complete_printed_immediately():
    sr, console = _make_sr()
    sr.on_tool_started("t1", "read_file", "path='a.py'")
    sr.on_tool_complete("t1", True, 7.0)
    calls = _calls(console)
    all_output = " ".join(calls)
    assert "read_file" in all_output
    assert "✓" in all_output
    assert "7" in all_output  # duration digits present (7ms)


# ── test_tool_complete_failure ────────────────────────────────────────────────


def test_tool_complete_failure():
    sr, console = _make_sr()
    sr.on_tool_started("t1", "shell", "")
    sr.on_tool_complete("t1", False, 0.0, error="TypeError: missing command")
    calls = _calls(console)
    all_output = " ".join(calls)
    assert "✗" in all_output
    assert "TypeError" in all_output


# ── test_text_delta_buffered_flushes_at_newline ───────────────────────────────


def test_text_delta_buffered_flushes_at_newline():
    sr, console = _make_sr()
    sr.on_text_delta("Hello\n")
    # A newline triggers a flush → buffer is empty and console was called.
    assert sr._text_buf == []
    calls = _calls(console)
    all_output = " ".join(calls)
    assert "Hello" in all_output


# ── test_text_delta_buffered_flushes_at_120_chars ────────────────────────────


def test_text_delta_buffered_flushes_at_120_chars():
    sr, console = _make_sr()
    sr.on_text_delta("x" * 130)
    # 130 chars >= 120 triggers a flush → buffer is cleared.
    assert sr._text_buf == []
    assert console.print.called


# ── test_text_delta_not_flushed_when_short ───────────────────────────────────


def test_text_delta_not_flushed_when_short():
    sr, console = _make_sr()
    sr.on_text_delta("short")
    # "short" (5 chars, no newline) must stay buffered.
    assert sr._text_buf != []
    assert "short" in "".join(sr._text_buf)


# ── test_text_before_tool_order ───────────────────────────────────────────────


def test_text_before_tool_order():
    sr, console = _make_sr()
    sr.on_text_delta("Pre-text\n")   # newline → immediate flush
    sr.on_tool_started("t1", "list_directory", "path='.'")
    sr.on_tool_complete("t1", True, 5.0)
    calls = _calls(console)
    # Find the call index that contains "Pre-text" and the one with "list_directory"
    text_idx = next(i for i, c in enumerate(calls) if "Pre-text" in c)
    tool_idx = next(i for i, c in enumerate(calls) if "list_directory" in c)
    assert text_idx < tool_idx


# ── test_on_turn_end_flushes_text ────────────────────────────────────────────


def test_on_turn_end_flushes_text():
    sr, console = _make_sr()
    sr.on_text_delta("partial")       # short, stays buffered
    sr.on_turn_end(turn_text="partial")
    # After turn_end the buffer must be empty.
    assert sr._text_buf == []


# ── test_finish_prints_summary ────────────────────────────────────────────────


def test_finish_prints_summary():
    sr, console = _make_sr()
    sr.on_turn_start()
    console.reset_mock()    # clear the header calls
    sr.finish()
    # finish() prints a summary line containing elapsed time + token counts.
    calls = _calls(console)
    assert len(calls) >= 1, "finish() should print at least one summary line"
    all_output = " ".join(calls)
    # Summary contains time (e.g. "0.0s") and cost/token markers.
    assert "s" in all_output   # seconds marker


# ── test_diff_printed_after_tool ──────────────────────────────────────────────


def test_diff_printed_after_tool():
    sr, console = _make_sr()
    diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n"
    sr.on_tool_started("t1", "write_file", "path='f.py'")
    sr.on_tool_complete("t1", True, 3.0, diff=diff)
    calls = _calls(console)
    all_output = " ".join(calls)
    assert "+new" in all_output
    assert "-old" in all_output


# ── test_diff_truncated_at_8_lines ────────────────────────────────────────────


def test_diff_truncated_at_8_lines():
    sr, console = _make_sr()
    diff = "\n".join(["+line" + str(i) for i in range(20)])
    sr.on_tool_started("t1", "write_file", "path='f.py'")
    sr.on_tool_complete("t1", True, 1.0, diff=diff)
    calls = _calls(console)
    all_output = " ".join(calls)
    # 20 diff lines → first 8 shown + overflow hint "… 12 more line(s)"
    assert "12 more line" in all_output


# ── test_parallel_tools_both_printed ─────────────────────────────────────────


def test_parallel_tools_both_printed():
    sr, console = _make_sr()
    sr.on_tool_started("A", "read_file", "path='a.py'")
    sr.on_tool_started("B", "read_file", "path='b.py'")
    sr.on_tool_complete("A", True, 5.0)
    sr.on_tool_complete("B", True, 3.0)
    calls = _calls(console)
    all_output = " ".join(calls)
    assert "a.py" in all_output
    assert "b.py" in all_output


# ── test_no_rich_live_import ──────────────────────────────────────────────────


def test_no_rich_live_import():
    import agenthicc.tui.stream_renderer as m
    import ast

    with open(m.__file__) as fh:
        source = fh.read()

    # Parse the source and check for any 'from rich.live import' or 'import rich.live'
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module != "rich.live", "stream_renderer must not import from rich.live"
            assert not module.startswith("rich.live."), (
                "stream_renderer must not import from rich.live.*"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "rich.live", (
                    "stream_renderer must not import rich.live"
                )
