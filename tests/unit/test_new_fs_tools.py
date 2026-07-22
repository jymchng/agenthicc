"""Unit tests for the new filesystem agent tools (grep_file, apply_diff,
checksum_file, truncate_file, touch_file, batch_*).

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""

import hashlib
from pathlib import Path

import pytest

import agenthicc.tools.fs.agent_tools as _at
from agenthicc.tools.fs.linux import LinuxFilesystemBackend
from agenthicc.tools.fs.router import BackendRouter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _configure(tmp_path):
    """Point the module-level router at tmp_path for this test."""
    router = BackendRouter(LinuxFilesystemBackend(tmp_path))
    _at._router = router


def _reset_router():
    """Remove router override so later tests are isolated."""
    _at._router = None


# ---------------------------------------------------------------------------
# grep_file
# ---------------------------------------------------------------------------


async def test_grep_file_finds_matches(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    pass\n")
        result = await _at.grep_file(str(f), "def foo")
        assert result["ok"] is True
        assert result["total_matches"] == 1
        assert result["matches"][0]["line_number"] == 1
    finally:
        _reset_router()


async def test_grep_file_case_insensitive(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "upper.py"
        f.write_text("DEF FOO():\n    pass\n")
        result = await _at.grep_file(str(f), "def foo", case_sensitive=False)
        assert result["ok"] is True
        assert result["total_matches"] == 1
    finally:
        _reset_router()


async def test_grep_file_context_lines(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "ctx.txt"
        f.write_text("line1\nline2\nTARGET\nline4\nline5\n")
        result = await _at.grep_file(str(f), "TARGET", context_lines=1)
        assert result["ok"] is True
        assert result["total_matches"] == 1
        match = result["matches"][0]
        assert len(match["context_before"]) == 1
        assert match["context_before"][0] == "line2"
    finally:
        _reset_router()


async def test_grep_file_not_found(tmp_path):
    _configure(tmp_path)
    try:
        missing = str(tmp_path / "no_such_file.py")
        result = await _at.grep_file(missing, "pattern")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# apply_diff
# ---------------------------------------------------------------------------


async def test_apply_diff_add_line(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "add.py"
        f.write_text("line1\nline2\n")
        diff = "@@ -1,2 +1,3 @@\n line1\n line2\n+line3\n"
        result = await _at.apply_diff(str(f), diff)
        assert result["ok"] is True
        assert result["hunks_applied"] == 1
        assert "line3" in f.read_text()
    finally:
        _reset_router()


async def test_apply_diff_remove_line(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "remove.py"
        f.write_text("line1\nline2\nline3\n")
        diff = "@@ -1,3 +1,2 @@\n line1\n-line2\n line3\n"
        result = await _at.apply_diff(str(f), diff)
        assert result["ok"] is True
        assert "line2" not in f.read_text()
    finally:
        _reset_router()


async def test_apply_diff_bad_context_fails(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "bad_ctx.py"
        f.write_text("alpha\nbeta\n")
        # Context says "gamma" but file has "alpha"
        diff = "@@ -1,2 +1,2 @@\n gamma\n-beta\n+delta\n"
        result = await _at.apply_diff(str(f), diff)
        assert result["ok"] is False
    finally:
        _reset_router()


async def test_apply_diff_partial_allowed(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "partial.py"
        f.write_text("good\nextra\n")
        # Two hunks: first succeeds (adds "inserted" after "good"),
        # second fails because context "WRONG_CONTEXT" is not in the file.
        diff = (
            "@@ -1,1 +1,2 @@\n"
            " good\n"
            "+inserted\n"
            "@@ -2,1 +3,1 @@\n"
            " WRONG_CONTEXT\n"
            "-extra\n"
            "+replaced\n"
        )
        result = await _at.apply_diff(str(f), diff, allow_partial=True)
        assert result["hunks_applied"] == 1
        assert result["hunks_failed"] == 1
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# checksum_file
# ---------------------------------------------------------------------------


async def test_checksum_sha256(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "hello.txt"
        content = b"hello"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        result = await _at.checksum_file(str(f))
        assert result["ok"] is True
        assert result["digest"] == expected
        assert result["algorithm"] == "sha256"
    finally:
        _reset_router()


async def test_checksum_md5(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "md5test.txt"
        content = b"hello md5"
        f.write_bytes(content)
        expected = hashlib.md5(content).hexdigest()
        result = await _at.checksum_file(str(f), algorithm="md5")
        assert result["ok"] is True
        assert result["digest"] == expected
        assert result["algorithm"] == "md5"
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# truncate_file
# ---------------------------------------------------------------------------


async def test_truncate_file(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "trunc.txt"
        f.write_text("hello world")
        result = await _at.truncate_file(str(f), size=5)
        assert result["ok"] is True
        assert result["new_size"] == 5
        assert f.read_bytes() == b"hello"
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# touch_file
# ---------------------------------------------------------------------------


async def test_touch_creates_new_file(tmp_path):
    _configure(tmp_path)
    try:
        target = str(tmp_path / "new.txt")
        result = await _at.touch_file(target)
        assert result["ok"] is True
        assert result["created"] is True
        assert (tmp_path / "new.txt").exists()
    finally:
        _reset_router()


async def test_touch_existing_updates_mtime(tmp_path):
    _configure(tmp_path)
    try:
        f = tmp_path / "existing.txt"
        f.write_text("data")
        result = await _at.touch_file(str(f))
        assert result["ok"] is True
        assert result["created"] is False
    finally:
        _reset_router()


async def test_touch_no_create_fails(tmp_path):
    _configure(tmp_path)
    try:
        missing = str(tmp_path / "missing.txt")
        result = await _at.touch_file(missing, create=False)
        assert result["ok"] is False
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# batch_read
# ---------------------------------------------------------------------------


async def test_batch_read_all_ok(tmp_path):
    _configure(tmp_path)
    try:
        file_names = ["a.txt", "b.txt", "c.txt"]
        for name in file_names:
            (tmp_path / name).write_text(f"content of {name}")
        paths = [str(tmp_path / name) for name in file_names]
        result = await _at.batch_read(paths)
        assert result["ok"] is True
        assert result["succeeded"] == 3
        assert result["failed"] == 0
    finally:
        _reset_router()


async def test_batch_read_partial(tmp_path):
    _configure(tmp_path)
    try:
        (tmp_path / "exists.txt").write_text("hello")
        paths = [str(tmp_path / "exists.txt"), str(tmp_path / "missing.txt")]
        result = await _at.batch_read(paths)
        assert result["ok"] is False
        assert result["succeeded"] == 1
        assert result["failed"] == 1
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# batch_write
# ---------------------------------------------------------------------------


async def test_batch_write_all(tmp_path):
    _configure(tmp_path)
    try:
        files = [
            {"path": str(tmp_path / "w1.txt"), "content": "alpha"},
            {"path": str(tmp_path / "w2.txt"), "content": "beta"},
            {"path": str(tmp_path / "w3.txt"), "content": "gamma"},
        ]
        result = await _at.batch_write(files)
        assert result["ok"] is True
        assert result["succeeded"] == 3
        for item in files:
            assert Path(item["path"]).read_text() == item["content"]
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# batch_delete
# ---------------------------------------------------------------------------


async def test_batch_delete_all(tmp_path):
    _configure(tmp_path)
    try:
        names = ["del1.txt", "del2.txt", "del3.txt"]
        for n in names:
            (tmp_path / n).write_text("bye")
        paths = [str(tmp_path / n) for n in names]
        result = await _at.batch_delete(paths)
        assert result["ok"] is True
        for p in paths:
            assert not Path(p).exists()
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# batch_move
# ---------------------------------------------------------------------------


async def test_batch_move(tmp_path):
    _configure(tmp_path)
    try:
        for n in ["mv1.txt", "mv2.txt"]:
            (tmp_path / n).write_text("data")
        moves = [
            {"source": str(tmp_path / "mv1.txt"), "destination": str(tmp_path / "mv1_dst.txt")},
            {"source": str(tmp_path / "mv2.txt"), "destination": str(tmp_path / "mv2_dst.txt")},
        ]
        result = await _at.batch_move(moves)
        assert result["ok"] is True
        assert result["succeeded"] == 2
        for m in moves:
            assert not Path(m["source"]).exists()
            assert Path(m["destination"]).exists()
    finally:
        _reset_router()


# ---------------------------------------------------------------------------
# batch_copy
# ---------------------------------------------------------------------------


async def test_batch_copy(tmp_path):
    _configure(tmp_path)
    try:
        for n in ["cp1.txt", "cp2.txt"]:
            (tmp_path / n).write_text("copy me")
        copies = [
            {"source": str(tmp_path / "cp1.txt"), "destination": str(tmp_path / "cp1_copy.txt")},
            {"source": str(tmp_path / "cp2.txt"), "destination": str(tmp_path / "cp2_copy.txt")},
        ]
        result = await _at.batch_copy(copies)
        assert result["ok"] is True
        assert result["succeeded"] == 2
        for c in copies:
            assert Path(c["source"]).exists(), "original should still exist"
            assert Path(c["destination"]).exists(), "copy should have been created"
    finally:
        _reset_router()
