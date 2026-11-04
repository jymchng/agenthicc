"""@tool() wrappers for every filesystem tool — for use with lauren-ai AgentRunnerBase.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""

import asyncio
import os
import re
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from lauren_ai._tools import tool
from agenthicc.tools.base import arg_str
from agenthicc.tools.capabilities import (
    tool_read,
    tool_write,
    tool_read_search,
    tool_exploratory,
)
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, current_workspace_access

if TYPE_CHECKING:
    from agenthicc.tools.fs.backend import FilesystemBackend
    from agenthicc.tools.fs.router import BackendRouter

__all__ = [
    "append_file",
    "apply_diff",
    "batch_copy",
    "batch_delete",
    "batch_move",
    "batch_read",
    "batch_write",
    "checksum_file",
    "copy_file",
    "delete_file",
    "file_exists",
    "get_file_info",
    "grep_file",
    "grep_files",
    "list_directory",
    "make_directory",
    "move_file",
    "patch_file",
    "read_file",
    "read_lines",
    "search_files",
    "touch_file",
    "truncate_file",
    "write_file",
    "FS_AGENT_TOOLS",
]


def _CTX() -> dict[str, object]:
    policy = current_workspace_access()
    context: dict[str, object] = {
        "workspace_root": (_router.default.root if _router is not None else os.getcwd())
    }
    if policy is not None:
        context["workspace_access"] = policy
        context["workspace_root"] = str(policy.scope.primary_root)
    return context


_router: "BackendRouter | None" = None


class _BatchWriteFile(TypedDict):
    """One file entry accepted by :func:`batch_write`."""

    path: str
    content: str


class _BatchMoveFile(TypedDict):
    """One source/destination entry accepted by batch move/copy tools."""

    source: str
    destination: str


def _get_backend(path: str = ".", context: dict[str, object] | None = None) -> "FilesystemBackend":
    supplied = (context or {}).get("workspace_access")
    policy = supplied if isinstance(supplied, WorkspaceAccessPolicy) else current_workspace_access()
    if policy is not None:
        # The mode-aware policy is the authoritative boundary for local
        # session tools.  A legacy router may still point at a root-scoped
        # backend and would therefore reject an explicitly approved parent
        # path (or, worse, make routing decide access before the policy does).
        if _router is not None:
            routed = _router.resolve(path)
            if routed is not _router.default:
                return routed
        from agenthicc.tools.fs.linux import LinuxFilesystemBackend

        return LinuxFilesystemBackend(policy.scope.primary_root, enforce_root=False)
    if _router is not None:
        return _router.resolve(path)
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend

    return LinuxFilesystemBackend(os.getcwd())


def configure_router(router: "BackendRouter") -> None:
    """Wire up a BackendRouter for all new fs tool calls."""
    global _router
    _router = router


@tool_exploratory
@tool_read
@tool()
async def read_file(path: str, encoding: str = "utf-8") -> dict[str, object]:
    """Read the full contents of a file.

    Args:
        path: File path to read (relative to workspace root).
        encoding: Text encoding (default utf-8).
    """
    from agenthicc.tools.fs import ReadFileTool

    return await ReadFileTool().execute({"path": path, "encoding": encoding}, _CTX())


@tool_write
@tool()
async def write_file(
    path: str,
    content: str,
    create_parents: bool = True,
    expected_version: str = "",
    force: bool = False,
) -> dict[str, object]:
    """Write content to a file atomically (creates parent directories if needed).

    An existing file that changed on disk since you last read it is refused
    with ``stale_file``; read it again, or pass ``force`` to overwrite.

    Args:
        path: Destination file path.
        content: Text content to write.
        create_parents: Create missing parent directories (default True).
        expected_version: The ``version`` from your last read; refuse if the file differs.
        force: Overwrite even if the file changed since you last read it.
    """
    from agenthicc.tools.fs import WriteFileTool

    return await WriteFileTool().execute(
        {
            "path": path,
            "content": content,
            "create_parents": create_parents,
            "expected_version": expected_version,
            "force": force,
        },
        _CTX(),
    )


@tool_write
@tool()
async def append_file(path: str, content: str) -> dict[str, object]:
    """Append text to the end of an existing file.

    Args:
        path: File path to append to.
        content: Text to append.
    """
    from agenthicc.tools.fs import AppendFileTool

    return await AppendFileTool().execute({"path": path, "content": content}, _CTX())


@tool_write
@tool()
async def delete_file(path: str) -> dict[str, object]:
    """Delete a file from the workspace.

    Args:
        path: File path to delete.
    """
    from agenthicc.tools.fs import DeleteFileTool

    return await DeleteFileTool().execute({"path": path}, _CTX())


@tool_write
@tool()
async def move_file(source: str, destination: str) -> dict[str, object]:
    """Move or rename a file within the workspace.

    Args:
        source: Source file path.
        destination: Destination file path.
    """
    from agenthicc.tools.fs import MoveFileTool

    return await MoveFileTool().execute({"source": source, "destination": destination}, _CTX())


@tool_write
@tool()
async def copy_file(source: str, destination: str) -> dict[str, object]:
    """Copy a file to a new location within the workspace.

    Args:
        source: Source file path.
        destination: Destination file path.
    """
    from agenthicc.tools.fs import CopyFileTool

    return await CopyFileTool().execute({"source": source, "destination": destination}, _CTX())


@tool_exploratory
@tool_read_search
@tool()
async def list_directory(
    path: str = ".", pattern: str = "*", recursive: bool = False
) -> dict[str, object]:
    """List files and directories at a path.

    Args:
        path: Directory to list (default: current directory).
        pattern: Glob pattern to filter entries (default: all).
        recursive: If True, traverse subdirectories recursively.
    """
    from agenthicc.tools.fs import ListDirectoryTool

    return await ListDirectoryTool().execute(
        {"path": path, "pattern": pattern, "recursive": recursive}, _CTX()
    )


@tool_write
@tool()
async def make_directory(path: str) -> dict[str, object]:
    """Create a directory (and all missing parent directories).

    Args:
        path: Directory path to create.
    """
    from agenthicc.tools.fs import MakeDirectoryTool

    return await MakeDirectoryTool().execute({"path": path}, _CTX())


@tool_exploratory
@tool_read
@tool()
async def file_exists(path: str) -> dict[str, object]:
    """Check whether a file or directory exists.

    Args:
        path: Path to check.
    """
    from agenthicc.tools.fs import FileExistsTool

    return await FileExistsTool().execute({"path": path}, _CTX())


@tool_exploratory
@tool_read_search
@tool()
async def search_files(pattern: str, path: str = ".", recursive: bool = True) -> dict[str, object]:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g. "*.py", "**/*.ts").
        path: Root directory to search (default: current directory).
        recursive: Search subdirectories recursively (default True).
    """
    from agenthicc.tools.fs import SearchFilesTool

    return await SearchFilesTool().execute(
        {"pattern": pattern, "path": path, "recursive": recursive}, _CTX()
    )


