"""Keep stray stderr and warnings off the TUI screen (TUX-08 §2.3).

While the TUI is live, anything a library writes to ``sys.stderr`` — and every
``warnings.warn`` — goes to the session's diagnostics log,
``~/.agenthicc/logs/<session>.log``, instead of the transcript. The first
stray write of a session calls a callback once, which shows the transient
notice "Diagnostics were written to the log · /logs".

The session installs the log right after the first frame, so this module
costs the first frame nothing, and hands it to
:meth:`~agenthicc.tui.workspace.workspace.Workspace.redirect_stderr`, which
makes it ``sys.stderr`` while the live block is up. ``--debug`` installs no
log, so stderr reaches the screen as before.
"""

from __future__ import annotations

import io
import re
import threading
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

__all__ = [
    "DIAGNOSTICS_NOTICE",
    "DiagnosticsLog",
    "diagnostics_log_path",
    "install_stderr_sink",
    "log_tail",
    "stderr_sink",
    "uninstall_stderr_sink",
]

#: The one-time transient notice after the first stray write of a session.
DIAGNOSTICS_NOTICE = "Diagnostics were written to the log · /logs"


def diagnostics_log_path(session_id: str) -> Path:
    """The session's diagnostics log: ``~/.agenthicc/logs/<session>.log``."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", session_id)[:128] or "session"
    return Path.home() / ".agenthicc" / "logs" / f"{safe}.log"


class _ShowWarning(Protocol):
    def __call__(
        self,
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = ...,
        line: str | None = ...,
    ) -> None: ...


class DiagnosticsLog(io.TextIOBase):
    """A text stream that appends to *path*; the first write calls *on_first_write*."""

    def __init__(self, path: Path, on_first_write: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.path = path
        self._on_first_write = on_first_write
        self._notified = False
        self._lock = threading.Lock()
        self._file: TextIO | None = None

    encoding = "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def _open(self) -> TextIO:
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")
        return self._file

    def fileno(self) -> int:
        """The log file's descriptor, so a child given ``sys.stderr`` writes to it too."""
        with self._lock:
            return self._open().fileno()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            handle = self._open()
            handle.write(text)
            handle.flush()
            first = not self._notified and bool(text.strip())
            if first:
                self._notified = True
        if first and self._on_first_write is not None:
            try:
                self._on_first_write()
            except Exception:  # noqa: BLE001 - a diagnostics notice must never raise into a writer
                pass
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
        super().close()


_sink: DiagnosticsLog | None = None
_previous_showwarning: _ShowWarning | None = None


def stderr_sink() -> DiagnosticsLog | None:
    """The session's installed diagnostics log, or ``None`` (``--debug``)."""
    return _sink


def install_stderr_sink(sink: DiagnosticsLog) -> None:
    """Make *sink* the session's diagnostics log and send every warning to it.

    Stray ``sys.stderr`` reaches it through the workspace's live block
    (:meth:`~agenthicc.tui.workspace.workspace.Workspace.redirect_stderr`).
    """
    global _sink, _previous_showwarning
    if _sink is None:
        _previous_showwarning = warnings.showwarning

    def _show(
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = None,
        line: str | None = None,
    ) -> None:
        sink.write(warnings.formatwarning(message, category, filename, lineno, line))

    _sink = sink
    warnings.showwarning = _show


def uninstall_stderr_sink() -> None:
    """Restore warnings to stderr and close the sink."""
    global _sink, _previous_showwarning
    sink, _sink = _sink, None
    if _previous_showwarning is not None:
        warnings.showwarning = _previous_showwarning
        _previous_showwarning = None
    if sink is not None:
        sink.close()


def log_tail(path: Path, *, max_lines: int = 200, max_bytes: int = 256_000) -> list[str]:
    """The last *max_lines* lines of the log at *path* (``[]`` when absent)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, io.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line may be cut
    return lines[-max_lines:]
