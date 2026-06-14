"""Integration tests for AgenthiccApp using Textual Pilot (PRD-55 Phase 7B).

Tests verify widget interactions across the full widget tree:
  - InputPanel submit cycle
  - StatusBar visibility during agent runs
  - TranscriptView refresh after _flush_new_lines()
  - TriggerMenu appears in app context
  - ModeFooter text changes on mode cycle
  - Command modal opens via /status
  - Pending queue count shown while streaming

All tests use textual.testing.Pilot (headless mode).

Note: AgenthiccApp replaces self.console with ConsoleShim, which breaks
Textual's internal rendering. We use TestAgenthiccApp (a thin subclass that
restores Textual's real console after init) so run_test() works correctly.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.messages import (
    AgentRunFinished,
    AgentRunStarted,
    InputSubmitted,
    ModeCycled,
    PendingQueueUpdated,
    ToolCallComplete,
    ToolCallStarted,
    TokensUpdated,
    TranscriptUpdated,
)

pytestmark = pytest.mark.integration


# ── Test-safe app wrapper ─────────────────────────────────────────────────────


class TestAgenthiccApp:
    """Helper that builds an AgenthiccApp with a restored Textual console.

    AgenthiccApp.__init__ replaces self.console with ConsoleShim, which
    prevents Textual's rendering pipeline from working (it needs a real
    rich.Console with console_options, render, etc.).

    This factory restores Textual's real console after AgenthiccApp.__init__
    so that run_test() works in headless mode.
    """

    @staticmethod
    def create(model: TranscriptModel | None = None) -> "AgenthiccApp":  # type: ignore[name-defined]
        from agenthicc.tui.app import AgenthiccApp

        if model is None:
            model = TranscriptModel()

        app = AgenthiccApp(model=model, base_path=".")
        # Restore Textual's real Console so rendering works in tests.
        # We keep the ConsoleShim in _console_shim for tests that need it.
        import textual
        from rich.console import Console
        real_console = Console(highlight=False, markup=True)
        object.__setattr__(app, "console", real_console)
        return app


def _make_app(model: TranscriptModel | None = None):
    """Create a test-safe AgenthiccApp."""
    if model is None:
        model = TranscriptModel()
    app = TestAgenthiccApp.create(model)
    return app, model


# ── Test 1: full input submit cycle ──────────────────────────────────────────


@pytest.mark.integration
async def test_full_input_submit_cycle() -> None:
    """Type text and press Enter — InputSubmitted fires and on_intent is called."""
    from agenthicc.tui.widgets.input_panel import InputPanel

    intent_received: list[str] = []

    async def mock_intent(text: str) -> None:
        intent_received.append(text)

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        app._on_intent = mock_intent
        panel = app.query_one(InputPanel)
        panel.focus()

        # Type "hello world" character by character.
        for ch in "hello world":
            await pilot.press(ch)
        await pilot.press("enter")

        # Give the event loop a moment to process messages.
        await pilot.pause(0.1)

    assert intent_received == ["hello world"]


# ── Test 2: StatusBar visible during agent run ────────────────────────────────


@pytest.mark.integration
async def test_status_bar_during_agent_run() -> None:
    """AgentRunStarted makes StatusBar active; AgentRunFinished deactivates it."""
    from agenthicc.tui.widgets.status_bar import StatusBar

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        bar = app.query_one(StatusBar)

        # Initially inactive.
        assert bar.active is False

        # Post AgentRunStarted.
        app.post_message(AgentRunStarted(agent_id="agent-1", model_short="claude"))
        await pilot.pause(0.05)

        assert bar.active is True, "StatusBar should be active after AgentRunStarted"

        # Post AgentRunFinished.
        app.post_message(AgentRunFinished())
        await pilot.pause(0.05)

        assert bar.active is False, "StatusBar should be inactive after AgentRunFinished"


# ── Test 3: TranscriptView updates after _flush_new_lines() ───────────────────


@pytest.mark.integration
async def test_transcript_updates_after_flush() -> None:
    """_flush_new_lines() posts TranscriptUpdated which refreshes TranscriptView."""
    from agenthicc.tui.widgets.transcript_view import TranscriptView
    from textual.widgets import RichLog

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        tv = app.query_one(TranscriptView)
        richlog = tv.query_one("#transcript-richlog", RichLog)

        # Count lines before.
        initial_count = len(richlog.lines)

        # Add content to the model.
        turn = model.append_turn("agent-1", "TestAgent")
        turn.lines.append("Hello from agent!")

        # Signal a flush (posts TranscriptUpdated).
        app._flush_new_lines()
        await pilot.pause(0.1)

        # The RichLog should have at least one more line.
        final_count = len(richlog.lines)
        assert final_count > initial_count, (
            f"RichLog line count should have increased; was {initial_count}, now {final_count}"
        )


# ── Test 4: TriggerMenu shows in app context ──────────────────────────────────


@pytest.mark.integration
async def test_trigger_menu_in_app_context() -> None:
    """Pressing '@' in InputPanel shows TriggerMenu."""
    from agenthicc.tui.widgets.input_panel import InputPanel
    from agenthicc.tui.widgets.trigger_menu import TriggerMenu

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        panel = app.query_one(InputPanel)
        panel.focus()
        menu = panel._trigger_menu()

        # Initially hidden.
        assert menu.display is False

        # Press '@' — AtMentionTrigger is registered and should activate.
        await pilot.press("@")
        await pilot.pause(0.05)

        assert menu.display is True, "TriggerMenu should be visible after '@' press"


# ── Test 5: mode cycling updates ModeFooter ───────────────────────────────────


@pytest.mark.integration
async def test_mode_cycling_updates_footer() -> None:
    """Shift+Tab cycles mode and ModeFooter text changes accordingly."""
    from agenthicc.tui.widgets.input_panel import InputPanel
    from agenthicc.tui.widgets.mode_footer import ModeFooter

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        panel = app.query_one(InputPanel)
        panel.focus()
        footer = panel.query_one(ModeFooter)

        initial_name = footer.mode_name

        # Press Shift+Tab to cycle mode.
        await pilot.press("shift+tab")
        await pilot.pause(0.1)

        # Mode name should have changed OR a notification was set.
        new_name = footer.mode_name
        mode_changed = (new_name != initial_name)
        notification_set = (footer.notification is not None)
        assert mode_changed or notification_set, (
            f"ModeFooter should update on Shift+Tab; name={new_name!r}, notif={footer.notification!r}"
        )


# ── Test 6: /status command opens AgentStatusModal ────────────────────────────


@pytest.mark.integration
async def test_command_modal_opens() -> None:
    """Dispatching /status via SlashCommandHandler pushes AgentStatusModal."""
    from agenthicc.tui.app import SlashCommandHandler
    from agenthicc.tui.widgets.command_modals import AgentStatusModal

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        handler = SlashCommandHandler(renderer=app)
        handled = handler.handle("/status", model, MagicMock())
        assert handled is True, "/status should be handled"

        await pilot.pause(0.1)

        # The screen stack should now have AgentStatusModal on top.
        screens = app.screen_stack
        assert len(screens) >= 2, "AgentStatusModal should be on the screen stack"
        assert isinstance(screens[-1], AgentStatusModal), (
            f"Top screen should be AgentStatusModal, got {type(screens[-1]).__name__}"
        )

        # Dismiss the modal.
        await pilot.press("escape")
        await pilot.pause(0.05)


# ── Test 7: pending queue count shown during streaming ────────────────────────


@pytest.mark.integration
async def test_pending_queue_during_streaming() -> None:
    """PendingQueueUpdated posts update app.pending_queue_count and ModeFooter."""
    from agenthicc.tui.widgets.input_panel import InputPanel
    from agenthicc.tui.widgets.mode_footer import ModeFooter

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        panel = app.query_one(InputPanel)
        footer = panel.query_one(ModeFooter)

        # Initially no pending queue.
        assert app.pending_queue_count == 0

        # Post PendingQueueUpdated with count=2.
        app.post_message(PendingQueueUpdated(count=2))
        await pilot.pause(0.1)

        assert app.pending_queue_count == 2
        # Footer notification should mention the queued count.
        assert footer.notification is not None, "Footer should show queue notification"
        notif_text = footer.notification
        assert "2" in notif_text or "queued" in notif_text.lower(), (
            f"Notification should mention '2' or 'queued', got: {notif_text!r}"
        )

        # Reset to 0.
        app.post_message(PendingQueueUpdated(count=0))
        await pilot.pause(0.1)
        assert app.pending_queue_count == 0
        assert footer.notification is None, "Footer notification should clear when count is 0"


# ── Test 8: TokensUpdated propagates to StatusBar ────────────────────────────


@pytest.mark.integration
async def test_tokens_updated_propagates_to_status_bar() -> None:
    """TokensUpdated message increments StatusBar token counts."""
    from agenthicc.tui.widgets.status_bar import StatusBar

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        bar = app.query_one(StatusBar)

        assert bar.input_tokens == 0
        assert bar.output_tokens == 0

        # Start agent run first so the bar is listening.
        app.post_message(AgentRunStarted(agent_id="a1", model_short="claude"))
        await pilot.pause(0.05)

        app.post_message(TokensUpdated(input_tokens=500, output_tokens=100, cost_usd=0.005))
        await pilot.pause(0.1)

        assert bar.input_tokens == 500
        assert bar.output_tokens == 100


# ── Test 9: ToolCallStarted mounts SpinnerPanel ───────────────────────────────


@pytest.mark.integration
async def test_tool_call_mounts_spinner_panel() -> None:
    """ToolCallStarted causes SpinnerPanel to be mounted inside TranscriptView."""
    from agenthicc.tui.widgets.spinner_panel import SpinnerPanel

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        # Initially no SpinnerPanel mounted.
        assert not app._spinner_mounted

        # Post AgentRunStarted then ToolCallStarted.
        app.post_message(AgentRunStarted(agent_id="a2", model_short="claude"))
        await pilot.pause(0.05)

        app.post_message(ToolCallStarted(tool_use_id="tc1", name="git_status", args={}))
        await pilot.pause(0.1)

        assert app._spinner_mounted is True, "SpinnerPanel should be mounted after ToolCallStarted"

        # Verify the SpinnerPanel itself tracks the tool call.
        spinner = app.query_one(SpinnerPanel)
        assert "tc1" in spinner._tool_calls


# ── Test 10: ToolCallComplete unmounts SpinnerPanel when agent finishes ───────


@pytest.mark.integration
async def test_spinner_unmounted_on_agent_finished() -> None:
    """AgentRunFinished unmounts SpinnerPanel."""
    from agenthicc.tui.widgets.spinner_panel import SpinnerPanel

    app, model = _make_app()

    async with app.run_test(headless=True) as pilot:
        # Mount the spinner by starting an agent run + tool call.
        app.post_message(AgentRunStarted(agent_id="a3", model_short="claude"))
        await pilot.pause(0.05)
        app.post_message(ToolCallStarted(tool_use_id="tc2", name="read_file", args={}))
        await pilot.pause(0.1)
        assert app._spinner_mounted is True

        # Finish the agent run — should unmount SpinnerPanel.
        app.post_message(AgentRunFinished())
        await pilot.pause(0.1)

        assert app._spinner_mounted is False, "SpinnerPanel should be unmounted after AgentRunFinished"
