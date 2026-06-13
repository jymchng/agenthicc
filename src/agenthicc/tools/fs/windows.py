"""WindowsFilesystemBackend — Windows path-safety layer on top of Linux (PRD-51)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from .backend import FileStat
from .linux import LinuxFilesystemBackend

__all__ = ["WindowsFilesystemBackend"]

# Windows reserved device names that must never be used as file stems.
_RESERVED: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class WindowsFilesystemBackend(LinuxFilesystemBackend):
    """Filesystem backend with Windows-specific path restrictions.

    Inherits all POSIX I/O from :class:`LinuxFilesystemBackend` and adds:

    * Rejection of Windows reserved device names (CON, NUL, COM1 … COM9,
      LPT1 … LPT9, PRN, AUX) before any write or destructive operation.
    * ``stat()`` returns a :class:`FileStat` with ``permissions=""`` and
      ``backend="windows"`` because Windows ACLs differ from POSIX mode bits.
    * ``symlink()`` raises :exc:`NotImplementedError` — Windows symbolic
      links require elevation; use shortcuts instead.
    """

    name = "windows"

    # ── reserved-name guard ───────────────────────────────────────────────

    def _check_reserved(self, path: str) -> None:
        """Raise :exc:`PermissionError` if *path*'s stem is a reserved name."""
        stem = Path(path).stem.upper()
        if stem in _RESERVED:
            raise PermissionError(
                f"Windows reserved device name '{stem}' is not allowed as a filename."
            )

    # ── write / destructive overrides ─────────────────────────────────────

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        self._check_reserved(path)
        return super().write_text(path, content, encoding=encoding, create_parents=create_parents)

    def write_bytes(
        self, path: str, data: bytes, create_parents: bool = True
    ) -> int:
        self._check_reserved(path)
        return super().write_bytes(path, data, create_parents=create_parents)

    def delete(self, path: str) -> None:
        self._check_reserved(path)
        super().delete(path)

    def move(self, src: str, dst: str) -> None:
        self._check_reserved(dst)
        super().move(src, dst)

    def copy(self, src: str, dst: str) -> None:
        self._check_reserved(dst)
        super().copy(src, dst)

    # ── stat override ────────────────────────────────────────────────────

    def stat(self, path: str) -> FileStat:
        """Return stat with ``permissions=""`` and ``backend="windows"``."""
        result = super().stat(path)
        return dataclasses.replace(result, permissions="", backend="windows")

    # ── symlink ──────────────────────────────────────────────────────────

    def symlink(self, target: str, link_path: str) -> None:  # type: ignore[override]
        """Not supported — Windows symlinks require elevation.

        Raises:
            NotImplementedError: Always.  Use Windows shortcuts instead.
        """
        raise NotImplementedError(
            "Symlinks on Windows require elevation. Use shortcuts instead."
        )
