"""Subagents run to completion: no turn, tool-call, iteration, time or budget limit."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

import agenthicc.subagents.pool as pool_module
from agenthicc.config import SubagentSettings
from agenthicc.memory.journal import ConversationJournal
from agenthicc.subagents.pool import (
    AggregatedResult,
    SubagentPool,
    SubagentResult,
    SubagentTask,
    _WorkerProgress,
)
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import DEFAULT_REGISTRY, SubagentTypeSpec

pytestmark = pytest.mark.unit

#: Far past every former limit: 256 tool calls, 128 iterations, a 15-turn explorer.
_MANY_TOOL_CALLS = 1_000
_HUGE_TOKEN_COUNT = 50_000_000


class _BusyWorker:
    """A worker that makes far more tool calls and tokens than any old limit allowed."""

    release: ClassVar[asyncio.Event]

    def __init__(self, *, task: SubagentTask, index: int, **kwargs: object) -> None:
        self._task = task
        self.label = f"{task.agent_type} #{index}"
        self._progress = cast(_WorkerProgress, kwargs["progress"])

    async def run(self) -> SubagentResult:
        for _ in range(_MANY_TOOL_CALLS):
            self._progress.tool_started("read_file", {"path": "README.md"})
        self._progress.usage(_HUGE_TOKEN_COUNT)
        await self.release.wait()
        return SubagentResult(self._task.task_id, self._task.agent_type, self.label, True, "done")


def _pool(**kwargs: object) -> SubagentPool:
    return SubagentPool(
        [SubagentTask("worker-1", "researcher", "research")],
        parent_runner=SimpleNamespace(_transport=None),
        parent_model="model",
        all_tools=[],
        max_concurrent=1,
        registry=DEFAULT_REGISTRY,
        max_workers=1,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_worker_is_never_stopped_for_how_much_it_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _BusyWorker.release = asyncio.Event()
    monkeypatch.setattr(pool_module, "SubagentWorker", _BusyWorker)
    pool = _pool()

    run = asyncio.create_task(pool.run())
    # Let the worker pass every former tool-call and iteration ceiling, then
    # keep it running well past where a per-pool time ceiling would have fired.
    await asyncio.sleep(0.05)
    assert not run.done()
    _BusyWorker.release.set()
    result = await run

    assert result.succeeded == 1
    assert result.failed == 0
    lifecycle = pool.lifecycle("worker-1")
    assert lifecycle is not None
    assert lifecycle.stop_request is None
    assert lifecycle.phase == "completed"


@pytest.mark.parametrize(
    "removed",
    [
        "max_tool_calls",
        "max_iterations",
        "max_total_time_s",
        "on_budget_exceeded",
        "budget",
    ],
)
def test_the_pool_has_no_limit_arguments(removed: str) -> None:
    with pytest.raises(TypeError, match=removed):
        _pool(**{removed: 1})


def test_no_limit_survives_in_the_pool_contracts() -> None:
    assert not hasattr(SubagentPool, "budget_exceeded")
    assert not hasattr(_WorkerProgress, "iteration_started")
    assert not hasattr(_WorkerProgress, "cost")
    assert not hasattr(ConversationJournal, "subagent_pool_stopped")

    result_fields = {field.name for field in dataclasses.fields(AggregatedResult)}
    assert not result_fields & {"budget", "budget_exceeded_reason"}

    spec_fields = {field.name for field in dataclasses.fields(SubagentTypeSpec)}
    assert "max_turns" not in spec_fields

    settings_fields = {field.name for field in dataclasses.fields(SubagentSettings)}
    assert not settings_fields & {
        "max_tool_calls",
        "max_iterations",
        "max_total_time_s",
        "max_budget",
        "on_budget_exceeded",
    }


def test_spawn_subagents_takes_no_budget() -> None:
    fn = make_spawn_subagents_tool(SimpleNamespace(_transport=None), "model", [])
    assert "budget" not in inspect.signature(fn).parameters


def test_a_reply_cut_off_at_the_output_limit_is_the_only_turn_stop() -> None:
    import sys

    assert pool_module._UNBOUNDED_TURNS == sys.maxsize
    # lauren-ai reports "max_turns" for a reply cut off at the output limit; with
    # no turn limit that is the only thing it can mean.
    assert pool_module._limit_stop_reason("max_turns") == "output_truncated"
    assert pool_module._limit_stop_reason("end_turn") == ""


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
