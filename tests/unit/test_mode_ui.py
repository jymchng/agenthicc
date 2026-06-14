"""Unit tests for the mode-aware UI components.

Covers:
- Key.SHIFT_TAB exists and has value "SHIFT_TAB"
- _redraw called with mode_line=None returns 0
- _redraw called with mode_line="some text" returns 1
- _redraw with mode_line + non-empty matches returns 1 + min(8, len(matches))
- mode_line text appears in captured stdout
"""
from __future__ import annotations

import pytest

from agenthicc.tui.mention_input import Key, _redraw
from agenthicc.tui.trigger import MatchItem

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Key enum
# ---------------------------------------------------------------------------


def test_shift_tab_exists():
    assert hasattr(Key, "SHIFT_TAB")


def test_shift_tab_value():
    assert Key.SHIFT_TAB.value == "SHIFT_TAB"


def test_shift_tab_is_key_member():
    assert Key.SHIFT_TAB in list(Key)


def test_shift_tab_string_representation():
    # Key extends str, so comparing against its value works
    assert Key.SHIFT_TAB == "SHIFT_TAB"


# ---------------------------------------------------------------------------
# _redraw helpers
# ---------------------------------------------------------------------------


def _items(n: int) -> list[MatchItem]:
    """Return a list of *n* MatchItem instances."""
    return [MatchItem(display=f"item{i}", value=f"val{i}") for i in range(n)]


# ---------------------------------------------------------------------------
# _redraw with mode_line=None
# ---------------------------------------------------------------------------


def test_redraw_mode_line_none_returns_zero(capsys):
    result = _redraw(
        prompt_str="> ",
        buf=[],
        fragment="",
        matches=[],
        selected=0,
        prev_n_lines=0,
        in_trigger=False,
        mode_line=None,
    )
    assert result == 0


def test_redraw_mode_line_none_no_extra_output(capsys):
    """With mode_line=None there is no mode footer line in the output."""
    _redraw(
        prompt_str="> ",
        buf=list("hello"),
        fragment="",
        matches=[],
        selected=0,
        prev_n_lines=0,
        in_trigger=False,
        mode_line=None,
    )
    out = capsys.readouterr().out
    # The mode footer is never written
    assert "mode" not in out.lower()


# ---------------------------------------------------------------------------
# _redraw with mode_line="some text"
# ---------------------------------------------------------------------------


def test_redraw_mode_line_text_returns_two(capsys):
    # mode_line now produces 2 rows: the border rule + the text line.
    result = _redraw(
        prompt_str="> ",
        buf=[],
        fragment="",
        matches=[],
        selected=0,
        prev_n_lines=0,
        in_trigger=False,
        mode_line="some text",
    )
    assert result == 2


def test_redraw_mode_line_empty_string_returns_two(capsys):
    """An empty-string mode_line still produces 2 extra lines (border + text)."""
    result = _redraw(
        prompt_str="> ",
        buf=[],
        fragment="",
        matches=[],
        selected=0,
        prev_n_lines=0,
        in_trigger=False,
        mode_line="",
    )
    assert result == 2


def test_redraw_mode_line_text_appears_in_stdout(capsys):
    _redraw(
        prompt_str="> ",
        buf=list("hi"),
        fragment="",
        matches=[],
        selected=0,
        prev_n_lines=0,
        in_trigger=False,
        mode_line="some text",
    )
    out = capsys.readouterr().out
    assert "some text" in out


def test_redraw_mode_line_ansi_text_appears(capsys):
    """Mode line text with ANSI codes still appears in raw stdout."""
    mode_text = "\x1b[32m[AUTO]\x1b[0m"
    _redraw(
        prompt_str="> ",
        buf=[],
        fragment="",
        matches=[],
        selected=0,
        prev_n_lines=0,
        in_trigger=False,
        mode_line=mode_text,
    )
    out = capsys.readouterr().out
    assert "AUTO" in out


# ---------------------------------------------------------------------------
# _redraw with mode_line + non-empty matches → 1 + min(8, len(matches))
# ---------------------------------------------------------------------------


def test_redraw_mode_line_with_one_match(capsys):
    result = _redraw(
        prompt_str="> ",
        buf=[],
        fragment="f",
        matches=_items(1),
        selected=0,
        prev_n_lines=0,
        in_trigger=True,
        mode_line="some text",
    )
    assert result == 2 + min(8, 1)  # == 3


def test_redraw_mode_line_with_three_matches(capsys):
    result = _redraw(
        prompt_str="> ",
        buf=[],
        fragment="f",
        matches=_items(3),
        selected=0,
        prev_n_lines=0,
        in_trigger=True,
        mode_line="some text",
    )
    assert result == 2 + min(8, 3)  # == 5


def test_redraw_mode_line_with_eight_matches(capsys):
    result = _redraw(
        prompt_str="> ",
        buf=[],
        fragment="f",
        matches=_items(8),
        selected=0,
        prev_n_lines=0,
        in_trigger=True,
        mode_line="some text",
    )
    assert result == 2 + min(8, 8)  # == 10


def test_redraw_mode_line_with_five_matches_formula(capsys):
    """Return value equals 2 + min(8, len(matches)) for any n <= 8 (border counts as +1)."""
    for n in range(1, 9):
        result = _redraw(
            prompt_str="> ",
            buf=[],
            fragment="x",
            matches=_items(n),
            selected=0,
            prev_n_lines=0,
            in_trigger=True,
            mode_line="mode",
        )
        assert result == 2 + min(8, n), f"n={n}: expected {2 + min(8, n)}, got {result}"
        capsys.readouterr()  # drain captured output between iterations


def test_redraw_mode_line_with_matches_shows_items_in_stdout(capsys):
    """Dropdown items appear in stdout when mode_line + matches are provided."""
    matches = _items(2)
    _redraw(
        prompt_str="> ",
        buf=[],
        fragment="f",
        matches=matches,
        selected=0,
        prev_n_lines=0,
        in_trigger=True,
        mode_line="test mode",
    )
    out = capsys.readouterr().out
    assert "item0" in out
    assert "item1" in out


def test_redraw_no_mode_line_with_matches_returns_min_8(capsys):
    """Without mode_line, return value is min(8, len(matches)) for n <= 8."""
    for n in range(1, 9):
        result = _redraw(
            prompt_str="> ",
            buf=[],
            fragment="x",
            matches=_items(n),
            selected=0,
            prev_n_lines=0,
            in_trigger=True,
            mode_line=None,
        )
        assert result == min(8, n), f"n={n}: expected {min(8, n)}, got {result}"
        capsys.readouterr()


# ---------------------------------------------------------------------------
# Additional Key enum coverage
# ---------------------------------------------------------------------------


def test_key_enum_has_expected_members():
    expected = {"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "TAB", "ESC",
                "BACKSPACE", "CTRL_C", "CTRL_D", "CTRL_U", "SHIFT_TAB",
                "AT", "CHAR"}
    actual = {k.name for k in Key}
    assert expected.issubset(actual)


def test_key_tab_value():
    assert Key.TAB.value == "TAB"


def test_key_enter_value():
    assert Key.ENTER.value == "ENTER"


def test_key_ctrl_c_value():
    assert Key.CTRL_C.value == "CTRL_C"
