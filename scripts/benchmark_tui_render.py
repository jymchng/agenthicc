"""Measure the TUI live block's render cost (AGX-11).

Renders the real ``Workspace`` into an in-memory terminal at 120×40 and
reports, as JSON, wall-clock and CPU (``thread_time``) percentiles — CPU time
is unaffected by a loaded host preempting the process:

- keystroke echo (build, diff and write one frame after typing a character)
  for 4,000- and 10,000-character prompts and a 200-line prompt;
- the cost of a frame after an ordinary state change;
- the bytes and time of one spinner frame;
- frames drawn while idle, and CPU used while a turn animates the spinner.

Example::

    uv run python scripts/benchmark_tui_render.py --samples 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from io import StringIO

from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace import Workspace


def _percentiles(samples_ms: list[float], prefix: str = "") -> dict[str, float]:
    if not samples_ms:
        return {f"{prefix}p50_ms": 0.0, f"{prefix}p95_ms": 0.0, f"{prefix}max_ms": 0.0}
    ordered = sorted(samples_ms)

    def pick(fraction: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.5))], 3)

    return {
        f"{prefix}p50_ms": pick(0.50),
        f"{prefix}p95_ms": pick(0.95),
        f"{prefix}max_ms": round(ordered[-1], 3),
    }


class _Timer:
    """Collect wall-clock and CPU milliseconds for repeated operations."""

    def __init__(self) -> None:
        self.wall: list[float] = []
        self.cpu: list[float] = []
        self._wall = 0.0
        self._cpu = 0.0

    def __enter__(self) -> "_Timer":
        self._wall = time.perf_counter()
        self._cpu = time.thread_time()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.wall.append((time.perf_counter() - self._wall) * 1_000)
        self.cpu.append((time.thread_time() - self._cpu) * 1_000)

    def summary(self) -> dict[str, float]:
        return {**_percentiles(self.wall), **_percentiles(self.cpu, "cpu_")}


def _workspace() -> tuple[AppState, Workspace, StringIO]:
    state = AppState.create()
    state.conversation.model_name.set("anthropic/claude-sonnet-5-5")
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120, height=40)
    return state, Workspace(state, console), output


def _keystrokes(prompt: str, samples: int) -> dict[str, float]:
    state, workspace, _output = _workspace()
    workspace.start()
    try:
        buffer = list(prompt)
        state.input.buf.set(list(buffer))
        state.input.cursor.set(len(buffer))
        timer = _Timer()
        for index in range(samples):
            buffer.append("abcdefgh"[index % 8])
            # Outside an event loop each signal draws its frame at once: the
            # buffer change and the cursor move are two frames, as typed.
            with timer:
                state.input.buf.set(list(buffer))
            with timer:
                state.input.cursor.set(len(buffer))
        return timer.summary()
    finally:
        workspace.stop()


def _state_change(samples: int) -> dict[str, float]:
    state, workspace, _output = _workspace()
    workspace.start()
    try:
        timer = _Timer()
        for index in range(samples):
            state.conversation.tokens_in.set(index * 37)
            with timer:
                workspace._flush_redraw()
        return timer.summary()
    finally:
        workspace.stop()


def _spinner(samples: int) -> dict[str, float]:
    state, workspace, output = _workspace()
    workspace.start()
    try:
        state.conversation.begin_turn("agent", "turn-1")
        workspace._flush_redraw()
        sizes: list[float] = []
        timer = _Timer()
        for _ in range(samples):
            output.seek(0)
            output.truncate(0)
            state.conversation.frame.set(state.conversation.frame() + 1)
            with timer:
                workspace._flush_redraw()
            sizes.append(float(len(output.getvalue().encode())))
        result = timer.summary()
        result["bytes_p50"] = sorted(sizes)[len(sizes) // 2]
        result["bytes_max"] = max(sizes)
        return result
    finally:
        workspace.stop()


async def _loop(seconds: float, *, running: bool) -> dict[str, float]:
    """Run the workspace with the session's 10 Hz tick for *seconds*."""
    state, workspace, _output = _workspace()
    workspace.start()
    try:
        if running:
            state.conversation.begin_turn("agent", "turn-1")
        await asyncio.sleep(0.2)
        stats = workspace.frame_stats()
        assert stats is not None
        frames_before = stats.frames
        cpu_started = time.process_time()
        wall_started = time.perf_counter()
        while time.perf_counter() - wall_started < seconds:
            await asyncio.sleep(0.1)
            state.conversation.tick()
        cpu = (time.process_time() - cpu_started) / (time.perf_counter() - wall_started)
        return {"frames": float(stats.frames - frames_before), "cpu_fraction": round(cpu, 4)}
    finally:
        workspace.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--idle-seconds", type=float, default=10.0)
    args = parser.parse_args()
    lines_200 = "\n".join(f"line {index:03d} of a long multi-line prompt" for index in range(200))
    result = {
        "terminal": {"width": 120, "height": 40},
        "keystroke_4000_chars": _keystrokes("x" * 4_000, args.samples),
        "keystroke_10000_chars": _keystrokes("y " * 5_000, args.samples),
        "keystroke_200_lines": _keystrokes(lines_200, args.samples),
        "frame_after_state_change": _state_change(args.samples),
        "spinner_frame": _spinner(args.samples),
        "idle": asyncio.run(_loop(args.idle_seconds, running=False)),
        "animating": asyncio.run(_loop(args.idle_seconds, running=True)),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
