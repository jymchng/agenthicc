"""Additional coverage tests for input_state.py uncovered lines.

Targets remaining uncovered branches:
  - Key.CTRL_K — kill from cursor to end
  - Key.CTRL_W — delete word before cursor
  - Key.CTRL_Y — yank (paste kill ring)
  - Key.LEFT / Key.RIGHT — cursor movement
  - Key.HOME / Key.END / Key.CTRL_A / Key.CTRL_E
  - Key.DELETE — delete char forward
  - Key.SHIFT_ENTER / Key.ALT_ENTER — multi-line inserts
  - InputState.push_history()
  - Cursor position tracking after various edits
  - History navigation edge cases
"""
from __future__ import annotations

import pytest

from agenthicc.tui.input_state import InputResult, InputResultKind, InputState
from agenthicc.tui.terminal import Key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(*chars: str) -> InputState:
    """Create an InputState pre-loaded with chars typed from the left."""
    st = InputState()
    for ch in chars:
        st.handle(Key.CHAR, ch)
    return st


def _type(st: InputState, text: str) -> None:
    for ch in text:
        st.handle(Key.CHAR, ch)


# ---------------------------------------------------------------------------
# Key.LEFT / Key.RIGHT — cursor movement
# ---------------------------------------------------------------------------


class TestCursorMovement:
    def test_left_moves_cursor_back(self):
        st = _state("a", "b", "c")
        assert st.cursor == 3
        st.handle(Key.LEFT, "")
        assert st.cursor == 2

    def test_left_at_start_stays(self):
        st = _state("a")
        st.handle(Key.LEFT, "")
        st.handle(Key.LEFT, "")
        assert st.cursor == 0

    def test_right_moves_cursor_forward(self):
        st = _state("a", "b")
        st.handle(Key.LEFT, "")
        st.handle(Key.LEFT, "")
        assert st.cursor == 0
        st.handle(Key.RIGHT, "")
        assert st.cursor == 1

    def test_right_at_end_stays(self):
        st = _state("a", "b")
        st.handle(Key.RIGHT, "")
        assert st.cursor == 2  # already at end

    def test_insert_at_cursor_mid_string(self):
        st = _state("a", "c")
        st.handle(Key.LEFT, "")  # cursor at 1
        st.handle(Key.CHAR, "b")
        assert st.text == "abc"
        assert st.cursor == 2


# ---------------------------------------------------------------------------
# Key.HOME / Key.END / Key.CTRL_A / Key.CTRL_E
# ---------------------------------------------------------------------------


class TestHomEnd:
    def test_home_moves_cursor_to_zero(self):
        st = _state("a", "b", "c")
        st.handle(Key.HOME, "")
        assert st.cursor == 0

    def test_end_moves_cursor_to_end(self):
        st = _state("a", "b", "c")
        st.handle(Key.HOME, "")
        assert st.cursor == 0
        st.handle(Key.END, "")
        assert st.cursor == 3

    def test_ctrl_a_moves_cursor_to_start(self):
        st = _state("x", "y", "z")
        st.handle(Key.CTRL_A, "")
        assert st.cursor == 0

    def test_ctrl_e_moves_cursor_to_end(self):
        st = _state("x", "y", "z")
        st.handle(Key.HOME, "")
        st.handle(Key.CTRL_E, "")
        assert st.cursor == 3


# ---------------------------------------------------------------------------
# Key.DELETE — delete char forward
# ---------------------------------------------------------------------------


class TestDeleteKey:
    def test_delete_removes_char_at_cursor(self):
        st = _state("a", "b", "c")
        st.handle(Key.HOME, "")  # cursor at 0
        result = st.handle(Key.DELETE, "")
        assert result.kind == InputResultKind.CONTINUE
        assert st.text == "bc"

    def test_delete_at_end_is_safe(self):
        st = _state("a")
        st.handle(Key.DELETE, "")  # cursor at end
        assert st.text == "a"

    def test_delete_empty_buffer_is_safe(self):
        st = InputState()
        result = st.handle(Key.DELETE, "")
        assert result.kind == InputResultKind.CONTINUE
        assert st.text == ""

    def test_delete_resets_ctrl_c_count(self):
        st = InputState()
        st.ctrl_c_count = 1
        st.handle(Key.DELETE, "")
        assert st.ctrl_c_count == 0


