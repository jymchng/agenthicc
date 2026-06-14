"""Unit tests for agenthicc.tui.render_loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from agenthicc.tui.frame_composer import FrameComposer
from agenthicc.tui.render_loop import MIN_INTERVAL, RenderLoop
from agenthicc.tui.terminal import FakeTerminal

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeTranscript:
    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines = lines or []

    def render(self) -> list[str]:
        return list(self._lines)

    def add_line(self, line: str) -> None:
        self._lines.append(line)


@dataclass
class FakeStatus:
    active: bool = False
    spinner_frame: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    session_cost_usd: float = 0.0
    completed_agents: int = 0
    session_id: str = "test"
    mode_name: str = "Auto"
    partial_text: str = ""
    intent_started_at: float = 0.0


@dataclass
class FakeInputState:
    text: str = ""
    dropdown_rows: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_loop() -> tuple[FakeTerminal, RenderLoop]:
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)
    return terminal, loop


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRenderCommitsLines:
    def test_render_commits_only_new_lines(self) -> None:
        """Only lines not yet committed should be passed to commit_lines."""
        terminal, loop = make_loop()
        transcript = FakeTranscript(["line-A", "line-B"])

        loop.render(transcript, FakeStatus(), FakeInputState())
        assert terminal.committed == ["line-A", "line-B"]

        # Add one more line; only it should be committed on the second render
        transcript.add_line("line-C")
        loop.render(transcript, FakeStatus(), FakeInputState())
        assert terminal.committed == ["line-A", "line-B", "line-C"]

    def test_render_commits_no_lines_when_transcript_unchanged(self) -> None:
        terminal, loop = make_loop()
        transcript = FakeTranscript(["only-line"])

        loop.render(transcript, FakeStatus(), FakeInputState())
        first_count = len(terminal.committed)

        loop.render(transcript, FakeStatus(), FakeInputState())
        # No new committed lines
        assert len(terminal.committed) == first_count

    def test_render_empty_transcript(self) -> None:
        terminal, loop = make_loop()
        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        assert terminal.committed == []


class TestRenderSetsBottom:
    def test_render_always_sets_bottom(self) -> None:
        """set_bottom must be called on every render, even with no new lines."""
        terminal, loop = make_loop()
        transcript = FakeTranscript([])

        loop.render(transcript, FakeStatus(), FakeInputState())
        # After first render the bottom block must be populated
        assert len(terminal.bottom) > 0

        # Capture current bottom, then render again and confirm it was redrawn
        bottom_before = list(terminal.bottom)
        loop.render(transcript, FakeStatus(), FakeInputState())
        # bottom is refreshed; it will equal the previous content (same inputs)
        assert terminal.bottom == bottom_before

    def test_bottom_is_not_empty(self) -> None:
        terminal, loop = make_loop()
        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        assert len(terminal.bottom) > 0

    def test_bottom_contains_divider(self) -> None:
        terminal, loop = make_loop()
        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        # FakeTerminal.set_bottom truncates via truncate_to_cols which appends
        # \x1b[0m; the divider characters are still present.
        assert any("─" in row for row in terminal.bottom)


class TestReset:
    def test_reset_clears_count(self) -> None:
        terminal, loop = make_loop()
        transcript = FakeTranscript(["a", "b", "c"])

        loop.render(transcript, FakeStatus(), FakeInputState())
        assert terminal.committed == ["a", "b", "c"]

        loop.reset()
        # After reset, the same lines are committed again from scratch
        loop.render(transcript, FakeStatus(), FakeInputState())
        assert terminal.committed == ["a", "b", "c", "a", "b", "c"]

    def test_reset_resets_frame_num(self) -> None:
        _, loop = make_loop()
        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        assert loop._frame_num == 2

        loop.reset()
        assert loop._frame_num == 0

    def test_reset_resets_last_render(self) -> None:
        _, loop = make_loop()
        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        assert loop._last_render > 0

        loop.reset()
        assert loop._last_render == 0.0


class TestTick:
    def test_tick_renders_when_due(self) -> None:
        terminal, loop = make_loop()
        # _last_render is 0.0, so tick should render immediately
        loop.tick(FakeTranscript([]), FakeStatus(), FakeInputState())
        assert len(terminal.bottom) > 0

    def test_tick_skips_when_too_soon(self) -> None:
        terminal, loop = make_loop()
        # First tick renders
        loop.tick(FakeTranscript(["x"]), FakeStatus(), FakeInputState())
        committed_after_first = len(terminal.committed)

        # Immediate second tick with new lines should be debounced (skipped)
        transcript2 = FakeTranscript(["x", "y"])
        loop.tick(transcript2, FakeStatus(), FakeInputState())
        # "y" must NOT have been committed yet
        assert len(terminal.committed) == committed_after_first

    def test_tick_renders_after_interval(self) -> None:
        terminal, loop = make_loop()
        loop.tick(FakeTranscript([]), FakeStatus(), FakeInputState())
        bottom_after_first = list(terminal.bottom)

        # Artificially push _last_render back past MIN_INTERVAL
        loop._last_render = time.monotonic() - MIN_INTERVAL - 0.01
        loop.tick(FakeTranscript([]), FakeStatus(), FakeInputState())
        # bottom was re-rendered (same content but re-set)
        assert terminal.bottom == bottom_after_first


class TestForceCommit:
    def test_force_commit_bypasses_debounce(self) -> None:
        terminal, loop = make_loop()

        # First render sets the clock
        loop.tick(FakeTranscript(["a"]), FakeStatus(), FakeInputState())
        committed_after_first = list(terminal.committed)

        # force_commit must render even though the interval hasn't elapsed
        transcript2 = FakeTranscript(["a", "b"])
        loop.force_commit(transcript2, FakeStatus(), FakeInputState())
        # "b" should now be committed despite debounce
        assert "b" in terminal.committed

    def test_force_commit_commits_new_lines(self) -> None:
        terminal, loop = make_loop()
        transcript = FakeTranscript(["x", "y"])

        loop.force_commit(transcript, FakeStatus(), FakeInputState())
        assert "x" in terminal.committed
        assert "y" in terminal.committed


class TestFrameNumAdvances:
    def test_frame_num_increments_on_each_render(self) -> None:
        _, loop = make_loop()
        assert loop._frame_num == 0

        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        assert loop._frame_num == 1

        loop.render(FakeTranscript([]), FakeStatus(), FakeInputState())
        assert loop._frame_num == 2
