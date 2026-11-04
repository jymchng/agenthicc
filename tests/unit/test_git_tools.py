"""Git tools against real repositories (PRD-15, TOOLR-03)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agenthicc.tools.git import (
    GitAddTool,
    GitBranchTool,
    GitCheckoutTool,
    GitCommitTool,
    GitDiffTool,
    GitGrepTool,
    GitLogTool,
    GitShowTool,
    GitStashTool,
    GitStatusTool,
    GitToolKit,
)
import agenthicc.tools.git as git_module

pytestmark = pytest.mark.unit


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, value in (
        ("GIT_AUTHOR_NAME", "T"),
        ("GIT_AUTHOR_EMAIL", "t@example.com"),
        ("GIT_COMMITTER_NAME", "T"),
        ("GIT_COMMITTER_EMAIL", "t@example.com"),
    ):
        monkeypatch.setenv(name, value)
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def _ctx(root: Path) -> dict[str, object]:
    return {"workspace_root": str(root)}


@pytest.mark.parametrize("branch", ["feature-x", "release-1.2"])
async def test_commit_reports_the_real_hash_on_any_branch_name(repo: Path, branch: str) -> None:
    _git(repo, "switch", "-q", "-c", branch)
    (repo / "a.txt").write_text("one\ntwo\nthree\n")
    _git(repo, "add", "a.txt")

    result = await GitCommitTool().execute({"message": "second"}, _ctx(repo))

    assert result["ok"] is True
    assert result["hash"] == _git(repo, "rev-parse", "HEAD").strip()


async def test_a_root_commit_reports_its_hash(tmp_path: Path, repo: Path) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-q", "-b", "main")
    (fresh / "f").write_text("x")
    _git(fresh, "add", "f")

    result = await GitCommitTool().execute({"message": "root"}, _ctx(fresh))

    assert result["ok"] is True and result["hash"] == _git(fresh, "rev-parse", "HEAD").strip()


async def test_a_slow_hook_commits_and_a_timeout_kills_git(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nsleep 3\n")
    hook.chmod(0o755)
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "add", "a.txt")

    monkeypatch.setattr(git_module, "GIT_COMMIT_TIMEOUT_S", 0.5)
    timed_out = await GitCommitTool().execute({"message": "slow"}, _ctx(repo))
    assert timed_out["ok"] is False and timed_out["error_code"] == "timeout"
    assert timed_out["committed"] is False
    leftover = subprocess.run(["pgrep", "-f", str(repo)], capture_output=True, text=True)
    assert leftover.stdout.strip() == ""

    monkeypatch.setattr(git_module, "GIT_COMMIT_TIMEOUT_S", 60.0)
    committed = await GitCommitTool().execute({"message": "slow"}, _ctx(repo))
    assert committed["ok"] is True


async def test_option_shaped_refs_and_branches_are_refused(repo: Path) -> None:
    target = repo.parent / "escaped.txt"
    show = await GitShowTool().execute({"ref": f"--output={target}"}, _ctx(repo))
    checkout = await GitCheckoutTool().execute({"branch": "--orphan=x"}, _ctx(repo))
    diff = await GitDiffTool().execute({"ref": "--no-index"}, _ctx(repo))

    for result in (show, checkout, diff):
        assert result["ok"] is False and result["error_code"] == "invalid_argument"
    assert not target.exists()


async def test_diff_counts_lines_not_file_headers(repo: Path) -> None:
    (repo / "a.txt").write_text("one\nTWO\n")

    result = await GitDiffTool().execute({}, _ctx(repo))

    assert result["ok"] is True
    assert result["additions"] == 1 and result["deletions"] == 1
    assert result["files"] == [{"path": "a.txt", "additions": 1, "deletions": 1, "binary": False}]
    assert "+++ b/a.txt" in result["diff"]


async def test_a_huge_diff_is_marked_truncated_with_full_totals(repo: Path) -> None:
    (repo / "a.txt").write_text("".join(f"line {n}\n" for n in range(20_000)))

    result = await GitDiffTool().execute({}, _ctx(repo))

    assert result["truncated"] is True
    assert result["additions"] == 20_000 and result["deletions"] == 2


async def test_status_reports_staged_unstaged_and_untracked(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    (repo / "c.txt").write_text("untracked\n")

    result = await GitStatusTool().execute({}, _ctx(repo))

    assert result["ok"] is True and result["branch"] == "main"
    assert result["staged"] == ["b.txt"]
    assert result["unstaged"] == ["a.txt"] and result["untracked"] == ["c.txt"]
    assert result["clean"] is False


async def test_log_branch_grep_add_show_and_stash(repo: Path) -> None:
    log = await GitLogTool().execute({"n": 5}, _ctx(repo))
    assert log["ok"] is True and log["commits"][0]["message"] == "first"

    branches = await GitBranchTool().execute({}, _ctx(repo))
    assert branches["branches"] == [
        {"name": "main", "current": True, "remote": None, "is_remote": False}
    ]

    grep = await GitGrepTool().execute({"pattern": "two"}, _ctx(repo))
    assert grep["matches"] == [{"file": "a.txt", "line_number": 2, "line": "two"}]

    (repo / "n.txt").write_text("n\n")
    added = await GitAddTool().execute({"paths": ["n.txt"]}, _ctx(repo))
    assert added == {"ok": True, "staged": ["n.txt"]}

    show = await GitShowTool().execute({"ref": "HEAD"}, _ctx(repo))
    assert show["ok"] is True and show["message"] == "first" and "a.txt" in show["diff"]

    stash = await GitStashTool().execute({"action": "push", "message": "wip"}, _ctx(repo))
    assert stash["ok"] is True
    listed = await GitStashTool().execute({"action": "list"}, _ctx(repo))
    assert "wip" in listed["output"]


async def test_switching_branches(repo: Path) -> None:
    created = await GitCheckoutTool().execute({"branch": "topic", "create": True}, _ctx(repo))
    back = await GitCheckoutTool().execute({"branch": "main"}, _ctx(repo))
    assert created["ok"] is True and back["ok"] is True
    assert _git(repo, "branch", "--show-current").strip() == "main"


async def test_git_never_prompts_or_reads_stdin(repo: Path) -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, b"should-not-be-read\n")
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        result = await GitStatusTool().execute({}, _ctx(repo))
    finally:
        os.dup2(saved, 0)
        for descriptor in (saved, read_end, write_end):
            os.close(descriptor)
    assert result["ok"] is True


async def test_outside_a_repository_is_classified(tmp_path: Path) -> None:
    result = await GitStatusTool().execute({}, _ctx(tmp_path))
    assert (
        result["ok"] is False
        and result["error_code"] == "not_found"
        and result["reason"] == "not_a_repository"
    )


async def test_nothing_to_commit_is_classified(repo: Path) -> None:
    result = await GitCommitTool().execute({"message": "empty"}, _ctx(repo))
    assert (
        result["ok"] is False
        and result["error_code"] == "invalid_argument"
        and result["reason"] == "nothing_to_commit"
    )


def test_the_toolkit_has_eleven_tools() -> None:
    tools = GitToolKit().tools()
    assert len(tools) == 11
    assert {"git_status", "git_blame"} <= {t.name for t in tools}