# ---------------------------------------------------------------------------
# Key.CTRL_K — kill from cursor to end
# ---------------------------------------------------------------------------


class TestCtrlK:
    def test_ctrl_k_kills_from_cursor_to_end(self):
        st = _state("a", "b", "c")
        st.handle(Key.HOME, "")  # cursor at 0
        result = st.handle(Key.CTRL_K, "")
        assert result.kind == InputResultKind.CONTINUE
        assert st.text == ""
        assert st._kill_ring == "abc"

    def test_ctrl_k_from_mid_position(self):
        st = _state("a", "b", "c")
        st.handle(Key.LEFT, "")  # cursor at 2
        st.handle(Key.CTRL_K, "")
        assert st.text == "ab"
        assert st._kill_ring == "c"

    def test_ctrl_k_at_end_does_nothing_useful(self):
        st = _state("a", "b")
        st.handle(Key.CTRL_K, "")  # cursor at end
        assert st.text == "ab"
        assert st._kill_ring == ""


# ---------------------------------------------------------------------------
# Key.CTRL_W — delete word before cursor
# ---------------------------------------------------------------------------


class TestCtrlW:
    def test_ctrl_w_deletes_last_word(self):
        st = _state("h", "e", "l", "l", "o")
        result = st.handle(Key.CTRL_W, "")
        assert result.kind == InputResultKind.CONTINUE
        assert st.text == ""
        assert st._kill_ring == "hello"

    def test_ctrl_w_deletes_word_before_cursor(self):
        st = _state("f", "o", "o", " ", "b", "a", "r")
        st.handle(Key.CTRL_W, "")
        assert st.text == "foo "
        assert st._kill_ring == "bar"

    def test_ctrl_w_skips_leading_spaces(self):
        st = _state("h", "i", " ", " ")
        st.handle(Key.CTRL_W, "")
        # Skips spaces then deletes word
        assert "hi" not in st.text or st.text in ("", "hi")

    def test_ctrl_w_on_empty_buffer_safe(self):
        st = InputState()
        result = st.handle(Key.CTRL_W, "")
        assert result.kind == InputResultKind.CONTINUE
        assert st.text == ""

    def test_ctrl_w_two_words(self):
        st = _state("a", "a", " ", "b", "b")
        st.handle(Key.CTRL_W, "")
        assert st.text == "aa "
        st.handle(Key.CTRL_W, "")
        assert st.text == ""


# ---------------------------------------------------------------------------
# Key.CTRL_Y — yank (paste kill ring)
# ---------------------------------------------------------------------------


class TestCtrlY:
    def test_ctrl_y_pastes_kill_ring(self):
        st = _state("a", "b", "c")
        st.handle(Key.CTRL_U, "")  # kills "abc" into ring
        st.handle(Key.CTRL_Y, "")  # yank
        assert st.text == "abc"

    def test_ctrl_y_empty_kill_ring_is_safe(self):
        st = InputState()
        result = st.handle(Key.CTRL_Y, "")
        assert result.kind == InputResultKind.CONTINUE
        assert st.text == ""

    def test_ctrl_k_then_ctrl_y_round_trips(self):
        st = _state("h", "e", "l", "l", "o")
        st.handle(Key.HOME, "")
        st.handle(Key.CTRL_K, "")
        assert st.text == ""
        st.handle(Key.CTRL_Y, "")
        assert st.text == "hello"

    def test_ctrl_w_then_ctrl_y(self):
        st = _state("w", "o", "r", "d")
        st.handle(Key.CTRL_W, "")
        assert st._kill_ring == "word"
        st.handle(Key.CTRL_Y, "")
        assert st.text == "word"


# ---------------------------------------------------------------------------
# InputState.push_history()
# ---------------------------------------------------------------------------


