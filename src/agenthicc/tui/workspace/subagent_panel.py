"""Responsive subagent status rows for the live and focused panels."""

from __future__ import annotations

import re
from collections.abc import Sequence

from rich.markup import escape

from agenthicc.subagents.pool import SubagentPoolState, WorkerState
from agenthicc.tui.rendering import fit, visible_len
from agenthicc.tui.workspace.formatters import compact_token_count, pool_status_counts

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
    "pending": ("◌", "dim", "queued"),
    "running": ("●", "cyan", "running"),
    "waiting": ("◆", "yellow", "waiting for approval"),
    "done": ("✓", "green", "done"),
    "failed": ("✗", "red", "failed"),
    "stopped": ("⊘", "dim", "stopped"),
}

_TOOL_LABELS: dict[str, str] = {
    "read_file": "Read",
    "read_lines": "Read",
    "batch_read": "Read",
    "list_directory": "List",
    "search_files": "Search",
    "grep_file": "Search",
    "grep_files": "Search",
    "run_command": "Run",
    "run_bash": "Run",
    "run_python": "Run",
    "run_python_expr": "Run",
    "run_tests": "Test",
    "write_file": "Edit",
    "modify_file": "Edit",
    "patch_file": "Edit",
    "apply_diff": "Edit",
    "git_status": "Git status",
    "git_diff": "Git diff",
    "git_log": "Git log",
}


def status_glyph(status: str) -> tuple[str, str, str]:
    """Glyph, style and spoken word for a worker status."""
    return _LOOKS.get(status, ("?", "dim", status or "unknown"))


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
    """Compact positive worker token values; zero remains visually omitted."""
    return compact_token_count(count) if count > 0 else ""


def summary_line(
    state: SubagentPoolState, cols: int, *, hint: str = "", plain: bool = False
) -> str:
    """Render one width-bounded summary from the current pool snapshot."""
    counts = pool_status_counts(state)
    separator = ", " if plain else " · "
    short = cols < 64
    parts: list[str] = []

    # Attention states precede ordinary progress so fitting cannot hide them.
    if counts.failed:
        parts.append(f"{counts.failed} {'fail' if short and not plain else 'failed'}")
    if counts.needs_input:
        label = "approval" if short and not plain else "needs input"
        parts.append(f"{counts.needs_input} {label}")
    if counts.stopped:
        parts.append(f"{counts.stopped} {'stop' if short and not plain else 'stopped'}")
    if counts.running:
        parts.append(f"{counts.running} {'run' if short and not plain else 'running'}")
    if counts.queued:
        parts.append(f"{counts.queued} queued")
    if counts.done:
        parts.append(f"{counts.done}/{state.total} done")
    if counts.queued and state.limit > 0:
        parts.append(f"limit {state.limit}")

    name = "Subagents" if plain else "  [bold magenta]Subagents[/bold magenta]"
    if not parts:
        parts.append("none" if plain else "0 workers")
    depth = f"depth {state.depth}" if state.depth > 1 else ""
    left = name + (separator + separator.join(parts) if parts else "")
    if depth:
        left += separator + depth if plain else f" [dim]{separator}{depth}[/dim]"

    right = f"[dim]{escape(hint)}[/dim]" if hint and not plain else ""
    if right:
        gap = cols - visible_len(left) - visible_len(right)
        if gap >= 2:
            return f"{left}{' ' * gap}{right}"

    if visible_len(left) <= cols:
        return left

    # At constrained widths, drop ordinary progress and then its explanatory
    # limit before fitting; failures and waits stay at the front of the line.
    reduced = list(parts)
    for marker in (" done", "limit ", " queued", " running", " run"):
        if visible_len(name + separator + separator.join(reduced)) <= cols:
            break
        reduced = [
            part
            for part in reduced
            if not (part.endswith(marker) if marker.startswith(" ") else part.startswith(marker))
        ]
    compact_left = name + (separator + separator.join(reduced) if reduced else "")
    return fit(compact_left, cols)


