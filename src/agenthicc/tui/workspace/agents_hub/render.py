"""Drawing the agents hub as lines of text (AGX-05, AGX-06, AGX-07).

The hub's list primitive renders ``Text`` lines itself instead of using Rich's
``Table``: a column is either shown at or above its minimum width or not shown
(AGX-06 §5.2), the selection is marked once in the gutter, and no line ever
wraps. Every function here is a pure function of the records and the width,
so golden renders need no terminal.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import cell_len, set_cell_size
from rich.text import Text

from agenthicc.background.inbox import BackgroundInboxItem
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.tui.keymap import HelpEntry, Hint
from agenthicc.tui.workspace.agents_hub.detail import (
    ChangeSummary,
    DetailData,
    TranscriptLine,
    approval_lines,
    needs_you_reason,
    report_line,
)
from agenthicc.tui.workspace.agents_hub.layout import GUTTER, SEPARATOR, Layout
from agenthicc.tui.workspace.agents_hub.model import (
    HubRow,
    LibraryEntry,
    RowKind,
    SavedSession,
    ScheduledEntry,
    Tab,
    highlight_spans,
    project_name,
)
from agenthicc.background.vocabulary import status_look

if TYPE_CHECKING:
    from agenthicc.subagents.pool import WorkerState

__all__ = [
    "DetailView",
    "RowContext",
    "age",
    "detail_lines",
    "fit",
    "footer_line",
    "help_lines",
    "listing_line",
    "peek_lines",
    "row_line",
    "row_sentence",
    "tab_bar",
    "tab_label",
]


def age(seconds: float) -> str:
    """A compact relative age: ``now``, ``45s``, ``2m``, ``3h``, ``4d``, ``2w``."""
    seconds = max(0.0, seconds)
    if seconds < 5:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3_600:
        return f"{int(seconds // 60)}m"
    if seconds < 86_400:
        return f"{int(seconds // 3_600)}h"
    if seconds < 14 * 86_400:
        return f"{int(seconds // 86_400)}d"
    return f"{int(seconds // (7 * 86_400))}w"


def age_words(seconds: float) -> str:
    """The same age in words, for screen readers (``10 minutes ago``)."""
    seconds = max(0.0, seconds)
    for unit, size in (("week", 604_800), ("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def fit(text: str, width: int) -> str:
    """*text* cut to *width* cells with ``…``, or padded to exactly *width*."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return set_cell_size(text, width)
    return set_cell_size(text, width - 1) + "…"


def _cost(session: BackgroundSession) -> str:
    return f"${session.cost_usd:.2f}" if session.cost_usd else ""


@dataclass(frozen=True)
class RowContext:
    """What a row needs beyond its own record."""

    now: float
    queue_positions: Mapping[str, int]
    sessions: Mapping[str, BackgroundSession]
    words: Sequence[str] = ()
    plain: bool = False


def _session_cells(
    session: BackgroundSession, row: HubRow, context: RowContext
) -> dict[str, tuple[str, str]]:
    """Column name → (text, style) for a background agent row."""
    look = status_look(
        session.status, queue_position=context.queue_positions.get(session.session_id)
    )
    title = session.title or session.intent or session.session_id
    if row.depth:
        title = "  " * (row.depth - 1) + "└ " + title
    if row.hidden:
        title = f"{title} (+{row.hidden})"
    if session.status in (SessionStatus.FAILED, SessionStatus.CANCELLED) and session.error:
        activity = session.error
    elif session.status is SessionStatus.WAITING_APPROVAL:
        activity = session.approval_request or session.latest_activity
    elif session.status is SessionStatus.WAITING_INPUT:
        activity = session.input_request or session.latest_activity
    else:
        activity = session.latest_activity
    return {
        "status": (f"{look.glyph} {look.label}", look.style),
        "title": (title, "bold" if session.status in _BOLD else ""),
        "activity": (" ".join(activity.split()), "dim"),
        "project": (project_name(session.cwd), "cyan"),
        "agent": (session.workflow_name or session.role or "direct", ""),
        "age": (age(context.now - (session.last_active or session.created_at)), "dim"),
        "cost": (_cost(session), "green"),
    }


_BOLD = frozenset({SessionStatus.WAITING_APPROVAL, SessionStatus.WAITING_INPUT})


