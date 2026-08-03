"""Unit tests for agenthicc.tui.input.paste.PasteState (PRD-57 §10.2)."""

from __future__ import annotations

import pytest
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.paste import PasteState

pytestmark = pytest.mark.unit

_COLS = 80


class TestApply:
    def test_short_paste_not_condensed(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        ps.apply(buf, "hi", _COLS)
        assert not ps.condensed
        assert "".join(buf.buf) == "hi"

    def test_long_paste_condensed_by_lines(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        text = "a\nb\nc\nd"  # 4 lines → above threshold (3)
        ps.apply(buf, text, _COLS)
        assert ps.condensed
        assert "+4 lines" in ps.label
        assert ps.count == 1

    def test_wide_paste_condensed_by_chars(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        text = "x" * 100  # single line but > cols - 4
        ps.apply(buf, text, _COLS)
        assert ps.condensed
        assert "chars" in ps.label

    def test_paste_count_increments(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        ps.apply(buf, "a\nb\nc\nd", _COLS)
        ps.expand()
        ps.apply(buf, "e\nf\ng\nh", _COLS)
        assert ps.count == 2
        assert "#2" in ps.label

    def test_paste_records_range(self) -> None:
        buf = InputBuffer(list("prefix"))
        ps = PasteState()
        ps.apply(buf, "AB", _COLS)
        assert ps.start == 6
        assert ps.end == 8


class TestExpand:
    def test_expand_clears_condensed(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        ps.apply(buf, "a\nb\nc\nd", _COLS)
        assert ps.condensed
        ps.expand()
        assert not ps.condensed

    def test_condensed_view_keeps_text_typed_after_paste_visible(self) -> None:
        buf = InputBuffer(list("prefix"))
        ps = PasteState()
        ps.apply(buf, "a\nb\nc\nd", _COLS)
        buf.insert("G")

        display, cursor = ps._condensed_view(buf)

        assert "".join(display) == f"prefix{ps.label}G"
        assert cursor == len(display)

    def test_insert_before_condensed_paste_moves_hidden_range(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        text = "a\nb\nc\nd"
        ps.apply(buf, text, _COLS)

        ps.move_home(buf)
        ps.insert(buf, "G")

        assert buf.text == "G" + text
        assert buf.cursor == 1
        assert (ps.start, ps.end) == (1, 1 + len(text))
        display, cursor = ps._condensed_view(buf)
        assert "".join(display) == "G" + ps.label
        assert cursor == 1

    def test_insert_inside_condensed_paste_is_placed_after_hidden_range(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        text = "a\nb\nc\nd"
        ps.apply(buf, text, _COLS)
        buf.cursor = ps.start + 2

        ps.insert(buf, "G")

        assert buf.text == text + "G"
        assert buf.cursor == len(text) + 1
        display, cursor = ps._condensed_view(buf)
        assert "".join(display) == ps.label + "G"
        assert cursor == len(display)

    def test_home_and_end_use_condensed_projection(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        ps.apply(buf, "a\nb\nc\nd", _COLS)

        ps.move_home(buf)
        assert buf.cursor == ps.start

        ps.move_end(buf)
        assert buf.cursor == ps.end


class TestBackspace:
    def test_backspace_deletes_one_character_from_condensed_paste(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        text = "x" * 100
        ps.apply(buf, text, _COLS)

        ps.backspace(buf)

        assert len(buf) == len(text) - 1
        assert ps.condensed
        assert "99 chars" in ps.label

    def test_backspace_deletes_typed_suffix_before_hidden_paste(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        ps.apply(buf, "a\nb\nc\nd", _COLS)
        buf.insert_many(list(" hello hey"))

        ps.backspace(buf)

        assert buf.text.endswith(" hello he")
        assert ps.condensed
        display, cursor = ps._condensed_view(buf)
        assert "".join(display) == f"{ps.label} hello he"
        assert cursor == len(display)

    def test_delete_condensed_preserves_suffix_and_boundary_cursor(self) -> None:
        buf = InputBuffer(list("prefix"))
        ps = PasteState()
        ps.apply(buf, "a\nb\nc\nd", _COLS)
        buf.insert("suffix")
        buf.cursor = ps.end

        ps.delete_condensed(buf)

        assert buf.text == "prefixsuffix"
        assert buf.cursor == len("prefix")
        assert not ps.condensed

    def test_placeholder_disappears_after_hidden_content_is_exhausted(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        text = "a\nb\nc\nd"
        ps.apply(buf, text, _COLS)

        for _ in text:
            ps.backspace(buf)

        assert not ps.condensed
        assert ps.label == ""
        assert buf.text == ""
