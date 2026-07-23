"""Append-only background-session lifecycle storage (PRD-141)."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Mapping

from .model import ACTIVE_STATUSES, BackgroundSession, SessionStatus, legal_transition


class SessionNotFound(KeyError):
    """Raised when a requested background session is not in the registry."""


class InvalidSessionTransition(ValueError):
    """Raised when a lifecycle operation would violate the state contract."""


def default_background_root() -> Path:
    return Path.home() / ".agenthicc" / "background"


def default_artifact_dir(session_id: str) -> Path:
    return Path.home() / ".agenthicc" / "sessions" / session_id


def _json_object(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


class BackgroundStore:
    """Durable registry whose authoritative history is ``events.jsonl``.

    The JSONL history is intentionally simple and rebuildable.  Each event is
    appended with ``O_APPEND`` and fsync'd; the in-memory records are folded
    from that history whenever the store is read.  A deleted session receives
    a tombstone so a stale worker or a rebuilt index cannot resurrect it.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or default_background_root()).expanduser()
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "registry.lock"
        self.trash_root = self.root / "trash"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

    def _read_events(self) -> list[dict[str, object]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, object]] = []
        try:
            with self.events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw, dict):
                        events.append({str(key): item for key, item in raw.items()})
        except OSError:
            return []
        return events

    def _append(self, event_type: str, payload: Mapping[str, object]) -> None:
        events = self._read_events()
        sequence_numbers: list[int] = []
        for event in events:
            sequence = event.get("seq")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                sequence_numbers.append(sequence)
        seq = max(sequence_numbers, default=0) + 1
        record = {
            "seq": seq,
            "event_type": event_type,
            "timestamp": time.time(),
            "payload": dict(payload),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _fold(self) -> dict[str, BackgroundSession]:
        records: dict[str, BackgroundSession] = {}
        for event in self._read_events():
            payload = _json_object(event.get("payload"))
            session_id = payload.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            event_type = event.get("event_type")
            if event_type == "created":
                try:
                    records[session_id] = BackgroundSession.from_mapping(payload)
                except (TypeError, ValueError):
                    continue
                continue
            current = records.get(session_id)
            if current is None:
                continue
            if event_type == "updated":
                changes = _json_object(payload.get("changes"))
                try:
                    records[session_id] = current.evolve(**changes)
                except (TypeError, ValueError):
                    continue
            elif event_type == "deleted":
                event_timestamp = event.get("timestamp")
                timestamp = (
                    float(event_timestamp)
                    if isinstance(event_timestamp, (int, float))
                    and not isinstance(event_timestamp, bool)
                    else time.time()
                )
                records[session_id] = current.evolve(
                    status=SessionStatus.DELETED,
                    trash_dir=str(payload.get("trash_dir", current.trash_dir)),
                    artifact_dir="",
                    last_active=timestamp,
                )
        return records

    def get(self, session_id: str, *, include_deleted: bool = False) -> BackgroundSession:
        record = self._fold().get(session_id)
        if record is None or (record.status == SessionStatus.DELETED and not include_deleted):
            raise SessionNotFound(session_id)
        return record

    def list(
        self,
        *,
        include_archived: bool = True,
        include_deleted: bool = False,
        cwd: str | None = None,
        workflow_name: str | None = None,
        query: str = "",
        status: SessionStatus | None = None,
    ) -> list[BackgroundSession]:
        query_lower = query.strip().lower()
        records = []
        for session in self._fold().values():
            if session.status == SessionStatus.DELETED and not include_deleted:
                continue
            if not include_archived and session.status == SessionStatus.ARCHIVED:
                continue
            if cwd is not None and session.cwd != cwd:
                continue
            if workflow_name is not None and session.workflow_name != workflow_name:
                continue
            if status is not None and session.status != status:
                continue
            haystack = " ".join(
                (session.session_id, session.title, session.cwd, session.workflow_name)
            ).lower()
            if query_lower and query_lower not in haystack:
                continue
            records.append(session)
        return sorted(
            records,
            key=lambda item: (not item.pinned, -item.last_active, item.session_id),
        )

    def create(self, session: BackgroundSession) -> BackgroundSession:
        with self._lock():
            existing = self._fold().get(session.session_id)
            if existing is not None and existing.status != SessionStatus.DELETED:
                raise ValueError(f"Background session already exists: {session.session_id}")
            self._append("created", session.to_dict())
        return session

    def update(
        self,
        session_id: str,
        *,
        expected_status: SessionStatus | None = None,
        expected_lease_token: str | None = None,
        **changes: object,
    ) -> BackgroundSession:
        with self._lock():
            current = self.get(session_id, include_deleted=True)
            if expected_status is not None and current.status != expected_status:
                raise InvalidSessionTransition(
                    f"Expected {expected_status.value}, found {current.status.value}"
                )
            if expected_lease_token is not None and current.lease_token != expected_lease_token:
                raise InvalidSessionTransition("Background worker lease is stale")
            allowed = set(BackgroundSession.__dataclass_fields__) - {"session_id"}
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"Unknown background session fields: {sorted(unknown)}")
            if "status" in changes and changes["status"] != current.status:
                changes.setdefault("state_changed_at", time.time())
            if "latest_activity" in changes and isinstance(changes["latest_activity"], str):
                changes["latest_activity"] = changes["latest_activity"][:512]
            if "error" in changes and isinstance(changes["error"], str):
                changes["error"] = changes["error"][:2_000]
            changes.setdefault("last_active", time.time())
            updated = current.evolve(**changes)
            serialized_changes = updated.to_dict()
            serialized_changes.pop("session_id", None)
            self._append(
                "updated",
                {"session_id": session_id, "changes": serialized_changes},
            )
            return updated

    def transition(
        self,
        session_id: str,
        target: SessionStatus,
        *,
        expected_status: SessionStatus | None = None,
        expected_lease_token: str | None = None,
        **changes: object,
    ) -> BackgroundSession:
        current = self.get(session_id, include_deleted=True)
        if current.status == target:
            return self.update(
                session_id,
                expected_status=expected_status,
                expected_lease_token=expected_lease_token,
                **changes,
            )
        if current.status == SessionStatus.DELETED:
            raise InvalidSessionTransition("Deleted background sessions cannot change state")
        if not legal_transition(current.status, target):
            raise InvalidSessionTransition(
                f"Cannot transition {current.status.value} → {target.value}"
            )
        changes["status"] = target
        if target in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}:
            changes.setdefault("completed_at", time.time())
        return self.update(
            session_id,
            expected_status=expected_status,
            expected_lease_token=expected_lease_token,
            **changes,
        )

    def claim(self, session_id: str, *, pid: int, lease_token: str) -> BackgroundSession:
        current = self.get(session_id)
        if current.status == SessionStatus.QUEUED:
            current = self.transition(session_id, SessionStatus.STARTING)
        elif current.status == SessionStatus.RETRYING:
            current = self.transition(session_id, SessionStatus.STARTING)
        elif current.status not in {SessionStatus.STARTING, SessionStatus.ORPHANED}:
            raise InvalidSessionTransition(f"Cannot claim {current.status.value} session")
        return self.transition(
            session_id,
            SessionStatus.RUNNING,
            expected_status=current.status,
            worker_pid=pid,
            lease_token=lease_token,
            attempt=current.attempt + 1,
            started_at=current.started_at or time.time(),
            latest_activity="Worker started",
        )

    def heartbeat(
        self, session_id: str, *, lease_token: str, phase: str = "", activity: str = ""
    ) -> BackgroundSession:
        current = self.get(session_id)
        return self.update(
            session_id,
            expected_status=current.status,
            expected_lease_token=lease_token,
            current_phase=phase or current.current_phase,
            latest_activity=activity or current.latest_activity,
            last_active=time.time(),
        )

    def mark_orphaned(self, session_id: str) -> BackgroundSession:
        current = self.get(session_id)
        if current.status in ACTIVE_STATUSES and current.status != SessionStatus.CANCELLING:
            return self.transition(
                session_id, SessionStatus.ORPHANED, latest_activity="Worker disappeared"
            )
        if current.status == SessionStatus.CANCELLING:
            return self.transition(
                session_id, SessionStatus.ORPHANED, latest_activity="Cancellation cleanup expired"
            )
        return current

    def archive(self, session_id: str) -> BackgroundSession:
        return self.transition(session_id, SessionStatus.ARCHIVED)

    def rename(self, session_id: str, title: str) -> BackgroundSession:
        """Persist a user-facing title without changing execution metadata."""

        cleaned = " ".join(title.split()).strip()
        if not cleaned:
            raise ValueError("Session title must not be empty")
        return self.update(session_id, title=cleaned[:160])

    def set_labels(self, session_id: str, labels: tuple[str, ...]) -> BackgroundSession:
        """Replace normalized, bounded user labels for a session."""

        normalized: list[str] = []
        for label in labels:
            cleaned = " ".join(label.split()).strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned[:48])
        return self.update(session_id, labels=tuple(normalized[:16]))

    def restore_archive(self, session_id: str) -> BackgroundSession:
        current = self.get(session_id)
        if current.status != SessionStatus.ARCHIVED:
            raise InvalidSessionTransition("Only archived sessions can be restored")
        return self.update(session_id, status=SessionStatus.COMPLETED, latest_activity="Restored")

    def _move_artifacts_to_trash(self, session: BackgroundSession) -> Path:
        source = Path(session.artifact_dir).expanduser() if session.artifact_dir else None
        trash = self.trash_root / f"{session.session_id}-{uuid.uuid4().hex[:10]}"
        trash.mkdir(parents=True, exist_ok=False)
        manifest = {
            "session_id": session.session_id,
            "original_artifact_dir": str(source) if source is not None else "",
        }
        if source is not None and source.name == session.session_id and source.exists():
            shutil.move(str(source), str(trash / "session"))
            kernel = source.parent / f"{session.session_id}.jsonl"
            if kernel.exists() and kernel.is_file():
                shutil.move(str(kernel), str(trash / "kernel.jsonl"))
        (trash / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return trash

    def delete(self, session_id: str, *, force: bool = False) -> BackgroundSession:
        with self._lock():
            current = self.get(session_id)
            if current.status in ACTIVE_STATUSES and not force:
                raise InvalidSessionTransition("Cancel the active session before deleting it")
            trash = self._move_artifacts_to_trash(current)
            self._append(
                "deleted",
                {"session_id": session_id, "trash_dir": str(trash)},
            )
            return current.evolve(
                status=SessionStatus.DELETED, artifact_dir="", trash_dir=str(trash)
            )

    def restore_deleted(self, session_id: str) -> BackgroundSession:
        with self._lock():
            current = self.get(session_id, include_deleted=True)
            if current.status != SessionStatus.DELETED or not current.trash_dir:
                raise InvalidSessionTransition("Session is not in recoverable trash")
            trash = Path(current.trash_dir)
            manifest = _json_object(
                json.loads((trash / "manifest.json").read_text(encoding="utf-8"))
            )
            original_text = str(manifest.get("original_artifact_dir", ""))
            original = Path(original_text).expanduser() if original_text else Path()
            if original_text:
                original.parent.mkdir(parents=True, exist_ok=True)
                moved = trash / "session"
                if moved.exists():
                    if original.exists():
                        raise InvalidSessionTransition("Original session directory already exists")
                    shutil.move(str(moved), str(original))
                kernel = trash / "kernel.jsonl"
                if kernel.exists():
                    shutil.move(str(kernel), str(original.parent / f"{session_id}.jsonl"))
            restored = current.evolve(
                status=SessionStatus.COMPLETED,
                artifact_dir=str(original),
                trash_dir="",
                latest_activity="Restored from trash",
            )
            self._append(
                "updated",
                {
                    "session_id": session_id,
                    "changes": {
                        key: value
                        for key, value in restored.to_dict().items()
                        if key != "session_id"
                    },
                },
            )
            return restored

    def purge_trash(self, *, older_than_s: float) -> List[str]:
        """Permanently remove only expired, manifest-backed trash entries.

        The retention worker never accepts a project path.  It resolves each
        direct child beneath this store's dedicated trash directory and only
        removes a directory containing the expected manifest and session id.
        """

        if older_than_s < 0:
            raise ValueError("older_than_s must be non-negative")
        if not self.trash_root.exists():
            return []
        cutoff = time.time() - older_than_s
        removed: list[str] = []
        trash_root = self.trash_root.resolve()
        for candidate in self.trash_root.iterdir():
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.parent != trash_root or not resolved.is_dir():
                continue
            manifest_path = resolved / "manifest.json"
            try:
                manifest = _json_object(json.loads(manifest_path.read_text(encoding="utf-8")))
                session_id = manifest.get("session_id")
                modified = resolved.stat().st_mtime
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(session_id, str) or not session_id:
                continue
            if modified <= cutoff:
                shutil.rmtree(resolved)
                removed.append(session_id)
        return removed
