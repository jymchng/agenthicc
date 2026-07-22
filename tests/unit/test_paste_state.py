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


class TestBackspace:
    def test_backspace_deletes_entire_paste(self) -> None:
        buf = InputBuffer()
        ps = PasteState()
        ps.apply(buf, "a\nb\nc\nd", _COLS)
        len(buf)
        ps.backspace(buf)
        assert len(buf) == 0  # all inserted chars removed
        assert not ps.condensed
