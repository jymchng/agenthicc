"""Filesystem tools: read, write, delete, search, grep, patch, etc. (PRD-14)."""

from __future__ import annotations

import asyncio
import datetime
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
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


#: Why a discovery stopped early, as told to the model.
_TRUNCATION_NOTES: dict[str, str] = {
    "result_cap": f"results capped at {_MAX_LIST_ENTRIES}; narrow the pattern or path",
    "examined_cap": "stopped after 50,000 entries; narrow the path",
    "time_cap": "stopped after 10 s; narrow the path",
}
#: Files a grep reads at most before stopping.
_MAX_GREP_FILES = 20_000


def _grep_slot() -> "AbstractAsyncContextManager[object]":
    """Content scanning shares the discovery walk limiter."""
    from agenthicc.tools.fs.discovery import _limiter

    return _limiter()


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


_walk_limit = 2


def configure_walk_limit(limit: int) -> None:
    """Set how many filesystem walks may run at once (``[tools] max_fs_walks``).

    Lives here, not in :mod:`agenthicc.tools.fs.discovery`, so session
    startup can set it without importing the walker before the first frame.
    """
    global _walk_limit
    _walk_limit = max(1, limit)


def walk_limit() -> int:
    return _walk_limit


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
    from agenthicc.tools.workspace_access import (
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


def _record_version(path: Path) -> str:
    """Record and return *path*'s current version for later stale-write checks."""
    from agenthicc.tools.fs.edits import FILE_VERSIONS, file_version

    version = file_version(path.stat())
    FILE_VERSIONS.record(path, version)
    return version


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

        def _read() -> dict[str, object]:
            # TOOLR-09: one thread does the whole read. The cache stamp is the
            # stat taken *before* the read, and content is cached only when a
            # second stat agrees — a write landing mid-read is never cached.
            from agenthicc.tools.fs.edits import FILE_VERSIONS, file_version
            from agenthicc.tools.fs.file_cache import get_file_cache

            try:
                before = resolved.stat()
            except FileNotFoundError:
                return {"ok": False, "error_code": "not_found", "error": f"not_found: {path}"}
            if before.st_size > _MAX_FILE_SIZE:
                return {
                    "ok": False,
                    "error_code": "too_large",
                    "error": f"file_too_large: {before.st_size} bytes",
                }
            cache = get_file_cache()
            content = cache.get_fresh(str(resolved), encoding=encoding) if cache else None
            cached = content is not None
            if content is None:
                content = resolved.read_text(encoding=encoding, errors="replace")
                after = resolved.stat()
                if cache is not None and (after.st_mtime_ns, after.st_size) == (
                    before.st_mtime_ns,
                    before.st_size,
                ):
                    cache.store_stamped(
                        str(resolved),
                        content,
                        mtime_ns=before.st_mtime_ns,
                        size=before.st_size,
                        encoding=encoding,
                    )
            version = file_version(before)
            FILE_VERSIONS.record(resolved, version)
            # PRD-133 Layer A: cap returned content to bound context tokens.
            out, truncated = _truncate_output(content)
            result: dict[str, object] = {
                "ok": True,
                "content": out,
                "size_bytes": before.st_size,
                "encoding": encoding,
                "version": version,
            }
            if cached:
                result["cached"] = True
            if truncated:
                result["truncated"] = True
            return result

        try:
            return await asyncio.to_thread(_read)
        except (OSError, LookupError) as e:
            from agenthicc.tools.errors import classify_exception, tool_error

            return tool_error(classify_exception(e), str(e))


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write content to a file atomically, creating parent directories if needed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "encoding": {"type": "string", "default": "utf-8"},
            "create_parents": {"type": "boolean", "default": True},
            "expected_version": {"type": "string", "default": ""},
            "force": {"type": "boolean", "default": False},
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
        expected_version = arg_str(args, "expected_version", "")
        force = arg_bool(args, "force", False)
        try:
            resolved = await _resolve_path(
                context,
                path,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
        except PermissionError as e:
            return {
                "ok": False,
                "error_code": "permission_denied",
                "error": f"permission_denied: {e}",
            }

        def _write() -> dict[str, object]:
            from agenthicc.tools.fs.edits import (
                FILE_VERSIONS,
                EditError,
                atomic_write,
                diff_preview,
            )

            try:
                FILE_VERSIONS.check(resolved, expected_version=expected_version, force=force)
                data = content.encode(encoding)
            except EditError as exc:
                return exc.result()
            except (LookupError, UnicodeEncodeError) as exc:
                return {"ok": False, "error_code": "invalid_argument", "error": str(exc)}
            try:
                before = resolved.read_bytes().decode(encoding)
            except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
                before = None
            version = atomic_write(resolved, data, create_parents=create_parents)
            result: dict[str, object] = {
                "ok": True,
                "path": str(resolved),
                "bytes_written": len(data),
                "version": version,
                "created": before is None,
            }
            if before is not None and before != content:
                diff, diff_truncated = diff_preview(before, content, resolved.name)
                result["diff"] = diff
                if diff_truncated:
                    result["diff_truncated"] = True
            return result

        try:
            return await asyncio.to_thread(_write)
        except OSError as e:
            from agenthicc.tools.errors import classify_exception, tool_error

            return tool_error(classify_exception(e), str(e))


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
            return {"ok": True, "path": str(resolved), "version": _record_version(resolved)}
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
            from agenthicc.tools.fs.edits import FILE_VERSIONS

            FILE_VERSIONS.forget(resolved)
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
            from agenthicc.tools.fs.edits import FILE_VERSIONS

            FILE_VERSIONS.forget(src)
            _record_version(dst)
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
        if not await asyncio.to_thread(resolved.is_dir):
            return {"ok": False, "error_code": "not_found", "error": f"not_a_directory: {path}"}
        from agenthicc.tools.fs.discovery import discover

        found = await discover(
            context,
            resolved,
            pattern=pattern,
            recursive=recursive,
            include_hidden=include_hidden,
            include_ignored=not recursive or arg_bool(args, "include_ignored", False),
            limit=_MAX_LIST_ENTRIES,
            operation="list",
            tool_name=self.name,
        )

        def _describe() -> list[dict[str, object]]:
            entries: list[dict[str, object]] = []
            for item in found.entries:
                try:
                    st = item.canonical.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": item.original.name,
                        "path": item.relative,
                        "type": "dir" if item.is_dir else "file",
                        "size_bytes": st.st_size,
                        "modified_at": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(),
                    }
                )
            return entries

        entries = await asyncio.to_thread(_describe)
        result: dict[str, object] = {
            "ok": True,
            "entries": entries,
            "count": len(entries),
            "discovery": found.diagnostics(),
        }
        if found.truncated:
            result["truncated"] = True
            result["note"] = _TRUNCATION_NOTES[found.truncated]
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
        except PermissionError as exc:
            from agenthicc.tools.errors import ToolErrorCode, tool_error

            return tool_error(ToolErrorCode.PERMISSION_DENIED, str(exc), path=path)
        exists = resolved.exists()
        file_type = None
        if exists:
            file_type = "dir" if resolved.is_dir() else "file"
        return {"ok": True, "exists": exists, "path": path, "type": file_type}


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

        from agenthicc.tools.fs.discovery import discover

        found = await discover(
            context,
            resolved,
            pattern=pattern,
            recursive=recursive,
            files_only=True,
            include_ignored=arg_bool(args, "include_ignored", False),
            limit=_MAX_LIST_ENTRIES,
            operation="search",
            tool_name=self.name,
        )
        matches = [item.relative for item in found.entries]
        result: dict[str, object] = {
            "ok": True,
            "matches": matches,
            "count": len(matches),
            "discovery": found.diagnostics(),
        }
        if found.truncated:
            result["truncated"] = True
            result["note"] = _TRUNCATION_NOTES[found.truncated]
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

        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return {
                "ok": False,
                "error_code": "invalid_argument",
                "error": f"invalid regular expression: {exc}",
            }
        from agenthicc.tools.fs.discovery import discover

        found = await discover(
            context,
            resolved,
            pattern="*",
            recursive=recursive,
            files_only=True,
            include_ignored=arg_bool(args, "include_ignored", False),
            limit=_MAX_GREP_FILES,
            operation="search",
            tool_name=self.name,
        )

        def _grep() -> tuple[list[dict[str, object]], int]:
            matches: list[dict[str, object]] = []
            files_matched = 0
            for item in found.entries:
                try:
                    text = item.canonical.read_text(encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, OSError):
                    continue
                hit = False
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        hit = True
                        matches.append(
                            {"file": item.relative, "line_number": i, "line": line.rstrip()}
                        )
                        if len(matches) >= max_results:
                            return matches, files_matched + 1
                files_matched += hit
            return matches, files_matched

        async with _grep_slot():
            matches, files_matched = await asyncio.to_thread(_grep)
        result: dict[str, object] = {
            "ok": True,
            "matches": matches,
            "count": len(matches),
            "files_matched": files_matched,
            "discovery": found.diagnostics(),
        }
        if len(matches) >= max_results:
            result["truncated"] = True
            result["note"] = f"stopped at max_results={max_results}"
        elif found.truncated:
            result["truncated"] = True
            result["note"] = _TRUNCATION_NOTES[found.truncated]
        return result


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
            from agenthicc.tools.errors import ToolErrorCode, tool_error

            return tool_error(ToolErrorCode.PERMISSION_DENIED, str(e), path=path)
        if not resolved.exists():
            from agenthicc.tools.errors import ToolErrorCode, tool_error

            return tool_error(ToolErrorCode.NOT_FOUND, f"not found: {path}", path=path)
        info = await asyncio.to_thread(_safe_stat, resolved)
        return {"ok": True, **info, "path": str(resolved)}


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
            "ok": True,
            "total_lines": total,
            "version": _record_version(resolved),
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
    description = (
        "Replace an exact string in a file. By default old_content must occur exactly "
        "expected_replacements times (1); set replace_all to replace every occurrence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_content": {"type": "string"},
            "new_content": {"type": "string"},
            "expected_replacements": {"type": "integer", "default": 1},
            "replace_all": {"type": "boolean", "default": False},
            "expected_version": {"type": "string", "default": ""},
            "force": {"type": "boolean", "default": False},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path", "old_content", "new_content"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        path = arg_str(args, "path")
        old = arg_str(args, "old_content")
        new = arg_str(args, "new_content")
        expected = arg_int(args, "expected_replacements", 1)
        replace_all = arg_bool(args, "replace_all", False)
        expected_version = arg_str(args, "expected_version", "")
        force = arg_bool(args, "force", False)
        encoding = arg_str(args, "encoding", "utf-8")
        if not old:
            return {
                "ok": False,
                "error_code": "invalid_argument",
                "error": "old_content must not be empty",
            }
        if expected < 1 and not replace_all:
            return {
                "ok": False,
                "error_code": "invalid_argument",
                "error": "expected_replacements must be at least 1 (or set replace_all)",
            }
        try:
            resolved = await _resolve_path(
                context,
                path,
                operation="write",
                tool_name=self.name,
                capabilities=frozenset({"write"}),
            )
        except PermissionError as e:
            return {
                "ok": False,
                "error_code": "permission_denied",
                "error": f"permission_denied: {e}",
            }

        def _patch() -> dict[str, object]:
            from agenthicc.tools.fs.edits import (
                FILE_VERSIONS,
                EditError,
                atomic_write,
                changed_line_ranges,
                decode_for_edit,
                diff_preview,
            )

            try:
                data = resolved.read_bytes()
            except FileNotFoundError:
                return {"ok": False, "error_code": "not_found", "error": f"not_found: {path}"}
            try:
                FILE_VERSIONS.check(resolved, expected_version=expected_version, force=force)
                decoded = decode_for_edit(data, encoding)
            except EditError as exc:
                return exc.result()
            text = decoded.text
            target, replacement = old, new
            count = text.count(target)
            if count == 0 and "\n" in old:
                # The model writes LF; match a CRLF (or mixed) file's lines too.
                crlf_old = old.replace("\r\n", "\n").replace("\n", "\r\n")
                if text.count(crlf_old):
                    target = crlf_old
                    replacement = new.replace("\r\n", "\n").replace("\n", "\r\n")
                    count = text.count(target)
            if count == 0:
                return {
                    "ok": False,
                    "error_code": "not_found",
                    "reason": "no_match",
                    "error": f"old_content not found in {path}",
                    "replacements": 0,
                }
            if not replace_all and count != expected:
                lines: list[int] = []
                position = text.find(target)
                while position != -1 and len(lines) < 3:
                    lines.append(text.count("\n", 0, position) + 1)
                    position = text.find(target, position + 1)
                return {
                    "ok": False,
                    "error_code": "ambiguous_match",
                    "error": (
                        f"old_content occurs {count} times in {path}, expected {expected}; "
                        "add surrounding lines to make it unique or set replace_all"
                    ),
                    "occurrences": count,
                    "lines": lines,
                    "replacements": 0,
                }
            patched = text.replace(target, replacement)
            version = atomic_write(resolved, decoded.encode(patched))
            diff, diff_truncated = diff_preview(text, patched, resolved.name)
            result: dict[str, object] = {
                "ok": True,
                "path": str(resolved),
                "replacements": count,
                "version": version,
                "changed_lines": changed_line_ranges(text, patched),
                "diff": diff,
            }
            if diff_truncated:
                result["diff_truncated"] = True
            return result

        try:
            return await asyncio.to_thread(_patch)
        except OSError as e:
            from agenthicc.tools.errors import classify_exception, tool_error

            return tool_error(classify_exception(e), str(e))


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
        from agenthicc.tools.fs.agent_tools import FS_AGENT_TOOLS

        return FS_AGENT_TOOLS
