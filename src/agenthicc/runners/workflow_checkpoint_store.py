"""Atomic workflow checkpoint persistence (PRD-156)."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agenthicc.runners.process_lease import (
    atomic_publish,
    directory_fsync,
    owner_alive,
    process_identity,
    read_json_object,
)
from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    WorkflowCheckpoint,
)

__all__ = ["WorkflowClaim", "WorkflowClaimError", "WorkflowCheckpointStore"]

MAX_RECOVERY_ERROR_BYTES = 32_000


class WorkflowClaimError(RuntimeError):
    """Raised when another live owner already holds a workflow run claim.

    ``owner_id``, ``pid``, and ``host`` are diagnostic metadata only.  They
    never grant permission to take a claim and contain no prompt, tool, or
    credential data.  Keeping them on the exception lets a TUI or API give an
    actionable message without parsing the human-readable error string.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        owner_id: str | None = None,
        pid: int | None = None,
        host: str | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.owner_id = owner_id
        self.pid = pid
        self.host = host


@dataclass(frozen=True)
class WorkflowClaim:
    """The process-local durable lease for one workflow run."""

    run_id: str
    owner_id: str
    path: Path


class WorkflowCheckpointStore:
    """Persist one workflow's metadata without duplicating conversation history."""

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        self._validate_identifier(session_id, "session_id")
        base = root or (Path.home() / ".agenthicc" / "sessions")
        self.session_id = session_id
        self.root = base / session_id / "workflows"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root.parent, 0o700)
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def path_for(self, run_id: str) -> Path:
        """Return the exact checkpoint path for a run ID."""
        self._validate_identifier(run_id, "run_id")
        return self.root / run_id / "checkpoint.json"

    def claim_path_for(self, run_id: str) -> Path:
        """Return the crash-recoverable claim path for *run_id*."""
        self._validate_identifier(run_id, "run_id")
        return self.root / run_id / ".claim"

    def recovery_error_path_for(self, run_id: str) -> Path:
        """Return the diagnostic-only fallback path for *run_id*."""
        self._validate_identifier(run_id, "run_id")
        return self.root / run_id / "recovery-error.json"

    @staticmethod
    def _validate_identifier(value: str, label: str) -> None:
        if (
            not value
            or value in {".", ".."}
            or any(separator in value for separator in ("/", "\\"))
            or "\x00" in value
        ):
            raise ValueError(f"{label} must be a non-empty safe identifier")

    def save(self, checkpoint: WorkflowCheckpoint) -> Path:
        """Atomically save *checkpoint* and return its path."""
        if checkpoint.conversation_id != self.session_id:
            raise CheckpointValidationError("checkpoint belongs to a different session")
        path = self.path_for(checkpoint.run_id)
        existing = self.load(checkpoint.run_id)
        if existing is not None and checkpoint.revision < existing.revision:
            raise CheckpointValidationError(
                f"checkpoint revision {checkpoint.revision} is older than "
                f"the durable revision {existing.revision}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        fd, temp_name = tempfile.mkstemp(prefix=".checkpoint-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return path

    def save_recovery_error(self, payload: Mapping[str, object]) -> Path:
        """Atomically save a bounded diagnostic for an uncheckpointable run.

        This is deliberately not a checkpoint. It exists so setup and storage
        failures remain visible after the process returns to idle; callers
        must not present it as resumable without a valid typed checkpoint.
        """
        run_id = payload.get("run_id")
        session_id = payload.get("session_id")
        if not isinstance(run_id, str):
            raise CheckpointValidationError("recovery error run_id must be a string")
        if session_id != self.session_id:
            raise CheckpointValidationError("recovery error belongs to a different session")
        self._validate_identifier(run_id, "run_id")
        allowed_fields = {
            "run_id",
            "session_id",
            "workflow_name",
            "plugin_fingerprint",
            "intent_digest",
            "phase",
            "phase_index",
            "failure_kind",
            "failure_message",
            "record_revision",
            "context_ready",
            "created_at",
            "updated_at",
        }
        safe_payload = {
            str(key): value for key, value in payload.items() if str(key) in allowed_fields
        }
        safe_payload["diagnostic_only"] = True
        safe_payload["resumable"] = False
        self._validate_recovery_payload(safe_payload, run_id)
        existing = self.load_recovery_error(run_id)
        if existing is not None:
            existing_revision = existing.get("record_revision")
            incoming_revision = safe_payload["record_revision"]
            if (
                isinstance(existing_revision, int)
                and isinstance(incoming_revision, int)
                and incoming_revision < existing_revision
            ):
                raise CheckpointValidationError(
                    f"recovery diagnostic revision {incoming_revision} is older than "
                    f"the durable revision {existing_revision}"
                )
        encoded = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, indent=2)
        if len(encoded.encode("utf-8")) + 1 > MAX_RECOVERY_ERROR_BYTES:
            raise CheckpointValidationError(
                f"workflow recovery error exceeds {MAX_RECOVERY_ERROR_BYTES} bytes"
            )
        path = self.recovery_error_path_for(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        fd, temp_name = tempfile.mkstemp(prefix=".recovery-error-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return path

    def load_recovery_error(self, run_id: str) -> dict[str, object] | None:
        """Load a diagnostic-only fallback record, if one exists."""
        path = self.recovery_error_path_for(run_id)
        if not path.exists():
            return None
        try:
            encoded = path.read_bytes()
            if len(encoded) > MAX_RECOVERY_ERROR_BYTES:
                raise CheckpointValidationError(
                    f"workflow recovery error exceeds {MAX_RECOVERY_ERROR_BYTES} bytes"
                )
            raw = json.loads(encoded.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointValidationError(
                f"invalid workflow recovery error {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise CheckpointValidationError("workflow recovery error must be a JSON object")
        self._validate_recovery_payload(raw, run_id)
        return {str(key): value for key, value in raw.items()}

    def _validate_recovery_payload(self, payload: Mapping[str, object], run_id: str) -> None:
        """Validate fallback identity, bounds, and disposition before display."""
        allowed_fields = {
            "run_id",
            "session_id",
            "workflow_name",
            "plugin_fingerprint",
            "intent_digest",
            "phase",
            "phase_index",
            "failure_kind",
            "failure_message",
            "record_revision",
            "context_ready",
            "created_at",
            "updated_at",
            "diagnostic_only",
            "resumable",
        }
        unknown_fields = set(payload).difference(allowed_fields)
        if unknown_fields:
            raise CheckpointValidationError(
                "workflow recovery error contains unsupported fields: "
                + ", ".join(sorted(str(field) for field in unknown_fields))
            )
        if payload.get("run_id") != run_id or payload.get("session_id") != self.session_id:
            raise CheckpointValidationError(
                "workflow recovery error identity does not match request"
            )
        if payload.get("diagnostic_only") is not True or payload.get("resumable") is not False:
            raise CheckpointValidationError("workflow recovery error disposition is invalid")
        string_limits = {
            "workflow_name": 128,
            "plugin_fingerprint": 128,
            "intent_digest": 128,
            "failure_kind": 64,
            "failure_message": 512,
        }
        for field_name, limit in string_limits.items():
            value = payload.get(field_name)
            if not isinstance(value, str):
                raise CheckpointValidationError(
                    f"workflow recovery error {field_name} must be a string"
                )
            if len(value) > limit:
                raise CheckpointValidationError(f"workflow recovery error {field_name} is too long")
        phase = payload.get("phase")
        if phase is not None:
            if not isinstance(phase, str):
                raise CheckpointValidationError("workflow recovery error phase must be a string")
            if len(phase) > 256:
                raise CheckpointValidationError("workflow recovery error phase is too long")
        phase_index = payload.get("phase_index")
        record_revision = payload.get("record_revision")
        if (
            not isinstance(phase_index, int)
            or isinstance(phase_index, bool)
            or phase_index < 0
            or not isinstance(record_revision, int)
            or isinstance(record_revision, bool)
            or record_revision < 0
        ):
            raise CheckpointValidationError(
                "workflow recovery error phase_index and record_revision must be non-negative integers"
            )
        context_ready = payload.get("context_ready")
        if not isinstance(context_ready, bool):
            raise CheckpointValidationError(
                "workflow recovery error context_ready must be a boolean"
            )
        for field_name in ("created_at", "updated_at"):
            value = payload.get(field_name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CheckpointValidationError(
                    f"workflow recovery error {field_name} must be numeric"
                )

    def delete_recovery_error(self, run_id: str) -> None:
        """Remove a fallback after a newer primary checkpoint is durable."""
        path = self.recovery_error_path_for(run_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def acquire_claim(self, run_id: str, owner_id: str) -> WorkflowClaim:
        """Atomically claim a run, reclaiming only a provably dead owner.

        The claim is deliberately separate from ``checkpoint.json``.  A
        process may disappear between any two checkpoint writes; an atomic
        claim publication prevents two live TUI/session owners from resuming
        one run while a dead process's claim can be recovered on the next
        start.
        """
        self._validate_identifier(run_id, "run_id")
        self._validate_owner(owner_id)
        path = self.claim_path_for(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        payload = {
            "owner_id": owner_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_at": time.time(),
        }
        process_identity = self._process_identity(os.getpid())
        if process_identity is not None:
            payload["process_start_token"] = process_identity[1]
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

        for _attempt in range(2):
            try:
                # Build the complete claim in a private temporary file and
                # publish it with a hard link.  O_EXCL followed by a write
                # leaves an empty/partial .claim if this process is killed in
                # that interval; a later process cannot distinguish that file
                # from a live owner and the run becomes permanently stranded.
                # Linking a fully fsynced file makes the visible claim either
                # absent or complete.
                self._install_claim(path, encoded)
            except FileExistsError:
                existing = self._read_claim(path)
                if existing is None or not self._claim_owner_alive(existing):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    continue
                if existing.get("owner_id") == owner_id:
                    return WorkflowClaim(run_id, owner_id, path)
                raise WorkflowClaimError(
                    f"workflow run {run_id!r} is already claimed by another live owner "
                    f"({self._claim_owner_description(existing)}); close that agenthicc "
                    "process or resume the run there before retrying",
                    run_id=run_id,
                    owner_id=self._string_value(existing.get("owner_id")),
                    pid=self._int_value(existing.get("pid")),
                    host=self._string_value(existing.get("host")),
                )
            self._fsync_directory(path.parent)
            return WorkflowClaim(run_id, owner_id, path)

        raise WorkflowClaimError(
            f"could not claim workflow run {run_id!r}",
            run_id=run_id,
        )

    def release_claim(self, run_id: str, owner_id: str) -> None:
        """Release a claim only when the caller owns it."""
        self._validate_identifier(run_id, "run_id")
        self._validate_owner(owner_id)
        path = self.claim_path_for(run_id)
        existing = self._read_claim(path)
        if existing is None or existing.get("owner_id") != owner_id:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def claim_owner(self, run_id: str) -> str | None:
        """Return the current claim owner, if a readable claim exists."""
        self._validate_identifier(run_id, "run_id")
        payload = self._read_claim(self.claim_path_for(run_id))
        if payload is None:
            return None
        if not self._claim_owner_alive(payload):
            try:
                self.claim_path_for(run_id).unlink()
            except FileNotFoundError:
                pass
            return None
        owner = payload.get("owner_id")
        return owner if isinstance(owner, str) else None

    def load(self, run_id: str) -> WorkflowCheckpoint | None:
        """Load and validate a checkpoint, returning ``None`` when absent."""
        path = self.path_for(run_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            checkpoint = WorkflowCheckpoint.from_dict(raw)
        except (OSError, json.JSONDecodeError, CheckpointValidationError) as exc:
            raise CheckpointValidationError(f"invalid workflow checkpoint {path}: {exc}") from exc
        if checkpoint.run_id != run_id or checkpoint.conversation_id != self.session_id:
            raise CheckpointValidationError("checkpoint identity does not match requested run")
        return checkpoint

    def delete(self, run_id: str) -> None:
        """Discard one workflow checkpoint after an explicit reset."""
        path = self.path_for(run_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.recovery_error_path_for(run_id).unlink()
        except FileNotFoundError:
            pass

    def list_run_ids(self) -> list[str]:
        """Return checkpoint run IDs in deterministic order."""
        if not self.root.exists():
            return []
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir()
            and ((child / "checkpoint.json").is_file() or (child / "recovery-error.json").is_file())
        )

    @staticmethod
    def _validate_owner(value: str) -> None:
        if not value or "\x00" in value or len(value) > 256:
            raise ValueError("owner_id must be a non-empty bounded string")

    @staticmethod
    def _string_value(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _int_value(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _claim_owner_description(payload: dict[str, object]) -> str:
        owner = WorkflowCheckpointStore._string_value(payload.get("owner_id")) or "unknown"
        pid = WorkflowCheckpointStore._int_value(payload.get("pid"))
        host = WorkflowCheckpointStore._string_value(payload.get("host")) or "unknown host"
        return f"owner={owner!r}, pid={pid if pid is not None else 'unknown'}, host={host!r}"

    @staticmethod
    def _install_claim(path: Path, encoded: bytes) -> None:
        """Publish a complete claim atomically, or raise ``FileExistsError``.

        A unique temporary file may remain after a hard process kill, but it
        is hidden and cannot block a future claim.  The visible destination is
        installed only after its contents and file metadata are durable.
        """
        atomic_publish(path, encoded, prefix=".claim-")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fsync(path)

    @staticmethod
    def _read_claim(path: Path) -> dict[str, object] | None:
        # A present but unreadable/malformed claim is not proof of a dead
        # owner.  ``read_json_object`` represents that state as ``{}``, which
        # makes the liveness check fail closed.
        return read_json_object(path)

    @staticmethod
    def _claim_owner_alive(payload: dict[str, object]) -> bool:
        return owner_alive(
            payload,
            identity_resolver=WorkflowCheckpointStore._process_identity,
        )

    @staticmethod
    def _process_identity(pid: int) -> tuple[str, str] | None:
        """Return ``(state, start-token)`` for a local process when available.

        Linux exposes both values in ``/proc/<pid>/stat``.  The boot ID keeps
        the token meaningful across host reboots.  Other platforms fall back
        to the conservative ``kill(pid, 0)`` check above.
        """
        return process_identity(pid)
