"""Unit tests for filesystem tools (PRD-14)."""

from __future__ import annotations
import pytest
from agenthicc.tools.fs import (
    FsToolKit,
    ReadFileTool,
    WriteFileTool,
    AppendFileTool,
    DeleteFileTool,
    MoveFileTool,
    CopyFileTool,
    ListDirectoryTool,
    MakeDirectoryTool,
    SearchFilesTool,
    GrepFilesTool,
    GetFileInfoTool,
    PatchFileTool,
)

pytestmark = pytest.mark.unit


def ctx(tmp_path):
    return {"workspace_root": str(tmp_path)}


class TestReadFileTool:
    async def test_reads_content(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        r = await ReadFileTool().execute({"path": "a.txt", "line_numbers": False}, ctx(tmp_path))
        assert r["content"] == "hello"

    async def test_not_found(self, tmp_path):
        r = await ReadFileTool().execute({"path": "nope.txt", "line_numbers": False}, ctx(tmp_path))
        assert r["ok"] is False and r["error_code"] == "not_found"

    async def test_traversal_denied(self, tmp_path):
        r = await ReadFileTool().execute(
            {"path": "../../etc/passwd", "line_numbers": False}, ctx(tmp_path)
        )
        assert r["ok"] is False


class TestWriteFileTool:
    async def test_writes_content(self, tmp_path):
        r = await WriteFileTool().execute({"path": "out.txt", "content": "world"}, ctx(tmp_path))
        assert r["ok"] is True
        assert (tmp_path / "out.txt").read_text() == "world"

    async def test_creates_parents(self, tmp_path):
        r = await WriteFileTool().execute({"path": "a/b/c.txt", "content": "x"}, ctx(tmp_path))
        assert r["ok"] is True
        assert (tmp_path / "a/b/c.txt").exists()

    async def test_traversal_denied(self, tmp_path):
        r = await WriteFileTool().execute(
            {"path": "../../tmp/evil.txt", "content": "x"}, ctx(tmp_path)
        )
        assert r["ok"] is False


class TestAppendFileTool:
    async def test_appends(self, tmp_path):
        (tmp_path / "f.txt").write_text("line1\n")
        r = await AppendFileTool().execute({"path": "f.txt", "content": "line2\n"}, ctx(tmp_path))
        assert r["ok"] is True
        assert (tmp_path / "f.txt").read_text() == "line1\nline2\n"


class TestDeleteFileTool:
    async def test_deletes(self, tmp_path):
        (tmp_path / "del.txt").write_text("bye")
        r = await DeleteFileTool().execute({"path": "del.txt"}, ctx(tmp_path))
        assert r["ok"] is True
        assert not (tmp_path / "del.txt").exists()

    async def test_not_found(self, tmp_path):
        r = await DeleteFileTool().execute({"path": "nope.txt"}, ctx(tmp_path))
        assert r["ok"] is False


class TestMoveFileTool:
    async def test_moves(self, tmp_path):
        (tmp_path / "src.txt").write_text("data")
        r = await MoveFileTool().execute(
            {"source": "src.txt", "destination": "dst.txt"}, ctx(tmp_path)
        )
        assert r["ok"] is True
        assert (tmp_path / "dst.txt").exists()
        assert not (tmp_path / "src.txt").exists()


class TestCopyFileTool:
    async def test_copies(self, tmp_path):
        (tmp_path / "orig.txt").write_text("copy me")
        r = await CopyFileTool().execute(
            {"source": "orig.txt", "destination": "copy.txt"}, ctx(tmp_path)
        )
        assert r["ok"] is True
        assert (tmp_path / "copy.txt").read_text() == "copy me"


class TestListDirectoryTool:
    async def test_lists_files(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        r = await ListDirectoryTool().execute({}, ctx(tmp_path))
        names = [e["name"] for e in r["entries"]]
        assert "a.py" in names and "b.py" in names

    async def test_pattern_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.txt").write_text("")
        r = await ListDirectoryTool().execute({"pattern": "*.py"}, ctx(tmp_path))
        assert all(e["name"].endswith(".py") for e in r["entries"])


class TestMakeDirectoryTool:
    async def test_creates(self, tmp_path):
        r = await MakeDirectoryTool().execute({"path": "newdir/sub"}, ctx(tmp_path))
        assert r["ok"] is True
        assert (tmp_path / "newdir/sub").is_dir()


class TestSearchFilesTool:
    async def test_finds_matching(self, tmp_path):
        (tmp_path / "main.py").write_text("")
        (tmp_path / "test.js").write_text("")
        r = await SearchFilesTool().execute({"pattern": "*.py"}, ctx(tmp_path))
        assert any("main.py" in m for m in r["matches"])
        assert not any(".js" in m for m in r["matches"])


class TestGrepFilesTool:
    async def test_finds_pattern(self, tmp_path):
        (tmp_path / "code.py").write_text("def hello():\n    pass\n")
        r = await GrepFilesTool().execute({"pattern": "def hello"}, ctx(tmp_path))
        assert r["total_matches"] >= 1
        assert any("hello" in m["line"] for m in r["matches"])

    async def test_no_match(self, tmp_path):
        (tmp_path / "f.py").write_text("nothing here\n")
        r = await GrepFilesTool().execute({"pattern": "ZZZNOMATCH"}, ctx(tmp_path))
        assert r["total_matches"] == 0 and r["matches"] == []


class TestGetFileInfoTool:
    async def test_returns_info(self, tmp_path):
        (tmp_path / "info.txt").write_text("x" * 100)
        r = await GetFileInfoTool().execute({"path": "info.txt"}, ctx(tmp_path))
        assert r["ok"] is True and r["exists"] is True
        assert r["size_bytes"] == 100
        assert r["type"] == "file"
        assert "modified_at" in r

    async def test_a_missing_path_is_ok_and_does_not_exist(self, tmp_path):
        r = await GetFileInfoTool().execute({"path": "nope.txt"}, ctx(tmp_path))
        assert r == {"ok": True, "exists": False, "path": "nope.txt", "type": None}


class TestPatchFileTool:
    async def test_replaces_content(self, tmp_path):
        (tmp_path / "p.py").write_text("import bcrypt\n")
        r = await PatchFileTool().execute(
            {"path": "p.py", "old_content": "bcrypt", "new_content": "argon2"}, ctx(tmp_path)
        )
        assert r["ok"] is True and r["replacements"] == 1
        assert (tmp_path / "p.py").read_text() == "import argon2\n"

    async def test_old_not_found(self, tmp_path):
        (tmp_path / "p.py").write_text("hello\n")
        r = await PatchFileTool().execute(
            {"path": "p.py", "old_content": "NOTHERE", "new_content": "x"}, ctx(tmp_path)
        )
        assert r["ok"] is False


class TestFsToolKit:
    def test_returns_12_tools(self, tmp_path):
        tools = FsToolKit().tools(str(tmp_path))
        assert len(tools) == 12
        names = {t.name for t in tools}
        assert "read_file" in names and "patch_file" in names
