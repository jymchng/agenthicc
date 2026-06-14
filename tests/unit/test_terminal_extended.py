"""Extended unit tests for agenthicc.tui.terminal — covers uncovered paths."""
from __future__ import annotations

import io
import os
import re
import shutil

import pytest

pytestmark = pytest.mark.unit

from agenthicc.tui.terminal import (
    FakeTerminal,
    Key,
    Size,
    Terminal,
    _clip_to_cols,
    _decode_escape,
    _display_width,
    _strip_ansi,
    _utf8_continuation_bytes,
    truncate_to_cols,
)


# ---------------------------------------------------------------------------
# _strip_ansi helper
# ---------------------------------------------------------------------------


class TestStripAnsiHelper:
    def test_removes_sgr_sequence(self) -> None:
        assert _strip_ansi("\x1b[32mhello\x1b[0m") == "hello"

    def test_plain_text_unchanged(self) -> None:
        assert _strip_ansi("plain") == "plain"

    def test_empty_string(self) -> None:
        assert _strip_ansi("") == ""

    def test_only_escape(self) -> None:
        assert _strip_ansi("\x1b[0m") == ""

    def test_strips_256_color(self) -> None:
        assert _strip_ansi("\x1b[38;5;200mtext\x1b[0m") == "text"

    def test_strips_osc_title(self) -> None:
        # OSC: ESC ] ... BEL
        result = _strip_ansi("\x1b]0;title\x07hello")
        assert result == "hello"

    def test_multiple_codes_in_sequence(self) -> None:
        result = _strip_ansi("\x1b[1m\x1b[4m\x1b[32mbold-underline-green\x1b[0m")
        assert "\x1b" not in result
        assert "bold-underline-green" in result


# ---------------------------------------------------------------------------
# _display_width helper
# ---------------------------------------------------------------------------


class TestDisplayWidth:
    def test_plain_ascii(self) -> None:
        assert _display_width("hello") == 5

    def test_empty(self) -> None:
        assert _display_width("") == 0

    def test_with_ansi(self) -> None:
        # ANSI codes should not contribute to width
        assert _display_width("\x1b[32mhello\x1b[0m") == 5

    def test_unicode_ascii_range(self) -> None:
        assert _display_width("abc") == 3


# ---------------------------------------------------------------------------
# _clip_to_cols helper
# ---------------------------------------------------------------------------


class TestClipToCols:
    def test_short_text_unchanged(self) -> None:
        assert _clip_to_cols("hi", 10) == "hi"

    def test_exact_width(self) -> None:
        assert _clip_to_cols("hello", 5) == "hello"

    def test_clips_long(self) -> None:
        result = _clip_to_cols("x" * 20, 10)
        assert _strip_ansi(result) == "x" * 10

    def test_zero_cols(self) -> None:
        assert _clip_to_cols("hello", 0) == ""

    def test_negative_cols(self) -> None:
        assert _clip_to_cols("hello", -1) == ""

    def test_preserves_ansi_within_limit(self) -> None:
        coloured = "\x1b[32m" + "abc" + "\x1b[0m"
        result = _clip_to_cols(coloured, 5)
        # text fits — original returned
        assert "abc" in result

    def test_clips_with_ansi(self) -> None:
        coloured = "\x1b[32m" + "abcdefgh" + "\x1b[0m"
        result = _clip_to_cols(coloured, 4)
        assert len(_strip_ansi(result)) <= 4

    def test_empty_string(self) -> None:
        assert _clip_to_cols("", 10) == ""


# ---------------------------------------------------------------------------
# truncate_to_cols
# ---------------------------------------------------------------------------


class TestTruncateToCols:
    def test_always_ends_with_reset(self) -> None:
        assert truncate_to_cols("hello", 10).endswith("\x1b[0m")

    def test_zero_width(self) -> None:
        assert truncate_to_cols("hello", 0) == "\x1b[0m"

    def test_visible_chars_clipped(self) -> None:
        result = truncate_to_cols("x" * 20, 5)
        visible = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result)
        assert len(visible) <= 5

    def test_fits_all_preserved(self) -> None:
        result = truncate_to_cols("hi", 10)
        visible = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result)
        assert visible == "hi"

    def test_unicode_multibyte_clipped(self) -> None:
        # Each ASCII char is 1 col
        result = truncate_to_cols("abcde", 3)
        visible = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result)
        assert len(visible) <= 3

    def test_empty_string(self) -> None:
        result = truncate_to_cols("", 10)
        assert result == "\x1b[0m"


