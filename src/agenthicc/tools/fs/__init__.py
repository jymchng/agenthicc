"""Filesystem tools: read, write, delete, search, grep, patch, etc. (PRD-14)."""
from __future__ import annotations

import asyncio
import datetime
import re
import shutil
import stat
from pathlib import Path
from typing import Any

from agenthicc.tools.base import Tool
from agenthicc.tools.sandbox import WorkspaceView

__all__ = ["FsToolKit"]

_MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB


def _view(context: dict) -> WorkspaceView:
    return WorkspaceView(context.get("workspace_root", "."))


def _safe_stat(path: Path) -> dict:
    s = path.stat()
    return {
        "size_bytes": s.st_size,
        "modified_at": datetime.datetime.fromtimestamp(s.st_mtime).isoformat(),
        "created_at": datetime.datetime.fromtimestamp(s.st_ctime).isoformat(),
        "type": "dir" if path.is_dir() else "file",
        "permissions": oct(stat.S_IMODE(s.st_mode))[2:],
    }


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the full content of a file within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        encoding = args.get("encoding", "utf-8")
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {args['path']}"}
        if resolved.stat().st_size > _MAX_FILE_SIZE:
            return {"ok": False, "error": f"file_too_large: {resolved.stat().st_size} bytes"}
        try:
            content = await asyncio.to_thread(resolved.read_text, encoding=encoding, errors="replace")
            return {"content": content, "size_bytes": resolved.stat().st_size, "encoding": encoding}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write content to a file, creating parent directories if needed."
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

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        content = args["content"]
        encoding = args.get("encoding", "utf-8")
        try:
            if args.get("create_parents", True):
                await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(resolved.write_text, content, encoding=encoding)
            return {"ok": True, "path": str(resolved), "bytes_written": len(content.encode(encoding))}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class AppendFileTool(Tool):
    name = "append_file"
    description = "Append content to an existing file."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        try:
            def _append():
                with open(resolved, "a", encoding="utf-8") as f:
                    f.write(args["content"])
            await asyncio.to_thread(_append)
            return {"ok": True, "path": str(resolved)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a file from the workspace."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {args['path']}"}
        try:
            await asyncio.to_thread(resolved.unlink)
            return {"ok": True, "path": str(resolved)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class MoveFileTool(Tool):
    name = "move_file"
    description = "Move or rename a file within the workspace."
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["source", "destination"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            view = _view(context)
            src = view.resolve(args["source"])
            dst = view.resolve(args["destination"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        try:
            await asyncio.to_thread(shutil.move, str(src), str(dst))
            return {"ok": True, "source": str(src), "destination": str(dst)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class CopyFileTool(Tool):
    name = "copy_file"
    description = "Copy a file within the workspace."
    parameters = {
        "type": "object",
        "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["source", "destination"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            view = _view(context)
            src = view.resolve(args["source"])
            dst = view.resolve(args["destination"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        try:
            await asyncio.to_thread(shutil.copy2, str(src), str(dst))
            return {"ok": True, "source": str(src), "destination": str(dst)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List files and directories in the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "pattern": {"type": "string", "default": "*"},
            "recursive": {"type": "boolean", "default": False},
            "include_hidden": {"type": "boolean", "default": False},
        },
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args.get("path", "."))
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.is_dir():
            return {"ok": False, "error": f"not_a_directory: {args.get('path', '.')}"}
        pattern = args.get("pattern", "*")
        recursive = args.get("recursive", False)
        include_hidden = args.get("include_hidden", False)

        def _list():
            entries = []
            glob_fn = resolved.rglob if recursive else resolved.glob
            for p in sorted(glob_fn(pattern)):
                if not include_hidden and p.name.startswith("."):
                    continue
                try:
                    s = p.stat()
                    entries.append({
                        "name": p.name,
                        "path": str(p.relative_to(resolved)),
                        "type": "dir" if p.is_dir() else "file",
                        "size_bytes": s.st_size,
                        "modified_at": datetime.datetime.fromtimestamp(s.st_mtime).isoformat(),
                    })
                except OSError:
                    pass
            return entries

        entries = await asyncio.to_thread(_list)
        return {"entries": entries, "count": len(entries)}


class MakeDirectoryTool(Tool):
    name = "make_directory"
    description = "Create a directory (and parents) in the workspace."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        try:
            await asyncio.to_thread(resolved.mkdir, parents=True, exist_ok=True)
            return {"ok": True, "path": str(resolved)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class FileExistsTool(Tool):
    name = "file_exists"
    description = "Check whether a file or directory exists in the workspace."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError:
            return {"exists": False, "path": args["path"], "type": None}
        exists = resolved.exists()
        file_type = None
        if exists:
            file_type = "dir" if resolved.is_dir() else "file"
        return {"exists": exists, "path": args["path"], "type": file_type}


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Find files matching a glob pattern within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "recursive": {"type": "boolean", "default": True},
        },
        "required": ["pattern"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args.get("path", "."))
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        pattern = args["pattern"]
        recursive = args.get("recursive", True)

        def _search():
            glob_fn = resolved.rglob if recursive else resolved.glob
            return [str(p.relative_to(resolved)) for p in sorted(glob_fn(pattern)) if p.is_file()]

        matches = await asyncio.to_thread(_search)
        return {"matches": matches, "count": len(matches)}


class GrepFilesTool(Tool):
    name = "grep_files"
    description = "Search for a regex pattern in file contents."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "recursive": {"type": "boolean", "default": True},
            "max_results": {"type": "integer", "default": 100},
        },
        "required": ["pattern"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args.get("path", "."))
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        pattern = args["pattern"]
        max_results = int(args.get("max_results", 100))
        recursive = args.get("recursive", True)

        def _grep():
            compiled = re.compile(pattern)
            matches = []
            glob_fn = resolved.rglob if recursive else resolved.glob
            for p in sorted(glob_fn("*")):
                if not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, OSError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        matches.append({
                            "file": str(p.relative_to(resolved)),
                            "line_number": i,
                            "line": line.rstrip(),
                        })
                        if len(matches) >= max_results:
                            return matches
            return matches

        matches = await asyncio.to_thread(_grep)
        return {"matches": matches, "count": len(matches)}


class GetFileInfoTool(Tool):
    name = "get_file_info"
    description = "Return metadata for a file or directory."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {args['path']}"}
        info = await asyncio.to_thread(_safe_stat, resolved)
        info["path"] = str(resolved)
        return info


class ReadLinesTool(Tool):
    name = "read_lines"
    description = "Read a specific range of lines from a file (1-indexed)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start": {"type": "integer", "default": 1},
            "end": {"type": "integer"},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {args['path']}"}
        try:
            all_lines = await asyncio.to_thread(resolved.read_text, encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        lines = all_lines.splitlines()
        total = len(lines)
        start = max(1, int(args.get("start", 1)))
        end = min(total, int(args["end"])) if args.get("end") else total
        selected = lines[start - 1:end]
        return {"lines": selected, "total_lines": total, "start": start, "end": end}


class PatchFileTool(Tool):
    name = "patch_file"
    description = "Replace all occurrences of old_content with new_content in a file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_content": {"type": "string"},
            "new_content": {"type": "string"},
        },
        "required": ["path", "old_content", "new_content"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {args['path']}"}
        try:
            original = await asyncio.to_thread(resolved.read_text, encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        old = args["old_content"]
        if old not in original:
            return {"ok": False, "error": f"old_content not found in {args['path']}", "replacements": 0}
        patched = original.replace(old, args["new_content"])
        replacements = original.count(old)
        await asyncio.to_thread(resolved.write_text, patched, encoding="utf-8")
        return {"ok": True, "replacements": replacements}


class FsToolKit:
    """Factory that returns all 14 filesystem tools."""

    def __init__(self, backend: Any = None) -> None:
        self._backend = backend

    def tools(self, workspace_root: str = ".") -> list[Tool]:
        return [
            ReadFileTool(), WriteFileTool(), AppendFileTool(), DeleteFileTool(),
            MoveFileTool(), CopyFileTool(), ListDirectoryTool(), MakeDirectoryTool(),
            FileExistsTool(), SearchFilesTool(), GrepFilesTool(), GetFileInfoTool(),
            ReadLinesTool(), PatchFileTool(),
        ]

    def all_agent_tools(self) -> list:
        """Return all 24 @tool()-decorated agent tools (14 original + 10 new)."""
        from agenthicc.tools.fs.agent_tools import FS_AGENT_TOOLS  # noqa: PLC0415
        return FS_AGENT_TOOLS
