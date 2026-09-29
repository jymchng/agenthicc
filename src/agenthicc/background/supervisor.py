"""Local worker supervisor for durable background sessions (PRD-141)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable, Sequence
from typing import TypedDict
from .deletion import DeleteFailure, DeleteResult
from .model import ACTIVE_STATUSES, BackgroundSession, SessionStatus
from .store import (
    BackgroundStore,
    InvalidSessionTransition,
    SessionNotFound,
    default_artifact_dir,
)


class _CancellationMetadata(TypedDict, total=False):
    """Typed terminal fields accepted by ``BackgroundStore.transition``."""

    worker_pid: int | None
    worker_finished_at: float
    worker_exit_code: int
    worker_exit_reason: str
    exit_reason: str
    worker_finalization_attempts: int
    lease_token: str


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
        }


class BackgroundSupervisor:
    """Own worker processes and expose idempotent lifecycle operations."""

    def __init__(
        self,
        store: BackgroundStore | None = None,
        *,
        max_workers: int = 2,
        max_workers_per_project: int = 2,
        cancel_grace_s: float = 5.0,
        wall_timeout_s: float = 0.0,
        max_activity_bytes: int = 64_000,
        artifact_root: Path | None = None,
        trash_retention_days: int = 30,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.store = store or BackgroundStore()
        self.max_workers = max_workers
        if max_workers_per_project < 1:
            raise ValueError("max_workers_per_project must be at least 1")
        self.max_workers_per_project = max_workers_per_project
        self.cancel_grace_s = max(0.1, cancel_grace_s)
        self.wall_timeout_s = max(0.0, wall_timeout_s)
        self.max_activity_bytes = max(1, max_activity_bytes)
        if trash_retention_days < 0:
            raise ValueError("trash_retention_days must be non-negative")
        self.trash_retention_days = trash_retention_days
        self.artifact_root = (artifact_root or Path.home() / ".agenthicc" / "sessions").expanduser()

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
                worker_exit_reason="launch_failed",
                exit_reason="Worker launch failed",
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
            return self.store.update(
                session.session_id,
                worker_pid=process.pid,
                worker_started_at=time.time(),
                latest_activity="Worker queued",
            )
        return current

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
    ) -> BackgroundSession:
        """Create and launch a new background session."""

        cleaned_intent = intent.strip()
        if not cleaned_intent:
            raise ValueError("Background intent must not be empty")
        if self._active_count() >= self.max_workers:
            raise RuntimeError(f"Background worker limit reached ({self.max_workers})")
        sid = session_id or uuid.uuid4().hex
        project = str(Path(cwd or os.getcwd()).resolve())
        if self._active_count(project) >= self.max_workers_per_project:
            raise RuntimeError(
                f"Background worker limit reached for project ({self.max_workers_per_project})"
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
        )
        self.store.create(session)
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
            )
            self.store.create(existing)
        elif existing.status in ACTIVE_STATUSES:
            raise InvalidSessionTransition("Session is already managed by a background worker")
        request = BackgroundRequest(
            session_id=session_id,
            workflow_name=workflow_name or existing.workflow_name,
            intent=intent or existing.intent,
            cwd=str(Path(cwd or existing.cwd).resolve()),
            config_path=config_path,
            set_overrides=set_overrides,
            dangerously_skip_permissions=dangerously_skip_permissions,
            set_secret_overrides=set_secret_overrides,
            run_id=run_id,
            wall_timeout_s=self.wall_timeout_s,
            max_activity_bytes=self.max_activity_bytes,
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
        if not start:
            # Persist the request before a caller releases a foreground owner.
            # ``start`` then launches this exact request after the owner handoff
            # has completed, eliminating a foreground/background write race.
            self._write_request(request)
            return existing
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
        return self._launch(request, session)

    def resume(self, session_id: str) -> BackgroundSession:
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
        )

    def retry(self, session_id: str) -> BackgroundSession:
        return self.resume(session_id)

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

    def provide_input(self, session_id: str, value: str) -> BackgroundSession:
        """Deliver explicit user input to a session paused at ``waiting_input``."""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("Input must not be empty")
        session = self.store.get(session_id)
        if session.status != SessionStatus.WAITING_INPUT:
            raise InvalidSessionTransition("Session is not waiting for input")
        return self.store.update(
            session_id,
            expected_status=SessionStatus.WAITING_INPUT,
            input_value=value[:8_000],
            latest_activity="Input received",
        )

    def cancel(self, session_id: str) -> BackgroundSession:
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

        pid = session.worker_pid
        if pid is None or not self._alive(pid) or os.name == "nt":
            return False
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        arguments = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if not any(
            argument in {"agenthicc.background.worker", "agenthicc/background/worker.py"}
            or argument.endswith("/agenthicc/background/worker.py")
            for argument in arguments
        ):
            return False

        def _argument_after(flag: str) -> str | None:
            try:
                return arguments[arguments.index(flag) + 1]
            except (ValueError, IndexError):
                return None

        request_argument = _argument_after("--request-file")
        store_argument = _argument_after("--store-root")
        if request_argument is None or store_argument is None:
            return False
        request_path = Path(request_argument)
        store_path = Path(store_argument)
        if not request_path.is_absolute():
            request_path = Path(session.cwd) / request_path
        if not store_path.is_absolute():
            store_path = Path(session.cwd) / store_path
        try:
            return (
                request_path.resolve() == self._request_path(session.session_id).resolve()
                and store_path.resolve() == self.store.root.resolve()
            )
        except OSError:
            return False

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

    def approve(self, session_id: str, allowed: bool) -> BackgroundSession:
        """Resolve a persisted approval request without bypassing policy."""

        session = self.store.get(session_id)
        if session.status != SessionStatus.WAITING_APPROVAL:
            raise InvalidSessionTransition("Session is not waiting for approval")
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

    def recover_stale(self, *, stale_after_s: float = 30.0) -> list[BackgroundSession]:
        now = time.time()
        changed: list[BackgroundSession] = []
        for session in self.store.list(include_archived=False):
            if session.status not in ACTIVE_STATUSES:
                continue
            worker_missing = session.worker_pid is not None and not self._worker_process_matches(
                session
            )
            lease_expired = session.last_active and now - session.last_active > stale_after_s
            if worker_missing or lease_expired:
                changed.append(self.store.mark_orphaned(session.session_id))
        return changed

    def purge_expired_trash(self) -> list[str]:
        """Apply the configured recoverable-trash retention policy."""

        return self.store.purge_trash(older_than_s=self.trash_retention_days * 86_400.0)
