---
title: "PRD-51: Filesystem Backend Protocol — Pluggable Storage Abstraction"
status: draft
version: 0.1.0
created: 2026-06-13
---

# PRD-51: Filesystem Backend Protocol

## Executive Summary

All 14 current fs tools call POSIX I/O directly through `WorkspaceView`.
This PRD introduces a `FilesystemBackend` Protocol that abstracts every
primitive operation, plus a `LinuxFilesystemBackend` as the default concrete
implementation.  Subsequent PRDs (S3, Windows, Pyodide) add more backends.
The tools themselves change minimally — they swap `WorkspaceView` calls for
backend calls.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `FilesystemBackend` Protocol declares every primitive the tools need |
| G2 | `LinuxFilesystemBackend` is the drop-in replacement for current POSIX I/O |
| G3 | `WorkspaceView` continues to enforce path safety; backends delegate to it |
| G4 | A `BackendRouter` selects the right backend for a given path |
| G5 | The `FsToolKit` factory accepts an optional backend, defaulting to Linux |
| G6 | All dataclasses (`FileStat`, `FileEntry`, `GrepMatch`) are defined here |
| G7 | Zero breaking changes to existing tool signatures or return shapes |

---

## Package Layout

```
src/agenthicc/tools/fs/
  __init__.py          existing tools + FsToolKit (updated to accept backend)
  agent_tools.py       @tool() wrappers (unchanged signatures)
  backend.py           NEW — FilesystemBackend Protocol + shared dataclasses
  linux.py             NEW — LinuxFilesystemBackend
  router.py            NEW — BackendRouter (path → backend)
```

---

## Shared Dataclasses (`backend.py`)

```python
# src/agenthicc/tools/fs/backend.py
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "FilesystemBackend",
    "FileStat",
    "FileEntry",
    "GrepMatch",
    "DiffHunk",
]


@dataclass(frozen=True)
class FileStat:
    path: str
    size: int                    # bytes; -1 for directories or unknown
    is_dir: bool
    is_file: bool
    modified_at: float           # Unix timestamp; 0 if unavailable
    created_at: float
    permissions: str = ""        # e.g. "644"; empty on backends that don't support it
    etag: str = ""               # S3 ETag / content hash; empty on local backends
    backend: str = "linux"       # originating backend name


@dataclass(frozen=True)
class FileEntry:
    name: str                    # basename
    path: str                    # path relative to the listed directory
    is_dir: bool
    size: int = -1


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_number: int
    line: str
    match_start: int = 0         # byte offset within line
    match_end: int = 0
```

---

## `FilesystemBackend` Protocol

```python
@runtime_checkable
class FilesystemBackend(Protocol):
    """Protocol every filesystem backend must implement.

    All *path* arguments are strings.  Relative paths are resolved against
    the backend's configured root (equivalent to WorkspaceView.root).
    Backends MUST raise PermissionError for any path that escapes the root.
    """

    # ── identity ──────────────────────────────────────────────────────────
    @property
    def name(self) -> str: ...        # "linux", "s3", "windows", "pyodide"
    @property
    def root(self) -> str: ...        # absolute root path (or "s3://bucket/prefix")

    # ── reads ────────────────────────────────────────────────────────────
    def read_bytes(self, path: str) -> bytes: ...
    def read_text(self, path: str, encoding: str = "utf-8") -> str: ...
    def read_lines(
        self, path: str, start: int = 1, end: int | None = None
    ) -> tuple[list[str], int]: ...   # (lines, total_line_count)

    # ── writes ───────────────────────────────────────────────────────────
    def write_bytes(
        self, path: str, data: bytes, create_parents: bool = True
    ) -> int: ...                      # returns bytes written
    def write_text(
        self, path: str, content: str, encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int: ...
    def append_text(self, path: str, content: str) -> int: ...
    def truncate(self, path: str, size: int = 0) -> None: ...

    # ── CRUD ─────────────────────────────────────────────────────────────
    def delete(self, path: str) -> None: ...
    def move(self, source: str, destination: str) -> None: ...
    def copy(self, source: str, destination: str) -> None: ...
    def make_directory(self, path: str, parents: bool = True) -> None: ...

    # ── queries ───────────────────────────────────────────────────────────
    def exists(self, path: str) -> bool: ...
    def stat(self, path: str) -> FileStat: ...
    def list_dir(
        self,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        include_hidden: bool = False,
    ) -> list[FileEntry]: ...
    def glob(
        self, pattern: str, path: str = ".", recursive: bool = True
    ) -> list[str]: ...
    def grep(
        self,
        regex: str,
        path: str = ".",
        recursive: bool = True,
        max_results: int = 100,
        case_sensitive: bool = True,
    ) -> list[GrepMatch]: ...

    # ── batch ─────────────────────────────────────────────────────────────
    def batch_read(
        self, paths: list[str], encoding: str = "utf-8"
    ) -> list[dict[str, Any]]: ...    # [{path, content, ok, error}]
    def batch_write(
        self, files: list[dict[str, str]], create_parents: bool = True
    ) -> list[dict[str, Any]]: ...    # [{path, ok, error, bytes_written}]
    def batch_delete(
        self, paths: list[str]
    ) -> list[dict[str, Any]]: ...    # [{path, ok, error}]
```

