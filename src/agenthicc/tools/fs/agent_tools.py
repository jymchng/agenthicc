"""@tool() wrappers for every filesystem tool — for use with lauren-ai AgentRunnerBase.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""

import os
import re
import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict
from lauren_ai._tools import tool
from agenthicc.tools.base import arg_str
from agenthicc.tools.capabilities import (
    tool_read,
    tool_write,
    tool_read_search,
)

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

_CTX = lambda: {"workspace_root": os.getcwd()}  # noqa: E731

_router: "BackendRouter | None" = None


class _BatchWriteFile(TypedDict):
    """One file entry accepted by :func:`batch_write`."""

    path: str
    content: str


class _BatchMoveFile(TypedDict):
    """One source/destination entry accepted by batch move/copy tools."""

    source: str
    destination: str


def _get_backend(path: str = ".") -> "FilesystemBackend":
    if _router is not None:
        return _router.resolve(path)
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend  # noqa: PLC0415

    return LinuxFilesystemBackend(os.getcwd())


def configure_router(router: "BackendRouter") -> None:
    """Wire up a BackendRouter for all new fs tool calls."""
    global _router
    _router = router


@tool_read
@tool()
async def read_file(path: str, encoding: str = "utf-8") -> dict[str, object]:
    """Read the full contents of a file.

    Args:
        path: File path to read (relative to workspace root).
        encoding: Text encoding (default utf-8).
    """
    from agenthicc.tools.fs import ReadFileTool  # noqa: PLC0415

    return await ReadFileTool().execute({"path": path, "encoding": encoding}, _CTX())


@tool_write
@tool()
async def write_file(path: str, content: str, create_parents: bool = True) -> dict[str, object]:
    """Write content to a file (creates parent directories if needed).

    Args:
        path: Destination file path.
        content: Text content to write.
        create_parents: Create missing parent directories (default True).
    """
    from agenthicc.tools.fs import WriteFileTool  # noqa: PLC0415

    return await WriteFileTool().execute(
        {"path": path, "content": content, "create_parents": create_parents}, _CTX()
    )


@tool_write
@tool()
async def append_file(path: str, content: str) -> dict[str, object]:
    """Append text to the end of an existing file.

    Args:
        path: File path to append to.
        content: Text to append.
    """
    from agenthicc.tools.fs import AppendFileTool  # noqa: PLC0415

    return await AppendFileTool().execute({"path": path, "content": content}, _CTX())


@tool_write
@tool()
async def delete_file(path: str) -> dict[str, object]:
    """Delete a file from the workspace.

    Args:
        path: File path to delete.
    """
    from agenthicc.tools.fs import DeleteFileTool  # noqa: PLC0415

    return await DeleteFileTool().execute({"path": path}, _CTX())


@tool_write
@tool()
async def move_file(source: str, destination: str) -> dict[str, object]:
    """Move or rename a file within the workspace.

    Args:
        source: Source file path.
        destination: Destination file path.
    """
    from agenthicc.tools.fs import MoveFileTool  # noqa: PLC0415

    return await MoveFileTool().execute({"source": source, "destination": destination}, _CTX())


@tool_write
@tool()
async def copy_file(source: str, destination: str) -> dict[str, object]:
    """Copy a file to a new location within the workspace.

    Args:
        source: Source file path.
        destination: Destination file path.
    """
    from agenthicc.tools.fs import CopyFileTool  # noqa: PLC0415

    return await CopyFileTool().execute({"source": source, "destination": destination}, _CTX())


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
    from agenthicc.tools.fs import ListDirectoryTool  # noqa: PLC0415

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
    from agenthicc.tools.fs import MakeDirectoryTool  # noqa: PLC0415

    return await MakeDirectoryTool().execute({"path": path}, _CTX())


@tool_read
@tool()
async def file_exists(path: str) -> dict[str, object]:
    """Check whether a file or directory exists.

    Args:
        path: Path to check.
    """
    from agenthicc.tools.fs import FileExistsTool  # noqa: PLC0415

    return await FileExistsTool().execute({"path": path}, _CTX())


@tool_read_search
@tool()
async def search_files(pattern: str, path: str = ".", recursive: bool = True) -> dict[str, object]:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g. "*.py", "**/*.ts").
        path: Root directory to search (default: current directory).
        recursive: Search subdirectories recursively (default True).
    """
    from agenthicc.tools.fs import SearchFilesTool  # noqa: PLC0415

    return await SearchFilesTool().execute(
        {"pattern": pattern, "path": path, "recursive": recursive}, _CTX()
    )


