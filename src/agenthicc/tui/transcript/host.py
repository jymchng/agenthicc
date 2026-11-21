"""The alternate-screen loop that runs a :class:`TranscriptView` (TUX-09 §2.1).

The host is what the agents hub's ``run`` is: it enters the alternate screen,
puts the terminal in raw mode, reads keys on an executor thread and draws the
view with a :class:`LiveRegion`. It differs in two ways. It is *entered from*
the composer (through the input session's terminal handoff), so the composer
and everything it holds are exactly as they were when it returns; and it runs
for as long as the view is open, polling the log for new items and a search
for its hits ten times a second.

Keys the view does not handle itself reach the host:

- ``y`` copies the selected item (OSC 52, or the system clipboard);
- ``e`` leaves the alternate screen and opens the item in ``$PAGER``;
- ``Enter`` in a rewind view opens the TUX-07 dialog that offers to fork the
  session at that turn.

A host entered while the fullscreen layout already owns the alternate screen
(``/fullscreen``) does not leave it on exit.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.text import Text

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.transcript.clipboard import copy_text, run_pager
from agenthicc.tui.transcript.view import OutcomeKind, TranscriptView, to_lines

if TYPE_CHECKING:
    from agenthicc.config import UISettings
    from agenthicc.tui.terminal.backend import TerminalBackend
    from agenthicc.tui.workspace.dialog import Dialog

__all__ = ["HostResult", "TranscriptHost"]

#: Seconds between polls of the log and of a running search.
_TICK_S = 0.1


@dataclass(frozen=True)
class HostResult:
    """How the view ended."""

    kind: Literal["closed", "rewind"] = "closed"
    #: The prompt of the turn to rewind to (``rewind``).
    text: str = ""
    #: The user turn's number (``rewind``).
    turn: int = 0


class TranscriptHost:
    """Run *view* until the user closes it."""

    def __init__(
        self,
        view: TranscriptView,
        console: Console,
        backend: TerminalBackend,
        *,
        ui: UISettings | None = None,
        pager: Callable[[str], str] | None = None,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.view = view
        self.console = console
        self.backend = backend
        self._ui = ui
        self._env = env
        self._pager = pager or (lambda text: run_pager(text, self._env))
        self._clock = clock
        self._dialog: Dialog | None = None
        self._rewind_turn = 0
        self._rewind_text = ""
        self._stream = console.file
        self._pager_text: str | None = None
        #: The size drawn last, to notice a resize.
        self._drawn: tuple[int, int] = (0, 0)

    # ── drawing ──────────────────────────────────────────────────────────────

    def _write(self, text: str) -> None:
        """Write a control sequence straight to the terminal (never through the live block)."""
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def _rows(self, width: int, height: int) -> list[Text]:
        rows = self.view.render(width, height)
        dialog = self._dialog
        if dialog is None:
            return rows
        from agenthicc.tui.workspace.dialog import render_lines

        dialog.set_viewport(width, max(4, height - 2))
        shown = render_lines([dialog.render()], width)
        return [*rows[: max(0, height - len(shown))], *shown][:height]

    def _draw(self, live: object) -> None:
        from agenthicc.tui.workspace.live_region import LiveRegion

        assert isinstance(live, LiveRegion)
        width, height = self.console.size
        self._drawn = (width, height)
        live.update(to_lines(self.console, self._rows(width, height), width))

    # ── keys ─────────────────────────────────────────────────────────────────

    def _handle(self, key: Key, ch: str) -> HostResult | None:
        if key is Key.CTRL_C:
            return HostResult()
        if self._dialog is not None:
            return self._dialog_key(key, ch)
        outcome = self.view.handle_key(key, ch)
        match outcome.kind:
            case OutcomeKind.CLOSE:
                return HostResult()
            case OutcomeKind.COPY:
                self._copy(outcome.text)
            case OutcomeKind.PAGER:
                self._pager_text = outcome.text
            case OutcomeKind.REWIND:
                self._ask_rewind(outcome.text, outcome.turn)
        return None

    def _copy(self, text: str) -> None:
        env = self._env if self._env is not None else dict(os.environ)
        result = copy_text(text, write=self._write, env=env)
        how = "" if result.method == "osc52" else " (system clipboard)"
        cut = " (cut to fit)" if result.cut else ""
        self.view.say(f"copied {len(text):,} characters{how}{cut}")

    # ── /rewind ──────────────────────────────────────────────────────────────

    def _ask_rewind(self, text: str, turn: int) -> None:
        from agenthicc.tui.workspace.dialog import (
            Dialog,
            DialogOption,
            DialogSettings,
        )

        settings = DialogSettings(
            keymap=self.view.keymap, plain=bool(self._ui and self._ui.screen_reader)
        )
        dialog = Dialog(f"Rewind to turn {turn}?", settings=settings, badge="")
        shown = " ".join(text.split())
        dialog.summary = f"Fork this conversation before turn {turn}: {shown[:120]}"
        dialog.reason = (
            "Only the conversation is forked. Files are not restored, and this session is kept."
        )
        dialog.options = [
            DialogOption("Edit and resend from here"),
            DialogOption("Copy the prompt"),
        ]
        dialog.set_body(turn, lambda: [Text(text or "(empty prompt)")])
        self._dialog = dialog
        self._rewind_turn, self._rewind_text = turn, text

    def _dialog_key(self, key: Key, ch: str) -> HostResult | None:
        from agenthicc.tui.workspace.dialog import DialogKey

        dialog = self._dialog
        assert dialog is not None
        result = dialog.handle_key(key, ch)
        if result.kind is DialogKey.CANCEL:
            self._dialog = None
        elif result.kind is DialogKey.CHOOSE:
            self._dialog = None
            if result.index == 0:
                return HostResult("rewind", self._rewind_text, self._rewind_turn)
            self._copy(self._rewind_text)
        return None

    # ── the loop ─────────────────────────────────────────────────────────────

    async def run(self) -> HostResult:
        """Show the view until it is closed; return how it ended."""
        result: HostResult | None = None
        try:
            while result is None:
                result = await self._session()
                if self._pager_text is not None:
                    text, self._pager_text = self._pager_text, None
                    problem = await asyncio.to_thread(self._pager, text)
                    if problem:
                        self.view.say(problem)
                    result = None
        finally:
            self.view.source.close()
        return result

    async def _session(self) -> HostResult | None:
        """One stay in the alternate screen: until closed, or until a pager is wanted."""
        from agenthicc.tui.terminal.capabilities import capabilities
        from agenthicc.tui.workspace.live_region import LiveRegion

        backend = self.backend
        mouse = self._ui.mouse_enabled(fullscreen=True) if self._ui is not None else True
        entered = self.console.set_alt_screen(True)
        live = LiveRegion(
            self.console,
            transient=True,
            synchronized_output=lambda: capabilities.synchronized_output,
        )
        live.start()
        loop = asyncio.get_running_loop()
        finished = threading.Event()
        read_future: asyncio.Future[tuple[Key, str]] | None = None
        result: HostResult | None = None

        def blocking_read_key() -> tuple[Key, str]:
            try:
                return backend.read_key()
            finally:
                finished.set()

        try:
            with backend.enter_raw_mode():
                if mouse:
                    backend.set_reporting(mouse=True)
                finished.clear()
                read_future = loop.run_in_executor(None, blocking_read_key)
                self._draw(live)
                while result is None and self._pager_text is None:
                    done, _pending = await asyncio.wait({read_future}, timeout=_TICK_S)
                    dirty = False
                    if read_future in done:
                        try:
                            key, ch = read_future.result()
                        except OSError:
                            return HostResult()
                        result = self._handle(key, ch)
                        dirty = True
                        if result is None and self._pager_text is None:
                            finished.clear()
                            read_future = loop.run_in_executor(None, blocking_read_key)
                    if self.view.tick():
                        dirty = True
                    if dirty or self.console.size != self._drawn:
                        self._draw(live)
        finally:
            live.stop()
            if entered or self._pager_text is not None:
                self.console.set_alt_screen(False)
            try:
                backend.restore()
            except Exception:  # noqa: BLE001 - leaving the terminal must not fail
                pass
            pending = read_future is not None and not read_future.done()
            if read_future is not None and pending:
                read_future.cancel()
                await asyncio.to_thread(finished.wait, 0.25)
            if read_future is None or finished.is_set() or not pending:
                backend.close()
        return result