---

## `LinuxFilesystemBackend`

```python
# src/agenthicc/tools/fs/linux.py

import asyncio, fnmatch, os, re, shutil, stat
from pathlib import Path
from .backend import FilesystemBackend, FileStat, FileEntry, GrepMatch

class LinuxFilesystemBackend:
    """POSIX filesystem backend — wraps WorkspaceView for safety."""

    name = "linux"

    def __init__(self, root: str | Path = ".") -> None:
        from agenthicc.tools.sandbox import WorkspaceView
        self._view = WorkspaceView(root)

    @property
    def root(self) -> str:
        return str(self._view.root)

    # All Protocol methods delegate to self._view.resolve() then call
    # standard pathlib / os operations.

    def read_bytes(self, path: str) -> bytes:
        return self._view.resolve(path).read_bytes()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._view.resolve(path).read_text(encoding=encoding)

    def read_lines(
        self, path: str, start: int = 1, end: int | None = None
    ) -> tuple[list[str], int]:
        lines = self._view.resolve(path).read_text().splitlines()
        total = len(lines)
        s = max(0, start - 1)
        e = end if end is None else min(end, total)
        return lines[s:e], total

    def write_text(
        self, path: str, content: str, encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        p = self._view.resolve(path)
        if create_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return len(content.encode(encoding))

    def append_text(self, path: str, content: str) -> int:
        p = self._view.resolve(path)
        with p.open("a") as f:
            f.write(content)
        return len(content.encode())

    def truncate(self, path: str, size: int = 0) -> None:
        self._view.resolve(path).open("ab").truncate(size)

    def delete(self, path: str) -> None:
        p = self._view.resolve(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()

    def move(self, source: str, destination: str) -> None:
        shutil.move(str(self._view.resolve(source)), str(self._view.resolve(destination)))

    def copy(self, source: str, destination: str) -> None:
        shutil.copy2(str(self._view.resolve(source)), str(self._view.resolve(destination)))

    def make_directory(self, path: str, parents: bool = True) -> None:
        self._view.resolve(path).mkdir(parents=parents, exist_ok=True)

    def exists(self, path: str) -> bool:
        try:
            return self._view.resolve(path).exists()
        except PermissionError:
            return False

    def stat(self, path: str) -> FileStat:
        p = self._view.resolve(path)
        s = p.stat()
        return FileStat(
            path=path,
            size=s.st_size,
            is_dir=p.is_dir(),
            is_file=p.is_file(),
            modified_at=s.st_mtime,
            created_at=s.st_ctime,
            permissions=oct(stat.S_IMODE(s.st_mode))[2:],
            backend="linux",
        )

    def list_dir(
        self, path: str = ".", pattern: str = "*",
        recursive: bool = False, include_hidden: bool = False,
    ) -> list[FileEntry]:
        root = self._view.resolve(path)
        it = root.rglob(pattern) if recursive else root.glob(pattern)
        entries = []
        for p in sorted(it):
            if not include_hidden and p.name.startswith("."):
                continue
            entries.append(FileEntry(
                name=p.name,
                path=str(p.relative_to(self._view.root)),
                is_dir=p.is_dir(),
                size=p.stat().st_size if p.is_file() else -1,
            ))
        return entries

    def glob(
        self, pattern: str, path: str = ".", recursive: bool = True
    ) -> list[str]:
        root = self._view.resolve(path)
        it = root.rglob(pattern) if recursive else root.glob(pattern)
        return [str(p.relative_to(self._view.root)) for p in sorted(it)]

    def grep(
        self, regex: str, path: str = ".",
        recursive: bool = True, max_results: int = 100,
        case_sensitive: bool = True,
    ) -> list[GrepMatch]:
        flags = 0 if case_sensitive else re.IGNORECASE
        pat = re.compile(regex, flags)
        root = self._view.resolve(path)
        results: list[GrepMatch] = []
        targets = sorted(root.rglob("*") if recursive else root.glob("*"))
        for p in targets:
            if not p.is_file():
                continue
            try:
                for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    m = pat.search(line)
                    if m:
                        results.append(GrepMatch(
                            path=str(p.relative_to(self._view.root)),
                            line_number=i, line=line,
                            match_start=m.start(), match_end=m.end(),
                        ))
                        if len(results) >= max_results:
                            return results
            except (PermissionError, OSError):
                continue
        return results

    def batch_read(
        self, paths: list[str], encoding: str = "utf-8"
    ) -> list[dict]:
        results = []
        for path in paths:
            try:
                content = self.read_text(path, encoding)
                results.append({"path": path, "content": content, "ok": True, "error": None})
            except Exception as e:
                results.append({"path": path, "content": None, "ok": False, "error": str(e)})
        return results

    def batch_write(
        self, files: list[dict[str, str]], create_parents: bool = True
    ) -> list[dict]:
        results = []
        for f in files:
            path, content = f["path"], f["content"]
            try:
                n = self.write_text(path, content, create_parents=create_parents)
                results.append({"path": path, "ok": True, "error": None, "bytes_written": n})
            except Exception as e:
                results.append({"path": path, "ok": False, "error": str(e), "bytes_written": 0})
        return results

    def batch_delete(self, paths: list[str]) -> list[dict]:
        results = []
        for path in paths:
            try:
                self.delete(path)
                results.append({"path": path, "ok": True, "error": None})
            except Exception as e:
                results.append({"path": path, "ok": False, "error": str(e)})
        return results
```

