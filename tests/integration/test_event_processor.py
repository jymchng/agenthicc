"""Integration tests for EventProcessor: ordering, persistence, replay (PRD-01)."""

from __future__ import annotations

import asyncio
import json

import pytest

from agenthicc.kernel import (
    AppState,
    Event,
    EventProcessor,
    SecurityPolicy,
    SystemSettings,
    restore_from_log,
    root_reducer,
)

pytestmark = pytest.mark.integration


async def test_sequential_processing(running_processor):
    for i in range(10):
        await running_processor.emit(
            Event.create("IntentCreated", {"intent_id": f"i{i}", "raw_text": f"task {i}"})
        )
    await running_processor.drain()
    state = running_processor.get_state()
    assert len(state.intents) == 10


async def test_concurrent_producers(running_processor):
    async def produce(prefix: str, n: int) -> None:
        for i in range(n):
            await running_processor.emit(
                Event.create("IntentCreated", {"intent_id": f"{prefix}-{i}", "raw_text": "x"})
            )

    await asyncio.gather(produce("a", 30), produce("b", 30), produce("c", 30))
    await running_processor.drain()
    assert len(running_processor.get_state().intents) == 90


async def test_subscriber_receives_snapshots(running_processor):
    sub = running_processor.subscribe()
    await running_processor.emit(
        Event.create("IntentCreated", {"intent_id": "watch", "raw_text": "observed"})
    )
    received = await asyncio.wait_for(sub.get(), timeout=2.0)
    assert "watch" in received.intents


async def test_event_log_written_to_disk(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    state = AppState.create(
        settings=SystemSettings(event_log_path=log_path, snapshot_path=str(tmp_path / "s.json")),
        policy=SecurityPolicy(),
    )
    processor = EventProcessor(initial_state=state, persist=True)
    task = asyncio.create_task(processor.run())

    for i in range(5):
        await processor.emit(Event.create("IntentCreated", {"intent_id": f"i{i}", "raw_text": "x"}))
    await processor.drain()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    with open(log_path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 5
    assert all(line["event_type"] == "IntentCreated" for line in lines)


async def test_restore_from_log_replays_state(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    settings = SystemSettings(event_log_path=log_path, snapshot_path=str(tmp_path / "s.json"))
    initial = AppState.create(settings=settings, policy=SecurityPolicy())

    events = [
        Event.create("IntentCreated", {"intent_id": f"r{i}", "raw_text": "test"})
        for i in range(7)
    ]
    with open(log_path, "w") as f:
        for event in events:
            f.write(json.dumps(event.to_dict()) + "\n")

    restored = await restore_from_log(log_path, initial, root_reducer)
    for i in range(7):
        assert f"r{i}" in restored.intents


async def test_restore_skips_corrupt_tail(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    settings = SystemSettings(event_log_path=log_path, snapshot_path=str(tmp_path / "s.json"))
    initial = AppState.create(settings=settings, policy=SecurityPolicy())

    with open(log_path, "w") as f:
        for i in range(3):
            event = Event.create("IntentCreated", {"intent_id": f"ok-{i}", "raw_text": "x"})
            f.write(json.dumps(event.to_dict()) + "\n")
        f.write('{"event_id": "bad", "event_type": "Trunc')  # crash mid-write

    restored = await restore_from_log(log_path, initial, root_reducer)
    assert len(restored.intents) == 3


async def test_replay_is_deterministic(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    settings = SystemSettings(event_log_path=log_path, snapshot_path=str(tmp_path / "s.json"))
    initial = AppState.create(settings=settings, policy=SecurityPolicy())

    with open(log_path, "w") as f:
        for i in range(20):
            event = Event.create("IntentCreated", {"intent_id": f"det-{i}", "raw_text": f"task {i}"})
            f.write(json.dumps(event.to_dict()) + "\n")

    r1 = await restore_from_log(log_path, initial, root_reducer)
    r2 = await restore_from_log(log_path, initial, root_reducer)
    assert set(r1.intents.keys()) == set(r2.intents.keys())
    for intent_id in r1.intents:
        assert r1.intents[intent_id].raw_text == r2.intents[intent_id].raw_text


async def test_harness_captures_events(harness):
    await harness.processor.emit(Event.create("IntentCreated", {"intent_id": "h1", "raw_text": "x"}))
    captured = await harness.wait_for_event("IntentCreated")
    assert captured.payload["intent_id"] == "h1"