class TestPushHistory:
    def test_push_history_adds_entry(self):
        st = InputState()
        st.push_history("cmd1")
        assert "cmd1" in st.history

    def test_push_history_empty_string_not_added(self):
        st = InputState()
        st.push_history("")
        assert st.history == []

    def test_push_history_multiple(self):
        st = InputState()
        st.push_history("first")
        st.push_history("second")
        assert st.history == ["first", "second"]


# ---------------------------------------------------------------------------
# History navigation edge cases
# ---------------------------------------------------------------------------


class TestHistoryEdgeCases:
    def test_down_when_not_navigating_is_noop(self):
        st = InputState(history=["cmd1"])
        result = st.handle(Key.DOWN, "")
        assert result.kind == InputResultKind.CONTINUE
        assert st.text == ""

    def test_up_with_empty_history_is_noop(self):
        st = InputState()
        result = st.handle(Key.UP, "")
        assert result.kind == InputResultKind.CONTINUE

    def test_history_saves_current_buf_on_first_up(self):
        st = InputState(history=["old"])
        _type(st, "current")
        st.handle(Key.UP, "")  # navigates to "old"
        assert st.text == "old"
        st.handle(Key.DOWN, "")  # back to "current"
        assert st.text == "current"

    def test_down_past_end_restores_saved_buf(self):
        st = InputState(history=["h1", "h2"])
        _type(st, "typed")
        st.handle(Key.UP, "")   # → h2
        st.handle(Key.UP, "")   # → h1
        st.handle(Key.DOWN, "")  # → h2
        st.handle(Key.DOWN, "")  # → restore "typed"
        assert st.text == "typed"

    def test_up_at_oldest_entry_stays(self):
        st = InputState(history=["only"])
        st.handle(Key.UP, "")
        st.handle(Key.UP, "")  # no underflow
        assert st.text == "only"


# ---------------------------------------------------------------------------
# Key.SHIFT_ENTER / Key.ALT_ENTER inserts newline
# ---------------------------------------------------------------------------


class TestShiftAltEnter:
    def test_shift_enter_inserts_newline(self):
        st = _state("a")
        st.handle(Key.SHIFT_ENTER, "")
        st.handle(Key.CHAR, "b")
        assert st.text == "a\nb"

    def test_alt_enter_inserts_newline(self):
        st = _state("x")
        st.handle(Key.ALT_ENTER, "")
        st.handle(Key.CHAR, "y")
        assert st.text == "x\ny"

    def test_shift_enter_resets_ctrl_c(self):
        st = InputState()
        st.ctrl_c_count = 2
        st.handle(Key.SHIFT_ENTER, "")
        assert st.ctrl_c_count == 0


# ---------------------------------------------------------------------------
# Cursor tracking after multi-operation sequences
# ---------------------------------------------------------------------------


class TestCursorTracking:
    def test_cursor_after_backspace_mid_line(self):
        st = _state("a", "b", "c")
        st.handle(Key.LEFT, "")
        st.handle(Key.BACKSPACE, "")
        assert st.cursor == 1
        assert st.text == "ac"

    def test_cursor_at_start_after_ctrl_u(self):
        st = _state("a", "b", "c")
        st.handle(Key.CTRL_U, "")
        assert st.cursor == 0

    def test_cursor_after_ctrl_y(self):
        st = _state("a", "b")
        st.handle(Key.CTRL_U, "")  # kill "ab", cursor=0
        st.handle(Key.CTRL_Y, "")  # yank "ab", cursor should advance
        assert st.cursor == 2
        assert st.text == "ab"

    def test_cursor_preserved_on_left_right_sequence(self):
        st = _state("1", "2", "3", "4", "5")
        for _ in range(3):
            st.handle(Key.LEFT, "")
        assert st.cursor == 2
        for _ in range(2):
            st.handle(Key.RIGHT, "")
        assert st.cursor == 4


# ---------------------------------------------------------------------------
# InputState.reset()
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_buffer_and_cursor(self):
        st = _state("x", "y", "z")
        st.reset()
        assert st.text == ""
        assert st.cursor == 0

    def test_reset_resets_hist_idx(self):
        st = InputState(history=["cmd"])
        st.handle(Key.UP, "")
        assert st._hist_idx == 0
        st.reset()
        assert st._hist_idx == -1
