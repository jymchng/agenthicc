"""TerminalBackend Protocol and platform backend factory (PRD-106).

``get_backend()`` is the single permitted platform-specific branch:

    if os.name == "nt":   → WindowsBackend (msvcrt)
    else:                 → PosixBackend   (termios / tty)

All application code must use the backend interface; no feature code may
import ``msvcrt``, ``termios``, or ``tty`` directly.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agenthicc.tui.cbreak_reader import Key

__all__ = ["TerminalBackend", "get_backend"]


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


def get_backend() -> TerminalBackend:
    """Return the appropriate ``TerminalBackend`` for the current platform.

    This is the only place in the codebase that may branch on ``os.name``.
    """
    if os.name == "nt":
        from agenthicc.tui.terminal.windows_backend import WindowsBackend  # noqa: PLC0415

        return WindowsBackend()
    from agenthicc.tui.terminal.posix_backend import PosixBackend  # noqa: PLC0415

    return PosixBackend()
