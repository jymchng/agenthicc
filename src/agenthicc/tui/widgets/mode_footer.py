"""ModeFooter widget — 1-row mode indicator pinned to the bottom of InputPanel.

Displays the current input mode badge and name, or a transient notification
that auto-clears after 2 seconds.  Listens for ModeCycled messages from
the Textual message bus.
"""
from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from agenthicc.tui.input_area import _NEW_LINE_HINT
from agenthicc.tui.messages import ModeCycled

__all__ = ["ModeFooter"]


class ModeFooter(Static):
    """Single-row footer widget that shows the current mode and transient notifications.

    Layout position: last row of InputPanel (height: 1, set in theme.css).

    Reactive properties update the rendered text via Textual's reactive system;
    the widget calls ``refresh()`` automatically when any reactive changes.
    """

    mode_name: reactive[str] = reactive("Auto")
    mode_badge: reactive[str] = reactive("⏵⏵")
    notification: reactive[str | None] = reactive(None)

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._notif_reset_handle: object | None = None

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render(self) -> str:
        """Return a Rich markup string for this widget's single row."""
        if self.notification is not None:
            return f"  [dim]{self.notification}[/dim]"
        badge = self.mode_badge
        name = self.mode_name
        hint = _NEW_LINE_HINT
        return f"  [dim]{badge} {name}  (shift+tab to cycle){hint}[/dim]"

    # ── Message handlers ──────────────────────────────────────────────────────

    def on_mode_cycled(self, event: ModeCycled) -> None:
        """Update mode display and show a transient notification."""
        event.stop()
        self.mode_name = event.new_name
        self.mode_badge = event.new_badge
        self.notification = f"Switched to {event.new_name} mode"

        # Cancel any outstanding reset timer before starting a new one.
        if self._notif_reset_handle is not None:
            try:
                self._notif_reset_handle.stop()
            except Exception:  # noqa: BLE001
                pass
            self._notif_reset_handle = None

        self._notif_reset_handle = self.set_timer(
            2.0, self._clear_notification
        )

    def _clear_notification(self) -> None:
        """Clear the transient notification (called by the 2s timer)."""
        self.notification = None
        self._notif_reset_handle = None

    # ── Public API ────────────────────────────────────────────────────────────

    def set_notification(self, text: str | None) -> None:
        """Set or clear the notification directly.

        Used by InputPanel for ctrl+c warnings and paste-expand hints.
        Cancels any pending auto-clear timer when *text* is None.
        """
        if self._notif_reset_handle is not None and text is None:
            try:
                self._notif_reset_handle.stop()
            except Exception:  # noqa: BLE001
                pass
            self._notif_reset_handle = None
        self.notification = text
