"""Tests for bounded fs tool output (PRD-133 Layer A)."""

from __future__ import annotations

import subprocess

import pytest

from agenthicc.tools.fs import (
    ListDirectoryTool,
    ReadFileTool,
    SearchFilesTool,
    _MAX_LIST_ENTRIES,
    _MAX_TOOL_OUTPUT_CHARS,
)

pytestmark = pytest.mark.unit


def _git_init(d) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(d), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(d), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(d), check=True)


class TestGitIgnoreFiltering:
    @pytest.mark.asyncio
    async def test_search_respects_gitignore(self, tmp_path) -> None:
        (tmp_path / "keep.py").write_text("x")
        (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
        (tmp_path / "ignored").mkdir()
        (tmp_path / "ignored" / "junk.py").write_text("j")
        (tmp_path / "app.log").write_text("l")
        _git_init(tmp_path)
        ctx = {"workspace_root": str(tmp_path)}

        r = await SearchFilesTool().execute({"pattern": "**/*"}, ctx)
        files = set(r["matches"])
        assert "keep.py" in files
        assert not any("ignored" in m for m in files), "ignored/ excluded via .gitignore"
        assert "app.log" not in files

    @pytest.mark.asyncio
    async def test_non_git_reads_everything(self, tmp_path) -> None:
        # No git repo → full walk (no arbitrary noise filter); completeness.
        (tmp_path / "keep.py").write_text("x")
        (tmp_path / ".gitignore").write_text("ignored/\n")
        (tmp_path / "ignored").mkdir()
        (tmp_path / "ignored" / "junk.py").write_text("j")
        ctx = {"workspace_root": str(tmp_path)}

        r = await SearchFilesTool().execute({"pattern": "**/*.py"}, ctx)
        assert any("junk" in m for m in r["matches"]), "fallback reads everything"

    @pytest.mark.asyncio
    async def test_list_directory_recursive_respects_gitignore(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / ".gitignore").write_text("build/\n")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "out.o").write_text("o")
        _git_init(tmp_path)
        ctx = {"workspace_root": str(tmp_path)}

        r = await ListDirectoryTool().execute({"path": ".", "pattern": "*", "recursive": True}, ctx)
        paths = {e["path"] for e in r["entries"]}
        assert "a.py" in paths
        assert not any("build" in p for p in paths)


class TestEntryCaps:
    @pytest.mark.asyncio
    async def test_search_caps_entries(self, tmp_path) -> None:
        for i in range(_MAX_LIST_ENTRIES + 50):
            (tmp_path / f"f{i}.txt").write_text("x")
        ctx = {"workspace_root": str(tmp_path)}  # no git → full walk
        r = await SearchFilesTool().execute({"pattern": "*.txt"}, ctx)
        assert r["count"] == _MAX_LIST_ENTRIES
        assert r.get("truncated") is True


class TestReadCaps:
    @pytest.mark.asyncio
    async def test_read_file_caps_large_content(self, tmp_path) -> None:
        f = tmp_path / "big.txt"
        f.write_text("A" * (_MAX_TOOL_OUTPUT_CHARS + 50_000))
        ctx = {"workspace_root": str(tmp_path)}
        from agenthicc.tools.fs.file_cache import configure_file_cache

        configure_file_cache(None)
        r = await ReadFileTool().execute({"path": "big.txt", "line_numbers": False}, ctx)
        assert r.get("truncated") is True
        assert len(r["content"]) <= _MAX_TOOL_OUTPUT_CHARS + 500  # + marker
        assert r["size_bytes"] == _MAX_TOOL_OUTPUT_CHARS + 50_000  # real size reported

    @pytest.mark.asyncio
    async def test_a_read_is_cut_by_whole_lines_with_the_next_offset(self, tmp_path) -> None:
        f = tmp_path / "big.txt"
        f.write_text("\n".join("L" * 200 for _ in range(2000)))  # ~400k chars
        ctx = {"workspace_root": str(tmp_path)}
        r = await ReadFileTool().execute({"path": "big.txt", "line_numbers": False}, ctx)
        assert r.get("truncated") is True
        assert len(r["content"]) <= _MAX_TOOL_OUTPUT_CHARS
        assert r["total_lines"] == 2000  # true total still reported
        assert r["next_offset"] == r["lines_returned"] + 1
        rest = await ReadFileTool().execute(
            {"path": "big.txt", "offset": r["next_offset"], "limit": 5}, ctx
        )
        assert rest["content"].splitlines()[0].startswith(f"{r['next_offset']}\t")

    @pytest.mark.asyncio
    async def test_small_read_not_truncated(self, tmp_path) -> None:
        f = tmp_path / "small.txt"
        f.write_text("hello world")
        ctx = {"workspace_root": str(tmp_path)}
        from agenthicc.tools.fs.file_cache import configure_file_cache

        configure_file_cache(None)
        r = await ReadFileTool().execute({"path": "small.txt", "line_numbers": False}, ctx)
        assert "truncated" not in r and r["content"] == "hello world"
