"""Clean-slate unit coverage for the PRD-157 usage ledger contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from lauren_ai._signals import AgentRunComplete, ModelCallComplete

from agenthicc.memory.journal import ConversationJournal
from agenthicc.runners.usage_ledger import (
    CostStatus,
    DurabilityStatus,
    UsageLedger,
    UsageLifecycle,
    UsageQuality,
    UsageRunTracker,
    UsageSource,
    summarize_usage_records,
)
from agenthicc.tui.conversation_store import ConversationEvent

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Usage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    price: float | None = None

    def cost_usd(self, _model: str) -> float:
        return self.price or 0.0


def _usage(inp: int | None, out: int | None, price: float | None = None) -> _Usage:
    return _Usage(inp, out, price=price)


def test_known_zero_is_not_the_same_as_unavailable() -> None:
    ledger = UsageLedger("session")

    zero_call = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(zero_call, _usage(0, 0), cost_status=CostStatus.UNAVAILABLE)
    unknown_call = ledger.begin_call(run_id="run", model="mock")

    snapshot = ledger.snapshot()
    assert snapshot.input_tokens == 0
    assert snapshot.output_tokens == 0
    assert snapshot.usage_status == UsageQuality.PARTIAL
    assert snapshot.known_calls == 1
    assert snapshot.unavailable_calls == 1
    assert snapshot.provisional_calls == 1
    assert ledger.records(include_provisional=True)[0].is_known
    assert ledger.records(include_provisional=True)[1].lifecycle == UsageLifecycle.PROVISIONAL
    assert unknown_call.record_id != zero_call.record_id


def test_tracker_reconciles_chunk_model_and_run_signals_exactly_once() -> None:
    ledger = UsageLedger("session", conversation_id="conversation")
    tracker = UsageRunTracker(ledger, run_id="run", provider="mock", model="mock-model")
    tracker.ensure_call()

    first = _usage(10, 2, price=0.10)
    tracker.observe_chunk(first)
    completion = ModelCallComplete(usage=first, cost_usd=0.10)
    tracker.on_model_complete(completion)
    tracker.on_model_complete(completion)  # duplicate per-call signal
    run_complete = AgentRunComplete(total_usage=first, total_cost_usd=0.10)
    tracker.on_run_complete(run_complete)
    tracker.on_run_complete(run_complete)  # duplicate cumulative signal

    snapshot = ledger.snapshot()
    assert snapshot.input_tokens == 10
    assert snapshot.output_tokens == 2
    assert snapshot.cost_usd == pytest.approx(0.10)
    assert len(ledger.records()) == 1
    assert ledger.records()[0].conversation_id == "conversation"
    assert ledger.records()[0].source == UsageSource.RECONCILED


def test_cumulative_run_summary_only_creates_the_missing_residual() -> None:
    ledger = UsageLedger("session")
    tracker = UsageRunTracker(ledger, run_id="run", provider="mock", model="mock-model")
    tracker.ensure_call()
    tracker.on_model_complete(ModelCallComplete(usage=_usage(10, 2), cost_usd=0.0))

    tracker.on_run_complete(AgentRunComplete(total_usage=_usage(15, 5), total_cost_usd=0.0))

    records = ledger.records()
    assert len(records) == 2
    assert sum(record.input_tokens or 0 for record in records) == 15
    assert sum(record.output_tokens or 0 for record in records) == 5
    assert records[-1].source == UsageSource.RUN_COMPLETE


def test_model_completion_without_chunks_is_known_and_duplicate_safe() -> None:
    ledger = UsageLedger("session")
    tracker = UsageRunTracker(ledger, run_id="run", provider="mock", model="mock-model")
    tracker.ensure_call()
    signal = ModelCallComplete(usage=_usage(8, 3), cost_usd=0.0)
    tracker.on_model_complete(signal)
    tracker.on_model_complete(signal)

    assert len(ledger.records()) == 1
    assert ledger.snapshot().usage_status == UsageQuality.COMPLETE
    assert ledger.snapshot().cost_status == CostStatus.UNAVAILABLE


def test_cancelled_partial_usage_is_persisted_without_fake_zero(tmp_path: Path) -> None:
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    ledger = UsageLedger("session", journal=journal)
    call = ledger.begin_call(run_id="run", model="mock")
    ledger.observe(call, _usage(12, None), source=UsageSource.CHUNK)
    ledger.finalize_run("run", lifecycle=UsageLifecycle.CANCELLED)

    record = ledger.records()[0]
    assert record.input_tokens == 12
    assert record.output_tokens is None
    assert record.lifecycle == UsageLifecycle.CANCELLED
    assert ledger.snapshot().usage_status == UsageQuality.COMPLETE
    assert ledger.snapshot().output_tokens == 0
    journal.close()


def test_journal_reopen_restores_latest_records_and_ignores_corrupt_tail(tmp_path: Path) -> None:
    path = tmp_path / "conversation-journal.jsonl"
    journal = ConversationJournal(path)
    ledger = UsageLedger("session", journal=journal)
    call = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(call, _usage(20, 4), cost_usd=0.2)
    journal.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind":"usage_record"\n')

    reopened_journal = ConversationJournal(path)
    reopened = UsageLedger("session", journal=reopened_journal)
    assert reopened.snapshot().input_tokens == 20
    assert reopened.snapshot().output_tokens == 4
    assert reopened.snapshot().durability_status == DurabilityStatus.DURABLE
    assert len(reopened.records()) == 1
    reopened_journal.close()


def test_canonical_journal_records_skip_lazy_legacy_history(tmp_path: Path) -> None:
    path = tmp_path / "conversation-journal.jsonl"
    journal = ConversationJournal(path)
    ledger = UsageLedger("session", journal=journal)
    call = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(call, _usage(20, 4), cost_usd=0.2)
    journal.close()

    def unexpected_legacy_scan():
        raise AssertionError("legacy conversation log should not be scanned")
        yield  # pragma: no cover

    reopened_journal = ConversationJournal(path)
    reopened = UsageLedger(
        "session",
        journal=reopened_journal,
        legacy_token_events=unexpected_legacy_scan(),
    )

    assert reopened.snapshot().input_tokens == 20
    assert len(reopened.records()) == 1
    reopened_journal.close()


def test_legacy_tokens_are_imported_without_becoming_false_zero() -> None:
    event = ConversationEvent(
        event_id="legacy-event",
        kind="tokens",
        payload={"input_tokens": 7, "output_tokens": 2, "cost_usd": 0.01},
        timestamp=123.0,
    )
    ledger = UsageLedger("session", legacy_token_events=[event])
    snapshot = ledger.snapshot()
    assert snapshot.input_tokens == 7
    assert snapshot.output_tokens == 2
    assert snapshot.usage_status == UsageQuality.COMPLETE
    assert ledger.records()[0].source == UsageSource.LEGACY


def test_projection_and_export_summary_share_the_same_aggregate() -> None:
    from agenthicc.tui.conversation_store import AppState

    ledger = UsageLedger("session")
    state = AppState.create()
    ledger.bind_conversation_store(state.conversation)
    call = ledger.begin_call(run_id="run", model="mock", category="compaction")
    ledger.complete(call, _usage(3, 1), cost_usd=0.03)

    assert state.conversation.tokens_in() == ledger.snapshot().input_tokens
    assert state.conversation.tokens_out() == ledger.snapshot().output_tokens
    assert state.conversation.cost_usd() == pytest.approx(ledger.snapshot().cost_usd)
    assert state.conversation.usage_calls() == 1
    assert state.conversation.usage_status() == "complete"
    summary = summarize_usage_records([record.to_dict() for record in ledger.records()])
    assert summary["input"] == 3
    assert summary["output"] == 1
    assert summary["calls"] == 1


def test_malformed_records_are_rejected_without_corrupting_the_fold(tmp_path: Path) -> None:
    path = tmp_path / "conversation-journal.jsonl"
    path.write_text(
        json.dumps(
            {
                "kind": "usage_record",
                "record": {"record_id": "bad", "session_id": "session", "created_at": "nan"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    journal = ConversationJournal(path)
    ledger = UsageLedger("session", journal=journal)
    assert ledger.records() == ()
    assert ledger.snapshot().usage_status == UsageQuality.UNAVAILABLE
    journal.close()


def test_mixed_cost_availability_is_not_reported_as_an_estimate() -> None:
    ledger = UsageLedger("session")
    priced = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(priced, _usage(4, 1), cost_usd=0.04)
    unpriced = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(unpriced, _usage(5, 1))

    summary = summarize_usage_records([record.to_dict() for record in ledger.records()])
    assert summary["cost_usd"] == pytest.approx(0.04)
    assert summary["cost_status"] == CostStatus.UNAVAILABLE


def test_failed_journal_write_surfaces_degraded_durability() -> None:
    class _BrokenJournal:
        def fold_usage_records(self) -> list[dict[str, object]]:
            return []

        def append_usage_record(self, _record: dict[str, object]) -> None:
            raise OSError("disk full")

    ledger = UsageLedger("session", journal=_BrokenJournal())  # type: ignore[arg-type]
    call = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(call, _usage(1, 1), cost_usd=0.01)
    assert ledger.snapshot().durability_status == DurabilityStatus.DEGRADED


def test_cost_reported_at_completion_is_an_estimate_not_unavailable() -> None:
    """A non-streaming call (every subagent worker) gets its cost only at completion.

    It used to inherit ``begin_call``'s ``unavailable`` placeholder, so every
    worker record carried a cost value labelled unavailable and any total that
    included a worker reported its cost as unavailable.
    """
    ledger = UsageLedger("session")
    tracker = UsageRunTracker(ledger, run_id="run", provider="mock", model="claude-sonnet-4-5")
    tracker.ensure_call()
    tracker.on_model_complete(ModelCallComplete(usage=_usage(1_000, 200), cost_usd=0.006))

    record = ledger.records()[0]
    assert record.cost_usd == pytest.approx(0.006)
    assert record.cost_status == CostStatus.ESTIMATED
    assert ledger.snapshot().cost_status == CostStatus.ESTIMATED

    explicit = ledger.begin_call(run_id="run", model="claude-sonnet-4-5")
    ledger.complete(explicit, _usage(1, 1), cost_usd=0.01, cost_status=CostStatus.AUTHORITATIVE)
    assert ledger.records()[-1].cost_status == CostStatus.AUTHORITATIVE


# ── SUBAGENT-06 / SUBAGENT-10 WP-7: per-pool accounting from the ledger ───────


def _worker_call(
    ledger: UsageLedger,
    *,
    pool_id: str,
    task_id: str,
    usage: object,
    cost: float | None = None,
    complete: bool = True,
    parent_run_id: str = "turn-1",
) -> None:
    from agenthicc.subagents.budget import worker_run_id

    call = ledger.begin_call(
        run_id=worker_run_id(parent_run_id, pool_id, task_id),
        model="claude-sonnet-4-5",
        category="subagent",
    )
    if complete:
        ledger.complete(call, usage, cost_usd=cost)
    else:
        ledger.observe(call, usage)


def test_pool_total_is_the_sum_of_its_workers_records_and_a_subset_of_the_session() -> None:
    from agenthicc.subagents.budget import pool_usage, worker_usage

    ledger = UsageLedger("session")
    _worker_call(ledger, pool_id="pool-a", task_id="task-0", usage=_usage(100, 20), cost=0.01)
    _worker_call(ledger, pool_id="pool-a", task_id="task-1", usage=_usage(300, 60), cost=0.03)
    _worker_call(ledger, pool_id="pool-a", task_id="task-1", usage=_usage(50, 5), cost=0.005)
    parent = ledger.begin_call(run_id="turn-1", model="claude-sonnet-4-5")
    ledger.complete(parent, _usage(1_000, 100), cost_usd=0.1)

    pool = pool_usage(ledger, "pool-a")

    assert (pool.input_tokens, pool.output_tokens, pool.calls) == (450, 85, 3)
    assert pool.total_tokens == 535
    assert pool.cost_usd == pytest.approx(0.045)
    assert pool.cost_status == "estimated"
    # Per-worker totals partition the pool, across a worker's repeated calls.
    first = worker_usage(ledger, "pool-a", "task-0")
    second = worker_usage(ledger, "pool-a", "task-1")
    assert first.total_tokens + second.total_tokens == pool.total_tokens
    assert second.calls == 2
    # The aggregation reads the ledger; the session total is unchanged by it.
    session = ledger.snapshot()
    assert session.input_tokens == 1_450 and session.output_tokens == 185


def test_two_pools_of_one_parent_run_have_disjoint_totals() -> None:
    from agenthicc.subagents.budget import pool_usage

    ledger = UsageLedger("session")
    _worker_call(ledger, pool_id="pool-a", task_id="task-0", usage=_usage(10, 1))
    _worker_call(ledger, pool_id="pool-b", task_id="task-0", usage=_usage(20, 2))

    assert pool_usage(ledger, "pool-a").total_tokens == 11
    assert pool_usage(ledger, "pool-b").total_tokens == 22


def test_unknown_cost_is_unavailable_not_zero() -> None:
    from agenthicc.subagents.budget import pool_usage

    ledger = UsageLedger("session")
    _worker_call(ledger, pool_id="pool-a", task_id="task-0", usage=_usage(10, 1), cost=0.01)
    _worker_call(ledger, pool_id="pool-a", task_id="task-1", usage=_usage(10, 1))

    pool = pool_usage(ledger, "pool-a")

    assert pool.cost_status == "unavailable"
    assert pool.to_dict()["cost_usd"] is None
    assert pool.total_tokens == 22


def test_provisional_calls_are_excluded_so_the_total_is_monotone() -> None:
    from agenthicc.subagents.budget import pool_usage

    ledger = UsageLedger("session")
    _worker_call(ledger, pool_id="pool-a", task_id="task-0", usage=_usage(10, 1))
    _worker_call(ledger, pool_id="pool-a", task_id="task-1", usage=_usage(500, 50), complete=False)

    before = pool_usage(ledger, "pool-a")
    assert before.total_tokens == 11 and before.calls == 1

    active = next(
        record for record in ledger.records(include_provisional=True) if record.is_provisional
    )
    from agenthicc.runners.usage_ledger import UsageCall

    call = UsageCall(active.record_id, active.run_id, active.call_index)
    ledger.complete(call, _usage(500, 50))
    ledger.complete(call, _usage(500, 50))  # a duplicate completion is not counted twice

    after = pool_usage(ledger, "pool-a")
    assert after.total_tokens == 561 and after.calls == 2
    assert after.total_tokens >= before.total_tokens


def test_no_ledger_or_no_records_reports_an_empty_unavailable_usage() -> None:
    from agenthicc.subagents.budget import PoolUsage, pool_usage

    assert pool_usage(None, "pool-a") == PoolUsage()
    assert pool_usage(UsageLedger("session"), "pool-a").cost_status == "unavailable"
