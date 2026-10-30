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
    {"seq": 2, "kind": "turn_started",   "turn_id": "...", "schema_version": 2}
    {"seq": 3, "kind": "step_started",  "turn_id": "...", "step_id": "...", "attempt_id": "..."}
    {"seq": 4, "kind": "step_committed", "turn_id": "...", "step_id": "...", "cursor": 7}
    {"seq": 5, "kind": "partial_fragment", "turn_id": "...", "text": "..."}
    {"seq": 6, "kind": "turn_failed",   "turn_id": "...", "last_committed_step": "..."}

Subagent worker and pool results are auxiliary records in this journal. They
are ignored by the provider-message fold and projected separately by the
subagent resume cache. This matters when a parent is cancelled after a pool
finishes but before lauren-ai commits the parent tool exchange.

:func:`fold_path` (Phase 2) replays ``append`` / ``reset`` to rebuild the message
list, ignoring lifecycle and diagnostic records.  :func:`fold_resume_state`
(Phase 3/PRD-182) replays turn markers, step receipts, and tool records to find
an **incomplete** turn (a ``turn_started`` with no matching terminal marker),
including its latest safe provider-step cursor. A corrupt trailing line — the
signature of a crash mid-write — is skipped, mirroring the kernel's
``restore_from_log``. ``turn_recovery_started`` is non-terminal; only
``turn_recovered`` written after a completed recovery handoff closes that
legacy lifecycle marker.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "ConversationJournal",
    "IncompleteTurn",
    "fold_path",
    "fold_resume_state",
    "journal_path_for",
    "tool_exchange_recovery_event_id",
]

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
logger = logging.getLogger(__name__)


def tool_exchange_recovery_event_id(exchange_id: str, event: str = "repaired") -> str:
    """Derive the PRD-191 stable ID without requiring a new lauren-ai API."""
    value = f"lauren-ai:tool-exchange:{event}:{exchange_id}"
    return f"tool-exchange-{event}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"


@dataclass(frozen=True)
class IncompleteTurn:
    """A turn that was started but never completed — recovered on resume.

    :param turn_id: The turn's stable identifier (reused when re-driving).
    :param user_message: The user message that drove the turn (re-submitted).
    :param base_count: Message count *before* the turn began — the rollback
        point retained for legacy consumers. New recovery must not use it to
        erase committed provider steps.
    :param tool_records: ``(key, result_payload)`` for every tool the turn
        already executed, in order; replayed so side effects don't repeat.
    :param last_committed_step: The most recent durable provider-step ID.
    :param last_committed_cursor: Journal cursor recorded for that step.
    :param interrupted_step_id: The provider step that was active at failure,
        when the journal contains a step interruption receipt.
    :param interrupted_attempt_id: The failed provider attempt ID, when known.
    """

    turn_id: str
    user_message: str
    base_count: int
    tool_records: list[tuple[str, object]] = field(default_factory=list)
    last_committed_step: str = ""
    last_committed_cursor: int | None = None
    interrupted_step_id: str | None = None
    interrupted_attempt_id: str | None = None
    workflow_run_id: str | None = None
    execution_kind: Literal["direct", "workflow"] | None = None


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
    messages, summary, _corrupt_tail_records = fold_path_with_report(path)
    return messages, summary


def fold_path_with_report(path: Path) -> tuple[list[object], str | None, int]:
    """Fold a journal and report non-empty records skipped after corrupt JSON."""
    if not path.exists():
        return [], None, 0
    messages: list[object] = []
    summary: str | None = None
    pending_replace: tuple[str, list[object], str | None] | None = None
    corrupt_tail_records = 0

    def rollback_pending() -> None:
        nonlocal messages, summary, pending_replace
        if pending_replace is not None:
            _transaction_id, previous_messages, previous_summary = pending_replace
            messages = list(previous_messages)
            summary = previous_summary
            pending_replace = None

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
                corrupt_tail_records = 1 + sum(1 for tail in fh if tail.strip())
                break
            kind = entry.get("kind")
            if kind == "state_replace_prepare":
                rollback_pending()
                transaction_id = entry.get("transaction_id")
                if isinstance(transaction_id, str):
                    pending_replace = (
                        transaction_id,
                        list(entry.get("previous_messages", [])),
                        entry.get("previous_summary"),
                    )
            elif kind == "reset":
                transaction_id = entry.get("transaction_id")
                if pending_replace is not None and transaction_id == pending_replace[0]:
                    messages = list(entry.get("messages", []))
                    summary = entry.get("summary")
                else:
                    rollback_pending()
                    messages = list(entry.get("messages", []))
                    summary = entry.get("summary")
            elif kind == "state_replace_commit":
                if (
                    pending_replace is not None
                    and entry.get("transaction_id") == pending_replace[0]
                ):
                    pending_replace = None
            elif kind == "append":
                rollback_pending()
                messages.append(entry["message"])
    rollback_pending()
    return messages, summary, corrupt_tail_records