@tool_exploratory
@tool_read_search
@tool()
async def grep_files(pattern: str, path: str = ".", max_results: int = 50) -> dict[str, object]:
    """Search file contents for a regex pattern and return matching lines.

    Args:
        pattern: Regular expression to search for.
        path: Directory to search (default: current directory).
        max_results: Maximum number of matches to return (default 50).
    """
    from agenthicc.tools.fs import GrepFilesTool

    return await GrepFilesTool().execute(
        {"pattern": pattern, "path": path, "recursive": True, "max_results": max_results},
        _CTX(),
    )


@tool_exploratory
@tool_read
@tool()
async def get_file_info(path: str) -> dict[str, object]:
    """Return metadata for a file or directory (size, dates, permissions).

    Args:
        path: File or directory path.
    """
    from agenthicc.tools.fs import GetFileInfoTool

    return await GetFileInfoTool().execute({"path": path}, _CTX())


@tool_exploratory
@tool_read
@tool()
async def read_lines(path: str, start: int = 1, end: int | None = None) -> dict[str, object]:
    """Read a specific range of lines from a file (1-indexed).

    Args:
        path: File path to read.
        start: First line to read (1-indexed, default 1).
        end: Last line to read inclusive (default: end of file).
    """
    from agenthicc.tools.fs import ReadLinesTool

    args: dict[str, object] = {"path": path, "start": start}
    if end is not None:
        args["end"] = end
    return await ReadLinesTool().execute(args, _CTX())


