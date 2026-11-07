"""Live rows for tool calls and background terminals that are still running.

Each running call shows under the transcript, the way Claude Code shows its
in-progress tool, with a blinking ``●`` and how long it has run::

    ● $ uv run pytest tests/unit -q                                  8m 10s
    ● $ npm run dev                                  background · 3m 2s

When the call completes, its row leaves the live region and the permanent,
quiet row is printed to the transcript (TUX-06). The dot blinks on the frame
clock (half a second on, half a second off); ``reduced_motion`` and the
screen reader keep it still.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState, RunningTool

__all__ = ["BLINK_FRAMES", "MAX_ROWS", "running_rows"]

#: Frames the dot stays on, then off (the session ticks at about 20 Hz).
BLINK_FRAMES = 10
#: Running calls shown before ``+N more``.
MAX_ROWS = 5


def _dot_on(frame: int, animated: bool) -> bool:
    return not animated or (frame // BLINK_FRAMES) % 2 == 0


def _row(item: RunningTool, cols: int, now: float, dot_on: bool) -> str:
    from rich.cells import cell_len
    from rich.markup import escape

    from agenthicc.tui.theme import glyphs, palette
    from agenthicc.tui.workspace.formatters import compact_duration

    p, g = palette(), glyphs()
    elapsed = compact_duration(max(0.0, now - item.started_at))
    meta = f"background {g.sep} {elapsed}" if item.background else elapsed
    dot_style = f"bold {p.info}" if dot_on else p.subtle
    head = f"{g.bullet} {item.operation} "
    room = cols - cell_len(head) - cell_len(meta) - 2
    target = item.target
    if cell_len(target) > max(0, room):
        keep = max(0, room - cell_len(g.ellipsis))
        target = target[:keep].rstrip() + g.ellipsis if keep else ""
    used = cell_len(head) + cell_len(target)
    gap = " " * max(2, cols - used - cell_len(meta))
    return (
        f"[{dot_style}]{g.bullet}[/] [bold]{escape(item.operation)}[/bold] "
        f"{escape(target)}{gap}[{p.muted}]{escape(meta)}[/]"
    )


def running_rows(state: AppState, cols: int, *, now: float | None = None) -> list[str]:
    """Markup rows for the running calls, framed by blank rows; ``[]`` when none."""
    from agenthicc.tui.preferences import ui_settings
    from agenthicc.tui.theme import palette

    conv = state.conversation
    items = (*conv.running_tools(), *conv.background_tools())
    if not items:
        return []
    ui = ui_settings(state)
    animated = ui is None or ui.animated
    dot_on = _dot_on(conv.frame(), animated)
    moment = time.time() if now is None else now
    rows = [""]
    rows.extend(_row(item, cols, moment, dot_on) for item in items[:MAX_ROWS])
    hidden = len(items) - MAX_ROWS
    if hidden > 0:
        rows.append(f"  [{palette().muted}]+{hidden} more running[/]")
    rows.append("")
    return rows
