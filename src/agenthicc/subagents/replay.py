"""Replay one subagent pool's timeline from what is on disk (SUBAGENT-09 goal 9).

:func:`replay_pool` merges the pool's worker transcripts with its durable
journal records into one ordered timeline. :data:`REPLAY_FIDELITY` states what
that timeline can and cannot show.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from agenthicc.subagents.transcripts import TRANSCRIPT_TEXT_LIMIT, read_transcript

if TYPE_CHECKING:
    from agenthicc.memory.journal import ConversationJournal

__all__ = ["REPLAY_FIDELITY", "PoolTimeline", "TimelineEntry", "replay_pool"]

#: What a replayed timeline is, in one line.
REPLAY_FIDELITY: Final = (
    "Transcripts and journal records only: the five Subagent* kernel events are "
    "projection-only and never stored, so they cannot contribute; the timeline "
    "replays what happened and re-derives no pool state."
)


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One thing that happened to a pool or one of its workers."""

    at: float
    source: Literal["transcript", "journal"]
    #: The transcript kind (``prompt``, ``tool_started``…) or journal kind.
    kind: str
    worker_id: str = ""
    attempt: int = 1
    #: Redacted and bounded.
    text: str = ""
    ok: bool | None = None


@dataclass(frozen=True, slots=True)
class PoolTimeline:
    """A pool's merged timeline, oldest first."""

    pool_id: str
    entries: tuple[TimelineEntry, ...]
    #: A transcript was compacted or failed to write, so events are missing.
    partial: bool
    fidelity: str = REPLAY_FIDELITY


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _journal_text(record: Mapping[str, object]) -> str:
    for key in ("text", "error", "reason"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _journal_entries(records: list[dict[str, object]], start: float) -> list[TimelineEntry]:
    from agenthicc.tui.runtime.session_export import _Redactor

    redactor = _Redactor()
    entries: list[TimelineEntry] = []
    previous = start
    for record in records:
        # A record written before records carried ``at`` takes its
        # predecessor's time, which keeps journal order.
        at = _number(record.get("at"))
        previous = at if at is not None else previous
        worker = record.get("task_id")
        attempt = record.get("attempt", 1)
        ok = record.get("ok")
        text = str(redactor.value(_journal_text(record), "text"))
        entries.append(
            TimelineEntry(
                at=previous,
                source="journal",
                kind=str(record.get("kind", "")),
                worker_id=worker if isinstance(worker, str) else "",
                attempt=attempt
                if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1
                else 1,
                text=text[:TRANSCRIPT_TEXT_LIMIT],
                ok=ok if isinstance(ok, bool) else None,
            )
        )
    return entries


def replay_pool(
    pool_id: str,
    *,
    journal: ConversationJournal | None,
    transcripts_root: Path | None,
) -> PoolTimeline:
    """Merge *pool_id*'s transcripts under *transcripts_root* with its journal records.

    Entries are ordered by time; at equal times journal records come after
    transcript events, since a worker's result is persisted after its last
    transcript line. See :data:`REPLAY_FIDELITY`.
    """
    transcript_entries: list[TimelineEntry] = []
    partial = False
    directory = transcripts_root / pool_id if transcripts_root is not None else None
    if directory is not None and directory.is_dir():
        for path in sorted(directory.glob("*.jsonl")):
            for event in read_transcript(path):
                if event.kind == "truncated":
                    partial = True
                    continue
                transcript_entries.append(
                    TimelineEntry(
                        at=event.at,
                        source="transcript",
                        kind=event.kind,
                        worker_id=event.worker_id or path.stem,
                        attempt=event.attempt,
                        text=event.text,
                        ok=event.ok,
                    )
                )
    records = journal.subagent_pool_records(pool_id) if journal is not None else []
    start = min((entry.at for entry in transcript_entries), default=0.0)
    entries = transcript_entries + _journal_entries(records, start)
    order = {"transcript": 0, "journal": 1}
    entries.sort(key=lambda entry: (entry.at, order[entry.source]))
    return PoolTimeline(pool_id=pool_id, entries=tuple(entries), partial=partial)
