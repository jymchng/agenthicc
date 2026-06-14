"""Integration tests for the TUI revamp: FakeTerminal, FrameComposer, RenderLoop,
and InputState.

These tests exercise the composed behaviour of the rendering pipeline without
touching a real TTY.  All I/O is captured by :class:`FakeTerminal`.
"""

from __future__ import annotations

import re
import time
from unittest.mock import MagicMock

import pytest

from agenthicc.tui.frame_composer import FrameComposer
from agenthicc.tui.input_state import InputState, InputResultKind
from agenthicc.tui.render_loop import MIN_INTERVAL, RenderLoop
from agenthicc.tui.terminal import FakeTerminal, Key

pytestmark = pytest.mark.integration


_ANSI_RE = re.compile(r"\x1b\[[^a-zA-Z]*[a-zA-Z]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_transcript(lines: list[str]) -> MagicMock:
    """Return a transcript stub whose render() returns *lines*."""
    t = MagicMock()
    t.render.return_value = list(lines)
    return t


def _mock_status(active: bool = False, partial_text: str = "") -> MagicMock:
    s = MagicMock()
    s.active = active
    s.partial_text = partial_text
    s.spinner_frame = 0
    s.intent_started_at = None
    s.input_tokens = 0
    s.output_tokens = 0
    s.session_id = "test-session"
    s.completed_agents = 0
    s.session_cost_usd = 0.0
    s.mode_name = "Auto"
    return s


# ---------------------------------------------------------------------------
# 1. Committed lines go to scrollback
# ---------------------------------------------------------------------------


def test_commit_lines_goes_to_scrollback():
    """All transcript lines rendered by RenderLoop end up in terminal.committed."""
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)

    transcript = _mock_transcript(["alpha-line", "beta-line", "gamma-line"])
    loop.render(transcript, status=None, input_state=None)

    assert terminal.committed == ["alpha-line", "beta-line", "gamma-line"]
    # None of the transcript lines should leak into the bottom block.
    bottom_text = _ANSI_RE.sub("", "\n".join(terminal.bottom))
    for line in ["alpha-line", "beta-line", "gamma-line"]:
        assert line not in bottom_text


# ---------------------------------------------------------------------------
# 2. Bottom block structure
# ---------------------------------------------------------------------------


def test_bottom_block_structure():
    """The bottom block always contains a divider row, a prompt glyph, and mode footer."""
    composer = FrameComposer()
    frame = composer.compose(
        transcript=None,
        status=None,
        input_state=None,
        cols=80,
    )

    bottom_text = "\n".join(frame.bottom)
    # Divider line
    assert "─" in bottom_text
    # Prompt glyph
    assert "❯" in bottom_text
    # Mode footer hint
    assert "shift+tab" in bottom_text


# ---------------------------------------------------------------------------
# 3. Only new lines committed on second render
# ---------------------------------------------------------------------------


def test_only_new_lines_committed_on_second_render():
    """On the second render only the newly added line is committed."""
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)

    transcript = _mock_transcript(["a", "b"])
    loop.render(transcript, status=None, input_state=None)

    transcript.render.return_value = ["a", "b", "c"]
    loop.render(transcript, status=None, input_state=None)

    # Total committed should be exactly ["a", "b", "c"], not duplicated.
    assert terminal.committed == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 4. Partial text appears in bottom, not committed
# ---------------------------------------------------------------------------


def test_partial_text_appears_in_bottom_not_committed():
    """A streaming partial_text is shown in the bottom block, not committed."""
    terminal = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(terminal, composer)

    status = _mock_status(active=True, partial_text="streaming content here")
    transcript = _mock_transcript([])
    loop.render(transcript, status=status, input_state=None)

    bottom_text = "\n".join(terminal.bottom)
    assert "streaming" in bottom_text
    # Must not be committed to scrollback.
    assert not any("streaming" in line for line in terminal.committed)


# ---------------------------------------------------------------------------
# 5. Status line: active vs idle
# ---------------------------------------------------------------------------


def test_status_line_active_vs_idle():
    """Active status shows 'Thinking'; idle status does not."""
    composer = FrameComposer()

    active_status = _mock_status(active=True)
    active_frame = composer.compose(
        transcript=None,
        status=active_status,
        input_state=None,
        cols=80,
    )
    # Strip ANSI escape sequences before asserting on plain text.
    active_bottom = _ANSI_RE.sub("", "\n".join(active_frame.bottom))
    assert "Thinking" in active_bottom

    idle_status = _mock_status(active=False)
    idle_frame = composer.compose(
        transcript=None,
        status=idle_status,
        input_state=None,
        cols=80,
    )
    idle_bottom = _ANSI_RE.sub("", "\n".join(idle_frame.bottom))
    assert "Thinking" not in idle_bottom


# ---------------------------------------------------------------------------
# 6. InputState: submit returns text
# ---------------------------------------------------------------------------


def test_input_state_submit_returns_text():
    """Feeding chars then ENTER produces a SUBMIT result carrying the typed text."""
    st = InputState()

    r = st.handle(Key.CHAR, "h")
    assert r.kind == InputResultKind.CONTINUE

    r = st.handle(Key.CHAR, "i")
    assert r.kind == InputResultKind.CONTINUE

    r = st.handle(Key.ENTER, "")
    assert r.kind == InputResultKind.SUBMIT
    assert r.text == "hi"


# ---------------------------------------------------------------------------
# 7. Multiline input appears in bottom rows
# ---------------------------------------------------------------------------


def test_multiline_input_in_bottom():
    """A multiline InputState produces multiple rows in the bottom block."""
    composer = FrameComposer()

    input_state = MagicMock()
    input_state.text = "line1\nline2"
    input_state.dropdown_rows = []

    frame = composer.compose(
        transcript=None,
        status=None,
        input_state=input_state,
        cols=80,
    )

    bottom_text = "\n".join(frame.bottom)
    assert "line1" in bottom_text
    assert "line2" in bottom_text


# ---------------------------------------------------------------------------
# 8. force_commit bypasses debounce
# ---------------------------------------------------------------------------


def test_force_commit_commits_immediately():
    """force_commit renders even when MIN_INTERVAL has not elapsed."""
    terminal = FakeTerminal()
    composer = FrameComposer()
    # Use an interval much larger than any realistic test duration.
    loop = RenderLoop(terminal, composer)

    transcript = _mock_transcript(["first"])
    # Do an initial render to set _last_render close to now.
    loop.render(transcript, status=None, input_state=None)
    assert terminal.committed == ["first"]

    # A tick immediately after should be debounced (interval not elapsed).
    transcript.render.return_value = ["first", "second"]
    loop.tick(transcript, status=None, input_state=None)
    # Should still be ["first"] — debounce suppressed the second render.
    assert terminal.committed == ["first"]

    # force_commit bypasses the debounce.
    loop.force_commit(transcript, status=None, input_state=None)
    assert terminal.committed == ["first", "second"]
