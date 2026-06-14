"""Tests for git agent tool wrappers (PRD-15)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.unit


async def test_git_status_tool_wrapper():
    from agenthicc.tools.git.agent_tools import git_status
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "## main\n", "")):
        result = await git_status()
    assert "branch" in result or "clean" in result or isinstance(result, dict)


async def test_git_diff_tool_wrapper():
    from agenthicc.tools.git.agent_tools import git_diff
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "diff output\n", "")):
        result = await git_diff()
    assert isinstance(result, dict)


async def test_git_log_tool_wrapper():
    from agenthicc.tools.git.agent_tools import git_log
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "abc\x1fshort\x1fAlice\x1f2025-01-01\x1fFix\n", "")):
        result = await git_log(n=5)
    assert "commits" in result


async def test_git_show_wrapper():
    from agenthicc.tools.git.agent_tools import git_show
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "abc\x1fAlice\x1f2025\x1fmsg\ndiff\n", "")):
        result = await git_show()
    assert isinstance(result, dict)


async def test_git_add_wrapper():
    from agenthicc.tools.git.agent_tools import git_add
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "", "")):
        result = await git_add(paths=["src/main.py"])
    assert isinstance(result, dict)


async def test_git_commit_wrapper():
    from agenthicc.tools.git.agent_tools import git_commit
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "[main abc1234] Fix\n", "")):
        result = await git_commit(message="Fix bug")
    assert "ok" in result or "hash" in result


async def test_git_checkout_wrapper():
    from agenthicc.tools.git.agent_tools import git_checkout
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "", "")):
        result = await git_checkout(branch="main")
    assert isinstance(result, dict)


async def test_git_branch_wrapper():
    from agenthicc.tools.git.agent_tools import git_branch
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "main\x1f*\x1forigin/main\n", "")):
        result = await git_branch()
    assert isinstance(result, dict)


async def test_git_stash_wrapper():
    from agenthicc.tools.git.agent_tools import git_stash
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "Saved as stash@{0}\n", "")):
        result = await git_stash()
    assert isinstance(result, dict)


async def test_git_blame_wrapper():
    from agenthicc.tools.git.agent_tools import git_blame
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "abc\nauthor Alice\n\thello\n", "")):
        result = await git_blame(path="main.py")
    assert isinstance(result, dict)


async def test_git_grep_wrapper():
    from agenthicc.tools.git.agent_tools import git_grep
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "HEAD:src/a.py:1:def foo\n", "")):
        result = await git_grep(pattern="foo")
    assert "matches" in result


async def test_git_diff_with_path():
    from agenthicc.tools.git.agent_tools import git_diff
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "", "")):
        result = await git_diff(path="src/main.py", staged=True)
    assert isinstance(result, dict)


async def test_git_log_with_path():
    from agenthicc.tools.git.agent_tools import git_log
    with patch("agenthicc.tools.git._run_git", new_callable=AsyncMock,
               return_value=(0, "", "")):
        result = await git_log(n=3, path="src/")
    assert isinstance(result, dict)


def test_git_agent_tools_list():
    from agenthicc.tools.git.agent_tools import GIT_AGENT_TOOLS
    assert len(GIT_AGENT_TOOLS) == 11
