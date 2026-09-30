"""POSIX terminal backend — delegates to cbreak_reader (PRD-106)."""

from __future__ import annotations

import os
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
        self._cancel_read_fd: int | None = None
        self._cancel_write_fd: int | None = None

    # ── TerminalBackend interface ─────────────────────────────────────────────

    def is_interactive(self) -> bool:
        """True when stdin is a real TTY with an accessible file descriptor."""
        if not sys.stdin.isatty():
            return False
        return self._resolve_fd() is not None

    def read_key(self) -> tuple[Key, str]:
        """Read one key; a wake pipe lets shutdown interrupt an idle read."""
        fd = self._resolve_fd()
        if fd is None:
            raise OSError("stdin has no file descriptor")
        cancel_fd = self._ensure_cancel_pipe()
        return _read_key(fd, cancel_fd=cancel_fd)

    @contextmanager
    def enter_raw_mode(self) -> Generator[None, None, None]:
        """Enable CBREAK mode; restore original settings on exit."""
        fd = self._resolve_fd()
        if fd is None:
            yield
            return
        # Create the wake pipe on the event-loop thread before a key-reader
        # executor task can start, so restore() cannot race its initialization.
        self._ensure_cancel_pipe()
        with _raw_mode(fd):
            try:
                yield
            finally:
                self.restore()

    def restore(self) -> None:
        """Wake a worker blocked in :meth:`read_key` during shutdown."""
        write_fd = self._cancel_write_fd
        if write_fd is None:
            return
        try:
            os.write(write_fd, b"\x00")
        except BlockingIOError:
            pass  # A wake byte is already pending.
        except OSError:
            pass  # The backend may already have been closed.

    def close(self) -> None:
        """Release the cancellation pipe after its reader has stopped."""
        for fd in (self._cancel_read_fd, self._cancel_write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._cancel_read_fd = None
        self._cancel_write_fd = None

    def __del__(self) -> None:
        # A read cancelled before its executor callable starts has no reader
        # that can close the pipe itself. The manager explicitly closes the
        # normal path; this is a final safeguard for that queued-cancellation
        # edge case.
        self.close()

    # ── internals ─────────────────────────────────────────────────────────────

    def _resolve_fd(self) -> int | None:
        if self._fd is not None:
            return self._fd
        try:
            self._fd = sys.stdin.fileno()
            return self._fd
        except Exception:  # noqa: BLE001
            return None

    def _ensure_cancel_pipe(self) -> int:
        if self._cancel_read_fd is None or self._cancel_write_fd is None:
            read_fd, write_fd = os.pipe()
            os.set_blocking(write_fd, False)
            self._cancel_read_fd = read_fd
            self._cancel_write_fd = write_fd
        return self._cancel_read_fd
