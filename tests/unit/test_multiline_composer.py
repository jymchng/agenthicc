"""Unit tests: multi-line ComposerComponent rendering (PRD-84)."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from rich.console import Group
from rich.text import Text

from agenthicc.tui.workspace.components import ComposerComponent, _render_multiline
from agenthicc.tui.input.renderer import PROMPT_CHAR, CURSOR_CHAR

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_comp(buf: list[str], cursor: int,
               paste_condensed: bool = False,
               paste_label: str = "") -> ComposerComponent:
    app_state = MagicMock()
    inp = app_state.input
    inp.buf.return_value       = buf
    inp.cursor.return_value    = cursor
    inp.paste_condensed.return_value = paste_condensed
    inp.paste_label.return_value     = paste_label
    return ComposerComponent(app_state)


# ── _render_multiline unit tests ──────────────────────────────────────────────


class TestRenderMultiline:
    def test_single_logical_line_still_works(self):
        result = _render_multiline(list("hello"), 5)
        assert isinstance(result, Group)
        assert len(result.renderables) == 1

    def test_two_logical_lines(self):
        buf = list("line1") + ["\n"] + list("line2")
        result = _render_multiline(buf, 0)
        assert isinstance(result, Group)
        assert len(result.renderables) == 2

    def test_three_logical_lines(self):
        buf = list("a") + ["\n"] + list("b") + ["\n"] + list("c")
        result = _render_multiline(buf, 0)
        assert len(result.renderables) == 3

    def test_first_line_has_prompt_prefix(self):
        buf = list("hello") + ["\n"] + list("world")
        result = _render_multiline(buf, 0)
        first_plain = result.renderables[0].plain
        assert first_plain.startswith(PROMPT_CHAR + " ")

    def test_continuation_lines_have_indent(self):
        buf = list("hello") + ["\n"] + list("world")
        result = _render_multiline(buf, 0)
        second_plain = result.renderables[1].plain
        assert second_plain.startswith("  ")
        assert not second_plain.startswith(PROMPT_CHAR)

    def test_cursor_appears_on_correct_line(self):
        # cursor after '\n' → on second line
        buf = list("abc") + ["\n"] + list("def")
        cursor = 4   # first char of second line
        result = _render_multiline(buf, cursor)
        second_plain = result.renderables[1].plain
        assert CURSOR_CHAR in second_plain
        first_plain = result.renderables[0].plain
        assert CURSOR_CHAR not in first_plain

    def test_cursor_at_start_of_buffer(self):
        buf = list("hello") + ["\n"] + list("world")
        result = _render_multiline(buf, 0)
        first_plain = result.renderables[0].plain
        assert CURSOR_CHAR in first_plain

    def test_cursor_at_end_of_buffer(self):
        buf = list("ab") + ["\n"] + list("cd")
        cursor = len(buf)  # after last char
        result = _render_multiline(buf, cursor)
        last_plain = result.renderables[-1].plain
        assert CURSOR_CHAR in last_plain

    def test_cursor_at_end_of_first_line(self):
        buf = list("abc") + ["\n"] + list("def")
        cursor = 3   # end of "abc", before '\n'
        result = _render_multiline(buf, cursor)
        first_plain = result.renderables[0].plain
        assert CURSOR_CHAR in first_plain
        second_plain = result.renderables[1].plain
        assert CURSOR_CHAR not in second_plain

    def test_empty_lines_render(self):
        buf = ["\n", "\n"]   # two empty lines
        result = _render_multiline(buf, 0)
        assert len(result.renderables) == 3

    def test_content_preserved(self):
        buf = list("hello") + ["\n"] + list("world")
        result = _render_multiline(buf, 0)
        combined = "".join(r.plain for r in result.renderables)
        assert "hello" in combined
        assert "world" in combined


# ── ComposerComponent.render() path selection ─────────────────────────────────


class TestComposerRenderPaths:
    def test_single_line_returns_group(self):
        # Non-condensed content always uses _render_multiline → Group,
        # even for single-line buffers (PRD-84 consolidation).
        comp = _make_comp(list("hello"), 5)
        result = comp.render()
        assert isinstance(result, Group)
        assert len(result.renderables) == 1

    def test_multiline_returns_group(self):
        buf = list("line1") + ["\n"] + list("line2")
        comp = _make_comp(buf, 0)
        result = comp.render()
        assert isinstance(result, Group)

    def test_multiline_all_lines_visible(self):
        buf = list("first") + ["\n"] + list("second") + ["\n"] + list("third")
        comp = _make_comp(buf, 0)
        result = comp.render()
        assert isinstance(result, Group)
        assert len(result.renderables) == 3
        combined = "".join(r.plain for r in result.renderables)
        assert "first" in combined
        assert "second" in combined
        assert "third" in combined

    def test_condensed_paste_returns_text(self):
        comp = _make_comp([], 0, paste_condensed=True,
                          paste_label="Pasted text #1 +10 lines")
        result = comp.render()
        assert isinstance(result, Text)
        assert "Pasted text" in result.plain

    def test_expanded_paste_multiline_returns_group(self):
        # After Ctrl+V: paste_condensed=False, buf has newlines
        buf = list("def f():\n    pass")
        comp = _make_comp(buf, len(buf))
        result = comp.render()
        assert isinstance(result, Group)
        combined = "".join(r.plain for r in result.renderables)
        assert "def f():" in combined
        assert "    pass" in combined

    def test_large_multiline_not_truncated(self):
        # 10 lines of 20 chars each → total visible ~200 > typical cols (80)
        # Previously _fit would truncate this; now it must show all 10 lines.
        line = list("x" * 20)
        buf: list[str] = []
        for i in range(10):
            if i > 0:
                buf.append("\n")
            buf.extend(line)
        comp = _make_comp(buf, 0)
        result = comp.render()
        assert isinstance(result, Group)
        assert len(result.renderables) == 10
