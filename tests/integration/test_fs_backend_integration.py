"""Integration tests: BackendRouter + LinuxFilesystemBackend + agent tools together.

Tests exercise real I/O through the full routing stack.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""

import pytest

import agenthicc.tools.fs.agent_tools as _at
from agenthicc.tools.fs.linux import LinuxFilesystemBackend
from agenthicc.tools.fs.router import BackendRouter

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_router(tmp_path) -> BackendRouter:
    return BackendRouter(LinuxFilesystemBackend(tmp_path))


# ---------------------------------------------------------------------------
# BackendRouter + LinuxFilesystemBackend basic roundtrip
# ---------------------------------------------------------------------------


def test_backend_router_linux_roundtrip(tmp_path):
    """BackendRouter with LinuxFilesystemBackend supports write + read + delete."""
    backend = LinuxFilesystemBackend(tmp_path)
    router = BackendRouter(backend)

    # write
    path = str(tmp_path / "hello.txt")
    backend.write_text(path, "hello world")
    assert (tmp_path / "hello.txt").exists()

    # read via router
    resolved_backend = router.resolve(path)
    content = resolved_backend.read_text(path)
    assert content == "hello world"

    # delete
    resolved_backend.delete(path)
    assert not (tmp_path / "hello.txt").exists()


# ---------------------------------------------------------------------------
# configure_router affects agent tools
# ---------------------------------------------------------------------------


async def test_configure_router_affects_tools(tmp_path):
    """configure_router() + agent tool batch_write / batch_read uses the new backend."""
    _at.configure_router(_make_router(tmp_path))
    try:
        files = [
            {"path": str(tmp_path / "r1.txt"), "content": "first"},
            {"path": str(tmp_path / "r2.txt"), "content": "second"},
        ]
        write_result = await _at.batch_write(files)
        assert write_result["ok"] is True

        paths = [f["path"] for f in files]
        read_result = await _at.batch_read(paths)
        assert read_result["ok"] is True
        contents = [r["content"] for r in read_result["results"]]
        assert "first" in contents
        assert "second" in contents
    finally:
        _at._router = None


# ---------------------------------------------------------------------------
# batch_write then grep_file
# ---------------------------------------------------------------------------


async def test_batch_write_then_grep_file(tmp_path):
    """Writing 3 Python files and then grep_file finds the correct pattern."""
    _at.configure_router(_make_router(tmp_path))
    try:
        files = [
            {"path": str(tmp_path / "alpha.py"), "content": "class Alpha:\n    pass\n"},
            {"path": str(tmp_path / "beta.py"), "content": "class Beta:\n    pass\n"},
            {"path": str(tmp_path / "gamma.py"), "content": "def helper():\n    return 42\n"},
        ]
        await _at.batch_write(files)

        result = await _at.grep_file(str(tmp_path / "gamma.py"), r"def helper")
        assert result["ok"] is True
        assert result["total_matches"] == 1
        assert "helper" in result["matches"][0]["line"]
    finally:
        _at._router = None


# ---------------------------------------------------------------------------
# apply_diff integration — multi-context-line realistic diff
# ---------------------------------------------------------------------------


async def test_apply_diff_integration(tmp_path):
    """Full realistic diff with multiple context lines applies cleanly."""
    _at.configure_router(_make_router(tmp_path))
    try:
        original = (
            "import os\n"
            "import sys\n"
            "\n"
            "def main():\n"
            "    print('hello')\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        f = tmp_path / "main.py"
        f.write_text(original)

        diff = "@@ -4,3 +4,3 @@\n def main():\n-    print('hello')\n+    print('hello world')\n \n"
        result = await _at.apply_diff(str(f), diff)
        assert result["ok"] is True
        assert result["hunks_applied"] == 1
        assert "hello world" in f.read_text()
        assert "print('hello')\n" not in f.read_text()
    finally:
        _at._router = None


# ---------------------------------------------------------------------------
# checksum changes after write
# ---------------------------------------------------------------------------


async def test_checksum_verify_after_write(tmp_path):
    """SHA-256 of a file changes after its content is replaced."""
    _at.configure_router(_make_router(tmp_path))
    try:
        f = tmp_path / "data.bin"
        f.write_bytes(b"version one")
        r1 = await _at.checksum_file(str(f))
        assert r1["ok"] is True
        digest_v1 = r1["digest"]

        f.write_bytes(b"version two")
        r2 = await _at.checksum_file(str(f))
        assert r2["ok"] is True
        digest_v2 = r2["digest"]

        assert digest_v1 != digest_v2
    finally:
        _at._router = None


# ---------------------------------------------------------------------------
# LinuxFilesystemBackend.grep() multi-file
# ---------------------------------------------------------------------------


def test_linux_backend_grep_multi_file(tmp_path):
    """LinuxFilesystemBackend.grep() searches across 5 files and finds all matches."""
    backend = LinuxFilesystemBackend(tmp_path)
    for i in range(5):
        backend.write_text(f"file{i}.py", f"# module {i}\ndef func_{i}(): pass\n")

    matches = backend.grep(r"def func_", path=".")
    assert len(matches) == 5
    matched_fns = {m.line for m in matches}
    for i in range(5):
        assert any(f"func_{i}" in line for line in matched_fns)


# ---------------------------------------------------------------------------
# batch_move roundtrip — 5 files
# ---------------------------------------------------------------------------


async def test_batch_move_roundtrip(tmp_path):
    """batch_move 5 files: all sources disappear and all destinations appear."""
    _at.configure_router(_make_router(tmp_path))
    try:
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        names = [f"file{i}.txt" for i in range(5)]
        for n in names:
            (src_dir / n).write_text(f"content {n}")

        moves = [
            {
                "source": str(src_dir / n),
                "destination": str(dst_dir / n),
            }
            for n in names
        ]
        result = await _at.batch_move(moves)
        assert result["ok"] is True
        assert result["succeeded"] == 5

        for n in names:
            assert not (src_dir / n).exists(), f"source {n} should be gone"
            assert (dst_dir / n).exists(), f"destination {n} should exist"
    finally:
        _at._router = None
