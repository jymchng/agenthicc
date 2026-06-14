"""Integration tests for the streaming pipeline: SpinnerPanel + StatusBar (PRD-55 Phase 4).

Tests:
- SpinnerPanel shows tool calls when ToolCallStarted is posted.
- SpinnerPanel updates when ToolCallComplete is posted.
- StatusBar token counts update when TokensUpdated is posted.
- StatusBar becomes hidden when AgentRunFinished is posted.
- Full round-trip: AgentRunStarted → ToolCallStarted → ToolCallComplete → AgentRunFinished.
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Label

from agenthicc.tui.messages import (
    AgentRunFinished,
    AgentRunStarted,
    ToolCallComplete,
    ToolCallStarted,
    TokensUpdated,
)
from agenthicc.tui.widgets.spinner_panel import SpinnerPanel
from agenthicc.tui.widgets.status_bar import StatusBar

pytestmark = pytest.mark.integration


# ── minimal Textual app with both widgets ─────────────────────────────────────


class _PipelineApp(App):
    """Minimal Textual app that mounts SpinnerPanel and StatusBar together."""

    def compose(self) -> ComposeResult:
        yield StatusBar(id="sb")
        yield SpinnerPanel(id="sp")
        # Placeholder content so the app has something visible.
        yield Label("ready", id="lbl")


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spinner_panel_shows_tool_call() -> None:
    """ToolCallStarted posted to the app must appear in SpinnerPanel's render."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)

        app.post_message(AgentRunStarted("agent-1", "claude"))
        await pilot.pause()
        await pilot.pause()

        # Post ToolCallStarted — message bubbles up to SpinnerPanel.
        sp.post_message(ToolCallStarted("tid-A", "read_file", {"path": "main.py"}))
        await pilot.pause()
        await pilot.pause()

        assert "tid-A" in sp._tool_calls
        rendered = sp.render()
        assert "read_file" in rendered


@pytest.mark.asyncio
async def test_spinner_panel_updates_on_complete() -> None:
    """ToolCallComplete must flip done=True and ok reflects success flag."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)

        app.post_message(AgentRunStarted("agent-2", "claude"))
        await pilot.pause()

        sp.post_message(ToolCallStarted("tid-B", "write_file", {"path": "out.py"}))
        await pilot.pause()
        await pilot.pause()

        sp.post_message(
            ToolCallComplete("tid-B", success=True, duration_ms=77.0, error=None, diff=None)
        )
        await pilot.pause()
        await pilot.pause()

        entry = sp._tool_calls.get("tid-B")
        assert entry is not None
        assert entry["done"] is True
        assert entry["ok"] is True
        rendered = sp.render()
        assert "✓" in rendered


@pytest.mark.asyncio
async def test_status_bar_tokens_update() -> None:
    """After TokensUpdated the StatusBar rendered text must include the token count."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sb = app.query_one("#sb", StatusBar)

        # Post directly to the StatusBar widget so on_agent_run_started fires.
        sb.post_message(AgentRunStarted("agent-3", "claude"))
        await pilot.pause()
        await pilot.pause()

        # The widget should be active now.
        assert "active" in sb.classes

        sb.post_message(TokensUpdated(input_tokens=512, output_tokens=256, cost_usd=0.001))
        await pilot.pause()
        await pilot.pause()

        rendered = sb.render()
        assert "512" in rendered


@pytest.mark.asyncio
async def test_status_bar_hides_on_agent_run_finished() -> None:
    """AgentRunFinished must hide StatusBar (remove 'active' class)."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sb = app.query_one("#sb", StatusBar)

        sb.post_message(AgentRunStarted("agent-4", "claude"))
        await pilot.pause()
        await pilot.pause()
        assert "active" in sb.classes

        sb.post_message(AgentRunFinished())
        await pilot.pause()
        await pilot.pause()
        assert "active" not in sb.classes


@pytest.mark.asyncio
async def test_full_turn_pipeline() -> None:
    """Full round-trip: AgentRunStarted → tool calls → AgentRunFinished."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sb = app.query_one("#sb", StatusBar)
        sp = app.query_one("#sp", SpinnerPanel)

        # Agent starts.
        sb.post_message(AgentRunStarted("agent-5", "claude-3-5"))
        await pilot.pause()
        await pilot.pause()
        assert "active" in sb.classes

        # Tool call 1.
        sp.post_message(ToolCallStarted("t1", "read_file", {"path": "x.py"}))
        await pilot.pause()
        await pilot.pause()

        # Tool call 2.
        sp.post_message(ToolCallStarted("t2", "run_bash", {"cmd": "ls"}))
        await pilot.pause()
        await pilot.pause()

        rendered = sp.render()
        assert "read_file" in rendered
        assert "run_bash" in rendered

        # Complete tool 1 (success).
        sp.post_message(ToolCallComplete("t1", success=True, duration_ms=30.0, error=None, diff=None))
        await pilot.pause()
        await pilot.pause()

        # Complete tool 2 (failure).
        sp.post_message(ToolCallComplete("t2", success=False, duration_ms=5.0, error="exit code 1", diff=None))
        await pilot.pause()
        await pilot.pause()

        rendered = sp.render()
        assert "✓" in rendered   # read_file succeeded
        assert "✗" in rendered   # run_bash failed

        # Token update.
        sb.post_message(TokensUpdated(300, 150, 0.002))
        await pilot.pause()
        await pilot.pause()
        assert "300" in sb.render()

        # Agent finishes → StatusBar goes idle.
        sb.post_message(AgentRunFinished())
        await pilot.pause()
        await pilot.pause()
        assert "active" not in sb.classes


@pytest.mark.asyncio
async def test_spinner_panel_hidden_until_started() -> None:
    """SpinnerPanel must be hidden (no 'active' class) before any agent turn."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)
        await pilot.pause()
        assert "active" not in sp.classes


@pytest.mark.asyncio
async def test_status_bar_hidden_until_started() -> None:
    """StatusBar must be hidden (no 'active' class) before any agent turn."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sb = app.query_one("#sb", StatusBar)
        await pilot.pause()
        assert "active" not in sb.classes


@pytest.mark.asyncio
async def test_spinner_panel_failure_tool_call() -> None:
    """A failed tool call (success=False) must render with '✗' marker."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)

        sp.post_message(ToolCallStarted("tid-fail", "run_bash", {"cmd": "bad"}))
        await pilot.pause()
        await pilot.pause()

        sp.post_message(
            ToolCallComplete("tid-fail", success=False, duration_ms=2.0, error="exit 1", diff=None)
        )
        await pilot.pause()
        await pilot.pause()

        rendered = sp.render()
        assert "✗" in rendered
        assert "✓" not in rendered


@pytest.mark.asyncio
async def test_spinner_panel_diff_in_complete() -> None:
    """ToolCallComplete with diff text must render diff preview lines."""
    app = _PipelineApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)

        sp.post_message(ToolCallStarted("tid-diff", "write_file", {"path": "foo.py"}))
        await pilot.pause()
        await pilot.pause()

        diff_text = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1 +1,2 @@\n"
            " existing\n"
            "+added line\n"
        )
        sp.post_message(
            ToolCallComplete("tid-diff", success=True, duration_ms=8.0, error=None, diff=diff_text)
        )
        await pilot.pause()
        await pilot.pause()

        rendered = sp.render()
        assert "+added line" in rendered
