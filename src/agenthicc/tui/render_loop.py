"""RenderLoop — synchronous 50ms debounce render loop."""
from __future__ import annotations

import time
from typing import Any

__all__ = ["MIN_INTERVAL", "RenderLoop"]

MIN_INTERVAL: float = 0.05  # 50ms debounce


class RenderLoop:
    """Connects transcript + status -> Terminal via FrameComposer.

    Design:
    - Terminal.commit_lines() and Terminal.set_bottom() are called ONLY here.
    - Committed lines are sent to the terminal exactly once (tracked by
      _committed_count). Old lines are never re-rendered.
    - Bottom block is redrawn on every render() call.
    - tick() is debounced at MIN_INTERVAL; force_commit() bypasses it.
    """

    def __init__(
        self,
        terminal: Any,
        composer: Any,
    ) -> None:
        self.terminal = terminal
        self.composer = composer
        self._committed_count: int = 0
        self._frame_num: int = 0
        self._last_render: float = 0.0
        self._last_bottom: list[str] = []

    def render(
        self,
        transcript: Any,
        status: Any,
        input_state: Any,
    ) -> None:
        """Unconditional render. Always produces a frame and writes it."""
        cols = self.terminal.size.cols
        frame = self.composer.compose(
            transcript, status, input_state,
            cols=cols,
            frame_num=self._frame_num,
        )
        self._flush_frame(frame)
        self._last_render = time.monotonic()
        self._frame_num += 1

    def tick(
        self,
        transcript: Any,
        status: Any,
        input_state: Any,
    ) -> None:
        """Debounced render. Skip if called again within MIN_INTERVAL."""
        now = time.monotonic()
        if now - self._last_render < MIN_INTERVAL:
            return
        self.render(transcript, status, input_state)

    def force_commit(
        self,
        transcript: Any,
        status: Any,
        input_state: Any,
    ) -> None:
        """Force immediate render bypassing debounce."""
        self.render(transcript, status, input_state)

    def reset(self) -> None:
        """Reset state. Call when starting a new session or after resume."""
        self._committed_count = 0
        self._frame_num = 0
        self._last_render = 0.0
        self._last_bottom = []
        self.composer.reset()

    def _flush_frame(self, frame: Any) -> None:
        # New committed lines since last render
        new_lines = frame.committed[self._committed_count:]
        if new_lines:
            self.terminal.commit_lines(new_lines)
            self._committed_count = len(frame.committed)

        # Always refresh bottom block
        self.terminal.set_bottom(frame.bottom)
        self._last_bottom = list(frame.bottom)
