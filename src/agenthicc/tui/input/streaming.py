"""StreamingSession — keystroke capture while the LivePanel is active.

During an agent turn the idle :class:`~agenthicc.tui.input.session.IdleInputSession`
is not running.  ``StreamingSession`` fills that gap: it reads keystrokes in the
background, updates ``InputBarState`` (so the live panel shows what the user is
typing), and on Enter appends the text to *pending_queue* so it is dispatched after
the current agent turn finishes.

Key differences from :class:`~agenthicc.tui.input.session.IdleInputSession`:

* No trigger system, no history navigation, no paste condensation, no cursor
  movement.  The only goal is "let the user pre-type the next message".
* Runs as an asyncio background task; must not block the event loop.  Key reading
  is non-blocking: data availability is checked with ``select(..., timeout=0)``
  before calling :func:`~agenthicc.tui.cbreak_reader.read_key`.
* Renders via ``input_bar_state.update()`` → ``ReactiveProperty`` → LivePanel
  redraw, rather than writing ANSI escape sequences directly.

CBREAK setup is delegated to :func:`~agenthicc.tui.cbreak_reader.raw_mode` so all
terminal-mode logic lives in one place.
"""
from __future__ import annotations

import asyncio
import select
import sys
from pathlib import Path
from typing import Any

from agenthicc.tui.cbreak_reader import Key, raw_mode, read_key
from agenthicc.tui.input.buffer import InputBuffer


def _read_streaming_key(fd: int) -> tuple[Key, str] | None:
    """Non-blocking key read: return ``None`` immediately if no data available.

    Checks the fd with ``select(..., timeout=0)`` first.  If data is ready,
    delegates to :func:`~agenthicc.tui.cbreak_reader.read_key` which may do
    additional blocking reads for multi-byte sequences (up to 50 ms each), but
    those are short enough to be acceptable in a streaming context.
    """
    r, _, _ = select.select([fd], [], [], 0)
    if not r:
        return None
    return read_key(fd)


class StreamingSession:
    """Asyncio background task that captures keystrokes during agent streaming.

    Parameters
    ----------
    input_bar_state:
        The ``InputBarState`` inside the LivePanel.  Each keystroke calls
        ``update()`` which triggers a reactive live-panel redraw.
    pending_queue:
        List shared with ``AgenthiccTUI``.  On Enter the current buffer text
        is stripped and appended here for dispatch after the agent turn.
    console:
        Rich Console used to print the "⌛ Queued" confirmation line.
    live_panel:
        Reserved for future streaming-trigger handoff (PRD-57 §8, P2-4).
        Not used in the current implementation.
    """

    def __init__(
        self,
        input_bar_state: Any,
        pending_queue: list[str],
        console: Any,
        live_panel: Any = None,
        # trigger_registry and cwd reserved for P2-4 streaming trigger handoff
        trigger_registry: Any = None,
        cwd: Path | None = None,
        on_interrupt: Any = None,
    ) -> None:
        self._state = input_bar_state
        self._queue = pending_queue
        self._console = console
        self._live_panel = live_panel
        self._registry = trigger_registry
        self._cwd = cwd or Path(".")
        self._on_interrupt = on_interrupt   # called when user presses Ctrl+C or Esc
        self._buf = InputBuffer()
        self._task: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background keystroke-reader task."""
        self._buf.clear()
        self._state.clear()
        self._task = asyncio.create_task(self._run(), name="streaming-session")

    def stop(self) -> None:
        """Cancel the background task and clear state."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._buf.clear()
        self._state.clear()

    # ── internal ──────────────────────────────────────────────────────────────

    def _push(self) -> None:
        """Sync InputBuffer into InputBarState for live-panel redraw."""
        self._state.update(list(self._buf.buf), self._buf.cursor)

    async def _run(self) -> None:
        from rich.markup import escape as _esc  # noqa: PLC0415

        fd = sys.stdin.fileno()
        try:
            with raw_mode(fd):
                await self._read_loop(fd, _esc)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # Terminal not available — exit silently.

    async def _read_loop(self, fd: int, _esc: Any) -> None:
        while True:
            await asyncio.sleep(0.02)
            result = _read_streaming_key(fd)
            if result is None:
                continue

            key, ch = result

            match key:
                case Key.CTRL_C | Key.ESC:
                    # Cancel the running agent.  The on_interrupt callback is
                    # _sigint_cancel from tui.py, which calls _current_task.cancel().
                    # We clear the pending input so the cancelled turn leaves no
                    # half-typed text.
                    self._buf.clear()
                    self._push()
                    if self._on_interrupt is not None:
                        self._on_interrupt()

                case Key.ENTER:
                    text = self._buf.text.strip()
                    self._buf.clear()
                    self._push()
                    if text:
                        self._queue.append(text)
                        self._console.print(
                            f"[dim]❯ {_esc(text)}  ⌛ Queued[/dim]",
                            markup=True, highlight=False,
                        )

                case Key.CTRL_ENTER:        # Ctrl+J — insert newline
                    self._buf.insert("\n")
                    self._push()

                case Key.BACKSPACE:
                    self._buf.delete_before()
                    self._push()

                case Key.CTRL_U:
                    self._buf.clear()
                    self._push()

                case Key.CHAR if ch:
                    self._buf.insert(ch)
                    self._push()

                case _:
                    pass   # arrow keys and other sequences — ignored
