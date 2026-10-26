"""Unit tests: the composer's folded, virtualised rows (PRD-84, AGX-11)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agenthicc.tui.input.renderer import CURSOR_CHAR, PROMPT_CHAR
from agenthicc.tui.workspace.components import ComposerComponent, _line_rows
from agenthicc.tui.workspace.live_region import lines_text

pytestmark = pytest.mark.unit


def _make_comp(
    buf: list[str],
    cursor: int,
    paste_condensed: bool = False,
    paste_label: str = "",
    *,
    max_rows: int = 12,
) -> ComposerComponent:
    app_state = MagicMock()
    inp = app_state.input
    inp.buf.return_value = buf
    inp.cursor.return_value = cursor
    inp.paste_condensed.return_value = paste_condensed
    inp.paste_label.return_value = paste_label
    return ComposerComponent(app_state, max_rows=max_rows)


def _rows(comp: ComposerComponent, cols: int = 80) -> list[str]:
    return lines_text(comp.lines(cols))


class TestLogicalLines:
    def test_one_line_is_one_row(self) -> None:
        assert _rows(_make_comp(list("hello"), 5)) == [f"{PROMPT_CHAR} hello{CURSOR_CHAR}"]

    def test_each_logical_line_is_a_row(self) -> None:
        buf = list("a") + ["\n"] + list("b") + ["\n"] + list("c")
        assert len(_rows(_make_comp(buf, 0))) == 3

    def test_first_row_has_the_prompt_and_the_rest_are_indented(self) -> None:
        rows = _rows(_make_comp(list("hello") + ["\n"] + list("world"), 0))
        assert rows[0].startswith(PROMPT_CHAR + " ")
        assert rows[1].startswith("  ")
        assert not rows[1].startswith(PROMPT_CHAR)

    def test_the_cursor_is_drawn_on_its_own_line(self) -> None:
        buf = list("abc") + ["\n"] + list("def")
        assert [CURSOR_CHAR in row for row in _rows(_make_comp(buf, 4))] == [False, True]
        assert [CURSOR_CHAR in row for row in _rows(_make_comp(buf, 3))] == [True, False]
        assert _rows(_make_comp(buf, len(buf)))[-1].endswith(CURSOR_CHAR)

    def test_empty_lines_are_rows(self) -> None:
        assert len(_rows(_make_comp(["\n", "\n"], 0))) == 3

    def test_text_is_preserved(self) -> None:
        rows = _rows(_make_comp(list("def f():\n    pass"), 17))
        assert rows[0] == f"{PROMPT_CHAR} def f():"
        assert rows[1] == f"      pass{CURSOR_CHAR}"


class TestFolding:
    def test_a_long_line_folds_at_the_width_under_the_prompt(self) -> None:
        rows = _rows(_make_comp(list("x" * 25), 0), cols=12)
        assert rows == [f"{PROMPT_CHAR} {CURSOR_CHAR}xxxxxxxxx", "  " + "x" * 10, "  " + "x" * 6]
        assert all(len(row) <= 12 for row in rows)

    def test_wide_characters_fold_by_cell_width(self) -> None:
        rows = _rows(_make_comp(list("界" * 7), 7), cols=8)
        assert rows == [f"{PROMPT_CHAR} 界界界", "  界界界", f"  界{CURSOR_CHAR}"]

    def test_height_matches_the_rows(self) -> None:
        comp = _make_comp(list("y" * 50) + ["\n"] + list("z"), 3)
        assert comp.height(20) == len(comp.lines(20)) == 4


class TestPaste:
    def test_a_label_without_text_shows_the_label(self) -> None:
        comp = _make_comp([], 0, paste_condensed=True, paste_label="Pasted text #1 +10 lines")
        assert _rows(comp) == [f"{PROMPT_CHAR} Pasted text #1 +10 lines{CURSOR_CHAR}"]

    def test_text_typed_after_a_condensed_paste_stays_visible(self) -> None:
        label = "[Pasted text #1 10845 chars]"
        comp = _make_comp(list(label + "G"), len(label) + 1, True, label)
        assert _rows(comp) == [f"{PROMPT_CHAR} {label}G{CURSOR_CHAR}"]


class TestViewport:
    def test_a_tall_prompt_shows_max_rows_around_the_cursor(self) -> None:
        lines = [f"line {index:03d}" for index in range(200)]
        text = "\n".join(lines)
        comp = _make_comp(list(text), len(text), max_rows=12)
        rows = _rows(comp)
        assert len(rows) == 12
        assert rows[0] == "  ↑ 189 more lines"
        assert rows[-1].endswith(f"line 199{CURSOR_CHAR}")

    def test_the_window_moves_only_when_the_cursor_leaves_it(self) -> None:
        text = "\n".join(f"l{index}" for index in range(40))
        comp = _make_comp(list(text), 0, max_rows=10)
        first = _rows(comp)
        assert first[0].startswith(f"{PROMPT_CHAR} {CURSOR_CHAR}l0")
        assert first[-1] == "  ↓ 31 more lines"
        # Moving the cursor down inside the window keeps the window.
        comp._state.input.cursor.return_value = len("l0\nl1\nl2\n")
        assert _rows(comp)[0] == first[0].replace(CURSOR_CHAR, "")

    def test_typing_refolds_only_the_edited_line(self) -> None:
        lines = [f"line {index:03d} " + "w" * 30 for index in range(200)]
        text = "\n".join(lines)
        comp = _make_comp(list(text), len(text))
        comp.lines(80)
        before = _line_rows.cache_info()

        comp._state.input.buf.return_value = list(text + "!")
        comp._state.input.cursor.return_value = len(text) + 1
        comp.lines(80)

        after = _line_rows.cache_info()
        assert after.misses - before.misses == 1
        assert after.hits - before.hits == 199
