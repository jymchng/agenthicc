"""Measure event-loop lag and name what caused each stall (TUIR-04).

Everything in the TUI — keys, frames, the subagent panel and every worker's
agent loop — shares one asyncio loop, so the TUI is unresponsive exactly when
that loop is blocked. :class:`LoopLagMonitor` measures this directly:

- a sampler task sleeps :data:`SAMPLE_INTERVAL_S` and records how late it
  woke; those samples feed ``/perf``'s p50, p95 and maximum;
- a watchdog thread notices when the sampler has not ticked for
  :data:`STALL_THRESHOLD_S`, and takes one snapshot of the loop thread's
  stack. The stack names the code that is holding the loop;
- a :data:`gc.callbacks` entry times garbage collections. A collection holds
  the GIL, so it stalls the loop from whichever thread triggered it, while
  the loop thread's stack shows it idle in ``select``. A stall that was mostly
  collection is attributed to it instead.

A stall the watchdog could not observe, because the GIL was held throughout,
is still recorded by the sampler when it wakes, without frames.

The watchdog only reads frames and writes to a lock-protected ring buffer; it
never touches loop state.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Final

__all__ = ["SAMPLE_INTERVAL_S", "STALL_THRESHOLD_S", "LoopLagMonitor", "Stall"]

#: How often the sampler wakes. Cheap: well under 1 % CPU when idle.
SAMPLE_INTERVAL_S: Final = 0.05
#: A loop that has not ticked for this long is stalled.
STALL_THRESHOLD_S: Final = 0.25
_SAMPLES: Final = 1_024
_STALLS: Final = 5
#: Frames from these paths are skipped when naming a stall's culprit.
_PLUMBING: Final = ("/asyncio/", "/threading.py", "/selectors.py", "/agenthicc/tui/loop_lag.py")


@dataclass(slots=True)
class Stall:
    """One stall: when it started, how long it lasted, and where the loop was."""

    started_at: float
    duration_s: float = 0.0
    frames: list[str] = field(default_factory=list)
    #: Seconds of garbage collection during the stall.
    gc_s: float = 0.0

    @property
    def culprit(self) -> str:
        """What held the loop.

        Garbage collection when it took at least half the stall, otherwise
        the innermost non-plumbing frame as ``file:line in function``.
        """
        if self.gc_s > 0 and self.gc_s * 2 >= self.duration_s:
            return f"garbage collection ({self.gc_s * 1000:.0f} ms)"
        for frame in reversed(self.frames):
            if not any(part in frame for part in _PLUMBING):
                return frame
        return self.frames[-1] if self.frames else "unknown"


class LoopLagMonitor:
    """Sample loop lag on the loop; catch stalls from a watchdog thread."""

    def __init__(
        self,
        *,
        interval_s: float = SAMPLE_INTERVAL_S,
        threshold_s: float = STALL_THRESHOLD_S,
    ) -> None:
        self._interval = interval_s
        self._threshold = threshold_s
        self._lags: deque[float] = deque(maxlen=_SAMPLES)
        self._stalls: deque[Stall] = deque(maxlen=_STALLS)
        self._lock = threading.Lock()
        self._last_tick = time.monotonic()
        self._current: Stall | None = None
        self._loop_thread_id = 0
        self._task: asyncio.Task[None] | None = None
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        # Written only by the gc callback, which runs with the GIL held; read
        # by the sampler. No lock: a collection can start while one is held.
        self._gc_started = 0.0
        self._gc_seconds = 0.0
        self._gc_at_tick = 0.0

    def start(self) -> None:
        """Start sampling on the running loop and watching from a thread."""
        if self._task is not None:
            return
        self._loop_thread_id = threading.get_ident()
        self._last_tick = time.monotonic()
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._sample(), name="loop-lag-monitor")
        self._watchdog = threading.Thread(target=self._watch, name="loop-lag-watchdog", daemon=True)
        self._watchdog.start()
        gc.callbacks.append(self._on_gc)

    async def stop(self) -> None:
        self._stop.set()
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            await asyncio.to_thread(watchdog.join, 1.0)

    async def _sample(self) -> None:
        while True:
            start = time.monotonic()
            await asyncio.sleep(self._interval)
            now = time.monotonic()
            lag = max(0.0, now - start - self._interval)
            collected = self._gc_seconds
            with self._lock:
                self._lags.append(lag)
                self._last_tick = now
                gc_s = collected - self._gc_at_tick
                self._gc_at_tick = collected
                if self._current is not None:
                    self._current.duration_s = now - self._current.started_at
                    self._current.gc_s = gc_s
                    self._current = None
                elif lag >= self._threshold:
                    self._stalls.append(
                        Stall(started_at=start + self._interval, duration_s=lag, gc_s=gc_s)
                    )

    def _on_gc(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._gc_started = time.monotonic()
        else:
            self._gc_seconds += time.monotonic() - self._gc_started

    def _watch(self) -> None:
        while not self._stop.wait(self._threshold / 4):
            with self._lock:
                silent = time.monotonic() - self._last_tick - self._interval
                if silent < self._threshold or self._current is not None:
                    continue
                frame = sys._current_frames().get(self._loop_thread_id)
                if frame is None:
                    continue
                stall = Stall(started_at=self._last_tick + self._interval)
                stall.frames = [
                    f"{item.filename}:{item.lineno} in {item.name}"
                    for item in traceback.extract_stack(frame)
                ]
                self._current = stall
                self._stalls.append(stall)

    def stalls(self) -> list[Stall]:
        """The most recent stalls, oldest first."""
        with self._lock:
            return list(self._stalls)

    def summary(self) -> dict[str, float]:
        """Loop lag p50/p95/max in milliseconds, for ``/perf``."""
        with self._lock:
            ordered = sorted(self._lags)
        if not ordered:
            return {"samples": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}

        def pick(fraction: float) -> float:
            return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.5))]

        return {
            "samples": float(len(ordered)),
            "p50_ms": pick(0.50) * 1_000,
            "p95_ms": pick(0.95) * 1_000,
            "max_ms": ordered[-1] * 1_000,
        }
