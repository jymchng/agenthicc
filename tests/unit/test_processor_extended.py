"""Extended tests for EventProcessor covering uncovered branches (PRD-01).

Targeted lines:
  processor.py: 79-80, 113-116, 128-129, 138-139, 151-152, 155-158, 182, 189-190
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agenthicc.kernel import AppState, Effect, Event, SecurityPolicy, SystemSettings
from agenthicc.kernel.processor import EventProcessor
from agenthicc.kernel.reducer import root_reducer

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path) -> SystemSettings:
    return SystemSettings(
        event_log_path=str(tmp_path / ".agenthicc" / "events.jsonl"),
        snapshot_path=str(tmp_path / ".agenthicc" / "snapshot.json"),
        max_parallel_tasks=5,
        agent_pool_size=5,
        snapshot_every_n_events=1000,
    )


def _fresh_state(tmp_path: Path) -> AppState:
    return AppState.create(
        settings=_make_settings(tmp_path),
        policy=SecurityPolicy(),
    )


def _noop_event(tag: str = "noop") -> Event:
    return Event.create(event_type=tag, payload={})


# ---------------------------------------------------------------------------
# Reducer exception is logged and processor continues (lines 113-116)
# ---------------------------------------------------------------------------


class TestReducerExceptionHandling:
    async def test_reducer_exception_does_not_crash_processor(self, tmp_path):
        """A reducer that raises on a specific event type must be swallowed."""
        boom_type = "EXPLODE"

        def exploding_reducer(state: AppState, event: Event):
            if event.event_type == boom_type:
                raise RuntimeError("boom from reducer")
            return root_reducer(state, event)

        state = _fresh_state(tmp_path)
        processor = EventProcessor(
            initial_state=state,
            reducer=exploding_reducer,
            persist=False,
        )
        task = asyncio.create_task(processor.run())
        try:
            await processor.emit(Event.create(boom_type, {}))
            await asyncio.sleep(0.05)  # Give the loop time to process
            # Processor is still running (hasn't crashed)
            assert processor._running is True
        finally:
            await processor.stop()
            await asyncio.gather(task, return_exceptions=True)

    async def test_reducer_exception_skips_event_state_unchanged(self, tmp_path):
        """After a reducer crash the state should remain as it was."""
        state = _fresh_state(tmp_path)
        original_session_id = state.session_id

        boom_type = "CRASH_ME"

        def crashing_reducer(s: AppState, event: Event):
            if event.event_type == boom_type:
                raise ValueError("intentional crash")
            return root_reducer(s, event)

        processor = EventProcessor(
            initial_state=state,
            reducer=crashing_reducer,
            persist=False,
        )
        task = asyncio.create_task(processor.run())
        try:
            await processor.emit(Event.create(boom_type, {}))
            await asyncio.sleep(0.05)
            # State should be unchanged
            assert processor.get_state().session_id == original_session_id
        finally:
            await processor.stop()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# QueueFull in subscriber notification is silently swallowed (lines 128-129)
# ---------------------------------------------------------------------------


class TestSubscriberQueueFull:
    async def test_queue_full_does_not_block_processor(self, tmp_path):
        """Fill a subscriber queue to capacity; the processor must keep running."""
        state = _fresh_state(tmp_path)
        processor = EventProcessor(
            initial_state=state,
            reducer=root_reducer,
            persist=False,
        )
        # Create a tiny subscriber queue (maxsize=1) by patching subscribe()
        # rather than mutating the read-only asyncio.Queue property.
        tiny_q: asyncio.Queue[AppState] = asyncio.Queue(maxsize=1)
        processor._readers.append(tiny_q)

        task = asyncio.create_task(processor.run())
        try:
            # Emit more events than the tiny queue can hold — no hang
            for i in range(5):
                await processor.emit(Event.create(f"TYPE_{i}", {}))
            await asyncio.sleep(0.1)
            assert processor._running is True
        finally:
            await processor.stop()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Unsubscribe (lines 79-80, 182)
# ---------------------------------------------------------------------------


class TestUnsubscribe:
    async def test_unsubscribe_removes_queue_from_readers(self, tmp_path):
        state = _fresh_state(tmp_path)
        processor = EventProcessor(
            initial_state=state,
            reducer=root_reducer,
            persist=False,
        )
        q = processor.subscribe()
        assert q in processor._readers
        processor.unsubscribe(q)
        assert q not in processor._readers

    async def test_unsubscribe_nonexistent_queue_no_error(self, tmp_path):
        state = _fresh_state(tmp_path)
        processor = EventProcessor(initial_state=state, persist=False)
        orphan_q: asyncio.Queue[AppState] = asyncio.Queue()
        # Should not raise
        processor.unsubscribe(orphan_q)

    async def test_unsubscribed_queue_does_not_receive_events(self, tmp_path):
        state = _fresh_state(tmp_path)
        processor = EventProcessor(
            initial_state=state,
            reducer=root_reducer,
            persist=False,
        )
        q = processor.subscribe()
        processor.unsubscribe(q)

        task = asyncio.create_task(processor.run())
        try:
            await processor.emit(Event.create("SOME_EVENT", {}))
            await asyncio.sleep(0.05)
            assert q.empty()
        finally:
            await processor.stop()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# stop() exits the run loop (lines 189-190)
# ---------------------------------------------------------------------------


class TestStopExitsRunLoop:
    async def test_stop_causes_run_to_return(self, tmp_path):
        state = _fresh_state(tmp_path)
        processor = EventProcessor(initial_state=state, persist=False)
        task = asyncio.create_task(processor.run())
        await asyncio.sleep(0.02)  # let it start
        await processor.stop()
        done, pending = await asyncio.wait({task}, timeout=1.0)
        assert task in done, "run() must exit after stop() is called"
        assert not pending

    async def test_stop_sets_running_false(self, tmp_path):
        state = _fresh_state(tmp_path)
        processor = EventProcessor(initial_state=state, persist=False)
        task = asyncio.create_task(processor.run())
        await asyncio.sleep(0.02)
        await processor.stop()
        await asyncio.gather(task, return_exceptions=True)
        assert processor._running is False


# ---------------------------------------------------------------------------
# persist=False — no log file created (lines 138-139, 151-152, 155-158)
# ---------------------------------------------------------------------------


class TestPersistFalseNoLogFile:
    async def test_persist_false_creates_no_event_log_file(self, tmp_path):
        state = _fresh_state(tmp_path)
        log_path = Path(state.settings.event_log_path)
        processor = EventProcessor(initial_state=state, persist=False)
        task = asyncio.create_task(processor.run())
        try:
            await processor.emit(Event.create("NOOP", {}))
            await asyncio.sleep(0.05)
        finally:
            await processor.stop()
            await asyncio.gather(task, return_exceptions=True)
        assert not log_path.exists()

    async def test_persist_true_creates_event_log_file(self, tmp_path):
        state = _fresh_state(tmp_path)
        log_path = Path(state.settings.event_log_path)
        processor = EventProcessor(initial_state=state, persist=True)
        task = asyncio.create_task(processor.run())
        try:
            await processor.emit(Event.create("NOOP", {}))
            await asyncio.sleep(0.1)
        finally:
            await processor.stop()
            await asyncio.gather(task, return_exceptions=True)
        assert log_path.exists()


# ---------------------------------------------------------------------------
# Snapshot persistence (lines 135-139 / _persist_snapshot)
# ---------------------------------------------------------------------------


class TestSnapshotPersistence:
    async def test_snapshot_written_when_threshold_reached(self, tmp_path):
        settings = SystemSettings(
            event_log_path=str(tmp_path / ".agenthicc" / "events.jsonl"),
            snapshot_path=str(tmp_path / ".agenthicc" / "snapshot.json"),
            snapshot_every_n_events=1,  # take snapshot on every event
        )
        state = AppState.create(settings=settings, policy=SecurityPolicy())
        processor = EventProcessor(initial_state=state, persist=True)
        task = asyncio.create_task(processor.run())
        try:
            await processor.emit(Event.create("NOOP", {}))
            await asyncio.sleep(0.15)
        finally:
            await processor.stop()
            await asyncio.gather(task, return_exceptions=True)
        snapshot_path = Path(state.settings.snapshot_path)
        assert snapshot_path.exists()
        data = json.loads(snapshot_path.read_text())
        assert "snapshot_index" in data
        assert "session_id" in data


# ---------------------------------------------------------------------------
# drain() timeout when run() is not started (line 89-91)
# ---------------------------------------------------------------------------


class TestSafeEffect:
    """Cover the _safe_effect error-logging branch (lines 149-152)."""

    async def test_safe_effect_swallows_exception(self, tmp_path):
        """_safe_effect must not propagate exceptions from the executor."""

        class BoomExecutor:
            async def execute(self, effect: Effect, state: AppState) -> None:
                raise RuntimeError("effect exploded")

        state = _fresh_state(tmp_path)
        processor = EventProcessor(
            initial_state=state,
            reducer=root_reducer,
            effect_executor=BoomExecutor(),
            persist=False,
        )
        effect = Effect(effect_type="spawn_agent", payload={})
        # Should not raise
        await processor._safe_effect(effect, state)

    async def test_safe_effect_calls_executor(self, tmp_path):
        """_safe_effect must call the executor's execute method."""
        executed: list[str] = []

        class TrackingExecutor:
            async def execute(self, effect: Effect, state: AppState) -> None:
                executed.append(effect.effect_type)

        state = _fresh_state(tmp_path)
        processor = EventProcessor(
            initial_state=state,
            reducer=root_reducer,
            effect_executor=TrackingExecutor(),
            persist=False,
        )
        effect = Effect(effect_type="spawn_agent", payload={})
        await processor._safe_effect(effect, state)
        assert "spawn_agent" in executed