# ---------------------------------------------------------------------------
# Terminal._erase_bottom with various heights
# ---------------------------------------------------------------------------


class TestTerminalEraseBottom:
    def _make_terminal(self, bottom_height: int = 0) -> Terminal:
        t = Terminal(out=io.StringIO())
        t._bottom_height = bottom_height
        return t

    def test_erase_bottom_zero_returns_empty(self) -> None:
        t = self._make_terminal(0)
        assert t._erase_bottom() == ""

    def test_erase_bottom_one_no_up(self) -> None:
        # height=1: cursor already on the only row; no CUU needed
        t = self._make_terminal(1)
        seq = t._erase_bottom()
        assert "\x1b[" + "1A" not in seq  # no move-up for height 1
        assert "\r\x1b[0J" in seq

    def test_erase_bottom_two_moves_up_once(self) -> None:
        t = self._make_terminal(2)
        seq = t._erase_bottom()
        assert "\x1b[1A" in seq
        assert "\r\x1b[0J" in seq

    def test_erase_bottom_five_moves_up_four(self) -> None:
        t = self._make_terminal(5)
        seq = t._erase_bottom()
        assert "\x1b[4A" in seq
        assert "\r\x1b[0J" in seq

    def test_erase_bottom_large(self) -> None:
        t = self._make_terminal(20)
        seq = t._erase_bottom()
        assert "\x1b[19A" in seq


# ---------------------------------------------------------------------------
# Terminal.commit_lines empty list is a no-op
# ---------------------------------------------------------------------------


class TestTerminalCommitLines:
    def _make_terminal(self) -> tuple[Terminal, io.StringIO]:
        buf = io.StringIO()
        t = Terminal(out=buf)
        # Avoid real signal setup issues — reset bottom height
        t._bottom_height = 0
        return t, buf

    def test_commit_empty_list_no_write(self) -> None:
        t, buf = self._make_terminal()
        t.commit_lines([])
        assert buf.getvalue() == ""

    def test_commit_lines_writes_to_out(self) -> None:
        t, buf = self._make_terminal()
        t.commit_lines(["hello"])
        written = buf.getvalue()
        assert "hello" in written

    def test_commit_lines_resets_bottom_height(self) -> None:
        t, buf = self._make_terminal()
        t._bottom_height = 3
        t.commit_lines(["a line"])
        assert t._bottom_height == 0

    def test_commit_lines_multiple(self) -> None:
        t, buf = self._make_terminal()
        t.commit_lines(["line1", "line2", "line3"])
        written = buf.getvalue()
        assert "line1" in written
        assert "line2" in written
        assert "line3" in written


# ---------------------------------------------------------------------------
# Terminal.set_bottom with empty list delegates to clear_bottom
# ---------------------------------------------------------------------------


class TestTerminalSetBottom:
    def _make_terminal(self) -> tuple[Terminal, io.StringIO]:
        buf = io.StringIO()
        t = Terminal(out=buf)
        t._bottom_height = 0
        return t, buf

    def test_set_bottom_empty_calls_clear_bottom(self) -> None:
        t, buf = self._make_terminal()
        t._bottom_height = 2
        t.set_bottom([])
        # After setting empty, height resets to 0
        assert t._bottom_height == 0

    def test_set_bottom_single_row(self) -> None:
        t, buf = self._make_terminal()
        t.set_bottom(["hello"])
        assert t._bottom_height == 1

    def test_set_bottom_multiple_rows(self) -> None:
        t, buf = self._make_terminal()
        t.set_bottom(["row1", "row2", "row3"])
        assert t._bottom_height == 3

    def test_set_bottom_writes_to_out(self) -> None:
        t, buf = self._make_terminal()
        t.set_bottom(["status line"])
        written = buf.getvalue()
        assert "status line" in written


# ---------------------------------------------------------------------------
# Terminal.clear_bottom
# ---------------------------------------------------------------------------


