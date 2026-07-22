"""Unit tests for PRD-124 Phase 4 — resume caching."""

from __future__ import annotations

import pytest

from agenthicc.subagents.tool import _tasks_fingerprint, _find_cached_result
from agenthicc.subagents.pool import SubagentTask
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


# ── _tasks_fingerprint ────────────────────────────────────────────────────────


class TestTasksFingerprint:
    def _tasks(self, pairs: list[tuple[str, str]]) -> list[SubagentTask]:
        return [SubagentTask(f"t{i}", t, d) for i, (t, d) in enumerate(pairs)]

    def test_same_tasks_same_fingerprint(self) -> None:
        t1 = self._tasks([("explorer", "Find auth"), ("tester", "Write tests")])
        t2 = self._tasks([("explorer", "Find auth"), ("tester", "Write tests")])
        assert _tasks_fingerprint(t1) == _tasks_fingerprint(t2)

    def test_different_tasks_different_fingerprint(self) -> None:
        t1 = self._tasks([("explorer", "Find auth")])
        t2 = self._tasks([("explorer", "Find JWT")])
        assert _tasks_fingerprint(t1) != _tasks_fingerprint(t2)

    def test_order_insensitive(self) -> None:
        t1 = self._tasks([("explorer", "Find auth"), ("tester", "Write tests")])
        t2 = self._tasks([("tester", "Write tests"), ("explorer", "Find auth")])
        assert _tasks_fingerprint(t1) == _tasks_fingerprint(t2)

    def test_returns_16_char_hex(self) -> None:
        tasks = self._tasks([("explorer", "Find files")])
        fp = _tasks_fingerprint(tasks)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_empty_tasks_stable(self) -> None:
        fp1 = _tasks_fingerprint([])
        fp2 = _tasks_fingerprint([])
        assert fp1 == fp2


# ── _find_cached_result ───────────────────────────────────────────────────────


class TestFindCachedResult:
    def _store_with_result(self, fingerprint: str, text: str) -> ConversationStore:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.append_event(
            "subagent_pool_result",
            {
                "fingerprint": fingerprint,
                "text": text,
                "total": 2,
                "succeeded": 2,
            },
        )
        conv.close_turn()
        return conv

    def test_returns_none_when_no_conv_store(self) -> None:
        assert _find_cached_result(None, "abc") is None

    def test_returns_none_when_no_matching_event(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("a", "t1")
        conv.close_turn()
        assert _find_cached_result(conv, "nonexistent") is None

    def test_returns_cached_text_on_match(self) -> None:
        conv = self._store_with_result("fp123", "cached result text")
        result = _find_cached_result(conv, "fp123")
        assert result == "cached result text"

    def test_returns_none_on_fingerprint_mismatch(self) -> None:
        conv = self._store_with_result("fp123", "some result")
        assert _find_cached_result(conv, "fp999") is None

    def test_returns_most_recent_match(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("a", "t1")
        conv.append_event("subagent_pool_result", {"fingerprint": "fp1", "text": "old result"})
        conv.close_turn()
        conv.begin_turn("a", "t2")
        conv.append_event("subagent_pool_result", {"fingerprint": "fp1", "text": "new result"})
        conv.close_turn()
        result = _find_cached_result(conv, "fp1")
        assert result == "new result"


# ── end-to-end cache round-trip ───────────────────────────────────────────────


class TestResumeCacheRoundTrip:
    async def test_tool_caches_and_retrieves_result(self) -> None:
        """Full round-trip: run pool, cache, retrieve on second call."""
        from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415
        from agenthicc.subagents.tool import make_spawn_subagents_tool  # noqa: PLC0415

        conv = ConversationStore()
        conv.begin_turn("agent", "t1")

        class FakeRunner:
            _transport = None

        tool_fn = make_spawn_subagents_tool(FakeRunner(), "test-model", [], conv_store=conv)

        tasks = [{"type": "explorer", "task": "Find auth module"}]

        # Patch SubagentPool.run() so no real LLM call is made.
        fake_result = MagicMock()
        fake_result.pool_id = "pool-123"
        fake_result.total = 1
        fake_result.succeeded = 1
        fake_result.failed = 0
        fake_result.text = "=== explorer #1 (✓ 1.0s) ===\nFound auth.py"

        with patch(
            "agenthicc.subagents.pool.SubagentPool.run", new=AsyncMock(return_value=fake_result)
        ):
            result1 = await tool_fn(tasks=tasks)

        assert result1["ok"]
        assert result1["results"] == fake_result.text

        # Second call with same tasks: must return cached result without running pool.
        with patch(
            "agenthicc.subagents.pool.SubagentPool.run",
            new=AsyncMock(side_effect=AssertionError("pool should not run on resume")),
        ):
            result2 = await tool_fn(tasks=tasks)

        assert result2["ok"]
        assert result2["results"] == fake_result.text
        assert result2["pool_id"] == "cached"
