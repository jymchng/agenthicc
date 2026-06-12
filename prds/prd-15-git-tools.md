---
title: "PRD-15: Git Tools — Structured Git Operations for Agents"
status: draft
version: 0.1.0
created: 2025-01-01
---

# PRD-15: Git Tools

## Executive Summary

Agents working on software engineering tasks need reliable, structured access to git operations. Raw `run_bash("git ...")` calls are fragile and hard to audit; this PRD specifies a `GitToolKit` — 11 tools in `tools/git/` that wrap git commands via `asyncio.create_subprocess_exec`, parse their output into structured dicts, and integrate with the same permission/hook/sandbox pipeline as all other tools. Every git write operation (commit, checkout, stash push) requires explicit `allow` permission so teams can safely run read-only agent sessions. Tools run `git -C {git_root} …` so the working directory is always explicit.

---

## Goals

| ID | Goal |
|----|------|
| G1 | All 11 git operations available as structured Tool subclasses |
| G2 | Non-zero exit codes become `ToolResultEnvelope(ok=False, error=stderr)` — never exceptions |
| G3 | Write ops (`commit`, `checkout`, `stash`, `add`) behind `git:write:allow` permission |
| G4 | All commands run `git -C {git_root}` for explicit working directory |
| G5 | `asyncio.create_subprocess_exec` (not blocking subprocess.run) |
| G6 | Output truncated to `max_output_kb` to prevent memory issues on large diffs |

## Non-Goals
- `git push` / `git pull` / remote operations (network-gated ops deferred to v2)
- Merge conflict resolution tools (too complex for v1)

---

## Tool Catalog

| Tool | Key Parameters | Return Shape |
|------|---------------|--------------|
| `git_status` | — | `{branch, staged, unstaged, untracked, clean, ahead, behind}` |
| `git_diff` | `path?, staged?, ref?` | `{diff, files_changed, additions, deletions}` |
| `git_log` | `n=10, path?, format="oneline"` | `{commits: [{hash, short_hash, author, date, message}]}` |
| `git_show` | `ref="HEAD"` | `{hash, author, date, message, diff}` |
| `git_add` | `paths: list[str]` | `{ok, staged}` |
| `git_commit` | `message, author?` | `{ok, hash, message}` |
| `git_checkout` | `branch, create=False` | `{ok, branch}` |
| `git_branch` | `pattern?` | `{branches: [{name, current, remote}]}` |
| `git_stash` | `action="push", message?` | `{ok, stash_ref?}` |
| `git_blame` | `path, start_line=1, end_line?` | `{lines: [{line_number, hash, author, date, content}]}` |
| `git_grep` | `pattern, ref="HEAD"` | `{matches: [{file, line_number, line}], count}` |

---

## Data Structures and Interfaces

```python
# src/agenthicc/tools/git/__init__.py
from __future__ import annotations
import asyncio
import shlex
from typing import Any
from agenthicc.tools.base import Tool

__all__ = ["GitToolKit"]

_MAX_OUTPUT_BYTES = 64 * 1024   # 64 KB


async def _run_git(git_root: str, args: list[str], input_data: str | None = None) -> tuple[int, str, str]:
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

    async def execute(self, args: dict, context: dict) -> Any:
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
            "staged": {"type": "boolean", "description": "Show staged (--cached) diff", "default": False},
            "ref": {"type": "string", "description": "Commit/branch to diff against"},
        },
    }

    async def execute(self, args: dict, context: dict) -> Any:
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
        if rc not in (0, 1):   # git diff returns 1 when there are diffs
            return {"ok": False, "error": err}
        # Parse stat summary
        additions = out.count("\n+")
        deletions = out.count("\n-")
        files_changed = [l.split("|")[0].strip() for l in out.splitlines() if "|" in l]
        return {"diff": out, "files_changed": files_changed,
                "additions": additions, "deletions": deletions}


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

    async def execute(self, args: dict, context: dict) -> Any:
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
                commits.append({"hash": parts[0], "short_hash": parts[1],
                                 "author": parts[2], "date": parts[3], "message": parts[4]})
        return {"commits": commits}


class GitAddTool(Tool):
    name = "git_add"
    description = "Stage files for commit."
    parameters = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
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

    async def execute(self, args: dict, context: dict) -> Any:
        root = context.get("workspace_root", ".")
        cmd = ["commit", "-m", args["message"]]
        if args.get("author"):
            cmd += ["--author", args["author"]]
        rc, out, err = await _run_git(root, cmd)
        if rc != 0:
            return {"ok": False, "error": err}
        # Parse hash from output: "[branch abc1234] message"
        import re
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

    async def execute(self, args: dict, context: dict) -> Any:
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

    async def execute(self, args: dict, context: dict) -> Any:
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
                branches.append({
                    "name": parts[0],
                    "current": parts[1] == "*",
                    "remote": parts[2] if len(parts) > 2 else None,
                })
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

    async def execute(self, args: dict, context: dict) -> Any:
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

    async def execute(self, args: dict, context: dict) -> Any:
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
                matches.append({"file": parts[1], "line_number": int(parts[2]) if parts[2].isdigit() else 0, "line": parts[3]})
            elif len(parts) == 3:
                matches.append({"file": parts[0], "line_number": int(parts[1]) if parts[1].isdigit() else 0, "line": parts[2]})
        return {"matches": matches, "count": len(matches)}


class GitShowTool(Tool):
    name = "git_show"
    description = "Show a commit's metadata and diff."
    parameters = {
        "type": "object",
        "properties": {"ref": {"type": "string", "default": "HEAD"}},
    }

    async def execute(self, args: dict, context: dict) -> Any:
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
        return {"hash": parts[0], "author": parts[1], "date": parts[2],
                "message": message, "diff": diff}


class GitStashTool(Tool):
    name = "git_stash"
    description = "Push or pop the stash."
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["push", "pop", "list", "drop"], "default": "push"},
            "message": {"type": "string"},
        },
    }

    async def execute(self, args: dict, context: dict) -> Any:
        root = context.get("workspace_root", ".")
        action = args.get("action", "push")
        cmd = ["stash", action]
        if action == "push" and args.get("message"):
            cmd += ["-m", args["message"]]
        rc, out, err = await _run_git(root, cmd)
        import re
        m = re.search(r"stash@\{(\d+)\}", out)
        return {"ok": rc == 0, "stash_ref": m.group(0) if m else None,
                "output": out.strip(), "error": err if rc != 0 else None}


class GitToolKit:
    """Factory that returns all git tools pre-configured for a workspace."""

    def __init__(self, git_root: str = ".") -> None:
        self._git_root = git_root

    def tools(self) -> list[Tool]:
        return [
            GitStatusTool(), GitDiffTool(), GitLogTool(), GitShowTool(),
            GitAddTool(), GitCommitTool(), GitCheckoutTool(), GitBranchTool(),
            GitStashTool(), GitBlameTool(), GitGrepTool(),
        ]
```

