"""ReleaseNotesOverlay — ``/release-notes`` in a scrollable :class:`Dialog` (TUX-03, TUX-07).

A pager over the packaged changelog's sections for the installed version and
every version since the last one the user looked at. The dialog has a body and
no options, so its arrows scroll; Esc or ``q`` closes it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from rich.segment import Segment

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.theme import glyphs
from agenthicc.tui.workspace.dialog import (
    DEFAULT_DIALOG_SETTINGS,
    Dialog,
    DialogKey,
    DialogSettings,
)
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderableType, RenderResult

    from agenthicc.release_notes import ReleaseEntry

__all__ = ["ReleaseNotesOverlay"]


class _ChangelogMarkdown(Markdown):
    """Markdown whose list bullets come from the glyph set (Rich draws ``•``)."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        bullet = glyphs().bullet
        for item in super().__rich_console__(console, options):
            if isinstance(item, Segment) and "•" in item.text and not item.control:
                yield Segment(item.text.replace("•", bullet), item.style)
            else:
                yield item


class ReleaseNotesOverlay(Overlay):
    """The release notes of *entries*, newest first, in a pager dialog."""

    name = "release_notes"

    def __init__(
        self,
        entries: Sequence[ReleaseEntry],
        *,
        version: str,
        on_close: Callable[[], None],
        settings: DialogSettings = DEFAULT_DIALOG_SETTINGS,
    ) -> None:
        self._entries = tuple(entries)
        self._on_close = on_close
        self.dialog = Dialog(
            "Release notes",
            settings=settings,
            badge=f"agenthicc {version}",
            badge_token="muted",
            cancel_label="close",
        )
        count = len(self._entries)
        self.dialog.summary = f"{count} release{'s' if count != 1 else ''} of agenthicc."
        self.dialog.set_body(id(self._entries), self._body)
        g = glyphs()
        self.dialog.legend_override = (f"{g.up}{g.down} scroll", "PgUp/PgDn page", "Esc close")

    def _body(self) -> list[RenderableType]:
        from rich.text import Text

        from agenthicc.tui.theme import current_theme, style

        if not self._entries:
            return [Text("No release notes ship with this installation.", style=style("muted"))]
        theme = current_theme().syntax_theme
        body: list[RenderableType] = []
        for entry in self._entries:
            body.append(Text(entry.title, style=style("accent", bold=True)))
            body.append(_ChangelogMarkdown(entry.body, code_theme=theme))
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
