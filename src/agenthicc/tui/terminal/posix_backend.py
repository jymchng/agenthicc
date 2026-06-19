"""POSIX terminal backend — delegates to cbreak_reader (PRD-106)."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Generator

from agenthicc.tui.cbreak_reader import Key, raw_mode as _raw_mode, read_key as _read_key

__all__ = ["PosixBackend"]


class PosixBackend:
    """Terminal backend for Linux and macOS using ``termios`` / ``tty``.

    ``raw_mode`` and ``read_key`` from ``cbreak_reader`` are the sole owners
    of all ``termios`` imports; this class is a thin coordinator.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    # ── TerminalBackend interface ─────────────────────────────────────────────

    def is_interactive(self) -> bool:
        """True when stdin is a real TTY with an accessible file descriptor."""
        if not sys.stdin.isatty():
            return False
        return self._resolve_fd() is not None

    def read_key(self) -> tuple[Key, str]:
        """Read one keystroke from stdin; blocks until a key arrives."""
        fd = self._resolve_fd()
        if fd is None:
            raise OSError("stdin has no file descriptor")
        return _read_key(fd)

    @contextmanager
    def enter_raw_mode(self) -> Generator[None, None, None]:
        """Enable CBREAK mode; restore original settings on exit."""
        fd = self._resolve_fd()
        if fd is None:
            yield
            return
        with _raw_mode(fd):
            yield

    def restore(self) -> None:
        pass  # _raw_mode context manager handles restore on exit

    # ── internals ─────────────────────────────────────────────────────────────

    def _resolve_fd(self) -> int | None:
        if self._fd is not None:
            return self._fd
        try:
            self._fd = sys.stdin.fileno()
            return self._fd
        except Exception:  # noqa: BLE001
            return None
