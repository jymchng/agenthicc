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
from typing import TYPE_CHECKING, Literal, cast
from agenthicc.tools.base import Tool, arg_bool, arg_int, arg_str
from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, current_workspace_access

if TYPE_CHECKING:
    from agenthicc.tools.fs.discovery import DiscoveredEntry

__all__ = ["FsToolKit"]

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# PRD-133 Layer A: bound tool output so a single result can't overflow the model
# context window.  These are upstream guards; the pre-send budget guard (Layer C)
# is the hard backstop.
_MAX_LIST_ENTRIES = 1000  # max entries from list_directory/search_files
_MAX_TOOL_OUTPUT_CHARS = 100_000  # ~25k tokens cap for read_file


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


#: Bytes inspected for a NUL when deciding whether a file is binary.
_BINARY_SNIFF_BYTES = 8 * 1024


def _number_lines(lines: list[str], first: int) -> str:
    """*lines* as ``<n>\t<line>``, numbers right-aligned to one width."""
    width = len(str(first + len(lines) - 1)) if lines else 1
    return "\n".join(f"{first + index:>{width}}\t{line}" for index, line in enumerate(lines))


class ReadFileTool(Tool):
    """Read a file as numbered lines, a range of it, or report it is binary (TOOLR-10)."""

    name = "read_file"
    description = "Read a file within the workspace as numbered lines."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 1},
            "limit": {"type": "integer", "default": 0},
            "line_numbers": {"type": "boolean", "default": True},
            "encoding": {"type": "string", "default": ""},
        },
        "required": ["path"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        from agenthicc.tools.errors import ToolErrorCode, tool_error

        path = arg_str(args, "path")
        explicit_encoding = arg_str(args, "encoding", "")
        encoding = explicit_encoding or "utf-8"
        offset = arg_int(args, "offset", 1)
        limit = arg_int(args, "limit", 0)
        numbered = arg_bool(args, "line_numbers", True)
        if offset < 1 or limit < 0:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENT, "offset must be >= 1 and limit >= 0", path=path
            )
        try:
            resolved = await _resolve_path(context, path, operation="read", tool_name=self.name)
        except PermissionError as e:
            return tool_error(ToolErrorCode.PERMISSION_DENIED, str(e), path=path)

        def _read() -> dict[str, object]:
            # TOOLR-09: one thread does the whole read. The cache stamp is the
            # stat taken *before* the read, and content is cached only when a
            # second stat agrees — a write landing mid-read is never cached.
            from agenthicc.tools.fs.edits import FILE_VERSIONS, file_version
            from agenthicc.tools.fs.file_cache import get_file_cache

            try:
                before = resolved.stat()
            except FileNotFoundError:
                return tool_error(ToolErrorCode.NOT_FOUND, f"not found: {path}", path=path)
            if before.st_size > _MAX_FILE_SIZE:
                return tool_error(
                    ToolErrorCode.TOO_LARGE,
                    f"file is {before.st_size} bytes; read_file reads up to {_MAX_FILE_SIZE}",
                    path=path,
                )
            version = file_version(before)
            # Only automatic UTF-8 reads use the cache: a lossy explicit decode
            # must never be served later as proof the file is text.
            cache = None if explicit_encoding else get_file_cache()
            content = cache.get_fresh(str(resolved), encoding=encoding) if cache else None
            cached = content is not None
            if content is None:
                data = resolved.read_bytes()
                after = resolved.stat()
                if explicit_encoding:
                    content = data.decode(encoding, errors="replace")
                else:
                    try:
                        if b"\0" in data[:_BINARY_SNIFF_BYTES]:
                            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
                        content = data.decode("utf-8")
                    except UnicodeDecodeError:
                        import mimetypes

                        FILE_VERSIONS.record(resolved, version)
                        mime, _ = mimetypes.guess_type(resolved.name)
                        return {
                            "ok": True,
                            "binary": True,
                            "path": path,
                            "size_bytes": before.st_size,
                            "mime_type": mime or "application/octet-stream",
                            "version": version,
                            "note": "binary file; pass encoding to read it as text",
                        }
                content = content.replace("\r\n", "\n").replace("\r", "\n")
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
            FILE_VERSIONS.record(resolved, version)

            lines = content.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            total = len(lines)
            selected = lines[offset - 1 : offset - 1 + limit] if limit else lines[offset - 1 :]
            # PRD-133 Layer A: cap returned content, by whole lines.
            kept: list[str] = []
            used = 0
            line_cut = False
            for line in selected:
                cost = len(line) + 8
                if kept and used + cost > _MAX_TOOL_OUTPUT_CHARS:
                    break
                if len(line) > _MAX_TOOL_OUTPUT_CHARS:
                    line, line_cut = line[:_MAX_TOOL_OUTPUT_CHARS], True
                kept.append(line)
                used += cost
            whole = offset == 1 and len(kept) == total and not line_cut
            if numbered:
                rendered = _number_lines(kept, offset)
            else:
                rendered = content if whole else "\n".join(kept)
            result: dict[str, object] = {
                "ok": True,
                "content": rendered,
                "total_lines": total,
                "offset": offset,
                "lines_returned": len(kept),
                "size_bytes": before.st_size,
                "encoding": encoding,
                "version": version,
            }
            if cached:
                result["cached"] = True
            if offset - 1 + len(kept) < min(total, offset - 1 + limit if limit else total):
                result["truncated"] = True
                result["next_offset"] = offset + len(kept)
            elif line_cut:
                result["truncated"] = True
                result["note"] = f"a line longer than {_MAX_TOOL_OUTPUT_CHARS} characters was cut"
            return result

        try:
            return await asyncio.to_thread(_read)
        except (OSError, LookupError) as e:
            from agenthicc.tools.errors import classify_exception

            return tool_error(classify_exception(e), str(e), path=path)


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


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Find files matching a glob pattern within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "recursive": {"type": "boolean", "default": True},
            "sort": {"type": "string", "enum": ["path", "mtime"], "default": "path"},
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
        sort = arg_str(args, "sort", "path")
        if sort not in {"path", "mtime"}:
            from agenthicc.tools.errors import ToolErrorCode, tool_error

            return tool_error(ToolErrorCode.INVALID_ARGUMENT, "sort must be path or mtime")
        entries = list(found.entries)
        if sort == "mtime":

            def _mtime(entry: DiscoveredEntry) -> float:
                try:
                    return entry.canonical.stat().st_mtime
                except OSError:
                    return 0.0

            stamps = await asyncio.to_thread(lambda: [_mtime(entry) for entry in entries])
            ranked = sorted(
                zip(stamps, entries, strict=True), key=lambda pair: (-pair[0], pair[1].relative)
            )
            entries = [entry for _, entry in ranked]
        matches = [item.relative for item in entries]
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


