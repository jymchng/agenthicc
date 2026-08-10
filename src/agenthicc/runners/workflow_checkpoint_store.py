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
        fd, temp_name = tempfile.mkstemp(
            prefix=".claim-",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            dir_fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Directory fsync is not available on every supported platform.
            # The claim contents have already been fsynced before publication.
            pass

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
        identity = WorkflowCheckpointStore._process_identity(pid)
        if identity is not None:
            state, current_start_token = identity
            # kill(pid, 0) succeeds for zombies.  A zombie cannot own an
            # executing workflow, so it is safe to reclaim its claim.
            if state == "Z":
                return False
            recorded_start_token = payload.get("process_start_token")
            # New claims bind the PID to its process-start identity.  If the
            # PID was reused, the old owner is provably gone even though the
            # replacement process is alive.  Legacy claims without this field
            # retain the old fail-closed behaviour.
            if (
                isinstance(recorded_start_token, str)
                and recorded_start_token
                and recorded_start_token != current_start_token
            ):
                return False
        return True

    @staticmethod
    def _process_identity(pid: int) -> tuple[str, str] | None:
        """Return ``(state, start-token)`` for a local process when available.

        Linux exposes both values in ``/proc/<pid>/stat``.  The boot ID keeps
        the token meaningful across host reboots.  Other platforms fall back
        to the conservative ``kill(pid, 0)`` check above.
        """
        if os.name == "nt":
            return None
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            end_comm = stat.rfind(")")
            if end_comm < 0:
                return None
            fields = stat[end_comm + 2 :].split()
            if len(fields) <= 19:
                return None
            state = fields[0]
            start_time = fields[19]
            try:
                boot_id = (
                    Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
                )
            except (OSError, UnicodeError):
                boot_id = ""
            return state, f"{boot_id}:{start_time}" if boot_id else start_time
        except (OSError, UnicodeError, ValueError):
            return None
