"""Built-in @tool() functions available to every agenthicc agent session.

These wrap the existing Tool-ABC implementations so lauren-ai's AgentRunnerBase
can discover and call them. The workspace_root is bound at import time from the
process working directory; tools that need the actual project root pass it via
context.
"""
from __future__ import annotations

import os

from lauren_ai._tools import tool

__all__ = [
    "list_files",
    "read_file",
    "write_file",
    "run_bash",
    "run_command",
    "run_python",
    "git_status",
    "git_diff",
    "git_log",
    "search_files",
    "grep_files",
    "AGENT_TOOLS",
]

# ── filesystem ────────────────────────────────────────────────────────────


@tool()
async def list_files(path: str = ".") -> dict:
    """List files and directories at a path.

    Args:
        path: Directory to list (default: current directory).
    """
    from agenthicc.tools.fs import ListDirectoryTool  # noqa: PLC0415
    return await ListDirectoryTool().execute(
        {"path": path, "recursive": False}, {"workspace_root": os.getcwd()}
    )


@tool()
async def read_file(path: str) -> dict:
    """Read the contents of a file.

    Args:
        path: File path to read.
    """
    from agenthicc.tools.fs import ReadFileTool  # noqa: PLC0415
    return await ReadFileTool().execute({"path": path}, {"workspace_root": os.getcwd()})


@tool()
async def write_file(path: str, content: str) -> dict:
    """Write content to a file (creates parent dirs if needed).

    Args:
        path: Destination file path.
        content: Text content to write.
    """
    from agenthicc.tools.fs import WriteFileTool  # noqa: PLC0415
    return await WriteFileTool().execute(
        {"path": path, "content": content}, {"workspace_root": os.getcwd()}
    )


@tool()
async def search_files(pattern: str, path: str = ".") -> dict:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g. "*.py", "src/**/*.ts").
        path: Root directory to search from (default: current directory).
    """
    from agenthicc.tools.fs import SearchFilesTool  # noqa: PLC0415
    return await SearchFilesTool().execute(
        {"pattern": pattern, "path": path, "recursive": True},
        {"workspace_root": os.getcwd()},
    )


@tool()
async def grep_files(pattern: str, path: str = ".") -> dict:
    """Search file contents for a regex pattern.

    Args:
        pattern: Regular expression to search for.
        path: Directory to search (default: current directory).
    """
    from agenthicc.tools.fs import GrepFilesTool  # noqa: PLC0415
    return await GrepFilesTool().execute(
        {"pattern": pattern, "path": path, "recursive": True, "max_results": 50},
        {"workspace_root": os.getcwd()},
    )


# ── shell / execution ─────────────────────────────────────────────────────


@tool()
async def run_bash(command: str, timeout: float = 30.0) -> dict:
    """Execute a bash shell command.

    Args:
        command: Shell command string to execute.
        timeout: Maximum seconds to wait (default 30).
    """
    from agenthicc.tools.exec import RunBashTool  # noqa: PLC0415
    return await RunBashTool().execute(
        {"command": command, "timeout": timeout}, {"workspace_root": os.getcwd()}
    )


@tool()
async def run_command(argv: list[str], timeout: float = 30.0) -> dict:
    """Execute an executable directly (no shell).

    Args:
        argv: Command and arguments as a list, e.g. ["python", "-c", "print(1)"].
        timeout: Maximum seconds to wait (default 30).
    """
    from agenthicc.tools.exec import RunCommandTool  # noqa: PLC0415
    return await RunCommandTool().execute(
        {"argv": argv, "timeout": timeout}, {"workspace_root": os.getcwd()}
    )


@tool()
async def run_python(code: str, timeout: float = 30.0) -> dict:
    """Execute a Python code snippet in a subprocess.

    Args:
        code: Python source code to execute.
        timeout: Maximum seconds to wait (default 30).
    """
    from agenthicc.tools.exec import RunPythonTool  # noqa: PLC0415
    return await RunPythonTool().execute(
        {"code": code, "timeout": timeout}, {"workspace_root": os.getcwd()}
    )


# ── git ───────────────────────────────────────────────────────────────────


@tool()
async def git_status() -> dict:
    """Show working tree status: current branch, staged/unstaged/untracked files."""
    from agenthicc.tools.git import GitStatusTool  # noqa: PLC0415
    return await GitStatusTool().execute({}, {"workspace_root": os.getcwd()})


@tool()
async def git_diff(path: str | None = None, staged: bool = False) -> dict:
    """Show changes between working tree, index, and commits.

    Args:
        path: Limit diff to this file or directory (optional).
        staged: Show staged (--cached) diff instead of unstaged.
    """
    from agenthicc.tools.git import GitDiffTool  # noqa: PLC0415
    args: dict = {"staged": staged}
    if path:
        args["path"] = path
    return await GitDiffTool().execute(args, {"workspace_root": os.getcwd()})


@tool()
async def git_log(n: int = 10) -> dict:
    """Show recent commit history.

    Args:
        n: Number of commits to show (default 10).
    """
    from agenthicc.tools.git import GitLogTool  # noqa: PLC0415
    return await GitLogTool().execute({"n": n}, {"workspace_root": os.getcwd()})


# ── registry ──────────────────────────────────────────────────────────────

#: All tools exposed to agents by default.
AGENT_TOOLS = [
    list_files,
    read_file,
    write_file,
    search_files,
    grep_files,
    run_bash,
    run_command,
    run_python,
    git_status,
    git_diff,
    git_log,
]
