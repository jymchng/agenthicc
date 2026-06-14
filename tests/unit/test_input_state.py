"""Unit tests for InputState — pure state-machine tests (no TTY required).

pytestmark = pytest.mark.unit
"""
from __future__ import annotations

import pytest

from agenthicc.tui.input_state import InputResult, InputResultKind, InputState
from agenthicc.tui.terminal import Key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def _char(c: str) -> tuple[Key, str]:
    return (Key.CHAR, c)


def _enter() -> tuple[Key, str]:
    return (Key.ENTER, "")


def _bs() -> tuple[Key, str]:
    return (Key.BACKSPACE, "")


def _ctrl_c() -> tuple[Key, str]:
    return (Key.CTRL_C, "")


def _ctrl_d() -> tuple[Key, str]:
    return (Key.CTRL_D, "")


def _ctrl_u() -> tuple[Key, str]:
    return (Key.CTRL_U, "")


def _up() -> tuple[Key, str]:
    return (Key.UP, "")


def _down() -> tuple[Key, str]:
    return (Key.DOWN, "")


def _newline() -> tuple[Key, str]:
    return (Key.NEWLINE, "")


def _drive(
    keys: list[tuple[Key, str]],
    history: list[str] | None = None,
) -> tuple[InputResult, InputState]:
    """Drive an InputState through a sequence of key events.

    Stops early when SUBMIT or EXIT is returned.

    Returns:
        The final InputResult and the InputState at that point.
    """
    st = InputState(history=history if history is not None else [])
    result = InputResult.continue_()
    for key, ch in keys:
        result = st.handle(key, ch)
        if result.kind in (InputResultKind.SUBMIT, InputResultKind.EXIT):
            break
    return result, st


# ---------------------------------------------------------------------------
# Basic editing
# ---------------------------------------------------------------------------

def test_simple_submit() -> None:
    result, st = _drive([_char("h"), _char("i"), _enter()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "hi"


def test_empty_enter_submits_empty() -> None:
    result, st = _drive([_enter()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == ""


def test_backspace_removes_char() -> None:
    result, st = _drive([_char("a"), _char("b"), _bs(), _enter()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "a"


def test_backspace_on_empty_is_safe() -> None:
    result, st = _drive([_bs(), _bs(), _enter()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == ""


def test_ctrl_u_clears() -> None:
    result, st = _drive([_char("a"), _char("b"), _char("c"), _ctrl_u(), _enter()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == ""


# ---------------------------------------------------------------------------
# Ctrl+C / Ctrl+D exit paths
# ---------------------------------------------------------------------------

def test_ctrl_c_once_clears_text() -> None:
    """First Ctrl+C clears the buffer but does NOT exit."""
    result, st = _drive([_char("a"), _char("b"), _ctrl_c()])
    assert result.kind == InputResultKind.CONTINUE
    assert st.text == ""
    assert st.ctrl_c_count == 1


def test_ctrl_c_twice_exits() -> None:
    result, st = _drive([_ctrl_c(), _ctrl_c()])
    assert result.kind == InputResultKind.EXIT


def test_ctrl_c_resets_on_other_key() -> None:
    """A non-Ctrl+C keypress resets the counter, so two non-consecutive
    Ctrl+C presses do not trigger exit."""
    result, st = _drive([_ctrl_c(), _char("x"), _ctrl_c()])
    # Second Ctrl+C after typing 'x' should count as first again.
    assert result.kind == InputResultKind.CONTINUE
    assert st.ctrl_c_count == 1


def test_ctrl_d_empty_exits() -> None:
    result, st = _drive([_ctrl_d()])
    assert result.kind == InputResultKind.EXIT


def test_ctrl_d_nonempty_submits() -> None:
    result, st = _drive([_char("h"), _char("i"), _ctrl_d()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "hi"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def test_history_saves_on_submit() -> None:
    result, st = _drive([_char("h"), _char("i"), _enter()])
    assert "hi" in st.history


def test_empty_submit_not_saved_to_history() -> None:
    result, st = _drive([_enter()])
    # Empty strings should not be appended to history.
    assert st.history == []


def test_history_up_down() -> None:
    history = ["first", "second"]
    # Type "current" then navigate up twice, then down once.
    keys = [
        _char("c"), _char("u"), _char("r"),  # "cur"
        _up(),   # → "second"
        _up(),   # → "first"
        _down(), # → "second"
        _enter(),
    ]
    result, st = _drive(keys, history=history)
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "second"


def test_history_down_past_end_restores_saved() -> None:
    """Pressing DOWN past the last history entry restores the original buffer."""
    history = ["old"]
    keys = [
        _char("n"), _char("e"), _char("w"),  # "new"
        _up(),    # → "old"
        _down(),  # → restored "new"
        _enter(),
    ]
    result, st = _drive(keys, history=history)
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "new"


def test_history_up_at_top_stays() -> None:
    """Pressing UP when already at the oldest entry does not underflow."""
    history = ["only"]
    keys = [_up(), _up(), _up(), _enter()]
    result, st = _drive(keys, history=history)
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "only"


# ---------------------------------------------------------------------------
# Multi-line input
# ---------------------------------------------------------------------------

def test_alt_enter_inserts_newline() -> None:
    """Key.NEWLINE (Alt+Enter) inserts a literal newline without submitting."""
    result, st = _drive([_char("a"), _newline(), _char("b"), _enter()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "a\nb"


def test_backslash_enter_inserts_newline() -> None:
    """A trailing backslash followed by ENTER continues the line."""
    result, st = _drive([_char("a"), _char("\\"), _enter(), _char("b"), _enter()])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "a\nb"


def test_multiline_submit() -> None:
    """Multi-line text is submitted as a single string with embedded newlines."""
    result, st = _drive([
        _char("l"), _char("i"), _char("n"), _char("e"), _char("1"),
        _newline(),
        _char("l"), _char("i"), _char("n"), _char("e"), _char("2"),
        _enter(),
    ])
    assert result.kind == InputResultKind.SUBMIT
    assert result.text == "line1\nline2"


# ---------------------------------------------------------------------------
# Shift+Tab
# ---------------------------------------------------------------------------

def test_shift_tab_returns_continue() -> None:
    """Shift+Tab returns CONTINUE; actual mode cycling is the caller's job."""
    st = InputState()
    result = st.handle(Key.SHIFT_TAB, "")
    assert result.kind == InputResultKind.CONTINUE


# ---------------------------------------------------------------------------
# InputResult factory methods
# ---------------------------------------------------------------------------

def test_result_continue_factory() -> None:
    r = InputResult.continue_()
    assert r.kind == InputResultKind.CONTINUE
    assert r.text == ""


def test_result_submit_factory() -> None:
    r = InputResult.submit("hello")
    assert r.kind == InputResultKind.SUBMIT
    assert r.text == "hello"


def test_result_exit_factory() -> None:
    r = InputResult.exit_()
    assert r.kind == InputResultKind.EXIT
    assert r.text == ""
