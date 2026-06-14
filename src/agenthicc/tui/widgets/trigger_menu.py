"""TriggerMenu widget — dropdown overlay for trigger completions (PRD-55 Phase 3).

Displays a scrollable list of MatchItem entries from the active TriggerHandler.
Hidden by default (display:none); shown/hidden by InputPanel in response to
trigger activation and cancellation events.

Key handling:
  Up/Down  — navigate selection
  Enter    — emit TriggerSelected, hide
  Esc      — emit TriggerCancelled, hide
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from agenthicc.tui.trigger import MatchItem, TriggerHandler, TriggerContext

__all__ = ["TriggerMenu"]

_MAX_VISIBLE = 8


class TriggerMenu(Widget):
    """Dropdown overlay widget for trigger-based completions.

    Call ``activate(handler, fragment, ctx)`` to populate and show the menu.
    Call ``hide()`` to dismiss it.

    Messages posted (to parent):
        TriggerSelected — user confirmed a match item (Enter).
        TriggerCancelled — user dismissed the menu (Esc).
    """

    can_focus = True

    # ── Reactive state ────────────────────────────────────────────────────────

    _selected: reactive[int] = reactive(0, layout=True)

    # ── Messages ──────────────────────────────────────────────────────────────

    class TriggerSelected(Message):
        """Emitted when the user selects a match item."""

        def __init__(self, item: MatchItem) -> None:
            super().__init__()
            self.item = item

    class TriggerCancelled(Message):
        """Emitted when the user dismisses the menu (Esc)."""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._active_handler: TriggerHandler | None = None
        self._fragment: str = ""
        self._matches: list[MatchItem] = []
        self._ctx: TriggerContext | None = None
        # Start hidden.
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="trigger-menu-inner")

    # ── Public API ────────────────────────────────────────────────────────────

    def activate(
        self,
        handler: TriggerHandler,
        fragment: str,
        ctx: TriggerContext,
    ) -> None:
        """Show the menu with matches for *handler* and *fragment*."""
        self._active_handler = handler
        self._fragment = fragment
        self._ctx = ctx
        self._matches = handler.get_matches(fragment, ctx)
        self._selected = 0
        self.display = True
        self._refresh_inner()

    def update_fragment(self, fragment: str) -> None:
        """Update the current fragment and refresh matches."""
        if self._active_handler is None or self._ctx is None:
            return
        self._fragment = fragment
        self._matches = self._active_handler.get_matches(fragment, self._ctx)
        self._selected = 0
        self._refresh_inner()

    def hide(self) -> None:
        """Hide the menu and reset state."""
        self.display = False
        self._active_handler = None
        self._fragment = ""
        self._matches = []
        self._selected = 0

    @property
    def active_handler(self) -> TriggerHandler | None:
        return self._active_handler

    @property
    def fragment(self) -> str:
        return self._fragment

    @property
    def matches(self) -> list[MatchItem]:
        return list(self._matches)

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def selected_item(self) -> MatchItem | None:
        if self._matches and 0 <= self._selected < len(self._matches):
            return self._matches[self._selected]
        return None

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _refresh_inner(self) -> None:
        """Rebuild the rendered text inside the Static child."""
        try:
            inner = self.query_one("#trigger-menu-inner", Static)
            inner.update(self._render_content())
        except Exception:  # noqa: BLE001
            pass

    def _render_content(self) -> str:
        """Build a Rich markup string for the current match list."""
        if not self._matches:
            return "  [dim]No matches[/dim]"

        n = min(_MAX_VISIBLE, len(self._matches))
        scroll = max(0, min(self._selected - n + 1, len(self._matches) - n))
        visible = self._matches[scroll : scroll + n]

        lines: list[str] = []
        for i, item in enumerate(visible):
            actual = scroll + i
            indicator = "▶" if actual == self._selected else " "
            display = item.display
            if actual == self._selected:
                lines.append(f"  [reverse]{indicator} + {display}[/reverse]")
            else:
                lines.append(f"  {indicator} + {display}")

        below = len(self._matches) - (scroll + n)
        if below > 0:
            lines.append(f"  [dim]… {below} more ↓[/dim]")
        elif scroll > 0:
            lines.append(f"  [dim]↑ {scroll} more above[/dim]")

        return "\n".join(lines)

    def watch__selected(self, _old: int, _new: int) -> None:  # noqa: N802
        """Refresh display when selection changes."""
        self._refresh_inner()

    # ── Key handling ──────────────────────────────────────────────────────────

    def on_key(self, event: object) -> None:
        """Handle up/down navigation, Enter to select, Esc to cancel."""
        # event is textual.events.Key
        key = getattr(event, "key", "")
        stop = getattr(event, "stop", lambda: None)

        if not self.display:
            return

        if key == "escape":
            stop()
            self.hide()
            self.post_message(TriggerMenu.TriggerCancelled())

        elif key == "enter":
            stop()
            item = self.selected_item
            if item is not None:
                self.hide()
                self.post_message(TriggerMenu.TriggerSelected(item))

        elif key == "up":
            stop()
            if self._matches:
                self._selected = (self._selected - 1) % len(self._matches)

        elif key == "down":
            stop()
            if self._matches:
                self._selected = (self._selected + 1) % len(self._matches)
