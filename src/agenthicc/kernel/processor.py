"""EventProcessor — MPSC event loop applying reducers sequentially (PRD-01)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Protocol, runtime_checkable

from .events import Effect, Event
from .reducer import ReducerFn, root_reducer
from .state import AppState

__all__ = [
    "EffectExecutor",
    "EventProcessor",
    "NoOpEffectExecutor",
    "restore_from_log",
]

logger = logging.getLogger(__name__)


@runtime_checkable
class EffectExecutor(Protocol):
    async def execute(self, effect: Effect, state: AppState) -> None: ...


class NoOpEffectExecutor:
    async def execute(self, effect: Effect, state: AppState) -> None:
        return None


class EventProcessor:
    """Multi-producer single-consumer event loop.

    Producers call :meth:`emit`; a single :meth:`run` task dequeues events,
    applies the reducer, persists the event to the append-only log, notifies
    snapshot subscribers, and schedules effects.
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
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._readers: list[asyncio.Queue[AppState]] = []
        self._event_log: list[Event] = []
        self._events_since_snapshot = 0
        self._running = False
        self._idle = asyncio.Event()
        self._idle.set()

    # ── reads ────────────────────────────────────────────────────────────

    def get_state(self) -> AppState:
        return self._state

    @property
    def event_log(self) -> list[Event]:
        return list(self._event_log)

    def subscribe(self) -> asyncio.Queue[AppState]:
        q: asyncio.Queue[AppState] = asyncio.Queue(maxsize=100)
        self._readers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[AppState]) -> None:
        try:
            self._readers.remove(q)
        except ValueError:
            pass

    # ── writes ───────────────────────────────────────────────────────────

    async def emit(self, event: Event) -> None:
        self._idle.clear()
        await self._queue.put(event)

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait until the queue is empty and the last event was applied."""
        async with asyncio.timeout(timeout):
            while not self._queue.empty() or not self._idle.is_set():
                await asyncio.sleep(0.001)

    # ── loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        log_file = None
        if self._persist:
            log_path = self._state.settings.event_log_path
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            log_file = open(log_path, "a")  # noqa: SIM115
        try:
            while self._running:
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except TimeoutError:
                    self._idle.set()
                    continue

                try:
                    new_state, effects = self._reducer(self._state, event)
                except Exception:
                    logger.exception("reducer failed for event %s", event.event_type)
                    self._queue.task_done()
                    continue

                self._state = new_state
                self._event_log.append(event)

                if log_file is not None:
                    log_file.write(json.dumps(event.to_dict()) + "\n")
                    log_file.flush()

                for reader in self._readers:
                    try:
                        reader.put_nowait(new_state)
                    except asyncio.QueueFull:
                        pass

                for effect in effects:
                    asyncio.ensure_future(self._safe_effect(effect, new_state))

                self._events_since_snapshot += 1
                if self._persist and (
                    self._events_since_snapshot >= self._state.settings.snapshot_every_n_events
                ):
                    self._events_since_snapshot = 0
                    asyncio.ensure_future(self._persist_snapshot(new_state))

                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()
        finally:
            if log_file is not None:
                log_file.close()

    async def _safe_effect(self, effect: Effect, state: AppState) -> None:
        try:
            await self._effects.execute(effect, state)
        except Exception:
            logger.exception("effect %s failed", effect.effect_type)

    async def _persist_snapshot(self, state: AppState) -> None:
        path = state.settings.snapshot_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(
                {"snapshot_index": len(self._event_log), "session_id": state.session_id},
                f,
            )

    async def stop(self) -> None:
        self._running = False


async def restore_from_log(
    log_path: str,
    initial_state: AppState,
    reducer: ReducerFn = root_reducer,
) -> AppState:
    """Rebuild AppState by replaying the JSON-lines event log.

    Corrupt trailing lines (from a crash mid-write) are skipped.
    """
    state = initial_state
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = Event.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    logger.warning("skipping corrupt event log line")
                    continue
                state, _ = reducer(state, event)
    except FileNotFoundError:
        pass
    return state
