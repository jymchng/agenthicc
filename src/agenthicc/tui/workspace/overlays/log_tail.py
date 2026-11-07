"""A simple pager over the tail of the session's diagnostics log (``/logs``, TUX-08).

TUX-09's transcript view will take over ``/logs``; until then this overlay
shows the last lines of ``~/.agenthicc/logs/<session>.log``, newest at the
bottom, as literal text.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType

__all__ = ["LogTailOverlay"]


class LogTailOverlay(Overlay):
    """Scroll the last lines of a log file; Esc or q closes it."""

    name = "log_tail"

    def __init__(self, path: Path, lines: list[str], on_close: Callable[[], None]) -> None:
        self._path = path
        self._lines = lines
        self._on_close = on_close
        self._rows = 12
        self._top = max(0, len(lines) - self._rows)

    def set_viewport(self, width: int, height: int) -> None:
        at_bottom = self._top >= max(0, len(self._lines) - self._rows)
        self._rows = max(1, height - 3)
        if at_bottom:
            self._top = max(0, len(self._lines) - self._rows)

    def _scroll(self, delta: int) -> None:
        self._top = max(0, min(max(0, len(self._lines) - self._rows), self._top + delta))

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        from agenthicc.tui.theme import glyphs, style

        g = glyphs()
        shown = self._lines[self._top : self._top + self._rows]
        end = self._top + len(shown)
        title = Text(
            f" Diagnostics log {g.sep} {self._path}",
            style=style("accent", bold=True),
            no_wrap=True,
            overflow="ellipsis",
        )
        rows = [Text(" " + line, no_wrap=True, overflow="ellipsis") for line in shown]
        footer = Text(
            f" {self._top + 1 if shown else 0}{g.ellipsis}{end} of {len(self._lines)} lines   "
            f"{g.up}{g.down}/PgUp/PgDn scroll   Esc close",
            style=style("muted"),
        )
        return Group(title, *rows, footer)

    def handle_key(self, key: Key, ch: str) -> bool:
        if key == Key.ESC or (key == Key.CHAR and ch.lower() == "q"):
            self._on_close()
        elif key == Key.UP:
            self._scroll(-1)
        elif key == Key.DOWN:
            self._scroll(1)
        elif key == Key.PAGE_UP:
            self._scroll(-self._rows)
        elif key == Key.PAGE_DOWN:
            self._scroll(self._rows)
        return True
