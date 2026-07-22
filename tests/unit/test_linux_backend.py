"""Unit tests for LinuxFilesystemBackend."""

from __future__ import annotations

import pytest

from agenthicc.tools.fs.linux import LinuxFilesystemBackend

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def test_write_read_roundtrip(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("hello.txt", "hello world")
    assert b.read_text("hello.txt") == "hello world"


def test_read_bytes(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_bytes("data.bin", b"\x00\x01\x02")
    assert b.read_bytes("data.bin") == b"\x00\x01\x02"


def test_append_text(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("log.txt", "line1\n")
    b.append_text("log.txt", "line2\n")
    assert b.read_text("log.txt") == "line1\nline2\n"


def test_truncate(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("t.txt", "abcdef")
    b.truncate("t.txt", size=3)
    assert b.read_bytes("t.txt") == b"abc"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_delete_file(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("del.txt", "bye")
    b.delete("del.txt")
    assert not b.exists("del.txt")


def test_delete_directory(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.make_directory("subdir")
    b.write_text("subdir/f.txt", "x")
    b.delete("subdir")
    assert not b.exists("subdir")


def test_move(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("src.txt", "data")
    b.move("src.txt", "dst.txt")
    assert not b.exists("src.txt")
    assert b.read_text("dst.txt") == "data"


def test_copy(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("original.txt", "copy me")
    b.copy("original.txt", "copy.txt")
    assert b.read_text("copy.txt") == "copy me"
    assert b.exists("original.txt")


def test_make_directory(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.make_directory("a/b/c")
    assert b.exists("a/b/c")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_exists_true_and_false(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("present.txt", "hi")
    assert b.exists("present.txt") is True
    assert b.exists("absent.txt") is False


def test_stat_fields(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("s.txt", "stat me")
    s = b.stat("s.txt")
    assert s.size > 0
    assert s.is_file is True
    assert s.permissions != ""
    assert s.backend == "linux"


def test_list_dir_files(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("a.txt", "a")
    b.write_text("b.txt", "b")
    names = {e.name for e in b.list_dir(".")}
    assert "a.txt" in names
    assert "b.txt" in names


def test_list_dir_recursive(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.make_directory("sub")
    b.write_text("sub/deep.txt", "d")
    entries = b.list_dir(".", recursive=True)
    names = {e.name for e in entries}
    assert "deep.txt" in names


def test_list_dir_hidden_excluded(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text(".hidden", "secret")
    b.write_text("visible.txt", "shown")
    names = {e.name for e in b.list_dir(".", include_hidden=False)}
    assert ".hidden" not in names
    assert "visible.txt" in names


def test_glob_pattern(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("foo.py", "")
    b.write_text("bar.py", "")
    b.write_text("baz.txt", "")
    py_files = b.glob("*.py")
    assert "foo.py" in py_files
    assert "bar.py" in py_files
    assert "baz.txt" not in py_files


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_basic(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("code.py", "def foo():\n    pass\n")
    matches = b.grep("def foo", path=".")
    assert len(matches) >= 1
    assert any("def foo" in m.line for m in matches)


def test_grep_case_insensitive(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("readme.txt", "Hello World\n")
    matches = b.grep("hello", path=".", case_sensitive=False)
    assert len(matches) >= 1
    assert any("Hello" in m.line for m in matches)


def test_grep_max_results(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    content = "\n".join(f"match line {i}" for i in range(20)) + "\n"
    b.write_text("many.txt", content)
    matches = b.grep("match line", path=".", max_results=5)
    assert len(matches) == 5


# ---------------------------------------------------------------------------
# read_lines
# ---------------------------------------------------------------------------


def test_read_lines_range(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    lines = [f"line {i}" for i in range(1, 11)]
    b.write_text("lines.txt", "\n".join(lines))
    result, total = b.read_lines("lines.txt", start=3, end=7)
    assert total == 10
    assert result == [f"line {i}" for i in range(3, 8)]


# ---------------------------------------------------------------------------
# Path escape
# ---------------------------------------------------------------------------


def test_path_escape_rejected(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    with pytest.raises(PermissionError):
        b.read_text("../../etc/passwd")


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------


def test_batch_read(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("r1.txt", "one")
    b.write_text("r2.txt", "two")
    results = b.batch_read(["r1.txt", "r2.txt"])
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    contents = {r["path"]: r["content"] for r in results}
    assert contents["r1.txt"] == "one"
    assert contents["r2.txt"] == "two"


def test_batch_read_partial_failure(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("exists.txt", "here")
    results = b.batch_read(["exists.txt", "missing.txt"])
    succeeded = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    assert succeeded == 1
    assert failed == 1


def test_batch_write(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    files = [
        {"path": "bw1.txt", "content": "alpha"},
        {"path": "bw2.txt", "content": "beta"},
    ]
    results = b.batch_write(files)
    assert all(r["ok"] for r in results)
    assert b.read_text("bw1.txt") == "alpha"
    assert b.read_text("bw2.txt") == "beta"


def test_batch_delete(tmp_path):
    b = LinuxFilesystemBackend(tmp_path)
    b.write_text("bd1.txt", "x")
    b.write_text("bd2.txt", "y")
    results = b.batch_delete(["bd1.txt", "bd2.txt"])
    assert all(r["ok"] for r in results)
    assert not b.exists("bd1.txt")
    assert not b.exists("bd2.txt")
