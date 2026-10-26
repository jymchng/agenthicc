"""Frame scheduling and frame statistics for the live block (AGX-11).

Signals request frames instead of redrawing. :class:`FrameScheduler` merges
every request made before the next frame into one render and renders at most
once per ``interval_s``; an *immediate* request (key echo) renders on the next
event-loop iteration even inside the interval. :class:`FrameStats` keeps
bounded samples of render and write cost for ``/perf`` and the AGX-14
benchmarks.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["FrameScheduler", "FrameStats"]

_SAMPLES = 512


def _percentile(samples: deque[float] | deque[int], fraction: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return float(ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.5))])


@dataclass
class FrameStats:
    """Counts and bounded latency samples for the frames a session drew."""

    frames: int = 0
    full_redraws: int = 0
    characters: int = 0
    render_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_SAMPLES))
    write_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_SAMPLES))
    frame_characters: deque[int] = field(default_factory=lambda: deque(maxlen=_SAMPLES))

    def record_render(self, seconds: float) -> None:
        """Record the cost of building one frame's lines."""
        self.render_ms.append(seconds * 1_000.0)

    def record(self, seconds: float, characters: int, *, full: bool) -> None:
        """Record one written frame: diff and write time and characters written."""
        self.frames += 1
        self.full_redraws += int(full)
        self.characters += characters
        self.write_ms.append(seconds * 1_000.0)
        self.frame_characters.append(characters)

    def summary(self) -> dict[str, float]:
        """Return counts and p50/p95 values, for ``/perf``."""
        return {
            "frames": float(self.frames),
            "full_redraws": float(self.full_redraws),
            "render_p50_ms": _percentile(self.render_ms, 0.50),
            "render_p95_ms": _percentile(self.render_ms, 0.95),
            "write_p50_ms": _percentile(self.write_ms, 0.50),
            "write_p95_ms": _percentile(self.write_ms, 0.95),
            "characters_p50": _percentile(self.frame_characters, 0.50),
            "characters_p95": _percentile(self.frame_characters, 0.95),
        }


class FrameScheduler:
    """Coalesce redraw requests into at most one frame per interval."""

    def __init__(self, render: Callable[[], None], *, interval_s: float = 0.016) -> None:
        self._render = render
        self.interval_s = interval_s
        self._handle: asyncio.Handle | None = None
        self._immediate = False
        self._last_frame = -math.inf
        #: Redraw requests received (each frame merges one or more).
        self.requests = 0

    @property
    def pending(self) -> bool:
        return self._handle is not None

    def request(self, *, immediate: bool = False) -> None:
        """Ask for a frame; *immediate* renders on the next loop iteration."""
        self.requests += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (start-up, shutdown, synchronous callers): draw now.
            self.cancel()
            self._flush()
            return
        if self._handle is not None:
            if not immediate or self._immediate:
                return
            # Key echo pulls a frame waiting out the interval forward.
            self._handle.cancel()
        delay = 0.0 if immediate else self._last_frame + self.interval_s - time.monotonic()
        if delay <= 0.0:
            self._immediate = True
            self._handle = loop.call_soon(self._flush)
        else:
            self._immediate = False
            self._handle = loop.call_later(delay, self._flush)

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _flush(self) -> None:
        self._handle = None
        self._last_frame = time.monotonic()
        self._render()