@tool_read_search
@tool()
async def grep_files(pattern: str, path: str = ".", max_results: int = 50) -> dict[str, object]:
    """Search file contents for a regex pattern and return matching lines.

    Args:
        pattern: Regular expression to search for.
        path: Directory to search (default: current directory).
        max_results: Maximum number of matches to return (default 50).
    """
    from agenthicc.tools.fs import GrepFilesTool  # noqa: PLC0415

    return await GrepFilesTool().execute(
        {"pattern": pattern, "path": path, "recursive": True, "max_results": max_results},
        _CTX(),
    )


@tool_read
@tool()
async def get_file_info(path: str) -> dict[str, object]:
    """Return metadata for a file or directory (size, dates, permissions).

    Args:
        path: File or directory path.
    """
    from agenthicc.tools.fs import GetFileInfoTool  # noqa: PLC0415

    return await GetFileInfoTool().execute({"path": path}, _CTX())


@tool_read
@tool()
async def read_lines(path: str, start: int = 1, end: int | None = None) -> dict[str, object]:
    """Read a specific range of lines from a file (1-indexed).

    Args:
        path: File path to read.
        start: First line to read (1-indexed, default 1).
        end: Last line to read inclusive (default: end of file).
    """
    from agenthicc.tools.fs import ReadLinesTool  # noqa: PLC0415

    args: dict[str, object] = {"path": path, "start": start}
    if end is not None:
        args["end"] = end
    return await ReadLinesTool().execute(args, _CTX())


