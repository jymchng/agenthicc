"""Integration tests for filesystem tools with real I/O (PRD-14)."""

from __future__ import annotations
import pytest
from agenthicc.tools.fs import (
    WriteFileTool,
    ReadFileTool,
    PatchFileTool,
    GrepFilesTool,
    ListDirectoryTool,
    MoveFileTool,
    DeleteFileTool,
    CopyFileTool,
    GetFileInfoTool,
    ReadLinesTool,
    SearchFilesTool,
)

pytestmark = pytest.mark.integration


def ctx(tmp_path):
    return {"workspace_root": str(tmp_path)}


async def test_write_read_roundtrip(tmp_path):
    await WriteFileTool().execute({"path": "hello.txt", "content": "world"}, ctx(tmp_path))
    r = await ReadFileTool().execute({"path": "hello.txt"}, ctx(tmp_path))
    assert r["content"] == "world"


async def test_write_then_grep(tmp_path):
    await WriteFileTool().execute(
        {"path": "src.py", "content": "def argon2_hash():\n    pass\n"}, ctx(tmp_path)
    )
    r = await GrepFilesTool().execute({"pattern": "argon2_hash"}, ctx(tmp_path))
    assert r["count"] == 1 and "argon2_hash" in r["matches"][0]["line"]


async def test_write_patch_read(tmp_path):
    await WriteFileTool().execute({"path": "auth.py", "content": "import bcrypt\n"}, ctx(tmp_path))
    await PatchFileTool().execute(
        {"path": "auth.py", "old_content": "bcrypt", "new_content": "argon2"}, ctx(tmp_path)
    )
    r = await ReadFileTool().execute({"path": "auth.py"}, ctx(tmp_path))
    assert "argon2" in r["content"] and "bcrypt" not in r["content"]


async def test_list_directory_recursive(tmp_path):
    (tmp_path / "subdir").mkdir()
    await WriteFileTool().execute({"path": "a.py", "content": ""}, ctx(tmp_path))
    await WriteFileTool().execute({"path": "subdir/b.py", "content": ""}, ctx(tmp_path))
    r = await ListDirectoryTool().execute({"recursive": True, "pattern": "*.py"}, ctx(tmp_path))
    paths = [e["path"] for e in r["entries"]]
    assert any("a.py" in p for p in paths)
    assert any("b.py" in p for p in paths)


async def test_search_files_glob(tmp_path):
    await WriteFileTool().execute({"path": "main.py", "content": ""}, ctx(tmp_path))
    await WriteFileTool().execute({"path": "main.js", "content": ""}, ctx(tmp_path))
    r = await SearchFilesTool().execute({"pattern": "*.py"}, ctx(tmp_path))
    assert any("main.py" in m for m in r["matches"])
    assert not any(".js" in m for m in r["matches"])


async def test_read_lines_range(tmp_path):
    await WriteFileTool().execute(
        {"path": "lines.txt", "content": "1\n2\n3\n4\n5\n"}, ctx(tmp_path)
    )
    r = await ReadLinesTool().execute({"path": "lines.txt", "start": 2, "end": 4}, ctx(tmp_path))
    assert r["lines"] == ["2", "3", "4"] and r["total_lines"] == 5


async def test_move_file(tmp_path):
    await WriteFileTool().execute({"path": "src.txt", "content": "data"}, ctx(tmp_path))
    await MoveFileTool().execute({"source": "src.txt", "destination": "dst.txt"}, ctx(tmp_path))
    assert (tmp_path / "dst.txt").exists()
    assert not (tmp_path / "src.txt").exists()


async def test_delete_file(tmp_path):
    await WriteFileTool().execute({"path": "bye.txt", "content": ""}, ctx(tmp_path))
    await DeleteFileTool().execute({"path": "bye.txt"}, ctx(tmp_path))
    assert not (tmp_path / "bye.txt").exists()


async def test_copy_file(tmp_path):
    await WriteFileTool().execute({"path": "orig.txt", "content": "hello"}, ctx(tmp_path))
    await CopyFileTool().execute({"source": "orig.txt", "destination": "copy.txt"}, ctx(tmp_path))
    assert (tmp_path / "copy.txt").read_text() == "hello"


async def test_get_file_info_size(tmp_path):
    content = "x" * 500
    await WriteFileTool().execute({"path": "big.txt", "content": content}, ctx(tmp_path))
    r = await GetFileInfoTool().execute({"path": "big.txt"}, ctx(tmp_path))
    assert r["size_bytes"] == 500
    assert r["type"] == "file"
