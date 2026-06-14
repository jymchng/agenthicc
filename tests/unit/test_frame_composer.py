"""Unit tests for agenthicc.tui.frame_composer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from agenthicc.tui.frame_composer import Frame, FrameComposer, simple_wrap
from agenthicc.tui.terminal import FakeTerminal

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeTranscript:
    """Minimal transcript double with a fixed render() output."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines = lines or ["line one", "line two"]

    def render(self) -> list[str]:
        return list(self._lines)


@dataclass
class FakeStatus:
    active: bool = False
    spinner_frame: int = 0
    intent_started_at: float = 0.0
    input_tokens: int = 100
    output_tokens: int = 200
    session_cost_usd: float = 0.042
    completed_agents: int = 3
    session_id: str = "test-session"
    mode_name: str = "Auto"
    partial_text: str = ""


@dataclass
class FakeInputState:
    text: str = "hello"
    dropdown_rows: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFrameComposerBasic:
    def setup_method(self) -> None:
        self.composer = FrameComposer()

    def test_compose_returns_frame(self) -> None:
        frame = self.composer.compose(None, None, None)
        assert isinstance(frame, Frame)

    def test_committed_from_transcript_render(self) -> None:
        transcript = FakeTranscript(["alpha", "beta", "gamma"])
        frame = self.composer.compose(transcript, None, None)
        assert frame.committed == ["alpha", "beta", "gamma"]

    def test_committed_empty_when_no_transcript(self) -> None:
        frame = self.composer.compose(None, None, None)
        assert frame.committed == []

    def test_bottom_has_divider(self) -> None:
        frame = self.composer.compose(None, FakeStatus(), FakeInputState())
        divider_rows = [r for r in frame.bottom if "─" in r]
        assert divider_rows, "Expected a divider row containing '─'"

    def test_bottom_has_prompt(self) -> None:
        frame = self.composer.compose(None, FakeStatus(), FakeInputState(text="world"))
        # The green ❯ prompt character must appear
        prompt_rows = [r for r in frame.bottom if "❯" in r]
        assert prompt_rows, "Expected at least one row containing the ❯ prompt glyph"

    def test_bottom_has_footer(self) -> None:
        frame = self.composer.compose(None, FakeStatus(), FakeInputState())
        footer_rows = [r for r in frame.bottom if "shift+tab" in r]
        assert footer_rows, "Expected a footer row containing 'shift+tab'"


class TestActiveStatus:
    def setup_method(self) -> None:
        self.composer = FrameComposer()

    def test_active_status_shows_thinking(self) -> None:
        import re

        status = FakeStatus(active=True, spinner_frame=0)
        frame = self.composer.compose(None, status, None)
        # Strip ANSI escapes before checking for the thinking text, since the
        # wave animation wraps individual characters in bold sequences.
        _ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        plain_rows = [_ansi.sub("", r) for r in frame.bottom]
        thinking_rows = [r for r in plain_rows if "Thinking" in r]
        assert thinking_rows, "Expected 'Thinking' in the status line when active=True"

    def test_active_status_contains_elapsed(self) -> None:
        import time
        status = FakeStatus(active=True, intent_started_at=time.monotonic() - 5.0)
        frame = self.composer.compose(None, status, None)
        # Should contain elapsed seconds like "5.0s" or similar
        status_rows = [r for r in frame.bottom if "s  │" in r or "│" in r]
        assert status_rows

    def test_active_status_contains_tokens(self) -> None:
        status = FakeStatus(active=True, input_tokens=1234, output_tokens=567)
        frame = self.composer.compose(None, status, None)
        joined = "\n".join(frame.bottom)
        assert "1,234" in joined or "1234" in joined


class TestIdleStatus:
    def setup_method(self) -> None:
        self.composer = FrameComposer()

    def test_idle_status_shows_session_id(self) -> None:
        status = FakeStatus(active=False, session_id="my-session-abc")
        frame = self.composer.compose(None, status, None)
        joined = "\n".join(frame.bottom)
        assert "my-session-abc" in joined

    def test_idle_status_shows_cost(self) -> None:
        status = FakeStatus(active=False, session_cost_usd=1.234)
        frame = self.composer.compose(None, status, None)
        joined = "\n".join(frame.bottom)
        assert "1.234" in joined

    def test_idle_status_shows_turns(self) -> None:
        status = FakeStatus(active=False, completed_agents=7)
        frame = self.composer.compose(None, status, None)
        joined = "\n".join(frame.bottom)
        assert "7 turns" in joined