@tool_write
@tool()
async def patch_file(
    path: str,
    old_content: str,
    new_content: str,
    expected_replacements: int = 1,
    replace_all: bool = False,
    expected_version: str = "",
    force: bool = False,
) -> dict[str, object]:
    """Replace an exact string in a file, keeping every other byte unchanged.

    ``old_content`` must occur exactly ``expected_replacements`` times (default
    once); otherwise nothing is written and ``ambiguous_match`` reports where
    it occurs. The file's encoding, BOM and line endings are preserved.

    Args:
        path: File path to patch.
        old_content: Exact text to find (include surrounding lines to make it unique).
        new_content: Replacement text.
        expected_replacements: How many occurrences must exist (default 1).
        replace_all: Replace every occurrence regardless of the count.
        expected_version: The ``version`` from your last read; refuse if the file differs.
        force: Edit even if the file changed since you last read it.
    """
    from agenthicc.tools.fs import PatchFileTool

    return await PatchFileTool().execute(
        {
            "path": path,
            "old_content": old_content,
            "new_content": new_content,
            "expected_replacements": expected_replacements,
            "replace_all": replace_all,
            "expected_version": expected_version,
            "force": force,
        },
        _CTX(),
    )


@tool_exploratory
@tool_read_search
@tool()
async def grep_file(
    path: str,
    pattern: str,
    case_sensitive: bool = True,
    context_lines: int = 0,
) -> dict[str, object]:
    """Search a single file for a regex pattern, returning per-line match details.

    Args:
        path: File path to search (relative to workspace root).
        pattern: Regular expression pattern to search for.
        case_sensitive: Whether the match is case-sensitive (default True).
        context_lines: Number of surrounding lines to include with each match (default 0).
    """
    from agenthicc.tools.fs import _authorized_path

    context = _CTX()
    try:
        resolved = await _authorized_path(
            context, path, operation="search", tool_name="grep_file", tool_input={"path": path}
        )
        text = await asyncio.to_thread(resolved.read_text, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"ok": False, "error_code": "not_found", "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error_code": "permission_denied", "error": "permission_denied"}

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return {
            "ok": False,
            "error_code": "invalid_argument",
            "error": f"invalid regular expression: {exc}",
        }
    lines = text.splitlines()
    matches = []
    for i, line in enumerate(lines):
        m = compiled.search(line)
        if m:
            entry: dict[str, object] = {
                "line_number": i + 1,
                "line": line,
                "match_start": m.start(),
                "match_end": m.end(),
            }
            if context_lines > 0:
                before_start = max(0, i - context_lines)
                after_end = min(len(lines), i + context_lines + 1)
                entry["context_before"] = lines[before_start:i]
                entry["context_after"] = lines[i + 1 : after_end]
            matches.append(entry)
    return {"ok": True, "path": path, "matches": matches, "total_matches": len(matches)}