def worker_line(
    worker: WorkerState,
    cols: int,
    *,
    now: float,
    selected: bool = False,
    plain: bool = False,
) -> str:
    """Render one responsive worker row fitted to the terminal's cell width."""
    glyph, style, spoken_status = status_glyph(worker.status)
    identity = _identity(worker.label)
    activity = _activity(worker)
    time_text = elapsed(worker, now)
    token_text = tokens(worker.tokens)

    if plain:
        lead = f"{spoken_status}, {identity}"
        if selected:
            lead = f"Selected, {lead}"
        body = f"{lead}, {activity}" if activity else lead
        if time_text:
            body += f", elapsed {time_text}"
        return fit(escape(body), cols)

    marker = "▸" if selected else " "
    lead = f" {marker}[{style}]{glyph}[/{style}] {escape(identity)}"
    tail = ""
    if cols >= 64 and time_text:
        time_part = f"  {time_text}"
        if cols >= 120 and token_text:
            token_part = f"  {token_text}"
            if visible_len(lead) + 2 + 10 + visible_len(time_part + token_part) <= cols:
                tail = time_part + token_part
        if not tail and visible_len(lead) + 2 + 8 + visible_len(time_part) <= cols:
            tail = time_part

    activity_width = cols - visible_len(lead) - visible_len(tail)
    if activity and activity_width > 1:
        activity_text = _fit_activity(activity, activity_width - 1)
        line = f"{lead} {escape(activity_text)}{tail}"
    else:
        line = f"{lead}{tail}"
    if selected:
        line = f"[reverse]{line}[/reverse]"
    return fit(line, cols)


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
    """The panel's summary and responsive worker rows.

    *rows* replaces the workers listed (the focused panel lists every worker);
    *selected* is a worker ID to highlight.
    """
    lines = [summary_line(state, cols, hint=hint, plain=plain)]
    if terminal_rows < COMPACT_BELOW_ROWS:
        return lines

    shown = list(state.shown(now) if rows is None else rows)
    if cols < 40:
        chosen = next((worker for worker in shown if worker.worker_id == selected), None)
        if chosen is None:
            chosen = next(iter(sorted(shown, key=_attention_rank)), None)
        if chosen is not None:
            lines.append(
                worker_line(
                    chosen, cols, now=now, selected=chosen.worker_id == selected, plain=plain
                )
            )
        return lines

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


def _identity(label: str) -> str:
    """Normalize one worker label without repeating its role."""
    value = " ".join(label.split())
    value = re.sub(r"\s*#\s*(\d+)$", r" \1", value)
    return value[:1].upper() + value[1:] if value else "Worker"


def _activity(worker: WorkerState) -> str:
    """Make known tool activity readable while retaining safe unknown labels."""
    value = " ".join(worker.activity.split())
    waiting = worker.status == "waiting"
    if waiting and value.lower().startswith("needs approval:"):
        value = value[len("needs approval:") :].strip()
    if not value:
        return "Needs approval" if waiting else ""

    operation, separator, target = value.partition(" ")
    display = _TOOL_LABELS.get(operation)
    if display is None and "_" in operation:
        display = operation.replace("_", " ").title()
    if display is not None:
        value = display + (f" {target}" if separator else "")
    if waiting:
        return f"Needs approval: {value}"
    return value


def _fit_activity(value: str, width: int) -> str:
    """Shorten activity at words or path boundaries, retaining useful path tails."""
    from rich.markup import escape
    from rich.text import Text

    if width <= 0:
        return ""
    if visible_len(escape(value)) <= width:
        return value

    def clip(text: str, cells: int) -> str:
        return Text.from_markup(fit(escape(text), cells), emoji=False).plain

    words = value.split()
    path_index = next((i for i in range(len(words) - 1, -1, -1) if "/" in words[i]), None)
    if path_index is not None:
        path = words[path_index].rstrip(".,;:")
        components = [component for component in path.split("/") if component]
        suffix = "/".join(components[-2:])
        prefix = " ".join(words[:path_index])
        candidate = f"{prefix} …/{suffix}" if prefix else f"…/{suffix}"
        if visible_len(escape(candidate)) <= width:
            return candidate
        last = components[-1] if components else path
        candidate = f"{prefix} {last}" if prefix else last
        if visible_len(escape(candidate)) <= width:
            return candidate
        prefix_width = visible_len(escape(prefix)) + (1 if prefix else 0)
        suffix_width = max(1, width - prefix_width - 1)
        short_last = clip(last, suffix_width)
        return f"{prefix} …{short_last}" if prefix else clip(last, width)

    kept: list[str] = []
    for word in words:
        candidate = " ".join((*kept, word))
        if visible_len(escape(candidate)) <= width:
            kept.append(word)
        else:
            break
    if kept:
        candidate = " ".join(kept)
        return f"{candidate}…" if visible_len(escape(candidate)) < width else candidate
    return clip(value, width)


def _attention_rank(worker: WorkerState) -> int:
    return {"failed": 0, "waiting": 1, "stopped": 2, "running": 3, "pending": 4, "done": 5}.get(
        worker.status, 6
    )
