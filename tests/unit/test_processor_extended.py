"""EventProcessor failure semantics, sinks, persistence and effects (PRD-01, PRD-250 D-08)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agenthicc.kernel import AppState, Effect, Event, SecurityPolicy, SystemSettings
from agenthicc.kernel.processor import EventProcessor, restore_from_log
from agenthicc.kernel.reducer import ReducerFn, root_reducer

pytestmark = pytest.mark.unit


def _fresh_state(tmp_path: Path, *, snapshot_every: int = 1000) -> AppState:
    return AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / ".agenthicc" / "events.jsonl"),
            snapshot_path=str(tmp_path / ".agenthicc" / "snapshot.json"),
            snapshot_every_n_events=snapshot_every,
        ),
        policy=SecurityPolicy(),
    )


def _intent(intent_id: str) -> Event:
    return Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": intent_id})


@asynccontextmanager
async def _running(processor: EventProcessor) -> AsyncIterator[EventProcessor]:
    """Run *processor* for the block, then stop it and wait for run() to return."""
    task = asyncio.create_task(processor.run())
    try:
        yield processor
    finally:
        await processor.stop()
        await asyncio.wait_for(task, timeout=10)


# ── rejected events ───────────────────────────────────────────────────────────


async def test_reducer_failure_publishes_a_rejection_and_processing_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def exploding_reducer(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
        if event.event_type == "IntentStatusChanged":
            raise RuntimeError("boom from reducer")
        return root_reducer(state, event)

    reducer: ReducerFn = exploding_reducer
    processor = EventProcessor(_fresh_state(tmp_path), reducer=reducer, persist=False)
    seen: list[Event] = []
    processor.add_sink(seen.append)
    broken = Event.create("IntentStatusChanged", {"intent_id": "a", "status": "running"})

    async with _running(processor):
        await processor.emit(_intent("a"))
        await processor.emit(broken)
        await processor.emit(_intent("b"))
        await processor.drain()

    assert [event.event_type for event in seen] == [
        "IntentCreated",
        "KernelEventRejected",
        "IntentCreated",
    ]
    rejection = seen[1].payload
    assert rejection["event_id"] == broken.event_id
    assert rejection["event_type"] == "IntentStatusChanged"
    assert "boom from reducer" in str(rejection["error"])
    assert processor.rejected_count == 1
    assert processor.applied_count == 2
    assert set(processor.get_state().intents) == {"a", "b"}
    assert "rejected IntentStatusChanged" in caplog.text


async def test_an_undeclared_event_type_is_rejected_not_ignored(tmp_path: Path) -> None:
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)
    seen: list[Event] = []
    processor.add_sink(seen.append)

    async with _running(processor):
        await processor.emit(Event.create("NoSuchEvent", {}))
        await processor.drain()

    assert [event.event_type for event in seen] == ["KernelEventRejected"]
    assert "unknown kernel event type 'NoSuchEvent'" in str(seen[0].payload["error"])
    assert processor.applied_count == 0


# ── sinks and subscribers ─────────────────────────────────────────────────────


async def test_sinks_see_every_event_in_order_and_isolate_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)
    first: list[str] = []
    second: list[str] = []

    def failing_sink(_event: Event) -> None:
        raise RuntimeError("sink exploded")

    processor.add_sink(lambda event: first.append(str(event.payload["intent_id"])))
    processor.add_sink(failing_sink)
    remove_second = processor.add_sink(lambda event: second.append(str(event.payload["intent_id"])))

    async with _running(processor):
        for name in ("a", "b", "c"):
            await processor.emit(_intent(name))
        await processor.drain()
        remove_second()
        remove_second()  # removing twice is harmless
        await processor.emit(_intent("d"))
        await processor.drain()

    assert first == ["a", "b", "c", "d"]
    assert second == ["a", "b", "c"]
    sink_failures = [r for r in caplog.records if "failing_sink" in r.getMessage()]
    assert len(sink_failures) == 4


async def test_full_subscriber_queues_are_counted_and_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)
    states = processor.subscribe()
    events = processor.subscribe_events(maxsize=1)

    async with _running(processor):
        for index in range(5):
            await processor.emit(_intent(f"i{index}"))
        await processor.drain()

    assert states.qsize() == 5
    assert events.qsize() == 1
    assert processor.dropped_count == 4
    assert caplog.text.count("subscriber queue is full") == 1


async def test_unsubscribed_queues_receive_nothing(tmp_path: Path) -> None:
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)
    states = processor.subscribe()
    events = processor.subscribe_events()
    processor.unsubscribe(states)
    processor.unsubscribe_events(events)
    processor.unsubscribe(states)  # removing twice is harmless

    async with _running(processor):
        await processor.emit(_intent("a"))
        await processor.drain()

    assert states.empty()
    assert events.empty()


# ── drain and stop ────────────────────────────────────────────────────────────


async def test_drain_returns_once_every_emitted_event_is_processed(tmp_path: Path) -> None:
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)

    async with _running(processor):
        for index in range(50):
            await processor.emit(_intent(f"i{index}"))
        await processor.drain()
        assert len(processor.get_state().intents) == 50


async def test_drain_times_out_when_nothing_consumes_the_queue(tmp_path: Path) -> None:
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)
    await processor.emit(_intent("stuck"))

    with pytest.raises(TimeoutError):
        await processor.drain(timeout=0.1)


async def test_stop_returns_run_after_running_effects_finish(tmp_path: Path) -> None:
    finished: list[str] = []

    class SlowExecutor:
        async def execute(self, effect: Effect, state: AppState) -> None:
            await asyncio.sleep(0.05)
            finished.append(effect.effect_type.value)

    processor = EventProcessor(
        _fresh_state(tmp_path), effect_executor=SlowExecutor(), persist=False
    )
    async with _running(processor):
        await processor.emit(_intent("a"))
        await processor.drain()

    # IntentCreated produces an emit_signal and an update_tui effect.
    assert sorted(finished) == ["emit_signal", "update_tui"]


# ── effects ───────────────────────────────────────────────────────────────────


async def test_effect_failures_are_logged_with_their_task_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BoomExecutor:
        async def execute(self, effect: Effect, state: AppState) -> None:
            raise RuntimeError("effect exploded")

    processor = EventProcessor(
        _fresh_state(tmp_path), effect_executor=BoomExecutor(), persist=False
    )
    with caplog.at_level(logging.ERROR):
        async with _running(processor):
            await processor.emit(_intent("a"))
            await processor.drain()
            await processor.emit(_intent("b"))
            await processor.drain()

    assert set(processor.get_state().intents) == {"a", "b"}
    assert "'kernel-effect-update_tui' failed" in caplog.text
    assert "effect exploded" in caplog.text


# ── persistence ───────────────────────────────────────────────────────────────


async def test_persist_false_writes_no_log(tmp_path: Path) -> None:
    state = _fresh_state(tmp_path)
    processor = EventProcessor(state, persist=False)
    async with _running(processor):
        await processor.emit(_intent("a"))
        await processor.drain()

    assert not Path(state.settings.event_log_path).exists()


async def test_the_log_holds_applied_and_rejected_events_in_order(tmp_path: Path) -> None:
    state = _fresh_state(tmp_path)
    processor = EventProcessor(state, persist=True)
    async with _running(processor):
        await processor.emit(_intent("a"))
        await processor.emit(Event.create("NoSuchEvent", {}))
        await processor.emit(_intent("b"))
        await processor.drain()

    lines = Path(state.settings.event_log_path).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event_type"] for line in lines] == [
        "IntentCreated",
        "KernelEventRejected",
        "IntentCreated",
    ]
    restored = await restore_from_log(state.settings.event_log_path, _fresh_state(tmp_path))
    assert set(restored.intents) == {"a", "b"}


async def test_snapshot_records_the_applied_event_count(tmp_path: Path) -> None:
    state = _fresh_state(tmp_path, snapshot_every=2)
    processor = EventProcessor(state, persist=True)
    async with _running(processor):
        for name in ("a", "b", "c"):
            await processor.emit(_intent(name))
        await processor.drain()

    data = json.loads(Path(state.settings.snapshot_path).read_text(encoding="utf-8"))
    assert data == {"snapshot_index": 2, "session_id": state.session_id}


# ── restore_from_log ──────────────────────────────────────────────────────────


async def test_restore_from_a_missing_or_empty_log_returns_the_initial_state(
    tmp_path: Path,
) -> None:
    state = _fresh_state(tmp_path)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    assert await restore_from_log(str(tmp_path / "missing.jsonl"), state) is state
    assert await restore_from_log(str(empty), state) is state


async def test_restore_skips_corrupt_lines_and_refused_events(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    log_path.write_text(
        "NOT VALID JSON\n"
        "{incomplete\n"
        "\n"
        + json.dumps(Event.create("RetiredEventType", {}).to_dict())
        + "\n"
        + json.dumps(_intent("kept").to_dict())
        + "\n",
        encoding="utf-8",
    )

    restored = await restore_from_log(str(log_path), _fresh_state(tmp_path))

    assert set(restored.intents) == {"kept"}


# ── shutdown ──────────────────────────────────────────────────────────────────


async def test_stop_processes_every_event_emitted_before_it(tmp_path: Path) -> None:
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)
    task = asyncio.create_task(processor.run())
    for name in ("a", "b", "c"):
        processor.emit_nowait(_intent(name))
    await processor.stop()
    await processor.stop()  # a repeated request is a no-op
    await asyncio.wait_for(task, timeout=10)

    assert set(processor.get_state().intents) == {"a", "b", "c"}
    restarted = asyncio.create_task(processor.run())
    processor.emit_nowait(_intent("d"))
    await processor.drain()
    assert "d" in processor.get_state().intents  # no stale stop marker remains
    await processor.stop()
    await asyncio.wait_for(restarted, timeout=10)


async def test_cancelling_run_stops_it_even_when_an_event_arrives_at_once(
    tmp_path: Path,
) -> None:
    """A cancellation racing an arriving event must not be lost.

    ``asyncio.wait_for`` on Python 3.11 returns the inner result when it
    completes in the same iteration as an outer cancellation (gh-86296); a run
    loop built on it then keeps running after its owner has left.
    """
    processor = EventProcessor(_fresh_state(tmp_path), persist=False)
    for attempt in range(50):
        task = asyncio.create_task(processor.run())
        await asyncio.sleep(0)  # let run() block on the queue
        processor.emit_nowait(_intent(f"race-{attempt}"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
