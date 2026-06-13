"""LinuxFilesystemBackend — local POSIX implementation of FilesystemBackend."""
from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from agenthicc.tools.fs.backend import FileEntry, FileStat, FilesystemBackend, GrepMatch
from agenthicc.tools.sandbox import WorkspaceView

__all__ = ["LinuxFilesystemBackend"]


class LinuxFilesystemBackend:
    """Concrete filesystem backend that operates within a sandboxed workspace.

    All path arguments are validated through
    :class:`~agenthicc.tools.sandbox.WorkspaceView` before any I/O is
    attempted, preventing path-traversal attacks.

    Satisfies ``isinstance(LinuxFilesystemBackend(), FilesystemBackend)``
    because it provides every method and property declared in the Protocol.
    """

    def __init__(self, root: str | Path = ".") -> None:
        self._view = WorkspaceView(root)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "linux"

    @property
    def root(self) -> str:
        return str(self._view.root)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Resolve *path* through the workspace view."""
        return self._view.resolve(path)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_bytes(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._resolve(path).read_text(encoding=encoding)

    def read_lines(
        self,
        path: str,
        start: int = 1,
        end: int | None = None,
    ) -> tuple[list[str], int]:
        """Return ``(lines[start-1:end], total_line_count)``.

        *start* and *end* are 1-indexed and inclusive.
        """
        all_lines = self._resolve(path).read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(all_lines)
        s = max(0, start - 1)
        e = total if end is None else min(total, end)
        return all_lines[s:e], total

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def write_bytes(
        self,
        path: str,
        data: bytes,
        create_parents: bool = True,
    ) -> int:
        resolved = self._resolve(path)
        if create_parents:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(data)
        return len(data)

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        resolved = self._resolve(path)
        if create_parents:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding=encoding)
        return len(content.encode(encoding))

    def append_text(self, path: str, content: str) -> int:
        resolved = self._resolve(path)
        with open(resolved, "a", encoding="utf-8") as fh:
            fh.write(content)
        return len(content.encode("utf-8"))

    def truncate(self, path: str, size: int = 0) -> None:
        with open(self._resolve(path), "r+b") as fh:
            fh.truncate(size)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def delete(self, path: str) -> None:
        resolved = self._resolve(path)
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()

    def move(self, src: str, dst: str) -> None:
        shutil.move(str(self._resolve(src)), str(self._resolve(dst)))

    def copy(self, src: str, dst: str) -> None:
        shutil.copy2(str(self._resolve(src)), str(self._resolve(dst)))

    def make_directory(self, path: str, parents: bool = True) -> None:
        self._resolve(path).mkdir(parents=parents, exist_ok=True)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        try:
            return self._resolve(path).exists()
        except PermissionError:
            return False

    def stat(self, path: str) -> FileStat:
        resolved = self._resolve(path)
        s = os.stat(resolved)
        return FileStat(
            path=str(resolved),
            size=s.st_size,
            is_dir=resolved.is_dir(),
            is_file=resolved.is_file(),
            modified_at=s.st_mtime,
            created_at=s.st_ctime,
            permissions=oct(stat.S_IMODE(s.st_mode))[2:],
            etag="",
            backend="linux",
        )

    def list_dir(
        self,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        include_hidden: bool = False,
    ) -> list[FileEntry]:
        resolved = self._resolve(path)
        glob_fn = resolved.rglob if recursive else resolved.glob
        entries: list[FileEntry] = []
        for p in sorted(glob_fn(pattern)):
            if not include_hidden and p.name.startswith("."):
                continue
            try:
                s = p.stat()
                entries.append(
                    FileEntry(
                        name=p.name,
                        path=str(p.relative_to(resolved)),
                        is_dir=p.is_dir(),
                        size=s.st_size,
                    )
                )
            except OSError:
                pass
        return entries

    def glob(
        self,
        pattern: str,
        path: str = ".",
        recursive: bool = True,
    ) -> list[str]:
        resolved = self._resolve(path)
        glob_fn = resolved.rglob if recursive else resolved.glob
        return [str(p.relative_to(resolved)) for p in sorted(glob_fn(pattern))]

    def grep(
        self,
        regex: str,
        path: str = ".",
        recursive: bool = True,
        max_results: int = 100,
        case_sensitive: bool = True,
    ) -> list[GrepMatch]:
        resolved = self._resolve(path)
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(regex, flags)
        glob_fn = resolved.rglob if recursive else resolved.glob
        matches: list[GrepMatch] = []
        for p in sorted(glob_fn("*")):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            rel_path = str(p.relative_to(resolved))
            for line_no, line in enumerate(text.splitlines(), 1):
                m = compiled.search(line)
                if m:
                    matches.append(
                        GrepMatch(
                            path=rel_path,
                            line_number=line_no,
                            line=line.rstrip(),
                            match_start=m.start(),
                            match_end=m.end(),
                        )
                    )
                    if len(matches) >= max_results:
                        return matches
        return matches

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    def batch_read(
        self,
        paths: list[str],
        encoding: str = "utf-8",
    ) -> list[dict]:
        results: list[dict] = []
        for path in paths:
            try:
                content = self.read_text(path, encoding=encoding)
                results.append({"path": path, "content": content, "ok": True, "error": ""})
            except Exception as exc:
                results.append({"path": path, "content": None, "ok": False, "error": str(exc)})
        return results

    def batch_write(
        self,
        files: list[dict],
        create_parents: bool = True,
    ) -> list[dict]:
        """Write multiple files.

        Each entry in *files* must contain ``"path"`` and ``"content"`` keys.
        An optional ``"encoding"`` key is respected (default ``"utf-8"``).
        """
        results: list[dict] = []
        for entry in files:
            path = entry.get("path", "")
            content = entry.get("content", "")
            encoding = entry.get("encoding", "utf-8")
            try:
                bytes_written = self.write_text(
                    path, content, encoding=encoding, create_parents=create_parents
                )
                results.append(
                    {"path": path, "ok": True, "error": "", "bytes_written": bytes_written}
                )
            except Exception as exc:
                results.append(
                    {"path": path, "ok": False, "error": str(exc), "bytes_written": 0}
                )
        return results

    def batch_delete(self, paths: list[str]) -> list[dict]:
        results: list[dict] = []
        for path in paths:
            try:
                self.delete(path)
                results.append({"path": path, "ok": True, "error": ""})
            except Exception as exc:
                results.append({"path": path, "ok": False, "error": str(exc)})
        return results


# ---------------------------------------------------------------------------
# Runtime Protocol conformance check — catches regressions at import time.
# ---------------------------------------------------------------------------
assert isinstance(
    LinuxFilesystemBackend(), FilesystemBackend
), "LinuxFilesystemBackend does not satisfy the FilesystemBackend Protocol"