@tool_write
@tool()
async def apply_diff(
    path: str,
    diff: str,
    allow_partial: bool = False,
    expected_version: str = "",
    force: bool = False,
) -> dict[str, object]:
    """Apply a unified diff to a file, keeping its encoding and line endings.

    Each hunk is applied at its stated line, or at the nearest exact match of
    its context within 50 lines; the result reports where each hunk landed.

    Args:
        path: File path to patch (relative to workspace root).
        diff: Unified diff string (output of ``diff -u`` or similar).
        allow_partial: If True, apply successfully-matched hunks even when some fail (default False).
        expected_version: The ``version`` from your last read; refuse if the file differs.
        force: Edit even if the file changed since you last read it.
    """
    from agenthicc.tools.fs import _authorized_path

    context = _CTX()
    try:
        resolved = await _authorized_path(
            context,
            path,
            operation="write",
            tool_name="apply_diff",
            capabilities=frozenset({"write"}),
            tool_input={"path": path},
        )
    except FileNotFoundError:
        return {"ok": False, "error_code": "not_found", "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error_code": "permission_denied", "error": "permission_denied"}
    return await asyncio.to_thread(
        _apply_diff_sync, resolved, diff, allow_partial, expected_version, force
    )


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
#: How far from its stated line a hunk's context is searched for.
_HUNK_FUZZ_LINES = 50


def _parse_hunks(diff: str) -> list[tuple[int, list[str], list[str]]]:
    """``(zero-based start, old lines, new lines)`` per hunk, line endings removed."""
    matches = list(_HUNK_HEADER.finditer(diff))
    hunks: list[tuple[int, list[str], list[str]]] = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(diff)
        old_lines: list[str] = []
        new_lines: list[str] = []
        for line in diff[match.end() : body_end].splitlines()[1:]:
            if line.startswith("-"):
                old_lines.append(line[1:])
            elif line.startswith("+"):
                new_lines.append(line[1:])
            elif line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif line == "":
                # Some generators strip the space of an empty context line.
                old_lines.append("")
                new_lines.append("")
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        start = int(match.group(1)) - (1 if old_count else 0)
        while (
            old_lines
            and new_lines
            and old_lines[-1] == "" == new_lines[-1]
            and len(old_lines) > old_count
        ):
            old_lines.pop()
            new_lines.pop()
        hunks.append((max(0, start), old_lines, new_lines))
    return hunks


def _apply_diff_sync(
    resolved: Path, diff: str, allow_partial: bool, expected_version: str, force: bool
) -> dict[str, object]:
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
        FILE_VERSIONS.check(resolved, expected_version=expected_version, force=force)
        decoded = decode_for_edit(data)
    except FileNotFoundError:
        return {"ok": False, "error_code": "not_found", "error": f"file not found: {resolved}"}
    except EditError as exc:
        return exc.result()
    hunks = _parse_hunks(diff)
    if not hunks:
        return {"ok": False, "error_code": "invalid_argument", "error": "no hunks found in diff"}

    lines = decoded.text.splitlines(keepends=True)
    bare = [line.rstrip("\r\n") for line in lines]
    ending = decoded.line_ending
    offset = 0
    placements: list[dict[str, object]] = []
    failed: list[int] = []
    for number, (start, old_lines, new_lines) in enumerate(hunks, 1):
        expected_at = start + offset
        at = -1
        for distance in range(_HUNK_FUZZ_LINES + 1):
            for candidate in (expected_at - distance, expected_at + distance):
                if 0 <= candidate <= len(bare) and bare[candidate : candidate + len(old_lines)] == (
                    old_lines
                ):
                    at = candidate
                    break
            if at != -1:
                break
        if at == -1:
            failed.append(number)
            if not allow_partial:
                return {
                    "ok": False,
                    "error_code": "no_match",
                    "error": f"hunk {number} context not found within {_HUNK_FUZZ_LINES} lines",
                    "hunks_applied": 0,
                    "hunks_failed": len(failed),
                }
            continue
        replaced_end = at + len(old_lines)
        # The file's last line may have no newline; a hunk ending there keeps that.
        keeps_open_end = (
            replaced_end == len(lines) and bool(lines) and not lines[-1].endswith(("\n", "\r"))
        )
        rendered = [line + ending for line in new_lines]
        if keeps_open_end and rendered:
            rendered[-1] = new_lines[-1]
        lines[at:replaced_end] = rendered
        bare[at:replaced_end] = new_lines
        placements.append({"hunk": number, "line": at + 1, "offset": at - expected_at})
        offset += len(new_lines) - len(old_lines)

    patched = "".join(lines)
    version = atomic_write(resolved, decoded.encode(patched))
    preview, preview_truncated = diff_preview(decoded.text, patched, resolved.name)
    result: dict[str, object] = {
        "ok": not failed,
        "hunks_applied": len(placements),
        "hunks_failed": len(failed),
        "hunks": placements,
        "version": version,
        "changed_lines": changed_line_ranges(decoded.text, patched),
        "diff": preview,
    }
    if failed:
        result["error_code"] = "partial"
        result["failed_hunks"] = failed
    if preview_truncated:
        result["diff_truncated"] = True
    return result


