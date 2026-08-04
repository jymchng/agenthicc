"""Filesystem tools: read, write, delete, search, grep, patch, etc. (PRD-14)."""

from __future__ import annotations

import asyncio
import datetime
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from agenthicc.tools.base import Tool, arg_bool, arg_int, arg_str
from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, current_workspace_access

__all__ = ["FsToolKit"]

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# PRD-133 Layer A: bound tool output so a single result can't overflow the model
# context window.  These are upstream guards; the pre-send budget guard (Layer C)
# is the hard backstop.
_MAX_LIST_ENTRIES = 1000  # max entries from list_directory/search_files
_MAX_TOOL_OUTPUT_CHARS = 100_000  # ~25k tokens cap for read_file/read_lines


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
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
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


def _view(context: Mapping[str, object]) -> WorkspaceView:
    return WorkspaceView(arg_str(context, "workspace_root", "."))


def _policy(context: Mapping[str, object]) -> WorkspaceAccessPolicy | None:
    value = context.get("workspace_access")
    if isinstance(value, WorkspaceAccessPolicy):
        return value
    return current_workspace_access()


async def _authorize_glob_pattern(
    context: Mapping[str, object],
    path: str,
    pattern: str,
    *,
    operation: str,
    tool_name: str,
) -> None:
    """Authorize a glob whose pattern itself contains an escape prefix."""

    policy = _policy(context)
    if policy is None:
        return
    from agenthicc.tools.workspace_access import (  # noqa: PLC0415
        _combine_pattern_path,
        _pattern_can_escape,
    )

    if not _pattern_can_escape(pattern):
        return
    combined = _combine_pattern_path(path, pattern)
    await policy.authorize(
        combined,
        operation=operation,
        tool_name=tool_name,
        pattern=True,
        tool_input={"path": path, "pattern": pattern},
    )


async def _resolve_path(
    context: Mapping[str, object],
    path: str,
    *,
    operation: str,
    tool_name: str,
    capabilities: frozenset[str] = frozenset(),
) -> Path:
    """Authorize and revalidate one path immediately before filesystem I/O."""

    policy = _policy(context)
    if policy is not None:
        return await policy.authorize(
            path,
            operation=operation,
            tool_name=tool_name,
            capabilities=capabilities,
        )
    return _view(context).resolve(path)


async def _authorized_path(
    context: Mapping[str, object],
    path: str,
    *,
    operation: str,
    tool_name: str,
    capabilities: frozenset[str] = frozenset(),
    tool_input: Mapping[str, object] | None = None,
) -> Path:
    """Return the exact canonical target that the adapter must use."""

    policy = _policy(context)
    if policy is not None:
        return await policy.authorize(
            path,
            operation=operation,
            tool_name=tool_name,
            capabilities=capabilities,
            tool_input=tool_input,
        )
    return _view(context).resolve(path)


async def _authorize_discovered_paths(
    context: Mapping[str, object],
    candidates: list[Path],
    *,
    operation: str,
    tool_name: str,
) -> list[tuple[Path, Path]]:
    """Authorize each discovered path before metadata or content access.

    Directory/search tools first authorize their root, then discover names.
    A symlink or nested path can still resolve outside that root, so every
    candidate gets its own canonical decision before the adapter calls
    ``stat()``, ``is_file()``, or ``read_text()`` on it.  Denied candidates are
    omitted from discovery results rather than leaking a decoy or causing a
    batch search to fail open.
    """

    policy = _policy(context)
    if policy is None:
        return [(candidate, candidate) for candidate in candidates]
    authorized: list[tuple[Path, Path]] = []
    for candidate in candidates:
        try:
            canonical = await policy.authorize(
                str(candidate),
                operation=operation,
                tool_name=tool_name,
                tool_input={"path": str(candidate)},
            )
        except (OSError, PermissionError, ValueError):
            continue
        authorized.append((candidate, canonical))
    return authorized


async def _authorize_tool(
    context: Mapping[str, object],
    tool_name: str,
    tool_input: Mapping[str, object],
    capabilities: frozenset[str] = frozenset(),
    *,
    bind_current: bool = True,
    finalize: bool = True,
) -> list[Path]:
    policy = _policy(context)
    if policy is None:
        return []
    result = await policy.authorize_tool(
        tool_name,
        tool_input,
        capabilities,
        bind_current=bind_current,
    )
    if not result.allowed:
        raise PermissionError(f"{result.code}: {result.error}")
    authorized: list[Path] = []
    if finalize:
        for requested, operation in policy.requests_for_tool(tool_name, tool_input):
            authorized.append(
                await policy.authorize(
                    requested,
                    operation=operation,
                    tool_name=tool_name,
                    capabilities=capabilities,
                    tool_input=tool_input,
                )
            )
    elif not bind_current:
        policy.clear_current_grant(tool_name)
    return authorized


