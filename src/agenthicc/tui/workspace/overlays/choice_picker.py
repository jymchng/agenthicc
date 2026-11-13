"""A titled :class:`Picker` overlay for ``/model``, ``/mode`` and ``/agent`` (TUX-05 §2.5).

Enter chooses the selected row (a dimmed row says why it cannot be chosen).
A picker built with ``keep_open`` stays open after the choice and shows what
happened, so a follow-up key (``s`` to save as default) can act on it.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Generic, TypeVar

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.workspace.picker import Picker, PickerRow, picker_height

if TYPE_CHECKING:
    from rich.console import RenderableType

__all__ = ["ChoicePickerOverlay", "ExtraKey"]

T = TypeVar("T")

#: A follow-up key: its footer label and what it does with the chosen value.
ExtraKey = tuple[str, Callable[[T], str]]


class ChoicePickerOverlay(Overlay, Generic[T]):
    """Rows with a title; Enter calls *choose* with the selected row's value."""

    name = "choice-picker"

    def __init__(
        self,
        title: str,
        rows: Sequence[PickerRow[T]],
        *,
        choose: Callable[[T], str],
        close: Callable[[], None],
        subtitle: str = "",
        keep_open: bool = False,
        extra_keys: Mapping[str, ExtraKey[T]] | None = None,
    ) -> None:
        from agenthicc.tui.theme import glyphs

        g = glyphs()
        self._title = title
        self._subtitle = subtitle
        self._choose = choose
        self._close = close
        self._keep_open = keep_open
        self._extra: Mapping[str, ExtraKey[T]] = extra_keys or {}
        self._chosen: T | None = None
        self.status = ""
        footer = [f"{g.up}{g.down} choose", "enter select"]
        footer.extend(f"{key} {label}" for key, (label, _) in self._extra.items())
        footer.append("esc close")
        self.picker: Picker[T] = Picker(rows, footer_keys=f" {g.sep} ".join(footer))
        self._width = shutil.get_terminal_size((80, 24)).columns
        self._height = picker_height(shutil.get_terminal_size((80, 24)).lines)

    def select_value(self, value: T) -> None:
        """Highlight the row whose value is *value* (the current setting)."""
        for index, row in enumerate(self.picker.rows):
            if row.value == value:
                self.picker.selected = index
                return

    def set_viewport(self, width: int, height: int) -> None:
        self._width = max(20, width)
        self._height = max(3, min(self._height, height - 3))

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.markup import escape
        from rich.text import Text

        from agenthicc.tui.theme import glyphs, palette

        p, g = palette(), glyphs()
        title = f"  [bold]{escape(self._title)}[/bold]"
        if self._subtitle:
            title += f" [{p.muted}]{g.sep} {escape(self._subtitle)}[/]"
        lines: list[RenderableType] = [Text.from_markup(title)]
        lines.extend(self.picker.render(self._width, self._height))
        if self.status:
            lines.append(Text(f"  {self.status}", style=p.muted))
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if key in (Key.UP, Key.DOWN):
            self.picker.move(-1 if key is Key.UP else 1)
        elif key is Key.ENTER:
            row = self.picker.current
            if row is None:
                return True
            if row.disabled:
                self.status = row.disabled
                return True
            self._chosen = row.value
            self.status = self._choose(row.value)
            if not self._keep_open:
                self._close()
        elif key is Key.CHAR and ch in self._extra and self._chosen is not None:
            _, action = self._extra[ch]
            self.status = action(self._chosen)
        elif key is Key.ESC:
            self._close()
        return True