def fold_resume_state(path: Path) -> IncompleteTurn | None:
    """Find the last incomplete turn in a journal, or ``None`` if all complete.

    An incomplete turn is a ``turn_started`` whose ``turn_id`` has no later
    terminal marker (``turn_completed``, ``turn_failed``, or ``turn_aborted``)
    — the signature of a crash mid-turn. Its already-executed tool results
    (``tool_recorded`` entries) and step receipts are returned so the re-drive
    can replay them instead of re-running their side effects.
    """
    if not path.exists():
        return None
    started: list[tuple[str, str, int]] = []  # (turn_id, user_message, base_count)
    workflow_runs: dict[str, str | None] = {}
    execution_kinds: dict[str, Literal["direct", "workflow"] | None] = {}
    completed: set[str] = set()
    records: dict[str, list[tuple[str, object]]] = {}
    committed: dict[str, tuple[str, int | None]] = {}
    interrupted: dict[str, tuple[str, str]] = {}
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
                workflow_run_id = entry.get("workflow_run_id")
                if isinstance(workflow_run_id, str) and workflow_run_id:
                    workflow_runs[tid] = workflow_run_id[:128]
                else:
                    workflow_runs.setdefault(tid, None)
                raw_execution_kind = entry.get("execution_kind")
                if raw_execution_kind == "workflow":
                    execution_kinds[tid] = raw_execution_kind
                elif raw_execution_kind == "direct":
                    # Re-starting the same logical turn without workflow
                    # context must not erase provenance from an earlier
                    # durable workflow attempt.
                    if execution_kinds.get(tid) != "workflow":
                        execution_kinds[tid] = raw_execution_kind
                elif tid not in execution_kinds and workflow_runs.get(tid):
                    execution_kinds[tid] = "workflow"
                else:
                    execution_kinds.setdefault(tid, None)
                records.setdefault(tid, [])
            elif kind in {"turn_completed", "turn_aborted", "turn_failed", "turn_recovered"}:
                completed.add(entry["turn_id"])
            elif kind == "tool_recorded":
                records.setdefault(entry["turn_id"], []).append((entry["key"], entry.get("result")))
            elif kind == "step_committed":
                turn_id = str(entry.get("turn_id", ""))
                if turn_id:
                    raw_cursor = entry.get("cursor")
                    committed[turn_id] = (
                        str(entry.get("step_id", "")),
                        int(raw_cursor) if isinstance(raw_cursor, int) else None,
                    )
            elif kind == "step_interrupted":
                turn_id = str(entry.get("turn_id", ""))
                if turn_id:
                    interrupted[turn_id] = (
                        str(entry.get("step_id", "")),
                        str(entry.get("attempt_id", "")),
                    )
    # The most recent started-but-not-completed turn is the one to resume.
    for tid, user_message, base_count in reversed(started):
        if tid not in completed:
            last_step, last_cursor = committed.get(tid, ("", None))
            interrupted_step, interrupted_attempt = interrupted.get(tid, (None, None))
            return IncompleteTurn(
                tid,
                user_message,
                base_count,
                records.get(tid, []),
                last_committed_step=last_step,
                last_committed_cursor=last_cursor,
                interrupted_step_id=interrupted_step,
                interrupted_attempt_id=interrupted_attempt,
                workflow_run_id=workflow_runs.get(tid),
                execution_kind=execution_kinds.get(tid),
            )
    return None


