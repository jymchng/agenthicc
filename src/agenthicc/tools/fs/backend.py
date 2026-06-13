"""FilesystemBackend Protocol and associated data structures."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["FilesystemBackend", "FileStat", "FileEntry", "GrepMatch"]


@dataclass(frozen=True)
class FileStat:
    """Metadata record returned by :meth:`FilesystemBackend.stat`."""

    path: str
    size: int
    is_dir: bool
    is_file: bool
    modified_at: float
    created_at: float
    permissions: str = ""
    etag: str = ""
    backend: str = "linux"


@dataclass(frozen=True)
class FileEntry:
    """Single entry returned by :meth:`FilesystemBackend.list_dir`."""

    name: str
    path: str
    is_dir: bool
    size: int = -1


@dataclass(frozen=True)
class GrepMatch:
    """A single line that matched a grep pattern."""

    path: str
    line_number: int
    line: str
    match_start: int = 0
    match_end: int = 0


@runtime_checkable
class FilesystemBackend(Protocol):
    """Abstract contract for every filesystem backend.

    Implementors must satisfy this Protocol so that
    ``isinstance(backend, FilesystemBackend)`` returns True at runtime.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Short identifier for this backend (e.g. ``"linux"``)."""
        ...

    @property
    def root(self) -> str:
        """Absolute workspace root path enforced by this backend."""
        ...

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_bytes(self, path: str) -> bytes:
        """Return the raw byte content of *path*."""
        ...

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Return the decoded text content of *path*."""
        ...

    def read_lines(
        self,
        path: str,
        start: int = 1,
        end: int | None = None,
    ) -> tuple[list[str], int]:
        """Return ``(lines[start-1:end], total_line_count)`` for *path*.

        *start* and *end* are 1-indexed and inclusive.
        """
        ...

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def write_bytes(
        self,
        path: str,
        data: bytes,
        create_parents: bool = True,
    ) -> int:
        """Write *data* to *path*; return bytes written."""
        ...

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        """Write *content* to *path*; return bytes written."""
        ...

    def append_text(self, path: str, content: str) -> int:
        """Append *content* to *path*; return bytes appended."""
        ...

    def truncate(self, path: str, size: int = 0) -> None:
        """Truncate *path* to *size* bytes (default 0 = empty)."""
        ...

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def delete(self, path: str) -> None:
        """Delete a file or directory tree at *path*."""
        ...

    def move(self, src: str, dst: str) -> None:
        """Move *src* to *dst*."""
        ...

    def copy(self, src: str, dst: str) -> None:
        """Copy *src* to *dst*."""
        ...

    def make_directory(self, path: str, parents: bool = True) -> None:
        """Create directory at *path*, optionally creating parent dirs."""
        ...

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        """Return True if *path* exists."""
        ...

    def stat(self, path: str) -> FileStat:
        """Return a :class:`FileStat` for *path*."""
        ...

    def list_dir(
        self,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        include_hidden: bool = False,
    ) -> list[FileEntry]:
        """Return directory entries matching *pattern* under *path*."""
        ...

    def glob(
        self,
        pattern: str,
        path: str = ".",
        recursive: bool = True,
    ) -> list[str]:
        """Return paths matching glob *pattern* under *path*."""
        ...

    def grep(
        self,
        regex: str,
        path: str = ".",
        recursive: bool = True,
        max_results: int = 100,
        case_sensitive: bool = True,
    ) -> list[GrepMatch]:
        """Search file contents for *regex*; return matching lines."""
        ...

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    def batch_read(
        self,
        paths: list[str],
        encoding: str = "utf-8",
    ) -> list[dict]:
        """Read multiple files.

        Each result dict has keys: ``path``, ``content``, ``ok``, ``error``.
        """
        ...

    def batch_write(
        self,
        files: list[dict],
        create_parents: bool = True,
    ) -> list[dict]:
        """Write multiple files.

        Each result dict has keys: ``path``, ``ok``, ``error``, ``bytes_written``.
        """
        ...

    def batch_delete(self, paths: list[str]) -> list[dict]:
        """Delete multiple paths.

        Each result dict has keys: ``path``, ``ok``, ``error``.
        """
        ...
