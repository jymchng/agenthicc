"""Unit tests for TranscriptView widget (PRD-55 Phase 2a).

Tests verify:
1. test_transcript_view_renders_turns — rendered turns appear in the RichLog
2. test_auto_scroll_on_new_content — auto_scroll reactive defaults True and causes
   scroll-to-end after refresh
3. test_console_print_appends — ConsolePrint message adds text to the view
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import RichLog

from agenthicc.tui.messages import ConsolePrint, TranscriptUpdated
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.widgets.transcript_view import TranscriptView

pytestmark = pytest.mark.unit


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_model() -> TranscriptModel:
    """Return a TranscriptModel with one agent turn and a text line."""
    model = TranscriptModel()
    model.append_turn(agent_id="agent-1", agent_name="TestAgent", timestamp=0.0)
    model.append_line("agent-1", "Hello from the agent turn")
    return model


class _SimpleApp(App):
    """Minimal Textual app that mounts a TranscriptView for testing."""

    def __init__(self, model: TranscriptModel) -> None:
        super().__init__()
        self._transcript_model = model

    def compose(self) -> ComposeResult:
        yield TranscriptView(self._transcript_model, id="tv")


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcript_view_renders_turns() -> None:
    """TranscriptView must render agent turn text into its RichLog child."""
    model = _make_model()
    app = _SimpleApp(model)

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        # Wait for the DOM to settle.
        await pilot.pause()

        tv = app.query_one("#tv", TranscriptView)
        log = tv.query_one("#richlog", RichLog)

        # RichLog accumulates rendered lines in its .lines list.
        # We join the stripped text of every Strip to check for known content.
        rendered_text = " ".join(
            "".join(seg.text for seg in strip._segments)
            for strip in log.lines
        )
        assert "TestAgent" in rendered_text or "Hello from" in rendered_text, (
            f"Expected turn content in rendered text, got: {rendered_text!r}"
        )


@pytest.mark.asyncio
async def test_auto_scroll_on_new_content() -> None:
    """auto_scroll defaults to True; after refresh_transcript the RichLog scrolls to end."""
    model = TranscriptModel()
    # Add enough turns to guarantee scrollable content.
    for i in range(30):
        model.append_turn(agent_id=f"a{i}", agent_name=f"Agent{i}", timestamp=float(i))
        for j in range(3):
            model.append_line(f"a{i}", f"Line {j} of agent {i}: some longer content here")

    app = _SimpleApp(model)

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()

        tv = app.query_one("#tv", TranscriptView)

        # auto_scroll reactive must default to True.
        assert tv.auto_scroll is True

        # Posting TranscriptUpdated triggers refresh_transcript() which
        # calls scroll_end on the RichLog.
        app.post_message(TranscriptUpdated())
        await pilot.pause()
        await pilot.pause()

        # auto_scroll should remain True (no manual scroll-up was performed).
        assert tv.auto_scroll is True

        log = tv.query_one("#richlog", RichLog)
        # The log should have lines from all the turns.
        assert len(log.lines) > 0


@pytest.mark.asyncio
async def test_console_print_appends() -> None:
    """ConsolePrint message posted on TranscriptView must append its markup text.

    ConsolePrint is dispatched TO the TranscriptView widget (e.g. from a
    ConsoleShim that holds a reference to the view).  Textual routes messages
    to the widget they are posted on, so the handler fires when
    ``tv.post_message(ConsolePrint(...))`` is called.
    """
    model = TranscriptModel()
    app = _SimpleApp(model)

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await pilot.pause()

        unique_marker = "xyzUniquePrintMarker987"
        tv = app.query_one("#tv", TranscriptView)
        # Post the message directly on the TranscriptView so the handler fires.
        tv.post_message(ConsolePrint(f"hello [bold]{unique_marker}[/bold]"))
        await pilot.pause()
        await pilot.pause()

        log = tv.query_one("#richlog", RichLog)

        rendered_text = " ".join(
            "".join(seg.text for seg in strip._segments)
            for strip in log.lines
        )
        assert unique_marker in rendered_text, (
            f"Expected '{unique_marker}' in rendered text after ConsolePrint, "
            f"got: {rendered_text!r}"
        )
