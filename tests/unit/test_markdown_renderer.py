"""Tests for agenthicc.tui.markdown_renderer."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from agenthicc.tui.markdown_renderer import render_markdown_to_lines
from agenthicc.tui.color import strip_ansi


# ---------------------------------------------------------------------------
# render_markdown_to_lines — basic contract
# ---------------------------------------------------------------------------


class TestRenderMarkdownToLines:
    def test_returns_list(self) -> None:
        lines = render_markdown_to_lines("hello", 80)
        assert isinstance(lines, list)

    def test_plain_text_in_output(self) -> None:
        lines = render_markdown_to_lines("hello world", 80)
        assert any("hello" in l for l in lines)

    def test_header_rendered(self) -> None:
        lines = render_markdown_to_lines("# My Title", 80)
        assert any("My Title" in l for l in lines)

    def test_empty_string(self) -> None:
        lines = render_markdown_to_lines("", 80)
        assert isinstance(lines, list)

    def test_width_respected(self) -> None:
        long_text = "word " * 50
        lines = render_markdown_to_lines(long_text, 40)
        for line in lines:
            visible = strip_ansi(line)
            # allow small overage for Rich padding, but not double the width
            assert len(visible) <= 50, f"Line too long: {len(visible)!r}: {line!r}"

    def test_code_block(self) -> None:
        lines = render_markdown_to_lines("```python\nprint('hi')\n```", 80)
        assert any("print" in l for l in lines)

    def test_force_terminal_true(self) -> None:
        lines = render_markdown_to_lines("**bold**", 80, force_terminal=True)
        assert isinstance(lines, list)

    def test_force_terminal_false(self) -> None:
        lines = render_markdown_to_lines("**bold**", 80, force_terminal=False)
        assert isinstance(lines, list)

    def test_bold_text_present(self) -> None:
        lines = render_markdown_to_lines("**boldword**", 80)
        combined = " ".join(strip_ansi(l) for l in lines)
        assert "boldword" in combined

    def test_italic_text_present(self) -> None:
        lines = render_markdown_to_lines("_italicword_", 80)
        combined = " ".join(strip_ansi(l) for l in lines)
        assert "italicword" in combined

    def test_no_trailing_blank_lines(self) -> None:
        lines = render_markdown_to_lines("hello\n\n\n", 80)
        # trailing blank lines should be stripped
        if lines:
            assert lines[-1].strip() != ""

    def test_multiline_text(self) -> None:
        text = "Line one.\n\nLine two."
        lines = render_markdown_to_lines(text, 80)
        combined = " ".join(strip_ansi(l) for l in lines)
        assert "Line one" in combined
        assert "Line two" in combined

    def test_list_items_rendered(self) -> None:
        text = "- item one\n- item two\n- item three"
        lines = render_markdown_to_lines(text, 80)
        combined = " ".join(strip_ansi(l) for l in lines)
        assert "item one" in combined
        assert "item two" in combined

    def test_narrow_width(self) -> None:
        lines = render_markdown_to_lines("hello world", 20)
        assert isinstance(lines, list)
        assert len(lines) >= 1

    def test_wide_width(self) -> None:
        lines = render_markdown_to_lines("hello", 200)
        assert isinstance(lines, list)

    def test_h2_header(self) -> None:
        lines = render_markdown_to_lines("## Section Two", 80)
        combined = " ".join(strip_ansi(l) for l in lines)
        assert "Section Two" in combined

    def test_h3_header(self) -> None:
        lines = render_markdown_to_lines("### Sub Section", 80)
        combined = " ".join(strip_ansi(l) for l in lines)
        assert "Sub Section" in combined

    def test_inline_code(self) -> None:
        lines = render_markdown_to_lines("Use `myvar` here", 80)
        combined = " ".join(strip_ansi(l) for l in lines)
        assert "myvar" in combined

    def test_horizontal_rule(self) -> None:
        lines = render_markdown_to_lines("---", 80)
        # Just ensure it doesn't raise and returns a list
        assert isinstance(lines, list)
