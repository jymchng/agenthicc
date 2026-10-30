"""Subagent pool budget enforcement and stop-policy behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import ClassVar, cast
from unittest.mock import AsyncMock, patch

import pytest

import agenthicc.subagents.pool as pool_module
from agenthicc.config import SubagentSettings
from agenthicc.subagents.pool import (
    AggregatedResult,
    SubagentPool,
    SubagentResult,
    SubagentTask,
    WorkerStopMode,
    _WorkerProgress,
)
from agenthicc.subagents.tool import (
    _find_cached_result,
    _tasks_fingerprint,
    make_spawn_subagents_tool,
)
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


class _BudgetWorker:
    mode: ClassVar[str] = "within"
    started: ClassVar[asyncio.Event]
    release: ClassVar[asyncio.Event]

    def __init__(self, *, task: SubagentTask, index: int, **kwargs: object) -> None:
        self._task = task
        self.label = f"{task.agent_type} #{index}"
        self._progress = cast(_WorkerProgress, kwargs["progress"])

    async def run(self) -> SubagentResult:
        if self.mode == "wait":
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                return SubagentResult(
                    self._task.task_id,
                    self._task.agent_type,
                    self.label,
                    False,
                    "partial output",
                    error="cancelled",
                    partial=True,
                    stop_reason="cancelled",
                )
        else:
            self._progress.iteration_started()
            if self.mode == "iterations":
                self._progress.iteration_started()
            if self.mode in {"within", "tools"}:
                self._progress.tool_started("read_file", {"path": "README.md"})
                if self.mode == "tools":
                    self._progress.tool_started("read_file", {"path": "README.md"})
            if self.mode in {"within", "tokens"}:
                self._progress.usage(10 if self.mode == "within" else 11)
        return SubagentResult(
            self._task.task_id,
            self._task.agent_type,
            self.label,
            True,
            "finished",
        )


def _prepare_worker(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    _BudgetWorker.mode = mode
    _BudgetWorker.started = asyncio.Event()
    _BudgetWorker.release = asyncio.Event()
    monkeypatch.setattr(pool_module, "SubagentWorker", _BudgetWorker)


def _pool(
    *,
    max_tool_calls: int = 1,
    max_iterations: int = 1,
    max_tokens: int = 10,
    max_total_time_s: float = 1.0,
    on_budget_exceeded: WorkerStopMode = "drain",
    stop_grace_s: float = 0.1,
) -> SubagentPool:
    return SubagentPool(
        [SubagentTask("worker-1", "researcher", "research")],
        parent_runner=SimpleNamespace(_transport=None),
        parent_model="model",
        all_tools=[],
        max_concurrent=1,
        registry=DEFAULT_REGISTRY,
        max_workers=1,
        max_tool_calls=max_tool_calls,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        max_total_time_s=max_total_time_s,
        on_budget_exceeded=on_budget_exceeded,
        stop_grace_s=stop_grace_s,
    )


@pytest.mark.parametrize(
    ("mode", "limits", "reason"),
    [
        ("iterations", {"max_iterations": 1}, "max_iterations_exceeded"),
        ("tools", {"max_tool_calls": 1}, "max_tool_calls_exceeded"),
        ("tokens", {"max_tokens": 10}, "max_tokens_exceeded"),
    ],
)
async def test_count_budget_crossings_apply_drain_policy(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    limits: Mapping[str, int],
    reason: str,
) -> None:
    _prepare_worker(monkeypatch, mode)
    pool = _pool(**limits)

    result = await pool.run()

    assert result.budget_exceeded_reason == reason
    lifecycle = pool.lifecycle("worker-1")
    assert lifecycle is not None
    assert lifecycle.stop_request is not None
    assert lifecycle.stop_request.mode == "drain"
    assert lifecycle.stop_request.source == "budget"
    assert lifecycle.stop_request.reason == reason


async def test_counts_at_the_configured_limit_do_not_exceed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_worker(monkeypatch, "within")
    pool = _pool()

    result = await pool.run()

    assert result.budget_exceeded_reason == ""
    assert result.succeeded == 1
    lifecycle = pool.lifecycle("worker-1")
    assert lifecycle is not None
    assert lifecycle.stop_request is None


async def test_time_budget_abort_cancels_worker_and_preserves_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_worker(monkeypatch, "wait")
    pool = _pool(
        max_total_time_s=0.02,
        on_budget_exceeded="abort",
        stop_grace_s=0.2,
    )
    run = asyncio.create_task(pool.run())
    await asyncio.wait_for(_BudgetWorker.started.wait(), timeout=1)

    result = await run

    assert result.budget_exceeded_reason == "max_total_time_s_exceeded"
    assert result.failed == 1
    lifecycle = pool.lifecycle("worker-1")
    assert lifecycle is not None
    assert lifecycle.phase == "cancelled"
    assert lifecycle.partial_result is not None
    assert lifecycle.partial_result.text == "partial output"
    assert lifecycle.stop_request is not None
    assert lifecycle.stop_request.mode == "abort"


async def test_time_budget_drain_allows_running_worker_to_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_worker(monkeypatch, "wait")
    pool = _pool(max_total_time_s=0.02, on_budget_exceeded="drain")

    async def release_after_budget() -> None:
        await asyncio.sleep(0.05)
        _BudgetWorker.release.set()

    release_task = asyncio.create_task(release_after_budget())
    result = await pool.run()
    await release_task

    assert result.budget_exceeded_reason == "max_total_time_s_exceeded"
    assert result.succeeded == 1
    lifecycle = pool.lifecycle("worker-1")
    assert lifecycle is not None
    assert lifecycle.phase == "completed"
    assert lifecycle.stop_request is not None
    assert lifecycle.stop_request.mode == "drain"


async def test_spawn_tool_rejects_task_lists_over_worker_limit() -> None:
    fn = make_spawn_subagents_tool(
        SimpleNamespace(_transport=None),
        "model",
        [],
        subagent_settings=SubagentSettings(max_workers=1, max_concurrent=1),
    )

    result = await fn(
        tasks=[
            {"type": "explorer", "task": "inspect one"},
            {"type": "explorer", "task": "inspect two"},
        ]
    )

    assert result["ok"] is False
    assert "max_workers=1" in result["error"]


async def test_spawn_tool_rejects_concurrency_over_configured_limit() -> None:
    fn = make_spawn_subagents_tool(
        SimpleNamespace(_transport=None),
        "model",
        [],
        subagent_settings=SubagentSettings(max_workers=2, max_concurrent=1),
    )

    result = await fn(
        tasks=[{"type": "explorer", "task": "inspect"}],
        max_concurrent=2,
    )

    assert result["ok"] is False
    assert "configured max_concurrent=1" in result["error"]


async def test_budget_limited_pool_is_not_cached_as_success() -> None:
    conversation = ConversationStore()
    conversation.begin_turn("agent", "turn-1")
    fn = make_spawn_subagents_tool(
        SimpleNamespace(_transport=None),
        "model",
        [],
        conv_store=conversation,
    )
    task_list = [{"type": "explorer", "task": "inspect"}]
    aggregate = AggregatedResult(
        pool_id="pool-budget",
        total=1,
        succeeded=1,
        failed=0,
        text="partial pool output",
        budget_exceeded_reason="max_tokens_exceeded",
    )

    with patch(
        "agenthicc.subagents.pool.SubagentPool.run",
        new=AsyncMock(return_value=aggregate),
    ):
        result = await fn(tasks=task_list)

    fingerprint = _tasks_fingerprint([SubagentTask("t0", "explorer", "inspect")])
    assert result["ok"] is False
    assert result["status"] == "budget_exceeded"
    assert result["budget_exceeded_reason"] == "max_tokens_exceeded"
    assert _find_cached_result(conversation, fingerprint) is None
