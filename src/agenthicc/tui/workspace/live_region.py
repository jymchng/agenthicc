"""The TUI's live block, written by line diff (AGX-11).

Rich's ``Live`` moves the cursor to the top of the block and rewrites every
line on every refresh. :class:`LiveRegion` keeps the lines it last wrote and,
for the next frame, rewrites only the lines that differ:

- a changed line is erased and rewritten in place;
- a taller block appends lines, a shorter one erases the lines it no longer
  uses;
- a width change, an invalidation (resize) or a frame in which more than half
  of the lines changed is a full redraw — the same sequence Rich emits.

Lines are Rich segment tuples rendered at exactly the terminal width, so the
terminal never wraps one and the region always knows where the cursor is: at
the end of the block's last line, as with Rich. Output printed while the region
is live (the scroll-back transcript, or anything written to stdout/stderr) goes
through the region's render hook: the block is erased, the output printed, and
the block repainted below it.

Frames are written through the console, so Rich still translates styles and
cursor controls for the terminal (including legacy Windows consoles). When the
terminal reports synchronized-output support (mode 2026), each write is
bracketed so the terminal presents it atomically.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence
from typing import TextIO, cast

from rich.console import Console, ConsoleRenderable, RenderHook
from rich.control import Control
from rich.file_proxy import FileProxy
from rich.segment import ControlType, Segment, Segments

from agenthicc.tui.workspace.frames import FrameStats

__all__ = ["Line", "LiveRegion", "lines_text"]

#: One rendered row of the block.
Line = tuple[Segment, ...]

_CR = Control(ControlType.CARRIAGE_RETURN).segment
_ERASE_LINE = Control((ControlType.ERASE_IN_LINE, 2)).segment
_NEWLINE = Segment.line()
_SYNC_BEGIN = Segment("\x1b[?2026h")
_SYNC_END = Segment("\x1b[?2026l")


def _move(from_row: int, to_row: int) -> list[Segment]:
    if to_row < from_row:
        return [Control((ControlType.CURSOR_UP, from_row - to_row)).segment]
    if to_row > from_row:
        return [Control((ControlType.CURSOR_DOWN, to_row - from_row)).segment]
    return []


def lines_text(lines: Sequence[Line]) -> list[str]:
    """Return the plain text of rendered *lines* (for diagnostics and tests)."""
    return ["".join(segment.text for segment in line if not segment.control) for line in lines]


class LiveRegion(RenderHook):
    """A live block at the bottom of the terminal, repainted by line diff."""

    def __init__(
        self,
        console: Console,
        *,
        transient: bool = True,
        synchronized_output: Callable[[], bool] = lambda: False,
        full_redraw_ratio: float = 0.5,
    ) -> None:
        self.console = console
        self.transient = transient
        self._synchronized_output = synchronized_output
        self._full_redraw_ratio = full_redraw_ratio
        self.stats = FrameStats()
        self._lines: list[Line] = []
        self._width = 0
        self._full = True
        self._started = False
        self._writing = False
        self._restore_stdout: TextIO | None = None
        self._restore_stderr: TextIO | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def interactive(self) -> bool:
        """Whether frames reach a terminal (a file or dumb terminal gets none)."""
        return self.console.is_terminal and not self.console.is_dumb_terminal

    @property
    def lines(self) -> list[Line]:
        """The lines currently on screen."""
        return list(self._lines)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.console.show_cursor(False)
        self.console.push_render_hook(self)
        self._enable_redirect_io()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        try:
            if self.interactive and self._lines:
                # Transient: clear the block. Otherwise leave it in the
                # scroll-back and move below it.
                self._write(self._erase() if self.transient else [_NEWLINE])
        finally:
            self.console.pop_render_hook()
            self._disable_redirect_io()
            self.console.show_cursor(True)
            self._lines = []
            self._full = True

    def invalidate(self) -> None:
        """Repaint every line in the next frame (after a resize)."""
        self._full = True

    # ── frames ───────────────────────────────────────────────────────────────

    def update(self, lines: Sequence[Line]) -> None:
        """Show *lines*, writing only what changed since the last frame."""
        width = self.console.width
        new = list(lines[: max(1, self.console.height)])
        if not self.interactive:
            self._lines = new
            self._width = width
            return
        started = time.perf_counter()
        segments, full = self._frame(new, width)
        self._lines = new
        self._width = width
        if not segments:
            return
        written = self._write(segments)
        self.stats.record(time.perf_counter() - started, written, full=full)

    def _frame(self, new: list[Line], width: int) -> tuple[list[Segment], bool]:
        old = self._lines
        if not old:
            self._full = False
            return self._paint(new), True
        if self._full or width != self._width:
            self._full = False
            return self._erase() + self._paint(new), True
        height = len(old)
        target = len(new)
        changed = [row for row in range(min(height, target)) if new[row] != old[row]]
        if not changed and height == target:
            return [], False
        if len(changed) + abs(target - height) > max(height, target) * self._full_redraw_ratio:
            return self._erase() + self._paint(new), True
        segments: list[Segment] = []
        row = height - 1
        for index in changed:
            segments += _move(row, index)
            segments += [_CR, _ERASE_LINE, *new[index]]
            row = index
        if target > height:
            segments += _move(row, height - 1)
            for index in range(height, target):
                segments += [_NEWLINE, _CR, *new[index]]
            row = target - 1
        elif target < height:
            for index in range(target, height):
                segments += _move(row, index)
                segments += [_CR, _ERASE_LINE]
                row = index
        segments += _move(row, target - 1)
        return segments, False

    def _erase(self) -> list[Segment]:
        """Clear the block, leaving the cursor at its top-left (Rich's ``position_cursor``)."""
        height = len(self._lines)
        if not height:
            return []
        segments = [_CR, _ERASE_LINE]
        for _ in range(height - 1):
            segments += [Control((ControlType.CURSOR_UP, 1)).segment, _ERASE_LINE]
        return segments

    @staticmethod
    def _paint(lines: Sequence[Line]) -> list[Segment]:
        segments: list[Segment] = []
        for index, line in enumerate(lines):
            if index:
                segments += [_NEWLINE, _CR]
            segments += line
        return segments

    def _write(self, segments: list[Segment]) -> int:
        """Write *segments* in one console write; return their character count."""
        frame = [_SYNC_BEGIN, *segments, _SYNC_END] if self._synchronized_output() else segments
        self._writing = True
        try:
            with self.console:
                self.console.print(Segments(frame), crop=False)
        finally:
            self._writing = False
        return sum(len(segment.text) for segment in frame)

    # ── output printed while live ────────────────────────────────────────────

    def process_renderables(self, renderables: list[ConsoleRenderable]) -> list[ConsoleRenderable]:
        """Print other output above the block, then repaint the block below it."""
        if self._writing or not self._started or not self.interactive or not self._lines:
            return renderables
        sync = self._synchronized_output()
        before = [_SYNC_BEGIN, *self._erase()] if sync else self._erase()
        after = [*self._paint(self._lines), _SYNC_END] if sync else self._paint(self._lines)
        return [Segments(before), *renderables, Segments(after)]

    def _enable_redirect_io(self) -> None:
        if not self.console.is_terminal:
            return
        if not isinstance(sys.stdout, FileProxy):
            self._restore_stdout = sys.stdout
            sys.stdout = cast("TextIO", FileProxy(self.console, sys.stdout))
        if not isinstance(sys.stderr, FileProxy):
            self._restore_stderr = sys.stderr
            sys.stderr = cast("TextIO", FileProxy(self.console, sys.stderr))

    def _disable_redirect_io(self) -> None:
        if self._restore_stdout is not None:
            sys.stdout = self._restore_stdout
            self._restore_stdout = None
        if self._restore_stderr is not None:
            sys.stderr = self._restore_stderr
            self._restore_stderr = None
