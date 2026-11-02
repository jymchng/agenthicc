"""Unit tests for record-only generated workflow transitions."""

from __future__ import annotations

import asyncio
import json

import pytest

from agenthicc.workflows.phase_transition import (
    MAX_TRANSITION_SUMMARY_CHARS,
    record_phase_transition,
)

pytestmark = pytest.mark.unit


def test_transition_records_fixed_outcome_and_trimmed_summary_before_signaling() -> None:
    data: dict[str, object] = {}

    class ObservedEvent(asyncio.Event):
        def set(self) -> None:
            assert data == {"outcome": "approved", "summary": "Checks passed."}
            super().set()

    event = ObservedEvent()

    result = record_phase_transition(
        event,
        data,
        outcome="approved",
        summary="  Checks passed.  ",
    )

    assert result == {
        "ok": True,
        "outcome": "approved",
        "summary_chars": len("Checks passed."),
    }
    assert event.is_set()
    assert data == {"outcome": "approved", "summary": "Checks passed."}
    json.dumps(data)


def test_transition_rejects_blank_summary_without_signaling() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}

    result = record_phase_transition(event, data, outcome="complete", summary=" \n\t ")

    assert result["ok"] is False
    assert not event.is_set()
    assert data == {}


def test_transition_bounds_oversized_summary_before_signaling() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}

    result = record_phase_transition(
        event,
        data,
        outcome="complete",
        summary=f"{'x' * MAX_TRANSITION_SUMMARY_CHARS}   {'y' * 100}",
    )

    assert result["ok"] is True
    assert event.is_set()
    assert data["summary"] == "x" * MAX_TRANSITION_SUMMARY_CHARS
    assert len(str(data["summary"])) == MAX_TRANSITION_SUMMARY_CHARS


def test_transition_is_first_successful_call_wins() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}

    first = record_phase_transition(event, data, outcome="approved", summary="first")
    second = record_phase_transition(event, data, outcome="blocked", summary="second")

    assert first["ok"] is True
    assert second["ok"] is False
    assert data == {"outcome": "approved", "summary": "first"}


@pytest.mark.parametrize("outcome", ["", "  ", "x" * 129])
def test_transition_rejects_invalid_fixed_outcome(outcome: str) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}

    result = record_phase_transition(event, data, outcome=outcome, summary="submitted")

    assert result["ok"] is False
    assert not event.is_set()
    assert data == {}