def _arg_globs(args: Mapping[str, object], key: str) -> tuple[str, ...]:
    """A glob argument given as one string or a list of strings."""
    value = args.get(key)
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError(f"tool argument {key!r} must be a string or a list of strings")


class GrepFilesTool(Tool):
    name = "grep_files"
    description = "Search file contents for a pattern; a file path searches that file."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "array", "items": {"type": "string"}},
            "exclude": {"type": "array", "items": {"type": "string"}},
            "case_sensitive": {"type": "boolean"},
            "fixed_string": {"type": "boolean", "default": False},
            "context_lines": {"type": "integer", "default": 0},
            "max_results": {"type": "integer", "default": 50},
            "output": {"type": "string", "enum": ["matches", "files", "count"]},
        },
        "required": ["pattern"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        from agenthicc.tools.errors import ToolErrorCode, tool_error
        from agenthicc.tools.fs.search import (
            GrepQuery,
            SearchFile,
            compile_query,
            filter_files,
            ripgrep_path,
            search_with_python,
            search_with_ripgrep,
        )

        path = arg_str(args, "path", ".")
        output = arg_str(args, "output", "matches")
        if output not in {"matches", "files", "count"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENT, "output must be matches, files or count"
            )
        raw_case = args.get("case_sensitive")
        if raw_case is not None and not isinstance(raw_case, bool):
            raise ValueError("tool argument 'case_sensitive' must be a boolean")
        query = GrepQuery.create(
            arg_str(args, "pattern"),
            case_sensitive=raw_case,
            fixed_string=arg_bool(args, "fixed_string", False),
            context_lines=arg_int(args, "context_lines", 0),
            max_results=arg_int(args, "max_results", 50),
            output=cast("Literal['matches', 'files', 'count']", output),
        )
        include, exclude = _arg_globs(args, "glob"), _arg_globs(args, "exclude")
        try:
            compile_query(query)
        except re.error as exc:
            return tool_error(ToolErrorCode.INVALID_ARGUMENT, f"invalid regular expression: {exc}")
        try:
            resolved = await _resolve_path(context, path, operation="search", tool_name=self.name)
        except PermissionError as e:
            return tool_error(ToolErrorCode.PERMISSION_DENIED, str(e), path=path)

        discovery: dict[str, object] | None = None
        truncation = ""
        if resolved.is_file():
            root = resolved.parent
            files = [SearchFile(resolved.name, resolved)]
        elif resolved.is_dir():
            from agenthicc.tools.fs.discovery import discover

            found = await discover(
                context,
                resolved,
                pattern="*",
                recursive=arg_bool(args, "recursive", True),
                files_only=True,
                include_ignored=arg_bool(args, "include_ignored", False),
                limit=_MAX_GREP_FILES,
                operation="search",
                tool_name=self.name,
            )
            root = resolved
            files = [SearchFile(item.relative, item.canonical) for item in found.entries]
            discovery = found.diagnostics()
            truncation = found.truncated
        else:
            return tool_error(ToolErrorCode.NOT_FOUND, f"not found: {path}", path=path)
        files = filter_files(files, include, exclude)

        async with _grep_slot():
            rg = ripgrep_path()
            result = await search_with_ripgrep(rg, root, files, query) if rg else None
            if result is None:
                result = await asyncio.to_thread(search_with_python, files, query)

        payload: dict[str, object] = {
            "ok": True,
            "total_matches": result.total_matches,
            "files_matched": result.files_matched,
            "files_searched": len(files),
            "backend": result.backend,
        }
        if query.output == "matches":
            payload["matches"] = result.matches
        elif query.output == "files":
            payload["files"] = list(result.counts)
        else:
            payload["counts"] = result.counts
        if discovery is not None:
            payload["discovery"] = discovery
        notes: list[str] = []
        if query.output == "matches" and result.truncated:
            notes.append(
                f"showing {len(result.matches)} of {result.total_matches} matches; "
                "raise max_results or narrow the pattern, path or glob"
            )
        if truncation == "result_cap":
            notes.append(f"searched the first {_MAX_GREP_FILES} files; narrow the path or glob")
        elif truncation:
            notes.append(f"searched {len(files)} files: {_TRUNCATION_NOTES[truncation]}")
        if notes:
            payload["truncated"] = True
            payload["note"] = "; ".join(notes)
        return payload


class GetFileInfoTool(Tool):
    name = "get_file_info"
    description = "Return whether a path exists and its metadata."
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
            return {"ok": True, "exists": False, "path": path, "type": None}
        info = await asyncio.to_thread(_safe_stat, resolved)
        return {"ok": True, "exists": True, **info, "path": str(resolved)}


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
    """Factory that returns the 12 class-form filesystem tools."""

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
            SearchFilesTool(),
            GrepFilesTool(),
            GetFileInfoTool(),
            PatchFileTool(),
        ]

    def all_agent_tools(self) -> list[Callable[..., object]]:
        """Return every @tool()-decorated filesystem agent tool."""
        from agenthicc.tools.fs.agent_tools import FS_AGENT_TOOLS

        return FS_AGENT_TOOLS
