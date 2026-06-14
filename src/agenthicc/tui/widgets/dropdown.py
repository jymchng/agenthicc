"""DropdownWidget — generic trigger-driven dropdown completion widget."""
from __future__ import annotations

from typing import Any

from agenthicc.tui.trigger import MatchItem, TriggerContext
from agenthicc.tui.menu import MenuResult, MenuResultKind
from agenthicc.tui.terminal import Key

__all__ = ["DropdownWidget"]


class DropdownWidget:
    """Dropdown completion widget backed by a TriggerHandler."""

    def __init__(
        self,
        handler: Any,
        ctx: TriggerContext,
        initial_fragment: str = "",
        # Legacy params for new-style usage
        items: list[MatchItem] | None = None,
        max_height: int = 8,
    ) -> None:
        self._handler = handler
        self._ctx = ctx
        self._fragment = initial_fragment
        self._max_height = max_height

        if items is not None:
            self._matches = items
        elif handler is not None:
            if hasattr(handler, "get_matches"):
                # Old-style: get_matches(fragment, ctx)
                self._matches = handler.get_matches(initial_fragment, ctx)
            else:
                self._matches = []
        else:
            self._matches = []

        self._selected = 0

    @property
    def edit_field_value(self) -> None:
        return None

    def render(self, prompt_str: str = "", buf: list = None, prev: int = 0) -> int:
        return 0

    # Legacy render API
    def render_list(self, width: int = 80) -> list[str]:
        rows = []
        for i, item in enumerate(self._matches[:self._max_height]):
            prefix = "▶ " if i == self._selected else "  "
            rows.append(f"{prefix}{item.display}"[:width])
        return rows

    def navigate(self, direction: int) -> None:
        if self._matches:
            self._selected = (self._selected + direction) % len(self._matches)

    def selected(self) -> MatchItem | None:
        if self._matches:
            return self._matches[self._selected]
        return None

    @property
    def visible(self) -> bool:
        return bool(self._matches)

    def handle_key(self, key: Any, ch: str = "") -> MenuResult:
        if key == Key.ESC or key == "ESC":
            suffix = self._handler.on_cancel(self._fragment, []) if self._handler else []
            return MenuResult.done({"action": "cancel", "buf_suffix": suffix})

        if key == Key.ENTER or key == "ENTER":
            if self._matches:
                item = self._matches[self._selected]
            else:
                item = None
            suffix = self._handler.on_select(item, self._fragment, []) if self._handler else []
            return MenuResult.done({"action": "select", "buf_suffix": suffix, "add_space": False})

        if key == Key.TAB or key == "TAB":
            if self._matches:
                item = self._matches[self._selected]
            else:
                item = None
            suffix = self._handler.on_select(item, self._fragment, []) if self._handler else []
            return MenuResult.done({"action": "select", "buf_suffix": suffix, "add_space": True})

        if key == Key.CHAR or key == "CHAR":
            self._fragment += ch
            if self._handler and hasattr(self._handler, "get_matches"):
                self._matches = self._handler.get_matches(self._fragment, self._ctx)
            self._selected = 0
            return MenuResult.continue_()

        if key == Key.BACKSPACE or key == "BACKSPACE":
            if self._fragment:
                self._fragment = self._fragment[:-1]
                if self._handler and hasattr(self._handler, "get_matches"):
                    self._matches = self._handler.get_matches(self._fragment, self._ctx)
                self._selected = 0
                return MenuResult.continue_()
            else:
                return MenuResult.done({"action": "backspace_past_trigger"})

        if key == Key.DOWN or key == "DOWN":
            if self._matches:
                self._selected = (self._selected + 1) % len(self._matches)
            return MenuResult.continue_()

        return MenuResult.continue_()
