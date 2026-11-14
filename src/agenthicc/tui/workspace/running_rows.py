"""Live rows for work that is still running: compaction, tool calls, terminals.

Each running call shows under the transcript, the way Claude Code shows its
in-progress tool, with a blinking ``●`` and how long it has run::

    ● $ uv run pytest tests/unit -q (8m 10s)
    ● $ npm run dev (background · 3m 2s)

When the call completes, its row leaves the live region and the permanent,
quiet row is printed to the transcript (TUX-06). The dot blinks on the frame
clock (half a second on, half a second off); ``reduced_motion`` and the
screen reader keep it still.

While the conversation is being compacted the first row is a quiet ring
spinner in the accent colour and the time it has taken::

    ◜ Refreshing context · 2s

A thin bar under it appears only when the compaction can say how far it has
got (a history summarised in several model calls); one that cannot is the
spinner alone, and nothing is ever estimated::

    ◜ Refreshing context · 2s
    ━━━━━━━━━━━━━━━━━━━━╸─────────
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState, RunningTool

__all__ = [
    "BAR_WIDTH",
    "BLINK_FRAMES",
    "MAX_ROWS",
    "RING_FRAMES",
    "compaction_bar",
    "compaction_row",
    "running_rows",
]

#: Frames the dot stays on, then off (the session ticks at about 20 Hz).
BLINK_FRAMES = 10
#: Running calls shown before ``+N more``.
MAX_ROWS = 5
#: Frames of one turn of the compaction ring: a second at the 20 Hz clock.
RING_FRAMES = 20
#: Cells in the compaction progress bar, fewer on a narrower terminal.
BAR_WIDTH = 30


def _dot_on(frame: int, animated: bool) -> bool:
    return not animated or (frame // BLINK_FRAMES) % 2 == 0


def compaction_row(state: AppState, *, monotonic_now: float | None = None) -> str:
    """``◜ Refreshing context · 2s``: the ring in the accent colour, the time muted.

    The ring turns once a second on the frame clock; ``reduced_motion`` and the
    screen reader keep it at rest as ``◌``. The time is shown from ``0s``, so
    the row never changes width when the first second passes.
    """
    from rich.markup import escape

    from agenthicc.tui.preferences import ui_settings
    from agenthicc.tui.theme import glyphs, palette
    from agenthicc.tui.workspace.formatters import compact_duration

    conv = state.conversation
    p, g = palette(), glyphs()
    ui = ui_settings(state)
    if ui is None or ui.animated:
        ring = g.ring[(conv.frame() % RING_FRAMES) * len(g.ring) // RING_FRAMES]
    else:
        ring = g.ring_still
    elapsed = compact_duration(conv.compaction_elapsed(monotonic_now))
    return f"[{p.accent}]{escape(ring)}[/] Refreshing context [{p.muted}]{g.sep} {elapsed}[/]"


def compaction_bar(state: AppState, cols: int) -> str | None:
    """The thin bar under the compaction row, or ``None`` when nothing is measured.

    It moves only when a model call of the compaction really finishes, in
    half-cell steps; the track is muted and the progress is the accent colour.
    """
    from agenthicc.tui.theme import glyphs, palette

    progress = state.conversation.compaction_progress()
    if progress is None:
        return None
    width = min(BAR_WIDTH, cols - 2)
    if width < 8:
        return None
    p, g = palette(), glyphs()
    halves = int(progress.fraction * width * 2)
    full, half = divmod(halves, 2)
    filled = g.progress_fill * full + (g.progress_half if half else "")
    track = g.progress_track * (width - full - half)
    return f"[{p.accent}]{filled}[/][{p.muted}]{track}[/]"


def _row(item: RunningTool, cols: int, now: float, dot_on: bool) -> str:
    """``● $ python3 - <<'EOF' (23s)``: the elapsed time right after the call."""
    from rich.cells import cell_len
    from rich.markup import escape

    from agenthicc.tui.theme import glyphs, palette
    from agenthicc.tui.workspace.formatters import compact_duration

    p, g = palette(), glyphs()
    elapsed = compact_duration(max(0.0, now - item.started_at))
    meta = f"({'background ' + g.sep + ' ' if item.background else ''}{elapsed})"
    dot_style = f"bold {p.info}" if dot_on else p.subtle
    head = f"{g.bullet} {item.operation} "
    # The elapsed time always shows whole; the target gives way.
    room = cols - cell_len(head) - cell_len(meta) - 1
    target = item.target
    if cell_len(target) > max(0, room):
        keep = max(0, room - cell_len(g.ellipsis))
        target = target[:keep].rstrip() + g.ellipsis if keep else ""
    space = " " if target else ""
    return (
        f"[{dot_style}]{g.bullet}[/] [bold]{escape(item.operation)}[/bold] "
        f"{escape(target)}{space}[{p.muted}]{escape(meta)}[/]"
    )


def running_rows(state: AppState, cols: int, *, now: float | None = None) -> list[str]:
    """Markup rows for the running calls, framed by blank rows; ``[]`` when none."""
    from agenthicc.tui.preferences import ui_settings
    from agenthicc.tui.theme import palette

    conv = state.conversation
    items = (*conv.running_tools(), *conv.background_tools())
    compacting = conv.compaction_active()
    if not items and not compacting:
        return []
    ui = ui_settings(state)
    animated = ui is None or ui.animated
    dot_on = _dot_on(conv.frame(), animated)
    moment = time.time() if now is None else now
    rows = [""]
    if compacting:
        rows.append(compaction_row(state))
        bar = compaction_bar(state, cols)
        if bar is not None:
            rows.append(bar)
    rows.extend(_row(item, cols, moment, dot_on) for item in items[:MAX_ROWS])
    hidden = len(items) - MAX_ROWS
    if hidden > 0:
        rows.append(f"  [{palette().muted}]+{hidden} more running[/]")
    rows.append("")
    return rows
