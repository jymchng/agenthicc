"""EventProcessor — MPSC event loop applying reducers sequentially (PRD-01)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from agenthicc.supervised_tasks import SupervisedTasks

from .events import Effect, Event
from .reducer import ReducerFn, root_reducer
from .state import AppState

__all__ = [
    "EffectExecutor",
    "EventProcessor",
    "EventSink",
    "NoOpEffectExecutor",
    "restore_from_log",
]

logger = logging.getLogger(__name__)

#: Synchronous observer of every event the processor publishes.
EventSink = Callable[[Event], None]

#: Seconds a stopping processor gives running effects before cancelling them.
_EFFECT_SHUTDOWN_GRACE_S = 5.0
#: A full subscriber queue is logged on its first drop and then every Nth.
_DROP_LOG_INTERVAL = 100


@runtime_checkable
class EffectExecutor(Protocol):
    async def execute(self, effect: Effect, state: AppState) -> None: ...


class NoOpEffectExecutor:
    async def execute(self, effect: Effect, state: AppState) -> None:
        return None


class _EventLogWriter:
    """Appends serialized events to the event log on a dedicated thread.

    The processor serializes each event on the event loop and hands the line
    to this writer, so a slow disk never blocks the loop. Lines keep their
    order, and the file is flushed whenever the writer catches up.
    """

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Closed by the writer thread once the sentinel arrives.
        self._file = open(path, "a", encoding="utf-8")
        self._lines: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._write_lines, name="agenthicc-kernel-event-log", daemon=True
        )
        self._thread.start()

    def write(self, line: str) -> None:
        self._lines.put(line)

    def _write_lines(self) -> None:
        failed = False
        try:
            while (line := self._lines.get()) is not None:
                if failed:
                    continue
                try:
                    self._file.write(line)
                    if self._lines.empty():
                        self._file.flush()
                except OSError:
                    failed = True
                    logger.exception("kernel event log write failed; later events are not logged")
        finally:
            try:
                self._file.close()
            except OSError:
                logger.exception("kernel event log close failed")

    async def aclose(self) -> None:
        """Write every queued line, then close the file."""
        self._lines.put(None)
        await asyncio.to_thread(self._thread.join)


class EventProcessor:
    """Multi-producer single-consumer event loop.

    Producers call :meth:`emit`; a single :meth:`run` task dequeues events,
    applies the reducer, persists the event to the append-only log, notifies
    sinks and subscribers, and starts effects.

    An event the reducer refuses — an undeclared type or an invalid payload —
    is not applied. The processor counts it and publishes a projection-only
    ``KernelEventRejected`` event in its place, so the refusal is logged,
    persisted and visible to subscribers instead of silently dropped.
    """

    def __init__(
        self,
        initial_state: AppState,
        reducer: ReducerFn = root_reducer,
        effect_executor: EffectExecutor | None = None,
        persist: bool = True,
    ) -> None:
        self._state = initial_state
        self._reducer = reducer
        self._effects = effect_executor or NoOpEffectExecutor()
        self._persist = persist
        # ``None`` is the stop marker ``stop()`` enqueues behind pending events.
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._readers: list[asyncio.Queue[AppState]] = []
        self._event_readers: list[asyncio.Queue[Event]] = []
        self._sinks: list[EventSink] = []
        self._tasks = SupervisedTasks("kernel event processor")
        self._applied = 0
        self._rejected = 0
        self._dropped = 0
        self._events_since_snapshot = 0
        self._stop_requested = False

    # ── reads ────────────────────────────────────────────────────────────

    def get_state(self) -> AppState:
        return self._state

    @property
    def applied_count(self) -> int:
        """Events the reducer applied since this processor was created."""
        return self._applied

    @property
    def rejected_count(self) -> int:
        """Events the reducer refused, each replaced by ``KernelEventRejected``."""
        return self._rejected

    @property
    def dropped_count(self) -> int:
        """Notifications lost because a subscriber queue was full."""
        return self._dropped

    def subscribe(self) -> asyncio.Queue[AppState]:
        q: asyncio.Queue[AppState] = asyncio.Queue(maxsize=100)
        self._readers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[AppState]) -> None:
        with contextlib.suppress(ValueError):
            self._readers.remove(q)

    def subscribe_events(self, *, maxsize: int = 1024) -> asyncio.Queue[Event]:
        """Subscribe to applied kernel events without owning reducer state."""

        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._event_readers.append(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue[Event]) -> None:
        """Remove a client-neutral event projection subscriber."""

        with contextlib.suppress(ValueError):
            self._event_readers.remove(q)

    def add_sink(self, sink: EventSink) -> Callable[[], None]:
        """Call *sink* with every published event; return a function that removes it.

        Sinks run synchronously on the processor loop after an event is
        applied and handed to the log, so they observe every event in order
        and cannot fall behind. They must be fast and must not block; an
        exception from one sink is logged and does not affect the others.
        """
        self._sinks.append(sink)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._sinks.remove(sink)

        return remove

    # ── writes ───────────────────────────────────────────────────────────

    async def emit(self, event: Event) -> None:
        await self._queue.put(event)

    def emit_nowait(self, event: Event) -> None:
        """Enqueue *event* from synchronous code, in order with every other emit.

        The queue is unbounded, so this never blocks and never drops the event.
        """
        self._queue.put_nowait(event)

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait until every event emitted so far has been processed."""
        async with asyncio.timeout(timeout):
            await self._queue.join()

    # ── loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._tasks = SupervisedTasks("kernel event processor")
        writer = (
            await asyncio.to_thread(_EventLogWriter, self._state.settings.event_log_path)
            if self._persist
            else None
        )
        try:
            # Block on the queue instead of polling it with asyncio.wait_for:
            # a cancelled get() always propagates, while wait_for on Python
            # 3.11 can drop a cancellation that races an arriving event
            # (gh-86296) and leave this loop running after its owner left.
            while True:
                event = await self._queue.get()
                try:
                    if event is None:
                        self._stop_requested = False
                        return
                    self._process(event, writer)
                finally:
                    self._queue.task_done()
        finally:
            await self._tasks.aclose(timeout=_EFFECT_SHUTDOWN_GRACE_S)
            if writer is not None:
                await writer.aclose()

    def _process(self, event: Event, writer: _EventLogWriter | None) -> None:
        try:
            new_state, effects = self._reducer(self._state, event)
        except Exception as exc:
            self._rejected += 1
            logger.exception(
                "kernel reducer rejected %s event %s", event.event_type, event.event_id
            )
            rejection = Event.create(
                "KernelEventRejected",
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                },
            )
            self._publish(rejection, None, writer)
            return

        self._state = new_state
        self._applied += 1
        self._publish(event, new_state, writer)
        for effect in effects:
            self._tasks.start(
                self._effects.execute(effect, new_state),
                name=f"kernel-effect-{effect.effect_type.value}",
            )
        self._events_since_snapshot += 1
        if self._persist and (
            self._events_since_snapshot >= new_state.settings.snapshot_every_n_events
        ):
            self._events_since_snapshot = 0
            self._tasks.start(_persist_snapshot(new_state, self._applied), name="kernel-snapshot")

    def _publish(
        self, event: Event, state: AppState | None, writer: _EventLogWriter | None
    ) -> None:
        """Log *event* and notify sinks and subscribers; *state* is None if unchanged."""
        if writer is not None:
            writer.write(json.dumps(event.to_dict()) + "\n")
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:
                logger.exception("kernel event sink %r failed for %s", sink, event.event_type)
        for event_reader in self._event_readers:
            try:
                event_reader.put_nowait(event)
            except asyncio.QueueFull:
                self._record_drop("event projection")
        if state is None:
            return
        for reader in self._readers:
            try:
                reader.put_nowait(state)
            except asyncio.QueueFull:
                self._record_drop("state")

    def _record_drop(self, kind: str) -> None:
        self._dropped += 1
        if self._dropped == 1 or self._dropped % _DROP_LOG_INTERVAL == 0:
            logger.error(
                "kernel %s subscriber queue is full; %d notifications dropped so far "
                "(the event log remains authoritative)",
                kind,
                self._dropped,
            )

    async def stop(self) -> None:
        """Ask :meth:`run` to return once every event emitted so far is processed.

        The stop marker queues behind pending events, so nothing already
        emitted is lost. Await the ``run()`` task afterwards for a clean
        shutdown. Repeated calls before ``run()`` consumes the marker are no-ops.
        """
        if not self._stop_requested:
            self._stop_requested = True
            self._queue.put_nowait(None)


async def _persist_snapshot(state: AppState, snapshot_index: int) -> None:
    payload = {"snapshot_index": snapshot_index, "session_id": state.session_id}
    await asyncio.to_thread(_write_json, state.settings.snapshot_path, payload)


def _write_json(path: str, payload: dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


async def restore_from_log(
    log_path: str,
    initial_state: AppState,
    reducer: ReducerFn = root_reducer,
) -> AppState:
    """Rebuild AppState by replaying the JSON-lines event log.

    Corrupt trailing lines (from a crash mid-write) and events the current
    reducer refuses are skipped with a warning.
    """
    state = initial_state
    for line in await asyncio.to_thread(_read_lines, log_path):
        line = line.strip()
        if not line:
            continue
        try:
            event = Event.from_dict(json.loads(line))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("skipping corrupt event log line")
            continue
        try:
            state, _ = reducer(state, event)
        except Exception:
            logger.warning(
                "skipping %s event %s that the reducer refuses",
                event.event_type,
                event.event_id,
                exc_info=True,
            )
    return state
