---
title: "PRD-14: Filesystem Tools — read_file, write_file, grep_files, patch_file and more"
status: draft
version: 0.1.0
created: 2025-01-01
---

# PRD-14: Filesystem Tools

## Executive Summary

Agents need to read, write, search, and patch files. This PRD specifies a `tools/fs/` subpackage containing 14 filesystem tools — all inheriting from `Tool` ABC, all scoped to a `workspace_root` via `WorkspaceView`, and all using `asyncio.to_thread` for non-blocking I/O. The full catalog covers reading (with line range), writing, appending, deleting, moving, copying, directory listing, file search by glob pattern, content search (grep), file info, and a surgical `patch_file` tool that replaces an exact string match — the agent's primary code-editing primitive.

## Tool Catalog

| Tool | Key Parameters | Returns |
|------|---------------|---------|
| `read_file` | `path, encoding="utf-8"` | `{content, size_bytes, encoding}` |
| `write_file` | `path, content, create_parents=True` | `{ok, path, bytes_written}` |
| `append_file` | `path, content` | `{ok, path}` |
| `delete_file` | `path` | `{ok, path}` |
| `move_file` | `source, destination` | `{ok, source, destination}` |
| `copy_file` | `source, destination` | `{ok, source, destination}` |
| `list_directory` | `path=".", pattern="*", recursive=False, include_hidden=False` | `{entries:[{name,path,type,size_bytes,modified_at}], count}` |
| `make_directory` | `path` | `{ok, path}` |
| `file_exists` | `path` | `{exists, path, type}` |
| `search_files` | `pattern, path=".", recursive=True` | `{matches:[str], count}` |
| `grep_files` | `pattern, path=".", recursive=True, max_results=100` | `{matches:[{file,line_number,line}], count}` |
| `get_file_info` | `path` | `{path,size_bytes,modified_at,created_at,type,permissions}` |
| `read_lines` | `path, start=1, end=None` | `{lines:[str], total_lines, start, end}` |
| `patch_file` | `path, old_content, new_content` | `{ok, replacements}` |

## Data Structures and Interfaces

```python
# src/agenthicc/tools/fs/__init__.py
from .toolkit import FsToolKit
__all__ = ["FsToolKit"]
```

