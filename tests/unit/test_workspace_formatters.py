"""Presentation-only workspace formatting contracts."""

from __future__ import annotations

import pytest

from agenthicc.subagents.pool import SubagentPoolState, WorkerState
from agenthicc.tui.workspace.formatters import (
    compact_token_count,
    cost_label,
    pool_status_counts,
    usage_quality_label,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1.0k"),
        (9_999, "10.0k"),
        (10_000, "10.0k"),
        (999_499, "999.5k"),
        (999_999, "1M"),
        (1_000_000, "1M"),
        (999_999_999, "1B"),
        (1_000_000_000, "1B"),
        (1_500_000_000, "1.5B"),
    ],
)
def test_compact_token_count_uses_half_up_rounding(count: int, expected: str) -> None:
    assert compact_token_count(count) == expected


def test_compact_token_count_does_not_mutate_exact_input() -> None:
    exact = 230_044_280
    assert compact_token_count(exact) == "230M"
    assert exact == 230_044_280


@pytest.mark.parametrize(
    ("status", "visual", "spoken"),
    [
        ("complete", "", ""),
        ("partial", "?", "usage partial"),
        ("unavailable", "usage unavailable", "usage unavailable"),
        ("unknown", "usage unavailable", "usage unavailable"),
    ],
)
def test_usage_quality_labels_are_explicit_and_accessible(
    status: str, visual: str, spoken: str
) -> None:
    assert usage_quality_label(status, screen_reader=False, compact=True) == visual
    assert usage_quality_label(status, screen_reader=True, compact=True) == spoken


def test_cost_provenance_is_separate_from_usage_quality() -> None:
    assert cost_label(0.1234, "estimated", screen_reader=False) == "~$0.123"
    assert cost_label(0.1234, "estimated", screen_reader=True) == "estimated cost $0.123"
    assert cost_label(0.1234, "authoritative", screen_reader=False) == "$0.123"
    assert cost_label(0.1234, "unavailable", screen_reader=True) == "cost unknown"


def test_pool_counts_reconcile_each_worker_status() -> None:
    pool = SubagentPoolState(
        "pool",
        6,
        (
            WorkerState("a", "explorer", "pending"),
            WorkerState("b", "explorer", "running"),
            WorkerState("c", "explorer", "waiting"),
            WorkerState("d", "explorer", "done"),
            WorkerState("e", "explorer", "failed"),
            WorkerState("f", "explorer", "stopped"),
        ),
    )
    counts = pool_status_counts(pool)
    assert (
        counts.running,
        counts.queued,
        counts.needs_input,
        counts.done,
        counts.failed,
        counts.stopped,
    ) == (1, 1, 1, 1, 1, 1)
