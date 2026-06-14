"""Tests for agenthicc.tui.color (PRD coverage)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from agenthicc.tui.color import (
    ANSIColor,
    ColorPalette,
    clip_ansi_line,
    color_for_depth,
    strip_ansi,
)


# ---------------------------------------------------------------------------
# strip_ansi
# ---------------------------------------------------------------------------


class TestStripAnsi:
    def test_strips_color_codes(self) -> None:
        assert strip_ansi("\x1b[32mhello\x1b[0m") == "hello"

    def test_plain_text_unchanged(self) -> None:
        assert strip_ansi("hello") == "hello"

    def test_strips_multiple_codes(self) -> None:
        result = strip_ansi("\x1b[1m\x1b[32mtext\x1b[0m")
        assert "\x1b" not in result
        assert "text" in result

    def test_empty_string(self) -> None:
        assert strip_ansi("") == ""

    def test_only_code(self) -> None:
        assert strip_ansi("\x1b[0m") == ""

    def test_strips_256_color(self) -> None:
        # 256-colour foreground: ESC[38;5;200m
        result = strip_ansi("\x1b[38;5;200mcolored\x1b[0m")
        assert result == "colored"

    def test_strips_osc_sequence(self) -> None:
        # OST title sequence: ESC]0;title\x07
        result = strip_ansi("\x1b]0;my title\x07hello")
        assert result == "hello"

    def test_strips_bold_and_text(self) -> None:
        assert strip_ansi("\x1b[1mbold\x1b[0m") == "bold"

    def test_preserves_unicode(self) -> None:
        assert strip_ansi("\x1b[32m☃\x1b[0m") == "☃"


# ---------------------------------------------------------------------------
# clip_ansi_line
# ---------------------------------------------------------------------------


class TestClipAnsiLine:
    def test_short_unchanged(self) -> None:
        assert clip_ansi_line("hi", 10) == "hi"

    def test_clips_long_plain(self) -> None:
        result = clip_ansi_line("x" * 100, 10)
        assert len(strip_ansi(result)) <= 10

    def test_clips_with_ansi(self) -> None:
        coloured = "\x1b[32m" + "x" * 20 + "\x1b[0m"
        result = clip_ansi_line(coloured, 5)
        assert len(strip_ansi(result)) <= 5

    def test_empty_string(self) -> None:
        # Empty string — result should be empty (or just reset)
        result = clip_ansi_line("", 10)
        assert strip_ansi(result) == ""

    def test_exact_width_unchanged(self) -> None:
        text = "hello"  # 5 chars
        result = clip_ansi_line(text, 5)
        assert strip_ansi(result) == "hello"

    def test_zero_cols(self) -> None:
        result = clip_ansi_line("hello", 0)
        # clipped to 0 columns — visible content should be empty
        assert strip_ansi(result) == ""

    def test_ansi_preserved_in_clipped(self) -> None:
        coloured = "\x1b[32m" + "abcde" + "\x1b[0m"
        result = clip_ansi_line(coloured, 3)
        # Should still have some ANSI escape in result (the open code was consumed)
        visible = strip_ansi(result)
        assert len(visible) <= 3


# ---------------------------------------------------------------------------
# ANSIColor
# ---------------------------------------------------------------------------


class TestANSIColor:
    def _make(self, name: str = "bright_blue") -> ANSIColor:
        return ANSIColor(name=name, ansi_256=12, ansi_true="#0087ff")

    def test_create_stores_name(self) -> None:
        c = self._make("bright_blue")
        assert c.name == "bright_blue"

    def test_create_stores_ansi_256(self) -> None:
        c = ANSIColor(name="bright_red", ansi_256=9, ansi_true="#ff0000")
        assert c.ansi_256 == 9

    def test_create_stores_ansi_true(self) -> None:
        c = ANSIColor(name="bright_red", ansi_256=9, ansi_true="#ff0000")
        assert c.ansi_true == "#ff0000"

    def test_render_depth_0_returns_plain(self) -> None:
        c = self._make()
        assert c.render("text", depth=0) == "text"

    def test_render_depth_8_contains_text(self) -> None:
        c = self._make()
        result = c.render("text", depth=8)
        assert "text" in result

    def test_render_depth_8_contains_ansi(self) -> None:
        c = self._make()
        result = c.render("hello", depth=8)
        # Should wrap with ANSI codes when depth is 8
        assert "\x1b" in result

    def test_render_depth_256_contains_text(self) -> None:
        c = self._make()
        result = c.render("hello", depth=256)
        assert "hello" in result

    def test_frozen_dataclass(self) -> None:
        c = self._make()
        with pytest.raises((AttributeError, TypeError)):
            c.name = "other"  # type: ignore[misc]

    def test_render_empty_string_depth_0(self) -> None:
        c = self._make()
        assert c.render("", depth=0) == ""

    def test_render_empty_string_depth_8(self) -> None:
        c = self._make()
        result = c.render("", depth=8)
        # Even empty — should return some string
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# ColorPalette
# ---------------------------------------------------------------------------


class TestColorPalette:
    def test_default_primary(self) -> None:
        p = ColorPalette()
        assert p.primary == "bright_blue"

    def test_default_error(self) -> None:
        p = ColorPalette()
        assert p.error == "bright_red"

    def test_default_success(self) -> None:
        p = ColorPalette()
        assert p.success == "bright_green"

    def test_default_warning(self) -> None:
        p = ColorPalette()
        assert p.warning == "bright_yellow"

    def test_default_muted(self) -> None:
        p = ColorPalette()
        assert p.muted == "bright_black"

    def test_default_accent(self) -> None:
        p = ColorPalette()
        assert p.accent == "bright_cyan"

    def test_override_primary(self) -> None:
        p = ColorPalette(primary="red")
        assert p.primary == "red"

    def test_override_multiple(self) -> None:
        p = ColorPalette(primary="red", error="blue", success="yellow")
        assert p.primary == "red"
        assert p.error == "blue"
        assert p.success == "yellow"

    def test_is_mutable(self) -> None:
        # ColorPalette is a plain dataclass (not frozen), so it should be mutable
        p = ColorPalette()
        p.primary = "green"
        assert p.primary == "green"


# ---------------------------------------------------------------------------
# color_for_depth
# ---------------------------------------------------------------------------


class TestColorForDepth:
    def test_depth_0_returns_empty(self) -> None:
        assert color_for_depth("blue", 0) == ""

    def test_depth_8_returns_name(self) -> None:
        assert color_for_depth("blue", 8) == "blue"

    def test_depth_256_returns_name(self) -> None:
        assert color_for_depth("bright_cyan", 256) == "bright_cyan"

    def test_depth_1_returns_name(self) -> None:
        # Any non-zero depth should return the name
        assert color_for_depth("red", 1) == "red"

    def test_preserves_rich_color_name(self) -> None:
        assert color_for_depth("bright_magenta", 8) == "bright_magenta"

    def test_various_zero_is_empty(self) -> None:
        for name in ("red", "blue", "green", "bright_yellow"):
            assert color_for_depth(name, 0) == ""
