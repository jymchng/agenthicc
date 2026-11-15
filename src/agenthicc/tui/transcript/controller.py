"""What opens the transcript view, and what happens when it closes (TUX-09).

The view runs in the alternate screen through the input session's *terminal
handoff* (the same mechanism the external editor uses): the composer's key
loop pauses, the workspace hands the screen over, the view runs, and the key
loop resumes with the composer's text, cursor and history exactly as they were.

One controller serves the session. The session gives it the few things only it
can do (suspend and resume its live block, hold the scrollback while the view
is up, fork a session) and the controller opens:

- ``history`` — the session's log with turn structure (``/history``);
- ``tool`` — the same, with the last tool call selected and expanded (Ctrl+T);
- ``logs`` — the diagnostics log as plain lines (``/logs``);
- ``rewind`` — the same log, selecting user turns only (``/rewind``);
- a *document* — an approval's full arguments or diff (Ctrl+T in a dialog).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agenthicc.tui.preferences import ui_settings

if TYPE_CHECKING:
    from rich.console import Console, RenderableType

    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.terminal.backend import TerminalBackend
    from agenthicc.tui.transcript.sources import TranscriptSource
    from agenthicc.tui.workspace.notice import Severity

__all__ = ["TranscriptController", "ViewKind"]

ViewKind = Literal["history", "tool", "logs", "rewind"]


@dataclass
class TranscriptController:
    """Open views over a session's log; see the module docstring."""

    session_id: str
    console: Console
    state: AppState
    #: The input session's terminal handoff.
    handoff: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]
    #: Starts a coroutine as a supervised task.
    spawn: Callable[[Coroutine[object, object, None], str], object]
    #: The workspace around the view: stop its live block, start it again.
    suspend: Callable[[], None]
    resume: Callable[[], None]
    #: Hold the scrollback appender while the view is up, and let it go.
    hold: Callable[[], None]
    release: Callable[[], None]
    notify: Callable[[Severity, str], object]
    #: Fork the session at a user turn (``/rewind``): ``(prompt, turn)``.
    rewind: Callable[[str, int], None]
    #: Whether a turn is running (a fork waits for it to end).
    busy: Callable[[], bool]
    backend: Callable[[], TerminalBackend]
    #: The diagnostics log's path for this session.
    log_path: Callable[[], Path]

    def open(self, kind: ViewKind) -> None:
        """Open a view on the session's log or the diagnostics log."""
        self.spawn(self._open(kind), "transcript-view")

    def open_document(self, title: str, renderables: Sequence[RenderableType]) -> None:
        """Open a view on content that exists already (an approval's full details)."""
        self.spawn(self._open_document(title, renderables), "transcript-document")

    # ── building the view ────────────────────────────────────────────────────

    def _interactive(self) -> bool:
        if self.backend().is_interactive():
            return True
        self.notify("warning", "The transcript view needs a terminal.")
        return False

    async def _open(self, kind: ViewKind) -> None:
        from agenthicc.tui.transcript.sources import SessionLogSource, TextFileSource

        if not self._interactive():
            return
        if kind == "logs":
            path = self.log_path()
            if not path.is_file() or path.stat().st_size == 0:
                self.notify("info", "The diagnostics log is empty.")
                return
            await self._run(TextFileSource(path), subtitle=str(path), start="end")
            return
        ui = ui_settings(self.state)
        title = self.state.conversation.session_title()
        source = SessionLogSource(self.session_id, ui=ui)
        await self._run(
            source,
            subtitle=title,
            start="tool" if kind == "tool" else "end",
            rewind=kind == "rewind",
        )

    async def _open_document(self, title: str, renderables: Sequence[RenderableType]) -> None:
        from agenthicc.tui.transcript.sources import DocumentSource

        if not self._interactive():
            return
        await self._run(DocumentSource(title, renderables), subtitle=title, start="start")

    async def _run(
        self,
        source: TranscriptSource,
        *,
        subtitle: str,
        start: str,
        rewind: bool = False,
    ) -> None:
        from agenthicc.tui.transcript.host import HostResult, TranscriptHost
        from agenthicc.tui.transcript.view import TranscriptView

        view = TranscriptView(
            source, self.state.keymap(), rewind=rewind, subtitle=subtitle, start=start
        )
        host = TranscriptHost(view, self.console, self.backend(), ui=ui_settings(self.state))
        outcome: list[HostResult] = []

        async def operation() -> None:
            self.suspend()
            self.hold()
            try:
                outcome.append(await host.run())
            finally:
                self.release()
                self.resume()

        try:
            await self.handoff(operation)
        except RuntimeError:
            source.close()
            self.notify("warning", "The transcript view could not take the terminal.")
            return
        if outcome and outcome[0].kind == "rewind":
            if self.busy():
                self.notify("warning", "Finish or stop the current turn before rewinding.")
                return
            self.rewind(outcome[0].text, outcome[0].turn)
