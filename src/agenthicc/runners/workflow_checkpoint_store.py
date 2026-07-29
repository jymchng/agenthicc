"""Atomic workflow checkpoint persistence (PRD-156)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agenthicc.workflows.checkpoint import (
    MAX_CHECKPOINT_BYTES,
    CheckpointValidationError,
    WorkflowCheckpoint,
)

__all__ = ["WorkflowCheckpointStore"]


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
