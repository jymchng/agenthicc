"""The subagent panel under the prompt (AGX-08 §5.1).

While a turn's subagent pool runs, the live block shows one row per worker
between the composer and the footer: its status, label, what it is doing now
(the current tool and target, a pending approval or its latest text), how long
it has run and its tokens. Finished workers stay for
:data:`~agenthicc.subagents.pool.FINISHED_ROW_S` and failed ones until they
are dismissed; past :data:`MAX_ROWS` the rest collapse into ``… +N more``, and
a terminal shorter than :data:`COMPACT_BELOW_ROWS` gets the summary line
alone. The same rows, with a selection, are the focused panel (Ctrl+O).
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.markup import escape

from agenthicc.subagents.pool import SubagentPoolState, WorkerState
from agenthicc.tui.rendering import fit, visible_len

__all__ = [
    "COMPACT_BELOW_ROWS",
    "MAX_ROWS",
    "elapsed",
    "panel_lines",
    "status_glyph",
    "summary_line",
    "tokens",
    "worker_line",
]

#: At most this many worker rows; the rest collapse into ``… +N more``.
MAX_ROWS = 6
#: Below this terminal height the panel is its summary line alone.
COMPACT_BELOW_ROWS = 24

_LOOKS: dict[str, tuple[str, str, str]] = {
    "pending": ("◌", "dim", "waiting to start"),
    "running": ("●", "cyan", "running"),
    "waiting": ("◆", "yellow", "needs approval"),
    "done": ("✓", "green", "done"),
    "failed": ("✗", "red", "failed"),
    "stopped": ("⊘", "dim", "stopped"),
}


def status_glyph(status: str) -> tuple[str, str, str]:
    """Glyph, style and spoken word for a worker status."""
    return _LOOKS.get(status, ("?", "dim", status))


def elapsed(worker: WorkerState, now: float) -> str:
    """``0:42`` since the worker started (to when it finished)."""
    if worker.started_at is None:
        return ""
    end = worker.finished_at if worker.finished_at is not None else now
    seconds = max(0, int(end - worker.started_at))
    minutes, seconds = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def tokens(count: int) -> str:
    """``3.1k``."""
    if count <= 0:
        return ""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


def summary_line(
    state: SubagentPoolState, cols: int, *, hint: str = "", plain: bool = False
) -> str:
    """``Subagents 3 running · 1 done · limit 4          Ctrl+O focus``."""
    running = sum(1 for worker in state.workers if worker.live)
    done = sum(1 for worker in state.workers if worker.status in ("done", "stopped"))
    failed = sum(1 for worker in state.workers if worker.status == "failed")
    separator = ", " if plain else " · "
    parts = [f"{running} running", f"{done} done"]
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    if state.limit and running:
        parts.append(f"limit {state.limit}")
    left = f"  [bold magenta]Subagents[/bold magenta] {separator.join(parts)}"
    if hint:
        right = f"[dim]{escape(hint)}[/dim]"
        gap = cols - visible_len(left) - visible_len(right) - 1
        if gap >= 2:
            return f"{left}{' ' * gap}{right}"
    return fit(left, cols) if visible_len(left) > cols else left


def worker_line(
    worker: WorkerState,
    cols: int,
    *,
    now: float,
    selected: bool = False,
    plain: bool = False,
) -> str:
    """One worker row, fitted to *cols*."""
    glyph, style, word = status_glyph(worker.status)
    time_text = elapsed(worker, now)
    token_text = tokens(worker.tokens)
    activity = " ".join(worker.activity.split())
    if plain:
        parts = [
            worker.label,
            word,
            activity,
            time_text,
            f"{token_text} tokens" if token_text else "",
        ]
        text = escape(", ".join(part for part in parts if part))
        line = f"{'> ' if selected else '  '}{text}"
        return fit(line, cols) if visible_len(line) > cols else line
    mark = "▸" if selected else " "
    right = f"{time_text:>7}  {token_text:>6}"
    label = f"{worker.label:<14}"
    left_width = cols - len(right) - 1
    lead = f" {mark}[{style}]{glyph}[/{style}] {escape(label)} "
    room = max(0, left_width - visible_len(lead))
    if len(activity) > room:
        activity = activity[: max(0, room - 1)] + "…" if room > 1 else ""
    body = f"{lead}[dim]{escape(activity)}[/dim]"
    padding = max(1, left_width - visible_len(body) + 1)
    line = f"{body}{' ' * padding}[dim]{right}[/dim]"
    if selected:
        line = f"[reverse]{line}[/reverse]"
    return fit(line, cols) if visible_len(line) > cols else line


def panel_lines(
    state: SubagentPoolState,
    cols: int,
    *,
    now: float,
    terminal_rows: int,
    hint: str = "",
    plain: bool = False,
    selected: str | None = None,
    rows: Sequence[WorkerState] | None = None,
    max_rows: int = MAX_ROWS,
) -> list[str]:
    """The panel's markup rows: the summary, then up to *max_rows* workers.

    *rows* replaces the workers listed (the focused panel lists every worker);
    *selected* is a worker ID to highlight.
    """
    lines = [summary_line(state, cols, hint=hint, plain=plain)]
    if terminal_rows < COMPACT_BELOW_ROWS:
        return lines
    shown = list(state.shown(now) if rows is None else rows)
    if len(shown) > max_rows:
        visible = shown[: max_rows - 1]
        if selected is not None:
            index = next((i for i, w in enumerate(shown) if w.worker_id == selected), 0)
            start = min(max(0, index - (max_rows - 2)), len(shown) - (max_rows - 1))
            visible = shown[start : start + max_rows - 1]
        hidden = len(shown) - len(visible)
        lines.extend(
            worker_line(w, cols, now=now, selected=w.worker_id == selected, plain=plain)
            for w in visible
        )
        lines.append(f"   [dim]… +{hidden} more[/dim]")
        return lines
    lines.extend(
        worker_line(w, cols, now=now, selected=w.worker_id == selected, plain=plain) for w in shown
    )
    return lines
