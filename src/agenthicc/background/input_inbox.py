"""Durable, owner-fenced input queue for live background sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agenthicc.runners.process_lease import InterProcessLock

from .model import ACTIVE_STATUSES, BackgroundSession, SessionStatus
from .store import BackgroundStore, InvalidSessionTransition

InputState = Literal["accepted", "claimed", "delivered", "completed", "failed", "rejected"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_INPUT_CHARS = 64_000
MAX_PENDING_MESSAGES = 64


@dataclass(frozen=True)
class BackgroundInput:
    """One target-addressed input command and its current durable receipt."""

    message_id: str
    session_id: str
    owner_attempt: int
    text: str
    accepted_at: float
    state: InputState = "accepted"
    state_changed_at: float = 0.0
    error: str = ""
    deferred: bool = False
    recovery_error: str = ""


class BackgroundInputInbox:
    """Append-only per-session inbox with FIFO, dedupe, and owner fencing.

    Message bodies are stored separately from the lifecycle registry so they
    do not appear in generic manager logs or list projections. Each write is
    fsync'd while holding an inter-process lock; the current state is rebuilt
    from the event log and therefore survives manager/worker restarts.
    """

    def __init__(self, store: BackgroundStore) -> None:
        self.store = store
        self.root = store.root / "input-inbox"

    def _paths(self, session_id: str) -> tuple[Path, Path]:
        if not isinstance(session_id, str) or not _SAFE_ID.fullmatch(session_id):
            raise ValueError("session_id must be a safe identifier")
        return (
            self.root / f"{session_id}.jsonl",
            self.root / f"{session_id}.lock",
        )

    def fingerprint(self, session_id: str) -> tuple[int, int, int]:
        """Return a cheap file identity for receipt-cache invalidation."""
        path, _lock_path = self._paths(session_id)
        try:
            stat = path.stat()
        except OSError:
            return (0, 0, 0)
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    @staticmethod
    def _owner_hash(lease_token: str) -> str:
        return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

    def _events(self, path: Path) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw, dict):
                        events.append(raw)
        except FileNotFoundError:
            pass
        return events

    @staticmethod
    def _projection(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
        records: dict[str, dict[str, object]] = {}
        for event in events:
            action = event.get("action")
            message_id = event.get("message_id")
            if not isinstance(message_id, str):
                continue
            if action == "accepted":
                records[message_id] = dict(event)
            elif (
                action
                in {
                    "claimed",
                    "delivered",
                    "completed",
                    "failed",
                    "rejected",
                    "rebound",
                }
                and message_id in records
            ):
                records[message_id].update(event)
        return records

    def _append(self, path: Path, event: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _as_input(record: dict[str, object]) -> BackgroundInput:
        raw_state = record.get("state", "accepted")
        states: dict[str, InputState] = {
            "accepted": "accepted",
            "claimed": "claimed",
            "delivered": "delivered",
            "completed": "completed",
            "failed": "failed",
            "rejected": "rejected",
        }
        state = states.get(raw_state, "rejected") if isinstance(raw_state, str) else "rejected"
        accepted_at = record.get("accepted_at", 0.0)
        changed_at = record.get("state_changed_at", accepted_at)
        attempt = record.get("owner_attempt", 0)
        return BackgroundInput(
            message_id=str(record.get("message_id", "")),
            session_id=str(record.get("session_id", "")),
            owner_attempt=attempt
            if isinstance(attempt, int) and not isinstance(attempt, bool)
            else 0,
            text=str(record.get("text", "")),
            accepted_at=accepted_at if isinstance(accepted_at, (int, float)) else 0.0,
            state=state,
            state_changed_at=changed_at if isinstance(changed_at, (int, float)) else 0.0,
            error=str(record.get("error", ""))[:240],
            deferred=record.get("deferred") is True,
        )

    def enqueue(
        self,
        session_id: str,
        text: str,
        *,
        owner_attempt: int,
        lease_token: str,
        message_id: str | None = None,
    ) -> BackgroundInput:
        """Durably accept input only for the currently live session owner."""
        cleaned = text.strip() if isinstance(text, str) else ""
        if not cleaned:
            raise ValueError("Input must not be empty")
        if len(cleaned) > MAX_INPUT_CHARS:
            raise ValueError(f"Input exceeds {MAX_INPUT_CHARS} characters")
        if (
            isinstance(owner_attempt, bool)
            or not isinstance(owner_attempt, int)
            or owner_attempt < 1
        ):
            raise ValueError("owner_attempt must be a positive integer")
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("a live owner lease is required")
        identifier = message_id or uuid.uuid4().hex
        if not _SAFE_ID.fullmatch(identifier):
            raise ValueError("message_id must be a safe identifier")
        path, lock_path = self._paths(session_id)
        current = self.store.get(session_id, include_deleted=True)
        self._validate_current_owner(current, owner_attempt, lease_token)
        with InterProcessLock(lock_path):
            records = self._projection(self._events(path))
            previous = records.get(identifier)
            if previous is not None:
                if (
                    previous.get("session_id") != session_id
                    or previous.get("owner_attempt") != owner_attempt
                    or previous.get("text") != cleaned
                ):
                    raise ValueError("message_id was already used for different input")
                return self._as_input(previous)
            pending = sum(
                record.get("state") in {"accepted", "claimed"} for record in records.values()
            )
            if pending >= MAX_PENDING_MESSAGES:
                raise InvalidSessionTransition("Background input queue is full")
            # Re-check immediately before persistence. The worker still fences
            # consumption by attempt and lease hash if ownership changes after.
            current = self.store.get(session_id, include_deleted=True)
            self._validate_current_owner(current, owner_attempt, lease_token)
            now = time.time()
            event: dict[str, object] = {
                "action": "accepted",
                "message_id": identifier,
                "session_id": session_id,
                "owner_attempt": owner_attempt,
                "owner_hash": self._owner_hash(lease_token),
                "text": cleaned,
                "accepted_at": now,
                "state": "accepted",
                "state_changed_at": now,
            }
            self._append(path, event)
            return self._as_input(event)

    def enqueue_deferred(
        self,
        session_id: str,
        text: str,
        *,
        expected_attempt: int,
        expected_lease_token: str | None,
        message_id: str | None = None,
    ) -> BackgroundInput:
        """Durably queue input for a recoverable session without a live owner.

        The next worker attempt rebinds this unowned message to its lease
        before delivery. This lets a user submit a continuation while viewing
        an orphaned/recoverable session without falsely claiming it is live or
        racing a stale process.
        """
        cleaned = text.strip() if isinstance(text, str) else ""
        if not cleaned:
            raise ValueError("Input must not be empty")
        if len(cleaned) > MAX_INPUT_CHARS:
            raise ValueError(f"Input exceeds {MAX_INPUT_CHARS} characters")
        if (
            isinstance(expected_attempt, bool)
            or not isinstance(expected_attempt, int)
            or expected_attempt < 0
        ):
            raise ValueError("expected_attempt must be a non-negative integer")
        identifier = message_id or uuid.uuid4().hex
        if not _SAFE_ID.fullmatch(identifier):
            raise ValueError("message_id must be a safe identifier")
        path, lock_path = self._paths(session_id)
        with InterProcessLock(lock_path):
            current = self.store.get(session_id, include_deleted=True)
            if current.status is SessionStatus.DELETED:
                raise InvalidSessionTransition("Deleted sessions cannot receive input")
            if current.attempt != expected_attempt or current.lease_token != expected_lease_token:
                raise InvalidSessionTransition("Session changed; reopen details and retry")
            records = self._projection(self._events(path))
            previous = records.get(identifier)
            if previous is not None:
                if previous.get("session_id") != session_id or previous.get("text") != cleaned:
                    raise ValueError("message_id was already used for different input")
                return self._as_input(previous)
            pending = sum(
                record.get("state") in {"accepted", "claimed"} for record in records.values()
            )
            if pending >= MAX_PENDING_MESSAGES:
                raise InvalidSessionTransition("Background input queue is full")
            current = self.store.get(session_id, include_deleted=True)
            if (
                current.status is SessionStatus.DELETED
                or current.attempt != expected_attempt
                or current.lease_token != expected_lease_token
            ):
                raise InvalidSessionTransition("Session changed; reopen details and retry")
            now = time.time()
            event: dict[str, object] = {
                "action": "accepted",
                "message_id": identifier,
                "session_id": session_id,
                "owner_attempt": expected_attempt,
                "owner_hash": "",
                "text": cleaned,
                "accepted_at": now,
                "state": "accepted",
                "state_changed_at": now,
                "deferred": True,
            }
            self._append(path, event)
            return self._as_input(event)

    @staticmethod
    def _validate_current_owner(
        session: BackgroundSession,
        owner_attempt: int,
        lease_token: str,
    ) -> None:
        if session.status not in ACTIVE_STATUSES:
            raise InvalidSessionTransition(
                f"Session is {session.status.value}; live input is unavailable"
            )
        if (
            session.attempt != owner_attempt
            or session.lease_token != lease_token
            or session.worker_pid is None
        ):
            raise InvalidSessionTransition("Background session owner changed; reopen details")

    def claim_next(
        self,
        session_id: str,
        *,
        owner_attempt: int,
        lease_token: str,
    ) -> BackgroundInput | None:
        """Claim the oldest accepted command for the current worker attempt."""
        path, lock_path = self._paths(session_id)
        with InterProcessLock(lock_path):
            current = self.store.get(session_id, include_deleted=True)
            self._validate_current_owner(current, owner_attempt, lease_token)
            records = self._projection(self._events(path))
            owner_hash = self._owner_hash(lease_token)
            ordered = sorted(records.values(), key=self._accepted_at)
            for record in ordered:
                if record.get("state") != "accepted":
                    continue
                if record.get("deferred") is True:
                    rebound = {
                        "action": "rebound",
                        "message_id": record.get("message_id"),
                        "owner_attempt": owner_attempt,
                        "owner_hash": owner_hash,
                        "deferred": False,
                        "state": "accepted",
                        "state_changed_at": time.time(),
                    }
                    self._append(path, rebound)
                    record.update(rebound)
                if (
                    record.get("owner_attempt") != owner_attempt
                    or record.get("owner_hash") != owner_hash
                ):
                    # Never allow an old worker attempt to consume input
                    # accepted for a different owner. It remains in the log
                    # for operator diagnosis rather than being replayed.
                    continue
                claimed = {
                    # The worker claims only at the safe tool boundary where
                    # the AgentTurnRunner can immediately append the user
                    # message before its next model request. This is the
                    # owner-side delivery acknowledgement exposed to the UI.
                    "action": "delivered",
                    "message_id": record.get("message_id"),
                    "state": "delivered",
                    "state_changed_at": time.time(),
                }
                self._append(path, claimed)
                record.update(claimed)
                return self._as_input(record)
        return None

    def peek_next(
        self,
        session_id: str,
        *,
        owner_attempt: int,
        lease_token: str,
    ) -> BackgroundInput | None:
        """Inspect the FIFO head without claiming it, under the current lease."""
        path, lock_path = self._paths(session_id)
        with InterProcessLock(lock_path):
            current = self.store.get(session_id, include_deleted=True)
            self._validate_current_owner(current, owner_attempt, lease_token)
            records = self._projection(self._events(path))
            owner_hash = self._owner_hash(lease_token)
            for record in sorted(records.values(), key=self._accepted_at):
                if record.get("state") != "accepted":
                    continue
                if record.get("deferred") is True:
                    return self._as_input(record)
                if (
                    record.get("owner_attempt") == owner_attempt
                    and record.get("owner_hash") == owner_hash
                ):
                    return self._as_input(record)
        return None

    def reject(
        self,
        session_id: str,
        message_id: str,
        *,
        owner_attempt: int,
        lease_token: str,
        reason: str,
    ) -> BackgroundInput:
        """Reject one queued item explicitly without exposing its body in logs."""
        path, lock_path = self._paths(session_id)
        with InterProcessLock(lock_path):
            records = self._projection(self._events(path))
            record = records.get(message_id)
            if record is None:
                raise KeyError(f"Unknown background input {message_id}")
            if (
                record.get("state") not in {"accepted", "claimed", "delivered"}
                or record.get("owner_attempt") != owner_attempt
                or record.get("owner_hash") != self._owner_hash(lease_token)
            ):
                raise InvalidSessionTransition("Input is not pending for this worker attempt")
            now = time.time()
            event: dict[str, object] = {
                "action": "rejected",
                "message_id": message_id,
                "state": "rejected",
                "state_changed_at": now,
                "error": reason[:240],
            }
            self._append(path, event)
            record.update(event)
            return self._as_input(record)

    def acknowledge(
        self,
        session_id: str,
        message_id: str,
        *,
        owner_attempt: int,
        lease_token: str,
    ) -> BackgroundInput:
        """Mark one claimed item delivered by its current owning worker."""
        path, lock_path = self._paths(session_id)
        with InterProcessLock(lock_path):
            records = self._projection(self._events(path))
            record = records.get(message_id)
            if record is None:
                raise KeyError(f"Unknown background input {message_id}")
            if record.get("state") == "delivered":
                return self._as_input(record)
            if (
                record.get("state") != "claimed"
                or record.get("owner_attempt") != owner_attempt
                or record.get("owner_hash") != self._owner_hash(lease_token)
            ):
                raise InvalidSessionTransition("Input is not claimed by this worker attempt")
            current = self.store.get(session_id, include_deleted=True)
            self._validate_current_owner(current, owner_attempt, lease_token)
            now = time.time()
            event: dict[str, object] = {
                "action": "delivered",
                "message_id": message_id,
                "state": "delivered",
                "state_changed_at": now,
            }
            self._append(path, event)
            record.update(event)
            return self._as_input(record)

    def settle_attempt(
        self,
        session_id: str,
        *,
        owner_attempt: int,
        lease_token: str,
        delivered_ids: set[str],
        completed_ids: set[str],
        succeeded: bool,
    ) -> tuple[BackgroundInput, ...]:
        """Close receipts at worker exit; never leave accepted input implicit.

        Messages consumed by the current turn become completed (or failed if
        the turn failed). Messages accepted too late to reach an agent safe
        boundary are explicitly rejected with a resend instruction.
        """
        path, lock_path = self._paths(session_id)
        owner_hash = self._owner_hash(lease_token)
        reason = (
            "Worker finished before this input reached a safe turn boundary; "
            "reopen details and resend."
        )
        with InterProcessLock(lock_path):
            records = self._projection(self._events(path))
            now = time.time()
            changed = False
            for message_id, record in records.items():
                if (
                    record.get("owner_attempt") != owner_attempt
                    or record.get("owner_hash") != owner_hash
                ):
                    continue
                state = record.get("state")
                next_state: InputState | None = None
                error = ""
                if message_id in delivered_ids and state == "delivered":
                    next_state = (
                        "completed" if succeeded or message_id in completed_ids else "failed"
                    )
                    if next_state == "failed":
                        error = "The owning worker failed after accepting this input."
                elif state in {"accepted", "claimed"}:
                    next_state = "rejected"
                    error = reason
                if next_state is None:
                    continue
                if record.get("deferred") is True and state in {"accepted", "claimed"}:
                    continue
                event: dict[str, object] = {
                    "action": next_state,
                    "message_id": message_id,
                    "state": next_state,
                    "state_changed_at": now,
                }
                if error:
                    event["error"] = error
                self._append(path, event)
                record.update(event)
                changed = True
            if changed:
                # Return the same projection while the file lock is held so
                # callers can publish receipts without a second read race.
                return tuple(
                    self._as_input(item) for item in sorted(records.values(), key=self._accepted_at)
                )
        return self.receipts(session_id)

    def receipts(self, session_id: str) -> tuple[BackgroundInput, ...]:
        """Return durable receipts in acceptance order, without exposing lease tokens.

        If the owner attempt has ended, accepted/delivered records cannot be
        consumed safely. Reconcile them as rejected instead of displaying a
        permanent, misleading queue or replaying them into a later attempt.
        """
        path, lock_path = self._paths(session_id)
        try:
            current = self.store.get(session_id, include_deleted=True)
        except KeyError:
            current = None
        with InterProcessLock(lock_path):
            records = self._projection(self._events(path))
            if current is not None:
                now = time.time()
                for message_id, record in records.items():
                    if record.get("state") not in {"accepted", "claimed", "delivered"}:
                        continue
                    if record.get("deferred") is True:
                        if current.status is SessionStatus.DELETED:
                            deleted_event = {
                                "action": "rejected",
                                "message_id": message_id,
                                "state": "rejected",
                                "state_changed_at": now,
                                "error": "The session was deleted before this input could be delivered.",
                            }
                            self._append(path, deleted_event)
                            record.update(deleted_event)
                        continue
                    owner_changed = record.get("owner_attempt") != current.attempt or record.get(
                        "owner_hash"
                    ) != self._owner_hash(current.lease_token)
                    if current.status in ACTIVE_STATUSES and not owner_changed:
                        continue
                    event: dict[str, object] = {
                        "action": "rejected",
                        "message_id": message_id,
                        "state": "rejected",
                        "state_changed_at": now,
                        "error": (
                            "The owning worker ended or changed before delivery was confirmed; "
                            "reopen details and resend."
                        ),
                    }
                    self._append(path, event)
                    record.update(event)
        ordered = sorted(records.values(), key=self._accepted_at)
        return tuple(self._as_input(item) for item in ordered)

    @staticmethod
    def _accepted_at(record: dict[str, object]) -> float:
        value = record.get("accepted_at", 0.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return 0.0