class ConversationJournal:
    """Append-only, ``fsync``-ed JSONL journal of conversation transitions.

    Opening an existing journal (resume) continues the sequence; the prior
    content is replayed via :meth:`fold`.
    """

    __slots__ = ("_path", "_seq", "_fh", "_corrupt_trailing_records")

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._count_existing()
        self._corrupt_trailing_records = 0
        self._fh = path.open("a", encoding="utf-8")

    def _count_existing(self) -> int:
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def _write(self, entry: dict[str, object]) -> None:
        self._fh.flush()
        descriptor = self._fh.fileno()
        start_offset = os.fstat(descriptor).st_size
        payload = (json.dumps(entry, default=str) + "\n").encode("utf-8")
        offset = 0
        try:
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("conversation journal write made no progress")
                offset += written
            os.fsync(descriptor)
        except BaseException:
            # A partial JSONL record must not become visible as a successful
            # state transition. Restore the exact previous EOF before
            # propagating the original disk error.
            try:
                os.ftruncate(descriptor, start_offset)
                os.fsync(descriptor)
            except OSError as rollback_exc:
                logger.critical(
                    "conversation journal append rollback failed (%s)",
                    type(rollback_exc).__name__,
                )
            raise
        self._seq += 1

    def replace_state(self, messages: list[object], summary: str | None) -> None:
        """Atomically journal a full projection replacement for compaction.

        The prepare record stores the prior folded projection. If a process
        stops after the candidate reset but before its commit receipt, folding
        rolls back to the prior projection. All records are fsynced
        individually, and a failed/partial final record is truncated by
        :meth:`_write` when the filesystem permits it.
        """
        previous_messages, previous_summary = self.fold()
        transaction_id = uuid.uuid4().hex
        self._write(
            {
                "seq": self._seq,
                "kind": "state_replace_prepare",
                "transaction_id": transaction_id,
                "previous_messages": previous_messages,
                "previous_summary": previous_summary,
            }
        )
        self._write(
            {
                "seq": self._seq,
                "kind": "reset",
                "transaction_id": transaction_id,
                "messages": list(messages),
                "summary": summary,
            }
        )
        self._write(
            {
                "seq": self._seq,
                "kind": "state_replace_commit",
                "transaction_id": transaction_id,
            }
        )

    def append_message(self, message: object, *, message_id: str = "") -> None:
        """Durably record one appended message, optionally with a stable receipt ID."""
        entry: dict[str, object] = {"seq": self._seq, "kind": "append", "message": message}
        if message_id:
            entry["message_id"] = str(message_id)[:160]
        self._write(entry)

    def has_message_id(self, message_id: str) -> bool:
        """Whether an idempotent message append with *message_id* is durable."""
        if not message_id or not self._path.exists():
            return False
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    break
                if (
                    isinstance(entry, dict)
                    and entry.get("kind") == "append"
                    and entry.get("message_id") == message_id
                ):
                    return True
        return False

    def reset(
        self,
        messages: list[object],
        summary: str | None,
        *,
        turn_id: str = "",
        step_id: str = "",
        reason: str = "",
    ) -> None:
        """Durably record a full-state replacement (rollback / compaction).

        Optional provider-step metadata makes an attempt-local rollback
        auditable while keeping the original two-argument API compatible with
        compaction and older callers.
        """
        entry: dict[str, object] = {
            "seq": self._seq,
            "kind": "reset",
            "messages": list(messages),
            "summary": summary,
        }
        if turn_id or step_id or reason:
            entry.update(
                {
                    "schema_version": 1,
                    "turn_id": str(turn_id)[:128],
                    "step_id": str(step_id)[:160],
                    "reason": str(reason)[:96],
                }
            )
        self._write(entry)

    def partial_fragment(
        self,
        turn_id: str,
        *,
        step_id: str = "",
        attempt_id: str = "",
        text: str,
        interrupted: bool = True,
    ) -> None:
        """Persist bounded diagnostic text from an incomplete provider step.

        A partial fragment is deliberately not an ``append`` message, so
        journal folding never feeds it back to a provider as valid assistant
        content. The TUI may project it as interrupted output.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "partial_fragment",
                "schema_version": 1,
                "turn_id": str(turn_id)[:128],
                "step_id": str(step_id)[:160],
                "attempt_id": str(attempt_id)[:160],
                "text": str(text)[:16_384],
                "interrupted": bool(interrupted),
            }
        )

    # ── Phase 3: turn lifecycle + durable tool records ───────────────────────

    def turn_started(
        self,
        turn_id: str,
        user_message: str,
        base_count: int,
        *,
        conversation_id: str = "",
        base_cursor: int | None = None,
        workflow_run_id: str | None = None,
    ) -> None:
        """Mark the start of a turn and its durable pre-turn checkpoint.

        ``base_count`` is retained for old resume consumers. ``base_cursor``
        and ``conversation_id`` make the newer step-aware recovery contract
        explicit without changing how legacy journals fold.
        """
        entry: dict[str, object] = {
            "seq": self._seq,
            "kind": "turn_started",
            "schema_version": 3,
            "execution_kind": "workflow" if workflow_run_id else "direct",
            "turn_id": str(turn_id)[:128],
            "conversation_id": str(conversation_id)[:128],
            "user_message": user_message,
            "base_count": max(0, int(base_count)),
            "base_cursor": base_cursor if base_cursor is not None else self._seq,
        }
        if workflow_run_id:
            entry["workflow_run_id"] = str(workflow_run_id)[:128]
        self._write(entry)

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

    def turn_failed(
        self,
        turn_id: str,
        *,
        last_committed_step: str = "",
        cursor: int | None = None,
        error_kind: str = "error",
        retryable: bool = False,
    ) -> None:
        """Close a failed turn without rolling back committed conversation state.

        ``turn_failed`` is intentionally distinct from ``turn_aborted``:
        cancellation is an intentional user action, while a provider/tool
        failure is a durable outcome that must retain the valid messages
        produced before the failure.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "turn_failed",
                "turn_id": turn_id,
                "last_committed_step": last_committed_step,
                "cursor": cursor if cursor is not None else self._seq,
                "error_kind": str(error_kind)[:96],
                "retryable": bool(retryable),
            }
        )

    def step_started(
        self,
        turn_id: str,
        step_id: str,
        attempt_id: str,
        *,
        step_index: int,
        base_cursor: int | None = None,
    ) -> None:
        """Record the beginning of one provider-step attempt.

        ``base_cursor`` identifies the projection from which this attempt
        started. It is diagnostic metadata only; folding still uses append and
        reset records as the message source of truth.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "step_started",
                "schema_version": 1,
                "turn_id": str(turn_id)[:128],
                "step_id": str(step_id)[:160],
                "attempt_id": str(attempt_id)[:160],
                "step_index": max(0, int(step_index)),
                "base_cursor": base_cursor if base_cursor is not None else self._seq,
            }
        )

    def step_committed(
        self,
        turn_id: str,
        step_id: str,
        *,
        step_index: int,
        cursor: int | None = None,
        message_count: int = 0,
    ) -> None:
        """Record a provider step after its messages are durable."""
        self._write(
            {
                "seq": self._seq,
                "kind": "step_committed",
                "schema_version": 1,
                "turn_id": str(turn_id)[:128],
                "step_id": str(step_id)[:160],
                "step_index": max(0, int(step_index)),
                "cursor": cursor if cursor is not None else self._seq,
                "message_count": max(0, int(message_count)),
            }
        )

    def step_interrupted(
        self,
        turn_id: str,
        step_id: str,
        attempt_id: str,
        *,
        error_kind: str,
        partial_chars: int,
        retryable: bool,
    ) -> None:
        """Record a failed provider attempt without storing raw prompt data."""
        self._write(
            {
                "seq": self._seq,
                "kind": "step_interrupted",
                "schema_version": 1,
                "turn_id": str(turn_id)[:128],
                "step_id": str(step_id)[:160],
                "attempt_id": str(attempt_id)[:160],
                "error_kind": str(error_kind)[:96],
                "partial_chars": max(0, int(partial_chars)),
                "retryable": bool(retryable),
            }
        )

    def turn_recovered(self, turn_id: str) -> None:
        """Mark a crash-interrupted turn as fully recovered and re-driven.

        This is a terminal marker retained for compatibility with callers that
        write it after the resumed turn has actually been handed off. Recovery
        discovery must use :meth:`turn_recovery_started` instead; writing this
        marker before the resumed work runs would hide a second crash.
        """
        self._write({"seq": self._seq, "kind": "turn_recovered", "turn_id": turn_id})

    def turn_recovery_started(self, turn_id: str) -> None:
        """Record that recovery was selected without closing the turn.

        The marker is deliberately non-terminal. If the process dies after
        this write but before the workflow is rehydrated, the original
        ``turn_started`` remains discoverable and its durable message/step
        projection can be resumed again.
        """
        self._write(
            {
                "seq": self._seq,
                "kind": "turn_recovery_started",
                "schema_version": 1,
                "turn_id": str(turn_id)[:128],
            }
        )

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
        event_id: str = "",
    ) -> None:
        """Persist an interrupted exchange and its idempotent recovery ID."""
        self._write(
            {
                "seq": self._seq,
                "kind": "tool_exchange_aborted",
                "schema_version": 2,
                "exchange_id": exchange_id,
                "call_count": call_count,
                "repaired": repaired,
                "event_id": str(event_id)[:128],
            }
        )

    def fold_pending_tool_exchange_call_hashes(self) -> frozenset[str]:
        """Return hashed call IDs from exchanges started but not terminalized.

        An unfinished lifecycle transaction is the durable evidence that a
        missing provider result can be repaired as an interruption. IDs remain
        hashed in the returned set; callers hash message IDs before matching.
        Legacy journals without lifecycle events therefore fail closed for
        missing-result synthesis while remaining eligible for exact result
        reordering when every result is present.
        """
        if not self._path.exists():
            return frozenset()
        pending: dict[str, set[str]] = {}
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
                kind = entry.get("kind")
                exchange_id = entry.get("exchange_id")
                if not isinstance(exchange_id, str) or not exchange_id:
                    continue
                if kind == "tool_exchange_started":
                    call_ids = entry.get("call_ids")
                    if isinstance(call_ids, list):
                        pending[exchange_id] = {
                            value for value in call_ids if isinstance(value, str) and value
                        }
                elif kind in {"tool_exchange_committed", "tool_exchange_aborted"}:
                    pending.pop(exchange_id, None)
        return frozenset(value for call_hashes in pending.values() for value in call_hashes)

    def fold_tool_exchange_recoveries(self) -> list[dict[str, object]]:
        """Return valid durable tool-recovery receipts in journal order.

        Older journals do not have ``event_id``.  They remain readable; only
        records with the new stable identity participate in replay
        deduplication.  A malformed trailing record is treated as an
        interrupted append, matching the normal journal folding contract.
        """
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
                if (
                    entry.get("kind") == "tool_exchange_aborted"
                    and entry.get("repaired") is True
                    and isinstance(entry.get("event_id"), str)
                    and entry["event_id"]
                ):
                    records.append({str(key): value for key, value in entry.items()})
        return records

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
        status: Literal["completed", "failed", "cancelled", "interrupted"] | None = None,
    ) -> None:
        """Persist one worker result, including its complete final text.

        Worker memory is deliberately isolated and short-lived. The worker
        result is therefore the durable representation of work produced before
        the parent tool exchange commits.
        """
        entry: dict[str, object] = {
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
        if status is not None:
            entry["status"] = status
        self._write(entry)

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

    def subagent_pool_started(
        self,
        *,
        pool_id: str,
        fingerprint: str,
        parent_run_id: str,
        tasks: list[dict[str, str]],
    ) -> None:
        """Persist a pool's identity before any worker can produce side effects."""
        self._write(
            {
                "seq": self._seq,
                "kind": "subagent_pool_started",
                "schema_version": 1,
                "pool_id": str(pool_id)[:128],
                "fingerprint": str(fingerprint)[:128],
                "parent_run_id": str(parent_run_id)[:128],
                "tasks": [dict(task) for task in tasks],
            }
        )

    def subagent_pool_backgrounded(
        self,
        *,
        pool_id: str,
        tool_result: Mapping[str, object],
    ) -> None:
        """Persist the exact provider receipt before returning control to the composer."""
        self._write(
            {
                "seq": self._seq,
                "kind": "subagent_pool_backgrounded",
                "schema_version": 1,
                "pool_id": str(pool_id)[:128],
                "tool_result": dict(tool_result),
            }
        )

    def subagent_pool_finished(
        self,
        *,
        pool_id: str,
        total: int,
        succeeded: int,
        failed: int,
        text: str,
    ) -> None:
        """Persist a terminal pool aggregate, including partial failures."""
        self._write(
            {
                "seq": self._seq,
                "kind": "subagent_pool_finished",
                "schema_version": 1,
                "pool_id": str(pool_id)[:128],
                "total": max(0, int(total)),
                "succeeded": max(0, int(succeeded)),
                "failed": max(0, int(failed)),
                "text": str(text),
            }
        )

    def subagent_pool_cancelled(self, *, pool_id: str, reason: str = "session closed") -> None:
        """Persist a graceful session cancellation and terminalize unfinished workers."""
        lifecycle = next(
            (
                record
                for record in self.fold_subagent_pool_lifecycle()
                if record.get("pool_id") == pool_id
            ),
            None,
        )
        if lifecycle is None or lifecycle.get("status") not in {"running", "backgrounded"}:
            return
        total, succeeded, text = self._record_unfinished_subagent_workers(
            lifecycle, f"cancelled: {reason}"
        )
        self._write(
            {
                "seq": self._seq,
                "kind": "subagent_pool_cancelled",
                "schema_version": 1,
                "pool_id": str(pool_id)[:128],
                "total": total,
                "succeeded": succeeded,
                "failed": max(0, total - succeeded),
                "text": text,
                "reason": str(reason)[:256],
            }
        )

    def subagent_pool_notification_delivered(
        self,
        *,
        pool_id: str,
        message_id: str,
    ) -> None:
        """Record that a background result reached provider conversation memory."""
        self._write(
            {
                "seq": self._seq,
                "kind": "subagent_pool_notification_delivered",
                "schema_version": 1,
                "pool_id": str(pool_id)[:128],
                "message_id": str(message_id)[:160],
            }
        )

    def delivered_subagent_pool_notifications(self) -> frozenset[str]:
        """Return pool IDs whose notification append has been acknowledged."""
        return frozenset(
            str(record["pool_id"])
            for record in self._fold_subagent_kind("subagent_pool_notification_delivered")
            if isinstance(record.get("pool_id"), str) and record.get("pool_id")
        )

    def _record_unfinished_subagent_workers(
        self,
        lifecycle: Mapping[str, object],
        reason: str,
    ) -> tuple[int, int, str]:
        """Record missing task outcomes and return aggregate counts and text."""
        pool_id = str(lifecycle.get("pool_id", ""))
        tasks_value = lifecycle.get("tasks", [])
        tasks = tasks_value if isinstance(tasks_value, list) else []
        latest_workers: dict[str, dict[str, object]] = {}
        for record in self.fold_subagent_worker_results():
            if record.get("pool_id") == pool_id and isinstance(record.get("task_id"), str):
                latest_workers[str(record["task_id"])] = record

        type_indices: dict[str, int] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id")
            agent_type = task.get("agent_type")
            if not isinstance(task_id, str) or not task_id or not isinstance(agent_type, str):
                continue
            type_indices[agent_type] = type_indices.get(agent_type, 0) + 1
            if task_id in latest_workers:
                continue
            label = f"{agent_type} #{type_indices[agent_type]}"
            worker_record: dict[str, object] = {
                "seq": self._seq,
                "kind": "subagent_worker_result",
                "schema_version": 1,
                "pool_id": pool_id,
                "fingerprint": str(lifecycle.get("fingerprint", "")),
                "task_id": task_id,
                "agent_type": agent_type,
                "label": label,
                "status": "cancelled" if reason.startswith("cancelled:") else "interrupted",
                "ok": False,
                "text": "",
                "error": reason,
                "duration_ms": 0.0,
                "tool_calls": [],
                "changed_paths": [],
            }
            self._write(worker_record)
            latest_workers[task_id] = worker_record

        succeeded = sum(1 for record in latest_workers.values() if record.get("ok") is True)
        sections: list[str] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id")
            worker = latest_workers.get(task_id) if isinstance(task_id, str) else None
            if worker is None:
                continue
            label = str(worker.get("label", task_id))
            status = "✓" if worker.get("ok") is True else f"✗ {worker.get('error', reason)}"
            text = str(worker.get("text", ""))
            sections.append(f"=== {label} ({status}) ===\n{text}".rstrip())
        return len(tasks), succeeded, "\n\n".join(sections)

    def fold_subagent_pool_lifecycle(self) -> list[dict[str, object]]:
        """Fold each pool's latest durable lifecycle state in journal order."""
        kinds = {
            "subagent_pool_started",
            "subagent_pool_backgrounded",
            "subagent_pool_finished",
            "subagent_pool_interrupted",
            "subagent_pool_cancelled",
        }
        records = [record for kind in kinds for record in self._fold_subagent_kind(kind)]

        def sequence(record: Mapping[str, object]) -> int:
            value = record.get("seq")
            return value if isinstance(value, int) and not isinstance(value, bool) else -1

        records.sort(key=sequence)
        latest: dict[str, dict[str, object]] = {}
        terminal_kinds = {
            "subagent_pool_finished": "completed",
            "subagent_pool_interrupted": "interrupted",
            "subagent_pool_cancelled": "cancelled",
        }
        for record in records:
            kind = record.get("kind")
            pool_id = record.get("pool_id")
            if not isinstance(pool_id, str) or not pool_id:
                continue
            if kind == "subagent_pool_started":
                latest[pool_id] = {**record, "status": "running", "backgrounded": False}
                continue
            value = latest.get(pool_id)
            if value is None:
                continue
            value.update(record)
            if kind == "subagent_pool_backgrounded":
                value["status"] = "backgrounded"
                value["backgrounded"] = True
            elif kind in terminal_kinds:
                value["status"] = terminal_kinds[kind]
        return list(latest.values())

    def find_subagent_pool(self, parent_run_id: str, fingerprint: str) -> dict[str, object] | None:
        """Find the latest pool created by one logical parent turn and task set."""
        for lifecycle in reversed(self.fold_subagent_pool_lifecycle()):
            if (
                lifecycle.get("parent_run_id") == parent_run_id
                and lifecycle.get("fingerprint") == fingerprint
            ):
                return lifecycle
        return None

    def recover_interrupted_subagent_pools(self) -> list[str]:
        """Mark pools left active by a crashed process without rerunning workers."""
        recovered: list[str] = []
        for lifecycle in self.fold_subagent_pool_lifecycle():
            if lifecycle.get("status") not in {"running", "backgrounded"}:
                continue
            pool_id = lifecycle.get("pool_id")
            if not isinstance(pool_id, str) or not pool_id:
                continue
            total, succeeded, text = self._record_unfinished_subagent_workers(
                lifecycle, "interrupted by process restart"
            )
            self._write(
                {
                    "seq": self._seq,
                    "kind": "subagent_pool_interrupted",
                    "schema_version": 1,
                    "pool_id": pool_id,
                    "total": total,
                    "succeeded": succeeded,
                    "failed": max(0, total - succeeded),
                    "text": text,
                }
            )
            recovered.append(pool_id)
        return recovered

    def completed_background_subagent_pools(self) -> list[dict[str, object]]:
        """Return backgrounded pools with a completed, interrupted, or cancelled outcome."""
        delivered = self.delivered_subagent_pool_notifications()
        return [
            record
            for record in self.fold_subagent_pool_lifecycle()
            if record.get("pool_id") not in delivered
            and record.get("backgrounded") is True
            and record.get("status") in {"completed", "interrupted", "cancelled"}
        ]

    def agent_message_event(
        self,
        *,
        pool_id: str,
        conversation_id: str,
        parent_run_id: str,
        policy_revision: str,
        event_kind: str,
        payload: Mapping[str, object],
    ) -> None:
        """Persist one bounded parent/worker communication lifecycle event.

        Communication records are auxiliary journal entries.  They never enter
        the provider-message fold, but are durable enough to rehydrate pending
        mail and questions after a process restart.
        """
        safe_payload = dict(payload)
        self._write(
            {
                "seq": self._seq,
                "kind": "agent_message_event",
                "schema_version": 1,
                "pool_id": str(pool_id)[:128],
                "conversation_id": str(conversation_id)[:128],
                "parent_run_id": str(parent_run_id)[:128],
                "policy_revision": str(policy_revision)[:128],
                "event_kind": str(event_kind)[:64],
                "payload": safe_payload,
            }
        )

    def fold_agent_message_events(self) -> list[dict[str, object]]:
        """Return durable subagent communication events in journal order."""
        return self._fold_subagent_kind("agent_message_event")

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
        messages, summary, corrupt_count = fold_path_with_report(self._path)
        self._corrupt_trailing_records = corrupt_count
        return messages, summary

    @property
    def corrupt_trailing_records(self) -> int:
        """Count non-empty records skipped after the first malformed JSON line."""
        return self._corrupt_trailing_records

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
