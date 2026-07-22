"""Integration tests for git tools with a real git repository (PRD-15)."""

from __future__ import annotations
import subprocess
import pytest
from agenthicc.tools.git import (
    GitStatusTool,
    GitLogTool,
    GitAddTool,
    GitCommitTool,
    GitDiffTool,
    GitBranchTool,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True, capture_output=True
    )
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True
    )
    return repo


def ctx(repo):
    return {"workspace_root": str(repo)}


async def test_status_clean(git_repo):
    r = await GitStatusTool().execute({}, ctx(git_repo))
    assert r["clean"] is True


async def test_status_with_untracked(git_repo):
    (git_repo / "new.py").write_text("x")
    r = await GitStatusTool().execute({}, ctx(git_repo))
    assert not r["clean"]
    assert any("new.py" in f for f in r["untracked"])


async def test_add_then_staged(git_repo):
    (git_repo / "new.py").write_text("x")
    await GitAddTool().execute({"paths": ["new.py"]}, ctx(git_repo))
    r = await GitStatusTool().execute({}, ctx(git_repo))
    assert any("new.py" in f for f in r["staged"])


async def test_commit_creates_hash(git_repo):
    (git_repo / "new.py").write_text("x")
    await GitAddTool().execute({"paths": ["new.py"]}, ctx(git_repo))
    r = await GitCommitTool().execute({"message": "Add new.py"}, ctx(git_repo))
    assert r["ok"] is True
    assert len(r["hash"]) > 0


async def test_log_returns_commits(git_repo):
    r = await GitLogTool().execute({"n": 5}, ctx(git_repo))
    assert len(r["commits"]) >= 1
    assert r["commits"][0]["message"] == "Initial commit"


async def test_branch_lists_branch(git_repo):
    r = await GitBranchTool().execute({}, ctx(git_repo))
    branch_names = [b["name"] for b in r["branches"]]
    assert len(branch_names) >= 1


async def test_diff_staged(git_repo):
    (git_repo / "new.py").write_text("print('hello')\n")
    await GitAddTool().execute({"paths": ["new.py"]}, ctx(git_repo))
    r = await GitDiffTool().execute({"staged": True}, ctx(git_repo))
    assert "new.py" in r["diff"] or r["additions"] >= 0
