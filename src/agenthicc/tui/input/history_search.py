"""The Ctrl+R history-search row above the prompt (TUX-04 §2.3).

::

     (search) git rebas▌                  3 of 41 · Ctrl+R older · Enter use · Esc cancel
     ❯ git rebase -i main and fix the lockfile conflict

The overlay never edits the prompt: it shows the match in place of the prompt
until Enter (or →) puts it there, unsent. Esc leaves the draft exactly as it
was. Its keys are the ``search`` keymap context; any other printable key
extends the query and Backspace shortens it.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key, key_name
from agenthicc.tui.input.history import HistorySearch
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agenthicc.tui.keymap import Keymap

__all__ = ["HistorySearchOverlay", "SearchOutcome"]


class SearchOutcome(Enum):
    STAY = auto()
    ACCEPT = auto()  # put the match into the prompt
    CANCEL = auto()  # restore the draft


class HistorySearchOverlay(Overlay):
    """Reverse incremental search over the composer's history."""

    name = "history_search"

    def __init__(self, history: list[str], keymap: Keymap, draft: list[str], cursor: int) -> None:
        self.search = HistorySearch(history)
        self._keymap = keymap
        self._draft = list(draft)
        self._draft_cursor = cursor

    @property
    def match(self) -> str | None:
        return self.search.current

    def choose(self, key: Key, ch: str) -> SearchOutcome:
        """Handle every key while the search is open (it claims them all)."""
        action = self._keymap.resolve("search", key_name(key, ch))
        name = action.name if action is not None else ""
        if name == "search.older":
            self.search.older()
        elif name == "search.newer":
            self.search.newer()
        elif name == "search.accept":
            return SearchOutcome.ACCEPT if self.search.current is not None else SearchOutcome.STAY
        elif name == "search.cancel":
            return SearchOutcome.CANCEL
        elif key is Key.BACKSPACE:
            self.search.set_query(self.search.query[:-1])
        elif key is Key.CHAR and ch:
            self.search.set_query(self.search.query + ch)
        elif key is Key.AT:
            self.search.set_query(self.search.query + "@")
        elif key is Key.PASTE and ch:
            self.search.set_query(self.search.query + ch.replace("\n", " "))
        return SearchOutcome.STAY

    def handle_key(self, key: Key, ch: str) -> bool:
        """Overlay contract; the composer routes keys through :meth:`choose`."""
        self.choose(key, ch)
        return True

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        from agenthicc.tui.input.renderer import build_prompt
        from agenthicc.tui.theme import glyphs, style

        g = glyphs()
        muted = style("muted")
        row = Text(" (search) ", style=muted)
        row.append(self.search.query)
        row.append(g.cursor, "bold")
        matches = self.search.matches
        count = f"{self.search.position + 1} of {len(matches)}" if matches else "no match"
        hints = f" {g.sep} ".join(
            f"{hint.keys} {hint.label}" for hint in self._keymap.hints("search")
        )
        row.append(f"   {count} {g.sep} {hints}", style=muted)
        current = self.search.current
        if current is None:
            prompt = build_prompt(self._draft, self._draft_cursor)
        else:
            prompt = build_prompt(list(current), len(current))
        return Group(row, prompt)
