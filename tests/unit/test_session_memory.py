"""Unit tests for SessionMemoryLayer (PRD-05 tier 1)."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.memory import SessionMemoryLayer

pytestmark = pytest.mark.unit


class TestLRUEviction:
    async def test_evicts_lru_entry_at_max_entries(self) -> None:
        layer = SessionMemoryLayer(max_entries=3)
        await layer.set("a", 1)
        await layer.set("b", 2)
        await layer.set("c", 3)
        # Touch "a" so it becomes most-recently-used.
        assert layer.get("a") == (True, 1)
        # Adding "d" must evict "b" (the LRU entry).
        await layer.set("d", 4)
        assert len(layer) == 3
        assert layer.get("b") == (False, None)
        assert layer.get("a") == (True, 1)
        assert layer.get("c") == (True, 3)
        assert layer.get("d") == (True, 4)

    async def test_no_eviction_below_capacity(self) -> None:
        layer = SessionMemoryLayer(max_entries=5)
        for i in range(5):
            await layer.set(f"k{i}", i)
        for i in range(5):
            assert layer.get(f"k{i}") == (True, i)

    async def test_capacity_is_bounded_under_overflow(self) -> None:
        layer = SessionMemoryLayer(max_entries=4)
        for i in range(20):
            await layer.set(f"k{i}", i)
        assert len(layer) == 4
        # Only the four most recent keys survive.
        for i in range(16, 20):
            assert layer.get(f"k{i}") == (True, i)

    async def test_rewrite_moves_key_to_mru(self) -> None:
        layer = SessionMemoryLayer(max_entries=2)
        await layer.set("x", 1)
        await layer.set("y", 2)
        await layer.set("x", 99)  # x becomes MRU
        await layer.set("z", 3)  # evicts y
        assert layer.get("x") == (True, 99)
        assert layer.get("y") == (False, None)
        assert layer.get("z") == (True, 3)


class TestTTLExpiry:
    async def test_entry_expires_after_ttl(self) -> None:
        layer = SessionMemoryLayer(max_entries=10)
        await layer.set("volatile", "soon-gone", ttl=0.01)
        assert layer.get("volatile") == (True, "soon-gone")
        await asyncio.sleep(0.02)
        assert layer.get("volatile") == (False, None)
        # Lazy eviction removed the entry from the cache entirely.
        assert len(layer) == 0

    async def test_entry_without_ttl_never_expires(self) -> None:
        layer = SessionMemoryLayer(max_entries=10)
        await layer.set("stable", "here")
        await asyncio.sleep(0.02)
        assert layer.get("stable") == (True, "here")

    async def test_prune_expired_removes_only_expired(self) -> None:
        layer = SessionMemoryLayer(max_entries=10)
        await layer.set("dead", 1, ttl=0.01)
        await layer.set("alive", 2, ttl=60)
        await asyncio.sleep(0.02)
        assert await layer.prune_expired() == 1
        assert layer.get("alive") == (True, 2)
        assert layer.get("dead") == (False, None)


class TestConcurrentWrites:
    async def test_fifty_concurrent_writes_all_present(self) -> None:
        layer = SessionMemoryLayer(max_entries=100)
        await asyncio.gather(*(layer.set(f"key-{i}", i) for i in range(50)))
        assert len(layer) == 50
        for i in range(50):
            assert layer.get(f"key-{i}") == (True, i)

    async def test_concurrent_writes_to_same_key_leave_one_entry(self) -> None:
        layer = SessionMemoryLayer(max_entries=100)
        await asyncio.gather(*(layer.set("shared", i) for i in range(50)))
        assert len(layer) == 1
        found, value = layer.get("shared")
        assert found and value in range(50)


class TestOverwrite:
    async def test_overwrite_updates_value(self) -> None:
        layer = SessionMemoryLayer(max_entries=10)
        await layer.set("k", "old")
        await layer.set("k", "new")
        assert layer.get("k") == (True, "new")
        assert len(layer) == 1

    async def test_overwrite_resets_ttl(self) -> None:
        layer = SessionMemoryLayer(max_entries=10)
        await layer.set("k", "v1", ttl=0.01)
        await layer.set("k", "v2")  # no TTL anymore
        await asyncio.sleep(0.02)
        assert layer.get("k") == (True, "v2")


class TestNamespaces:
    async def test_namespace_isolation(self) -> None:
        layer = SessionMemoryLayer(max_entries=10)
        await layer.set("key", "a-value", namespace="ns-a")
        await layer.set("key", "b-value", namespace="ns-b")
        assert layer.get("key", namespace="ns-a") == (True, "a-value")
        assert layer.get("key", namespace="ns-b") == (True, "b-value")
        assert layer.get("key", namespace="ns-c") == (False, None)
