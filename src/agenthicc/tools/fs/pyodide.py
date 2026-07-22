"""PyodideFilesystemBackend — Emscripten MEMFS backend for Pyodide (PRD-51).

This backend is only importable inside a Pyodide environment.  It wraps the
Emscripten in-memory filesystem (``pyodide.FS``) and exposes the full
:class:`~agenthicc.tools.fs.backend.FilesystemBackend` interface.

All paths are resolved against ``self._root`` (default: ``/workspace``).
Relative paths are joined to the root; absolute paths are used as-is,
provided they do not escape the root.
"""

from __future__ import annotations

import posixpath
import re

from .backend import FilesystemBackend, FileStat, FileEntry, GrepMatch

__all__ = ["PyodideFilesystemBackend"]

# Emscripten mode constant for directories.
_S_IFDIR = 0o040000
_MODE_MASK = 0o170000


class PyodideFilesystemBackend(FilesystemBackend):  # type: ignore[misc]
    """Filesystem backend backed by Emscripten MEMFS via ``pyodide.FS``.

    Args:
        root: Absolute MEMFS path used as the workspace root.
              Created on construction if it does not yet exist.

    Raises:
        ImportError: When instantiated outside a Pyodide environment.
    """

    name = "pyodide"

    def __init__(self, root: str = "/workspace") -> None:
        try:
            import pyodide  # noqa: F401
        except ImportError:
            raise ImportError("PyodideFilesystemBackend requires Pyodide environment")

        import pyodide.FS as _FS  # type: ignore[import]

        self._fs = _FS
        self._root = root
        try:
            self._fs.mkdir(root)
        except Exception:
            pass  # Directory already exists — ignore

    # ── identity ─────────────────────────────────────────────────────────

    @property
    def root(self) -> str:
        return self._root

    # ── internal helpers ─────────────────────────────────────────────────

    def _resolve(self, path: str) -> str:
        """Return a normalised absolute MEMFS path that stays within *root*.

        Raises:
            PermissionError: When the resolved path would escape the root.
        """
        if path.startswith("/"):
            abs_path = path
        else:
            abs_path = f"{self._root}/{path.lstrip('/')}"
        normed = posixpath.normpath(abs_path)
        if not normed.startswith(self._root):
            raise PermissionError(f"Path '{path}' resolves outside workspace root '{self._root}'.")
        return normed

    def _ensure_dir(self, path: str) -> None:
        """Create *path* and all missing parent directories."""
        parts = path.split("/")
        current = ""
        for part in parts:
            if not part:
                continue
            current = f"{current}/{part}"
            try:
                self._fs.mkdir(current)
            except Exception:
                pass  # Already exists or not a directory error — best-effort

    # ── reads ────────────────────────────────────────────────────────────

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._fs.readFile(self._resolve(path), {"encoding": "utf8"})

    def read_bytes(self, path: str) -> bytes:
        return self._fs.readFile(self._resolve(path))

    def read_lines(
        self, path: str, start: int = 1, end: int | None = None
    ) -> tuple[list[str], int]:
        lines = self.read_text(path).splitlines()
        total = len(lines)
        s = max(0, start - 1)
        e = end if end is None else min(end, total)
        return lines[s:e], total

    # ── writes ───────────────────────────────────────────────────────────

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        abs_path = self._resolve(path)
        if create_parents:
            parent = posixpath.dirname(abs_path)
            self._ensure_dir(parent)
        self._fs.writeFile(abs_path, content, {"encoding": "utf8"})
        return len(content.encode(encoding))

    def write_bytes(self, path: str, data: bytes, create_parents: bool = True) -> int:
        abs_path = self._resolve(path)
        if create_parents:
            parent = posixpath.dirname(abs_path)
            self._ensure_dir(parent)
        self._fs.writeFile(abs_path, data)
        return len(data)

    def append_text(self, path: str, content: str) -> int:
        try:
            existing = self.read_text(path)
        except Exception:
            existing = ""
        return self.write_text(path, existing + content)

    def truncate(self, path: str, size: int = 0) -> None:
        data = self.read_bytes(path)
        self.write_bytes(path, data[:size])

    # ── CRUD ─────────────────────────────────────────────────────────────

    def delete(self, path: str) -> None:
        self._fs.unlink(self._resolve(path))

    def move(self, src: str, dst: str) -> None:
        data = self.read_bytes(src)
        self.write_bytes(dst, data)
        self.delete(src)

    def copy(self, src: str, dst: str) -> None:
        self.write_bytes(dst, self.read_bytes(src))

    def make_directory(self, path: str, parents: bool = True) -> None:
        self._ensure_dir(self._resolve(path))

    # ── queries ───────────────────────────────────────────────────────────

    def exists(self, path: str) -> bool:
        try:
            self._fs.stat(self._resolve(path))
            return True
        except Exception:
            return False

    def stat(self, path: str) -> FileStat:
        s = self._fs.stat(self._resolve(path))
        mode = int(s.mode)
        is_dir = (mode & _MODE_MASK) == _S_IFDIR
        return FileStat(
            path=path,
            size=int(s.size),
            is_dir=is_dir,
            is_file=not is_dir,
            modified_at=float(s.mtime) / 1000,
            created_at=float(s.ctime) / 1000,
            backend="pyodide",
        )

    def list_dir(
        self,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        include_hidden: bool = False,
    ) -> list[FileEntry]:
        abs_path = self._resolve(path)
        entries: list[FileEntry] = []

        def _visit(dir_path: str) -> None:
            try:
                names = self._fs.readdir(dir_path)
            except Exception:
                return
            for name in sorted(names):
                if name in (".", ".."):
                    continue
                if not include_hidden and name.startswith("."):
                    continue
                child = f"{dir_path}/{name}"
                try:
                    s = self._fs.stat(child)
                    mode = int(s.mode)
                    child_is_dir = (mode & _MODE_MASK) == _S_IFDIR
                except Exception:
                    continue
                # Simple glob pattern match against name
                import fnmatch

                if fnmatch.fnmatch(name, pattern):
                    rel = posixpath.relpath(child, abs_path)
                    entries.append(
                        FileEntry(
                            name=name,
                            path=rel,
                            is_dir=child_is_dir,
                            size=-1 if child_is_dir else int(s.size),
                        )
                    )
                if recursive and child_is_dir:
                    _visit(child)

        _visit(abs_path)
        return entries

    def glob(self, pattern: str, path: str = ".", recursive: bool = True) -> list[str]:
        entries = self.list_dir(path, pattern="*", recursive=recursive, include_hidden=False)
        import fnmatch

        return [e.path for e in entries if fnmatch.fnmatch(e.name, pattern)]

    def grep(
        self,
        regex: str,
        path: str = ".",
        recursive: bool = True,
        max_results: int = 100,
        case_sensitive: bool = True,
    ) -> list[GrepMatch]:
        flags = 0 if case_sensitive else re.IGNORECASE
        pat = re.compile(regex, flags)
        abs_path = self._resolve(path)
        results: list[GrepMatch] = []

        def _search_dir(dir_path: str) -> None:
            try:
                names = self._fs.readdir(dir_path)
            except Exception:
                return
            for name in sorted(names):
                if name in (".", ".."):
                    continue
                child = f"{dir_path}/{name}"
                try:
                    s = self._fs.stat(child)
                    mode = int(s.mode)
                    child_is_dir = (mode & _MODE_MASK) == _S_IFDIR
                except Exception:
                    continue
                if child_is_dir:
                    if recursive:
                        _search_dir(child)
                else:
                    try:
                        text = self._fs.readFile(child, {"encoding": "utf8"})
                    except Exception:
                        continue
                    rel = posixpath.relpath(child, abs_path)
                    for i, line in enumerate(text.splitlines(), 1):
                        m = pat.search(line)
                        if m:
                            results.append(
                                GrepMatch(
                                    path=rel,
                                    line_number=i,
                                    line=line,
                                    match_start=m.start(),
                                    match_end=m.end(),
                                )
                            )
                            if len(results) >= max_results:
                                return

        _search_dir(abs_path)
        return results

    # ── batch ─────────────────────────────────────────────────────────────
    # MEMFS has no async parallelism benefit — simple sequential loops.

    def batch_read(self, paths: list[str], encoding: str = "utf-8") -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for path in paths:
            try:
                content = self.read_text(path, encoding)
                results.append({"path": path, "content": content, "ok": True, "error": None})
            except Exception as exc:
                results.append({"path": path, "content": None, "ok": False, "error": str(exc)})
        return results

    def batch_write(
        self, files: list[dict[str, str]], create_parents: bool = True
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for f in files:
            path, content = f["path"], f["content"]
            try:
                n = self.write_text(path, content, create_parents=create_parents)
                results.append({"path": path, "ok": True, "error": None, "bytes_written": n})
            except Exception as exc:
                results.append({"path": path, "ok": False, "error": str(exc), "bytes_written": 0})
        return results

    def batch_delete(self, paths: list[str]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for path in paths:
            try:
                self.delete(path)
                results.append({"path": path, "ok": True, "error": None})
            except Exception as exc:
                results.append({"path": path, "ok": False, "error": str(exc)})
        return results