class TestTerminalClearBottom:
    def _make_terminal(self) -> tuple[Terminal, io.StringIO]:
        buf = io.StringIO()
        t = Terminal(out=buf)
        t._bottom_height = 0
        return t, buf

    def test_clear_bottom_when_empty_is_noop(self) -> None:
        t, buf = self._make_terminal()
        t._bottom_height = 0
        t.clear_bottom()
        assert buf.getvalue() == ""

    def test_clear_bottom_resets_height(self) -> None:
        t, buf = self._make_terminal()
        t._bottom_height = 3
        t.clear_bottom()
        assert t._bottom_height == 0

    def test_clear_bottom_writes_erase_sequence(self) -> None:
        t, buf = self._make_terminal()
        t._bottom_height = 2
        t.clear_bottom()
        written = buf.getvalue()
        assert "\r\x1b[0J" in written


# ---------------------------------------------------------------------------
# Terminal._query_size — correct row/col mapping
# ---------------------------------------------------------------------------


class TestTerminalQuerySize:
    def test_rows_from_lines_cols_from_columns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # os.terminal_size(columns=120, lines=40) — columns is FIRST
        fake_size = os.terminal_size((120, 40))
        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: fake_size)
        buf = io.StringIO()
        t = Terminal(out=buf)
        s = t._query_size()
        assert s.rows == 40
        assert s.cols == 120

    def test_fallback_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With no terminal, fallback=(80,24) gives columns=80, lines=24
        fake_size = os.terminal_size((80, 24))
        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: fake_size)
        buf = io.StringIO()
        t = Terminal(out=buf)
        s = t._query_size()
        assert s.rows == 24
        assert s.cols == 80


# ---------------------------------------------------------------------------
# FakeTerminal — additional coverage
# ---------------------------------------------------------------------------


class TestFakeTerminalExtended:
    def test_commit_empty_no_write_calls(self) -> None:
        t = FakeTerminal()
        t.commit_lines([])
        assert t.write_calls == 0

    def test_commit_non_empty_increments_write_calls(self) -> None:
        t = FakeTerminal()
        t.commit_lines(["line"])
        assert t.write_calls == 1

    def test_commit_multiple_lines_single_write_call(self) -> None:
        t = FakeTerminal()
        t.commit_lines(["a", "b", "c"])
        assert t.write_calls == 1

    def test_cleared_count_increments_on_clear_bottom(self) -> None:
        t = FakeTerminal()
        t.clear_bottom()
        assert t.cleared_count == 1
        t.clear_bottom()
        assert t.cleared_count == 2

    def test_cleared_count_starts_at_zero(self) -> None:
        t = FakeTerminal()
        assert t.cleared_count == 0

    def test_set_bottom_empty_list(self) -> None:
        t = FakeTerminal()
        # set_bottom with empty list should result in empty bottom
        # FakeTerminal.set_bottom does NOT delegate to clear_bottom
        # it clips an empty list, bottom stays empty
        t.set_bottom([])
        assert t.bottom == []

    def test_set_bottom_empty_list_still_records_write(self) -> None:
        # set_bottom always increments write_calls even for empty rows in FakeTerminal
        t = FakeTerminal()
        t.set_bottom([])
        assert t.write_calls == 1

    def test_set_bottom_stores_history(self) -> None:
        t = FakeTerminal()
        t.set_bottom(["a"])
        t.set_bottom(["b", "c"])
        assert len(t.bottom_history) == 2

    def test_bottom_history_records_each_call(self) -> None:
        t = FakeTerminal()
        t.set_bottom(["row1"])
        t.set_bottom(["row2"])
        visible_0 = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", t.bottom_history[0][0])
        visible_1 = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", t.bottom_history[1][0])
        assert visible_0 == "row1"
        assert visible_1 == "row2"

    def test_enter_returns_self(self) -> None:
        t = FakeTerminal()
        with t as ctx:
            assert ctx is t

    def test_exit_is_noop(self) -> None:
        t = FakeTerminal()
        t.set_bottom(["something"])
        with t:
            pass
        # bottom should still be set after exit (no teardown called)
        assert len(t.bottom) == 1

    def test_on_resize_marks_dirty(self) -> None:
        t = FakeTerminal()
        assert not t._size_dirty
        t.on_resize()
        assert t._size_dirty

    def test_size_access_clears_dirty(self) -> None:
        t = FakeTerminal()
        t.on_resize()
        _ = t.size
        assert not t._size_dirty

    def test_size_returns_fixed(self) -> None:
        t = FakeTerminal(rows=30, cols=100)
        assert t.size.rows == 30
        assert t.size.cols == 100

    def test_read_key_raises(self) -> None:
        t = FakeTerminal()
        with pytest.raises(NotImplementedError):
            t.read_key()

    def test_teardown_clears_bottom(self) -> None:
        t = FakeTerminal()
        t.set_bottom(["row"])
        t.teardown()
        assert t.bottom == []

    def test_teardown_increments_cleared_count(self) -> None:
        t = FakeTerminal()
        t.set_bottom(["row"])
        before = t.cleared_count
        t.teardown()
        assert t.cleared_count == before + 1


