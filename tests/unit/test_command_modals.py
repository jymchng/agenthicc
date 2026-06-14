"""Unit tests for CommandModals (PRD-55 Phase 5).

Tests cover:
  1. AgentStatusModal renders agent turns as DataTable rows
  2. HistoryModal renders transcript lines into a RichLog
  3. HelpModal shows /status in the DataTable
  4. Escape dismisses any modal
  5. Pressing q dismisses any modal
"""
from __future__ import annotations

import pytest

from agenthicc.tui.transcript import TranscriptModel

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_model_with_turns(n: int = 2) -> TranscriptModel:
    model = TranscriptModel()
    for i in range(n):
        turn = model.append_turn(
            agent_id=f"agent-{i:04x}0000000000000000000000000000",
            agent_name=f"Agent{i}",
        )
        turn.cost_usd = 0.001 * (i + 1)
        turn.tokens = 100 * (i + 1)
    return model


# ── test_status_modal_shows_turns ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_modal_shows_turns() -> None:
    """AgentStatusModal DataTable must have one row per agent turn."""
    from textual.app import App, ComposeResult

    from agenthicc.tui.widgets.command_modals import AgentStatusModal

    model = _make_model_with_turns(2)

    class _TestApp(App):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(AgentStatusModal(model))

    async with _TestApp().run_test(headless=True) as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("DataTable")
        # 2 data rows + 1 header row; row_count counts data rows only
        assert table.row_count == 2


# ── test_history_modal_shows_lines ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_modal_shows_lines() -> None:
    """HistoryModal RichLog must contain content from the transcript."""
    from textual.app import App, ComposeResult

    from agenthicc.tui.widgets.command_modals import HistoryModal

    model = _make_model_with_turns(1)
    model.append_line(model.turns[0].agent_id, "Hello from Agent0")

    class _TestApp(App):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(HistoryModal(model))

    async with _TestApp().run_test(headless=True) as pilot:
        await pilot.pause()
        log = pilot.app.screen.query_one("RichLog")
        # RichLog has at least one line written (the agent header or the text line)
        assert log is not None
        # Verify lines were written by checking the log's lines attribute
        assert len(log.lines) > 0


# ── test_help_modal_shows_commands ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_modal_shows_commands() -> None:
    """/status must appear in the HelpModal DataTable."""
    from textual.app import App, ComposeResult

    from agenthicc.commands import build_builtin_registry
    from agenthicc.tui.widgets.command_modals import HelpModal

    registry = build_builtin_registry()

    class _TestApp(App):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(HelpModal(registry=registry))

    async with _TestApp().run_test(headless=True) as pilot:
        await pilot.pause()
        # At least one DataTable must be rendered
        tables = pilot.app.screen.query("DataTable")
        assert len(tables) > 0

        # Collect all cell values from all tables and verify /status is present
        found_status = False
        for table in tables:
            for row_key in table.rows:
                row = table.get_row(row_key)
                if any("/status" in str(cell) for cell in row):
                    found_status = True
                    break
            if found_status:
                break

        assert found_status, "/status not found in HelpModal tables"


# ── test_escape_dismisses_modal ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_escape_dismisses_modal() -> None:
    """Pressing Escape must pop the modal from the screen stack."""
    from textual.app import App, ComposeResult

    from agenthicc.tui.widgets.command_modals import AgentStatusModal

    model = _make_model_with_turns(0)

    class _TestApp(App):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(AgentStatusModal(model))

    async with _TestApp().run_test(headless=True) as pilot:
        await pilot.pause()
        # Modal is on the screen stack
        assert isinstance(pilot.app.screen, AgentStatusModal)
        # Press Escape
        await pilot.press("escape")
        await pilot.pause()
        # Modal should be dismissed — screen is back to default
        assert not isinstance(pilot.app.screen, AgentStatusModal)


# ── test_modal_keyboard_q ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modal_keyboard_q() -> None:
    """Pressing q must dismiss any modal, just like Escape."""
    from textual.app import App, ComposeResult

    from agenthicc.tui.widgets.command_modals import HistoryModal

    model = _make_model_with_turns(0)

    class _TestApp(App):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(HistoryModal(model))

    async with _TestApp().run_test(headless=True) as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, HistoryModal)
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, HistoryModal)
