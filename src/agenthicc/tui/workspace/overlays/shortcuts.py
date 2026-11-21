"""ShortcutsOverlay: ``/shortcuts`` and ``?`` as a scrollable pager (TUX-10 §2.6, TUX-07).

Every key in every context, grouped as the screens are: the composer, the
pickers, the dialogs, the transcript view and the agents hub. Rows wrap under
their key instead of being cut off, and a key the user rebound says so::

    Composer
      Enter          Send the prompt; while a turn runs it joins the turn   default
      Ctrl+Q         Undo the last edit                      your keybindings
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.table import Table
from rich.text import Text

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.shortcuts import USER_SOURCE, ShortcutGroup, ShortcutRow, shortcut_groups
from agenthicc.tui.theme import glyphs, style
from agenthicc.tui.workspace.dialog import (
    DEFAULT_DIALOG_SETTINGS,
    Dialog,
    DialogKey,
    DialogSettings,
)
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType

__all__ = ["ShortcutsOverlay", "keys_column_width"]

#: The key column is at most this wide; a longer list of keys wraps in it.
_KEYS_MAX = 24


def keys_column_width(groups: tuple[ShortcutGroup, ...]) -> int:
    """The widest key label, up to :data:`_KEYS_MAX` cells."""
    widest = max((cell_len(row.keys) for group in groups for row in group.rows), default=4)
    return min(_KEYS_MAX, widest)


def _row_grid(row: ShortcutRow, keys_width: int) -> Table:
    grid = Table.grid(expand=True, padding=(0, 2, 0, 0))
    grid.add_column(width=keys_width, no_wrap=False, overflow="fold")
    grid.add_column(ratio=1, overflow="fold")
    text = Text(row.description)
    if row.conditions:
        text.append(f" ({row.conditions})", style=style("muted"))
    text.append(f" {glyphs().sep} ", style=style("muted"))
    text.append(row.source, style=style("accent") if row.source == USER_SOURCE else style("muted"))
    grid.add_row(Text(row.keys, style=style("text", bold=True)), text)
    return grid


class ShortcutsOverlay(Overlay):
    """The effective key bindings of *keymap*, as a pager dialog."""

    name = "shortcuts"

    def __init__(
        self,
        keymap: Keymap,
        *,
        on_close: Callable[[], None],
        settings: DialogSettings = DEFAULT_DIALOG_SETTINGS,
    ) -> None:
        self._groups = shortcut_groups(keymap)
        self._on_close = on_close
        self.dialog = Dialog("Keyboard shortcuts", settings=settings, cancel_label="close")
        count = sum(len(group.rows) for group in self._groups)
        self.dialog.summary = f"{count} keys, from your key bindings and the defaults."
        self.dialog.set_body(id(self._groups), self._body)
        g = glyphs()
        self.dialog.legend_override = (f"{g.up}{g.down} scroll", "PgUp/PgDn page", "Esc close")

    @property
    def groups(self) -> tuple[ShortcutGroup, ...]:
        return self._groups

    def _body(self) -> list[RenderableType]:
        width = keys_column_width(self._groups)
        body: list[RenderableType] = []
        for group in self._groups:
            body.append(Text(group.title, style=style("accent", bold=True)))
            for section in group.sections:
                if section.title:
                    body.append(Text(f"  {section.title}", style=style("muted")))
                body.extend(_row_grid(row, width) for row in section.rows)
            body.append(Text(""))
        return body

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.dialog.scroll = 0

    def set_viewport(self, width: int, height: int) -> None:
        self.dialog.set_viewport(width, height)

    def render(self) -> RenderableType:
        return self.dialog.render()

    def handle_key(self, key: Key, ch: str) -> bool:
        if key is Key.CHAR and ch.casefold() == "q":
            self._on_close()
            return True
        if self.dialog.handle_key(key, ch).kind is DialogKey.CANCEL:
            self._on_close()
        return True
