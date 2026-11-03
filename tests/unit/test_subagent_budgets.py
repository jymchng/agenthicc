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


async def test_budget_drained_pool_journals_a_stopped_record_with_its_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """SUBAGENT-10 B-1: a budget crossing is journaled as stopped, not cancelled."""
    from pathlib import Path

    from agenthicc.memory.journal import ConversationJournal

    _prepare_worker(monkeypatch, "tokens")
    journal = ConversationJournal(Path(str(tmp_path)) / "journal.jsonl")
    pool = SubagentPool(
        [SubagentTask("worker-1", "researcher", "research")],
        parent_runner=SimpleNamespace(_transport=None),
        parent_model="model",
        all_tools=[],
        max_concurrent=1,
        registry=DEFAULT_REGISTRY,
        max_workers=1,
        max_tokens=10,
        on_budget_exceeded="drain",
        conversation_journal=journal,
    )
    try:
        result = await pool.run()

        assert result.budget_exceeded_reason == "max_tokens_exceeded"
        lifecycle = next(
            record
            for record in journal.fold_subagent_pool_lifecycle()
            if record["pool_id"] == pool.pool_id
        )
        assert lifecycle["status"] == "stopped"
        assert lifecycle["kind"] == "subagent_pool_stopped"
        assert lifecycle["reason"] == "max_tokens_exceeded"
        # The drain let the running worker finish; its result is kept.
        assert lifecycle["total"] == 1
        assert lifecycle["succeeded"] == 1
        assert "finished" in str(lifecycle["text"])
    finally:
        journal.close()


# ── SUBAGENT-06 declared budgets: parsing, clamping and reporting ─────────────

from agenthicc.subagents.budget import (  # noqa: E402
    BudgetError,
    PoolBudget,
    PoolUsage,
    budget_digest,
    budget_report,
    model_has_known_price,
    parse_budget,
    resolve_budget,
)


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ({"tokens": 0}, "tokens"),
        ({"tokens": -5}, "tokens"),
        ({"tokens": 1.5}, "tokens"),
        ({"tokens": True}, "tokens"),
        ({"cost_usd": 0}, "cost_usd"),
        ({"cost_usd": float("inf")}, "cost_usd"),
        ({"cost_usd": float("nan")}, "cost_usd"),
        ({"wall_clock_s": -1}, "wall_clock_s"),
        ({"wall_clock_s": "60"}, "wall_clock_s"),
        ({"dollars": 5}, "dollars"),
        ("5000 tokens", "budget"),
    ],
)
def test_malformed_budget_is_refused_naming_the_field(raw: object, field: str) -> None:
    with pytest.raises(BudgetError) as caught:
        parse_budget(raw)
    assert caught.value.field == field
    assert str(caught.value).startswith(f"budget.{field}:")


def test_absent_and_partial_budgets_parse() -> None:
    assert parse_budget(None) == PoolBudget()
    assert not parse_budget(None).declared
    partial = parse_budget({"tokens": 1_000, "cost_usd": None})
    assert partial == PoolBudget(tokens=1_000)
    assert partial.declared


def test_declaration_lowers_operator_limits_and_never_raises_them() -> None:
    lowered = resolve_budget(
        PoolBudget(tokens=1_000, wall_clock_s=30.0),
        ceiling=PoolBudget(),
        operator_tokens=200_000,
        operator_wall_clock_s=600.0,
    )
    assert lowered.limits == PoolBudget(tokens=1_000, cost_usd=None, wall_clock_s=30.0)
    assert lowered.sources == {"tokens": "declared", "wall_clock_s": "declared"}

    raised = resolve_budget(
        PoolBudget(tokens=900_000, wall_clock_s=7_200.0),
        ceiling=PoolBudget(),
        operator_tokens=200_000,
        operator_wall_clock_s=600.0,
    )
    assert raised.limits.tokens == 200_000 and raised.limits.wall_clock_s == 600.0
    assert raised.sources == {"tokens": "operator", "wall_clock_s": "operator"}


def test_declaration_above_the_settings_ceiling_is_clamped_and_reported() -> None:
    effective = resolve_budget(
        PoolBudget(tokens=50_000, cost_usd=5.0),
        ceiling=PoolBudget(tokens=10_000, cost_usd=1.0),
        operator_tokens=200_000,
        operator_wall_clock_s=600.0,
    )
    assert effective.limits.tokens == 10_000
    assert effective.limits.cost_usd == 1.0
    assert effective.clamped == {"tokens": 50_000, "cost_usd": 5.0}
    assert effective.sources["tokens"] == "settings_ceiling"
    assert effective.sources["cost_usd"] == "settings_ceiling"
    assert effective.to_dict()["clamped_from"] == {"tokens": 50_000, "cost_usd": 5.0}


def test_report_names_the_crossed_limit_and_its_source() -> None:
    effective = resolve_budget(
        PoolBudget(tokens=1_000),
        ceiling=PoolBudget(),
        operator_tokens=200_000,
        operator_wall_clock_s=600.0,
    )
    usage = PoolUsage(input_tokens=900, output_tokens=200, calls=3)

    report = budget_report(
        usage, effective, crossed_reason="max_tokens_exceeded", stop_mode="drain"
    )

    assert report["crossed"] == {
        "reason": "max_tokens_exceeded",
        "unit": "tokens",
        "source": "declared",
        "stop_mode": "drain",
    }
    assert report["usage"]["total_tokens"] == 1_100  # type: ignore[index]
    digest = budget_digest(
        usage, effective, crossed_reason="max_tokens_exceeded", stop_mode="drain"
    )
    assert digest.startswith("[pool usage] 1,100 tokens (in 900 / out 200), cost unavailable")
    assert "limits: tokens 1,000 (declared), wall clock 600s (operator)" in digest
    assert "CROSSED max_tokens_exceeded (declared limit)" in digest
    assert "stop policy (drain)" in digest


def test_no_declaration_and_no_crossing_adds_no_digest() -> None:
    effective = resolve_budget(
        PoolBudget(), ceiling=PoolBudget(), operator_tokens=200_000, operator_wall_clock_s=600.0
    )
    assert budget_digest(PoolUsage(input_tokens=5), effective) == ""
    assert budget_digest(PoolUsage(input_tokens=5), None) == ""
    # An operator ceiling crossing is always reported, declared or not.
    assert "CROSSED max_total_time_s_exceeded (operator limit)" in budget_digest(
        PoolUsage(), effective, crossed_reason="max_total_time_s_exceeded"
    )


def test_unpriced_models_are_distinguished_from_lauren_fallback_pricing() -> None:
    assert model_has_known_price("claude-sonnet-4-5")
    assert not model_has_known_price("mock-model")
    assert not model_has_known_price("definitely-not-a-real-model")