@tool_exploratory
@tool_read
@tool()
async def checksum_file(path: str, algorithm: str = "sha256") -> dict[str, object]:
    """Compute a cryptographic checksum of a file.

    Args:
        path: File path to checksum (relative to workspace root).
        algorithm: Hash algorithm name accepted by ``hashlib`` (default ``sha256``).
    """
    from agenthicc.tools.fs import _authorized_path

    context = _CTX()
    try:
        resolved = await _authorized_path(
            context, path, operation="read", tool_name="checksum_file", tool_input={"path": path}
        )
    except FileNotFoundError:
        return {"ok": False, "error_code": "not_found", "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error_code": "permission_denied", "error": "permission_denied"}
    try:
        hashlib.new(algorithm)
    except ValueError:
        return {
            "ok": False,
            "error_code": "invalid_argument",
            "error": f"unsupported algorithm: {algorithm}",
        }

    def _digest() -> str:
        # Streamed in a worker thread (TOOLR-09): any size, no loop stall.
        digest = hashlib.new(algorithm)
        with resolved.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()

    try:
        value = await asyncio.to_thread(_digest)
    except FileNotFoundError:
        return {"ok": False, "error_code": "not_found", "error": f"file not found: {path}"}
    return {"ok": True, "path": path, "algorithm": algorithm, "digest": value}


@tool_write
@tool()
async def truncate_file(path: str, size: int = 0) -> dict[str, object]:
    """Truncate a file to a given byte size.

    Args:
        path: File path to truncate (relative to workspace root).
        size: Target size in bytes (default 0 — empties the file).
    """
    from agenthicc.tools.fs import _authorized_path

    if size < 0:
        return {"ok": False, "error_code": "invalid_argument", "error": "size must be >= 0"}
    context = _CTX()
    try:
        resolved = await _authorized_path(
            context,
            path,
            operation="write",
            tool_name="truncate_file",
            capabilities=frozenset({"write"}),
            tool_input={"path": path},
        )
    except FileNotFoundError:
        return {"ok": False, "error_code": "not_found", "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error_code": "permission_denied", "error": "permission_denied"}

    def _truncate() -> dict[str, object]:
        from agenthicc.tools.fs.edits import atomic_write

        try:
            data = resolved.read_bytes()
        except FileNotFoundError:
            return {"ok": False, "error_code": "not_found", "error": f"file not found: {path}"}
        kept = data[:size] + b"\0" * max(0, size - len(data))
        version = atomic_write(resolved, kept, create_parents=False)
        return {"ok": True, "path": path, "new_size": len(kept), "version": version}

    return await asyncio.to_thread(_truncate)


