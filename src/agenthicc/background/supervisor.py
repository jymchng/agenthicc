"""Local worker supervisor for durable background sessions (PRD-141)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from .model import ACTIVE_STATUSES, BackgroundSession, SessionStatus
from .store import BackgroundStore, InvalidSessionTransition, default_artifact_dir


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

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "workflow_name": self.workflow_name,
            "intent": self.intent,
            "cwd": self.cwd,
            "config_path": self.config_path,
            "set_overrides": list(self.set_overrides),
            "dangerously_skip_permissions": self.dangerously_skip_permissions,
            "wall_timeout_s": self.wall_timeout_s,
            "max_activity_bytes": self.max_activity_bytes,
            "source": self.source,
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
        path = self.store.root / "requests" / f"{session_id}.json"
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
            str(self.store.root),
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
            wall_timeout_s=self.wall_timeout_s,
            max_activity_bytes=self.max_activity_bytes,
            source="cli",
        )
        session = BackgroundSession.create(
            sid,
            title=title or cleaned_intent[:80],
            cwd=project,
            workflow_name=workflow_name,
            intent=cleaned_intent,
            artifact_dir=str(self.artifact_root / sid),
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
        return self._launch(request, existing)

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
        current = self.store.get(session_id, include_deleted=True)
        if current.status == SessionStatus.CANCELLING:
            return self.store.transition(
                session_id, SessionStatus.CANCELLED, latest_activity="Cancelled"
            )
        return current

    def _alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

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

    def delete(self, session_id: str) -> BackgroundSession:
        session = self.store.get(session_id)
        if session.status in ACTIVE_STATUSES:
            self.cancel(session_id)
        return self.store.delete(session_id, force=True)

    def restore_deleted(self, session_id: str) -> BackgroundSession:
        return self.store.restore_deleted(session_id)

    def recover_stale(self, *, stale_after_s: float = 30.0) -> list[BackgroundSession]:
        now = time.time()
        changed: list[BackgroundSession] = []
        for session in self.store.list(include_archived=False):
            if session.status not in ACTIVE_STATUSES:
                continue
            worker_missing = session.worker_pid is not None and not self._alive(session.worker_pid)
            lease_expired = session.last_active and now - session.last_active > stale_after_s
            if worker_missing or lease_expired:
                changed.append(self.store.mark_orphaned(session.session_id))
        return changed

    def purge_expired_trash(self) -> list[str]:
        """Apply the configured recoverable-trash retention policy."""

        return self.store.purge_trash(older_than_s=self.trash_retention_days * 86_400.0)
