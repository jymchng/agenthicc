"""The compaction lifecycle, end to end through the real compactor and store.

A compaction that is one model call has nothing to measure: the row is the
spinner alone, and no progress is ever invented. One that summarises a long
history in several calls (map/reduce) reports each call as it finishes, and
that is the only thing the bar follows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from io import StringIO
from types import SimpleNamespace

import pytest
from lauren_ai._memory import ShortTermMemory
from rich.console import Console
from rich.text import Text

from agenthicc.memory.compactor import CompactionOutcome, compact_memory_with_result
from agenthicc.tui.conversation_store import (
    CompactionProgress,
    ConversationEvent,
    ConversationStore,
)
from agenthicc.tui.workspace.running_rows import compaction_bar

pytestmark = pytest.mark.integration

#: A chunk is at least 2,000 characters, so this many make a four-chunk history.
_HISTORY_CHARS = 7_500


class _Transport:
    """Answer every summarising call with a short summary, noting what it saw."""

    def __init__(
        self,
        *,
        fail_on_call: int = 0,
        on_call: Callable[[int], None] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.calls = 0
        self._fail_on_call = fail_on_call
        self._on_call = on_call
        self._delay_s = delay_s

    async def complete(self, messages, *, model, system, max_tokens, temperature, stream):
        self.calls += 1
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._on_call is not None:
            self._on_call(self.calls)
        if self.calls == self._fail_on_call:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(content="Short summary.")


def _memory(chars: int) -> ShortTermMemory:
    memory = ShortTermMemory(max_tokens=64_000)
    memory.add_user("Please review the project")
    memory._messages.append({"role": "assistant", "content": "a" * chars})
    memory.add_user("and report back")
    return memory


class _Run:
    """One compaction with every signal and event recorded."""

    def __init__(self) -> None:
        self.store = ConversationStore()
        self.events: list[ConversationEvent] = []
        self.store.on_event(self.events.append)
        self.progress: list[CompactionProgress | None] = []
        self.active: list[bool] = []
        self.store.compaction_progress.subscribe(
            lambda: self.progress.append(self.store.compaction_progress())
        )
        self.store.compaction_active.subscribe(
            lambda: self.active.append(self.store.compaction_active())
        )

    def statuses(self) -> list[dict[str, object]]:
        return [dict(e.payload) for e in self.events if e.kind == "compaction_status"]


async def _compact(
    run: _Run, transport: _Transport, *, chars: int, window: int
) -> CompactionOutcome:
    return await compact_memory_with_result(
        _memory(chars), transport, model="m", conv_store=run.store, max_input_tokens=window
    )


# ── a single call has nothing to measure ─────────────────────────────────────


async def test_a_one_call_compaction_never_reports_progress() -> None:
    run = _Run()
    transport = _Transport()
    outcome = await _compact(run, transport, chars=_HISTORY_CHARS, window=0)

    assert outcome.status == "compacted_summary"
    assert transport.calls == 1
    assert [p for p in run.progress if p is not None] == []  # nothing invented
    assert run.active == [True, False]


async def test_the_start_and_the_result_are_status_events_with_the_time_taken() -> None:
    run = _Run()
    await _compact(run, _Transport(delay_s=0.05), chars=500, window=0)

    started, result = run.statuses()
    assert started["status"] == "started" and "elapsed_s" not in started
    assert result["status"] == "compacted_summary"
    elapsed = result["elapsed_s"]
    assert isinstance(elapsed, float) and 0.04 <= elapsed < 5
    assert str(result["label"]).startswith("Compacted →")
    # Every earlier field of the event is still there.
    for key in ("before_tokens", "after_tokens", "durable", "summary_result", "recovery_reason"):
        assert key in result
    # The old plain-text lines are gone: the row is drawn from the status event.
    assert [e for e in run.events if e.kind == "system"] == []


# ── a map/reduce compaction reports each real call ──────────────────────────


async def test_a_multi_call_compaction_reports_each_call_as_it_finishes() -> None:
    run = _Run()
    seen_during: list[CompactionProgress | None] = []
    transport = _Transport(on_call=lambda _n: seen_during.append(run.store.compaction_progress()))
    # 4,100 tokens leaves 2,000-character chunks: four chunks and one merge.
    outcome = await _compact(run, transport, chars=_HISTORY_CHARS, window=4_100)

    assert outcome.status == "compacted_summary"
    chunks = transport.calls - 1
    assert chunks >= 3
    reported = [p for p in run.progress if p is not None]
    assert [(p.done, p.total) for p in reported] == [(n, chunks + 1) for n in range(chunks + 2)][
        : len(reported)
    ]
    assert reported[0].done == 0 and reported[-1].done == transport.calls
    assert reported[-1].fraction == 1.0
    fractions = [p.fraction for p in reported]
    assert fractions == sorted(fractions)
    # Each model call saw only the calls that had really finished before it.
    assert [p.done if p else 0 for p in seen_during] == list(range(transport.calls))
    # The row and the bar go away with the compaction.
    assert run.store.compaction_progress() is None and not run.store.compaction_active()


async def test_the_bar_is_drawn_only_while_the_calls_are_in_flight() -> None:
    run = _Run()
    drawn: list[bool] = []
    state = SimpleNamespace(conversation=run.store)

    def watch(_n: int) -> None:
        drawn.append(compaction_bar(state, 80) is not None)  # type: ignore[arg-type]

    await _compact(run, _Transport(on_call=watch), chars=_HISTORY_CHARS, window=4_100)
    assert drawn and all(drawn)
    assert compaction_bar(state, 80) is None  # type: ignore[arg-type]


async def test_the_algorithm_makes_the_same_calls_with_and_without_a_store() -> None:
    watched, bare = _Transport(), _Transport()
    memory_a, memory_b = _memory(_HISTORY_CHARS), _memory(_HISTORY_CHARS)
    with_store = await compact_memory_with_result(
        memory_a, watched, model="m", conv_store=ConversationStore(), max_input_tokens=4_100
    )
    without_store = await compact_memory_with_result(
        memory_b, bare, model="m", conv_store=None, max_input_tokens=4_100
    )
    assert watched.calls == bare.calls
    assert with_store.status == without_store.status == "compacted_summary"
    assert memory_a._messages == memory_b._messages


# ── failure ──────────────────────────────────────────────────────────────────


async def test_a_failed_call_ends_the_row_and_leaves_a_failure_status() -> None:
    run = _Run()
    transport = _Transport(fail_on_call=2)
    outcome = await _compact(run, transport, chars=_HISTORY_CHARS, window=4_100)

    assert outcome.status == "skipped_summary_unavailable"
    reported = [p for p in run.progress if p is not None]
    assert reported and reported[-1].done == 2  # only the calls that finished
    assert reported[-1].fraction < 1.0  # a failure never fills the bar
    assert run.statuses()[-1]["status"] == "skipped_summary_unavailable"
    assert not run.store.compaction_active() and run.store.compaction_progress() is None


async def test_a_failed_compaction_still_says_how_long_it_took() -> None:
    run = _Run()
    await _compact(run, _Transport(fail_on_call=1, delay_s=0.05), chars=500, window=0)
    result = run.statuses()[-1]
    assert result["status"] == "skipped_summary_unavailable"
    assert isinstance(result["elapsed_s"], float) and result["elapsed_s"] >= 0.04


# ── what the transcript prints from those events ────────────────────────────


async def test_the_transcript_rows_are_drawn_from_the_recorded_events() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace.appender import ScrollBufferAppender

    ok, bad = _Run(), _Run()
    await _compact(ok, _Transport(delay_s=0.02), chars=500, window=0)
    await _compact(bad, _Transport(fail_on_call=1), chars=500, window=0)
    console = Console(file=StringIO(), record=True, width=80, color_system=None, highlight=False)
    appender = ScrollBufferAppender(AppState.create(), console)
    for event in (*ok.events, *bad.events):
        appender._render_one(event)

    rows = [line for line in console.export_text().splitlines() if line.strip()]
    assert Text(rows[0]).plain.startswith(" ✓ Context refreshed · ")
    assert rows[0].endswith("s") and "tokens" in rows[0]
    assert any(row.startswith(" ✗ Context refresh failed") for row in rows)
    assert not any("Compacting" in row for row in rows)