def _inbox_cells(item: BackgroundInboxItem, context: RowContext) -> dict[str, tuple[str, str]]:
    session = context.sessions.get(item.session_id)
    label = "needs approval" if item.kind == "approval" else "needs input"
    target = dict(item.target)
    tool = str(target.get("tool_name") or item.kind)
    summary = " ".join(value for _label, value in approval_lines(item, session)[:1]).strip()
    activity = f"{tool}: {summary}" if summary and summary != tool else tool
    return {
        "status": (f"◆ {label}", "bold yellow"),
        "title": (
            session.title if session is not None else item.workspace or item.session_id,
            "bold",
        ),
        "activity": (" ".join(activity.split()), "dim"),
        "project": (item.workspace or project_name(item.project_root), "cyan"),
        "agent": ((session.workflow_name or "direct") if session is not None else "", ""),
        "age": (age(context.now - item.created_at), "dim"),
        "cost": ("", ""),
    }


def _saved_cells(record: SavedSession, context: RowContext) -> dict[str, tuple[str, str]]:
    return {
        "status": ("○ saved", "dim"),
        "title": (record.title or f"session {record.session_id[:8]}", ""),
        "activity": (record.model, "dim"),
        "project": (project_name(record.cwd), "cyan"),
        "agent": ("foreground", "dim"),
        "age": (age(context.now - record.last_used) if record.last_used else "", "dim"),
        "cost": ("", ""),
    }


def _scheduled_cells(entry: ScheduledEntry, context: RowContext) -> dict[str, tuple[str, str]]:
    remaining = entry.next_run_at - context.now
    when = "due" if remaining <= 0 else f"in {age(remaining)}"
    return {
        "status": ("◷ scheduled", "magenta"),
        "title": (entry.name, ""),
        "activity": (when, "dim"),
        "project": (entry.project, "cyan"),
        "agent": ("schedule", "dim"),
        "age": (when if remaining > 0 else "due", "dim"),
        "cost": ("", ""),
    }


def _definition_cells(entry: LibraryEntry) -> dict[str, tuple[str, str]]:
    return {
        "status": (f"● {entry.name}", "bold"),
        "title": (entry.description or entry.name, ""),
        "activity": (entry.tools, "dim"),
        "project": (entry.source, "cyan"),
        "agent": ("", ""),
        "age": ("", ""),
        "cost": ("", ""),
    }


def _subagent_cells(worker: WorkerState, context: RowContext) -> dict[str, tuple[str, str]]:
    """Column name → (text, style) for a subagent worker of the session (AGX-08)."""
    from agenthicc.tui.workspace.subagent_panel import elapsed, status_glyph

    glyph, style, word = status_glyph(worker.status)
    return {
        "status": (f"{glyph} {word}", style),
        "title": (worker.label, "bold" if worker.status == "waiting" else ""),
        "activity": (" ".join(worker.activity.split()), "dim"),
        "project": ("", ""),
        "agent": (worker.agent_type, ""),
        "age": (elapsed(worker, context.now), "dim"),
        "cost": ("", ""),
    }


def _cells(row: HubRow, context: RowContext) -> dict[str, tuple[str, str]]:
    if row.worker is not None:
        return _subagent_cells(row.worker, context)
    if row.session is not None:
        return _session_cells(row.session, row, context)
    if row.item is not None:
        return _inbox_cells(row.item, context)
    if row.saved is not None:
        return _saved_cells(row.saved, context)
    if row.scheduled is not None:
        return _scheduled_cells(row.scheduled, context)
    if row.entry is not None:
        return _definition_cells(row.entry)
    return {}


