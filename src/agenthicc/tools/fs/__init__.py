"""Filesystem tools: read, write, delete, search, grep, patch, etc. (PRD-14)."""
from __future__ import annotations

import asyncio
import datetime
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from agenthicc.tools.base import Tool
from agenthicc.tools.sandbox import WorkspaceView

__all__ = ["FsToolKit"]

_MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB

# PRD-133 Layer A: bound tool output so a single result can't overflow the model
# context window.  These are upstream guards; the pre-send budget guard (Layer C)
# is the hard backstop.
_MAX_LIST_ENTRIES = 1000                # max entries from list_directory/search_files
_MAX_TOOL_OUTPUT_CHARS = 100_000        # ~25k tokens cap for read_file/read_lines


def _git_keep_filter(root: Path) -> "Callable[[str], bool] | None":
    """Return a predicate keeping only git-relevant paths, or ``None``.

    Uses ``git ls-files --cached --others --exclude-standard`` so the project's
    own ``.gitignore`` defines what is relevant (tracked + untracked-not-ignored)
    — far more complete and correct than a hardcoded blocklist.  Returns ``None``
    when *root* is not inside a git repo (or git is unavailable), so the caller
    falls back to reading everything (capped + backstopped by Layer C).
    """
    import subprocess  # noqa: PLC0415

    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    files = {line for line in proc.stdout.splitlines() if line}
    # A directory is relevant if it is a prefix of any relevant file.
    relevant: set[str] = set(files)
    for f in files:
        parts = f.split("/")
        for i in range(1, len(parts)):
            relevant.add("/".join(parts[:i]))
    return lambda relpath: relpath in relevant


def _truncate_output(text: str, limit: int = _MAX_TOOL_OUTPUT_CHARS) -> tuple[str, bool]:
    """Cap *text* to ≤ *limit* chars (head+tail with a marker).  Returns (text, truncated)."""
    if len(text) <= limit:
        return text, False
    marker = (
        f"\n…[truncated {len(text) - limit} of {len(text)} chars — "
        "read a line range with read_lines for the rest]…\n"
    )
    keep = max(0, limit - len(marker))
    head = (keep * 2) // 3
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else ""), True


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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        encoding = args.get("encoding", "utf-8")
        try:
            resolved = _view(context).resolve(args["path"])
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {args['path']}"}
        if resolved.stat().st_size > _MAX_FILE_SIZE:
            return {"ok": False, "error": f"file_too_large: {resolved.stat().st_size} bytes"}
        # PRD-132 L1: serve from the durable file cache when the file is
        # unchanged (mtime/size/encoding match); otherwise read and record.
        from agenthicc.tools.fs.file_cache import get_file_cache  # noqa: PLC0415
        _fc = get_file_cache()
        _abspath = str(resolved)
        if _fc is not None:
            _hit = _fc.get_fresh(_abspath, encoding=encoding)
            if _hit is not None:
                # PRD-133 Layer A: cap returned content (cache keeps the full bytes).
                _out, _trunc = _truncate_output(_hit)
                _res: dict[str, object] = {
                    "content": _out, "size_bytes": resolved.stat().st_size,
                    "encoding": encoding, "cached": True,
                }
                if _trunc:
                    _res["truncated"] = True
                return _res
        try:
            content = await asyncio.to_thread(resolved.read_text, encoding=encoding, errors="replace")
            if _fc is not None:
                _fc.store(_abspath, content, encoding=encoding)  # store full content
            # PRD-133 Layer A: cap returned content to bound context tokens.
            _out, _trunc = _truncate_output(content)
            _res = {"content": _out, "size_bytes": resolved.stat().st_size, "encoding": encoding}
            if _trunc:
                _res["truncated"] = True
            return _res
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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
            truncated = False
            # Recursive listings respect .gitignore via git ls-files; a flat
            # (non-recursive) listing shows the directory's literal contents.
            keep = _git_keep_filter(resolved) if recursive else None
            glob_fn = resolved.rglob if recursive else resolved.glob
            for p in glob_fn(pattern):
                if not include_hidden and p.name.startswith("."):
                    continue
                rel = p.relative_to(resolved)
                if keep is not None and not keep(str(rel)):
                    continue
                if len(entries) >= _MAX_LIST_ENTRIES:
                    truncated = True
                    break
                try:
                    s = p.stat()
                    entries.append({
                        "name": p.name,
                        "path": str(rel),
                        "type": "dir" if p.is_dir() else "file",
                        "size_bytes": s.st_size,
                        "modified_at": datetime.datetime.fromtimestamp(s.st_mtime).isoformat(),
                    })
                except OSError:
                    pass
            entries.sort(key=lambda e: e["path"])
            return entries, truncated

        entries, truncated = await asyncio.to_thread(_list)
        result: dict[str, object] = {"entries": entries, "count": len(entries)}
        if truncated:
            result["truncated"] = True
            result["note"] = f"results capped at {_MAX_LIST_ENTRIES}; narrow the pattern or path"
        return result


class MakeDirectoryTool(Tool):
    name = "make_directory"
    description = "Create a directory (and parents) in the workspace."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        try:
            resolved = _view(context).resolve(args.get("path", "."))
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        pattern = args["pattern"]
        recursive = args.get("recursive", True)

        def _search():
            matches = []
            truncated = False
            keep = _git_keep_filter(resolved)  # respect .gitignore; None → full walk
            glob_fn = resolved.rglob if recursive else resolved.glob
            for p in glob_fn(pattern):
                if not p.is_file():
                    continue
                rel = str(p.relative_to(resolved))
                if keep is not None and not keep(rel):
                    continue
                if len(matches) >= _MAX_LIST_ENTRIES:
                    truncated = True
                    break
                matches.append(rel)
            matches.sort()
            return matches, truncated

        matches, truncated = await asyncio.to_thread(_search)
        result: dict[str, object] = {"matches": matches, "count": len(matches)}
        if truncated:
            result["truncated"] = True
            result["note"] = f"results capped at {_MAX_LIST_ENTRIES}; narrow the pattern"
        return result


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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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
            keep = _git_keep_filter(resolved)  # respect .gitignore; None → full walk
            glob_fn = resolved.rglob if recursive else resolved.glob
            for p in sorted(glob_fn("*")):
                if not p.is_file():
                    continue
                if keep is not None and not keep(str(p.relative_to(resolved))):
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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
        # PRD-133 Layer A: cap output to bound context tokens.
        out_lines: list[str] = []
        used = 0
        truncated = False
        for ln in selected:
            if used + len(ln) + 1 > _MAX_TOOL_OUTPUT_CHARS:
                truncated = True
                break
            out_lines.append(ln)
            used += len(ln) + 1
        result: dict[str, object] = {
            "lines": out_lines,
            "total_lines": total,
            "start": start,
            "end": start - 1 + len(out_lines),
        }
        if truncated:
            result["truncated"] = True
            result["note"] = (
                f"output capped; {len(selected) - len(out_lines)} more lines in range "
                "— request a smaller line range"
            )
        return result


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

    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    def __init__(self, backend: object = None) -> None:
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
