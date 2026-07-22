"""Git tools for structured git operations (PRD-15)."""

from __future__ import annotations

import asyncio
import re
from agenthicc.tools.base import Tool

__all__ = ["GitToolKit"]

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB


async def _run_git(
    git_root: str, args: list[str], input_data: str | None = None
) -> tuple[int, str, str]:
    """Run `git -C git_root *args` and return (returncode, stdout, stderr)."""
    cmd = ["git", "-C", git_root, *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if input_data else None,
    )
    stdin_bytes = input_data.encode() if input_data else None
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=stdin_bytes),
        timeout=30.0,
    )
    out = stdout.decode(errors="replace")[:_MAX_OUTPUT_BYTES]
    err = stderr.decode(errors="replace")[:4096]
    return proc.returncode, out, err


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show working tree status: branch, staged/unstaged/untracked files."
    parameters: dict = {"type": "object", "properties": {}, "required": []}

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        rc, out, err = await _run_git(root, ["status", "--porcelain=v1", "-b"])
        if rc != 0:
            return {"ok": False, "error": err}
        lines = out.splitlines()
        branch = ""
        staged, unstaged, untracked = [], [], []
        for line in lines:
            if line.startswith("##"):
                parts = line[3:].split("...")
                branch = parts[0].strip()
            elif line.startswith("??"):
                untracked.append(line[3:])
            elif len(line) >= 2:
                if line[0] != " ":
                    staged.append(line[3:])
                if line[1] != " ":
                    unstaged.append(line[3:])
        return {
            "branch": branch,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "clean": not staged and not unstaged and not untracked,
        }


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show changes between working tree, index, and commits."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Limit diff to this file/dir"},
            "staged": {
                "type": "boolean",
                "description": "Show staged (--cached) diff",
                "default": False,
            },
            "ref": {"type": "string", "description": "Commit/branch to diff against"},
        },
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        cmd = ["diff"]
        if args.get("staged"):
            cmd.append("--cached")
        if args.get("ref"):
            cmd.append(args["ref"])
        cmd += ["--stat", "--patch"]
        if args.get("path"):
            cmd += ["--", args["path"]]
        rc, out, err = await _run_git(root, cmd)
        if rc not in (0, 1):  # git diff returns 1 when there are diffs
            return {"ok": False, "error": err}
        additions = out.count("\n+")
        deletions = out.count("\n-")
        files_changed = [line.split("|")[0].strip() for line in out.splitlines() if "|" in line]
        return {
            "diff": out,
            "files_changed": files_changed,
            "additions": additions,
            "deletions": deletions,
        }


class GitLogTool(Tool):
    name = "git_log"
    description = "Show commit history."
    parameters = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "default": 10, "description": "Number of commits"},
            "path": {"type": "string"},
            "format": {"type": "string", "default": "oneline"},
        },
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        n = int(args.get("n", 10))
        fmt = "%H\x1f%h\x1f%an\x1f%ai\x1f%s"
        cmd = ["log", f"-{n}", f"--format={fmt}"]
        if args.get("path"):
            cmd += ["--", args["path"]]
        rc, out, err = await _run_git(root, cmd)
        if rc != 0:
            return {"ok": False, "error": err}
        commits = []
        for line in out.strip().splitlines():
            parts = line.split("\x1f")
            if len(parts) == 5:
                commits.append(
                    {
                        "hash": parts[0],
                        "short_hash": parts[1],
                        "author": parts[2],
                        "date": parts[3],
                        "message": parts[4],
                    }
                )
        return {"commits": commits}


class GitAddTool(Tool):
    name = "git_add"
    description = "Stage files for commit."
    parameters = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        paths = args.get("paths", [])
        rc, out, err = await _run_git(root, ["add", "--", *paths])
        return {"ok": rc == 0, "staged": paths, "error": err if rc != 0 else None}


class GitCommitTool(Tool):
    name = "git_commit"
    description = "Create a commit from staged changes."
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "author": {"type": "string", "description": "Author in 'Name <email>' format"},
        },
        "required": ["message"],
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        cmd = ["commit", "-m", args["message"]]
        if args.get("author"):
            cmd += ["--author", args["author"]]
        rc, out, err = await _run_git(root, cmd)
        if rc != 0:
            return {"ok": False, "error": err}
        m = re.search(r"\[[\w/]+ ([0-9a-f]+)\]", out)
        commit_hash = m.group(1) if m else ""
        return {"ok": True, "hash": commit_hash, "message": args["message"]}