def row_line(
    row: HubRow,
    columns: Layout,
    context: RowContext,
    *,
    selected: bool = False,
    marked: bool = False,
    unseen: bool = False,
) -> Text:
    """One list line at ``columns.width`` cells (never wraps)."""
    width = columns.width
    if row.kind is RowKind.HEADER:
        return Text(fit(f" {row.text}", width), style="bold")
    if row.kind is RowKind.NOTE:
        return Text(fit(f"   {row.text}", width), style="dim italic")
    cells = _cells(row, context)
    line = Text(no_wrap=True, overflow="crop")
    gutter = ("▸" if selected else " ") + ("✓" if marked else "•" if unseen else " ")
    line.append(gutter[:GUTTER], style="bold cyan" if selected else "yellow")
    for index, name in enumerate(columns.columns):
        if index:
            line.append(" " * SEPARATOR)
        text, style = cells.get(name, ("", ""))
        cell_width = columns.widths[index]
        shown = fit(text, cell_width) if name != "age" else text[:cell_width].rjust(cell_width)
        start = len(line.plain)
        line.append(shown, style=style)
        if name in ("title", "activity") and context.words:
            for span_start, span_end in highlight_spans(shown, list(context.words)):
                line.stylize("reverse", start + span_start, start + span_end)
    if selected:
        line.stylize("on grey19", 0, len(line.plain))
    return line


def listing_line(row: HubRow, context: RowContext) -> str:
    """A row as one plain line for output without a terminal: ID first."""
    if row.kind is RowKind.HEADER:
        return f"{row.text}:"
    if row.kind is RowKind.NOTE:
        return f"  {row.text}"
    cells = _cells(row, context)
    if row.session_id is not None:
        identity = row.session_id
    elif row.saved is not None:
        identity = row.saved.session_id
    elif row.scheduled is not None:
        identity = row.scheduled.schedule_id
    elif row.worker is not None:
        identity = f"{row.pool_id[:8]}:{row.worker.worker_id}"
    else:
        identity = row.entry.name if row.entry is not None else ""
    parts = [
        f"  {identity:<16}",
        f"{cells['status'][0]:<18}",
        cells["title"][0],
        cells["project"][0],
        cells["age"][0],
    ]
    return "  ".join(part for part in parts if part).rstrip()


def row_sentence(row: HubRow, context: RowContext, *, selected: bool = False) -> Text:
    """A row as a sentence in reading order, for screen readers (AGX-13 §5.5)."""
    if row.kind is RowKind.HEADER:
        return Text(f"{row.text}:")
    if row.kind is RowKind.NOTE:
        return Text(row.text)
    cells = _cells(row, context)
    parts = [cells["title"][0], cells["status"][0].split(" ", 1)[-1]]
    if cells["project"][0]:
        parts.append(cells["project"][0])
    if row.session is not None:
        parts.append(age_words(context.now - (row.session.last_active or row.session.created_at)))
    elif row.item is not None:
        parts.append(age_words(context.now - row.item.created_at))
    sentence = ", ".join(part for part in parts if part) + "."
    return Text(("Selected: " if selected else "") + sentence)


def tab_label(tab: Tab, counts: Mapping[Tab, int]) -> str:
    """``History 214``; a tab whose count is not known yet shows its name alone."""
    count = counts.get(tab)
    return tab.label if count is None else f"{tab.label} {count}"


def tab_bar(
    tab: Tab,
    counts: Mapping[Tab, int],
    width: int,
    *,
    paused: bool = False,
    plain: bool = False,
    help_hint: Hint | None = None,
) -> Text:
    """`` Agents   Running 4 · Needs you 2 · History 214 · Library 9   ? help``."""
    line = Text(no_wrap=True, overflow="crop")
    line.append(" Agents", style="bold cyan")
    line.append("   ")
    for index, candidate in enumerate(Tab):
        if index:
            line.append(", " if plain else " · ", style="dim")
        label = tab_label(candidate, counts)
        style = "dim"
        if candidate is tab:
            style = "bold underline"
            if plain:
                label = f"[{label}]"
        if candidate is Tab.NEEDS_YOU and counts.get(candidate, 0):
            style = "bold yellow" + (" underline" if candidate is tab else "")
        line.append(label, style=style)
    if paused:
        line.append("   updates paused", style="yellow")
    if help_hint is not None:
        right = f"{help_hint.keys} {help_hint.label}"
        gap = width - cell_len(line.plain) - cell_len(right) - 1
        if gap >= 2:
            line.append(" " * gap)
            line.append(right, style="dim")
    line.truncate(width, overflow="crop")
    return line


