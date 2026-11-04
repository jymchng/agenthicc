"""Git tools for structured git operations (PRD-15)."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Final

from agenthicc.tools.base import Tool, arg_bool, arg_int, arg_str
from agenthicc.tools.errors import ToolErrorCode, tool_error
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, current_workspace_access

__all__ = ["GIT_COMMIT_TIMEOUT_S", "GIT_TIMEOUT_S", "GitRun", "GitToolKit"]


async def _authorize_git(
    context: Mapping[str, object],
    tool_name: str,
    tool_input: Mapping[str, object],
    capabilities: frozenset[str] = frozenset(),
) -> str | None:
    """Run the direct git adapter through the session workspace policy."""

    error, _ = await _authorize_git_paths(context, tool_name, tool_input, capabilities)
    return error


async def _authorize_git_paths(
    context: Mapping[str, object],
    tool_name: str,
    tool_input: Mapping[str, object],
    capabilities: frozenset[str] = frozenset(),
) -> tuple[str | None, dict[tuple[str, str], str]]:
    """Authorize Git path arguments and return their canonical spellings.

    Git receives paths as command-line arguments, so authorizing a request and
    then passing the original relative/symlink spelling would leave a small
    but important gap between policy revalidation and the actual I/O.  Keep
    the public error-only helper above for older callers, while path-bearing
    adapters use this helper to execute the exact canonical target that was
    revalidated by the session policy.
    """

    policy = context.get("workspace_access")
    if not isinstance(policy, WorkspaceAccessPolicy):
        policy = current_workspace_access()
    if policy is None:
        return None, {}

    root = arg_str(context, "workspace_root", ".")
    authorized_input = dict(tool_input)
    # This private field lets the shared resolver authorize the repository
    # root even for Git commands whose model-facing schema has no path field.
    authorized_input["__workspace_root"] = root
    canonical: dict[tuple[str, str], str] = {}
    for requested, operation in policy.requests_for_tool(tool_name, authorized_input):
        try:
            resolved = await policy.authorize(
                requested,
                operation=operation,
                tool_name=tool_name,
                capabilities=capabilities,
                tool_input=authorized_input,
            )
        except (OSError, PermissionError, ValueError) as exc:
            policy.clear_current_grant(tool_name)
            return str(exc), {}
        canonical[(requested, operation)] = str(resolved)
    return None, canonical


@dataclass(frozen=True, slots=True)
class GitRun:
    """One finished git invocation: its exit code and bounded output."""

    returncode: int
    out: str
    err: str
    truncated: bool = False
    timed_out: bool = False


#: Read-only tools get this long; git_commit runs hooks and gets longer.
GIT_TIMEOUT_S: Final = 30.0
GIT_COMMIT_TIMEOUT_S: Final = 300.0


async def _run_git(
    git_root: str,
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_S,
    read_only: bool = True,
) -> GitRun:
    """Run git non-interactively with bounded output (TOOLR-03).

    No pager, colour, editor, credential prompt or shared stdin; a deadline
    kills the whole process group instead of escaping as ``TimeoutError``.
    """
    from agenthicc.tools.exec.child import StreamCapture, child_env

    env = child_env({"LC_ALL": "C", "GIT_EDITOR": "true", "GIT_ASKPASS": ""})
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    cmd = [
        "git",
        "-c",
        "core.pager=cat",
        "-c",
        "color.ui=never",
        "-c",
        "core.quotepath=off",
        "-C",
        git_root,
        *args,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return GitRun(-1, "", f"git could not start: {exc}")
    out_capture = StreamCapture()
    err_capture = StreamCapture()

    async def drain(stream: asyncio.StreamReader | None, capture: StreamCapture) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65_536):
            capture.feed(chunk)

    async def collect() -> None:
        await asyncio.gather(drain(proc.stdout, out_capture), drain(proc.stderr, err_capture))
        await proc.wait()

    collector = asyncio.ensure_future(collect())
    try:
        await asyncio.wait_for(asyncio.shield(collector), timeout=timeout)
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            await asyncio.wait_for(collector, timeout=2.0)
        except asyncio.TimeoutError:
            collector.cancel()
    except asyncio.CancelledError:
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
        collector.cancel()
        raise
    return GitRun(
        returncode=proc.returncode if proc.returncode is not None else -1,
        out=out_capture.render(),
        err=err_capture.render()[-4096:],
        truncated=out_capture.omitted > 0,
        timed_out=timed_out,
    )


def _failure(run: GitRun) -> dict[str, object]:
    """The TOOLR-04 envelope for a failed git run.

    ``error_code`` is from the shared vocabulary; ``reason`` names the git
    condition when there is a more specific one.
    """
    text = run.err.strip() or run.out.strip()
    lowered = text.lower()
    reason = ""
    if run.timed_out:
        code = ToolErrorCode.TIMEOUT
    elif "not a git repository" in lowered:
        code, reason = ToolErrorCode.NOT_FOUND, "not_a_repository"
    elif "conflict" in lowered or "would be overwritten" in lowered:
        code = ToolErrorCode.CONFLICT
    elif "nothing to commit" in lowered or "no changes added to commit" in lowered:
        code, reason = ToolErrorCode.INVALID_ARGUMENT, "nothing_to_commit"
    elif "hook" in lowered:
        code, reason = ToolErrorCode.PERMISSION_DENIED, "hook_rejected"
    elif "unknown revision" in lowered or "bad revision" in lowered or "invalid" in lowered:
        code = ToolErrorCode.INVALID_ARGUMENT
    else:
        code = ToolErrorCode.INTERNAL
    fields: dict[str, object] = {"reason": reason} if reason else {}
    return tool_error(
        code,
        text or f"git exited with {run.returncode}",
        retryable=code is ToolErrorCode.TIMEOUT,
        **fields,
    )


def _invalid(message: str) -> dict[str, object]:
    return {"ok": False, "error_code": "invalid_argument", "error": message, "retryable": False}


def _option_like(value: str) -> bool:
    """A ref or branch that git would parse as an option."""
    return value.startswith("-")


def _access_failure(error: str) -> dict[str, object]:
    return {
        "ok": False,
        "error_code": "permission_denied",
        "code": "workspace_access",
        "error": error,
    }


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show working tree status: branch, staged/unstaged/untracked files."
    parameters: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        run = await _run_git(root, ["status", "--porcelain=v2", "--branch", "-z"])
        if run.returncode != 0:
            return _failure(run)
        branch = ""
        upstream = ""
        ahead = behind = 0
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        conflicted: list[str] = []
        entries = run.out.split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if entry.startswith("# branch.head "):
                branch = entry.removeprefix("# branch.head ")
            elif entry.startswith("# branch.upstream "):
                upstream = entry.removeprefix("# branch.upstream ")
            elif entry.startswith("# branch.ab "):
                counts = entry.removeprefix("# branch.ab ").split()
                ahead = int(counts[0].lstrip("+")) if counts else 0
                behind = int(counts[1].lstrip("-")) if len(counts) > 1 else 0
            elif entry.startswith("? "):
                untracked.append(entry[2:])
            elif entry.startswith("u "):
                conflicted.append(entry.split(" ", 10)[-1])
            elif entry.startswith(("1 ", "2 ")):
                fields = entry.split(" ", 8 if entry[0] == "1" else 9)
                xy, path = fields[1], fields[-1]
                if entry[0] == "2":
                    index += 1  # the rename's original path follows
                if xy[0] != ".":
                    staged.append(path)
                if xy[1] != ".":
                    unstaged.append(path)
        return {
            "ok": True,
            "branch": branch,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "conflicted": conflicted,
            "clean": not (staged or unstaged or untracked or conflicted),
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
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        path = arg_str(args, "path", "")
        ref = arg_str(args, "ref", "")
        if ref and _option_like(ref):
            return _invalid(f"ref must not start with '-': {ref!r}")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        canonical_path = canonical_paths.get((path, "read"), path)
        base = ["diff"]
        if arg_bool(args, "staged", False):
            base.append("--cached")
        tail = (["--end-of-options", ref] if ref else []) + (["--", canonical_path] if path else [])
        numstat = await _run_git(root, [*base, "--numstat", "-z", *tail])
        if numstat.returncode not in (0, 1):
            return _failure(numstat)
        files: list[dict[str, object]] = []
        additions = deletions = 0
        records = numstat.out.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            added, removed, name = (record.split("\t", 2) + ["", ""])[:3]
            if not name:  # a rename: "<a>\t<d>\t" then old and new paths
                name = records[index + 1] if index + 1 < len(records) else ""
                index += 2
            binary = added == "-"
            files.append(
                {
                    "path": name,
                    "additions": 0 if binary else int(added or 0),
                    "deletions": 0 if binary else int(removed or 0),
                    "binary": binary,
                }
            )
            additions += 0 if binary else int(added or 0)
            deletions += 0 if binary else int(removed or 0)
        patch = await _run_git(root, [*base, "--patch", *tail])
        if patch.returncode not in (0, 1):
            return _failure(patch)
        result: dict[str, object] = {
            "ok": True,
            "diff": patch.out,
            "files": files,
            "files_changed": [str(item["path"]) for item in files],
            "additions": additions,
            "deletions": deletions,
        }
        if patch.truncated:
            result["truncated"] = True
        return result


class GitLogTool(Tool):
    name = "git_log"
    description = "Show commit history."
    parameters = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "default": 10, "description": "Number of commits"},
            "path": {"type": "string"},
        },
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        n = arg_int(args, "n", 10)
        if n < 1:
            return _invalid("n must be at least 1")
        path = arg_str(args, "path", "")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        canonical_path = canonical_paths.get((path, "read"), path)
        fmt = "%H\x1f%h\x1f%an\x1f%ai\x1f%s\x1e"
        cmd = ["log", f"--max-count={n}", f"--format={fmt}"]
        if path:
            cmd += ["--", canonical_path]
        run = await _run_git(root, cmd)
        if run.returncode != 0:
            return _failure(run)
        commits = []
        for record in run.out.split("\x1e"):
            parts = record.strip("\n").split("\x1f")
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
        return {"ok": True, "commits": commits}


class GitAddTool(Tool):
    name = "git_add"
    description = "Stage files for commit."
    parameters = {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        raw_paths = args.get("paths", [])
        if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
            return _invalid("paths must be a list of strings")
        paths = list(raw_paths)
        if not paths:
            return _invalid("paths must name at least one file")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        executable_paths = [canonical_paths.get((path, "write"), path) for path in paths]
        run = await _run_git(root, ["add", "--", *executable_paths], read_only=False)
        if run.returncode != 0:
            return {**_failure(run), "staged": []}
        return {"ok": True, "staged": paths}


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
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        message = arg_str(args, "message")
        author = arg_str(args, "author", "")
        if not message.strip():
            return _invalid("message must not be empty")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        before = await _run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
        head_before = before.out.strip() if before.returncode == 0 else ""
        cmd = ["commit", f"--message={message}"]
        if author:
            cmd.append(f"--author={author}")
        run = await _run_git(root, cmd, timeout=GIT_COMMIT_TIMEOUT_S, read_only=False)
        after = await _run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
        head_after = after.out.strip() if after.returncode == 0 else ""
        if run.returncode != 0 or run.timed_out:
            failure = _failure(run)
            failure["committed"] = bool(head_after) and head_after != head_before
            if failure["committed"]:
                failure["hash"] = head_after
            return failure
        return {
            "ok": True,
            "hash": head_after,
            "short_hash": head_after[:7],
            "message": message,
        }


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
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        branch = arg_str(args, "branch")
        if not branch or _option_like(branch):
            return _invalid(f"branch must be a branch name, not an option: {branch!r}")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        check = await _run_git(root, ["check-ref-format", "--branch", branch])
        if check.returncode != 0:
            return _invalid(f"not a valid branch name: {branch!r}")
        cmd = (
            ["switch", "--create", branch]
            if arg_bool(args, "create", False)
            else [
                "switch",
                branch,
            ]
        )
        run = await _run_git(root, cmd, read_only=False)
        if run.returncode != 0:
            return {**_failure(run), "branch": branch}
        return {"ok": True, "branch": branch}


class GitBranchTool(Tool):
    name = "git_branch"
    description = "List branches."
    parameters = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        pattern = arg_str(args, "pattern", "")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        run = await _run_git(
            root,
            [
                "for-each-ref",
                "--format=%(refname:short)\x1f%(HEAD)\x1f%(upstream:short)\x1f%(refname)",
                "refs/heads",
                "refs/remotes",
            ],
        )
        if run.returncode != 0:
            return _failure(run)
        branches = []
        for line in run.out.strip().splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4 or parts[3].endswith("/HEAD"):
                continue
            if pattern and not fnmatch.fnmatch(parts[0], pattern):
                continue
            branches.append(
                {
                    "name": parts[0],
                    "current": parts[1] == "*",
                    "remote": parts[2] or None,
                    "is_remote": parts[3].startswith("refs/remotes/"),
                }
            )
        return {"ok": True, "branches": branches}


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
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        path = arg_str(args, "path")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        canonical_path = canonical_paths.get((path, "read"), path)
        cmd = ["blame", "--line-porcelain"]
        start = arg_int(args, "start_line", 1)
        end_value = args.get("end_line")
        end = arg_int(args, "end_line") if end_value is not None else None
        if end is not None:
            cmd += [f"-L{start},{end}"]
        elif start > 1:
            cmd += [f"-L{start}"]
        cmd += ["--", canonical_path]
        run = await _run_git(root, cmd)
        if run.returncode != 0:
            return _failure(run)
        lines = []
        current: dict[str, object] = {}
        line_number = start
        for raw in run.out.splitlines():
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
        result: dict[str, object] = {"ok": True, "lines": lines}
        if run.truncated:
            result["truncated"] = True
        return result


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
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        pattern = arg_str(args, "pattern")
        ref = arg_str(args, "ref", "HEAD")
        if not pattern:
            return _invalid("pattern must not be empty")
        if _option_like(ref):
            return _invalid(f"ref must not start with '-': {ref!r}")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        run = await _run_git(root, ["grep", "--line-number", "-z", "-e", pattern, ref])
        if run.returncode not in (0, 1):
            return _failure(run)
        matches = []
        for line in run.out.splitlines():
            # -z separates ref:file, the line number and the text with NULs.
            parts = line.split("\0")
            if len(parts) < 3:
                continue
            name = parts[0].split(":", 1)[1] if parts[0].startswith(f"{ref}:") else parts[0]
            matches.append(
                {
                    "file": name,
                    "line_number": int(parts[1]) if parts[1].isdigit() else 0,
                    "line": parts[2],
                }
            )
        result: dict[str, object] = {"ok": True, "matches": matches, "count": len(matches)}
        if run.truncated:
            result["truncated"] = True
        return result


class GitShowTool(Tool):
    name = "git_show"
    description = "Show a commit's metadata and diff."
    parameters = {
        "type": "object",
        "properties": {"ref": {"type": "string", "default": "HEAD"}},
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        ref = arg_str(args, "ref", "HEAD")
        if not ref or _option_like(ref):
            return _invalid(f"ref must be a revision, not an option: {ref!r}")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        fmt = "%H\x1f%an\x1f%ai\x1f%B\x1e"
        run = await _run_git(root, ["show", f"--format={fmt}", "--end-of-options", ref])
        if run.returncode != 0:
            return _failure(run)
        header, separator, diff = run.out.partition("\x1e")
        parts = header.split("\x1f", 3)
        if len(parts) < 4 or not separator:
            return {"ok": False, "error_code": "internal", "error": "unparsable git show output"}
        result: dict[str, object] = {
            "ok": True,
            "hash": parts[0],
            "author": parts[1],
            "date": parts[2],
            "message": parts[3].strip(),
            "diff": diff.lstrip("\n"),
        }
        if run.truncated:
            result["truncated"] = True
        return result


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
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        root = arg_str(context, "workspace_root", ".")
        action = arg_str(args, "action", "push")
        if action not in {"push", "pop", "list", "drop"}:
            return _invalid("action must be push, pop, list or drop")
        message = arg_str(args, "message", "")
        access_error, canonical_paths = await _authorize_git_paths(context, self.name, args)
        if access_error is not None:
            return _access_failure(access_error)
        root = canonical_paths.get((root, "git_root"), root)
        cmd = ["stash", action]
        if action == "push" and message:
            cmd.append(f"--message={message}")
        run = await _run_git(root, cmd, read_only=action == "list")
        if run.returncode != 0:
            return _failure(run)
        m = re.search(r"stash@\{(\d+)\}", run.out)
        return {
            "ok": True,
            "stash_ref": m.group(0) if m else None,
            "output": run.out.strip(),
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
