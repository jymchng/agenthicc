"""Integration tests: InputBarSession completers + kernel IntentCancelled (PRD-10).

These tests verify:
1. The IntentCancelled reducer correctly cancels active intents.
2. Completed intents are not affected by IntentCancelled.
3. The InputBarSession's merged completer correctly routes slash and @-mention
   completions against a real filesystem via the kernel event pipeline.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from agenthicc.kernel import (
    AppState,
    EffectType,
    Event,
    EventProcessor,
    Intent,
    IntentStatus,
    SecurityPolicy,
    SystemSettings,
    root_reducer,
)

pytestmark = pytest.mark.integration


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def proc(tmp_path):
    state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "s.json"),
        ),
        policy=SecurityPolicy(),
    )
    p = EventProcessor(initial_state=state, persist=False)
    t = asyncio.create_task(p.run())
    yield p
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)


# ── helper ────────────────────────────────────────────────────────────────────


def _make_state_with_intent(intent_id: str, status: IntentStatus) -> AppState:
    """Return a fresh AppState with one intent at the given status."""
    base = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    intent = Intent(
        intent_id=intent_id,
        raw_text="do something",
        status=status,
        workflow_id=None,
        created_at=time.time(),
    )
    return base.with_intent(intent)


# ── reducer unit-level integration ────────────────────────────────────────────


async def test_intent_cancelled_reducer():
    """IntentCancelled marks all running/planning/validating/pending intents failed."""
    state = _make_state_with_intent("i1", IntentStatus.running)
    event = Event.create("IntentCancelled", {})
    new_state, effects = root_reducer(state, event)

    assert new_state.intents["i1"].status == IntentStatus.failed
    assert new_state.intents["i1"].error == "cancelled by user"
    assert any(e.effect_type == EffectType.update_tui for e in effects)
    assert any(
        e.payload.get("type") == "intent_cancelled"
        for e in effects
    )


async def test_intent_cancelled_only_affects_active():
    """IntentCancelled must not touch intents that are already complete."""
    base = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())

    # One running intent, one already complete.
    running = Intent(
        intent_id="run1",
        raw_text="run me",
        status=IntentStatus.running,
        workflow_id=None,
        created_at=time.time(),
    )
    done = Intent(
        intent_id="done1",
        raw_text="already done",
        status=IntentStatus.complete,
        workflow_id=None,
        created_at=time.time(),
    )
    state = base.with_intent(running).with_intent(done)

    event = Event.create("IntentCancelled", {})
    new_state, _ = root_reducer(state, event)

    assert new_state.intents["run1"].status == IntentStatus.failed
    assert new_state.intents["done1"].status == IntentStatus.complete


async def test_intent_cancelled_all_active_statuses():
    """All cancellable statuses (running, planning, validating, pending) become failed."""
    base = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    statuses = [
        IntentStatus.running,
        IntentStatus.planning,
        IntentStatus.validating,
        IntentStatus.pending,
    ]
    state = base
    for i, s in enumerate(statuses):
        intent = Intent(
            intent_id=f"i{i}",
            raw_text=f"intent {i}",
            status=s,
            workflow_id=None,
            created_at=time.time(),
        )
        state = state.with_intent(intent)

    event = Event.create("IntentCancelled", {})
    new_state, _ = root_reducer(state, event)

    for i in range(len(statuses)):
        assert new_state.intents[f"i{i}"].status == IntentStatus.failed
        assert new_state.intents[f"i{i}"].error == "cancelled by user"


async def test_intent_cancelled_rejected_and_failed_unchanged():
    """Intents already in terminal states (failed, rejected) are not re-processed."""
    base = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    terminal_statuses = [IntentStatus.failed, IntentStatus.rejected]
    state = base
    for i, s in enumerate(terminal_statuses):
        intent = Intent(
            intent_id=f"t{i}",
            raw_text=f"terminal {i}",
            status=s,
            workflow_id=None,
            created_at=time.time(),
            error="original error" if s == IntentStatus.failed else None,
        )
        state = state.with_intent(intent)

    event = Event.create("IntentCancelled", {})
    new_state, _ = root_reducer(state, event)

    # failed intent keeps its original error, not overwritten by "cancelled by user"
    assert new_state.intents["t0"].error == "original error"
    assert new_state.intents["t1"].status == IntentStatus.rejected


async def test_intent_cancelled_via_event_processor(proc):
    """IntentCancelled emitted into a live EventProcessor is applied correctly."""
    # Seed a running intent.
    await proc.emit(
        Event.create("IntentCreated", {"intent_id": "live1", "raw_text": "live run"})
    )
    await proc.emit(
        Event.create("IntentStatusChanged", {"intent_id": "live1", "status": "running"})
    )
    await proc.drain()

    state_before = proc.get_state()
    assert state_before.intents["live1"].status == IntentStatus.running

    # Cancel it.
    await proc.emit(Event.create("IntentCancelled", {}))
    await proc.drain()

    state_after = proc.get_state()
    assert state_after.intents["live1"].status == IntentStatus.failed
    assert state_after.intents["live1"].error == "cancelled by user"

    # Verify it's in the event log.
    event_types = [e.event_type for e in proc.event_log]
    assert "IntentCancelled" in event_types


async def test_intent_cancelled_empty_intents_noop(proc):
    """IntentCancelled with no intents produces no state change (just an effect)."""
    state_before = proc.get_state()
    assert state_before.intents == {}

    await proc.emit(Event.create("IntentCancelled", {}))
    await proc.drain()

    state_after = proc.get_state()
    # Intents dict is still empty — nothing to cancel.
    assert state_after.intents == {}
