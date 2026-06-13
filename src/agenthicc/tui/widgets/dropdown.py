"""DropdownWidget — inline filter-as-you-type dropdown (PRD-41).

Wraps a TriggerHandler and renders the existing dropdown UI.
Implements MenuWidget so the MenuDriver can host it.
"""
from __future__ import annotations

import shutil
import sys

from agenthicc.tui.menu import MenuResult
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandler

__all__ = ["DropdownWidget"]

_MAX_VISIBLE = 8


class DropdownWidget:
    """Inline filter-as-you-type dropdown backed by a TriggerHandler.

    Implements the MenuWidget protocol (PRD-41): the MenuDriver can host it
    as the active widget, delegating all rendering and key-handling to this
    class.

    The input bar continues to display ``trigger_char + fragment`` during the
    lifecycle of this widget (``edit_field_value`` returns ``None``).
    """

    def __init__(
        self,
        handler: TriggerHandler,
        ctx: TriggerContext,
        initial_fragment: str = "",
    ) -> None:
        self._handler = handler
        self._ctx = ctx
        self._fragment = initial_fragment
        self._matches: list[MatchItem] = handler.get_matches(initial_fragment, ctx)
        self._selected = 0
        self._hint: str | None = handler.get_hint(
            self._matches[0] if self._matches else None
        )

    # ── MenuWidget protocol ───────────────────────────────────────────────────

    @property
    def edit_field_value(self) -> None:
        """Return None: input bar shows trigger_char + fragment, not a field value."""
        return None

    # ── Extra accessors ───────────────────────────────────────────────────────

    @property
    def fragment(self) -> str:
        """The text typed after the trigger character."""
        return self._fragment

    @property
    def selected_item(self) -> MatchItem | None:
        """Currently highlighted match, or None if the list is empty."""
        if self._matches and 0 <= self._selected < len(self._matches):
            return self._matches[self._selected]
        return None

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render(self, prompt_str: str, buf: list[str], prev_n_lines: int) -> int:
        """Erase old rows and redraw the input line plus the dropdown below it.

        Returns the number of lines now visible below the input row.  The
        caller (MenuDriver) stores this value and passes it back as
        *prev_n_lines* on the next call so stale rows are erased correctly.
        """
        out = sys.stdout
        cols = shutil.get_terminal_size((80, 24)).columns

        # Erase rows from the previous render cycle.
        if prev_n_lines > 0:
            for _ in range(prev_n_lines):
                out.write("\n\r\x1b[2K")
            out.write(f"\x1b[{prev_n_lines}A")

        # Input line: show trigger char + current fragment appended to buf.
        tc = self._handler.char
        out.write("\r\x1b[2K" + prompt_str + "".join(buf) + tc + self._fragment)

        if not self._matches:
            out.flush()
            return 0

        # Build dropdown rows with scroll window.
        n = min(_MAX_VISIBLE, len(self._matches))
        scroll = max(0, min(self._selected - n + 1, len(self._matches) - n))
        visible = self._matches[scroll : scroll + n]
        lines: list[str] = []

        for i, item in enumerate(visible):
            actual = scroll + i
            indicator = "▶" if actual == self._selected else " "
            name = f"+ {item.display}"
            if actual == self._selected:
                lines.append(f"\r\x1b[2K  \x1b[7m{indicator} {name}\x1b[0m")
            else:
                lines.append(f"\r\x1b[2K  {indicator} {name}")

        # Optional hint line below the dropdown for the highlighted item.
        if self._hint:
            sep = "─" * min(cols - 4, 60)
            lines.append(f"\r\x1b[2K  \x1b[2m{sep}\x1b[0m")
            lines.append(f"\r\x1b[2K  \x1b[2m{self._hint[: cols - 4]}\x1b[0m")

        # Scroll indicators.
        below = len(self._matches) - (scroll + n)
        if below > 0:
            lines.append(f"\r\x1b[2K  \x1b[2m… {below} more ↓\x1b[0m")
        elif scroll > 0:
            lines.append(f"\r\x1b[2K  \x1b[2m↑ {scroll} more above\x1b[0m")

        n_lines = len(lines)
        out.write("\n" + "\n".join(lines))
        out.write(f"\x1b[{n_lines}A")
        # Reposition cursor at end of input content; after the cursor-up it
        # sits at the last dropdown-row column, not the end of the input text.
        out.write("\r" + prompt_str + "".join(buf) + tc + self._fragment)
        out.flush()
        return n_lines

    # ── Key handling ──────────────────────────────────────────────────────────

    def handle_key(self, key: object, ch: str) -> MenuResult:
        """Process one keystroke and return a MenuResult.

        Terminal key constants are imported from ``mention_input`` at call-time
        to avoid a circular import (``mention_input`` imports from ``trigger``,
        which this module also imports).
        """
        from agenthicc.tui.mention_input import Key  # noqa: PLC0415

        if key == Key.ESC:
            buf_suffix = self._handler.on_cancel(self._fragment, [])
            return MenuResult.done({"action": "cancel", "buf_suffix": buf_suffix})

        if key in (Key.ENTER, Key.TAB):
            item = self._matches[self._selected] if self._matches else None
            buf_suffix = self._handler.on_select(item, self._fragment, [])
            return MenuResult.done(
                {
                    "action": "select",
                    "buf_suffix": buf_suffix,
                    "add_space": key == Key.TAB,
                }
            )

        if key == Key.CTRL_C:
            buf_suffix = self._handler.on_cancel(self._fragment, [])
            return MenuResult.done({"action": "ctrl_c", "buf_suffix": buf_suffix})

        if key == Key.UP and self._matches:
            self._selected = (self._selected - 1) % len(self._matches)
            self._hint = self._handler.get_hint(self._matches[self._selected])
        elif key == Key.DOWN and self._matches:
            self._selected = (self._selected + 1) % len(self._matches)
            self._hint = self._handler.get_hint(self._matches[self._selected])
        elif key == Key.BACKSPACE:
            if self._fragment:
                self._fragment = self._fragment[:-1]
                self._matches = self._handler.get_matches(self._fragment, self._ctx)
                self._selected = 0
                self._hint = self._handler.get_hint(
                    self._matches[0] if self._matches else None
                )
            else:
                # Backspace past the trigger character: dismiss the dropdown.
                return MenuResult.done({"action": "backspace_past_trigger"})
        elif key == Key.CHAR and ch:
            self._fragment += ch
            self._matches = self._handler.get_matches(self._fragment, self._ctx)
            self._selected = 0
            self._hint = self._handler.get_hint(
                self._matches[0] if self._matches else None
            )

        return MenuResult.continue_()
