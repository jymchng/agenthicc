"""StreamingInput — keystroke capture while the LivePanel is active.

During an agent turn the normal CBREAK input loop (mention_input.py) is not
running.  This component fills that gap: it reads keystrokes in the background,
updates InputBarState (so the live panel shows what the user is typing), and
on Enter appends the text to *pending_queue* so it is dispatched after the
current agent turn finishes.

Ctrl+J inserts a newline (multi-line queued message).
Ctrl+U clears the buffer.
Backspace removes the last character.
Any printable character is appended.

The component is intentionally simpler than mention_input.py — no @-mention
triggers, no slash-command picker, no history.  Those features only make sense
at the top-level prompt; during streaming the user is just queuing free-form text.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any


class StreamingInput:
    """Runs as an asyncio background task during a LivePanel agent turn.

    Parameters
    ----------
    input_bar_state:
        The InputBarState instance inside the LivePanel.  Every keystroke
        updates its ``buf`` and ``cursor`` via ``update()``, which triggers
        a reactive redraw of the live panel.
    pending_queue:
        List shared with AgenthiccTUI.  On Enter the current buffer is
        stripped and appended here.
    console:
        Rich Console used to print the "⌛ Queued" confirmation line.
    """

    def __init__(
        self,
        input_bar_state: Any,
        pending_queue: list[str],
        console: Any,
    ) -> None:
        self._state = input_bar_state
        self._queue = pending_queue
        self._console = console
        self._buf: list[str] = []
        self._task: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background keystroke-reader task."""
        self._buf = []
        self._state.clear()
        self._task = asyncio.create_task(self._run(), name="streaming-input")

    def stop(self) -> None:
        """Cancel the background task and flush any partial buffer."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._buf = []
        self._state.clear()

    # ── internal ──────────────────────────────────────────────────────────────

    def _push(self) -> None:
        """Sync the local buffer into InputBarState for live-panel redraw."""
        self._state.update(list(self._buf), len(self._buf))

    async def _run(self) -> None:
        import select as _sel  # noqa: PLC0415
        import tty as _tty     # noqa: PLC0415
        import termios as _tm  # noqa: PLC0415
        from rich.markup import escape as _esc  # noqa: PLC0415

        fd = sys.stdin.fileno()
        try:
            old = _tm.tcgetattr(fd)
            _tty.setcbreak(fd)
            cur = list(_tm.tcgetattr(fd))
            cur[0] &= ~_tm.ICRNL                      # don't translate \r → \n
            cur[3] &= ~(_tm.ECHOCTL | _tm.ISIG)       # suppress ^C echo, no SIGINT
            _tm.tcsetattr(fd, _tm.TCSANOW, cur)
        except Exception:
            return

        try:
            while True:
                await asyncio.sleep(0.02)
                r, _, _ = _sel.select([fd], [], [], 0)
                if not r:
                    continue

                b = os.read(fd, 1)

                if b == b"\r":                          # Enter — submit
                    text = "".join(self._buf).strip()
                    self._buf.clear()
                    self._push()
                    if text:
                        self._queue.append(text)
                        self._console.print(
                            f"[dim]❯ {_esc(text)}  ⌛ Queued[/dim]",
                            markup=True, highlight=False,
                        )

                elif b == b"\n":                        # Ctrl+J — newline
                    self._buf.append("\n")
                    self._push()

                elif b in (b"\x7f", b"\x08"):          # Backspace
                    if self._buf:
                        self._buf.pop()
                        self._push()

                elif b == b"\x15":                      # Ctrl+U — clear
                    self._buf.clear()
                    self._push()

                elif b == b"\x1b":                      # escape sequences — skip
                    r2, _, _ = _sel.select([fd], [], [], 0.05)
                    if r2:
                        os.read(fd, 2)                 # consume CSI bytes

                elif b >= b" ":                         # printable / UTF-8
                    raw = b
                    first = b[0]
                    n_extra = (
                        1 if first & 0b11100000 == 0b11000000 else
                        2 if first & 0b11110000 == 0b11100000 else
                        3 if first & 0b11111000 == 0b11110000 else
                        0
                    )
                    for _ in range(n_extra):
                        r3, _, _ = _sel.select([fd], [], [], 0.05)
                        if r3:
                            raw += os.read(fd, 1)
                    try:
                        ch = raw.decode("utf-8")
                        if ch.isprintable():
                            self._buf.append(ch)
                            self._push()
                    except UnicodeDecodeError:
                        pass

        except asyncio.CancelledError:
            pass
        finally:
            try:
                _tm.tcsetattr(fd, _tm.TCSADRAIN, old)
            except Exception:
                pass
