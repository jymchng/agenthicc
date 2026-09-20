"""Session-scoped recurring prompt scheduling (PRD-193).

The scheduler deliberately has no model, workflow, or tool dependencies.  It
only owns a durable schedule and asks the session owner to enqueue one normal
user turn when the session is idle.  This keeps recurring work on the same
conversation, approval, checkpoint, retry, and security path as a manual
message.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from agenthicc.runners.process_lease import InterProcessLock, InterProcessLockError, atomic_replace

__all__ = [
    "LoopLifecycle",
    "LoopPayloadKind",
    "LoopRecord",
    "LoopSettingsProtocol",
    "LoopStore",
    "LoopStorageError",
    "LoopManager",
    "parse_loop_duration",
    "format_loop_duration",
]


class LoopStorageError(RuntimeError):
    """Raised when a loop record cannot be read or written safely."""


class LoopLifecycle(StrEnum):
    """Durable lifecycle states for one session loop."""

    SCHEDULED = "scheduled"
    WAITING_FOR_IDLE = "waiting_for_idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    EXPIRED = "expired"
    FAILED = "failed"


class LoopPayloadKind(StrEnum):
    """The two payloads accepted by the v1 command surface."""

    PROMPT = "prompt"
    COMMAND = "command"


class LoopSettingsProtocol(Protocol):
    """Small structural settings object used by :class:`LoopManager`.

    ``config.LoopSettings`` is the production instance.  Keeping this protocol
    local lets the durable scheduler remain easy to use from isolated tests and
    from client-neutral session adapters.
    """

    enabled: bool
    default_interval_s: int
    min_interval_s: int
    max_interval_s: int
    max_age_s: int
    max_prompt_bytes: int
    max_consecutive_failures: int
    busy_poll_s: float
    persist: bool
    allow_slash_commands: bool


_DURATION_RE: Final[re.Pattern[str]] = re.compile(r"(?i)^(?P<value>[0-9]+)(?P<unit>[smhd])$")
_DURATION_MULTIPLIERS: Final[dict[str, int]] = {
    "s": 1,
    "m": 60,
    "h": 3_600,
    "d": 86_400,
}
_MAX_DURATION_SECONDS: Final[int] = 2**31 - 1


def parse_loop_duration(value: str) -> int:
    """Parse a strict ``integer + s/m/h/d`` duration into seconds."""

    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("interval must look like 30s, 5m, 1h, or 2d")
    seconds = int(match.group("value")) * _DURATION_MULTIPLIERS[match.group("unit").lower()]
    if seconds <= 0 or seconds > _MAX_DURATION_SECONDS:
        raise ValueError("interval is outside the supported duration range")
    return seconds


def format_loop_duration(seconds: float) -> str:
    """Return a compact human-readable duration for status messages."""

    value = max(0, int(seconds))
    if value % 86_400 == 0 and value:
        return f"{value // 86_400}d"
    if value % 3_600 == 0 and value:
        return f"{value // 3_600}h"
    if value % 60 == 0 and value:
        return f"{value // 60}m"
    return f"{value}s"


@dataclass(frozen=True, slots=True)
class LoopRecord:
    """Versioned, serializable state for one session-scoped loop."""

    loop_id: str
    session_id: str
    conversation_id: str
    payload_kind: LoopPayloadKind
    payload: str
    interval_s: int
    created_at: float
    updated_at: float
    next_due_at: float
    expires_at: float
    state: LoopLifecycle = LoopLifecycle.SCHEDULED
    revision: int = 0
    runs: int = 0
    coalesced_runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_dispatch_at: float | None = None
    last_completed_at: float | None = None
    last_error: str = ""
    pending: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.loop_id or not self.session_id or not self.conversation_id:
            raise ValueError("loop identity fields must be non-empty")
        if not self.payload.strip():
            raise ValueError("loop payload must be non-empty")
        if self.interval_s <= 0:
            raise ValueError("loop interval must be positive")
        if self.expires_at < self.created_at:
            raise ValueError("loop expiry cannot precede creation")
        if self.revision < 0 or self.runs < 0 or self.coalesced_runs < 0:
            raise ValueError("loop counters cannot be negative")
        if self.failures < 0 or self.consecutive_failures < 0:
            raise ValueError("loop failure counters cannot be negative")
        if self.schema_version != 1:
            raise ValueError(f"unsupported loop schema version {self.schema_version}")

    def to_mapping(self) -> dict[str, object]:
        """Serialize the record without lossy stringification."""

        return {
            "schema_version": self.schema_version,
            "loop_id": self.loop_id,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "payload_kind": self.payload_kind.value,
            "payload": self.payload,
            "interval_s": self.interval_s,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_due_at": self.next_due_at,
            "expires_at": self.expires_at,
            "state": self.state.value,
            "revision": self.revision,
            "runs": self.runs,
            "coalesced_runs": self.coalesced_runs,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_dispatch_at": self.last_dispatch_at,
            "last_completed_at": self.last_completed_at,
            "last_error": self.last_error,
            "pending": self.pending,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "LoopRecord":
        """Decode and validate a durable mapping."""

        def text(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str):
                raise ValueError(f"loop field {name!r} must be a string")
            return value

        def integer(name: str) -> int:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"loop field {name!r} must be an integer")
            return int(value)

        def number(name: str) -> float:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"loop field {name!r} must be a number")
            return float(value)

        def optional_number(name: str) -> float | None:
            value = raw.get(name)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"loop field {name!r} must be a number or null")
            return float(value)

        try:
            payload_kind = LoopPayloadKind(text("payload_kind"))
            state = LoopLifecycle(text("state"))
            pending_value = raw.get("pending", False)
            if not isinstance(pending_value, bool):
                raise ValueError("loop field 'pending' must be a boolean")
            return cls(
                loop_id=text("loop_id"),
                session_id=text("session_id"),
                conversation_id=text("conversation_id"),
                payload_kind=payload_kind,
                payload=text("payload"),
                interval_s=integer("interval_s"),
                created_at=number("created_at"),
                updated_at=number("updated_at"),
                next_due_at=number("next_due_at"),
                expires_at=number("expires_at"),
                state=state,
                revision=integer("revision"),
                runs=integer("runs"),
                coalesced_runs=integer("coalesced_runs"),
                failures=integer("failures"),
                consecutive_failures=integer("consecutive_failures"),
                last_dispatch_at=optional_number("last_dispatch_at"),
                last_completed_at=optional_number("last_completed_at"),
                last_error=text("last_error"),
                pending=pending_value,
                schema_version=integer("schema_version"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid loop record: {exc}") from exc


class LoopStore:
    """Atomic owner-only persistence for one session's loop record."""

    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root.expanduser()
        self.session_id = session_id
        self.session_dir = self.root / session_id
        self.path = self.session_dir / "loop.json"
        self.lock_path = self.session_dir / "loop.lock"

    def load(self) -> LoopRecord | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LoopStorageError(f"cannot read loop record {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise LoopStorageError(f"loop record {self.path} is not an object")
        try:
            record = LoopRecord.from_mapping(raw)
        except ValueError as exc:
            raise LoopStorageError(f"invalid loop record {self.path}: {exc}") from exc
        if record.session_id != self.session_id or record.conversation_id != self.session_id:
            raise LoopStorageError("loop record identity does not match its session")
        return record

    @classmethod
    def list_all(cls, root: Path) -> list[LoopRecord]:
        """Return every valid session-scoped loop below *root*.

        Loop records are intentionally kept inside their owning session
        directory, so the list is a projection rather than a second registry.
        A missing, corrupt, or unreadable record is skipped; its owning session
        remains inspectable through the normal session diagnostics and cannot
        make ``/loops`` fail closed for otherwise healthy jobs.
        """

        root = root.expanduser()
        if not root.is_dir():
            return []
        records: list[LoopRecord] = []
        try:
            children = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            return []
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            try:
                record = cls(root, child.name).load()
            except (LoopStorageError, ValueError):
                continue
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: (-item.updated_at, item.session_id, item.loop_id))
        return records

    def save(self, record: LoopRecord) -> None:
        if record.session_id != self.session_id or record.conversation_id != self.session_id:
            raise LoopStorageError("cannot save a loop for a different session")
        if record.schema_version != 1:
            raise LoopStorageError("cannot save an unsupported loop schema")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.session_dir.chmod(0o700)
        except OSError:
            pass
        encoded = (json.dumps(record.to_mapping(), sort_keys=True, indent=2) + "\n").encode()
        try:
            with InterProcessLock(self.lock_path):
                atomic_replace(self.path, encoded, prefix=".loop-")
        except (OSError, InterProcessLockError) as exc:
            raise LoopStorageError(f"cannot persist loop record {self.path}: {exc}") from exc

    def delete(self, *, expected_loop_id: str | None = None) -> LoopRecord | None:
        """Delete this session's record, optionally using an ID precondition."""

        try:
            with InterProcessLock(self.lock_path):
                current = self.load()
                if current is None:
                    return None
                if expected_loop_id is not None and current.loop_id != expected_loop_id:
                    raise LoopStorageError("loop changed before it could be deleted")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    return None
                return current
        except (OSError, InterProcessLockError) as exc:
            raise LoopStorageError(f"cannot delete loop record {self.path}: {exc}") from exc


