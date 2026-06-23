"""Tests for the workspace file cache (PRD-132 L1)."""
from __future__ import annotations

import time

import pytest

from agenthicc.tools.fs.file_cache import (
    WorkspaceFileCache,
    configure_file_cache,
    get_file_cache,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_global_cache():
    configure_file_cache(None)
    yield
    configure_file_cache(None)


class TestWorkspaceFileCache:
    def test_miss_when_no_entry(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        cache = WorkspaceFileCache(tmp_path / "c.db")
        assert cache.get_fresh(str(f)) is None

    def test_store_then_fresh_hit(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        cache = WorkspaceFileCache(tmp_path / "c.db")
        cache.store(str(f), "hello")
        assert cache.get_fresh(str(f)) == "hello"
        assert len(cache) == 1

    def test_changed_file_misses(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        cache = WorkspaceFileCache(tmp_path / "c.db")
        cache.store(str(f), "hello")
        time.sleep(0.01)
        f.write_text("changed")  # different size + mtime
        assert cache.get_fresh(str(f)) is None

    def test_same_size_different_mtime_misses(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        cache = WorkspaceFileCache(tmp_path / "c.db")
        cache.store(str(f), "hello")
        time.sleep(0.01)
        f.write_text("world")  # same length (5), new mtime → must miss
        assert cache.get_fresh(str(f)) is None

    def test_encoding_mismatch_misses(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        cache = WorkspaceFileCache(tmp_path / "c.db")
        cache.store(str(f), "hello", encoding="utf-8")
        assert cache.get_fresh(str(f), encoding="latin-1") is None
        assert cache.get_fresh(str(f), encoding="utf-8") == "hello"

    def test_deleted_file_misses(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        cache = WorkspaceFileCache(tmp_path / "c.db")
        cache.store(str(f), "hello")
        f.unlink()
        assert cache.get_fresh(str(f)) is None

    def test_durable_across_instances(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        db = tmp_path / "c.db"
        c1 = WorkspaceFileCache(db)
        c1.store(str(f), "hello")
        c1.close()
        c2 = WorkspaceFileCache(db)  # new process simulation
        assert c2.get_fresh(str(f)) == "hello"

    def test_store_replaces_on_change(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("v1")
        cache = WorkspaceFileCache(tmp_path / "c.db")
        cache.store(str(f), "v1")
        time.sleep(0.01)
        f.write_text("v2")
        cache.store(str(f), "v2")
        assert cache.get_fresh(str(f)) == "v2"
        assert len(cache) == 1  # replaced, not appended


class TestReadFileIntegration:
    @pytest.mark.asyncio
    async def test_read_file_stores_then_serves_cached(self, tmp_path) -> None:
        from agenthicc.tools.fs import ReadFileTool

        f = tmp_path / "a.txt"
        f.write_text("hello world")
        configure_file_cache(WorkspaceFileCache(tmp_path / "c.db"))
        ctx = {"workspace_root": str(tmp_path)}

        r1 = await ReadFileTool().execute({"path": "a.txt"}, ctx)
        assert r1["content"] == "hello world"
        assert "cached" not in r1  # first read = miss
        assert len(get_file_cache()) == 1

        r2 = await ReadFileTool().execute({"path": "a.txt"}, ctx)
        assert r2.get("cached") is True
        assert r2["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_read_file_changed_invalidates(self, tmp_path) -> None:
        from agenthicc.tools.fs import ReadFileTool

        f = tmp_path / "a.txt"
        f.write_text("old")
        configure_file_cache(WorkspaceFileCache(tmp_path / "c.db"))
        ctx = {"workspace_root": str(tmp_path)}

        await ReadFileTool().execute({"path": "a.txt"}, ctx)
        time.sleep(0.01)
        f.write_text("new content")
        r = await ReadFileTool().execute({"path": "a.txt"}, ctx)
        assert r["content"] == "new content"
        assert "cached" not in r

    @pytest.mark.asyncio
    async def test_read_file_disabled_cache_is_noop(self, tmp_path) -> None:
        from agenthicc.tools.fs import ReadFileTool

        f = tmp_path / "a.txt"
        f.write_text("hello")
        configure_file_cache(None)  # disabled
        ctx = {"workspace_root": str(tmp_path)}
        r1 = await ReadFileTool().execute({"path": "a.txt"}, ctx)
        r2 = await ReadFileTool().execute({"path": "a.txt"}, ctx)
        assert r1["content"] == r2["content"] == "hello"
        assert "cached" not in r1 and "cached" not in r2
