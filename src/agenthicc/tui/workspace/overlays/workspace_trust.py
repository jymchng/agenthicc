"""WorkspaceTrustOverlay — "Work in <directory>?" on a directory agenthicc does not know (TUX-03).

Shown once per directory, in a :class:`Dialog` (TUX-07), when the session
starts in a directory that is not a git repository and has no history with
agenthicc, or in the home directory:

1. **Yes** — work here; the answer is remembered.
2. **Yes, in Plan mode** — work here, but start in Plan mode; remembered.
3. **Choose another directory…** — an embedded composer asks for the
   directory, and the session restarts there.

Esc is "not now": nothing is remembered and the question comes back next time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.dialog import (
    DEFAULT_DIALOG_SETTINGS,
    Dialog,
    DialogKey,
    DialogOption,
    DialogSettings,
)
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text

    from agenthicc.tui.input.embedded import EmbeddedComposer

__all__ = ["WorkspaceTrustOverlay"]

_REMEMBERED = "Your answer is remembered for this directory."


class WorkspaceTrustOverlay(Overlay):
    """Ask whether agenthicc may work in *directory*."""

    name = "workspace_trust"

    def __init__(
        self,
        directory: Path,
        *,
        on_decision: Callable[[str], None],
        on_choose_directory: Callable[[Path], None],
        on_dismiss: Callable[[], None],
        home: Path | None = None,
        settings: DialogSettings = DEFAULT_DIALOG_SETTINGS,
    ) -> None:
        from agenthicc.tui.theme import glyphs
        from agenthicc.tui.welcome import home_relative

        self._directory = directory
        self._on_decision = on_decision
        self._on_choose_directory = on_choose_directory
        self._on_dismiss = on_dismiss
        self._settings = settings
        self._composer: EmbeddedComposer | None = None
        shown = home_relative(directory, home if home is not None else Path.home())
        self.dialog = Dialog(
            f"Work in {shown}?",
            settings=settings,
            badge=settings.mode,
            cancel_label="not now",
        )
        self.dialog.summary = f"agenthicc can read and change files in {shown}."
        self.dialog.reason = _REMEMBERED
        self.dialog.options = [
            DialogOption("Yes"),
            DialogOption("Yes, in Plan mode"),
            DialogOption(f"Choose another directory{glyphs().ellipsis}"),
        ]
        self.dialog.set_body(id(self), self._body)

    def _body(self) -> list[RenderableType]:
        from rich.text import Text

        return [Text("agenthicc can read and change files here.")]

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_unmount(self) -> None:
        self._stop_composing()

    def set_viewport(self, width: int, height: int) -> None:
        self.dialog.set_viewport(width, height)

    def render(self) -> RenderableType:
        return self.dialog.render()

    def handle_key(self, key: Key, ch: str) -> bool:
        composer = self._composer
        if composer is not None:
            if key is Key.ESC and not composer.picker_open:
                self._stop_composing()
            else:
                composer.feed(key, ch)
            return True
        result = self.dialog.handle_key(key, ch)
        if result.kind is DialogKey.CANCEL:
            self._on_dismiss()
        elif result.kind is DialogKey.CHOOSE:
            match result.index:
                case 0:
                    self._on_decision("allow")
                case 1:
                    self._on_decision("plan")
                case _:
                    self._start_composing()
        return True

    # ── choosing another directory ─────────────────────────────────────────────

    def _start_composing(self) -> None:
        from agenthicc.tui.input.embedded import ANSWER_EXCLUDED, ComposerContext, EmbeddedComposer

        context = self._settings.composer or ComposerContext(cwd=self._directory)
        self._composer = EmbeddedComposer(
            context,
            on_submit=self._submit_directory,
            redraw=self._settings.redraw,
            exclude=ANSWER_EXCLUDED(),
        )
        self.dialog.footer = self._composer_rows
        self.dialog.legend_override = ("Enter open", "Esc back")

    def _stop_composing(self) -> None:
        if self._composer is not None:
            self._composer.close()
            self._composer = None
        self.dialog.footer = None
        self.dialog.legend_override = ()
        self.dialog.reason = _REMEMBERED

    def _composer_rows(self, width: int) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.theme import palette
        from agenthicc.tui.workspace.dialog import render_lines

        composer = self._composer
        if composer is None:
            return []
        rows = [Text(""), Text(" Directory to work in:", style=palette().muted)]
        return rows + render_lines(composer.render(), width)

    async def _submit_directory(self, text: str) -> bool:
        wanted = text.strip()
        target = await asyncio.to_thread(_existing_directory, wanted)
        if target is None:
            self.dialog.reason = f"{wanted or 'That'} is not a directory."
            self._settings.redraw()
            return False
        self._on_choose_directory(target)
        return True


def _existing_directory(text: str) -> Path | None:
    """*text* as a resolved directory, or ``None`` when it is empty or not one."""
    if not text:
        return None
    target = Path(text).expanduser()
    return target.resolve() if target.is_dir() else None
