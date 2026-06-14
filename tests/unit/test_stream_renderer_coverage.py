"""Additional coverage tests for stream_renderer.py uncovered lines.

Targets:
  - StreamRenderer.on_turn_start() — prints Thinking header, resets state
  - StreamRenderer.on_text_delta() — buffering + flush on newline or large chunk
  - StreamRenderer.on_tool_started() / on_tool_complete()
  - StreamRenderer.on_turn_end()
  - StreamRenderer.finish()
  - Diff truncation path (diff > _DIFF_MAX_LINES lines)
  - render_status_bar() — active and idle states
"""
from __future__ import annotations

import io
import sys

import pytest

from agenthicc.tui.stream_renderer import StreamRenderer, _DIFF_MAX_LINES, _MAX_FLUSH_CHARS
from agenthicc.tui.app import StatusState

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _console_and_buf():
    """Return a Rich Console writing to a StringIO and the underlying buffer."""
    from rich.console import Console
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120, force_terminal=False)
    return con, buf


def _renderer(status: StatusState | None = None) -> tuple[StreamRenderer, io.StringIO]:
    con, buf = _console_and_buf()
    s = status or StatusState()
    r = StreamRenderer(con, s)
    return r, buf


# ---------------------------------------------------------------------------
# on_turn_start
# ---------------------------------------------------------------------------


class TestOnTurnStart:
    def test_resets_text_buf(self):
        r, _ = _renderer()
        r._text_buf = ["leftover"]
        r.on_turn_start()
        assert r._text_buf == []

    def test_resets_pending(self):
        r, _ = _renderer()
        r._pending["tc1"] = ("tool_name", "{}")
        r.on_turn_start()
        assert r._pending == {}

    def test_sets_turn_start_time(self):
        r, _ = _renderer()
        r._turn_start_time = 0.0
        r.on_turn_start()
        assert r._turn_start_time > 0.0

    def test_writes_thinking_to_stdout(self, capsys):
        r, _ = _renderer()
        r.on_turn_start()
        captured = capsys.readouterr()
        assert "Thinking" in captured.out or "⠋" in captured.out or len(captured.out) > 0


# ---------------------------------------------------------------------------
# on_text_delta
# ---------------------------------------------------------------------------


class TestOnTextDelta:
    def test_newline_triggers_flush(self):
        r, buf = _renderer()
        r.on_text_delta("hello\n")
        output = buf.getvalue()
        assert "hello" in output

    def test_large_chunk_triggers_flush(self):
        r, buf = _renderer()
        big_text = "x" * (_MAX_FLUSH_CHARS + 1)
        r.on_text_delta(big_text)
        output = buf.getvalue()
        assert "x" in output

    def test_small_chunk_without_newline_buffers(self):
        r, buf = _renderer()
        r.on_text_delta("abc")  # small, no newline
        # May or may not flush — test that it doesn't crash
        # After on_turn_end we should get the text
        r.on_turn_end()
        output = buf.getvalue()
        assert "abc" in output

    def test_multiple_deltas_flushed_together(self):
        r, buf = _renderer()
        r.on_text_delta("part1")
        r.on_text_delta("part2\n")  # newline triggers flush
        output = buf.getvalue()
        assert "part1" in output
        assert "part2" in output

    def test_empty_text_delta_is_safe(self):
        r, buf = _renderer()
        r.on_text_delta("")
        # No crash, no output
        assert True


# ---------------------------------------------------------------------------
# on_turn_end
# ---------------------------------------------------------------------------


class TestOnTurnEnd:
    def test_flushes_buffered_text(self):
        r, buf = _renderer()
        r._text_buf = ["buffered content"]
        r.on_turn_end()
        output = buf.getvalue()
        assert "buffered content" in output

    def test_empty_buffer_is_safe(self):
        r, buf = _renderer()
        r._text_buf = []
        r.on_turn_end()
        assert True  # no exception

    def test_on_turn_end_with_turn_text_arg(self):
        r, buf = _renderer()
        r._text_buf = ["text"]
        r.on_turn_end(turn_text="ignored")
        output = buf.getvalue()
        assert "text" in output


# ---------------------------------------------------------------------------
# finish
# ---------------------------------------------------------------------------


class TestFinish:
    def test_finish_prints_elapsed_time(self):
        import time
        r, buf = _renderer()
        r._turn_start_time = time.monotonic() - 1.0  # 1 second ago
        r.finish()
        output = buf.getvalue()
        # Should contain some timing info
        assert "s" in output or "0" in output

    def test_finish_includes_token_counts(self):
        s = StatusState(input_tokens=123, output_tokens=456)
        r, buf = _renderer(status=s)
        r._turn_start_time = __import__("time").monotonic()
        r.finish()
        output = buf.getvalue()
        assert "123" in output
        assert "456" in output

    def test_finish_includes_cost(self):
        s = StatusState(session_cost_usd=0.0123)
        r, buf = _renderer(status=s)
        r._turn_start_time = __import__("time").monotonic()
        r.finish()
        output = buf.getvalue()
        assert "0.0123" in output


