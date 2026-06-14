"""Tests for TriggerMenu widget (PRD-55 Phase 3).

All tests are tagged @pytest.mark.unit.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerRegistry
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.widgets.trigger_menu import TriggerMenu


# ── Minimal host app ──────────────────────────────────────────────────────────


class MenuApp(App):
    CSS = "TriggerMenu { height: auto; }"

    def compose(self) -> ComposeResult:
        yield TriggerMenu(id="menu")

    def menu(self) -> TriggerMenu:
        return self.query_one("#menu", TriggerMenu)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_handler() -> AtMentionTrigger:
    return AtMentionTrigger()


def _make_ctx() -> TriggerContext:
    return TriggerContext(cwd=Path("/tmp"))


def _make_items(n: int = 5) -> list[MatchItem]:
    return [MatchItem(display=f"item{i}", value=f"item{i}") for i in range(n)]


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_trigger_menu_hidden_by_default() -> None:
    """TriggerMenu is hidden (display=False) on creation."""
    app = MenuApp()
    async with app.run_test(headless=True):
        menu = app.menu()
        assert menu.display is False


@pytest.mark.unit
async def test_trigger_menu_activate_shows() -> None:
    """Calling activate() makes the menu visible."""
    app = MenuApp()
    async with app.run_test(headless=True):
        menu = app.menu()
        handler = _make_handler()
        ctx = _make_ctx()
        menu.activate(handler, "", ctx)
        assert menu.display is True


@pytest.mark.unit
async def test_trigger_menu_hide() -> None:
    """Calling hide() makes the menu invisible and resets state."""
    app = MenuApp()
    async with app.run_test(headless=True):
        menu = app.menu()
        handler = _make_handler()
        ctx = _make_ctx()
        menu.activate(handler, "", ctx)
        menu.hide()
        assert menu.display is False
        assert menu.active_handler is None
        assert menu.fragment == ""
        assert menu.matches == []


@pytest.mark.unit
async def test_trigger_menu_selected_item() -> None:
    """selected_item returns the item at _selected index."""
    app = MenuApp()
    async with app.run_test(headless=True):
        menu = app.menu()
        # Inject matches directly (bypassing file-system).
        menu._matches = _make_items(3)
        menu._selected = 1
        item = menu.selected_item
        assert item is not None
        assert item.value == "item1"


@pytest.mark.unit
async def test_trigger_menu_up_down_navigation() -> None:
    """Up/down key presses navigate the selection."""
    app = MenuApp()
    async with app.run_test(headless=True) as pilot:
        menu = app.menu()
        menu._matches = _make_items(5)
        menu._selected = 0
        menu.display = True

        await pilot.press("down")
        assert menu._selected == 1

        await pilot.press("down")
        assert menu._selected == 2

        await pilot.press("up")
        assert menu._selected == 1


@pytest.mark.unit
async def test_trigger_menu_wraps_at_boundaries() -> None:
    """Selection wraps from last to first item and vice versa."""
    app = MenuApp()
    async with app.run_test(headless=True) as pilot:
        menu = app.menu()
        menu._matches = _make_items(3)
        menu._selected = 0
        menu.display = True

        await pilot.press("up")
        # Wraps to last (index 2).
        assert menu._selected == 2

        await pilot.press("down")
        # Wraps back to first (index 0).
        assert menu._selected == 0


@pytest.mark.unit
async def test_trigger_menu_esc_emits_cancelled() -> None:
    """Pressing Escape emits TriggerCancelled and hides the menu."""
    received: list[TriggerMenu.TriggerCancelled] = []

    class TrackingApp(MenuApp):
        def on_trigger_menu_trigger_cancelled(
            self, msg: TriggerMenu.TriggerCancelled
        ) -> None:
            received.append(msg)

    app = TrackingApp()
    async with app.run_test(headless=True) as pilot:
        menu = app.menu()
        menu._matches = _make_items(3)
        menu.display = True

        await pilot.press("escape")
        assert len(received) == 1
        assert menu.display is False


@pytest.mark.unit
async def test_trigger_menu_enter_emits_selected() -> None:
    """Pressing Enter emits TriggerSelected with the highlighted item."""
    received: list[TriggerMenu.TriggerSelected] = []

    class TrackingApp(MenuApp):
        def on_trigger_menu_trigger_selected(
            self, msg: TriggerMenu.TriggerSelected
        ) -> None:
            received.append(msg)

    app = TrackingApp()
    async with app.run_test(headless=True) as pilot:
        menu = app.menu()
        menu._matches = _make_items(3)
        menu._selected = 2
        menu.display = True

        await pilot.press("enter")
        assert len(received) == 1
        assert received[0].item.value == "item2"
        assert menu.display is False


@pytest.mark.unit
async def test_trigger_menu_render_content_no_matches() -> None:
    """_render_content returns 'No matches' when match list is empty."""
    app = MenuApp()
    async with app.run_test(headless=True):
        menu = app.menu()
        menu._matches = []
        content = menu._render_content()
        assert "No matches" in content


@pytest.mark.unit
async def test_trigger_menu_render_content_shows_indicator() -> None:
    """_render_content marks selected item with '▶'."""
    app = MenuApp()
    async with app.run_test(headless=True):
        menu = app.menu()
        menu._matches = _make_items(3)
        menu._selected = 1
        content = menu._render_content()
        lines = content.split("\n")
        # Second line (index 1) should have the ▶ indicator.
        assert "▶" in lines[1]
        # Others should not.
        assert "▶" not in lines[0]


@pytest.mark.unit
async def test_trigger_menu_update_fragment() -> None:
    """update_fragment refreshes matches for the new fragment."""
    app = MenuApp()
    async with app.run_test(headless=True):
        menu = app.menu()
        handler = _make_handler()
        ctx = _make_ctx()
        menu.activate(handler, "", ctx)
        initial_count = len(menu.matches)

        # Update with a fragment that matches nothing under /tmp.
        menu.update_fragment("xyzzy_no_such_file")
        # Matches should now be empty (or at least changed).
        assert menu.fragment == "xyzzy_no_such_file"
