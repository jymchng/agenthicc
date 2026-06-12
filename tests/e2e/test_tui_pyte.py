"""E2E: TUI renders with Input Bar always on the last row (PRD-06)."""
from __future__ import annotations
import pytest
import pyte
from agenthicc.tui.app import render_frame_ansi
from agenthicc.tui.transcript import TranscriptModel

pytestmark = pytest.mark.e2e

COLS, ROWS = 80, 24

def _feed(frame: str) -> pyte.Screen:
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    stream.feed(frame.encode())
    return screen

def _last_row_text(screen: pyte.Screen) -> str:
    return "".join(c.data for c in screen.buffer[ROWS - 1].values())

class TestInputBarPosition:
    def _model_with_n_lines(self, n: int) -> TranscriptModel:
        m = TranscriptModel()
        for i in range(n):
            m.append_turn(f"a{i}", f"agent:{i}", float(i))
            m.append_line(f"a{i}", f"line content {i}")
        return m

    def test_input_bar_on_last_row_short_transcript(self):
        model = self._model_with_n_lines(3)
        frame = render_frame_ansi(model, cols=COLS, rows=ROWS)
        screen = _feed(frame)
        last_row = _last_row_text(screen)
        assert ">" in last_row, f"Input bar not on last row. Got: {last_row!r}"

    def test_input_bar_on_last_row_long_transcript(self):
        model = self._model_with_n_lines(50)
        frame = render_frame_ansi(model, cols=COLS, rows=ROWS)
        screen = _feed(frame)
        last_row = _last_row_text(screen)
        assert ">" in last_row, f"Input bar not on last row with long transcript. Got: {last_row!r}"

    def test_input_bar_on_last_row_with_menu_overlay(self):
        model = self._model_with_n_lines(5)
        menu = ["/status: 3 agents", "  - agent-0: busy", "  - agent-1: idle"]
        frame = render_frame_ansi(model, cols=COLS, rows=ROWS, menu_lines=menu)
        screen = _feed(frame)
        last_row = _last_row_text(screen)
        assert ">" in last_row, f"Input bar displaced by menu. Got: {last_row!r}"

    def test_menu_does_not_overwrite_input_bar(self):
        model = self._model_with_n_lines(3)
        # Menu with many lines that could theoretically overflow onto input bar
        long_menu = [f"menu line {i}" for i in range(10)]
        frame = render_frame_ansi(model, cols=COLS, rows=ROWS, menu_lines=long_menu)
        screen = _feed(frame)
        last_row = _last_row_text(screen)
        assert ">" in last_row

    def test_input_text_appears_in_last_row(self):
        model = TranscriptModel()
        frame = render_frame_ansi(model, cols=COLS, rows=ROWS, input_text="hello")
        screen = _feed(frame)
        last_row = _last_row_text(screen)
        assert "hello" in last_row, f"Input text not visible. Got: {last_row!r}"