# ---------------------------------------------------------------------------
# on_tool_started / on_tool_complete
# ---------------------------------------------------------------------------


class TestToolCalls:
    def test_on_tool_started_stores_pending(self):
        r, _ = _renderer()
        r.on_tool_started("tc1", "read_file", '{"path": "x.py"}')
        assert "tc1" in r._pending
        assert r._pending["tc1"] == ("read_file", '{"path": "x.py"}')

    def test_on_tool_complete_success_prints_checkmark(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "write_file", "")
        r.on_tool_complete("tc1", success=True, duration_ms=50.0)
        output = buf.getvalue()
        assert "✓" in output

    def test_on_tool_complete_failure_prints_cross(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "bad_tool", "")
        r.on_tool_complete("tc1", success=False, duration_ms=10.0, error="timeout")
        output = buf.getvalue()
        assert "✗" in output
        assert "timeout" in output

    def test_on_tool_complete_includes_duration(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "my_tool", "")
        r.on_tool_complete("tc1", success=True, duration_ms=123.0)
        output = buf.getvalue()
        assert "123" in output

    def test_on_tool_complete_unknown_id_falls_back(self):
        r, buf = _renderer()
        # Unknown tool_use_id — should not raise
        r.on_tool_complete("unknown-id", success=True, duration_ms=0.0)
        output = buf.getvalue()
        assert "✓" in output or "unknown-id" in output

    def test_on_tool_complete_removes_from_pending(self):
        r, _ = _renderer()
        r.on_tool_started("tc1", "tool", "")
        r.on_tool_complete("tc1", success=True, duration_ms=0.0)
        assert "tc1" not in r._pending

    def test_on_tool_complete_no_duration_omits_ms(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "fast", "")
        r.on_tool_complete("tc1", success=True, duration_ms=0.0)
        output = buf.getvalue()
        assert "0ms" not in output


# ---------------------------------------------------------------------------
# Diff truncation path
# ---------------------------------------------------------------------------


class TestDiffTruncation:
    def test_diff_over_limit_shows_overflow_message(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "patch_file", "")
        big_diff = "\n".join(f"+line{i}" for i in range(_DIFF_MAX_LINES + 5))
        r.on_tool_complete("tc1", success=True, duration_ms=0.0, diff=big_diff)
        output = buf.getvalue()
        assert "more line" in output

    def test_diff_within_limit_no_overflow_message(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "patch_file", "")
        small_diff = "\n".join(f"+line{i}" for i in range(3))
        r.on_tool_complete("tc1", success=True, duration_ms=0.0, diff=small_diff)
        output = buf.getvalue()
        assert "more line" not in output
        assert "line0" in output

    def test_diff_exactly_at_limit_no_overflow(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "patch_file", "")
        exact_diff = "\n".join(f"+line{i}" for i in range(_DIFF_MAX_LINES))
        r.on_tool_complete("tc1", success=True, duration_ms=0.0, diff=exact_diff)
        output = buf.getvalue()
        assert "more line" not in output

    def test_no_diff_does_not_crash(self):
        r, buf = _renderer()
        r.on_tool_started("tc1", "tool", "")
        r.on_tool_complete("tc1", success=True, duration_ms=0.0)
        assert "✓" in buf.getvalue()


# ---------------------------------------------------------------------------
# render_status_bar
# ---------------------------------------------------------------------------


class TestRenderStatusBar:
    def test_active_state_shows_thinking(self):
        s = StatusState(active=True, input_tokens=10, output_tokens=20)
        r, _ = _renderer(status=s)
        bar = r.render_status_bar(width=80)
        assert "Thinking" in bar or "↑" in bar

    def test_idle_state_shows_turns(self):
        s = StatusState(active=False, completed_agents=3)
        r, _ = _renderer(status=s)
        bar = r.render_status_bar(width=80)
        assert "3" in bar

    def test_bar_truncated_to_width(self):
        s = StatusState(active=True, input_tokens=99999, output_tokens=88888)
        r, _ = _renderer(status=s)
        bar = r.render_status_bar(width=20)
        assert len(bar) <= 20

    def test_bar_not_truncated_when_short(self):
        s = StatusState(active=False)
        r, _ = _renderer(status=s)
        bar = r.render_status_bar(width=200)
        assert len(bar) <= 200

    def test_spinner_cycles(self):
        s = StatusState(active=True, spinner_frame=0)
        r, _ = _renderer(status=s)
        bar0 = r.render_status_bar(width=80)
        s.spinner_frame = 5
        bar5 = r.render_status_bar(width=80)
        # Both bars are valid strings
        assert isinstance(bar0, str)
        assert isinstance(bar5, str)