---

## Configuration Reference

```toml
[tools.git]
git_root = "."
allow_commit = false     # set true to allow git_commit
allow_checkout = false   # set true to allow git_checkout
allow_stash_write = true
git_executable = "git"
max_output_kb = 64
```

Permission rules for SecurityPolicy (auto-generated from config):
```toml
[[security.permission_rules]]
tool_pattern = "git_add"
action = "allow"

[[security.permission_rules]]
tool_pattern = "git_commit"
action = "deny"       # overridden by allow_commit = true

[[security.permission_rules]]
tool_pattern = "git_*"
action = "allow"
```

---

## Tests

```python
# tests/unit/test_git_tools.py
"""Unit tests for git tools (PRD-15)."""
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agenthicc.tools.git import GitToolKit, GitStatusTool, GitLogTool, GitCommitTool

pytestmark = pytest.mark.unit


def _mock_git(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    return proc


@pytest.fixture
def mock_git():
    with patch("agenthicc.tools.git._run_git") as m:
        yield m


class TestGitStatusTool:
    async def test_clean_repo(self, mock_git):
        mock_git.return_value = (0, "## main...origin/main\n", "")
        result = await GitStatusTool().execute({}, {"workspace_root": "."})
        assert result["clean"] is True
        assert result["branch"] == "main"

    async def test_with_staged_files(self, mock_git):
        mock_git.return_value = (0, "## main\nM  src/foo.py\n", "")
        result = await GitStatusTool().execute({}, {"workspace_root": "."})
        assert result["clean"] is False
        assert "src/foo.py" in result["staged"]

    async def test_git_error(self, mock_git):
        mock_git.return_value = (128, "", "not a git repo")
        result = await GitStatusTool().execute({}, {"workspace_root": "."})
        assert result.get("ok") is False

    async def test_untracked_files(self, mock_git):
        mock_git.return_value = (0, "## main\n?? newfile.py\n", "")
        result = await GitStatusTool().execute({}, {})
        assert "newfile.py" in result["untracked"]


class TestGitLogTool:
    async def test_parses_commits(self, mock_git):
        log_output = "abc1234\x1fabc\x1fAlice\x1f2025-01-01\x1fFix bug\n"
        mock_git.return_value = (0, log_output, "")
        result = await GitLogTool().execute({"n": 5}, {})
        assert len(result["commits"]) == 1
        assert result["commits"][0]["author"] == "Alice"

    async def test_n_parameter_forwarded(self, mock_git):
        mock_git.return_value = (0, "", "")
        await GitLogTool().execute({"n": 3}, {})
        call_args = mock_git.call_args[0]
        assert "-3" in call_args[1]


class TestGitCommitTool:
    async def test_success(self, mock_git):
        mock_git.return_value = (0, "[main abc1234] Fix auth\n", "")
        result = await GitCommitTool().execute({"message": "Fix auth"}, {})
        assert result["ok"] is True
        assert result["hash"] == "abc1234"

    async def test_nothing_to_commit(self, mock_git):
        mock_git.return_value = (1, "", "nothing to commit")
        result = await GitCommitTool().execute({"message": "empty"}, {})
        assert result["ok"] is False


class TestGitToolKit:
    def test_returns_all_11_tools(self):
        kit = GitToolKit()
        tools = kit.tools()
        assert len(tools) == 11
        names = {t.name for t in tools}
        assert "git_status" in names
        assert "git_blame" in names
```

---

## Open Questions

1. **`git push`/`git pull`**: network operations add latency and need credential management. Defer to v2 with explicit `PUSH_ALLOWED` config.
2. **Merge conflicts**: `git_merge` could emit a structured conflict list. Complex; v2.
3. **Worktrees**: agents running in parallel might benefit from `git worktree` isolation. PRD-10 session continuity already uses separate log files; worktrees would need workspace view changes.