```python
# src/agenthicc/tools/fs/toolkit.py
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenthicc.tools.base import Tool
from agenthicc.tools.sandbox import WorkspaceView

__all__ = ["FsToolKit"]

MAX_FILE_SIZE_DEFAULT = 10 * 1024 * 1024  # 10 MB


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the full contents of a file within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file."},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: WorkspaceView, max_size: int = MAX_FILE_SIZE_DEFAULT) -> None:
        self._ws = workspace
        self._max_size = max_size

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        resolved = self._ws.resolve(args["path"])
        size = resolved.stat().st_size
        if size > self._max_size:
            raise ValueError(f"File too large ({size} bytes > {self._max_size} limit)")
        encoding = args.get("encoding", "utf-8")
        content = await asyncio.to_thread(resolved.read_text, encoding=encoding)
        return {"content": content, "size_bytes": size, "encoding": encoding}


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write (overwrite) a file within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "encoding": {"type": "string", "default": "utf-8"},
            "create_parents": {"type": "boolean", "default": True},
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace: WorkspaceView) -> None:
        self._ws = workspace

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        resolved = self._ws.resolve(args["path"])
        if args.get("create_parents", True):
            await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
        encoding = args.get("encoding", "utf-8")
        content = args["content"]
        await asyncio.to_thread(resolved.write_text, content, encoding=encoding)
        return {"ok": True, "path": str(resolved.relative_to(self._ws.root)),
                "bytes_written": len(content.encode(encoding))}


class PatchFileTool(Tool):
    name = "patch_file"
    description = "Replace an exact string in a file with new content. Returns replacement count."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_content": {"type": "string", "description": "Exact string to find and replace."},
            "new_content": {"type": "string", "description": "Replacement string."},
        },
        "required": ["path", "old_content", "new_content"],
    }

    def __init__(self, workspace: WorkspaceView) -> None:
        self._ws = workspace

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        resolved = self._ws.resolve(args["path"])
        original = await asyncio.to_thread(resolved.read_text)
        old, new = args["old_content"], args["new_content"]
        if old not in original:
            return {"ok": False, "replacements": 0,
                    "error": "old_content not found in file"}
        updated = original.replace(old, new)
        count = original.count(old)
        await asyncio.to_thread(resolved.write_text, updated)
        return {"ok": True, "replacements": count}


class GrepFilesTool(Tool):
    name = "grep_files"
    description = "Search file contents for a regex pattern. Returns matching lines with context."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for."},
            "path": {"type": "string", "default": "."},
            "recursive": {"type": "boolean", "default": True},
            "max_results": {"type": "integer", "default": 100},
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: WorkspaceView) -> None:
        self._ws = workspace

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        root = self._ws.resolve(args.get("path", "."))
        pattern = re.compile(args["pattern"])
        max_r = int(args.get("max_results", 100))
        recursive = bool(args.get("recursive", True))
        matches: list[dict] = []

        def _scan():
            glob = "**/*" if recursive else "*"
            for p in root.glob(glob):
                if not p.is_file():
                    continue
                try:
                    for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                        if pattern.search(line):
                            matches.append({
                                "file": str(p.relative_to(self._ws.root)),
                                "line_number": i,
                                "line": line,
                            })
                            if len(matches) >= max_r:
                                return
                except Exception:
                    pass

        await asyncio.to_thread(_scan)
        return {"matches": matches, "count": len(matches)}


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List directory contents, optionally with glob pattern and recursion."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "pattern": {"type": "string", "default": "*"},
            "recursive": {"type": "boolean", "default": False},
            "include_hidden": {"type": "boolean", "default": False},
        },
    }

    def __init__(self, workspace: WorkspaceView) -> None:
        self._ws = workspace

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        root = self._ws.resolve(args.get("path", "."))
        pattern = args.get("pattern", "*")
        recursive = bool(args.get("recursive", False))
        include_hidden = bool(args.get("include_hidden", False))
        glob_pattern = f"**/{pattern}" if recursive else pattern

        def _scan():
            entries = []
            for p in sorted(root.glob(glob_pattern)):
                if not include_hidden and p.name.startswith("."):
                    continue
                st = p.stat()
                entries.append({
                    "name": p.name,
                    "path": str(p.relative_to(self._ws.root)),
                    "type": "dir" if p.is_dir() else "file",
                    "size_bytes": st.st_size,
                    "modified_at": st.st_mtime,
                })
            return entries

        entries = await asyncio.to_thread(_scan)
        return {"entries": entries, "count": len(entries)}


class ReadLinesTool(Tool):
    name = "read_lines"
    description = "Read a range of lines from a file (1-indexed, inclusive)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start": {"type": "integer", "default": 1},
            "end": {"type": "integer", "description": "Inclusive end line. Omit for end of file."},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: WorkspaceView) -> None:
        self._ws = workspace

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        resolved = self._ws.resolve(args["path"])
        all_lines = await asyncio.to_thread(resolved.read_text)
        lines = all_lines.splitlines()
        total = len(lines)
        start = max(1, int(args.get("start", 1)))
        end = min(total, int(args.get("end", total)))
        selected = lines[start - 1:end]
        return {"lines": selected, "total_lines": total, "start": start, "end": end}


class FsToolKit:
    """Factory: instantiates all 14 filesystem tools for a given workspace root."""

    def __init__(self, workspace_root: str = ".", max_file_size_mb: int = 10) -> None:
        self._ws = WorkspaceView(workspace_root)
        self._max_size = max_file_size_mb * 1024 * 1024

    def all_tools(self) -> list[Tool]:
        ws = self._ws
        return [
            ReadFileTool(ws, self._max_size),
            WriteFileTool(ws),
            PatchFileTool(ws),
            GrepFilesTool(ws),
            ListDirectoryTool(ws),
            ReadLinesTool(ws),
            # Simple one-liners below
            _AppendFileTool(ws),
            _DeleteFileTool(ws),
            _MoveFileTool(ws),
            _CopyFileTool(ws),
            _MakeDirectoryTool(ws),
            _FileExistsTool(ws),
            _SearchFilesTool(ws),
            _GetFileInfoTool(ws),
        ]


# ── simple tool stubs (full implementation follows same pattern) ──────────────

class _AppendFileTool(Tool):
    name = "append_file"; description = "Append text to a file."
    parameters = {"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        p = self._ws.resolve(args["path"])
        await asyncio.to_thread(lambda: p.open("a").write(args["content"]))
        return {"ok": True, "path": str(p.relative_to(self._ws.root))}

class _DeleteFileTool(Tool):
    name = "delete_file"; description = "Delete a file."
    parameters = {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        p = self._ws.resolve(args["path"])
        await asyncio.to_thread(p.unlink)
        return {"ok": True, "path": args["path"]}

class _MoveFileTool(Tool):
    name = "move_file"; description = "Move or rename a file."
    parameters = {"type":"object","properties":{"source":{"type":"string"},"destination":{"type":"string"}},"required":["source","destination"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        src = self._ws.resolve(args["source"]); dst = self._ws.resolve(args["destination"])
        await asyncio.to_thread(shutil.move, str(src), str(dst))
        return {"ok": True, "source": args["source"], "destination": args["destination"]}

class _CopyFileTool(Tool):
    name = "copy_file"; description = "Copy a file."
    parameters = {"type":"object","properties":{"source":{"type":"string"},"destination":{"type":"string"}},"required":["source","destination"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        src = self._ws.resolve(args["source"]); dst = self._ws.resolve(args["destination"])
        await asyncio.to_thread(shutil.copy2, str(src), str(dst))
        return {"ok": True, "source": args["source"], "destination": args["destination"]}

class _MakeDirectoryTool(Tool):
    name = "make_directory"; description = "Create a directory (and parents)."
    parameters = {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        p = self._ws.resolve(args["path"])
        await asyncio.to_thread(p.mkdir, parents=True, exist_ok=True)
        return {"ok": True, "path": args["path"]}

class _FileExistsTool(Tool):
    name = "file_exists"; description = "Check whether a path exists."
    parameters = {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        try:
            p = self._ws.resolve(args["path"])
            t = "dir" if p.is_dir() else "file" if p.is_file() else "other"
            return {"exists": p.exists(), "path": args["path"], "type": t if p.exists() else None}
        except PermissionError:
            return {"exists": False, "path": args["path"], "type": None}

class _SearchFilesTool(Tool):
    name = "search_files"; description = "Find files by glob pattern."
    parameters = {"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string","default":"."},"recursive":{"type":"boolean","default":True}},"required":["pattern"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        root = self._ws.resolve(args.get("path", "."))
        g = f"**/{args['pattern']}" if args.get("recursive", True) else args["pattern"]
        matches = [str(p.relative_to(self._ws.root)) for p in root.glob(g) if p.is_file()]
        return {"matches": matches, "count": len(matches)}

class _GetFileInfoTool(Tool):
    name = "get_file_info"; description = "Get metadata about a file."
    parameters = {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
    def __init__(self, ws): self._ws = ws
    async def execute(self, args, ctx):
        p = self._ws.resolve(args["path"]); st = p.stat()
        return {"path": args["path"], "size_bytes": st.st_size,
                "modified_at": st.st_mtime, "created_at": st.st_ctime,
                "type": "dir" if p.is_dir() else "file",
                "permissions": oct(stat.S_IMODE(st.st_mode))}
```

