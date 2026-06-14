"""Unit tests for StatusState and Textual StatusBar widget (PRD-20)."""
from __future__ import annotations
import pytest
from agenthicc.tui.app import StatusState

pytestmark = pytest.mark.unit


class TestStatusState:
    def test_defaults(self):
        s = StatusState()
        assert s.active is False
        assert s.spinner_frame == 0
        assert s.input_tokens == 0
        assert s.output_tokens == 0
        assert s.session_cost_usd == 0.0
        assert s.completed_agents == 0
        assert s.session_id == ""

    def test_mutable_fields(self):
        s = StatusState()
        s.active = True
        s.input_tokens = 500
        assert s.active and s.input_tokens == 500


# ── Textual StatusBar widget tests (PRD-55 Phase 2b) ─────────────────────────


from textual.app import App, ComposeResult  # noqa: E402
from agenthicc.tui.widgets.status_bar import StatusBar  # noqa: E402
from agenthicc.tui.messages import AgentRunStarted, AgentRunFinished, TokensUpdated  # noqa: E402


class _StatusBarApp(App):
    """Minimal Textual app that hosts a single StatusBar for testing."""

    def compose(self) -> ComposeResult:
        yield StatusBar()


async def test_status_bar_shows_idle_by_default():
    """StatusBar is always visible; idle state shows 'Idle' in render."""
    app = _StatusBarApp()
    async with app.run_test() as pilot:
        sb = app.query_one(StatusBar)
        rendered = sb.render()
        assert "Idle" in rendered


async def test_status_bar_shows_on_agent_run():
    """Posting AgentRunStarted changes agent_state to 'thinking'."""
    app = _StatusBarApp()
    async with app.run_test() as pilot:
        sb = app.query_one(StatusBar)
        sb.post_message(AgentRunStarted("agent-1", "claude"))
        await pilot.pause()
        await pilot.pause()
        assert sb.agent_state in ("thinking", "running")


async def test_status_bar_updates_tokens():
    """After TokensUpdated(100, 50, 0.01) the rendered output must contain '100'."""
    app = _StatusBarApp()
    async with app.run_test() as pilot:
        sb = app.query_one(StatusBar)
        sb.post_message(AgentRunStarted("agent-1", "claude"))
        await pilot.pause()
        await pilot.pause()
        sb.post_message(TokensUpdated(100, 50, 0.01))
        await pilot.pause()
        await pilot.pause()
        rendered = sb.render()
        assert "150" in rendered  # 100 input + 50 output = 150 total


async def test_status_bar_returns_to_idle_on_finish():
    """Posting AgentRunFinished returns agent_state to 'idle'."""
    app = _StatusBarApp()
    async with app.run_test() as pilot:
        sb = app.query_one(StatusBar)
        sb.post_message(AgentRunStarted("agent-1", "claude"))
        await pilot.pause()
        await pilot.pause()
        sb.post_message(AgentRunFinished())
        await pilot.pause()
        await pilot.pause()
        assert sb.agent_state == "idle"