def _safe_stat(path: Path) -> dict[str, object]:
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        encoding = arg_str(args, "encoding", "utf-8")
        try:
            resolved = await _resolve_path(context, path, operation="read", tool_name=self.name)
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {path}"}
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
                    "content": _out,
                    "size_bytes": resolved.stat().st_size,
                    "encoding": encoding,
                    "cached": True,
                }
                if _trunc:
                    _res["truncated"] = True
                return _res
        try:
            content = await asyncio.to_thread(
                resolved.read_text, encoding=encoding, errors="replace"
            )
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        content = arg_str(args, "content")
        encoding = arg_str(args, "encoding", "utf-8")
        create_parents = arg_bool(args, "create_parents", True)
        try:
            resolved = await _resolve_path(
                context,
                path,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        try:
            if create_parents:
                await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(resolved.write_text, content, encoding=encoding)
            return {
                "ok": True,
                "path": str(resolved),
                "bytes_written": len(content.encode(encoding)),
            }
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        content = arg_str(args, "content")
        try:
            resolved = await _resolve_path(
                context,
                path,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        try:

            def _append() -> None:
                with open(resolved, "a", encoding="utf-8") as f:
                    f.write(content)

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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        try:
            resolved = await _resolve_path(
                context,
                path,
                operation="delete",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {path}"}
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        source = arg_str(args, "source")
        destination = arg_str(args, "destination")
        try:
            await _authorize_tool(
                context,
                self.name,
                {"source": source, "destination": destination},
                frozenset({"write"}),
                bind_current=True,
                finalize=False,
            )
            src = await _resolve_path(
                context,
                source,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
            dst = await _resolve_path(
                context,
                destination,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        source = arg_str(args, "source")
        destination = arg_str(args, "destination")
        try:
            await _authorize_tool(
                context,
                self.name,
                {"source": source, "destination": destination},
                frozenset({"write"}),
                bind_current=True,
                finalize=False,
            )
            src = await _resolve_path(
                context,
                source,
                operation="read",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
            dst = await _resolve_path(
                context,
                destination,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path", ".")
        pattern = arg_str(args, "pattern", "*")
        recursive = arg_bool(args, "recursive", False)
        include_hidden = arg_bool(args, "include_hidden", False)
        try:
            await _authorize_glob_pattern(
                context,
                path,
                pattern,
                operation="list",
                tool_name=self.name,
            )
            resolved = await _resolve_path(context, path, operation="list", tool_name=self.name)
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.is_dir():
            return {"ok": False, "error": f"not_a_directory: {path}"}

        candidates = await asyncio.to_thread(
            lambda: list((resolved.rglob if recursive else resolved.glob)(pattern))
        )
        authorized = await _authorize_discovered_paths(
            context,
            candidates,
            operation="list",
            tool_name=self.name,
        )

        def _list() -> tuple[list[dict[str, object]], bool]:
            entries: list[dict[str, object]] = []
            truncated = False
            # Recursive listings respect .gitignore via git ls-files; a flat
            # (non-recursive) listing shows the directory's literal contents.
            keep = _git_keep_filter(resolved) if recursive else None
            for original, canonical in authorized:
                if not include_hidden and original.name.startswith("."):
                    continue
                rel = original.relative_to(resolved)
                if keep is not None and not keep(str(rel)):
                    continue
                if len(entries) >= _MAX_LIST_ENTRIES:
                    truncated = True
                    break
                try:
                    s = canonical.stat()
                    entries.append(
                        {
                            "name": original.name,
                            "path": str(rel),
                            "type": "dir" if canonical.is_dir() else "file",
                            "size_bytes": s.st_size,
                            "modified_at": datetime.datetime.fromtimestamp(s.st_mtime).isoformat(),
                        }
                    )
                except OSError:
                    pass
            entries.sort(key=lambda e: str(e["path"]))
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        try:
            resolved = await _resolve_path(
                context,
                path,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        try:
            resolved = await _resolve_path(context, path, operation="metadata", tool_name=self.name)
        except PermissionError:
            return {"exists": False, "path": path, "type": None}
        exists = resolved.exists()
        file_type = None
        if exists:
            file_type = "dir" if resolved.is_dir() else "file"
        return {"exists": exists, "path": path, "type": file_type}


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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path", ".")
        pattern = arg_str(args, "pattern")
        recursive = arg_bool(args, "recursive", True)
        try:
            await _authorize_glob_pattern(
                context,
                path,
                pattern,
                operation="search",
                tool_name=self.name,
            )
            resolved = await _resolve_path(context, path, operation="search", tool_name=self.name)
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}

        candidates = await asyncio.to_thread(
            lambda: list((resolved.rglob if recursive else resolved.glob)(pattern))
        )
        authorized = await _authorize_discovered_paths(
            context,
            candidates,
            operation="search",
            tool_name=self.name,
        )

        def _search() -> tuple[list[str], bool]:
            matches: list[str] = []
            truncated = False
            keep = _git_keep_filter(resolved)  # respect .gitignore; None → full walk
            for original, canonical in authorized:
                if not canonical.is_file():
                    continue
                rel = str(original.relative_to(resolved))
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path", ".")
        pattern = arg_str(args, "pattern")
        max_results = arg_int(args, "max_results", 100)
        recursive = arg_bool(args, "recursive", True)
        try:
            resolved = await _resolve_path(context, path, operation="search", tool_name=self.name)
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}

        candidates = await asyncio.to_thread(
            lambda: list((resolved.rglob if recursive else resolved.glob)("*"))
        )
        authorized = await _authorize_discovered_paths(
            context,
            candidates,
            operation="search",
            tool_name=self.name,
        )

        def _grep() -> list[dict[str, object]]:
            compiled = re.compile(pattern)
            matches: list[dict[str, object]] = []
            keep = _git_keep_filter(resolved)  # respect .gitignore; None → full walk
            for original, canonical in sorted(authorized, key=lambda item: str(item[0])):
                if not canonical.is_file():
                    continue
                rel = str(original.relative_to(resolved))
                if keep is not None and not keep(rel):
                    continue
                try:
                    text = canonical.read_text(encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, OSError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        matches.append(
                            {
                                "file": rel,
                                "line_number": i,
                                "line": line.rstrip(),
                            }
                        )
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        try:
            resolved = await _resolve_path(context, path, operation="metadata", tool_name=self.name)
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {path}"}
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        start = max(1, arg_int(args, "start", 1))
        end_value = args.get("end")
        end = arg_int(args, "end") if end_value is not None else None
        try:
            resolved = await _resolve_path(context, path, operation="read", tool_name=self.name)
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {path}"}
        try:
            all_lines = await asyncio.to_thread(
                resolved.read_text, encoding="utf-8", errors="replace"
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        lines = all_lines.splitlines()
        total = len(lines)
        end = min(total, end) if end is not None else total
        selected = lines[start - 1 : end]
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        old = arg_str(args, "old_content")
        new = arg_str(args, "new_content")
        try:
            resolved = await _resolve_path(
                context,
                path,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
        except PermissionError as e:
            return {"ok": False, "error": f"permission_denied: {e}"}
        if not resolved.exists():
            return {"ok": False, "error": f"not_found: {path}"}
        try:
            original = await asyncio.to_thread(
                resolved.read_text, encoding="utf-8", errors="replace"
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if old not in original:
            return {
                "ok": False,
                "error": f"old_content not found in {path}",
                "replacements": 0,
            }
        patched = original.replace(old, new)
        replacements = original.count(old)
        await asyncio.to_thread(resolved.write_text, patched, encoding="utf-8")
        return {"ok": True, "replacements": replacements}


class FsToolKit:
    """Factory that returns all 14 filesystem tools."""

    def __init__(self, backend: object = None) -> None:
        self._backend = backend

    def tools(self, workspace_root: str = ".") -> list[Tool]:
        return [
            ReadFileTool(),
            WriteFileTool(),
            AppendFileTool(),
            DeleteFileTool(),
            MoveFileTool(),
            CopyFileTool(),
            ListDirectoryTool(),
            MakeDirectoryTool(),
            FileExistsTool(),
            SearchFilesTool(),
            GrepFilesTool(),
            GetFileInfoTool(),
            ReadLinesTool(),
            PatchFileTool(),
        ]

    def all_agent_tools(self) -> list[Callable[..., object]]:
        """Return all 24 @tool()-decorated agent tools (14 original + 10 new)."""
        from agenthicc.tools.fs.agent_tools import FS_AGENT_TOOLS  # noqa: PLC0415

        return FS_AGENT_TOOLS