## Configuration

```toml
[tools.fs]
workspace_root = "."
max_file_size_mb = 10
allow_delete = true
allow_write = true
```

## Tests

```python
# tests/unit/test_fs_tools.py
"""Unit tests for filesystem tools (PRD-14)."""
from __future__ import annotations
import pytest
from agenthicc.tools.fs.toolkit import (
    FsToolKit, ReadFileTool, WriteFileTool, PatchFileTool,
    GrepFilesTool, ListDirectoryTool, ReadLinesTool,
)
from agenthicc.tools.sandbox import WorkspaceView

pytestmark = pytest.mark.unit


@pytest.fixture
def kit(tmp_path):
    return FsToolKit(workspace_root=str(tmp_path))


@pytest.fixture
def ws(tmp_path):
    return WorkspaceView(tmp_path)


class TestReadFile:
    async def test_reads_text_file(self, tmp_path, ws):
        (tmp_path / "hello.txt").write_text("hello world")
        t = ReadFileTool(ws)
        r = await t.execute({"path": "hello.txt"}, {})
        assert r["content"] == "hello world"
        assert r["size_bytes"] == 11

    async def test_traversal_blocked(self, tmp_path, ws):
        with pytest.raises(PermissionError):
            t = ReadFileTool(ws)
            await t.execute({"path": "../../etc/passwd"}, {})

    async def test_missing_file_raises(self, tmp_path, ws):
        t = ReadFileTool(ws)
        with pytest.raises(FileNotFoundError):
            await t.execute({"path": "nonexistent.txt"}, {})


class TestWriteFile:
    async def test_writes_creates_file(self, tmp_path, ws):
        t = WriteFileTool(ws)
        r = await t.execute({"path": "new.txt", "content": "hello"}, {})
        assert r["ok"] is True
        assert (tmp_path / "new.txt").read_text() == "hello"

    async def test_creates_parents(self, tmp_path, ws):
        t = WriteFileTool(ws)
        await t.execute({"path": "a/b/c.txt", "content": "deep"}, {})
        assert (tmp_path / "a" / "b" / "c.txt").exists()


class TestPatchFile:
    async def test_replaces_exact_match(self, tmp_path, ws):
        (tmp_path / "code.py").write_text("import bcrypt\nHASH = bcrypt.hash(pw)")
        t = PatchFileTool(ws)
        r = await t.execute({
            "path": "code.py",
            "old_content": "import bcrypt\nHASH = bcrypt.hash(pw)",
            "new_content": "import argon2\nHASH = argon2.hash(pw)",
        }, {})
        assert r["ok"] is True
        assert r["replacements"] == 1
        assert "argon2" in (tmp_path / "code.py").read_text()

    async def test_not_found_returns_error(self, tmp_path, ws):
        (tmp_path / "f.py").write_text("original")
        t = PatchFileTool(ws)
        r = await t.execute({"path": "f.py", "old_content": "NOTHERE", "new_content": "x"}, {})
        assert r["ok"] is False
        assert r["replacements"] == 0


class TestGrepFiles:
    async def test_finds_pattern(self, tmp_path, ws):
        (tmp_path / "a.py").write_text("def foo():\n    pass\ndef bar():\n    pass")
        t = GrepFilesTool(ws)
        r = await t.execute({"pattern": "def foo", "path": "."}, {})
        assert r["count"] >= 1
        assert any("foo" in m["line"] for m in r["matches"])

    async def test_no_match_returns_empty(self, tmp_path, ws):
        (tmp_path / "b.py").write_text("nothing here")
        t = GrepFilesTool(ws)
        r = await t.execute({"pattern": "ZZZNOMATCH"}, {})
        assert r["count"] == 0


class TestReadLines:
    async def test_reads_line_range(self, tmp_path, ws):
        (tmp_path / "f.txt").write_text("line1\nline2\nline3\nline4\nline5")
        t = ReadLinesTool(ws)
        r = await t.execute({"path": "f.txt", "start": 2, "end": 4}, {})
        assert r["lines"] == ["line2", "line3", "line4"]
        assert r["total_lines"] == 5

    async def test_full_file_when_no_range(self, tmp_path, ws):
        (tmp_path / "f.txt").write_text("a\nb\nc")
        t = ReadLinesTool(ws)
        r = await t.execute({"path": "f.txt"}, {})
        assert len(r["lines"]) == 3


class TestFsToolKit:
    def test_all_tools_returns_14(self, tmp_path):
        kit = FsToolKit(workspace_root=str(tmp_path))
        tools = kit.all_tools()
        assert len(tools) == 14
        names = {t.name for t in tools}
        assert "read_file" in names
        assert "patch_file" in names
        assert "grep_files" in names
```

## Open Questions

1. **Binary file support**: `read_file` currently reads as text. Add `mode: "binary"` → return base64-encoded content.
2. **Atomic writes**: use temp file + rename for `write_file` to prevent partial writes on crash.
3. **File watching**: a `watch_file(path)` tool that emits `FileChanged` events when a file is modified would enable reactive agents. PRD-14b.