class TestRestoreFromLog:
    """Cover restore_from_log (lines 176-191)."""

    async def test_restore_from_empty_log_returns_initial_state(self, tmp_path):
        from agenthicc.kernel.processor import restore_from_log

        log_path = str(tmp_path / "events.jsonl")
        open(log_path, "w").close()  # empty file
        state = _fresh_state(tmp_path)
        result = await restore_from_log(log_path, state)
        assert result.session_id == state.session_id

    async def test_restore_from_nonexistent_log_returns_initial_state(self, tmp_path):
        from agenthicc.kernel.processor import restore_from_log

        log_path = str(tmp_path / "does_not_exist.jsonl")
        state = _fresh_state(tmp_path)
        result = await restore_from_log(log_path, state)
        assert result.session_id == state.session_id

    async def test_restore_skips_corrupt_lines(self, tmp_path):
        from agenthicc.kernel.processor import restore_from_log

        log_path = str(tmp_path / "events.jsonl")
        with open(log_path, "w") as f:
            f.write("NOT VALID JSON\n")
            f.write("{incomplete\n")
            f.write("\n")  # blank line
        state = _fresh_state(tmp_path)
        # Should not raise and should return initial state
        result = await restore_from_log(log_path, state)
        assert result.session_id == state.session_id

    async def test_restore_replays_valid_events(self, tmp_path):
        from agenthicc.kernel.processor import restore_from_log

        log_path = str(tmp_path / "events.jsonl")
        event = Event.create("NOOP", {"data": "hello"})
        with open(log_path, "w") as f:
            import json
            f.write(json.dumps(event.to_dict()) + "\n")
        state = _fresh_state(tmp_path)
        result = await restore_from_log(log_path, state)
        # State comes back (even if NOOP event doesn't change it)
        assert result is not None


class TestDrainTimeout:
    async def test_drain_raises_timeout_error_when_no_run(self, tmp_path):
        """drain() should time out if events sit in the queue with no consumer."""
        state = _fresh_state(tmp_path)
        processor = EventProcessor(initial_state=state, persist=False)
        # Emit an event but don't start run() — queue stays non-empty
        await processor.emit(Event.create("STUCK", {}))
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await processor.drain(timeout=0.1)