def footer_line(
    hints: Sequence[Hint],
    width: int,
    *,
    tail: Sequence[Hint] = (),
    plain: bool = False,
) -> Text:
    """One line of hints by priority; *tail* (``? help``, ``q quit``) always ends it.

    Hints that do not fit are dropped from the lowest priority up, so the
    footer never wraps (AGX-06 §5.6).
    """
    separator = ", " if plain else "  "
    chosen = list(hints)

    def render(entries: Sequence[Hint]) -> str:
        return " " + separator.join(f"{hint.keys} {hint.label}" for hint in entries)

    while chosen and cell_len(render([*chosen, *tail])) > width:
        chosen.pop()
    line = Text(no_wrap=True, overflow="crop")
    line.append(" ")
    for index, hint in enumerate([*chosen, *tail]):
        if index:
            line.append(separator)
        line.append(hint.keys, style="bold")
        line.append(f" {hint.label}", style="dim")
    line.truncate(width, overflow="crop")
    return line


def help_lines(title: str, entries: Sequence[HelpEntry], width: int) -> list[Text]:
    """Every key of the current screen, from the key map (AGX-13 §5.1)."""
    lines = [Text(fit(f" {title} — keys", width), style="bold cyan")]
    keys_width = min(22, max((cell_len(entry.keys) for entry in entries), default=4))
    for entry in entries:
        line = Text(no_wrap=True, overflow="crop")
        line.append(" " + fit(entry.keys, keys_width), style="bold")
        line.append("  " + entry.description)
        if entry.conditions:
            line.append(f"  ({entry.conditions})", style="dim")
        if entry.source != "default":
            line.append("  · your binding", style="cyan")
        line.truncate(width, overflow="crop")
        lines.append(line)
    lines.append(Text(" Any key closes this help.", style="dim"))
    return lines


# ── peek and detail ──────────────────────────────────────────────────────────


def _labelled(label: str, value: str, width: int, *, label_width: int = 8) -> Text:
    line = Text(no_wrap=True, overflow="crop")
    line.append("   " + fit(label, label_width), style="bold")
    line.append("  " + value)
    line.truncate(width, overflow="crop")
    return line


def _waiting(session: BackgroundSession, now: float) -> str:
    since = session.state_changed_at or session.last_active
    return f"waiting {age(now - since)}" if since else "waiting"


def peek_lines(
    session: BackgroundSession,
    data: DetailData | None,
    item: BackgroundInboxItem | None,
    width: int,
    *,
    now: float,
    hints: Sequence[Hint] = (),
) -> list[Text]:
    """The panel under a row (AGX-07 §5.1): the exact request, or the progress."""
    look = status_look(session.status)
    header = Text(no_wrap=True, overflow="crop")
    header.append("   " + look.glyph + " " + look.label, style=look.style)
    if session.status in (
        SessionStatus.WAITING_APPROVAL,
        SessionStatus.WAITING_INPUT,
        SessionStatus.ORPHANED,
    ):
        header.append(" · " + _waiting(session, now), style="dim")
    elif session.started_at:
        header.append(f" · {age(now - session.started_at)}", style="dim")
    lines = [header]
    if item is not None:
        for label, value in approval_lines(item, session):
            lines.append(_labelled(label, value, width))
    elif reason := needs_you_reason(session):
        lines.append(_labelled("", reason, width, label_width=0))
    else:
        report = report_line(data.transcript) if data is not None else ""
        if report or session.latest_activity:
            lines.append(_labelled("", report or session.latest_activity, width, label_width=0))
        if data is not None:
            tool = next(
                (line for line in reversed(data.transcript) if line.role not in _TEXT_ROLES),
                None,
            )
            if tool is not None:
                lines.append(_labelled(tool.role, f"{tool.status} {tool.text}".strip(), width))
    if hints:
        lines.append(footer_line(hints, width))
    return lines


_TEXT_ROLES = frozenset({"you", "assistant", "system", "question", "error"})


def _section(title: str, width: int, key: str = "", *, plain: bool = False) -> Text:
    if plain:
        return Text(f" {title}:", style="bold")
    label = f" {title} "
    tail = f" {key} " if key else ""
    rule = "─" * max(0, width - cell_len(label) - cell_len(tail))
    line = Text(no_wrap=True, overflow="crop")
    line.append(label, style="bold")
    line.append(rule, style="dim")
    line.append(tail, style="dim")
    return line


