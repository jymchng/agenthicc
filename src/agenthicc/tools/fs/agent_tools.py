"""@tool() wrappers for every filesystem tool — for use with lauren-ai AgentRunnerBase.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""
import os
from lauren_ai._tools import tool

__all__ = [
    "append_file",
    "copy_file",
    "delete_file",
    "file_exists",
    "get_file_info",
    "grep_files",
    "list_directory",
    "make_directory",
    "move_file",
    "patch_file",
    "read_file",
    "read_lines",
    "search_files",
    "write_file",
    "FS_AGENT_TOOLS",
]

_CTX = lambda: {"workspace_root": os.getcwd()}  # noqa: E731


@tool()
async def read_file(path: str, encoding: str = "utf-8") -> dict:
    """Read the full contents of a file.

    Args:
        path: File path to read (relative to workspace root).
        encoding: Text encoding (default utf-8).
    """
    from agenthicc.tools.fs import ReadFileTool  # noqa: PLC0415
    return await ReadFileTool().execute({"path": path, "encoding": encoding}, _CTX())


@tool()
async def write_file(path: str, content: str, create_parents: bool = True) -> dict:
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


@tool()
async def append_file(path: str, content: str) -> dict:
    """Append text to the end of an existing file.

    Args:
        path: File path to append to.
        content: Text to append.
    """
    from agenthicc.tools.fs import AppendFileTool  # noqa: PLC0415
    return await AppendFileTool().execute({"path": path, "content": content}, _CTX())


@tool()
async def delete_file(path: str) -> dict:
    """Delete a file from the workspace.

    Args:
        path: File path to delete.
    """
    from agenthicc.tools.fs import DeleteFileTool  # noqa: PLC0415
    return await DeleteFileTool().execute({"path": path}, _CTX())


@tool()
async def move_file(source: str, destination: str) -> dict:
    """Move or rename a file within the workspace.

    Args:
        source: Source file path.
        destination: Destination file path.
    """
    from agenthicc.tools.fs import MoveFileTool  # noqa: PLC0415
    return await MoveFileTool().execute({"source": source, "destination": destination}, _CTX())


@tool()
async def copy_file(source: str, destination: str) -> dict:
    """Copy a file to a new location within the workspace.

    Args:
        source: Source file path.
        destination: Destination file path.
    """
    from agenthicc.tools.fs import CopyFileTool  # noqa: PLC0415
    return await CopyFileTool().execute({"source": source, "destination": destination}, _CTX())


@tool()
async def list_directory(path: str = ".", pattern: str = "*", recursive: bool = False) -> dict:
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


@tool()
async def make_directory(path: str) -> dict:
    """Create a directory (and all missing parent directories).

    Args:
        path: Directory path to create.
    """
    from agenthicc.tools.fs import MakeDirectoryTool  # noqa: PLC0415
    return await MakeDirectoryTool().execute({"path": path}, _CTX())


@tool()
async def file_exists(path: str) -> dict:
    """Check whether a file or directory exists.

    Args:
        path: Path to check.
    """
    from agenthicc.tools.fs import FileExistsTool  # noqa: PLC0415
    return await FileExistsTool().execute({"path": path}, _CTX())


@tool()
async def search_files(pattern: str, path: str = ".", recursive: bool = True) -> dict:
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


@tool()
async def grep_files(pattern: str, path: str = ".", max_results: int = 50) -> dict:
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


@tool()
async def get_file_info(path: str) -> dict:
    """Return metadata for a file or directory (size, dates, permissions).

    Args:
        path: File or directory path.
    """
    from agenthicc.tools.fs import GetFileInfoTool  # noqa: PLC0415
    return await GetFileInfoTool().execute({"path": path}, _CTX())


@tool()
async def read_lines(path: str, start: int = 1, end: int | None = None) -> dict:
    """Read a specific range of lines from a file (1-indexed).

    Args:
        path: File path to read.
        start: First line to read (1-indexed, default 1).
        end: Last line to read inclusive (default: end of file).
    """
    from agenthicc.tools.fs import ReadLinesTool  # noqa: PLC0415
    args: dict = {"path": path, "start": start}
    if end is not None:
        args["end"] = end
    return await ReadLinesTool().execute(args, _CTX())


@tool()
async def patch_file(path: str, old_content: str, new_content: str) -> dict:
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


#: All 14 fs agent tools — ready to pass to @use_tools().
FS_AGENT_TOOLS = [
    read_file, write_file, append_file, delete_file,
    move_file, copy_file, list_directory, make_directory,
    file_exists, search_files, grep_files, get_file_info,
    read_lines, patch_file,
]