@tool_write
@tool()
async def patch_file(path: str, old_content: str, new_content: str) -> dict[str, object]:
    """Replace all occurrences of old_content with new_content in a file.

    Args:
        path: File path to patch.
        old_content: Exact string to find and replace.
        new_content: Replacement string.
    """
    from agenthicc.tools.fs import PatchFileTool  # noqa: PLC0415

    return await PatchFileTool().execute(
        {"path": path, "old_content": old_content, "new_content": new_content}, _CTX()
    )


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
    try:
        b = _get_backend(path)
        text = b.read_text(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error": "permission_denied"}

    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = re.compile(pattern, flags)
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
async def apply_diff(path: str, diff: str, allow_partial: bool = False) -> dict[str, object]:
    """Apply a unified diff to a file.

    Args:
        path: File path to patch (relative to workspace root).
        diff: Unified diff string (output of ``diff -u`` or similar).
        allow_partial: If True, apply successfully-matched hunks even when some fail (default False).
    """
    try:
        b = _get_backend(path)
        original = b.read_text(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error": "permission_denied"}

    hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
    file_lines = original.splitlines(keepends=True)

    # Split diff into hunk blocks
    hunk_matches = list(hunk_header.finditer(diff))
    if not hunk_matches:
        return {"ok": False, "error": "no hunks found in diff"}

    hunks = []
    for idx, match in enumerate(hunk_matches):
        start_pos = match.end()
        end_pos = hunk_matches[idx + 1].start() if idx + 1 < len(hunk_matches) else len(diff)
        hunk_body = diff[start_pos:end_pos]
        old_start = int(match.group(1)) - 1  # convert to 0-indexed
        hunks.append((old_start, hunk_body, idx + 1))

    result_lines = list(file_lines)
    offset = 0
    hunks_applied = 0
    hunks_failed = 0

    for old_start, hunk_body, hunk_num in hunks:
        hunk_lines = hunk_body.splitlines(keepends=True)
        old_lines = []
        new_lines = []
        for hl in hunk_lines:
            if hl.startswith("-"):
                old_lines.append(hl[1:])
            elif hl.startswith("+"):
                new_lines.append(hl[1:])
            elif hl.startswith(" ") or hl.startswith("\\ "):
                ctx = hl[1:] if not hl.startswith("\\ ") else ""
                old_lines.append(ctx)
                new_lines.append(ctx)

        apply_at = old_start + offset
        # Verify context matches
        file_slice = result_lines[apply_at : apply_at + len(old_lines)]
        if file_slice != old_lines:
            hunks_failed += 1
            if not allow_partial:
                return {
                    "ok": False,
                    "hunks_applied": 0,
                    "hunks_failed": hunks_failed,
                    "error": f"hunk {hunk_num} context mismatch",
                }
            continue
        result_lines[apply_at : apply_at + len(old_lines)] = new_lines
        offset += len(new_lines) - len(old_lines)
        hunks_applied += 1

    new_content = "".join(result_lines)
    b.write_text(path, new_content)
    return {
        "ok": True,
        "hunks_applied": hunks_applied,
        "hunks_failed": hunks_failed,
        "result": new_content,
    }


@tool_read
@tool()
async def checksum_file(path: str, algorithm: str = "sha256") -> dict[str, object]:
    """Compute a cryptographic checksum of a file.

    Args:
        path: File path to checksum (relative to workspace root).
        algorithm: Hash algorithm name accepted by ``hashlib`` (default ``sha256``).
    """
    try:
        b = _get_backend(path)
        data = b.read_bytes(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error": "permission_denied"}

    try:
        h = hashlib.new(algorithm, data)
    except ValueError:
        return {"ok": False, "error": f"unsupported algorithm: {algorithm}"}
    return {"ok": True, "path": path, "algorithm": algorithm, "digest": h.hexdigest()}


@tool_write
@tool()
async def truncate_file(path: str, size: int = 0) -> dict[str, object]:
    """Truncate a file to a given byte size.

    Args:
        path: File path to truncate (relative to workspace root).
        size: Target size in bytes (default 0 — empties the file).
    """
    try:
        b = _get_backend(path)
        b.truncate(path, size)
        new_size = b.stat(path).size
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error": "permission_denied"}
    return {"ok": True, "path": path, "new_size": new_size}


@tool_write
@tool()
async def touch_file(path: str, create: bool = True) -> dict[str, object]:
    """Create a file if it does not exist, or update its modification time if it does.

    Args:
        path: File path to touch (relative to workspace root).
        create: Create the file when it does not exist (default True).
                If False and the file is missing, returns an error.
    """
    try:
        b = _get_backend(path)
        existed = b.exists(path)
        if not existed and not create:
            return {"ok": False, "error": f"not found: {path}", "created": False}
        if not existed:
            b.write_text(path, "")
        else:
            b.append_text(path, "")
    except PermissionError:
        return {"ok": False, "error": "permission_denied"}
    return {"ok": True, "path": path, "created": not existed}


@tool_read
@tool()
async def batch_read(paths: list[str], encoding: str = "utf-8") -> dict[str, object]:
    """Read multiple files in a single call.

    Args:
        paths: List of file paths to read (relative to workspace root).
        encoding: Text encoding for all files (default utf-8).
    """
    b = _get_backend()
    results = b.batch_read(paths, encoding)
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
    b = _get_backend()
    backend_files: list[dict[str, object]] = [dict(item) for item in files]
    results = b.batch_write(backend_files, create_parents)
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
    b = _get_backend()
    results = b.batch_delete(paths)
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
    b = _get_backend()
    results = []
    all_ok = True
    for item in moves:
        src = arg_str(item, "source", "")
        dst = arg_str(item, "destination", "")
        try:
            b.move(src, dst)
            results.append({"source": src, "destination": dst, "ok": True})
        except Exception as exc:
            results.append({"source": src, "destination": dst, "ok": False, "error": str(exc)})
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
    b = _get_backend()
    results = []
    all_ok = True
    for item in copies:
        src = arg_str(item, "source", "")
        dst = arg_str(item, "destination", "")
        try:
            b.copy(src, dst)
            results.append({"source": src, "destination": dst, "ok": True})
        except Exception as exc:
            results.append({"source": src, "destination": dst, "ok": False, "error": str(exc)})
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