def _usage(session: BackgroundSession) -> str:
    parts: list[str] = []
    tokens = session.tokens_in + session.tokens_out
    if tokens:
        parts.append(f"{tokens / 1000:.1f}k tokens" if tokens >= 1000 else f"{tokens} tokens")
    if session.cost_usd:
        parts.append(f"${session.cost_usd:.2f}")
    return " · ".join(parts)


def _transcript_line(line: TranscriptLine, width: int) -> Text:
    text = Text(no_wrap=True, overflow="crop")
    if line.status:
        style = {"✓": "green", "✗": "red", "◆": "yellow", "●": "cyan"}.get(line.status, "")
        text.append(f"   {line.status} ", style=style)
    else:
        text.append("     ")
    role_style = {"you": "bold cyan", "assistant": "bold", "error": "bold red"}.get(line.role, "")
    text.append(fit(line.role, 10), style=role_style)
    text.append(" " + line.text)
    text.truncate(width, overflow="crop")
    return text


def _detail_fields(session: BackgroundSession, now: float) -> list[tuple[str, str]]:
    """Only non-empty fields, with plain labels (AGX-07 §5.3)."""

    def when(timestamp: float | None) -> str:
        if not timestamp:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) + (
            f" ({age_words(now - timestamp)})"
        )

    mode = session.mode_name
    if session.mode_application_status.value != "applied":
        requested = session.requested_mode_name
        mode = f"{session.mode_application_status.value}" + (
            f" (requested {requested or 'default'})" if requested is not None else ""
        )
    fields = [
        ("Agent ID", session.session_id),
        ("Directory", session.cwd),
        ("Workflow", session.workflow_name),
        ("Mode", mode),
        ("Mode error", session.mode_application_error),
        ("Provider", session.provider),
        ("Model", session.model),
        ("Source", session.source),
        ("Priority", session.priority.value if session.priority.value != "normal" else ""),
        (
            "Dispatch",
            session.dispatch_state.value if session.dispatch_state.value != "legacy" else "",
        ),
        ("Created", when(session.created_at)),
        ("Started", when(session.started_at)),
        ("Updated", when(session.last_active)),
        ("Finished", when(session.completed_at)),
        ("Labels", ", ".join(session.labels)),
        ("Run", session.run_id),
        ("Parent", session.parent_session_id),
        ("Role", session.role),
        ("Task", session.task_id),
        ("Branch", session.branch),
        ("Base commit", session.base_commit[:12]),
        ("Exit reason", session.exit_reason),
        # An error a still-active agent carries from an earlier attempt.
        (
            "Earlier error",
            session.error or ""
            if session.status not in (SessionStatus.FAILED, SessionStatus.CANCELLED)
            else "",
        ),
        ("Failure", session.failure_category),
        ("Worker PID", str(session.worker_pid) if session.worker_pid else ""),
        ("Heartbeat", "delayed; the worker is being checked" if session.heartbeat_stale else ""),
        (
            "Environment runs",
            str(len(session.environment_receipts)) if session.environment_receipts else "",
        ),
    ]
    return [(label, value) for label, value in fields if value]


@dataclass(frozen=True)
class DetailView:
    """The detail view's lines and where its transcript and request are."""

    lines: list[Text]
    #: The last line of the transcript section (following keeps it in view).
    transcript_end: int
    #: The pending request's line in the transcript, else the Needs you card.
    request_line: int
    #: Leading lines that stay in view: the title and the Needs you card.
    pinned: int = 0


