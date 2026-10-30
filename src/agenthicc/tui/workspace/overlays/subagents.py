"""The focused subagent panel and a worker's transcript (AGX-08 §5.2).

Ctrl+O (or ``/subagents``) moves focus from the prompt to the panel: every
worker of the current pool, then the session's earlier pools read from their
transcripts. Enter opens a worker's transcript — its task, text, tool calls,
approvals and result, one line each, redacted — where ←/→ move to its
siblings and Esc (or ↑ at the top) returns to the conversation. ``x`` stops a
running worker without touching the others, or dismisses a failed row. Keys
come from the key map's ``subagents`` and ``transcript`` contexts (AGX-13).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.subagents.pool import SubagentPoolState, WorkerState
from agenthicc.subagents.transcripts import (
    PoolRecord,
    TranscriptEvent,
    TranscriptTail,
    last_event,
    list_pools,
)
from agenthicc.tui.cbreak_reader import Key, key_name
from agenthicc.tui.keymap import Dispatcher
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agenthicc.tui.conversation_store import AppState

__all__ = [
    "EarlierPool",
    "SubagentTranscriptOverlay",
    "SubagentsOverlay",
    "TranscriptTarget",
    "load_earlier",
    "transcript_lines",
]


@dataclass(frozen=True, slots=True)
class TranscriptTarget:
    """A worker transcript to open, with the siblings ←/→ move between."""

    pool_id: str
    workers: tuple[WorkerState, ...]
    directory: Path
    index: int


@dataclass(frozen=True, slots=True)
class _Entry:
    """A worker row of the focused panel."""

    pool_id: str
    worker: WorkerState
    #: The pool is the one still published (its workers can be stopped).
    current: bool
    directory: Path | None


@dataclass(frozen=True, slots=True)
class EarlierPool:
    """A pool of an earlier turn, as its transcripts left it."""

    record: PoolRecord
    workers: tuple[WorkerState, ...]


def load_earlier(root: Path, *, limit: int = 20) -> tuple[EarlierPool, ...]:
    """The session's recorded pools, newest first (blocking: run it off the loop)."""
    return tuple(
        EarlierPool(record, _record_workers(record)) for record in list_pools(root, limit=limit)
    )


def _record_workers(record: PoolRecord) -> tuple[WorkerState, ...]:
    """An earlier pool's workers, with the status their transcripts end with."""
    workers: list[WorkerState] = []
    for item in record.workers:
        last = last_event(record.transcript_path(item.worker_id))
        if last is None:
            status, activity = "stopped", "no transcript"
        elif last.kind == "finished":
            status = "done" if last.ok else "failed"
            activity = _first_line(last.text)
        else:
            status, activity = "stopped", "interrupted"
        workers.append(
            WorkerState(
                item.label,
                item.agent_type,
                status,
                worker_id=item.worker_id,
                activity=activity,
            )
        )
    return tuple(workers)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _hints(app_state: AppState, context: str, conditions: frozenset[str]) -> str:
    from rich.markup import escape

    hints = app_state.keymap().hints(context, conditions)
    return "   ".join(f"[bold]{escape(h.keys)}[/bold] [dim]{escape(h.label)}[/dim]" for h in hints)