@tool_write
@tool()
async def touch_file(path: str, create: bool = True) -> dict[str, object]:
    """Create a file if it does not exist, or update its modification time if it does.

    Args:
        path: File path to touch (relative to workspace root).
        create: Create the file when it does not exist (default True).
                If False and the file is missing, returns an error.
    """
    from agenthicc.tools.fs import _authorized_path

    context = _CTX()
    try:
        resolved = await _authorized_path(
            context,
            path,
            operation="write",
            tool_name="touch_file",
            capabilities=frozenset({"write"}),
            tool_input={"path": path},
        )
    except PermissionError:
        return {"ok": False, "error_code": "permission_denied", "error": "permission_denied"}

    def _touch() -> dict[str, object]:
        from agenthicc.tools.fs.edits import FILE_VERSIONS, file_version

        existed = resolved.exists()
        if not existed and not create:
            return {
                "ok": False,
                "error_code": "not_found",
                "error": f"not found: {path}",
                "created": False,
            }
        if existed:
            os.utime(resolved)
        else:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.touch()
        version = file_version(resolved.stat())
        FILE_VERSIONS.record(resolved, version)
        return {"ok": True, "path": path, "created": not existed, "version": version}

    return await asyncio.to_thread(_touch)


@tool_exploratory
@tool_read
@tool()
async def batch_read(paths: list[str], encoding: str = "utf-8") -> dict[str, object]:
    """Read multiple files in a single call.

    Args:
        paths: List of file paths to read (relative to workspace root).
        encoding: Text encoding for all files (default utf-8).
    """
    from agenthicc.tools.fs import _authorize_tool

    context = _CTX()
    try:
        authorized = await _authorize_tool(context, "batch_read", {"paths": paths})
    except PermissionError as exc:
        return {"ok": False, "error": str(exc), "results": [], "total": len(paths)}
    b = _get_backend(context=context)
    backend_paths = [str(path) for path in authorized] if authorized else paths
    results = await asyncio.to_thread(b.batch_read, backend_paths, encoding)
    for original, result in zip(paths, results, strict=False):
        result["path"] = original
    total_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": total_ok == len(paths),
        "results": results,
        "total": len(paths),
        "succeeded": total_ok,
        "failed": len(paths) - total_ok,
    }


@tool_write
@tool()
async def batch_write(
    files: list[_BatchWriteFile], create_parents: bool = True
) -> dict[str, object]:
    """Write multiple files in a single call.

    Args:
        files: List of ``{"path": str, "content": str}`` dicts.
        create_parents: Create missing parent directories for each file (default True).
    """
    for item in files:
        if "path" not in item or "content" not in item:
            return {
                "ok": False,
                "error": 'each entry must have "path" and "content" keys',
                "results": [],
                "total": len(files),
                "succeeded": 0,
                "failed": len(files),
            }
    from agenthicc.tools.fs import _authorize_tool

    context = _CTX()
    try:
        authorized = await _authorize_tool(
            context, "batch_write", {"files": files}, frozenset({"write"})
        )
    except PermissionError as exc:
        return {"ok": False, "error": str(exc), "results": [], "total": len(files)}
    b = _get_backend(context=context)
    backend_files: list[dict[str, object]] = [dict(item) for item in files]
    if authorized:
        for entry, path in zip(backend_files, authorized, strict=False):
            entry["path"] = str(path)
    results = await asyncio.to_thread(b.batch_write, backend_files, create_parents)
    for original, result in zip(files, results, strict=False):
        result["path"] = original["path"]
    total_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": total_ok == len(files),
        "results": results,
        "total": len(files),
        "succeeded": total_ok,
        "failed": len(files) - total_ok,
    }


@tool_write
@tool()
async def batch_delete(paths: list[str]) -> dict[str, object]:
    """Delete multiple files in a single call.

    Args:
        paths: List of file paths to delete (relative to workspace root).
    """
    from agenthicc.tools.fs import _authorize_tool

    context = _CTX()
    try:
        authorized = await _authorize_tool(
            context, "batch_delete", {"paths": paths}, frozenset({"write"})
        )
    except PermissionError as exc:
        return {"ok": False, "error": str(exc), "results": [], "total": len(paths)}
    b = _get_backend(context=context)
    backend_paths = [str(path) for path in authorized] if authorized else paths
    results = await asyncio.to_thread(b.batch_delete, backend_paths)
    for original, result in zip(paths, results, strict=False):
        result["path"] = original
    total_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": total_ok == len(paths),
        "results": results,
        "total": len(paths),
        "succeeded": total_ok,
        "failed": len(paths) - total_ok,
    }