---

## `BackendRouter`

```python
# src/agenthicc/tools/fs/router.py

from __future__ import annotations

from .backend import FilesystemBackend
from .linux import LinuxFilesystemBackend


class BackendRouter:
    """Select the correct backend for a given path or URI.

    Registration order matters: the first matching prefix wins.
    The fallback is always the Linux backend.

    Path matching rules:
      - Paths starting with "s3://"  → S3 backend (if registered)
      - All other paths              → Linux backend (default)

    Custom prefixes can be registered at session startup:
      router.register("s3://my-bucket/", s3_backend)
    """

    def __init__(self, default: FilesystemBackend | None = None) -> None:
        self._default = default or LinuxFilesystemBackend()
        self._routes: list[tuple[str, FilesystemBackend]] = []

    def register(self, prefix: str, backend: FilesystemBackend) -> None:
        self._routes.append((prefix, backend))

    def resolve(self, path: str) -> FilesystemBackend:
        for prefix, backend in self._routes:
            if path.startswith(prefix):
                return backend
        return self._default

    @property
    def default(self) -> FilesystemBackend:
        return self._default
```

---

## Updated `FsToolKit`

```python
class FsToolKit:
    def __init__(self, backend: FilesystemBackend | None = None) -> None:
        from .linux import LinuxFilesystemBackend
        self._backend = backend or LinuxFilesystemBackend()

    def tools(self) -> list[Tool]:
        return [
            ReadFileTool(self._backend),
            WriteFileTool(self._backend),
            # ... all tools receive the backend
        ]
```

Each tool stores `self._backend` and calls `self._backend.read_text(path)` instead of
`_view(context).read_text(path)`.  The `context` dict still passes `workspace_root`
for backwards compat, but the backend is the authoritative I/O handler.

---

## Tests

```python
# tests/unit/test_fs_backend.py  (pytestmark = pytest.mark.unit)

def test_linux_backend_write_read(tmp_path):
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("hello.txt", "world")
    assert b.read_text("hello.txt") == "world"

def test_linux_backend_path_escape_rejected(tmp_path):
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend
    b = LinuxFilesystemBackend(tmp_path)
    with pytest.raises(PermissionError):
        b.read_text("../../etc/passwd")

def test_linux_backend_grep(tmp_path):
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend
    b = LinuxFilesystemBackend(tmp_path)
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    matches = b.grep("def foo", ".")
    assert len(matches) == 1
    assert matches[0].line_number == 1

def test_backend_router_default_is_linux(tmp_path):
    from agenthicc.tools.fs.router import BackendRouter
    r = BackendRouter()
    assert r.resolve("/some/path").name == "linux"

def test_backend_router_s3_prefix():
    from agenthicc.tools.fs.router import BackendRouter
    from unittest.mock import MagicMock
    mock_s3 = MagicMock(); mock_s3.name = "s3"
    r = BackendRouter()
    r.register("s3://", mock_s3)
    assert r.resolve("s3://my-bucket/file.txt").name == "s3"

def test_linux_batch_read(tmp_path):
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend
    b = LinuxFilesystemBackend(tmp_path)
    (tmp_path / "a.txt").write_text("aaa")
    (tmp_path / "b.txt").write_text("bbb")
    results = b.batch_read(["a.txt", "b.txt"])
    assert all(r["ok"] for r in results)
    assert results[0]["content"] == "aaa"

def test_linux_batch_write(tmp_path):
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend
    b = LinuxFilesystemBackend(tmp_path)
    results = b.batch_write([
        {"path": "x.txt", "content": "x"},
        {"path": "y.txt", "content": "y"},
    ])
    assert all(r["ok"] for r in results)
    assert (tmp_path / "x.txt").read_text() == "x"

def test_filesystembackend_protocol_check():
    from agenthicc.tools.fs.backend import FilesystemBackend
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend
    assert isinstance(LinuxFilesystemBackend(), FilesystemBackend)
```