class LoopManager:
    """Controller and scheduler for one live session.

    ``dispatch`` is supplied by ``TUISession`` and must enqueue work through
    its ordinary message path.  The scheduler never receives a provider or a
    workflow runner.
    """

    def __init__(
        self,
        *,
        session_id: str,
        settings: LoopSettingsProtocol,
        store: LoopStore,
        is_idle: Callable[[], bool],
        dispatch: Callable[[LoopRecord], Awaitable[None]],
        event_sink: Callable[[str, dict[str, object]], None],
        payload_validator: Callable[[str, LoopPayloadKind], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session_id = session_id
        self.settings = settings
        self.store = store
        self.is_idle = is_idle
        self.dispatch = dispatch
        self.event_sink = event_sink
        self.payload_validator = payload_validator
        self.clock = clock
        self.sleep = sleep
        self.record: LoopRecord | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def active(self) -> bool:
        """Whether a loop can still schedule work."""

        return self.record is not None and self.record.state in {
            LoopLifecycle.SCHEDULED,
            LoopLifecycle.WAITING_FOR_IDLE,
            LoopLifecycle.RUNNING,
            LoopLifecycle.PAUSED,
        }

    async def start(self, *, rehydrate: bool = False) -> None:
        """Start the scheduler and optionally rehydrate an explicit resume."""

        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        if not self.settings.enabled:
            return
        if rehydrate and self.settings.persist:
            try:
                loaded = self.store.load()
            except LoopStorageError as exc:
                self.event_sink("loop_recovery_failed", {"error": str(exc)})
                loaded = None
            if loaded is not None and loaded.state in {
                LoopLifecycle.SCHEDULED,
                LoopLifecycle.WAITING_FOR_IDLE,
                LoopLifecycle.RUNNING,
            }:
                now = self.clock()
                self.record = replace(
                    loaded,
                    state=LoopLifecycle.SCHEDULED,
                    pending=True,
                    next_due_at=min(loaded.next_due_at, now),
                    updated_at=now,
                    revision=loaded.revision + 1,
                )
                self._save_and_emit("loop_rehydrated", {"coalesced": True})
        self._task = asyncio.create_task(self._run(), name=f"loop-scheduler-{self.session_id}")
        self._wake.set()

    async def shutdown(self) -> None:
        """Stop the scheduler task without deleting durable state."""

        self._stopping = True
        self._wake.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    def handle_command(self, args: str) -> str:
        """Apply one `/loop` subcommand and return a user-facing message."""

        stripped = args.strip()
        lowered = stripped.lower()
        if lowered == "status":
            return self.status_text()
        if lowered == "stop":
            return self.stop()
        if lowered == "pause":
            return self.pause()
        if not self.settings.enabled:
            return "Loop scheduling is disabled by configuration."
        if lowered == "resume":
            return self.resume()

        interval_s = self.settings.default_interval_s
        payload = stripped
        if payload:
            first, separator, remainder = payload.partition(" ")
            if _DURATION_RE.fullmatch(first):
                interval_s = parse_loop_duration(first)
                payload = remainder.strip() if separator else ""
            elif first[:1].isdigit():
                return "Cannot schedule loop: interval must look like 30s, 5m, 1h, or 2d"
        if not payload:
            return "Usage: /loop [<interval>] <prompt or registered slash command>"
        try:
            self._validate_interval(interval_s)
            kind = LoopPayloadKind.COMMAND if payload.startswith("/") else LoopPayloadKind.PROMPT
            if kind is LoopPayloadKind.COMMAND and not self.settings.allow_slash_commands:
                return "Scheduled slash commands are disabled by configuration."
            if self.payload_validator is not None:
                error = self.payload_validator(payload, kind)
                if error:
                    return error
            encoded_size = len(payload.encode("utf-8"))
            if encoded_size > self.settings.max_prompt_bytes:
                return (
                    f"Loop payload is too large ({encoded_size} bytes; maximum "
                    f"{self.settings.max_prompt_bytes})."
                )
        except ValueError as exc:
            return f"Cannot schedule loop: {exc}"

        now = self.clock()
        old = self.record
        record = LoopRecord(
            loop_id=uuid.uuid4().hex,
            session_id=self.session_id,
            conversation_id=self.session_id,
            payload_kind=kind,
            payload=payload,
            interval_s=interval_s,
            created_at=now,
            updated_at=now,
            next_due_at=now,
            expires_at=now + self.settings.max_age_s,
        )
        self.record = record
        if self.settings.persist:
            try:
                self.store.save(record)
            except LoopStorageError as exc:
                self.record = old
                return f"Cannot persist loop: {exc}"
        self.event_sink(
            "loop_replaced"
            if old is not None and old.state not in {LoopLifecycle.STOPPED, LoopLifecycle.EXPIRED}
            else "loop_created",
            {"loop_id": record.loop_id, "interval_s": interval_s, "payload_kind": kind.value},
        )
        self._wake.set()
        return (
            f"↻ Loop scheduled every {format_loop_duration(interval_s)}; "
            f"first iteration is due now and expires in {format_loop_duration(self.settings.max_age_s)}."
        )

    def status_text(self) -> str:
        """Return bounded status without exposing the full stored prompt."""

        record = self.record
        if record is None:
            try:
                record = self.store.load() if self.settings.persist else None
            except LoopStorageError as exc:
                return f"Loop status unavailable: {exc}"
        if record is None or record.state in {
            LoopLifecycle.STOPPED,
            LoopLifecycle.EXPIRED,
            LoopLifecycle.FAILED,
        }:
            if record is None:
                return "No active loop."
            detail = f" · error={record.last_error}" if record.last_error else ""
            return (
                f"Loop {record.state.value} · runs={record.runs} · "
                f"failures={record.consecutive_failures}{detail}"
            )
        assert record is not None
        remaining = max(0, record.next_due_at - self.clock())
        preview = _redacted_preview(record.payload)
        return (
            f"Loop {record.state.value} · every {format_loop_duration(record.interval_s)} · "
            f"next in {format_loop_duration(remaining)} · runs={record.runs} · "
            f"failures={record.consecutive_failures} · payload={preview}"
        )

    def list_jobs(self) -> list[LoopRecord]:
        """List all valid persisted loop jobs visible to this installation."""

        return LoopStore.list_all(self.store.root)

    def run_job_now(self, record: LoopRecord) -> str:
        """Make *record* due immediately, without bypassing the scheduler.

        A job owned by this live TUI is woken immediately.  A job belonging to
        another live session is rejected to avoid racing its scheduler; an
        unowned job is marked due and will run on the next explicit attach to
        that session.  This preserves the one-owner session contract.
        """

        if not self.settings.enabled:
            return "Loop scheduling is disabled by configuration."

        if record.session_id != self.session_id:
            try:
                from agenthicc.runners.session_lease import SessionOpenCoordinator

                inspection = SessionOpenCoordinator(self.store.root).inspect(record.session_id)
            except Exception as exc:  # noqa: BLE001
                return f"Cannot run loop {record.loop_id[:12]}: ownership check failed ({exc})."
            if inspection.state == "active":
                return (
                    f"Loop {record.loop_id[:12]} belongs to a live session; "
                    "resume that session to run it."
                )
            store = LoopStore(self.store.root, record.session_id)
            try:
                current = store.load()
                if current is None or current.loop_id != record.loop_id:
                    return "The selected loop no longer exists."
                now = self.clock()
                updated = replace(
                    current,
                    state=LoopLifecycle.SCHEDULED,
                    pending=True,
                    next_due_at=now,
                    updated_at=now,
                    revision=current.revision + 1,
                )
                store.save(updated)
            except LoopStorageError as exc:
                return f"Cannot run loop {record.loop_id[:12]}: {exc}"
            return (
                f"Loop {record.loop_id[:12]} is due now; attach session "
                f"{record.session_id[:12]} to execute it."
            )

        current = self.record
        if current is None or current.loop_id != record.loop_id:
            try:
                current = self.store.load()
            except LoopStorageError as exc:
                return f"Cannot run loop {record.loop_id[:12]}: {exc}"
        if current is None or current.loop_id != record.loop_id:
            return "The selected loop no longer exists."
        now = self.clock()
        self.record = replace(
            current,
            state=LoopLifecycle.SCHEDULED,
            pending=True,
            next_due_at=now,
            updated_at=now,
            revision=current.revision + 1,
        )
        self._save_and_emit("loop_run_requested")
        self._wake.set()
        return f"Loop {record.loop_id[:12]} is due now."

    def delete_job(self, record: LoopRecord) -> str:
        """Delete one scheduled job with an owner-safe precondition."""

        if record.session_id != self.session_id:
            try:
                from agenthicc.runners.session_lease import SessionOpenCoordinator

                inspection = SessionOpenCoordinator(self.store.root).inspect(record.session_id)
            except Exception as exc:  # noqa: BLE001
                return f"Cannot delete loop {record.loop_id[:12]}: ownership check failed ({exc})."
            if inspection.state == "active":
                return (
                    f"Loop {record.loop_id[:12]} belongs to a live session; "
                    "stop it from that session before deleting it."
                )
            store = LoopStore(self.store.root, record.session_id)
        else:
            store = self.store
        try:
            deleted = store.delete(expected_loop_id=record.loop_id)
        except LoopStorageError as exc:
            return f"Cannot delete loop {record.loop_id[:12]}: {exc}"
        if deleted is None:
            return "The selected loop no longer exists."
        if record.session_id == self.session_id and self.record is not None:
            self.record = None
            self._wake.set()
            self.event_sink(
                "loop_deleted", {"loop_id": record.loop_id, "session_id": self.session_id}
            )
        return f"Loop {record.loop_id[:12]} deleted."

    def stop(self) -> str:
        record = self._load_record_if_needed()
        if record is None:
            return "No active loop."
        if record.state is LoopLifecycle.STOPPED:
            return "Loop is already stopped."
        now = self.clock()
        self.record = replace(
            record,
            state=LoopLifecycle.STOPPED,
            pending=False,
            updated_at=now,
            revision=record.revision + 1,
        )
        self._save_and_emit("loop_stopped")
        self._wake.set()
        return "Loop stopped."

    def pause(self) -> str:
        record = self._load_record_if_needed()
        if record is None or record.state in {LoopLifecycle.STOPPED, LoopLifecycle.EXPIRED}:
            return "No active loop."
        if record.state is LoopLifecycle.PAUSED:
            return "Loop is already paused."
        now = self.clock()
        self.record = replace(
            record, state=LoopLifecycle.PAUSED, updated_at=now, revision=record.revision + 1
        )
        self._save_and_emit("loop_paused")
        return "Loop paused."

    def resume(self) -> str:
        record = self._load_record_if_needed()
        if record is None or record.state in {
            LoopLifecycle.STOPPED,
            LoopLifecycle.EXPIRED,
            LoopLifecycle.FAILED,
        }:
            return "No resumable loop."
        if record.state is not LoopLifecycle.PAUSED:
            return "Loop is not paused."
        now = self.clock()
        self.record = replace(
            record,
            state=LoopLifecycle.SCHEDULED,
            pending=True,
            next_due_at=now,
            updated_at=now,
            revision=record.revision + 1,
        )
        self._save_and_emit("loop_resumed")
        self._wake.set()
        return "Loop resumed; one iteration is due now."

    async def tick_once(self) -> bool:
        """Run one deterministic scheduler tick; useful for tests."""

        record = self.record
        if record is None or record.state in {
            LoopLifecycle.PAUSED,
            LoopLifecycle.STOPPED,
            LoopLifecycle.EXPIRED,
            LoopLifecycle.FAILED,
            LoopLifecycle.RUNNING,
        }:
            return False
        now = self.clock()
        if now >= record.expires_at:
            self.record = replace(
                record,
                state=LoopLifecycle.EXPIRED,
                pending=False,
                updated_at=now,
                revision=record.revision + 1,
            )
            self._save_and_emit("loop_expired")
            return False
        if now < record.next_due_at:
            return False
        if not self.is_idle():
            if record.state is not LoopLifecycle.WAITING_FOR_IDLE or not record.pending:
                self.record = replace(
                    record,
                    state=LoopLifecycle.WAITING_FOR_IDLE,
                    pending=True,
                    coalesced_runs=record.coalesced_runs + 1,
                    updated_at=now,
                    revision=record.revision + 1,
                )
                self._save_and_emit("loop_due_while_busy", {"coalesced": True})
            return False

        claimed = replace(
            record,
            state=LoopLifecycle.RUNNING,
            pending=False,
            runs=record.runs + 1,
            last_dispatch_at=now,
            updated_at=now,
            revision=record.revision + 1,
        )
        self.record = claimed
        self._save_and_emit("loop_dispatch_started")
        try:
            await self.dispatch(claimed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._finish_failure(claimed, exc)
        else:
            await self._finish_success(claimed)
        return True

    async def _finish_success(self, claimed: LoopRecord) -> None:
        current = self.record
        if (
            current is None
            or current.loop_id != claimed.loop_id
            or current.state is LoopLifecycle.STOPPED
        ):
            return
        now = self.clock()
        if current.state is LoopLifecycle.PAUSED:
            state = LoopLifecycle.PAUSED
            pending = False
        elif now >= current.expires_at:
            state = LoopLifecycle.EXPIRED
            pending = False
        else:
            state = LoopLifecycle.SCHEDULED
            pending = False
        self.record = replace(
            current,
            state=state,
            pending=pending,
            next_due_at=now + current.interval_s,
            last_completed_at=now,
            last_error="",
            consecutive_failures=0,
            updated_at=now,
            revision=current.revision + 1,
        )
        self._save_and_emit("loop_dispatch_completed")

    async def _finish_failure(self, claimed: LoopRecord, exc: Exception) -> None:
        current = self.record
        if (
            current is None
            or current.loop_id != claimed.loop_id
            or current.state is LoopLifecycle.STOPPED
        ):
            return
        now = self.clock()
        consecutive = current.consecutive_failures + 1
        terminal = consecutive >= self.settings.max_consecutive_failures
        state = (
            LoopLifecycle.PAUSED
            if current.state is LoopLifecycle.PAUSED
            else LoopLifecycle.FAILED
            if terminal
            else LoopLifecycle.SCHEDULED
        )
        # Backoff is bounded below the requested interval and never causes a
        # tight retry storm when the provider is unavailable.
        delay = min(current.interval_s, max(1.0, 2 ** min(consecutive - 1, 8)))
        self.record = replace(
            current,
            state=state,
            pending=state is LoopLifecycle.SCHEDULED,
            next_due_at=now + delay,
            failures=current.failures + 1,
            consecutive_failures=consecutive,
            last_completed_at=now,
            last_error=_redacted_error(exc),
            updated_at=now,
            revision=current.revision + 1,
        )
        self._save_and_emit(
            "loop_failed" if terminal else "loop_retry_scheduled",
            {"error": _redacted_error(exc), "backoff_s": delay},
        )

    async def _run(self) -> None:
        while not self._stopping:
            record = self.record
            if record is None or record.state in {
                LoopLifecycle.PAUSED,
                LoopLifecycle.STOPPED,
                LoopLifecycle.EXPIRED,
                LoopLifecycle.FAILED,
            }:
                await self._wait_for_wake(None)
                continue
            delay = max(0.0, record.next_due_at - self.clock())
            if record.state is LoopLifecycle.WAITING_FOR_IDLE:
                delay = min(delay, max(0.05, float(self.settings.busy_poll_s)))
            await self._wait_for_wake(delay)
            if not self._stopping:
                await self.tick_once()

    async def _wait_for_wake(self, timeout: float | None) -> None:
        self._wake.clear()
        if timeout is None:
            await self._wake.wait()
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return

    def _validate_interval(self, interval_s: int) -> None:
        if interval_s < self.settings.min_interval_s:
            raise ValueError(
                f"interval must be at least {format_loop_duration(self.settings.min_interval_s)}"
            )
        if interval_s > self.settings.max_interval_s:
            raise ValueError(
                f"interval must be at most {format_loop_duration(self.settings.max_interval_s)}"
            )
        if self.settings.max_age_s <= 0:
            raise ValueError("loop maximum age must be positive")
        if self.settings.max_consecutive_failures <= 0:
            raise ValueError("loop maximum consecutive failures must be positive")

    def _load_record_if_needed(self) -> LoopRecord | None:
        if self.record is not None or not self.settings.persist:
            return self.record
        try:
            self.record = self.store.load()
        except LoopStorageError:
            return None
        return self.record

    def _save_and_emit(self, kind: str, extra: Mapping[str, object] | None = None) -> None:
        record = self.record
        if record is None:
            return
        if self.settings.persist:
            try:
                self.store.save(record)
            except LoopStorageError as exc:
                self.event_sink("loop_persistence_failed", {"error": str(exc)})
        payload: dict[str, object] = {
            "loop_id": record.loop_id,
            "session_id": record.session_id,
            "state": record.state.value,
            "revision": record.revision,
            "runs": record.runs,
            "failures": record.consecutive_failures,
        }
        if extra:
            payload.update(extra)
        self.event_sink(kind, payload)


def _redacted_preview(payload: str, *, limit: int = 96) -> str:
    value = re.sub(r"(?i)\b(sk|pk|api|token|bearer)[-_ ][A-Za-z0-9._-]+", r"\1 [redacted]", payload)
    value = " ".join(value.split())
    return value[:limit] + ("…" if len(value) > limit else "")


def _redacted_error(exc: Exception) -> str:
    return _redacted_preview(f"{type(exc).__name__}: {exc}", limit=240)
