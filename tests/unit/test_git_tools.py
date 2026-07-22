"""Unit tests for git tools (PRD-15)."""

from __future__ import annotations
import pytest
from unittest.mock import patch, AsyncMock
from agenthicc.tools.git import (
    GitToolKit,
    GitStatusTool,
    GitLogTool,
    GitCommitTool,
    GitAddTool,
    GitCheckoutTool,
    GitBranchTool,
    GitGrepTool,
)

pytestmark = pytest.mark.unit


def _mock(rc=0, out="", err=""):
    with patch(
        "agenthicc.tools.git._run_git",
        new_callable=lambda: lambda: AsyncMock(return_value=(rc, out, err)),
    ):
        pass


@pytest.fixture
def mock_git():
    with patch("agenthicc.tools.git._run_git") as m:
        yield m


class TestGitStatusTool:
    async def test_clean_repo(self, mock_git):
        mock_git.return_value = (0, "## main...origin/main\n", "")
        r = await GitStatusTool().execute({}, {})
        assert r["clean"] is True and r["branch"] == "main"

    async def test_with_staged(self, mock_git):
        mock_git.return_value = (0, "## main\nM  src/foo.py\n", "")
        r = await GitStatusTool().execute({}, {})
        assert not r["clean"] and "src/foo.py" in r["staged"]

    async def test_untracked(self, mock_git):
        mock_git.return_value = (0, "## main\n?? new.py\n", "")
        r = await GitStatusTool().execute({}, {})
        assert "new.py" in r["untracked"]

    async def test_error(self, mock_git):
        mock_git.return_value = (128, "", "not a git repo")
        r = await GitStatusTool().execute({}, {})
        assert r.get("ok") is False or "error" in r


class TestGitLogTool:
    async def test_parses_commits(self, mock_git):
        mock_git.return_value = (0, "abc1234\x1fabc\x1fAlice\x1f2025-01-01\x1fFix bug\n", "")
        r = await GitLogTool().execute({"n": 1}, {})
        assert len(r["commits"]) == 1
        assert r["commits"][0]["author"] == "Alice"

    async def test_n_forwarded(self, mock_git):
        mock_git.return_value = (0, "", "")
        await GitLogTool().execute({"n": 3}, {})
        assert "-3" in mock_git.call_args[0][1]


class TestGitCommitTool:
    async def test_success_parses_hash(self, mock_git):
        mock_git.return_value = (0, "[main abc1234] Fix auth\n", "")
        r = await GitCommitTool().execute({"message": "Fix auth"}, {})
        assert r["ok"] is True and r["hash"] == "abc1234"

    async def test_failure(self, mock_git):
        mock_git.return_value = (1, "", "nothing to commit")
        r = await GitCommitTool().execute({"message": "empty"}, {})
        assert r["ok"] is False


class TestGitAddTool:
    async def test_success(self, mock_git):
        mock_git.return_value = (0, "", "")
        r = await GitAddTool().execute({"paths": ["src/a.py"]}, {})
        assert r["ok"] is True

    async def test_paths_forwarded(self, mock_git):
        mock_git.return_value = (0, "", "")
        await GitAddTool().execute({"paths": ["a.py", "b.py"]}, {})
        args = mock_git.call_args[0][1]
        assert "a.py" in args and "b.py" in args


class TestGitCheckoutTool:
    async def test_success(self, mock_git):
        mock_git.return_value = (0, "", "")
        r = await GitCheckoutTool().execute({"branch": "feature"}, {})
        assert r["ok"] is True and r["branch"] == "feature"

    async def test_create_flag(self, mock_git):
        mock_git.return_value = (0, "", "")
        await GitCheckoutTool().execute({"branch": "new", "create": True}, {})
        assert "-b" in mock_git.call_args[0][1]


class TestGitBranchTool:
    async def test_parses_branches(self, mock_git):
        mock_git.return_value = (0, "main\x1f*\x1forigin/main\nfeature\x1f \x1f\n", "")
        r = await GitBranchTool().execute({}, {})
        names = [b["name"] for b in r["branches"]]
        assert "main" in names


class TestGitGrepTool:
    async def test_parses_matches(self, mock_git):
        mock_git.return_value = (0, "HEAD:src/a.py:10:def foo():\n", "")
        r = await GitGrepTool().execute({"pattern": "foo"}, {})
        assert r["count"] >= 1


class TestGitToolKit:
    def test_returns_11_tools(self):
        tools = GitToolKit().tools()
        assert len(tools) == 11
        names = {t.name for t in tools}
        assert "git_status" in names and "git_blame" in names