# ---------------------------------------------------------------------------
# Key enum — all required string values match their names
# ---------------------------------------------------------------------------


class TestKeyEnum:
    def test_up_value(self) -> None:
        assert Key.UP == "UP"
        assert Key.UP.value == "UP"

    def test_down_value(self) -> None:
        assert Key.DOWN == "DOWN"

    def test_left_value(self) -> None:
        assert Key.LEFT == "LEFT"

    def test_right_value(self) -> None:
        assert Key.RIGHT == "RIGHT"

    def test_enter_value(self) -> None:
        assert Key.ENTER == "ENTER"

    def test_tab_value(self) -> None:
        assert Key.TAB == "TAB"

    def test_shift_tab_value(self) -> None:
        assert Key.SHIFT_TAB == "SHIFT_TAB"

    def test_esc_value(self) -> None:
        assert Key.ESC == "ESC"

    def test_backspace_value(self) -> None:
        assert Key.BACKSPACE == "BACKSPACE"

    def test_ctrl_c_value(self) -> None:
        assert Key.CTRL_C == "CTRL_C"

    def test_ctrl_d_value(self) -> None:
        assert Key.CTRL_D == "CTRL_D"

    def test_ctrl_u_value(self) -> None:
        assert Key.CTRL_U == "CTRL_U"

    def test_newline_value(self) -> None:
        assert Key.NEWLINE == "NEWLINE"

    def test_at_value(self) -> None:
        assert Key.AT == "AT"

    def test_char_value(self) -> None:
        assert Key.CHAR == "CHAR"

    def test_extended_ctrl_a(self) -> None:
        assert Key.CTRL_A == "CTRL_A"  # type: ignore[attr-defined]

    def test_extended_ctrl_e(self) -> None:
        assert Key.CTRL_E == "CTRL_E"  # type: ignore[attr-defined]

    def test_extended_home(self) -> None:
        assert Key.HOME == "HOME"  # type: ignore[attr-defined]

    def test_extended_end(self) -> None:
        assert Key.END == "END"  # type: ignore[attr-defined]

    def test_extended_unknown(self) -> None:
        assert Key.UNKNOWN == "UNKNOWN"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _decode_escape
# ---------------------------------------------------------------------------


class TestDecodeEscape:
    def test_up_arrow(self) -> None:
        result = _decode_escape(b'\x1b[A')
        assert result == (Key.UP, '')

    def test_down_arrow(self) -> None:
        result = _decode_escape(b'\x1b[B')
        assert result == (Key.DOWN, '')

    def test_right_arrow(self) -> None:
        result = _decode_escape(b'\x1b[C')
        assert result == (Key.RIGHT, '')

    def test_left_arrow(self) -> None:
        result = _decode_escape(b'\x1b[D')
        assert result == (Key.LEFT, '')

    def test_shift_tab(self) -> None:
        result = _decode_escape(b'\x1b[Z')
        assert result == (Key.SHIFT_TAB, '')

    def test_oa_up(self) -> None:
        result = _decode_escape(b'\x1bOA')
        assert result == (Key.UP, '')

    def test_ob_down(self) -> None:
        result = _decode_escape(b'\x1bOB')
        assert result == (Key.DOWN, '')

    def test_alt_enter_cr(self) -> None:
        result = _decode_escape(b'\x1b\r')
        assert result == (Key.NEWLINE, '')

    def test_alt_enter_lf(self) -> None:
        result = _decode_escape(b'\x1b\n')
        assert result == (Key.NEWLINE, '')

    def test_lone_esc_returns_esc(self) -> None:
        # Single byte ESC sequence (len==1) returns Key.ESC
        result = _decode_escape(b'\x1b')
        assert result == (Key.ESC, '')

    def test_unknown_two_byte_returns_none(self) -> None:
        result = _decode_escape(b'\x1bX')
        assert result is None

    def test_unknown_three_byte_returns_none(self) -> None:
        result = _decode_escape(b'\x1b[Z' + b'x')
        # 4 bytes, not in table -> None
        assert result is None