class GitCheckoutTool(Tool):
    name = "git_checkout"
    description = "Switch or create a branch."
    parameters = {
        "type": "object",
        "properties": {
            "branch": {"type": "string"},
            "create": {"type": "boolean", "default": False},
        },
        "required": ["branch"],
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        cmd = ["checkout"]
        if args.get("create"):
            cmd.append("-b")
        cmd.append(args["branch"])
        rc, out, err = await _run_git(root, cmd)
        return {"ok": rc == 0, "branch": args["branch"], "error": err if rc != 0 else None}


class GitBranchTool(Tool):
    name = "git_branch"
    description = "List branches."
    parameters = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        cmd = ["branch", "-a", "--format=%(refname:short)\x1f%(HEAD)\x1f%(upstream:short)"]
        if args.get("pattern"):
            cmd.append(args["pattern"])
        rc, out, err = await _run_git(root, cmd)
        if rc != 0:
            return {"ok": False, "error": err}
        branches = []
        for line in out.strip().splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 2:
                branches.append(
                    {
                        "name": parts[0],
                        "current": parts[1] == "*",
                        "remote": parts[2] if len(parts) > 2 else None,
                    }
                )
        return {"branches": branches}


class GitBlameTool(Tool):
    name = "git_blame"
    description = "Show line-by-line authorship of a file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "default": 1},
            "end_line": {"type": "integer"},
        },
        "required": ["path"],
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        cmd = ["blame", "--line-porcelain"]
        start = int(args.get("start_line", 1))
        end = args.get("end_line")
        if end:
            cmd += [f"-L{start},{int(end)}"]
        elif start > 1:
            cmd += [f"-L{start}"]
        cmd += ["--", args["path"]]
        rc, out, err = await _run_git(root, cmd)
        if rc != 0:
            return {"ok": False, "error": err}
        lines = []
        current: dict = {}
        line_number = start
        for raw in out.splitlines():
            if raw.startswith("\t"):
                current["content"] = raw[1:]
                current["line_number"] = line_number
                lines.append(current)
                current = {}
                line_number += 1
            elif " " in raw:
                key, _, value = raw.partition(" ")
                if len(key) == 40:
                    current["hash"] = key
                elif key == "author":
                    current["author"] = value
                elif key == "author-time":
                    current["date"] = value
        return {"lines": lines}


class GitGrepTool(Tool):
    name = "git_grep"
    description = "Search for a pattern in tracked files."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "ref": {"type": "string", "default": "HEAD"},
        },
        "required": ["pattern"],
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        cmd = ["grep", "-n", "--line-number", args["pattern"], args.get("ref", "HEAD")]
        rc, out, err = await _run_git(root, cmd)
        if rc not in (0, 1):
            return {"ok": False, "error": err}
        matches = []
        for line in out.strip().splitlines():
            # format: ref:file:line_number:content
            parts = line.split(":", 3)
            if len(parts) >= 4:
                matches.append(
                    {
                        "file": parts[1],
                        "line_number": int(parts[2]) if parts[2].isdigit() else 0,
                        "line": parts[3],
                    }
                )
            elif len(parts) == 3:
                matches.append(
                    {
                        "file": parts[0],
                        "line_number": int(parts[1]) if parts[1].isdigit() else 0,
                        "line": parts[2],
                    }
                )
        return {"matches": matches, "count": len(matches)}


class GitShowTool(Tool):
    name = "git_show"
    description = "Show a commit's metadata and diff."
    parameters = {
        "type": "object",
        "properties": {"ref": {"type": "string", "default": "HEAD"}},
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        ref = args.get("ref", "HEAD")
        fmt = "%H\x1f%an\x1f%ai\x1f%B"
        rc, out, err = await _run_git(root, ["show", f"--format={fmt}", ref])
        if rc != 0:
            return {"ok": False, "error": err}
        parts = out.split("\x1f", 3)
        if len(parts) < 4:
            return {"ok": False, "error": "Could not parse git show output"}
        diff_start = parts[3].find("\ndiff ")
        message = parts[3][:diff_start].strip() if diff_start > 0 else parts[3].strip()
        diff = parts[3][diff_start:] if diff_start > 0 else ""
        return {
            "hash": parts[0],
            "author": parts[1],
            "date": parts[2],
            "message": message,
            "diff": diff,
        }


class GitStashTool(Tool):
    name = "git_stash"
    description = "Push or pop the stash."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["push", "pop", "list", "drop"],
                "default": "push",
            },
            "message": {"type": "string"},
        },
    }

    async def execute(
        self, args: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        root = context.get("workspace_root", ".")
        action = args.get("action", "push")
        cmd = ["stash", action]
        if action == "push" and args.get("message"):
            cmd += ["-m", args["message"]]
        rc, out, err = await _run_git(root, cmd)
        m = re.search(r"stash@\{(\d+)\}", out)
        return {
            "ok": rc == 0,
            "stash_ref": m.group(0) if m else None,
            "output": out.strip(),
            "error": err if rc != 0 else None,
        }


class GitToolKit:
    """Factory that returns all git tools pre-configured for a workspace."""

    def __init__(self, git_root: str = ".") -> None:
        self._git_root = git_root

    def tools(self) -> list[Tool]:
        return [
            GitStatusTool(),
            GitDiffTool(),
            GitLogTool(),
            GitShowTool(),
            GitAddTool(),
            GitCommitTool(),
            GitCheckoutTool(),
            GitBranchTool(),
            GitStashTool(),
            GitBlameTool(),
            GitGrepTool(),
        ]
