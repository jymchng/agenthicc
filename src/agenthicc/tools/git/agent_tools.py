"""@tool() wrappers for every git tool — for use with lauren-ai AgentRunnerBase.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""
import os
from lauren_ai._tools import tool

__all__ = [
    "git_add",
    "git_blame",
    "git_branch",
    "git_checkout",
    "git_commit",
    "git_diff",
    "git_grep",
    "git_log",
    "git_show",
    "git_stash",
    "git_status",
    "GIT_AGENT_TOOLS",
]

_CTX = lambda: {"workspace_root": os.getcwd()}  # noqa: E731


@tool()
async def git_status() -> dict:
    """Show working tree status: branch, staged, unstaged, and untracked files."""
    from agenthicc.tools.git import GitStatusTool  # noqa: PLC0415
    return await GitStatusTool().execute({}, _CTX())


@tool()
async def git_diff(path: str | None = None, staged: bool = False, ref: str | None = None) -> dict:
    """Show changes between the working tree, index, and commits.

    Args:
        path: Limit diff to this file or directory (optional).
        staged: Show staged (--cached) diff instead of unstaged.
        ref: Commit or branch to diff against (optional).
    """
    from agenthicc.tools.git import GitDiffTool  # noqa: PLC0415
    args: dict = {"staged": staged}
    if path:
        args["path"] = path
    if ref:
        args["ref"] = ref
    return await GitDiffTool().execute(args, _CTX())


@tool()
async def git_log(n: int = 10, path: str | None = None) -> dict:
    """Show recent commit history.

    Args:
        n: Number of commits to return (default 10).
        path: Limit log to commits that touch this file (optional).
    """
    from agenthicc.tools.git import GitLogTool  # noqa: PLC0415
    args: dict = {"n": n}
    if path:
        args["path"] = path
    return await GitLogTool().execute(args, _CTX())


@tool()
async def git_show(ref: str = "HEAD") -> dict:
    """Show a commit's metadata and diff.

    Args:
        ref: Commit hash, branch, or tag to show (default HEAD).
    """
    from agenthicc.tools.git import GitShowTool  # noqa: PLC0415
    return await GitShowTool().execute({"ref": ref}, _CTX())


@tool()
async def git_add(paths: list[str]) -> dict:
    """Stage files for the next commit.

    Args:
        paths: List of file paths to stage.
    """
    from agenthicc.tools.git import GitAddTool  # noqa: PLC0415
    return await GitAddTool().execute({"paths": paths}, _CTX())


@tool()
async def git_commit(message: str, author: str | None = None) -> dict:
    """Create a commit from currently staged changes.

    Args:
        message: Commit message.
        author: Optional author string in "Name <email>" format.
    """
    from agenthicc.tools.git import GitCommitTool  # noqa: PLC0415
    args: dict = {"message": message}
    if author:
        args["author"] = author
    return await GitCommitTool().execute(args, _CTX())


@tool()
async def git_checkout(branch: str, create: bool = False) -> dict:
    """Switch to a branch, optionally creating it.

    Args:
        branch: Branch name to switch to.
        create: If True, create the branch before switching (-b).
    """
    from agenthicc.tools.git import GitCheckoutTool  # noqa: PLC0415
    return await GitCheckoutTool().execute({"branch": branch, "create": create}, _CTX())


@tool()
async def git_branch(pattern: str | None = None) -> dict:
    """List branches in the repository.

    Args:
        pattern: Optional pattern to filter branch names.
    """
    from agenthicc.tools.git import GitBranchTool  # noqa: PLC0415
    args: dict = {}
    if pattern:
        args["pattern"] = pattern
    return await GitBranchTool().execute(args, _CTX())


@tool()
async def git_stash(action: str = "push", message: str | None = None) -> dict:
    """Save or restore the current working state.

    Args:
        action: One of "push" (save), "pop" (restore), "list", or "drop".
        message: Optional stash message (only for action="push").
    """
    from agenthicc.tools.git import GitStashTool  # noqa: PLC0415
    args: dict = {"action": action}
    if message:
        args["message"] = message
    return await GitStashTool().execute(args, _CTX())


@tool()
async def git_blame(path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    """Show line-by-line authorship of a file.

    Args:
        path: File path to annotate.
        start_line: First line to show (1-indexed, default 1).
        end_line: Last line to show inclusive (default: end of file).
    """
    from agenthicc.tools.git import GitBlameTool  # noqa: PLC0415
    args: dict = {"path": path, "start_line": start_line}
    if end_line is not None:
        args["end_line"] = end_line
    return await GitBlameTool().execute(args, _CTX())


@tool()
async def git_grep(pattern: str, ref: str = "HEAD") -> dict:
    """Search for a pattern in files tracked by git.

    Args:
        pattern: Regular expression or string to search for.
        ref: Git reference (commit/branch/tag) to search in (default HEAD).
    """
    from agenthicc.tools.git import GitGrepTool  # noqa: PLC0415
    return await GitGrepTool().execute({"pattern": pattern, "ref": ref}, _CTX())


#: All 11 git agent tools — ready to pass to @use_tools().
GIT_AGENT_TOOLS = [
    git_status, git_diff, git_log, git_show,
    git_add, git_commit, git_checkout, git_branch,
    git_stash, git_blame, git_grep,
]