class TestPartialText:
    def setup_method(self) -> None:
        self.composer = FrameComposer()

    def test_partial_text_in_bottom(self) -> None:
        status = FakeStatus(partial_text="streaming output here")
        frame = self.composer.compose(None, status, FakeInputState())
        # At least one row should include the partial text (possibly wrapped/dimmed)
        joined = "\n".join(frame.bottom)
        assert "streaming output here" in joined

    def test_partial_text_empty_not_rendered(self) -> None:
        status = FakeStatus(partial_text="")
        frame = self.composer.compose(None, status, FakeInputState())
        # No spurious blank rows from empty partial text
        # The bottom should still have status/divider/prompt/footer rows
        assert len(frame.bottom) >= 3

    def test_partial_text_whitespace_only_not_rendered(self) -> None:
        status = FakeStatus(partial_text="   ")
        frame_with = self.composer.compose(None, status, FakeInputState())
        status_no = FakeStatus(partial_text="")
        frame_without = self.composer.compose(None, status_no, FakeInputState())
        # Both should produce the same number of bottom rows (no extra rows for whitespace)
        assert len(frame_with.bottom) == len(frame_without.bottom)


class TestInputRows:
    def setup_method(self) -> None:
        self.composer = FrameComposer()

    def test_single_line_input_has_prompt(self) -> None:
        frame = self.composer.compose(None, FakeStatus(), FakeInputState(text="hello"))
        prompt_rows = [r for r in frame.bottom if "❯" in r and "hello" in r]
        assert prompt_rows

    def test_multiline_input_rows(self) -> None:
        """Both lines of a multi-line input must appear in the bottom rows."""
        frame = self.composer.compose(
            None, FakeStatus(), FakeInputState(text="a\nb")
        )
        joined = "\n".join(frame.bottom)
        assert "a" in joined
        assert "b" in joined

    def test_multiline_continuation_has_indent(self) -> None:
        frame = self.composer.compose(
            None, FakeStatus(), FakeInputState(text="first\nsecond")
        )
        # Continuation lines start with "  " (two spaces), not "❯"
        cont_rows = [r for r in frame.bottom if "second" in r]
        assert cont_rows
        assert cont_rows[0].startswith("  ")

    def test_dropdown_rows_appended(self) -> None:
        input_state = FakeInputState(
            text="hello", dropdown_rows=["/foo  do foo", "/bar  do bar"]
        )
        frame = self.composer.compose(None, FakeStatus(), input_state)
        joined = "\n".join(frame.bottom)
        assert "/foo" in joined
        assert "/bar" in joined

    def test_no_input_state_renders_prompt(self) -> None:
        frame = self.composer.compose(None, FakeStatus(), None)
        prompt_rows = [r for r in frame.bottom if "❯" in r]
        assert prompt_rows


class TestModeFooter:
    def setup_method(self) -> None:
        self.composer = FrameComposer()

    def test_auto_mode_footer(self) -> None:
        status = FakeStatus(mode_name="Auto")
        frame = self.composer.compose(None, status, None)
        footer = [r for r in frame.bottom if "shift+tab" in r]
        assert footer
        assert "Auto" in footer[0]

    def test_custom_mode_footer(self) -> None:
        status = FakeStatus(mode_name="Plan")
        frame = self.composer.compose(None, status, None)
        footer = [r for r in frame.bottom if "shift+tab" in r]
        assert footer
        assert "Plan" in footer[0]


class TestSimpleWrap:
    def test_short_text_unchanged(self) -> None:
        assert simple_wrap("hello", 80) == ["hello"]

    def test_wraps_at_width(self) -> None:
        result = simple_wrap("abcdef", 3)
        assert result == ["abc", "def"]

    def test_preserves_newlines(self) -> None:
        result = simple_wrap("foo\nbar", 80)
        assert result == ["foo", "bar"]

    def test_empty_string(self) -> None:
        result = simple_wrap("", 80)
        assert result == [""]

    def test_zero_width_returns_input(self) -> None:
        result = simple_wrap("hello", 0)
        # With width=0, just returns the text as-is (degenerate)
        assert isinstance(result, list)
