"""Local worker supervisor for durable background sessions (PRD-141)."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, TypedDict
from .deletion import DeleteFailure, DeleteResult
from .input_inbox import BackgroundInput, BackgroundInputInbox
from .model import (
    ACTIVE_STATUSES,
    BackgroundSession,
    ModeApplicationStatus,
    QueueDispatchState,
    QueuePriority,
    SessionStatus,
)
from .queue import (
    current_owner_id,
    fair_queue_order,
    is_pending_queue_record,
    occupies_worker_slot,
    parse_priority,
    queue_position_map,
)
from agenthicc.runners.process_lease import InterProcessLock, atomic_replace
from .store import (
    BackgroundStore,
    InvalidSessionTransition,
    SessionNotFound,
    default_artifact_dir,
)

_MAX_ASK_USER_INPUT_CHARS = 8_000
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .settings import BackgroundNotificationSettings


class QueueFullError(RuntimeError):
    """Admission was rejected because the durable pending queue is bounded."""


class _CancellationMetadata(TypedDict, total=False):
    """Typed terminal fields accepted by ``BackgroundStore.transition``."""

    worker_pid: int | None
    worker_finished_at: float
    worker_exit_code: int
    worker_exit_reason: str
    exit_reason: str
    worker_finalization_attempts: int
    lease_token: str


class _LaunchMetadata(TypedDict, total=False):
    """Typed projection fields written after a child process is spawned."""

    worker_pid: int
    worker_started_at: float
    latest_activity: str
    dispatch_state: QueueDispatchState


class _WorkerProcessState(str, Enum):
    LIVE = "live"
    DEAD = "dead"
    MISMATCHED = "mismatched"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackgroundRequest:
    """Validated input persisted privately for a worker launch."""

    session_id: str
    workflow_name: str
    intent: str
    cwd: str
    config_path: str | None = None
    set_overrides: tuple[str, ...] = ()
    dangerously_skip_permissions: bool = False
    wall_timeout_s: float = 0.0
    max_activity_bytes: int = 64_000
    source: str = "cli"
    set_secret_overrides: tuple[str, ...] = ()
    run_id: str = ""
    detached_goal: bool = False
    mode_name: str | None = None
    resume_existing_only: bool = False
    dispatch_token: str = ""
    priority: QueuePriority = QueuePriority.NORMAL
    idempotency_key: str = ""
    owner_id: str = ""
    requested_profile_name: str = ""

    @classmethod
    def from_mapping(cls, value: object) -> "BackgroundRequest":
        """Decode a persisted request without trusting arbitrary JSON types."""

        if not isinstance(value, dict):
            raise ValueError("background request must be an object")
        required = (value.get("session_id"), value.get("intent"), value.get("cwd"))
        if not all(isinstance(item, str) and item for item in required):
            raise ValueError("background request requires session_id, intent, and cwd")
        raw_overrides = value.get("set_overrides", ())
        raw_secrets = value.get("set_secret_overrides", ())
        raw_mode_name = value.get("mode_name")
        if raw_mode_name is not None and not isinstance(raw_mode_name, str):
            raise ValueError("background request mode_name must be a string or null")
        raw_priority = value.get("priority", QueuePriority.NORMAL.value)
        try:
            priority = parse_priority(
                raw_priority if isinstance(raw_priority, str) else QueuePriority.NORMAL
            )
        except ValueError as exc:
            raise ValueError("background request priority is invalid") from exc
        return cls(
            session_id=str(value["session_id"]),
            workflow_name=str(value.get("workflow_name", "")),
            intent=str(value["intent"]),
            cwd=str(value["cwd"]),
            config_path=(
                value.get("config_path") if isinstance(value.get("config_path"), str) else None
            ),
            set_overrides=tuple(item for item in raw_overrides if isinstance(item, str))
            if isinstance(raw_overrides, (list, tuple))
            else (),
            dangerously_skip_permissions=bool(value.get("dangerously_skip_permissions", False)),
            wall_timeout_s=(
                float(value.get("wall_timeout_s", 0.0))
                if isinstance(value.get("wall_timeout_s", 0.0), (int, float))
                and not isinstance(value.get("wall_timeout_s", 0.0), bool)
                else 0.0
            ),
            max_activity_bytes=(
                int(value.get("max_activity_bytes", 64_000))
                if isinstance(value.get("max_activity_bytes", 64_000), int)
                and not isinstance(value.get("max_activity_bytes", 64_000), bool)
                else 64_000
            ),
            source=str(value.get("source", "cli")),
            set_secret_overrides=tuple(item for item in raw_secrets if isinstance(item, str))
            if isinstance(raw_secrets, (list, tuple))
            else (),
            run_id=str(value.get("run_id", "")),
            detached_goal=bool(value.get("detached_goal", False)),
            mode_name=raw_mode_name[:128] if isinstance(raw_mode_name, str) else None,
            resume_existing_only=value.get("resume_existing_only") is True,
            dispatch_token=(
                value.get("dispatch_token", "")[:128]
                if isinstance(value.get("dispatch_token", ""), str)
                else ""
            ),
            priority=priority,
            idempotency_key=(
                value.get("idempotency_key", "")[:256]
                if isinstance(value.get("idempotency_key", ""), str)
                else ""
            ),
            owner_id=(
                value.get("owner_id", "")[:256]
                if isinstance(value.get("owner_id", ""), str)
                else ""
            ),
            requested_profile_name=(
                value.get("requested_profile_name", "")[:128]
                if isinstance(value.get("requested_profile_name", ""), str)
                else ""
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "workflow_name": self.workflow_name,
            "intent": self.intent,
            "cwd": self.cwd,
            "config_path": self.config_path,
            "set_overrides": list(self.set_overrides),
            "dangerously_skip_permissions": self.dangerously_skip_permissions,
            "set_secret_overrides": list(self.set_secret_overrides),
            "wall_timeout_s": self.wall_timeout_s,
            "max_activity_bytes": self.max_activity_bytes,
            "source": self.source,
            "run_id": self.run_id,
            "detached_goal": self.detached_goal,
            "mode_name": self.mode_name[:128] if isinstance(self.mode_name, str) else None,
            "resume_existing_only": self.resume_existing_only,
            "dispatch_token": self.dispatch_token,
            "priority": self.priority.value,
            "idempotency_key": self.idempotency_key,
            "owner_id": self.owner_id,
            "requested_profile_name": self.requested_profile_name,
        }


class BackgroundSupervisor:
    """Own worker processes and expose idempotent lifecycle operations."""

    def __init__(
        self,
        store: BackgroundStore | None = None,
        *,
        max_workers: int = 4,
        max_workers_per_project: int = 4,
        cancel_grace_s: float = 5.0,
        wall_timeout_s: float = 0.0,
        max_activity_bytes: int = 64_000,
        artifact_root: Path | None = None,
        trash_retention_days: int = 30,
        queue_enabled: bool = False,
        max_queue_length: int = 100,
        max_workers_per_user: int | None = None,
        priority_aging_interval_s: float = 900.0,
        dispatch_lease_s: float = 30.0,
        notification_settings: BackgroundNotificationSettings | None = None,
    ) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or not 1 <= max_workers <= 1_024
        ):
            raise ValueError("max_workers must be an integer between 1 and 1024")
        self.store = store or BackgroundStore()
        if notification_settings is not None:
            try:
                self.store.configure_notifications(notification_settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "background notification service unavailable (%s)", type(exc).__name__
                )
        self.max_workers = max_workers
        if (
            isinstance(max_workers_per_project, bool)
            or not isinstance(max_workers_per_project, int)
            or not 1 <= max_workers_per_project <= 1_024
        ):
            raise ValueError("max_workers_per_project must be an integer between 1 and 1024")
        self.max_workers_per_project = max_workers_per_project
        if not isinstance(queue_enabled, bool):
            raise ValueError("queue_enabled must be a boolean")
        if (
            isinstance(max_queue_length, bool)
            or not isinstance(max_queue_length, int)
            or not 1 <= max_queue_length <= 100_000
        ):
            raise ValueError("max_queue_length must be an integer between 1 and 100000")
        if max_workers_per_user is not None and (
            isinstance(max_workers_per_user, bool)
            or not isinstance(max_workers_per_user, int)
            or not 1 <= max_workers_per_user <= 1_024
        ):
            raise ValueError("max_workers_per_user must be an integer between 1 and 1024")
        if (
            isinstance(priority_aging_interval_s, bool)
            or not isinstance(priority_aging_interval_s, (int, float))
            or not math.isfinite(float(priority_aging_interval_s))
            or not 1.0 <= priority_aging_interval_s <= 2_678_400.0
        ):
            raise ValueError("priority_aging_interval_s must be between 1 and 2678400")
        if (
            isinstance(dispatch_lease_s, bool)
            or not isinstance(dispatch_lease_s, (int, float))
            or not math.isfinite(float(dispatch_lease_s))
            or not 0.1 <= dispatch_lease_s <= 3_600.0
        ):
            raise ValueError("dispatch_lease_s must be between 0.1 and 3600")
        self.queue_enabled = queue_enabled
        self.max_queue_length = max_queue_length
        self.max_workers_per_user = max_workers_per_user
        self.priority_aging_interval_s = priority_aging_interval_s
        self.dispatch_lease_s = dispatch_lease_s
        self.cancel_grace_s = max(0.1, cancel_grace_s)
        self.wall_timeout_s = max(0.0, wall_timeout_s)
        self.max_activity_bytes = max(1, max_activity_bytes)
        if trash_retention_days < 0:
            raise ValueError("trash_retention_days must be non-negative")
        self.trash_retention_days = trash_retention_days
        self.artifact_root = (artifact_root or Path.home() / ".agenthicc" / "sessions").expanduser()
        self._recovery_cursor = ""

    @property
    def _scheduler_state_path(self) -> Path:
        return self.store.root / "queue-scheduler-v1.json"

    def _scheduler_state(self) -> dict[str, object]:
        try:
            value = json.loads(self._scheduler_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_scheduler_state(self, *, last_project: str) -> None:
        payload = json.dumps(
            {"schema_version": 1, "last_project": last_project},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        atomic_replace(self._scheduler_state_path, payload, prefix=".queue-state-")

    def _scheduler_lock(self) -> InterProcessLock:
        return InterProcessLock(self.store.root / "queue-scheduler.lock")

    def _worker_slots(self, sessions: Sequence[BackgroundSession]) -> int:
        return sum(occupies_worker_slot(item) for item in sessions)

    def _has_capacity(
        self,
        sessions: Sequence[BackgroundSession],
        *,
        project: str,
        owner_id: str,
    ) -> bool:
        occupying = [item for item in sessions if occupies_worker_slot(item)]
        if len(occupying) >= self.max_workers:
            return False
        if sum(item.cwd == project for item in occupying) >= self.max_workers_per_project:
            return False
        if self.max_workers_per_user is not None:
            owned = sum(item.owner_id in {"", owner_id} for item in occupying)
            if owned >= self.max_workers_per_user:
                return False
        return True

    def _active_count(self, cwd: str | None = None) -> int:
        return sum(
            item.status in ACTIVE_STATUSES and (cwd is None or item.cwd == cwd)
            for item in self.store.list(include_archived=False)
        )

    def _request_path(self, session_id: str) -> Path:
        path = (self.store.root / "requests" / f"{session_id}.json").expanduser().resolve()
        if path.name != f"{session_id}.json":
            raise ValueError("invalid background session id")
        return path

    def _write_request(self, request: BackgroundRequest) -> Path:
        path = self._request_path(request.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(request.to_dict(), indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return path

    def _worker_command(self, request_path: Path) -> list[str]:
        return [
            sys.executable,
            "-m",
            "agenthicc.background.worker",
            "--request-file",
            str(request_path),
            "--store-root",
            str(self.store.root.expanduser().resolve()),
        ]

    def _launch(self, request: BackgroundRequest, session: BackgroundSession) -> BackgroundSession:
        request_path = self._write_request(request)
        artifact_dir = Path(session.artifact_dir or default_artifact_dir(session.session_id))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = artifact_dir / "background-worker.log"
        try:
            log_handle = log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                self._worker_command(request_path),
                cwd=request.cwd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
                close_fds=(os.name != "nt"),
            )
        except OSError as exc:
            try:
                log_handle.close()
            except UnboundLocalError:
                pass
            return self.store.transition(
                session.session_id,
                SessionStatus.FAILED,
                error=f"Worker launch failed: {type(exc).__name__}: {exc}",
                latest_activity="Worker launch failed",
                dispatch_state=(
                    QueueDispatchState.FAILED if request.dispatch_token else session.dispatch_state
                ),
                worker_exit_reason="launch_failed",
                exit_reason="launch_failed",
                worker_exit_code=1,
                worker_finished_at=time.time(),
            )
        finally:
            if "log_handle" in locals():
                log_handle.close()
        # A very short worker can finish before the parent records its PID.
        # Never overwrite a terminal result with stale launch metadata.
        current = self.store.get(session.session_id, include_deleted=True)
        if current.status in ACTIVE_STATUSES:
            changes: _LaunchMetadata = {
                "worker_pid": process.pid,
                "worker_started_at": time.time(),
                "latest_activity": "Worker process launched",
            }
            if current.dispatch_state is QueueDispatchState.CLAIMED:
                changes["dispatch_state"] = QueueDispatchState.LAUNCHED
            try:
                return self.store.update(
                    session.session_id,
                    expected_status=current.status,
                    expected_attempt=current.attempt,
                    **changes,
                )
            except InvalidSessionTransition:
                # The child may claim or finish between Popen and this
                # projection update. Its newer lifecycle state is canonical.
                return self.store.get(session.session_id, include_deleted=True)
        return current

    def _load_request(self, session_id: str) -> BackgroundRequest:
        try:
            raw = json.loads(self._request_path(session_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load queued request {session_id}: {exc}") from exc
        request = BackgroundRequest.from_mapping(raw)
        if request.session_id != session_id:
            raise RuntimeError("queued request session ID does not match its record")
        return request

    def _queued_records(self) -> list[BackgroundSession]:
        return [
            item
            for item in self.store.list(include_archived=False)
            if is_pending_queue_record(item)
        ]

    def _scheduler_last_project(self) -> str:
        value = self._scheduler_state().get("last_project", "")
        return value if isinstance(value, str) else ""

    def _start_claimed_reservations(self, *, now: float) -> list[BackgroundSession]:
        """Recover a claimed dispatch lease after a crash around subprocess start."""

        changed: list[BackgroundSession] = []
        for session in self.store.list(include_archived=False):
            if (
                session.status is not SessionStatus.STARTING
                or session.dispatch_state is not QueueDispatchState.CLAIMED
            ):
                continue
            if session.worker_pid is not None:
                process_state = self._worker_process_state(session)
                if process_state in {_WorkerProcessState.LIVE, _WorkerProcessState.UNKNOWN}:
                    continue
                try:
                    session = self.store.update(
                        session.session_id,
                        expected_status=SessionStatus.STARTING,
                        expected_attempt=session.attempt,
                        worker_pid=None,
                        worker_started_at=None,
                    )
                except InvalidSessionTransition:
                    continue
            elif (
                session.dispatch_claimed_at is None
                or now - session.dispatch_claimed_at < self.dispatch_lease_s
            ):
                continue
            try:
                request = self._load_request(session.session_id)
                if not session.dispatch_token or request.dispatch_token != session.dispatch_token:
                    raise RuntimeError("queued request dispatch token does not match its lease")
            except (OSError, RuntimeError, ValueError) as exc:
                try:
                    changed.append(
                        self.store.transition(
                            session.session_id,
                            SessionStatus.ORPHANED,
                            expected_status=SessionStatus.STARTING,
                            expected_attempt=session.attempt,
                            error=f"Queued worker recovery failed: {type(exc).__name__}: {exc}",
                            latest_activity="Queued worker needs recovery",
                        )
                    )
                except InvalidSessionTransition:
                    pass
                continue
            try:
                changed.append(self._launch(request, session))
            except OSError as exc:
                try:
                    changed.append(
                        self.store.transition(
                            session.session_id,
                            SessionStatus.FAILED,
                            expected_status=SessionStatus.STARTING,
                            expected_attempt=session.attempt,
                            dispatch_state=QueueDispatchState.FAILED,
                            error=f"Worker launch failed: {type(exc).__name__}: {exc}",
                            latest_activity="Worker launch failed",
                            worker_exit_reason="launch_failed",
                            exit_reason="launch_failed",
                            worker_exit_code=1,
                            worker_finished_at=now,
                        )
                    )
                except InvalidSessionTransition:
                    pass
        return changed

    def _dispatch_pending_locked(self, *, now: float | None = None) -> list[BackgroundSession]:
        """Dispatch eligible durable requests while the cross-process lock is held."""

        timestamp = time.time() if now is None else now
        changed = self._start_claimed_reservations(now=timestamp)
        last_project = self._scheduler_last_project()
        while True:
            sessions = self.store.list(include_archived=False)
            running = [item for item in sessions if occupies_worker_slot(item)]
            if len(running) >= self.max_workers:
                break
            ordered = fair_queue_order(
                sessions,
                now=timestamp,
                aging_interval_s=self.priority_aging_interval_s,
                last_project=last_project,
            )
            selected: BackgroundSession | None = None
            for candidate in ordered:
                if (
                    sum(item.cwd == candidate.cwd for item in running)
                    >= self.max_workers_per_project
                ):
                    continue
                if self.max_workers_per_user is not None:
                    user_count = sum(item.owner_id in {"", candidate.owner_id} for item in running)
                    if user_count >= self.max_workers_per_user:
                        continue
                selected = candidate
                break
            if selected is None:
                break

            dispatch_token = uuid.uuid4().hex
            try:
                request = replace(
                    self._load_request(selected.session_id), dispatch_token=dispatch_token
                )
                # Persist the child input first. A crash before the following
                # CAS leaves an unreferenced request that can be overwritten.
                self._write_request(request)
                reserved = self.store.transition(
                    selected.session_id,
                    SessionStatus.STARTING,
                    expected_status=SessionStatus.QUEUED,
                    expected_attempt=selected.attempt,
                    dispatch_state=QueueDispatchState.CLAIMED,
                    dispatch_token=dispatch_token,
                    dispatch_claimed_at=timestamp,
                    latest_activity="Queued · starting worker",
                )
            except InvalidSessionTransition:
                continue
            except (OSError, RuntimeError, ValueError) as exc:
                try:
                    changed.append(
                        self.store.transition(
                            selected.session_id,
                            SessionStatus.FAILED,
                            expected_status=SessionStatus.QUEUED,
                            expected_attempt=selected.attempt,
                            dispatch_state=QueueDispatchState.FAILED,
                            error=(
                                f"Queued worker could not be prepared: {type(exc).__name__}: {exc}"
                            ),
                            latest_activity="Queued request could not be started",
                            worker_exit_reason="queued_request_invalid",
                            exit_reason="queued_request_invalid",
                            worker_exit_code=1,
                            worker_finished_at=timestamp,
                        )
                    )
                except InvalidSessionTransition:
                    pass
                continue

            last_project = reserved.cwd
            try:
                self._write_scheduler_state(last_project=last_project)
            except OSError:
                logger.warning("could not persist background queue fairness cursor", exc_info=True)
            try:
                changed.append(self._launch(request, reserved))
            except OSError as exc:
                try:
                    changed.append(
                        self.store.transition(
                            reserved.session_id,
                            SessionStatus.FAILED,
                            expected_status=SessionStatus.STARTING,
                            expected_attempt=reserved.attempt,
                            dispatch_state=QueueDispatchState.FAILED,
                            error=f"Worker launch failed: {type(exc).__name__}: {exc}",
                            latest_activity="Worker launch failed",
                            worker_exit_reason="launch_failed",
                            exit_reason="launch_failed",
                            worker_exit_code=1,
                            worker_finished_at=timestamp,
                        )
                    )
                except InvalidSessionTransition:
                    pass
        return changed

    def dispatch_pending(self, *, now: float | None = None) -> list[BackgroundSession]:
        """Dispatch pending work under a process-safe scheduler lease."""

        with self._scheduler_lock():
            return self._dispatch_pending_locked(now=now)

    def queue_positions(self, *, now: float | None = None) -> dict[str, int]:
        """Return current advisory positions for the user-visible pending queue."""

        timestamp = time.time() if now is None else now
        with self._scheduler_lock():
            return queue_position_map(
                self.store.list(include_archived=False),
                now=timestamp,
                aging_interval_s=self.priority_aging_interval_s,
                last_project=self._scheduler_last_project(),
            )

    def _existing_idempotent_request(
        self,
        *,
        key: str,
        owner_id: str,
        project: str,
        intent: str,
        workflow_name: str,
        mode_name: str | None,
        profile_name: str,
    ) -> BackgroundSession | None:
        if not key:
            return None
        for existing in self.store.list(include_archived=True):
            if existing.idempotency_key != key or existing.owner_id != owner_id:
                continue
            if (
                existing.cwd != project
                or existing.intent != intent
                or existing.workflow_name != workflow_name
                or existing.requested_mode_name != mode_name
                or existing.requested_profile_name != profile_name
            ):
                raise ValueError(
                    "idempotency key was already used for a different background request"
                )
            return existing
        return None

    def submit(
        self,
        *,
        intent: str,
        workflow_name: str = "",
        title: str = "",
        cwd: str | None = None,
        session_id: str | None = None,
        config_path: str | None = None,
        set_overrides: tuple[str, ...] = (),
        dangerously_skip_permissions: bool = False,
        set_secret_overrides: tuple[str, ...] = (),
        parent_session_id: str = "",
        role: str = "",
        task_id: str = "",
        worktree_id: str = "",
        branch: str = "",
        base_commit: str = "",
        run_id: str = "",
        detached_goal: bool = False,
        mode_name: str | None = None,
        priority: QueuePriority | str = QueuePriority.NORMAL,
        idempotency_key: str = "",
        requested_profile_name: str = "",
        environment_receipts: tuple[str, ...] = (),
    ) -> BackgroundSession:
        """Durably accept a background request, then launch or queue it."""

        if not intent.strip():
            raise ValueError("Background intent must not be empty")
        # Preserve the original user payload exactly. Schedules and session
        # forwarding rely on this boundary to avoid silently rewriting prompts.
        cleaned_intent = intent
        sid = session_id or uuid.uuid4().hex
        project = str(Path(cwd or os.getcwd()).resolve())
        normalized_priority = parse_priority(priority)
        if not isinstance(idempotency_key, str) or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must be a string of at most 256 characters")
        idempotency_key = idempotency_key.strip()
        if not isinstance(requested_profile_name, str) or len(requested_profile_name) > 128:
            raise ValueError("requested_profile_name must be a string of at most 128 characters")
        owner_id = current_owner_id()
        profile_name = requested_profile_name
        if not profile_name:
            for override in reversed(set_overrides):
                key, separator, value = override.partition("=")
                if separator and key.strip() == "execution.profile":
                    profile_name = value.strip().strip("\"'")[:128]
                    break

        with self._scheduler_lock():
            self._dispatch_pending_locked()
            existing = self._existing_idempotent_request(
                key=idempotency_key,
                owner_id=owner_id,
                project=project,
                intent=cleaned_intent,
                workflow_name=workflow_name,
                mode_name=mode_name,
                profile_name=profile_name,
            )
            if existing is not None:
                return existing
            try:
                self.store.get(sid, include_deleted=True)
            except SessionNotFound:
                pass
            else:
                raise ValueError(f"Background session already exists: {sid}")

            current_sessions = self.store.list(include_archived=False)
            has_capacity = self._has_capacity(
                current_sessions,
                project=project,
                owner_id=owner_id,
            )
            if not self.queue_enabled and not has_capacity:
                if self._worker_slots(current_sessions) >= self.max_workers:
                    raise RuntimeError(f"Background worker limit reached ({self.max_workers})")
                raise RuntimeError(
                    f"Background worker limit reached for project ({self.max_workers_per_project})"
                )
            queued_count = sum(is_pending_queue_record(item) for item in current_sessions)
            queue_full = self.queue_enabled and queued_count >= self.max_queue_length
            if queue_full:
                raise QueueFullError(
                    f"Background queue is full ({self.max_queue_length} pending requests)"
                )

            request = BackgroundRequest(
                session_id=sid,
                workflow_name=workflow_name,
                intent=cleaned_intent,
                cwd=project,
                config_path=config_path,
                set_overrides=set_overrides,
                dangerously_skip_permissions=dangerously_skip_permissions,
                set_secret_overrides=set_secret_overrides,
                wall_timeout_s=self.wall_timeout_s,
                max_activity_bytes=self.max_activity_bytes,
                source="cli",
                run_id=run_id,
                detached_goal=detached_goal,
                mode_name=mode_name,
                priority=normalized_priority,
                idempotency_key=idempotency_key,
                owner_id=owner_id,
                requested_profile_name=profile_name,
            )
            dispatch_state = (
                QueueDispatchState.PENDING
                if self.queue_enabled and not queue_full
                else QueueDispatchState.LEGACY
            )
            session = BackgroundSession.create(
                sid,
                title=title or cleaned_intent[:80],
                cwd=project,
                workflow_name=workflow_name,
                intent=cleaned_intent,
                artifact_dir=str(self.artifact_root / sid),
                parent_session_id=parent_session_id,
                run_id=run_id,
                role=role,
                task_id=task_id,
                worktree_id=worktree_id,
                branch=branch,
                base_commit=base_commit,
                detached_goal=detached_goal,
                requested_mode_name=mode_name,
                priority=normalized_priority,
                idempotency_key=idempotency_key,
                owner_id=owner_id,
                requested_profile_name=profile_name,
                dispatch_state=dispatch_state,
                environment_receipts=environment_receipts,
            )
            if self.queue_enabled and not queue_full:
                session = session.evolve(latest_activity="Queued · waiting for worker capacity")
            # Write the recoverable launch envelope before publishing the
            # accepted session event; no caller receives acceptance without
            # both durable records being committed.
            self._write_request(request)
            self.store.create(session)
            if self.queue_enabled and not queue_full:
                try:
                    self._dispatch_pending_locked()
                except Exception:  # noqa: BLE001
                    # Admission is already durable. Do not turn a committed
                    # queue receipt into a misleading submission failure.
                    logger.warning(
                        "background request %s was accepted but dispatch did not complete",
                        sid,
                        exc_info=True,
                    )
                return self.store.get(sid, include_deleted=True)
            return self._launch(request, session)

    def handoff(
        self,
        *,
        session_id: str,
        intent: str,
        workflow_name: str = "",
        title: str = "Foreground session",
        cwd: str | None = None,
        config_path: str | None = None,
        set_overrides: tuple[str, ...] = (),
        dangerously_skip_permissions: bool = False,
        set_secret_overrides: tuple[str, ...] = (),
        run_id: str = "",
        start: bool = True,
        mode_name: str | None = None,
        resume_existing_only: bool | None = None,
    ) -> BackgroundSession:
        """Detach an existing foreground session into one tracked worker."""

        existing: BackgroundSession | None
        try:
            existing = self.store.get(session_id)
        except KeyError:
            existing = None
        if existing is None:
            project = str(Path(cwd or os.getcwd()).resolve())
            existing = BackgroundSession.create(
                session_id,
                title=title,
                cwd=project,
                workflow_name=workflow_name,
                intent=intent,
                artifact_dir=str(self.artifact_root / session_id),
                run_id=run_id,
                requested_mode_name=mode_name,
            )
            self.store.create(existing)
        elif existing.status in ACTIVE_STATUSES:
            raise InvalidSessionTransition("Session is already managed by a background worker")
        was_completed = existing.status is SessionStatus.COMPLETED
        effective_mode_name = self._mode_name_for_resume(session_id, mode_name)
        request = BackgroundRequest(
            session_id=session_id,
            workflow_name=workflow_name or existing.workflow_name,
            intent=intent or existing.intent,
            cwd=str(Path(cwd or existing.cwd).resolve()),
            config_path=config_path,
            set_overrides=set_overrides,
            dangerously_skip_permissions=dangerously_skip_permissions,
            set_secret_overrides=set_secret_overrides,
            run_id=run_id or existing.run_id,
            wall_timeout_s=self.wall_timeout_s,
            max_activity_bytes=self.max_activity_bytes,
            mode_name=effective_mode_name,
            resume_existing_only=(
                was_completed if resume_existing_only is None else resume_existing_only
            ),
            priority=existing.priority,
            idempotency_key=existing.idempotency_key,
            owner_id=existing.owner_id or current_owner_id(),
            requested_profile_name=existing.requested_profile_name,
        )
        if existing.status == SessionStatus.FAILED:
            existing = self.store.transition(
                session_id,
                SessionStatus.RETRYING,
                retry_count=existing.retry_count + 1,
                resume_marker=f"retry:{existing.attempt + 1}",
            )
        elif existing.status in {
            SessionStatus.CANCELLED,
            SessionStatus.ORPHANED,
            SessionStatus.ARCHIVED,
        }:
            existing = self.store.transition(
                session_id,
                SessionStatus.STARTING,
                resume_marker=f"resume:{existing.attempt + 1}",
            )
        elif existing.status is SessionStatus.COMPLETED:
            existing = self.store.transition(
                session_id,
                SessionStatus.STARTING,
                expected_status=SessionStatus.COMPLETED,
                expected_attempt=existing.attempt,
                resume_marker=f"resume:{existing.attempt + 1}",
                completed_at=None,
                error=None,
                failure_category="",
                exit_reason="",
                worker_exit_reason="",
                worker_exit_code=None,
                worker_finished_at=None,
                latest_activity="Foreground session handoff prepared",
            )
        existing = self.store.update(
            session_id,
            expected_status=existing.status,
            expected_attempt=existing.attempt,
            requested_mode_name=effective_mode_name,
            mode_name="",
            mode_application_status="pending",
            mode_application_attempt=existing.attempt + 1,
            mode_application_error="",
        )
        if not start:
            # Persist the request before a caller releases a foreground owner.
            # ``start`` then launches this exact request after the owner handoff
            # has completed, eliminating a foreground/background write race.
            self._write_request(request)
            return existing
        if self.queue_enabled:
            self._write_request(request)
            return self.start(session_id)
        return self._launch(request, existing)

    def start(self, session_id: str) -> BackgroundSession:
        """Launch a request prepared by ``handoff(..., start=False)``."""

        request_path = self._request_path(session_id)
        try:
            raw = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load prepared background request: {exc}") from exc
        request = BackgroundRequest.from_mapping(raw)
        if request.session_id != session_id:
            raise ValueError("prepared background request session ID does not match")
        session = self.store.get(session_id, include_deleted=True)
        if session.status not in ACTIVE_STATUSES:
            raise InvalidSessionTransition(f"Cannot start prepared {session.status.value} session")
        if is_pending_queue_record(session):
            self.dispatch_pending()
            return self.store.get(session_id, include_deleted=True)
        if not self.queue_enabled:
            return self._launch(request, session)
        with self._scheduler_lock():
            session = self.store.get(session_id, include_deleted=True)
            sessions = [
                item
                for item in self.store.list(include_archived=False)
                if item.session_id != session_id
            ]
            if not self._has_capacity(
                sessions,
                project=session.cwd,
                owner_id=session.owner_id or current_owner_id(),
            ):
                queued_count = sum(is_pending_queue_record(item) for item in sessions)
                if queued_count >= self.max_queue_length:
                    # The handoff is not accepted into the bounded queue. Keep
                    # its durable transcript and make the rejection explicit,
                    # so a later retry can be submitted without pretending a
                    # worker was started or silently exceeding queue bounds.
                    self.store.transition(
                        session_id,
                        SessionStatus.FAILED,
                        expected_status=session.status,
                        expected_attempt=session.attempt,
                        dispatch_state=QueueDispatchState.FAILED,
                        error=(
                            f"Background queue is full ({self.max_queue_length} pending requests)"
                        ),
                        latest_activity="Background handoff rejected · queue full",
                        worker_exit_reason="queue_full",
                        exit_reason="queue_full",
                        worker_exit_code=1,
                        worker_finished_at=time.time(),
                    )
                    raise QueueFullError(
                        f"Background queue is full ({self.max_queue_length} pending requests)"
                    )
                queued = self.store.transition(
                    session_id,
                    SessionStatus.QUEUED,
                    expected_status=session.status,
                    expected_attempt=session.attempt,
                    dispatch_state=QueueDispatchState.PENDING,
                    owner_id=session.owner_id or current_owner_id(),
                    latest_activity="Queued · waiting for worker capacity",
                )
                return queued
            return self._launch(request, session)

    def _mode_name_for_resume(self, session_id: str, explicit_mode_name: str | None) -> str | None:
        """Preserve a requested mode only until canonical session state exists."""

        if explicit_mode_name is not None:
            return explicit_mode_name
        from agenthicc.tui.runtime.session_log import load_session_mode  # noqa: PLC0415

        try:
            session = self.store.get(session_id, include_deleted=True)
        except KeyError:
            session = None
        if session is not None:
            if (
                session.mode_application_status is ModeApplicationStatus.APPLIED
                and session.mode_name
            ):
                # Normally session metadata already contains this value. If
                # it was lost/corrupted, the attested canonical mode remains
                # sufficient to recover the worker's runtime selection.
                return None if load_session_mode(session_id) is not None else session.mode_name
            if session.requested_mode_name is not None:
                # New-format records distinguish an unconsumed explicit
                # request from placeholder metadata such as register_session's
                # initial Safe value. Do not let that placeholder erase it.
                return session.requested_mode_name
        if load_session_mode(session_id) is not None:
            return None
        try:
            raw = json.loads(self._request_path(session_id).read_text(encoding="utf-8"))
            request = BackgroundRequest.from_mapping(raw)
            return request.mode_name if request.session_id == session_id else None
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def resume(self, session_id: str, *, mode_name: str | None = None) -> BackgroundSession:
        session = self.store.get(session_id)
        if session.status not in {
            SessionStatus.ORPHANED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.ARCHIVED,
        }:
            raise InvalidSessionTransition(f"Cannot resume {session.status.value} session")
        return self.handoff(
            session_id=session_id,
            intent=session.intent,
            workflow_name=session.workflow_name,
            cwd=session.cwd,
            mode_name=self._mode_name_for_resume(session_id, mode_name),
        )

    def retry(self, session_id: str, *, mode_name: str | None = None) -> BackgroundSession:
        return self.resume(session_id, mode_name=mode_name)

    def attach_foreground(self, session_id: str) -> BackgroundSession:
        """Stop background execution and release *session_id* to the TUI.

        A foreground TUI uses the same durable session journal as a background
        worker.  Starting it while the worker still owns the session would
        allow two agent runtimes to append turns, checkpoints, and tool results
        concurrently.  This operation therefore makes the ownership handoff
        explicit: active work is moved through ``cancelling``, the worker and
        its session-owned terminals are stopped, and only then is the record
        left in a resumable terminal state.

        The method deliberately does not launch another worker.  The caller may
        safely construct a normal TUI with ``resume_id=session_id`` after this
        method returns.  If the worker cannot be stopped within the configured
        grace period, the record remains ``cancelling`` and an exception is
        raised so the caller cannot accidentally create a duplicate runtime.
        """

        session = self.store.get(session_id, include_deleted=True)
        if session.status == SessionStatus.DELETED:
            raise InvalidSessionTransition("Deleted background sessions cannot be foregrounded")
        if session.status not in ACTIVE_STATUSES:
            return session

        if session.status != SessionStatus.CANCELLING:
            try:
                session = self.store.transition(
                    session_id,
                    SessionStatus.CANCELLING,
                    cancellation_reason="foreground handoff requested",
                    latest_activity="Stopping background worker for foreground handoff",
                )
            except InvalidSessionTransition:
                # A short-lived worker can finish between the initial read and
                # this transition.  Re-read before reporting failure; a
                # terminal record is already safe to open in the foreground.
                session = self.store.get(session_id, include_deleted=True)
                if session.status in ACTIVE_STATUSES:
                    raise
                return session

        pid = session.worker_pid
        if pid is not None:
            self._terminate(pid)
            deadline = time.monotonic() + self.cancel_grace_s
            while time.monotonic() < deadline and self._alive(pid):
                time.sleep(0.05)
            if self._alive(pid):
                raise RuntimeError(
                    f"Background worker {pid} did not stop; foreground handoff aborted"
                )

        # Detached terminal tools use their own process groups, so stopping
        # the worker group alone is not sufficient to release this session.
        from .terminals import stop_persisted_session_terminals  # noqa: PLC0415

        stop_persisted_session_terminals(
            session_id,
            store_root=self.store.root / "terminals",
            grace_s=self.cancel_grace_s,
        )
        current = self.store.get(session_id, include_deleted=True)
        if current.status == SessionStatus.CANCELLING:
            cancelled = self.store.transition(
                session_id,
                SessionStatus.CANCELLED,
                cancellation_reason="foreground handoff requested",
                latest_activity="Background worker stopped; ready for foreground",
                **self._cancelled_metadata(current),
            )
            if current.detached_goal:
                self.store.record_worker_exit(
                    session_id,
                    worker_pid=cancelled.worker_pid,
                    exit_reason="cancelled",
                    exit_code=130,
                )
            return cancelled
        if current.status in ACTIVE_STATUSES:
            raise RuntimeError(
                f"Background session {session_id} is still active; foreground handoff aborted"
            )
        return current

    def provide_input(
        self,
        session_id: str,
        value: str,
        *,
        expected_attempt: int | None = None,
        expected_lease_token: str | None = None,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> BackgroundSession:
        """Deliver explicit user input to a session paused at ``waiting_input``."""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("Input must not be empty")
        if len(value) > _MAX_ASK_USER_INPUT_CHARS:
            raise ValueError(f"Input exceeds {_MAX_ASK_USER_INPUT_CHARS} characters")
        session = self.store.get(session_id)
        if session.status != SessionStatus.WAITING_INPUT:
            raise InvalidSessionTransition("Session is not waiting for input")
        if session.input_item_id:
            from agenthicc.background.inbox import (  # noqa: PLC0415
                BackgroundInteractionInbox,
                response_from_text,
            )

            inbox = BackgroundInteractionInbox(self.store)
            item = inbox.get(session.input_item_id)
            inbox.respond(
                item.item_id,
                response_from_text(item, value),
                expected_version=expected_version if expected_version is not None else item.version,
                idempotency_key=idempotency_key or uuid.uuid4().hex,
            )
            return self.store.get(session_id)
        return self.store.update(
            session_id,
            expected_status=SessionStatus.WAITING_INPUT,
            expected_attempt=expected_attempt,
            expected_lease_token=expected_lease_token,
            input_value=value,
            latest_activity="Input received",
        )

    def enqueue_input(
        self,
        session_id: str,
        text: str,
        *,
        owner_attempt: int,
        lease_token: str,
        message_id: str,
    ) -> BackgroundInput:
        """Accept one durable message for the exact live worker attempt."""

        return BackgroundInputInbox(self.store).enqueue(
            session_id,
            text,
            owner_attempt=owner_attempt,
            lease_token=lease_token,
            message_id=message_id,
        )

    def enqueue_input_or_recover(
        self,
        session_id: str,
        text: str,
        *,
        expected_attempt: int,
        expected_lease_token: str,
        message_id: str,
    ) -> BackgroundInput:
        """Queue input for a live worker or recover a stale session in background.

        Recoverable terminal sessions receive a durable deferred inbox item
        before a worker attempt starts. Completed sessions use an explicit
        input-driven transition; stale sessions retain their recovery path.
        Workers rebind the message to their lease and consume it instead of
        replaying the session's original request. No foreground handoff or
        attachment occurs.
        """

        inbox = BackgroundInputInbox(self.store)
        current = self.store.get(session_id, include_deleted=True)
        self._validate_input_target(current, expected_attempt, expected_lease_token)

        if current.status in {SessionStatus.WAITING_INPUT, SessionStatus.CANCELLING}:
            raise InvalidSessionTransition(
                f"Session is {current.status.value}; this input path is unavailable"
            )

        if current.status is SessionStatus.STARTING:
            # A starting worker has not claimed its new lease yet. Bind the
            # message to the still-current revision as deferred input so the
            # worker will rebind and consume it after claim. Do not attach it
            # to the previous attempt's lease during this startup window.
            return inbox.enqueue_deferred(
                session_id,
                text,
                expected_attempt=expected_attempt,
                expected_lease_token=expected_lease_token,
                message_id=message_id,
            )

        if current.status is SessionStatus.COMPLETED:
            return self._defer_and_reactivate_completed(
                inbox,
                session_id,
                text,
                expected_attempt=expected_attempt,
                expected_lease_token=expected_lease_token,
                message_id=message_id,
            )

        if current.status in {
            SessionStatus.ORPHANED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.ARCHIVED,
        }:
            return self._defer_and_resume(
                inbox,
                session_id,
                text,
                expected_attempt=expected_attempt,
                expected_lease_token=expected_lease_token,
                message_id=message_id,
            )

        if current.status not in ACTIVE_STATUSES:
            raise InvalidSessionTransition(
                f"Session is {current.status.value}; background input is unavailable"
            )

        if current.worker_pid is None or not current.lease_token:
            raise InvalidSessionTransition(
                "Background worker has not claimed this session yet; retry input shortly"
            )

        process_state = self._worker_process_state(current)
        if process_state in {_WorkerProcessState.DEAD, _WorkerProcessState.MISMATCHED}:
            try:
                self.store.mark_orphaned(
                    session_id,
                    expected_attempt=expected_attempt,
                    expected_lease_token=expected_lease_token,
                    expected_status=current.status,
                    expected_last_active=current.last_active,
                )
            except InvalidSessionTransition:
                # A heartbeat, owner change, or worker completion raced the
                # liveness check. Re-read and route only if the original
                # session revision is still current.
                current = self.store.get(session_id, include_deleted=True)
                self._validate_input_target(current, expected_attempt, expected_lease_token)
            else:
                current = self.store.get(session_id, include_deleted=True)

            if current.status in {
                SessionStatus.ORPHANED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
                SessionStatus.ARCHIVED,
            }:
                return self._defer_and_resume(
                    inbox,
                    session_id,
                    text,
                    expected_attempt=expected_attempt,
                    expected_lease_token=expected_lease_token,
                    message_id=message_id,
                )

        # A verified-live owner (or an owner whose identity cannot safely be
        # disproved) remains the only process allowed to consume this input.
        return inbox.enqueue(
            session_id,
            text,
            owner_attempt=expected_attempt,
            lease_token=expected_lease_token,
            message_id=message_id,
        )

    def _input_reactivation_request(self, session: BackgroundSession) -> BackgroundRequest:
        """Build a new-turn request while preserving the session's runtime config."""

        mode_name = self._mode_name_for_resume(session.session_id, None)
        try:
            raw = json.loads(self._request_path(session.session_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = None
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("Cannot load the saved session launch configuration") from exc
        else:
            previous = BackgroundRequest.from_mapping(raw)
            if previous.session_id != session.session_id:
                raise RuntimeError("Saved session launch configuration has a mismatched ID")

        if previous is None:
            return BackgroundRequest(
                session_id=session.session_id,
                workflow_name=session.workflow_name,
                intent=session.intent,
                cwd=str(Path(session.cwd).expanduser().resolve()),
                wall_timeout_s=self.wall_timeout_s,
                max_activity_bytes=self.max_activity_bytes,
                mode_name=mode_name,
            )

        # A new input is not a retry of a detached goal run. Keep the original
        # provider/configuration/security settings, but prevent old run
        # bookkeeping from treating this turn as a workflow continuation.
        return replace(
            previous,
            workflow_name=session.workflow_name,
            intent=session.intent,
            cwd=str(Path(session.cwd).expanduser().resolve()),
            run_id="",
            detached_goal=False,
            mode_name=mode_name,
        )

    def _start_deferred_input_attempt(
        self,
        session: BackgroundSession,
        *,
        message_id: str,
        expected_attempt: int,
        expected_lease_token: str,
    ) -> BackgroundSession:
        """Start one owner-fenced worker solely to process deferred input."""

        current = self.store.get(session.session_id, include_deleted=True)
        self._validate_input_target(current, expected_attempt, expected_lease_token)
        input_recoverable_statuses = {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.ORPHANED,
            SessionStatus.CANCELLED,
            SessionStatus.ARCHIVED,
        }
        if current.status not in input_recoverable_statuses:
            raise InvalidSessionTransition(
                f"Cannot start an input-driven attempt from {current.status.value}"
            )
        if not self.queue_enabled:
            if self._active_count() >= self.max_workers:
                raise RuntimeError(f"Background worker limit reached ({self.max_workers})")
            if self._active_count(current.cwd) >= self.max_workers_per_project:
                raise RuntimeError(
                    f"Background worker limit reached for project ({self.max_workers_per_project})"
                )

        request = self._input_reactivation_request(current)
        # Write the complete worker request before changing the lifecycle
        # record. If the process stops here, maintenance can retry from the
        # still-recoverable record and its already durable inbox item.
        self._write_request(request)
        BackgroundInputInbox(self.store).rearm_recovery_wait(
            current.session_id,
            expected_attempt=expected_attempt,
            expected_lease_token=expected_lease_token,
        )
        starting = self.store.transition(
            current.session_id,
            SessionStatus.STARTING,
            expected_status=current.status,
            expected_attempt=expected_attempt,
            expected_lease_token=expected_lease_token,
            resume_marker=f"input:{message_id}",
            requested_mode_name=request.mode_name,
            mode_name="",
            mode_application_status=ModeApplicationStatus.PENDING,
            mode_application_attempt=current.attempt + 1,
            mode_application_error="",
            worker_pid=None,
            worker_started_at=None,
            worker_finished_at=None,
            worker_exit_code=None,
            worker_exit_reason="",
            worker_finalization_attempts=0,
            worker_cleanup_error="",
            completed_at=None,
            error=None,
            failure_category="",
            cancellation_reason="",
            exit_reason="",
            latest_activity="Starting worker for accepted input",
        )
        if self.queue_enabled:
            return self.start(current.session_id)
        return self._launch(request, starting)

    def _defer_and_resume(
        self,
        inbox: BackgroundInputInbox,
        session_id: str,
        text: str,
        *,
        expected_attempt: int,
        expected_lease_token: str,
        message_id: str,
    ) -> BackgroundInput:
        receipt = inbox.enqueue_deferred(
            session_id,
            text,
            expected_attempt=expected_attempt,
            expected_lease_token=expected_lease_token,
            message_id=message_id,
        )
        if receipt.state != "accepted" or not receipt.deferred:
            # An idempotent retry of an already delivered or terminal receipt
            # must not create a worker that has no deferred user input (and
            # would otherwise fall back to the original request intent).
            return receipt
        try:
            resumed = self._start_deferred_input_attempt(
                self.store.get(session_id, include_deleted=True),
                message_id=message_id,
                expected_attempt=expected_attempt,
                expected_lease_token=expected_lease_token,
            )
        except InvalidSessionTransition:
            # A competing submitter or maintenance pass may have started the
            # replacement worker. The accepted inbox item remains authoritative.
            current = self.store.get(session_id, include_deleted=True)
            if current.status in ACTIVE_STATUSES:
                return receipt
            return replace(
                receipt,
                recovery_error="Session changed; accepted input needs recovery"[:240],
            )
        except Exception as exc:  # noqa: BLE001
            # Input is already durable. Do not report it as rejected and
            # invite a duplicate submission; expose a bounded recovery note.
            return replace(
                receipt,
                recovery_error=f"{type(exc).__name__}: background recovery did not start"[:240],
            )
        if resumed.status is SessionStatus.FAILED:
            return replace(
                receipt,
                recovery_error=(resumed.error or "Background recovery did not start")[:240],
            )
        return receipt

    def _defer_and_reactivate_completed(
        self,
        inbox: BackgroundInputInbox,
        session_id: str,
        text: str,
        *,
        expected_attempt: int,
        expected_lease_token: str,
        message_id: str,
    ) -> BackgroundInput:
        """Persist input, then start a fresh attempt for a completed session."""

        receipt = inbox.enqueue_deferred(
            session_id,
            text,
            expected_attempt=expected_attempt,
            expected_lease_token=expected_lease_token,
            message_id=message_id,
        )
        if receipt.state != "accepted" or not receipt.deferred:
            return receipt
        try:
            started = self._start_deferred_input_attempt(
                self.store.get(session_id, include_deleted=True),
                message_id=message_id,
                expected_attempt=expected_attempt,
                expected_lease_token=expected_lease_token,
            )
        except InvalidSessionTransition:
            # Another submitter or maintenance pass may already have won the
            # completed→starting compare-and-set. The message is durable and
            # the winning worker will consume the deferred inbox in FIFO order.
            current = self.store.get(session_id, include_deleted=True)
            if current.status in ACTIVE_STATUSES:
                return receipt
            return replace(
                receipt,
                recovery_error="Session changed; accepted input needs recovery"[:240],
            )
        except Exception as exc:  # noqa: BLE001
            # Acceptance preceded startup. Preserve the receipt and avoid
            # encouraging a duplicate user message after a launch failure.
            return replace(
                receipt,
                recovery_error=f"{type(exc).__name__}: background recovery did not start"[:240],
            )
        if started.status is SessionStatus.FAILED:
            return replace(
                receipt,
                recovery_error=(started.error or "Background recovery did not start")[:240],
            )
        return receipt

    @staticmethod
    def _validate_input_target(
        session: BackgroundSession,
        expected_attempt: int,
        expected_lease_token: str,
    ) -> None:
        if session.status is SessionStatus.DELETED:
            raise InvalidSessionTransition("Deleted sessions cannot receive input")
        if session.attempt != expected_attempt or session.lease_token != expected_lease_token:
            raise InvalidSessionTransition("Session changed; reopen details and retry")

    def cancel(self, session_id: str) -> BackgroundSession:
        with self._scheduler_lock():
            queued = self.store.get(session_id, include_deleted=True)
            if is_pending_queue_record(queued):
                # A pending record has no child process, model call, or worker
                # lease. Cancel it directly while holding the dispatcher lock
                # so it cannot launch concurrently with this transition.
                return self.store.transition(
                    session_id,
                    SessionStatus.CANCELLED,
                    expected_status=SessionStatus.QUEUED,
                    expected_attempt=queued.attempt,
                    dispatch_state=QueueDispatchState.CANCELLED,
                    cancellation_reason="user cancelled before worker start",
                    latest_activity="Cancelled while queued; worker was not started",
                )
        session = self.store.get(session_id)
        if session.status in {SessionStatus.CANCELLED, SessionStatus.ARCHIVED}:
            return session
        if session.status in {SessionStatus.COMPLETED, SessionStatus.FAILED}:
            return session
        if session.status != SessionStatus.CANCELLING:
            session = self.store.transition(
                session_id,
                SessionStatus.CANCELLING,
                cancellation_reason="user requested cancellation",
            )
        pid = session.worker_pid
        if pid is not None:
            self._terminate(pid)
            deadline = time.monotonic() + self.cancel_grace_s
            while time.monotonic() < deadline and self._alive(pid):
                time.sleep(0.05)
        # Detached terminal tools use their own process groups.  The worker's
        # process-group signal cannot reach those child groups, so explicitly
        # clean up only records linked to this exact parent session.
        from .terminals import stop_persisted_session_terminals  # noqa: PLC0415

        stop_persisted_session_terminals(
            session_id,
            store_root=self.store.root / "terminals",
            grace_s=self.cancel_grace_s,
        )
        current = self.store.get(session_id, include_deleted=True)
        if current.status == SessionStatus.CANCELLING:
            cancelled = self.store.transition(
                session_id,
                SessionStatus.CANCELLED,
                latest_activity="Cancelled",
                **self._cancelled_metadata(current),
            )
            if current.detached_goal:
                self.store.record_worker_exit(
                    session_id,
                    worker_pid=cancelled.worker_pid,
                    exit_reason="cancelled",
                    exit_code=130,
                )
            return cancelled
        return current

    def set_priority(
        self,
        session_id: str,
        priority: QueuePriority | str,
        *,
        changed_by: str = "local-user",
    ) -> BackgroundSession:
        """Explicitly reprioritize a queued or running job and audit the change."""

        normalized = parse_priority(priority)
        with self._scheduler_lock():
            updated = self.store.change_priority(
                session_id,
                normalized,
                changed_by=changed_by,
            )
            if is_pending_queue_record(updated):
                self._dispatch_pending_locked()
                return self.store.get(session_id, include_deleted=True)
            return updated

    def _alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        # ``kill(pid, 0)`` also succeeds for a zombie until its parent reaps
        # it.  A zombie cannot execute tools, so treating it as alive would
        # unnecessarily block a safe foreground handoff (and can leave the
        # lifecycle stuck in ``cancelling`` in short-lived worker tests).
        if os.name != "nt":
            try:
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
                end_comm = stat.rfind(")")
                fields = stat[end_comm + 2 :].split()
                if fields and fields[0] == "Z":
                    return False
            except (OSError, UnicodeError):
                pass
        return True

    def _worker_process_matches(self, session: BackgroundSession) -> bool:
        """Check that a live PID still belongs to this exact worker.

        ``kill(pid, 0)`` only proves that *some* process owns the number. A
        reused PID must never be treated as proof that the original worker is
        alive. Procfs gives us a safe, bounded identity check on POSIX; when
        the operating system cannot expose a checkable command line, recovery
        fails closed and marks the session for reconciliation.
        """

        return self._worker_process_state(session) is _WorkerProcessState.LIVE

    def _worker_process_state(self, session: BackgroundSession) -> _WorkerProcessState:
        """Distinguish verified liveness from death and uninspectable PIDs."""

        pid = session.worker_pid
        if pid is None:
            return _WorkerProcessState.UNKNOWN
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return _WorkerProcessState.DEAD
        except PermissionError:
            return _WorkerProcessState.UNKNOWN
        except OSError as exc:
            return (
                _WorkerProcessState.DEAD
                if exc.errno == errno.ESRCH
                else _WorkerProcessState.UNKNOWN
            )
        if os.name == "nt":
            # PID existence alone does not prove this is the worker for this
            # session. Keep it unknown until a platform identity adapter exists.
            return _WorkerProcessState.UNKNOWN
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            end_comm = stat.rfind(")")
            fields = stat[end_comm + 2 :].split()
            if fields and fields[0] == "Z":
                return _WorkerProcessState.DEAD
        except FileNotFoundError:
            return _WorkerProcessState.DEAD
        except (OSError, UnicodeError):
            return _WorkerProcessState.UNKNOWN
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except FileNotFoundError:
            return _WorkerProcessState.DEAD
        except OSError:
            return _WorkerProcessState.UNKNOWN
        arguments = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if not any(
            argument in {"agenthicc.background.worker", "agenthicc/background/worker.py"}
            or argument.endswith("/agenthicc/background/worker.py")
            for argument in arguments
        ):
            return _WorkerProcessState.MISMATCHED

        def _argument_after(flag: str) -> str | None:
            try:
                return arguments[arguments.index(flag) + 1]
            except (ValueError, IndexError):
                return None

        request_argument = _argument_after("--request-file")
        store_argument = _argument_after("--store-root")
        if request_argument is None or store_argument is None:
            return _WorkerProcessState.MISMATCHED
        request_path = Path(request_argument)
        store_path = Path(store_argument)
        if not request_path.is_absolute():
            request_path = Path(session.cwd) / request_path
        if not store_path.is_absolute():
            store_path = Path(session.cwd) / store_path
        try:
            matches = (
                request_path.resolve() == self._request_path(session.session_id).resolve()
                and store_path.resolve() == self.store.root.resolve()
            )
        except OSError:
            return _WorkerProcessState.UNKNOWN
        return _WorkerProcessState.LIVE if matches else _WorkerProcessState.MISMATCHED

    @staticmethod
    def _cancelled_metadata(session: BackgroundSession) -> _CancellationMetadata:
        """Build cancellation metadata without erasing a detached PID."""

        if not session.detached_goal:
            return {"worker_pid": None, "lease_token": ""}
        return {
            "worker_finished_at": time.time(),
            "worker_exit_code": 130,
            "worker_exit_reason": "cancelled",
            "exit_reason": "cancelled",
            "worker_finalization_attempts": max(1, session.worker_finalization_attempts),
            "lease_token": "",
        }

    def _terminate(self, pid: int) -> None:
        try:
            if os.name != "nt":
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return

    def archive(self, session_id: str) -> BackgroundSession:
        return self.store.archive(session_id)

    def restore_archive(self, session_id: str) -> BackgroundSession:
        return self.store.restore_archive(session_id)

    def approve(
        self,
        session_id: str,
        allowed: bool,
        *,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> BackgroundSession:
        """Resolve a persisted approval request without bypassing policy."""

        session = self.store.get(session_id)
        if session.status != SessionStatus.WAITING_APPROVAL:
            raise InvalidSessionTransition("Session is not waiting for approval")
        if session.approval_item_id:
            from agenthicc.background.inbox import BackgroundInteractionInbox  # noqa: PLC0415

            inbox = BackgroundInteractionInbox(self.store)
            item = inbox.get(session.approval_item_id)
            response: dict[str, object] = {"allowed": allowed}
            if allowed and item.target.get("workspace_access"):
                response["scope_grant"] = "target_once"
            inbox.respond(
                item.item_id,
                response,
                expected_version=expected_version if expected_version is not None else item.version,
                idempotency_key=idempotency_key or uuid.uuid4().hex,
            )
            return self.store.get(session_id)
        return self.store.update(
            session_id,
            expected_status=SessionStatus.WAITING_APPROVAL,
            approval_decision=allowed,
            latest_activity="Approval granted" if allowed else "Approval denied",
        )

    def delete(self, session_id: str, *, operation_id: str = "") -> BackgroundSession:
        if operation_id:
            self.store.mark_delete_requested(
                session_id,
                operation_id=operation_id,
                requested_by="sync",
            )
        session = self.store.get(session_id, include_deleted=True)
        if session.status == SessionStatus.DELETED:
            return session
        if session.status in ACTIVE_STATUSES:
            self.cancel(session_id)
        return self.store.delete(session_id, force=True, operation_id=operation_id)

    async def delete_async(
        self,
        session_ids: Sequence[str],
        *,
        operation_id: str,
        requested_by: str = "agents",
        progress: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> DeleteResult:
        """Delete exact targets without blocking the caller's event loop.

        The existing synchronous lifecycle remains available to CLI/plugin
        callers.  Interactive TUI code uses this method so cancellation waits,
        artifact movement, and JSONL/fsync work are isolated behind the shared
        async boundary.  Targets are processed serially: this preserves the
        store's append ordering and makes partial success/retry deterministic.
        """

        target_ids = tuple(dict.fromkeys(item for item in session_ids if item))
        if not target_ids:
            raise ValueError("at least one session ID is required")
        if not operation_id or not operation_id.replace("-", "").isalnum():
            raise ValueError("operation_id must be a non-empty identifier")

        deleted: list[str] = []
        failures: list[DeleteFailure] = []

        # Validate every target before changing any target. This prevents a
        # typo or stale bulk-selection ID from producing a surprising partial
        # deletion. State is fetched again when each target is claimed so a
        # concurrent lifecycle change is still handled safely.
        validated: dict[str, BackgroundSession] = {}
        for session_id in target_ids:
            try:
                validated[session_id] = await asyncio.to_thread(
                    self.store.get, session_id, include_deleted=True
                )
            except SessionNotFound as exc:
                failures.append(
                    DeleteFailure(
                        session_id=session_id,
                        code="target_not_found",
                        message=f"Session not found: {exc}",
                        retryable=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    DeleteFailure(
                        session_id=session_id,
                        code="target_validation_failed",
                        message=f"{type(exc).__name__}: {exc}"[:2_000],
                    )
                )
        if failures:
            return DeleteResult(operation_id=operation_id, failures=tuple(failures))

        async def report(session_id: str, phase: str) -> None:
            if progress is None:
                return
            try:
                await progress(session_id, phase)
            except Exception:
                # Progress is advisory. A UI disconnect must never turn a
                # durable deletion into a false failure.
                return

        for session_id in target_ids:
            try:
                current = validated[session_id]
                if current.status == SessionStatus.DELETED:
                    deleted.append(session_id)
                    await report(session_id, "completed")
                    continue

                claimed = await asyncio.to_thread(
                    self.store.mark_delete_requested,
                    session_id,
                    operation_id=operation_id,
                    requested_by=requested_by,
                )
                await report(session_id, "requested")

                if claimed.status in ACTIVE_STATUSES:
                    await asyncio.to_thread(
                        self.store.mark_delete_progress,
                        session_id,
                        operation_id=operation_id,
                        phase="stopping_worker",
                        activity="Stopping worker before deletion",
                    )
                    await report(session_id, "stopping_worker")
                    # cancel() includes its own bounded grace period and
                    # terminal cleanup. It runs off-loop until the fully
                    # observed lifecycle state is available.
                    await asyncio.to_thread(self.cancel, session_id)

                await asyncio.to_thread(
                    self.store.mark_delete_progress,
                    session_id,
                    operation_id=operation_id,
                    phase="moving_artifacts",
                    activity="Moving artifacts to recoverable trash",
                )
                await report(session_id, "moving_artifacts")
                await asyncio.to_thread(
                    self.store.delete,
                    session_id,
                    force=True,
                    operation_id=operation_id,
                )
                deleted.append(session_id)
                await report(session_id, "completed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                retryable = not isinstance(exc, (ValueError, InvalidSessionTransition))
                message = f"{type(exc).__name__}: {exc}"[:2_000]
                try:
                    await asyncio.to_thread(
                        self.store.mark_delete_failed,
                        session_id,
                        operation_id=operation_id,
                        error=message,
                        retryable=retryable,
                    )
                except Exception:
                    # Keep the original failure. The next manager open will
                    # reconcile the exact target from the artifact/store state.
                    pass
                failures.append(
                    DeleteFailure(
                        session_id=session_id,
                        code="invalid_transition" if not retryable else "delete_failed",
                        message=message,
                        retryable=retryable,
                    )
                )
                await report(session_id, "failed")

        return DeleteResult(
            operation_id=operation_id, deleted=tuple(deleted), failures=tuple(failures)
        )

    def restore_deleted(self, session_id: str) -> BackgroundSession:
        return self.store.restore_deleted(session_id)

    def _recover_stale_records(
        self,
        sessions: tuple[BackgroundSession, ...] | list[BackgroundSession],
        stale_after_s: float,
    ) -> list[BackgroundSession]:
        now = time.time()
        changed: list[BackgroundSession] = []
        for session in sessions:
            if session.status not in ACTIVE_STATUSES:
                continue
            if is_pending_queue_record(session):
                # A durable queue reservation has no worker identity yet; it
                # is not a dead process and must remain eligible for dispatch.
                continue
            if (
                session.status is SessionStatus.STARTING
                and session.dispatch_state is QueueDispatchState.CLAIMED
                and session.worker_pid is None
            ):
                # The scheduler lease, not heartbeat age, owns recovery of
                # the narrow reservation-before-Popen crash window.
                continue
            heartbeat_expired = bool(
                session.last_active and now - session.last_active > stale_after_s
            )
            process_state = self._worker_process_state(session)
            if process_state in {_WorkerProcessState.DEAD, _WorkerProcessState.MISMATCHED}:
                try:
                    changed.append(
                        self.store.mark_orphaned(
                            session.session_id,
                            expected_attempt=session.attempt,
                            expected_lease_token=session.lease_token or None,
                            expected_status=session.status,
                            expected_last_active=session.last_active,
                        )
                    )
                except InvalidSessionTransition:
                    # A heartbeat or a new attempt won the race with process
                    # inspection. Its newer state is reconciled next pass.
                    continue
                continue

            if heartbeat_expired:
                if session.worker_pid is None and session.status in {
                    SessionStatus.QUEUED,
                    SessionStatus.STARTING,
                    SessionStatus.RETRYING,
                }:
                    try:
                        changed.append(
                            self.store.mark_orphaned(
                                session.session_id,
                                expected_attempt=session.attempt,
                                expected_status=session.status,
                                expected_last_active=session.last_active,
                            )
                        )
                    except InvalidSessionTransition:
                        continue
                    continue
                if not session.heartbeat_stale:
                    try:
                        changed.append(
                            self.store.update(
                                session.session_id,
                                expected_attempt=session.attempt,
                                expected_lease_token=session.lease_token or None,
                                expected_status=session.status,
                                expected_last_active=session.last_active,
                                heartbeat_stale=True,
                            )
                        )
                    except InvalidSessionTransition:
                        continue
                # A verified live worker is not orphaned for a delayed
                # heartbeat. Unknown identity is also insufficient evidence
                # for a terminal transition; retain the active record and
                # surface the stale-heartbeat health flag instead.
                continue

            if session.heartbeat_stale:
                try:
                    changed.append(
                        self.store.update(
                            session.session_id,
                            expected_attempt=session.attempt,
                            expected_lease_token=session.lease_token or None,
                            expected_status=session.status,
                            expected_last_active=session.last_active,
                            heartbeat_stale=False,
                        )
                    )
                except InvalidSessionTransition:
                    continue
        return changed

    def _recover_pending_input_records(
        self,
        sessions: tuple[BackgroundSession, ...] | list[BackgroundSession],
    ) -> list[BackgroundSession]:
        """Restart only sessions with an accepted, unconsumed deferred input."""

        changed: dict[str, BackgroundSession] = {}
        inbox = BackgroundInputInbox(self.store)
        for snapshot in sessions:
            try:
                current = self.store.get(snapshot.session_id, include_deleted=True)
            except SessionNotFound:
                continue
            legacy_workflow_recovery_wait = (
                current.status in {SessionStatus.FAILED, SessionStatus.ORPHANED}
                and current.worker_exit_reason == "workflow_recovery_waiting"
                and not current.resume_marker.startswith("input:")
            )
            if current.status is SessionStatus.COMPLETED:
                input_start_recovery = True
            elif current.status in {SessionStatus.FAILED, SessionStatus.ORPHANED}:
                # Retry a worker startup interrupted after input acceptance.
                # A legacy workflow-recovery wait gets one startup retry after
                # upgrade. The input marker plus receipt error then prevents
                # maintenance from repeatedly launching an unresolved worker.
                input_start_recovery = (
                    current.resume_marker.startswith("input:") or legacy_workflow_recovery_wait
                )
            else:
                input_start_recovery = False
            if not input_start_recovery or current.cancellation_reason:
                continue
            pending = tuple(
                item
                for item in inbox.receipts(current.session_id)
                if item.state == "accepted"
                and item.deferred
                and (legacy_workflow_recovery_wait or not item.recovery_error)
            )
            if not pending:
                continue
            if (
                self._active_count() >= self.max_workers
                or self._active_count(current.cwd) >= self.max_workers_per_project
            ):
                continue
            try:
                self._start_deferred_input_attempt(
                    current,
                    message_id=pending[0].message_id,
                    expected_attempt=current.attempt,
                    expected_lease_token=current.lease_token,
                )
            except (InvalidSessionTransition, OSError, RuntimeError, ValueError):
                # The inbox is the durable record. A later maintenance pass
                # retries after a competing owner, capacity, or launch issue.
                continue
            try:
                updated = self.store.get(current.session_id, include_deleted=True)
            except SessionNotFound:
                continue
            if (
                updated.status != current.status
                or updated.attempt != current.attempt
                or updated.worker_pid != current.worker_pid
            ):
                changed[updated.session_id] = updated
        return list(changed.values())

    def recover_stale(self, *, stale_after_s: float = 30.0) -> list[BackgroundSession]:
        """Reconcile stale workers and resume accepted deferred input records."""

        sessions = self.store.list(include_archived=True)
        changed = self._recover_stale_records(sessions, stale_after_s)
        changed.extend(self._recover_pending_input_records(sessions))
        changed.extend(self.dispatch_pending())
        return list({item.session_id: item for item in changed}.values())

    def recover_stale_batch(
        self, *, stale_after_s: float = 30.0, max_sessions: int = 1_024
    ) -> list[BackgroundSession]:
        """Inspect one bounded stable-ID slice for periodic TUI maintenance."""

        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        sessions, self._recovery_cursor = self.store._lifecycle_page_after(
            self._recovery_cursor, max_sessions
        )
        changed = self._recover_stale_records(sessions, stale_after_s)
        changed.extend(self._recover_pending_input_records(sessions))
        changed.extend(self.dispatch_pending())
        return list({item.session_id: item for item in changed}.values())

    def purge_expired_trash(self) -> list[str]:
        """Apply the configured recoverable-trash retention policy."""

        return self.store.purge_trash(older_than_s=self.trash_retention_days * 86_400.0)