@tool_write
@tool()
async def batch_move(moves: list[_BatchMoveFile]) -> dict[str, object]:
    """Move multiple files in a single call.

    Args:
        moves: List of ``{"source": str, "destination": str}`` dicts.
    """
    from agenthicc.tools.fs import _authorize_tool

    context = _CTX()
    try:
        authorized = await _authorize_tool(
            context, "batch_move", {"moves": moves}, frozenset({"write"})
        )
    except PermissionError as exc:
        return {"ok": False, "error": str(exc), "results": [], "total": len(moves)}
    b = _get_backend(context=context)
    results = []
    all_ok = True
    for index, item in enumerate(moves):
        display_src = arg_str(item, "source", "")
        display_dst = arg_str(item, "destination", "")
        src = display_src
        dst = display_dst
        if authorized:
            src = str(authorized[index * 2])
            dst = str(authorized[index * 2 + 1])
        try:
            await asyncio.to_thread(b.move, src, dst)
            results.append({"source": display_src, "destination": display_dst, "ok": True})
        except Exception as exc:
            results.append(
                {
                    "source": display_src,
                    "destination": display_dst,
                    "ok": False,
                    "error": str(exc),
                }
            )
            all_ok = False
    succeeded = sum(1 for r in results if r["ok"])
    return {
        "ok": all_ok,
        "results": results,
        "succeeded": succeeded,
        "failed": len(moves) - succeeded,
    }


@tool_write
@tool()
async def batch_copy(copies: list[_BatchMoveFile]) -> dict[str, object]:
    """Copy multiple files in a single call.

    Args:
        copies: List of ``{"source": str, "destination": str}`` dicts.
    """
    from agenthicc.tools.fs import _authorize_tool

    context = _CTX()
    try:
        authorized = await _authorize_tool(
            context, "batch_copy", {"copies": copies}, frozenset({"write"})
        )
    except PermissionError as exc:
        return {"ok": False, "error": str(exc), "results": [], "total": len(copies)}
    b = _get_backend(context=context)
    results = []
    all_ok = True
    for index, item in enumerate(copies):
        display_src = arg_str(item, "source", "")
        display_dst = arg_str(item, "destination", "")
        src = display_src
        dst = display_dst
        if authorized:
            src = str(authorized[index * 2])
            dst = str(authorized[index * 2 + 1])
        try:
            await asyncio.to_thread(b.copy, src, dst)
            results.append({"source": display_src, "destination": display_dst, "ok": True})
        except Exception as exc:
            results.append(
                {
                    "source": display_src,
                    "destination": display_dst,
                    "ok": False,
                    "error": str(exc),
                }
            )
            all_ok = False
    succeeded = sum(1 for r in results if r["ok"])
    return {
        "ok": all_ok,
        "results": results,
        "succeeded": succeeded,
        "failed": len(copies) - succeeded,
    }


#: All 24 fs agent tools — ready to pass to @use_tools().
FS_AGENT_TOOLS: list[Callable[..., object]] = [
    read_file,
    write_file,
    append_file,
    delete_file,
    move_file,
    copy_file,
    list_directory,
    make_directory,
    file_exists,
    search_files,
    grep_files,
    get_file_info,
    read_lines,
    patch_file,
    grep_file,
    apply_diff,
    checksum_file,
    truncate_file,
    touch_file,
    batch_read,
    batch_write,
    batch_delete,
    batch_move,
    batch_copy,
]
