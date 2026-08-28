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

Subagent worker and pool results are auxiliary records in this journal. They
are ignored by the provider-message fold and projected separately by the
subagent resume cache. This matters when a parent is cancelled after a pool
finishes but before lauren-ai commits the parent tool exchange.

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
from collections.abc import Mapping
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
            elif kind in {"turn_completed", "turn_aborted", "turn_recovered"}:
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

    def turn_aborted(self, turn_id: str, *, reason: str = "cancelled") -> None:
        """Mark a turn as intentionally aborted before natural completion.

        The memory owner rolls back any incomplete message tail before this
        marker is written.  The marker closes lifecycle bookkeeping without
        pretending that the provider produced a completed assistant turn.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "turn_aborted",
                "turn_id": turn_id,
                "reason": reason,
            }
        )

    def turn_recovered(self, turn_id: str) -> None:
        """Mark a crash-interrupted turn as recovered and re-driven."""
        self._write({"seq": self._seq, "kind": "turn_recovered", "turn_id": turn_id})

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

    def workflow_phase_boundary(
        self,
        run_id: str,
        workflow_name: str,
        *,
        completed_phase: str,
        next_phase: str | None,
        phase_index: int,
        phase_iteration: int,
        outcome: str,
        plan_version: str = "",
        boundary_key: str = "",
    ) -> None:
        """Record redacted workflow-boundary metadata after a checkpoint.

        This is an auxiliary recovery index, not a second workflow state
        store. It is written only after the workflow checkpoint succeeds and
        contains no prompt, tool arguments, artifact bodies, or credentials.
        The checkpoint remains authoritative when an older journal lacks this
        record or a journal write is interrupted.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "workflow_phase_boundary",
                "run_id": str(run_id)[:128],
                "workflow_name": str(workflow_name)[:128],
                "completed_phase": str(completed_phase)[:128],
                "next_phase": str(next_phase)[:128] if next_phase is not None else None,
                "phase_index": max(0, int(phase_index)),
                "phase_iteration": max(0, int(phase_iteration)),
                "outcome": str(outcome)[:96],
                "plan_version": str(plan_version)[:128],
                "boundary_key": str(boundary_key)[:512],
            }
        )

    def fold_workflow_phase_boundaries(
        self,
        run_id: str,
        workflow_name: str,
    ) -> list[dict[str, object]]:
        """Return valid boundary records for one run in journal order.

        Invalid records and a corrupt trailing line are ignored. A journal is
        an auxiliary source for resume reconciliation, so malformed metadata
        must never make an otherwise valid checkpoint unusable.
        """
        if not self._path.exists():
            return []
        result: list[dict[str, object]] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    break
                if not isinstance(entry, dict):
                    continue
                if (
                    entry.get("kind") != "workflow_phase_boundary"
                    or entry.get("run_id") != run_id
                    or entry.get("workflow_name") != workflow_name
                    or not isinstance(entry.get("completed_phase"), str)
                    or not entry["completed_phase"].strip()
                    or not isinstance(entry.get("phase_index"), int)
                    or isinstance(entry["phase_index"], bool)
                    or entry["phase_index"] < 0
                    or not isinstance(entry.get("phase_iteration"), int)
                    or isinstance(entry["phase_iteration"], bool)
                    or entry["phase_iteration"] < 0
                ):
                    continue
                result.append({str(key): value for key, value in entry.items()})
        return result

    # ── PRD-169: tool-exchange lifecycle ────────────────────────────────────

    def tool_exchange_started(
        self,
        exchange_id: str,
        *,
        run_id: str | None,
        call_count: int,
        call_ids: list[str],
    ) -> None:
        """Persist the start of a provider-neutral tool exchange.

        IDs are hashed before they enter diagnostics so a journal cannot be
        used as a second source of provider correlation data. The canonical
        messages remain in the normal journal projection.
        """
        import hashlib

        self._write(
            {
                "seq": self._seq,
                "kind": "tool_exchange_started",
                "exchange_id": exchange_id,
                "run_id": run_id,
                "call_count": call_count,
                "call_ids": [hashlib.sha256(value.encode()).hexdigest()[:12] for value in call_ids],
            }
        )

    def tool_exchange_committed(
        self,
        exchange_id: str,
        *,
        call_count: int,
        completed_count: int,
        synthetic_count: int,
    ) -> None:
        """Persist a successful or synthesized exchange commit."""
        self._write(
            {
                "seq": self._seq,
                "kind": "tool_exchange_committed",
                "exchange_id": exchange_id,
                "call_count": call_count,
                "completed_count": completed_count,
                "synthetic_count": synthetic_count,
            }
        )

    def tool_exchange_result_recorded(
        self,
        exchange_id: str,
        *,
        tool_use_id: str,
        status: str,
        synthetic: bool,
    ) -> None:
        """Record safe, hashed metadata for one committed exchange result."""
        import hashlib

        self._write(
            {
                "seq": self._seq,
                "kind": "tool_exchange_result_recorded",
                "exchange_id": exchange_id,
                "tool_use_id_hash": hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:12],
                "status": status,
                "synthetic": synthetic,
            }
        )

    def tool_exchange_aborted(
        self,
        exchange_id: str,
        *,
        call_count: int,
        repaired: bool,
    ) -> None:
        """Persist an interrupted exchange and whether it was repaired."""
        self._write(
            {
                "seq": self._seq,
                "kind": "tool_exchange_aborted",
                "exchange_id": exchange_id,
                "call_count": call_count,
                "repaired": repaired,
            }
        )

    # ── subagent output durability ──────────────────────────────────────────

    def subagent_worker_result(
        self,
        *,
        pool_id: str,
        fingerprint: str,
        task_id: str,
        agent_type: str,
        label: str,
        ok: bool,
        text: str,
        error: str,
        duration_ms: float,
        tool_calls: list[str],
        changed_paths: list[str],
    ) -> None:
        """Persist one worker result, including its complete final text.

        Worker memory is deliberately isolated and short-lived. The worker
        result is therefore the durable representation of work produced before
        the parent tool exchange commits.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "subagent_worker_result",
                "schema_version": 1,
                "pool_id": pool_id,
                "fingerprint": fingerprint,
                "task_id": task_id,
                "agent_type": agent_type,
                "label": label,
                "ok": ok,
                "text": text,
                "error": error,
                "duration_ms": duration_ms,
                "tool_calls": list(tool_calls),
                "changed_paths": list(changed_paths),
            }
        )

    def subagent_pool_result(
        self,
        *,
        pool_id: str,
        fingerprint: str,
        total: int,
        succeeded: int,
        failed: int,
        text: str,
    ) -> None:
        """Persist a complete aggregate used by ``spawn_subagents`` resume.

        Partial pools are intentionally not written through this method as
        successful cache entries. Their individual worker records remain
        available for diagnostics without hiding failed work on resume.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "subagent_pool_result",
                "schema_version": 1,
                "pool_id": pool_id,
                "fingerprint": fingerprint,
                "total": total,
                "succeeded": succeeded,
                "failed": failed,
                "text": text,
            }
        )

    def fold_subagent_worker_results(self) -> list[dict[str, object]]:
        """Return durable worker result records in journal order.

        This projection is an audit/recovery view and does not enter the
        provider-message fold. A corrupt trailing JSONL line is treated as an
        interrupted write, matching the other journal projections.
        """
        return self._fold_subagent_kind("subagent_worker_result")

    def fold_subagent_pool_results(self) -> list[dict[str, object]]:
        """Return complete durable pool results in journal order."""
        records = self._fold_subagent_kind("subagent_pool_result")
        valid: list[dict[str, object]] = []
        for record in records:
            failed_value = record.get("failed", 1)
            if isinstance(failed_value, bool) or not isinstance(failed_value, (int, float, str)):
                continue
            try:
                failed = int(failed_value)
            except (TypeError, ValueError):
                continue
            if (
                failed == 0
                and isinstance(record.get("fingerprint"), str)
                and isinstance(record.get("text"), str)
            ):
                valid.append(record)
        return valid

    def _fold_subagent_kind(self, kind: str) -> list[dict[str, object]]:
        """Fold one auxiliary subagent record kind, tolerating a bad tail."""
        if not self._path.exists():
            return []
        records: list[dict[str, object]] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    break
                if not isinstance(entry, dict):
                    continue
                if entry.get("kind") == kind:
                    records.append(entry)
        return records

    def append_usage_record(self, record: Mapping[str, object]) -> None:
        """Durably append one PRD-157 provider-usage record.

        Usage entries share the session journal with provider-memory and
        workflow durability, but are intentionally ignored by ``fold()`` and
        ``fold_resume_state()``. The usage ledger folds them independently.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "usage_record",
                "schema_version": 1,
                "record": dict(record),
            }
        )

    def fold_usage_records(self) -> list[dict[str, object]]:
        """Return the latest valid usage record for each record ID."""
        if not self._path.exists():
            return []
        latest: dict[str, dict[str, object]] = {}
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    break
                if entry.get("kind") != "usage_record":
                    continue
                record = entry.get("record")
                if not isinstance(record, dict):
                    continue
                record_id = record.get("record_id")
                if isinstance(record_id, str) and record_id:
                    latest[record_id] = record
        return list(latest.values())

    def fold(self) -> tuple[list[object], str | None]:
        """Reconstruct ``(messages, summary)`` by replaying the on-disk journal."""
        return fold_path(self._path)

    def resume_state(self) -> IncompleteTurn | None:
        """Return the incomplete turn to resume, or ``None``."""
        return fold_resume_state(self._path)

    @property
    def cursor(self) -> int:
        """Return the next journal sequence number for checkpoint cursors."""
        return self._seq

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