# ---------------------------------------------------------------------------
# _utf8_continuation_bytes
# ---------------------------------------------------------------------------


class TestUtf8ContinuationBytes:
    def test_two_byte_sequence(self) -> None:
        # 0b11000010 = start of 2-byte UTF-8 sequence (U+0080..U+07FF)
        assert _utf8_continuation_bytes(0b11000010) == 1

    def test_three_byte_sequence(self) -> None:
        # 0b11100010 = start of 3-byte UTF-8 sequence (U+0800..U+FFFF)
        assert _utf8_continuation_bytes(0b11100010) == 2

    def test_four_byte_sequence(self) -> None:
        # 0b11110000 = start of 4-byte UTF-8 sequence (U+10000..)
        assert _utf8_continuation_bytes(0b11110000) == 3

    def test_ascii_byte_returns_zero(self) -> None:
        # ASCII byte (0xxxxxxx) — not a multi-byte starter
        assert _utf8_continuation_bytes(0x61) == 0  # 'a'

    def test_continuation_byte_returns_zero(self) -> None:
        # 0b10xxxxxx is a continuation byte, not a starter
        assert _utf8_continuation_bytes(0b10000000) == 0

    def test_0xC0_returns_1(self) -> None:
        assert _utf8_continuation_bytes(0xC0) == 1

    def test_0xE0_returns_2(self) -> None:
        assert _utf8_continuation_bytes(0xE0) == 2

    def test_0xF0_returns_3(self) -> None:
        assert _utf8_continuation_bytes(0xF0) == 3


# ---------------------------------------------------------------------------
# FakeTerminal._erase_bottom is always empty string
# ---------------------------------------------------------------------------


class TestFakeTerminalEraseBottom:
    def test_erase_bottom_always_empty(self) -> None:
        t = FakeTerminal()
        t._bottom_height = 5
        assert t._erase_bottom() == ''

    def test_erase_bottom_zero_height(self) -> None:
        t = FakeTerminal()
        t._bottom_height = 0
        assert t._erase_bottom() == ''


# ---------------------------------------------------------------------------
# Real Terminal.on_resize (non-TTY path)
# ---------------------------------------------------------------------------


class TestRealTerminalOnResize:
    def test_on_resize_clears_dirty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_size = os.terminal_size((80, 24))
        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: fake_size)
        t = Terminal(out=io.StringIO())
        t._size_dirty = True
        t.on_resize()
        assert not t._size_dirty

    def test_on_resize_updates_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = [0]

        def fake_get_size(*a, **kw):
            call_count[0] += 1
            return os.terminal_size((100 + call_count[0], 30))

        monkeypatch.setattr(shutil, "get_terminal_size", fake_get_size)
        t = Terminal(out=io.StringIO())
        # Reset call count since Terminal.__init__ calls _query_size once
        initial_count = call_count[0]
        t.on_resize()
        # Should have called _query_size again
        assert call_count[0] > initial_count


# ---------------------------------------------------------------------------
# Real Terminal.size property with dirty flag
# ---------------------------------------------------------------------------


class TestRealTerminalSizeProperty:
    def test_size_dirty_triggers_requery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = [0]

        def fake_get_size(*a, **kw):
            call_count[0] += 1
            return os.terminal_size((80, 24))

        monkeypatch.setattr(shutil, "get_terminal_size", fake_get_size)
        t = Terminal(out=io.StringIO())
        count_after_init = call_count[0]
        t._size_dirty = True
        _ = t.size  # should trigger requery
        assert call_count[0] > count_after_init
        assert not t._size_dirty

    def test_size_not_dirty_no_requery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = [0]

        def fake_get_size(*a, **kw):
            call_count[0] += 1
            return os.terminal_size((80, 24))

        monkeypatch.setattr(shutil, "get_terminal_size", fake_get_size)
        t = Terminal(out=io.StringIO())
        count_after_init = call_count[0]
        t._size_dirty = False
        _ = t.size
        assert call_count[0] == count_after_init