def detail_lines(
    session: BackgroundSession,
    data: DetailData | None,
    item: BackgroundInboxItem | None,
    width: int,
    *,
    now: float,
    queue_position: int | None = None,
    follow: bool = True,
    full_transcript: bool = False,
    plain: bool = False,
) -> DetailView:
    """The detail view's lines before scrolling (AGX-07 §5.2).

    Sections: Needs you (when waiting), Now (while active), Transcript,
    Changes (worktree agents), Attempts (more than one) and Details.
    """
    look = status_look(session.status, queue_position=queue_position)
    lines: list[Text] = []
    head = Text(no_wrap=True, overflow="crop")
    head.append(f" {look.glyph} {look.label}", style=look.style)
    head.append("   " + (session.title or session.intent or session.session_id), style="bold")
    if session.attempt > 1:
        attempt = f"attempt {session.attempt}"
        gap = width - cell_len(head.plain) - len(attempt) - 1
        head.append(" " * max(2, gap) + attempt, style="dim")
    head.truncate(width, overflow="crop")
    lines.append(head)
    facts = [
        project_name(session.cwd),
        session.workflow_name or "direct",
        session.model,
    ]
    if session.started_at:
        facts.append(f"started {age(now - session.started_at)} ago")
    usage = _usage(session)
    if usage:
        facts.append(usage)
    lines.append(Text(fit(" " + " · ".join(fact for fact in facts if fact), width), style="dim"))
    lines.append(Text(""))

    # Needs you
    request_line = 0
    if item is not None or session.status in (
        SessionStatus.WAITING_APPROVAL,
        SessionStatus.WAITING_INPUT,
        SessionStatus.ORPHANED,
    ):
        request_line = len(lines)
        lines.append(_section("Needs you", width, plain=plain))
        if item is not None:
            for label, value in approval_lines(item, session):
                lines.append(_labelled(label, value, width))
        else:
            lines.append(_labelled("", needs_you_reason(session), width, label_width=0))
        lines.append(Text(""))

    pinned = len(lines)

    # Now
    if session.status in (
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.RETRYING,
        SessionStatus.CANCELLING,
        SessionStatus.WAITING_APPROVAL,
        SessionStatus.WAITING_INPUT,
        SessionStatus.QUEUED,
    ):
        lines.append(_section("Now", width, plain=plain))
        if session.phase_history or session.current_phase:
            phases = [
                f"{phase} ✓" for phase in session.phase_history if phase != session.current_phase
            ]
            if session.current_phase:
                phases.append(f"{session.current_phase} ●")
            position = len(session.phase_history) or 1
            lines.append(Text(fit(f"   Phase {position} · " + " → ".join(phases), width)))
        if queue_position is not None:
            lines.append(Text(fit(f"   Queued #{queue_position} (advisory)", width)))
        if session.latest_activity:
            lines.append(Text(fit("   " + " ".join(session.latest_activity.split()), width)))
        lines.append(Text(""))

    # Transcript
    title = "Transcript (all)" if full_transcript else "Transcript"
    if follow:
        title += " (following)"
    lines.append(_section(title, width, "f t", plain=plain))
    transcript = data.transcript if data is not None else ()
    if not transcript:
        lines.append(
            Text("   No messages yet." if data is not None else "   Loading…", style="dim")
        )
    for entry in transcript:
        if entry.status == "◆":
            request_line = len(lines)
        lines.append(_transcript_line(entry, width))
    if data is not None and data.receipt:
        lines.append(Text(fit(f"   Latest input: {data.receipt}", width), style="dim"))
    transcript_end = len(lines) - 1
    lines.append(Text(""))

    # Changes
    changes: ChangeSummary | None = data.changes if data is not None else None
    if changes is not None:
        lines.append(_section("Changes", width, "d", plain=plain))
        summary = changes.headline
        if changes.branch:
            summary += f"   (branch {changes.branch}, base {changes.base})"
        lines.append(Text(fit("   " + summary, width)))
        for stat in changes.lines:
            lines.append(Text(fit("   " + stat, width), style="dim"))
        lines.append(Text(""))

    # Attempts
    if session.attempt_history:
        lines.append(_section(f"Attempts ({len(session.attempt_history) + 1})", width, plain=plain))
        for previous in session.attempt_history:
            outcome = previous.status.value
            detail = previous.error or previous.latest_activity or ""
            if previous.failure_category:
                outcome += f" · {previous.failure_category}"
            lines.append(
                Text(fit(f"   #{previous.attempt} {outcome}  {' '.join(detail.split())}", width))
            )
        lines.append(Text(fit(f"   #{session.attempt} {session.status.value} (current)", width)))
        lines.append(Text(""))

    # Error
    if session.error and session.status in (SessionStatus.FAILED, SessionStatus.CANCELLED):
        lines.append(_section("Error", width, plain=plain))
        lines.append(Text(fit("   " + " ".join(session.error.split()), width), style="red"))
        lines.append(Text(""))

    # Details
    lines.append(_section("Details", width, plain=plain))
    for label, value in _detail_fields(session, now):
        lines.append(_labelled(label, value, width, label_width=16))
    return DetailView(lines, transcript_end, request_line, pinned)