class SubagentsOverlay(Overlay):
    """The subagent panel with focus: select a worker, open, stop or dismiss it."""

    name = "subagents"

    def __init__(
        self,
        app_state: AppState,
        *,
        earlier: Sequence[EarlierPool] = (),
        open_transcript: Callable[[TranscriptTarget], None],
        stop: Callable[[str, str], bool],
        dismiss: Callable[[str, str], bool],
        close: Callable[[], None],
        retry: Callable[[str, str], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state = app_state
        self._earlier = tuple(earlier)
        self._open = open_transcript
        self._stop = stop
        self._dismiss = dismiss
        self._retry = retry
        self._close = close
        self._clock = clock
        self._selected: tuple[str, str] | None = None
        self._height = 12
        self._width = 80
        self._dispatcher = Dispatcher(app_state.keymap())

    def set_viewport(self, width: int, height: int) -> None:
        self._width = max(20, width)
        self._height = max(3, height)

    def entries(self) -> list[_Entry]:
        """The current pool's workers, then the earlier pools' (newest first)."""
        current = self._state.conversation.subagent_pool_state()
        rows: list[_Entry] = []
        if current is not None:
            rows.extend(
                _Entry(current.pool_id, worker, True, current.transcripts)
                for worker in current.workers
                if not worker.dismissed
            )
        for pool in self._earlier:
            record = pool.record
            if current is not None and record.pool_id == current.pool_id:
                continue
            rows.extend(
                _Entry(record.pool_id, worker, False, record.directory) for worker in pool.workers
            )
        return rows

    def selected(self) -> _Entry | None:
        rows = self.entries()
        if not rows:
            return None
        if self._selected is not None:
            for row in rows:
                if (row.pool_id, row.worker.worker_id) == self._selected:
                    return row
        first = rows[0]
        self._selected = (first.pool_id, first.worker.worker_id)
        return first

    def conditions(self) -> frozenset[str]:
        entry = self.selected()
        if entry is None:
            return frozenset()
        flags: set[str] = set()
        if entry.directory is not None:
            flags.add("worker")
        if entry.current and entry.worker.live:
            flags.add("running_worker")
        if entry.current and entry.worker.status == "failed" and not entry.worker.dismissed:
            flags.add("failed_worker")
        if entry.current:
            current = self._state.conversation.subagent_pool_state()
            lifecycle = current.lifecycle(entry.worker.worker_id) if current is not None else None
            if (
                current is not None
                and not current.active
                and lifecycle is not None
                and lifecycle.phase in {"failed", "cancelled"}
            ):
                flags.add("retryable_worker")
        return frozenset(flags)

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        from agenthicc.tui.workspace.subagent_panel import summary_line, worker_line

        now = self._clock()
        width = self._width
        entry = self.selected()
        chosen = (entry.pool_id, entry.worker.worker_id) if entry is not None else None
        lines: list[str] = []
        current = self._state.conversation.subagent_pool_state()
        if current is not None:
            lines.append(summary_line(current, width))
        elif not self._earlier:
            lines.append("  [bold magenta]Subagents[/bold magenta] [dim]none in this session[/dim]")
        pool_seen = ""
        for row in self.entries():
            if not row.current and row.pool_id != pool_seen:
                pool_seen = row.pool_id
                record = next(p.record for p in self._earlier if p.record.pool_id == row.pool_id)
                started = datetime.fromtimestamp(record.created_at).strftime("%Y-%m-%d %H:%M")
                lines.append(
                    f"  [bold]Earlier[/bold] [dim]{started} · {len(record.workers)} workers[/dim]"
                )
            lines.append(
                worker_line(
                    row.worker,
                    width,
                    now=now,
                    selected=(row.pool_id, row.worker.worker_id) == chosen,
                )
            )
        room = max(1, self._height - 1)
        if len(lines) > room:
            index = next((i for i, line in enumerate(lines) if "[reverse]" in line), len(lines) - 1)
            start = min(max(0, index - room // 2), len(lines) - room)
            lines = lines[start : start + room]
        lines.append("  " + _hints(self._state, "subagents", self.conditions()))
        return Group(*(Text.from_markup(line, emoji=False) for line in lines))

    def handle_key(self, key: Key, ch: str) -> bool:
        self._dispatcher.keymap = self._state.keymap()
        action = self._dispatcher.dispatch("subagents", key_name(key, ch), self.conditions())
        if action is None:
            return True
        rows = self.entries()
        entry = self.selected()
        index = rows.index(entry) if entry is not None else 0
        if action.name in ("subagents.up", "subagents.down") and rows:
            step = -1 if action.name == "subagents.up" else 1
            target = rows[max(0, min(len(rows) - 1, index + step))]
            self._selected = (target.pool_id, target.worker.worker_id)
        elif action.name == "subagents.open" and entry is not None and entry.directory is not None:
            siblings = tuple(row.worker for row in rows if row.pool_id == entry.pool_id)
            self._open(
                TranscriptTarget(
                    entry.pool_id, siblings, entry.directory, siblings.index(entry.worker)
                )
            )
        elif action.name == "subagents.stop" and entry is not None:
            self._stop(entry.pool_id, entry.worker.worker_id)
        elif action.name == "subagents.retry" and entry is not None and self._retry is not None:
            self._retry(entry.pool_id, entry.worker.worker_id)
        elif action.name == "subagents.dismiss" and entry is not None:
            if self._dismiss(entry.pool_id, entry.worker.worker_id) and len(rows) > 1:
                neighbour = rows[index + 1] if index + 1 < len(rows) else rows[index - 1]
                self._selected = (neighbour.pool_id, neighbour.worker.worker_id)
        elif action.name == "subagents.back":
            self._close()
        return True


# ── transcripts ──────────────────────────────────────────────────────────────

_EVENT_LOOKS: dict[str, tuple[str, str]] = {
    "prompt": ("▸", "bold"),
    "text": ("”", ""),
    "tool_started": ("●", "cyan"),
    "approval_requested": ("◆", "yellow"),
    "truncated": ("…", "dim"),
}


def transcript_lines(events: Sequence[TranscriptEvent], width: int) -> list[str]:
    """A worker transcript as one markup line per event."""
    from rich.markup import escape

    from agenthicc.tui.rendering import fit, visible_len

    lines: list[str] = []
    for event in events:
        clock = datetime.fromtimestamp(event.at).strftime("%H:%M:%S") if event.at else "        "
        text = " ".join(event.text.split())
        if event.kind == "tool_finished":
            glyph, style = ("✓", "green") if event.ok else ("✗", "red")
            body = f"{event.tool} {text}".strip()
        elif event.kind == "approval_resolved":
            glyph, style = ("✓", "green") if event.ok else ("✗", "red")
            body = f"{event.tool} {'approved' if event.ok else 'denied'}"
        elif event.kind == "finished":
            glyph, style = ("✓", "green") if event.ok else ("✗", "red")
            body = ("done · " if event.ok else "failed · ") + text
        elif event.kind == "truncated":
            glyph, style = _EVENT_LOOKS["truncated"]
            body = f"{event.dropped} earlier events dropped to keep the transcript small"
        elif event.kind == "approval_requested":
            glyph, style = _EVENT_LOOKS["approval_requested"]
            body = f"needs approval: {event.tool} {text}".strip()
        elif event.kind == "tool_started":
            glyph, style = _EVENT_LOOKS["tool_started"]
            body = f"{event.tool}  {text}".strip()
        elif event.kind == "prompt":
            glyph, style = _EVENT_LOOKS["prompt"]
            body = f"task: {text}"
        else:
            glyph, style = _EVENT_LOOKS["text"]
            body = text
        styled = f"[{style}]{glyph}[/{style}]" if style else glyph
        line = f"  [dim]{clock}[/dim]  {styled} {escape(body)}"
        lines.append(fit(line, width) if visible_len(line) > width else line)
    return lines


class SubagentTranscriptOverlay(Overlay):
    """One worker's transcript; its siblings are ← and → away."""

    name = "subagent-transcript"

    def __init__(
        self,
        app_state: AppState,
        target: TranscriptTarget,
        *,
        stop: Callable[[str, str], bool],
        close: Callable[[], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state = app_state
        self.pool_id = target.pool_id
        self._workers = target.workers
        self._directory = target.directory
        self._index = max(0, min(len(self._workers) - 1, target.index))
        self._stop = stop
        self._close = close
        self._clock = clock
        self._tails: dict[str, TranscriptTail] = {}
        self._top = 0
        self._follow = True
        self._height = 12
        self._width = 80
        self._dispatcher = Dispatcher(app_state.keymap())
        self._reset_scroll()

    @property
    def worker_id(self) -> str:
        return self._workers[self._index].worker_id

    def set_viewport(self, width: int, height: int) -> None:
        self._width = max(20, width)
        self._height = max(4, height)

    def _worker(self) -> WorkerState:
        """The shown worker, as the published pool now has it when it is current."""
        shown = self._workers[self._index]
        current = self._state.conversation.subagent_pool_state()
        if isinstance(current, SubagentPoolState) and current.pool_id == self.pool_id:
            return current.worker(shown.worker_id) or shown
        return shown

    def _tail(self) -> TranscriptTail:
        tail = self._tails.get(self.worker_id)
        if tail is None:
            tail = TranscriptTail(self._directory / f"{self.worker_id}.jsonl")
            self._tails[self.worker_id] = tail
        tail.refresh()
        return tail

    def _reset_scroll(self) -> None:
        # A running worker is followed; a finished one opens at its task.
        self._follow = self._worker().live
        self._top = 0

    def conditions(self) -> frozenset[str]:
        worker = self._worker()
        flags = {"worker"}
        current = self._state.conversation.subagent_pool_state()
        if worker.live and current is not None and current.pool_id == self.pool_id:
            flags.add("running_worker")
        return frozenset(flags)

    def _body_rows(self) -> int:
        return max(1, self._height - 3)

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.markup import escape
        from rich.text import Text

        from agenthicc.tui.workspace.subagent_panel import elapsed, status_glyph, tokens

        worker = self._worker()
        glyph, style, word = status_glyph(worker.status)
        facts = [word, elapsed(worker, self._clock()), tokens(worker.tokens)]
        position = f"{self._index + 1}/{len(self._workers)}" if len(self._workers) > 1 else ""
        head = (
            f"  [{style}]{glyph}[/{style}] [bold]{escape(worker.label)}[/bold] "
            f"[dim]({escape(worker.agent_type)}) · {' · '.join(f for f in facts if f)}"
            f"{'   ←→ ' + position if position else ''}[/dim]"
        )
        body = transcript_lines(self._tail().events, self._width)
        rows = self._body_rows()
        last = max(0, len(body) - rows)
        if self._follow:
            self._top = last
        self._top = max(0, min(self._top, last))
        shown = body[self._top : self._top + rows] or ["  [dim]No events yet.[/dim]"]
        lines = [head, *shown]
        lines.append("  " + _hints(self._state, "transcript", self.conditions()))
        return Group(*(Text.from_markup(line, emoji=False) for line in lines))

    def handle_key(self, key: Key, ch: str) -> bool:
        self._dispatcher.keymap = self._state.keymap()
        action = self._dispatcher.dispatch("transcript", key_name(key, ch), self.conditions())
        if action is None:
            return True
        name = action.name
        page = self._body_rows()
        if name == "transcript.up":
            if self._top == 0:
                self._close()
                return True
            self._follow = False
            self._top -= 1
        elif name == "transcript.down":
            self._top += 1
            self._follow = False
        elif name == "transcript.page_up":
            self._follow = False
            self._top = max(0, self._top - page)
        elif name == "transcript.page_down":
            self._follow = False
            self._top += page
        elif name in ("transcript.previous", "transcript.next") and len(self._workers) > 1:
            step = -1 if name == "transcript.previous" else 1
            self._index = (self._index + step) % len(self._workers)
            self._reset_scroll()
        elif name == "transcript.follow":
            self._follow = not self._follow
        elif name == "transcript.stop":
            self._stop(self.pool_id, self.worker_id)
        elif name == "transcript.back":
            self._close()
        return True
