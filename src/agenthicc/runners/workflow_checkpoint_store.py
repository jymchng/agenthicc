"""Atomic workflow checkpoint persistence (PRD-156)."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from agenthicc.workflows.checkpoint import (
    MAX_CHECKPOINT_BYTES,
    CheckpointValidationError,
    WorkflowCheckpoint,
)

__all__ = ["WorkflowClaim", "WorkflowClaimError", "WorkflowCheckpointStore"]


class WorkflowClaimError(RuntimeError):
    """Raised when another live owner already holds a workflow run claim."""


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
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        if len(payload.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise CheckpointValidationError(
                f"workflow checkpoint exceeds {MAX_CHECKPOINT_BYTES} bytes"
            )
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

    def acquire_claim(self, run_id: str, owner_id: str) -> WorkflowClaim:
        """Atomically claim a run, reclaiming only a provably dead owner.

        The claim is deliberately separate from ``checkpoint.json``.  A
        process may disappear between any two checkpoint writes; an atomic
        ``O_EXCL`` file prevents two live TUI/session owners from resuming one
        run while a dead process's claim can be recovered on the next start.
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
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

        for _attempt in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
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
                    f"workflow run {run_id!r} is already claimed by another live owner"
                )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    dir_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            except Exception:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                raise
            return WorkflowClaim(run_id, owner_id, path)

        raise WorkflowClaimError(f"could not claim workflow run {run_id!r}")

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
            return

    def list_run_ids(self) -> list[str]:
        """Return checkpoint run IDs in deterministic order."""
        if not self.root.exists():
            return []
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir() and (child / "checkpoint.json").is_file()
        )

    @staticmethod
    def _validate_owner(value: str) -> None:
        if not value or "\x00" in value or len(value) > 256:
            raise ValueError("owner_id must be a non-empty bounded string")

    @staticmethod
    def _read_claim(path: Path) -> dict[str, object] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            # A present but unreadable/malformed claim is not proof of a dead
            # owner.  Returning an empty object makes the liveness check fail
            # closed instead of allowing a second process to take the run.
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _claim_owner_alive(payload: dict[str, object]) -> bool:
        host = payload.get("host")
        pid = payload.get("pid")
        if host != socket.gethostname() or not isinstance(pid, int) or pid <= 0:
            # A claim from another host cannot be proven dead locally. Fail
            # closed rather than allowing two machines to execute the run.
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
