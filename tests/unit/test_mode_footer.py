"""Unit tests for ModeFooter widget (PRD-55 Phase 2c).

Four test scenarios:
1. test_mode_footer_default_render — default mode text appears in render()
2. test_mode_footer_notification_overrides — notification text overrides mode text
3. test_mode_footer_notification_clears — notification clears after 2s timer fires
4. test_mode_cycled_updates_badge — on_mode_cycled updates badge and name in render
"""
from __future__ import annotations

import pytest

from agenthicc.tui.input_area import _NEW_LINE_HINT
from agenthicc.tui.messages import ModeCycled
from agenthicc.tui.widgets.mode_footer import ModeFooter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_widget() -> ModeFooter:
    """Return a bare ModeFooter instance (not mounted in an app)."""
    return ModeFooter()


# ---------------------------------------------------------------------------
# 1. Default render
# ---------------------------------------------------------------------------


def test_mode_footer_default_render() -> None:
    """Default state renders badge, name, and cycle hint."""
    widget = _make_widget()

    rendered = widget.render()

    # Default values
    assert "⏵⏵" in rendered
    assert "Auto" in rendered
    assert "shift+tab to cycle" in rendered
    # New-line hint from input_area
    assert _NEW_LINE_HINT.strip() in rendered
    # Notification must not appear
    assert "Switched" not in rendered


def test_mode_footer_default_notification_is_none() -> None:
    """Default notification reactive is None."""
    widget = _make_widget()
    assert widget.notification is None


def test_mode_footer_default_mode_name() -> None:
    widget = _make_widget()
    assert widget.mode_name == "Auto"


def test_mode_footer_default_badge() -> None:
    widget = _make_widget()
    assert widget.mode_badge == "⏵⏵"


# ---------------------------------------------------------------------------
# 2. Notification overrides mode text
# ---------------------------------------------------------------------------


def test_mode_footer_notification_overrides() -> None:
    """When notification is set, render shows notification text, not mode text."""
    widget = _make_widget()
    widget.notification = "Press Ctrl+C again to interrupt"

    rendered = widget.render()

    assert "Press Ctrl+C again to interrupt" in rendered
    # Mode badge / name should NOT appear when notification is active
    assert "shift+tab to cycle" not in rendered


def test_mode_footer_set_notification_api() -> None:
    """set_notification() updates notification attribute."""
    widget = _make_widget()
    widget.set_notification("Paste detected")

    assert widget.notification == "Paste detected"
    rendered = widget.render()
    assert "Paste detected" in rendered


def test_mode_footer_set_notification_none_clears() -> None:
    """set_notification(None) clears the notification."""
    widget = _make_widget()
    widget.notification = "some warning"
    widget.set_notification(None)

    assert widget.notification is None
    rendered = widget.render()
    assert "some warning" not in rendered
    # Back to default mode line
    assert "shift+tab to cycle" in rendered


# ---------------------------------------------------------------------------
# 3. Notification clears after timer (full Textual app test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_footer_notification_clears() -> None:
    """Notification is cleared after the 2s timer fires."""
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ModeFooter()

    app = _App()
    async with app.run_test(headless=True) as pilot:
        widget = pilot.app.query_one(ModeFooter)

        # Set notification directly (no timer involved yet)
        widget.notification = "Test notification"
        assert widget.notification == "Test notification"

        # Simulate what on_mode_cycled does: schedule a clear timer
        # Call _clear_notification directly to mimic timer firing
        widget._clear_notification()

        assert widget.notification is None
        rendered = widget.render()
        assert "shift+tab to cycle" in rendered


# ---------------------------------------------------------------------------
# 4. ModeCycled updates badge and render
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_cycled_updates_badge() -> None:
    """Posting ModeCycled changes mode_badge, mode_name, and render output."""
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ModeFooter()

    app = _App()
    async with app.run_test(headless=True) as pilot:
        widget = pilot.app.query_one(ModeFooter)

        # Verify default state first
        assert widget.mode_name == "Auto"
        assert widget.mode_badge == "⏵⏵"

        # Post the ModeCycled message to the widget
        msg = ModeCycled(new_name="Plan", new_badge="📋")
        await widget._on_message(msg)

        assert widget.mode_name == "Plan"
        assert widget.mode_badge == "📋"
        # Notification should be set
        assert widget.notification is not None
        assert "Plan" in widget.notification

        # While notification is active, render shows it
        rendered = widget.render()
        assert "Plan" in rendered

        # Clear notification to check mode line
        widget._clear_notification()
        rendered_after = widget.render()
        assert "📋" in rendered_after
        assert "Plan" in rendered_after


@pytest.mark.asyncio
async def test_mode_cycled_notification_content() -> None:
    """ModeCycled notification message contains the new mode name."""
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ModeFooter()

    app = _App()
    async with app.run_test(headless=True) as pilot:
        widget = pilot.app.query_one(ModeFooter)

        msg = ModeCycled(new_name="Edit", new_badge="✏")
        await widget._on_message(msg)

        assert widget.notification is not None
        assert "Edit" in widget.notification
        rendered = widget.render()
        assert "Edit" in rendered


# ---------------------------------------------------------------------------
# 5. render() format checks
# ---------------------------------------------------------------------------


def test_render_uses_dim_markup() -> None:
    """render() wraps content in [dim]...[/dim] Rich markup."""
    widget = _make_widget()
    rendered = widget.render()
    assert "[dim]" in rendered
    assert "[/dim]" in rendered


def test_render_notification_uses_dim_markup() -> None:
    """Notification render also uses [dim] markup."""
    widget = _make_widget()
    widget.set_notification("warning!")
    rendered = widget.render()
    assert "[dim]" in rendered
    assert "warning!" in rendered


def test_render_leading_spaces() -> None:
    """render() has leading spaces for left margin (per spec '  ...')."""
    widget = _make_widget()
    rendered = widget.render()
    assert rendered.startswith("  ")


def test_mode_name_change_reflects_in_render() -> None:
    """Changing mode_name reactive updates render output."""
    widget = _make_widget()
    widget.mode_name = "Search"
    rendered = widget.render()
    assert "Search" in rendered


def test_mode_badge_change_reflects_in_render() -> None:
    """Changing mode_badge reactive updates render output."""
    widget = _make_widget()
    widget.mode_badge = "🔍"
    rendered = widget.render()
    assert "🔍" in rendered


# ---------------------------------------------------------------------------
# 6. ModeFooter importable from widgets package
# ---------------------------------------------------------------------------


def test_mode_footer_importable_from_widgets_package() -> None:
    """ModeFooter must be exported from agenthicc.tui.widgets."""
    from agenthicc.tui.widgets import ModeFooter as MF  # noqa: PLC0415

    assert MF is ModeFooter
