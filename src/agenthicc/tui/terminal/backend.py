"""TerminalBackend Protocol and platform backend factory (PRD-106).

``get_backend()`` is the single permitted platform-specific branch:

    if os.name == "nt":   → WindowsBackend (msvcrt)
    else:                 → PosixBackend   (termios / tty)

All application code must use the backend interface; no feature code may
import ``msvcrt``, ``termios``, or ``tty`` directly.
"""

from __future__ import annotations

import os
import sys
from contextlib import AbstractContextManager
from typing import IO, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agenthicc.tui.cbreak_reader import Key

__all__ = ["TerminalBackend", "get_backend", "terminal_stream"]


def terminal_stream() -> IO[str]:
    """The stream the terminal reads: ``sys.stdout``, without the live block's text proxy.

    While the live block runs, ``sys.stdout`` is a Rich ``FileProxy`` that prints what
    is written as text: it strips control characters, including the BEL that ends a
    title, progress or notification sequence, and so leaves the sequence unterminated,
    when everything printed after it is swallowed. Control sequences are not text;
    they are written to the stream underneath.
    """
    from rich.file_proxy import FileProxy

    stream = sys.stdout
    return stream.rich_proxied_file if isinstance(stream, FileProxy) else stream


class TerminalBackend(Protocol):
    """Platform-independent interface for terminal keyboard input."""

    def is_interactive(self) -> bool:
        """Return True when an interactive terminal session is available."""
        ...

    def read_key(self) -> tuple[Key, str]:
        """Read and return one keystroke.  Blocks until a key arrives."""
        ...

    def enter_raw_mode(self) -> AbstractContextManager[None]:
        """Context manager: configure the terminal for single-keystroke input.

        On POSIX this enables CBREAK mode and restores the original settings
        on exit.  On Windows ``msvcrt.getwch()`` already bypasses line
        buffering, so this is a no-op context manager.
        """
        ...

    def restore(self) -> None:
        """Best-effort terminal restore — safe to call even after an error."""
        ...

    def close(self) -> None:
        """Release what :meth:`read_key` uses, once no read is in progress."""
        ...

    def query_background(self, timeout: float) -> tuple[int, int, int] | None:
        """The terminal's background colour (OSC 11), or ``None`` (TUX-01).

        Waits at most *timeout* seconds; never called on a non-interactive
        terminal.
        """
        ...

    def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        """Turn focus and/or mouse reporting on or off (AGX-13).

        Reports arrive from :meth:`read_key` as ``Key.FOCUS_IN`` /
        ``Key.FOCUS_OUT`` and ``Key.MOUSE``. Leaving raw mode turns both off.
        """
        ...

    def write_control(self, sequence: str) -> None:
        """Write a control sequence (window title, progress, notification) to the terminal.

        Nothing is written when the output is not a terminal, so a pipe or a
        file never receives an escape sequence (TUX-10). Builders for the
        sequences are in :mod:`agenthicc.tui.terminal.osc`.
        """
        ...

    def supports_progress(self) -> bool:
        """Whether this terminal draws ``OSC 9;4`` progress (TUX-10)."""
        ...

    def supports_hyperlinks(self) -> bool:
        """Whether this terminal opens ``OSC 8`` hyperlinks (TUX-10)."""
        ...


def get_backend() -> TerminalBackend:
    """Return the appropriate ``TerminalBackend`` for the current platform.

    This is the only place in the codebase that may branch on ``os.name``.
    """
    if os.name == "nt":
        from agenthicc.tui.terminal.windows_backend import WindowsBackend

        return WindowsBackend()
    from agenthicc.tui.terminal.posix_backend import PosixBackend

    return PosixBackend()
