"""Durable conversation journal (PRD-129 Phases 2 & 3).

An append-only, ``fsync``-ed record of every conversation-memory transition:
message appends (user / assistant / tool-result), full-state resets (retry
rollbacks and compaction), turn-lifecycle markers, and durable tool results.
The live :class:`~lauren_ai._memory.ShortTermMemory` becomes a *projection* of
this journal — folding it reconstructs the message list, so a process crash
mid-turn no longer loses the in-flight turn.  On restart the journal is folded
straight back into memory.

Entry format — one JSON object per line::

    {"seq": 0, "kind": "append", "message": {...}}
    {"seq": 1, "kind": "reset",  "messages": [...], "summary": "..."}
    {"seq": 2, "kind": "turn_started",   "turn_id": "...", "user_message": "...", "base_count": 4}
    {"seq": 3, "kind": "tool_recorded",  "turn_id": "...", "key": "...", "result": {...}}
    {"seq": 4, "kind": "turn_completed", "turn_id": "..."}

:func:`fold_path` (Phase 2) replays ``append`` / ``reset`` to rebuild the message
list, ignoring the Phase 3 markers.  :func:`fold_resume_state` (Phase 3) replays
the turn markers + tool records to find an **incomplete** turn (a
``turn_started`` with no matching ``turn_completed``) and the tools it already
ran — everything a :class:`RunCoordinator` needs to resume it.  A corrupt
trailing line — the signature of a crash mid-write — is skipped, mirroring the
kernel's ``restore_from_log``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ConversationJournal",
    "IncompleteTurn",
    "fold_path",
    "fold_resume_state",
    "journal_path_for",
]

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"


@dataclass(frozen=True)
class IncompleteTurn:
    """A turn that was started but never completed — recovered on resume.

    :param turn_id: The turn's stable identifier (reused when re-driving).
    :param user_message: The user message that drove the turn (re-submitted).
    :param base_count: Message count *before* the turn began — the rollback
        point so the re-drive starts from a clean pre-turn history.
    :param tool_records: ``(key, result_payload)`` for every tool the turn
        already executed, in order; replayed so side effects don't repeat.
    """

    turn_id: str
    user_message: str
    base_count: int
    tool_records: list[tuple[str, object]] = field(default_factory=list)


def journal_path_for(session_id: str) -> Path:
    """Return the durable journal path for *session_id*.

    Sits alongside the kernel event log and TUI conversation log under
    ``~/.agenthicc/sessions/<session_id>/``.
    """
    return _SESSIONS_DIR / session_id / "conversation-journal.jsonl"


def fold_path(path: Path) -> tuple[list[object], str | None]:
    """Fold a journal file into ``(messages, summary)``.

    A missing file folds to ``([], None)``.  Corrupt trailing lines are skipped.
    """
    if not path.exists():
        return [], None
    messages: list[object] = []
    summary: str | None = None
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Crash mid-write left a partial last line — stop folding here;
                # everything before it is intact and durable.
                break
            kind = entry.get("kind")
            if kind == "append":
                messages.append(entry["message"])
            elif kind == "reset":
                messages = list(entry.get("messages", []))
                summary = entry.get("summary")
    return messages, summary


def fold_resume_state(path: Path) -> IncompleteTurn | None:
    """Find the last incomplete turn in a journal, or ``None`` if all complete.

    An incomplete turn is a ``turn_started`` whose ``turn_id`` has no later
    ``turn_completed`` — the signature of a crash mid-turn.  Its already-executed
    tool results (``tool_recorded`` entries) are returned so the re-drive can
    replay them instead of re-running their side effects.
    """
    if not path.exists():
        return None
    started: list[tuple[str, str, int]] = []  # (turn_id, user_message, base_count)
    completed: set[str] = set()
    records: dict[str, list[tuple[str, object]]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                break
            kind = entry.get("kind")
            if kind == "turn_started":
                tid = entry["turn_id"]
                started.append(
                    (tid, entry.get("user_message", ""), int(entry.get("base_count", 0)))
                )
                records.setdefault(tid, [])
            elif kind == "turn_completed":
                completed.add(entry["turn_id"])
            elif kind == "tool_recorded":
                records.setdefault(entry["turn_id"], []).append((entry["key"], entry.get("result")))
    # The most recent started-but-not-completed turn is the one to resume.
    for tid, user_message, base_count in reversed(started):
        if tid not in completed:
            return IncompleteTurn(tid, user_message, base_count, records.get(tid, []))
    return None


class ConversationJournal:
    """Append-only, ``fsync``-ed JSONL journal of conversation transitions.

    Opening an existing journal (resume) continues the sequence; the prior
    content is replayed via :meth:`fold`.
    """

    __slots__ = ("_path", "_seq", "_fh")

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._count_existing()
        self._fh = path.open("a", encoding="utf-8")

    def _count_existing(self) -> int:
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def _write(self, entry: dict[str, object]) -> None:
        self._fh.write(json.dumps(entry, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._seq += 1

    def append_message(self, message: object) -> None:
        """Durably record one appended message."""
        self._write({"seq": self._seq, "kind": "append", "message": message})

    def reset(self, messages: list[object], summary: str | None) -> None:
        """Durably record a full-state replacement (rollback / compaction)."""
        self._write(
            {
                "seq": self._seq,
                "kind": "reset",
                "messages": list(messages),
                "summary": summary,
            }
        )

    # ── Phase 3: turn lifecycle + durable tool records ───────────────────────

    def turn_started(self, turn_id: str, user_message: str, base_count: int) -> None:
        """Mark the start of a turn and the rollback point that precedes it."""
        self._write(
            {
                "seq": self._seq,
                "kind": "turn_started",
                "turn_id": turn_id,
                "user_message": user_message,
                "base_count": base_count,
            }
        )

    def turn_completed(self, turn_id: str) -> None:
        """Mark a turn as durably complete (it will not be resumed)."""
        self._write({"seq": self._seq, "kind": "turn_completed", "turn_id": turn_id})

    def tool_recorded(self, turn_id: str, key: str, result: object) -> None:
        """Durably record one executed tool result for idempotent replay."""
        self._write(
            {
                "seq": self._seq,
                "kind": "tool_recorded",
                "turn_id": turn_id,
                "key": key,
                "result": result,
            }
        )

    def fold(self) -> tuple[list[object], str | None]:
        """Reconstruct ``(messages, summary)`` by replaying the on-disk journal."""
        return fold_path(self._path)

    def resume_state(self) -> IncompleteTurn | None:
        """Return the incomplete turn to resume, or ``None``."""
        return fold_resume_state(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
