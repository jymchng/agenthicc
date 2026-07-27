"""Owned background terminal processes (PRD-149).

This module deliberately manages only subprocesses created by agenthicc.  It
does not inspect or attach to arbitrary host processes.  A terminal receives a
new process group, a durable record, and a bounded output buffer; all control
operations resolve through the in-memory entry that created that group.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from agenthicc.reactive import Signal
from agenthicc.tools.exec.outcome import resolve_deadline, validate_timeout

__all__ = [
    "TerminalManager",
    "TerminalRecord",
    "TerminalState",
    "TerminalStore",
    "get_current_terminal_manager",
    "reset_current_terminal_manager",
    "set_current_terminal_manager",
    "get_current_terminal_wait_policy",
    "reset_current_terminal_wait_policy",
    "set_current_terminal_wait_policy",
    "stop_persisted_session_terminals",
]


class TerminalState(StrEnum):
    """Durable lifecycle states for an owned terminal."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SPAWN_FAILED = "spawn_failed"
    REJECTED = "rejected"
    STOPPED = "stopped"
    ORPHANED = "orphaned"


_ACTIVE_STATES = {
    TerminalState.STARTING,
    TerminalState.RUNNING,
    TerminalState.STOPPING,
}
_SECRET_FLAGS = re.compile(
    r"(?i)(--?(?:password|passwd|token|secret|api[-_]?key|authorization))\s*(?:=|\s)\s*([^\s]+)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b((?:api[_-]?key|token|secret|password|passwd|authorization))\s*=\s*([^\s;&]+)"
)
_REDACTION = "<redacted>"
_DEFAULT_MAX_OUTPUT = 64 * 1024


def _is_loopback(host: str) -> bool:
    """Return whether a readiness probe targets the local machine."""

    import ipaddress

    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def redact_text(value: str, *, limit: int = 240) -> str:
    """Redact common credential-shaped command fragments and bound text."""

    value = _SECRET_FLAGS.sub(lambda match: f"{match.group(1)}={_REDACTION}", value)
    value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}={_REDACTION}", value)
    value = " ".join(value.split())
    if len(value) > limit:
        return value[: max(0, limit - 1)] + "…"
    return value


def _redact_readiness(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        str(key): redact_text(str(item)) if isinstance(item, str) else item
        for key, item in value.items()
    }


def _redact_output(value: str) -> str:
    """Redact credential-shaped output without changing its line structure."""

    value = _SECRET_FLAGS.sub(lambda match: f"{match.group(1)}={_REDACTION}", value)
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}={_REDACTION}", value)


def _bounded_append(current: str, addition: str, limit: int) -> tuple[str, bool]:
    """Keep a useful head/tail buffer without allowing unbounded output."""

    if not addition:
        return current, False
    combined = current + addition
    encoded = combined.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return combined, False
    marker = "\n[… output truncated …]\n"
    marker_bytes = len(marker.encode())
    keep = max(1, (limit - marker_bytes) // 2)
    head = encoded[:keep].decode("utf-8", errors="replace")
    tail = encoded[-keep:].decode("utf-8", errors="replace")
    return head + marker + tail, True


@dataclass
class TerminalRecord:
    """Durable and display-safe metadata for one terminal process."""

    terminal_id: str
    session_id: str
    project_root: str
    cwd: str
    kind: str
    command: str
    label: str
    state: TerminalState
    created_at: float
    tool_call_id: str = ""
    parent_job_id: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    pid: int | None = None
    pgid: int | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_bytes: int = 0
    truncated: bool = False
    timed_out: bool = False
    stop_reason: str = ""
    timeout_s: float = 0.0
    effective_timeout_s: float | None = None
    last_output_at: float | None = None
    lifecycle: str = "oneshot"
    ready: bool | None = None
    readiness: dict[str, object] | None = None
    readiness_evidence: str = ""
    deadline_owner: str | None = None
    termination_reason: str | None = None
    cleanup_result: str = "not_required"
    cancelled: bool = False
    spawn_failure: str | None = None
    command_digest: str = ""

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at or time.time()
        start = self.started_at or self.created_at
        return max(0.0, end - start)

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_STATES

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe, redacted public metadata."""

        return {
            "terminal_id": self.terminal_id,
            "session_id": self.session_id,
            "project_root": self.project_root,
            "cwd": self.cwd,
            "kind": self.kind,
            "command": self.command,
            "command_digest": self.command_digest,
            "label": self.label,
            "state": self.state.value,
            "created_at": self.created_at,
            "tool_call_id": self.tool_call_id,
            "parent_job_id": self.parent_job_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pid": self.pid,
            "pgid": self.pgid,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_bytes": self.output_bytes,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "stop_reason": self.stop_reason,
            "timeout_s": self.timeout_s,
            "effective_timeout_s": self.effective_timeout_s,
            "last_output_at": self.last_output_at,
            "lifecycle": self.lifecycle,
            "ready": self.ready,
            "readiness": self.readiness,
            "readiness_evidence": self.readiness_evidence,
            "deadline_owner": self.deadline_owner,
            "termination_reason": self.termination_reason,
            "cleanup_result": self.cleanup_result,
            "cancelled": self.cancelled,
            "spawn_failure": self.spawn_failure,
        }

    def result(self) -> dict[str, object]:
        """Return the stable tool result for a completed or observed terminal."""

        return {
            "ok": self.state == TerminalState.EXITED and self.returncode == 0,
            "background": True,
            "terminal_id": self.terminal_id,
            "tool_call_id": self.tool_call_id,
            "parent_job_id": self.parent_job_id,
            "command_kind": self.kind,
            "command": self.command,
            "command_digest": self.command_digest,
            "cwd": self.cwd,
            "state": self.state.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode if self.returncode is not None else -1,
            "duration_ms": round(self.elapsed_s * 1000, 1),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "output_bytes": self.output_bytes,
            "label": self.label,
            "lifecycle": self.lifecycle,
            "ready": self.ready,
            "readiness_evidence": self.readiness_evidence,
            "deadline": (
                {
                    "requested_s": self.timeout_s,
                    "effective_s": self.effective_timeout_s or self.timeout_s,
                    "owner": self.deadline_owner or "command",
                }
                if self.timeout_s > 0 or self.deadline_owner
                else None
            ),
            "deadline_owner": self.deadline_owner,
            "termination_reason": self.termination_reason or self.stop_reason or None,
            "cleanup_result": self.cleanup_result,
            "spawn_failure": self.spawn_failure,
            "pid": self.pid,
            "pgid": self.pgid,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "TerminalRecord":
        """Load a tolerant record from the append-only store."""

        def text(name: str, default: str = "") -> str:
            value = raw.get(name, default)
            return value if isinstance(value, str) else default

        def number(name: str, default: float | None = None) -> float | None:
            value = raw.get(name, default)
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else default
            )

        state_raw = text("state", TerminalState.ORPHANED.value)
        try:
            state = TerminalState(state_raw)
        except ValueError:
            state = TerminalState.ORPHANED
        pid = raw.get("pid")
        pgid = raw.get("pgid")
        raw_returncode = raw.get("returncode")
        returncode = (
            raw_returncode
            if isinstance(raw_returncode, int) and not isinstance(raw_returncode, bool)
            else None
        )
        ready_raw = raw.get("ready")
        deadline_raw = raw.get("deadline_owner")
        termination_raw = raw.get("termination_reason")
        spawn_raw = raw.get("spawn_failure")
        return cls(
            terminal_id=text("terminal_id"),
            session_id=text("session_id"),
            project_root=text("project_root"),
            cwd=text("cwd"),
            kind=text("kind", "shell"),
            command=redact_text(text("command")),
            command_digest=text("command_digest"),
            label=redact_text(text("label")),
            state=state,
            created_at=number("created_at", time.time()) or time.time(),
            tool_call_id=text("tool_call_id"),
            parent_job_id=text("parent_job_id"),
            started_at=number("started_at"),
            finished_at=number("finished_at"),
            pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            pgid=pgid if isinstance(pgid, int) and not isinstance(pgid, bool) else None,
            returncode=returncode,
            stdout=text("stdout"),
            stderr=text("stderr"),
            output_bytes=int(number("output_bytes", 0) or 0),
            truncated=bool(raw.get("truncated", False)),
            timed_out=bool(raw.get("timed_out", False)),
            stop_reason=text("stop_reason"),
            timeout_s=number("timeout_s", 0.0) or 0.0,
            effective_timeout_s=number("effective_timeout_s"),
            last_output_at=number("last_output_at"),
            lifecycle=text("lifecycle", "oneshot"),
            ready=ready_raw if isinstance(ready_raw, bool) else None,
            readiness=(
                {str(key): item for key, item in readiness_value.items()}
                if isinstance((readiness_value := raw.get("readiness")), dict)
                else None
            ),
            readiness_evidence=text("readiness_evidence"),
            deadline_owner=deadline_raw if isinstance(deadline_raw, str) else None,
            termination_reason=termination_raw if isinstance(termination_raw, str) else None,
            cleanup_result=text("cleanup_result", "not_required"),
            cancelled=bool(raw.get("cancelled", False)),
            spawn_failure=spawn_raw if isinstance(spawn_raw, str) else None,
        )


class TerminalStore:
    """Small fsync'd JSONL registry for terminal lifecycle records."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or (Path.home() / ".agenthicc" / "background" / "terminals"))
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "registry.lock"

    def _lock(self) -> AbstractContextManager[None]:
        @contextmanager
        def locked() -> Iterator[None]:
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

        return locked()

    def _events(self) -> list[dict[str, object]]:
        if not self.events_path.exists():
            return []
        result: list[dict[str, object]] = []
        try:
            for line in self.events_path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    result.append({str(key): item for key, item in value.items()})
        except OSError:
            return []
        return result

    def _append(self, record: TerminalRecord) -> None:
        with self._lock():
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"event": "upsert", "record": record.to_dict(), "timestamp": time.time()}
            fd = os.open(self.events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, (json.dumps(payload, separators=(",", ":")) + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)

    def upsert(self, record: TerminalRecord) -> None:
        self._append(record)

    def records(self) -> list[TerminalRecord]:
        folded: dict[str, TerminalRecord] = {}
        for event in self._events():
            raw = event.get("record")
            if isinstance(raw, dict):
                record = TerminalRecord.from_mapping(raw)
                if record.terminal_id:
                    folded[record.terminal_id] = record
        return list(folded.values())

    def get(self, terminal_id: str) -> TerminalRecord | None:
        return next((item for item in self.records() if item.terminal_id == terminal_id), None)

    def prune(self, *, older_than_s: float) -> int:
        """Compact completed records older than the configured retention window."""

        if older_than_s <= 0:
            return 0
        cutoff = time.time() - older_than_s
        current = self.records()
        keep = [record for record in current if record.active or record.created_at >= cutoff]
        removed = len(current) - len(keep)
        if removed <= 0:
            return 0
        with self._lock():
            temporary = self.events_path.with_suffix(".tmp")
            temporary.write_text(
                "".join(
                    json.dumps(
                        {"event": "upsert", "record": record.to_dict(), "timestamp": time.time()},
                        separators=(",", ":"),
                    )
                    + "\n"
                    for record in keep
                ),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.events_path)
        return removed


@dataclass
class _RuntimeEntry:
    record: TerminalRecord
    process: asyncio.subprocess.Process
    completion: asyncio.Task[None]
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    stop_requested: bool = False
    force_requested: bool = False


_manager_var: contextvars.ContextVar["TerminalManager | None"] = contextvars.ContextVar(
    "agenthicc_terminal_manager", default=None
)
_wait_policy_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agenthicc_terminal_wait_policy", default="foreground"
)


def get_current_terminal_manager() -> "TerminalManager | None":
    """Return the terminal manager bound to the current agent turn."""

    return _manager_var.get()


def set_current_terminal_manager(
    manager: "TerminalManager | None",
) -> contextvars.Token["TerminalManager | None"]:
    """Bind a manager to the current task; callers must reset the token."""

    return _manager_var.set(manager)


def reset_current_terminal_manager(token: contextvars.Token["TerminalManager | None"]) -> None:
    """Restore the manager binding captured before a session started."""

    _manager_var.reset(token)


def get_current_terminal_wait_policy() -> str:
    """Return the workflow phase's terminal wait policy."""

    return _wait_policy_var.get()


def set_current_terminal_wait_policy(policy: str) -> contextvars.Token[str]:
    """Bind a validated phase policy to the current agent-turn task."""

    return _wait_policy_var.set(policy if policy in {"foreground", "background"} else "foreground")


def reset_current_terminal_wait_policy(token: contextvars.Token[str]) -> None:
    """Restore the prior phase terminal policy."""

    _wait_policy_var.reset(token)


class TerminalManager:
    """Own background terminal process groups for one agent session."""

    def __init__(
        self,
        *,
        session_id: str,
        cwd: str | Path = ".",
        store_root: str | Path | None = None,
        store: TerminalStore | None = None,
        enabled: bool = True,
        max_terminals: int = 4,
        max_terminals_per_project: int = 8,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT,
        wall_timeout_s: float = 0.0,
        cancel_grace_s: float = 5.0,
        parent_job_id: str = "",
        retention_days: int = 30,
    ) -> None:
        self.session_id = session_id
        self.cwd = str(Path(cwd).resolve())
        self.project_root = self.cwd
        self.enabled = enabled
        self.max_terminals = max(1, max_terminals)
        self.max_terminals_per_project = max(1, max_terminals_per_project)
        self.max_output_bytes = max(1024, max_output_bytes)
        self.wall_timeout_s = max(0.0, wall_timeout_s)
        self.cancel_grace_s = max(0.0, cancel_grace_s)
        self.parent_job_id = parent_job_id or session_id
        self.retention_days = max(0, retention_days)
        self.store = store or TerminalStore(store_root)
        if self.retention_days:
            self.store.prune(older_than_s=self.retention_days * 86_400)
        self.changed: Signal[int] = Signal(0)
        self._records: dict[str, TerminalRecord] = {
            record.terminal_id: record for record in self.store.records()
        }
        self._entries: dict[str, _RuntimeEntry] = {}
        self._stop_tasks: dict[str, asyncio.Task[bool]] = {}
        self._wait_ids: list[str] = []
        self._closed = False
        self._recover_stale_records()

    def _recover_stale_records(self) -> None:
        """Mark active records from a previous manager as orphaned.

        We intentionally do not signal a PID read from disk.  Only a live
        ``_RuntimeEntry`` created by this manager is eligible for termination;
        this prevents PID reuse from becoming an arbitrary kill primitive.
        """

        for record in self._records.values():
            if record.active and record.session_id == self.session_id:
                record.state = TerminalState.ORPHANED
                record.finished_at = record.finished_at or time.time()
                record.stop_reason = "manager restarted before terminal completed"
                self.store.upsert(record)

    def _notify(self) -> None:
        self.changed.set(self.changed() + 1)

    def _visible_records(self, *, all_sessions: bool = False) -> list[TerminalRecord]:
        records = list(self._records.values())
        if not all_sessions:
            records = [record for record in records if record.session_id == self.session_id]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def list_records(self, *, all_sessions: bool = False) -> list[TerminalRecord]:
        """Return display-safe records, newest first."""

        return self._visible_records(all_sessions=all_sessions)

    def get(self, terminal_id: str) -> TerminalRecord | None:
        return self._records.get(terminal_id)

    def running_count(self) -> int:
        return sum(record.active for record in self._visible_records())

    def current_wait_id(self) -> str | None:
        return self._wait_ids[-1] if self._wait_ids else None

    def wait_snapshot(self) -> dict[str, object] | None:
        terminal_id = self.current_wait_id()
        if terminal_id is None:
            return None
        record = self._records.get(terminal_id)
        if record is None:
            return None
        return {
            "terminal_id": record.terminal_id,
            "label": record.label,
            "elapsed_s": record.elapsed_s,
            "running_count": self.running_count(),
            "state": record.state.value,
        }

    def _check_limits(self) -> str | None:
        if not self.enabled:
            return "background terminals are disabled by configuration"
        session_running = sum(
            record.active
            for record in self._records.values()
            if record.session_id == self.session_id
        )
        if session_running >= self.max_terminals:
            return f"session terminal limit reached ({self.max_terminals})"
        project_running = sum(
            record.active
            for record in self._records.values()
            if record.project_root == self.project_root
        )
        if project_running >= self.max_terminals_per_project:
            return f"project terminal limit reached ({self.max_terminals_per_project})"
        return None

    async def start(
        self,
        *,
        command: str | None = None,
        argv: list[str] | None = None,
        cwd: str | Path | None = None,
        timeout: float = 0.0,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
        label: str = "",
        tool_call_id: str = "",
        lifecycle: str = "oneshot",
        readiness: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Start an owned process and return a stable handle immediately."""

        try:
            timeout = validate_timeout(timeout)
        except ValueError as exc:
            return {
                "ok": False,
                "background": True,
                "state": TerminalState.REJECTED.value,
                "error": str(exc),
                "termination_reason": str(exc),
            }
        if lifecycle not in {"oneshot", "service"}:
            return {
                "ok": False,
                "background": True,
                "state": TerminalState.REJECTED.value,
                "error": "lifecycle must be 'oneshot' or 'service'",
            }
        if env is not None and not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            return {
                "ok": False,
                "background": True,
                "state": TerminalState.REJECTED.value,
                "error": "environment overlay must contain only string keys and values",
                "spawn_failure": "environment",
            }
        reason = self._check_limits()
        if reason:
            return {"ok": False, "background": True, "error": reason, "state": "rejected"}
        if (command is None) == (argv is None):
            return {
                "ok": False,
                "background": True,
                "error": "provide command or argv",
                "state": "failed",
            }
        requested_cwd = Path(cwd) if cwd is not None else Path(self.cwd)
        if not requested_cwd.is_absolute():
            requested_cwd = Path(self.cwd) / requested_cwd
        resolved_cwd = str(requested_cwd.resolve())
        if not Path(resolved_cwd).is_dir():
            return {
                "ok": False,
                "background": True,
                "state": TerminalState.SPAWN_FAILED.value,
                "error": f"cwd is not a directory: {resolved_cwd}",
                "termination_reason": "spawn failed: invalid cwd",
                "spawn_failure": "cwd",
            }
        if lifecycle == "service":
            duplicate = self._find_active_service(
                command=command,
                argv=argv,
                cwd=resolved_cwd,
            )
            if duplicate is not None:
                return {
                    "ok": False,
                    "background": True,
                    "state": TerminalState.REJECTED.value,
                    "error": f"service already owned by terminal {duplicate.terminal_id}",
                    "terminal_id": duplicate.terminal_id,
                }
        terminal_id = f"term-{secrets.token_hex(4)}"
        display_command = command if command is not None else " ".join(argv or ())
        command_digest = hashlib.sha256(display_command.encode()).hexdigest()
        record = TerminalRecord(
            terminal_id=terminal_id,
            session_id=self.session_id,
            project_root=self.project_root,
            cwd=resolved_cwd,
            kind="shell" if shell else "exec",
            command=redact_text(display_command),
            command_digest=command_digest,
            label=redact_text(label or display_command),
            state=TerminalState.STARTING,
            created_at=time.time(),
            tool_call_id=tool_call_id,
            parent_job_id=self.parent_job_id,
            timeout_s=timeout,
            lifecycle=lifecycle,
            readiness=_redact_readiness(readiness),
        )
        self._records[terminal_id] = record
        self.store.upsert(record)
        self._notify()
        effective_env = {**os.environ, **dict(env or {})}
        try:
            if shell:
                process = await asyncio.create_subprocess_shell(
                    command or "",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=resolved_cwd,
                    env=effective_env,
                    start_new_session=True,
                    executable=(
                        "/bin/bash"
                        if os.name != "nt" and os.path.exists("/bin/bash")
                        else shutil.which("bash")
                    ),
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *(argv or ()),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=resolved_cwd,
                    env=effective_env,
                    start_new_session=True,
                )
        except (FileNotFoundError, OSError) as exc:
            record.state = TerminalState.SPAWN_FAILED
            record.finished_at = time.time()
            record.returncode = -1
            record.stderr = redact_text(str(exc), limit=self.max_output_bytes)
            record.termination_reason = f"spawn failed: {type(exc).__name__}"
            record.cleanup_result = "not_spawned"
            record.spawn_failure = (
                "executable_or_shell"
                if isinstance(exc, FileNotFoundError)
                else "permission"
                if isinstance(exc, PermissionError)
                else "spawn"
            )
            self.store.upsert(record)
            self._notify()
            return record.result()

        record.state = TerminalState.RUNNING
        record.started_at = time.time()
        record.pid = process.pid
        try:
            record.pgid = os.getpgid(process.pid)
        except (OSError, ProcessLookupError):
            record.pgid = process.pid
        completion = asyncio.create_task(self._monitor(terminal_id), name=f"terminal-{terminal_id}")
        entry = _RuntimeEntry(record=record, process=process, completion=completion)
        self._entries[terminal_id] = entry
        entry.stdout_task = asyncio.create_task(self._pump(terminal_id, process.stdout, "stdout"))
        entry.stderr_task = asyncio.create_task(self._pump(terminal_id, process.stderr, "stderr"))
        self.store.upsert(record)
        self._notify()
        result: dict[str, object] = {
            "ok": True,
            "background": True,
            "terminal_id": terminal_id,
            "state": record.state.value,
            "pid": record.pid,
            "label": record.label,
            "tool_call_id": tool_call_id,
            "lifecycle": lifecycle,
        }
        if readiness is not None:
            result.update(await self.wait_readiness(terminal_id, readiness))
        return result

    def _find_active_service(
        self,
        *,
        command: str | None,
        argv: list[str] | None,
        cwd: str,
    ) -> TerminalRecord | None:
        identity = hashlib.sha256((command or " ".join(argv or ())).encode()).hexdigest()
        return next(
            (
                record
                for record in self._visible_records(all_sessions=True)
                if (
                    record.active
                    or (record.state is TerminalState.ORPHANED and _pid_alive(record.pid))
                )
                and record.lifecycle == "service"
                and record.cwd == cwd
                and record.command_digest == identity
            ),
            None,
        )

    async def _pump(
        self, terminal_id: str, stream: asyncio.StreamReader | None, channel: str
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            record = self._records.get(terminal_id)
            if record is None:
                return
            text = chunk.decode("utf-8", errors="replace")
            text = _redact_output(text)
            record.output_bytes += len(chunk)
            record.last_output_at = time.time()
            bounded, truncated = _bounded_append(
                getattr(record, channel), text, self.max_output_bytes
            )
            setattr(record, channel, bounded)
            record.truncated = record.truncated or truncated
            self._notify()

    async def _monitor(self, terminal_id: str) -> None:
        entry = self._entries.get(terminal_id)
        if entry is None:
            return
        record = entry.record
        deadline = resolve_deadline(
            record.timeout_s,
            configured_s=self.wall_timeout_s if self.wall_timeout_s > 0 else None,
            configured_owner="background_session",
        )
        if deadline.owner is not None and deadline.owner != "command":
            record.deadline_owner = deadline.owner
        record.effective_timeout_s = deadline.effective_s
        try:
            if deadline.effective_s is not None:
                await asyncio.wait_for(entry.process.wait(), timeout=deadline.effective_s)
            else:
                await entry.process.wait()
        except asyncio.TimeoutError:
            record.timed_out = True
            record.stop_reason = "timeout"
            record.termination_reason = "command deadline expired"
            record.deadline_owner = deadline.owner or "command"
            entry.stop_requested = True
            record.state = TerminalState.STOPPING
            self._signal(entry, force=False)
            try:
                await asyncio.wait_for(
                    asyncio.shield(entry.process.wait()), timeout=self.cancel_grace_s
                )
                record.cleanup_result = "graceful_timeout"
            except asyncio.TimeoutError:
                entry.force_requested = True
                self._signal(entry, force=True)
                try:
                    await asyncio.wait_for(asyncio.shield(entry.process.wait()), timeout=1.0)
                    record.cleanup_result = "force_timeout"
                except asyncio.TimeoutError:
                    record.cleanup_result = "cleanup_unproven"
        except asyncio.CancelledError:
            record.cancelled = True
            record.stop_reason = "cancelled"
            record.termination_reason = "owning task cancelled"
            entry.stop_requested = True
            self._signal(entry, force=False)
            raise
        finally:
            pump_tasks = [
                task for task in (entry.stdout_task, entry.stderr_task) if task is not None
            ]
            await asyncio.gather(*pump_tasks, return_exceptions=True)
            record.returncode = entry.process.returncode
            record.finished_at = time.time()
            if record.cleanup_result == "cleanup_unproven" or record.returncode is None:
                record.state = TerminalState.ORPHANED
                record.termination_reason = (
                    record.termination_reason or "ownership cleanup unproven"
                )
            elif record.timed_out:
                record.state = TerminalState.TIMED_OUT
            elif record.cancelled:
                record.state = TerminalState.CANCELLED
            elif entry.stop_requested:
                record.state = TerminalState.STOPPED
            elif record.returncode == 0:
                record.state = TerminalState.EXITED
            else:
                record.state = TerminalState.FAILED
            if record.returncode is None:
                record.returncode = -1
            self.store.upsert(record)
            self._entries.pop(terminal_id, None)
            self._notify()

    def _signal(self, entry: _RuntimeEntry, *, force: bool) -> None:
        process = entry.process
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                if force:
                    process.kill()
                else:
                    process.send_signal(1)  # Windows CTRL_BREAK_EVENT
            else:
                pgid = entry.record.pgid
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL if force else signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError):
            if force:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass

    async def wait_readiness(
        self,
        terminal_id: str,
        readiness: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Observe a service readiness probe without changing ownership."""

        record = self._records.get(terminal_id)
        if record is None:
            return {"ok": False, "state": "unknown", "error": "unknown terminal"}
        probe = dict(readiness or record.readiness or {})
        if not probe:
            record.ready = None
            return record.result()
        raw_timeout = probe.get("timeout", 30.0)
        try:
            probe_timeout = validate_timeout(raw_timeout, name="readiness timeout")
        except ValueError as exc:
            return {
                **record.result(),
                "ok": False,
                "state": TerminalState.REJECTED.value,
                "error": str(exc),
            }
        if sum(key in probe for key in ("url", "tcp", "marker")) != 1:
            return {
                **record.result(),
                "ok": False,
                "state": TerminalState.REJECTED.value,
                "error": "readiness requires exactly one of url, tcp, or marker",
            }
        record.readiness = _redact_readiness(probe)
        deadline = time.monotonic() + probe_timeout if probe_timeout > 0 else None
        while True:
            if record.state not in _ACTIVE_STATES:
                return record.result()
            evidence = await self._probe_readiness(record, probe)
            if evidence is not None:
                record.ready = True
                record.readiness_evidence = evidence
                self.store.upsert(record)
                self._notify()
                return record.result()
            if deadline is not None and time.monotonic() >= deadline:
                record.ready = False
                record.readiness_evidence = "readiness probe timed out"
                self.store.upsert(record)
                self._notify()
                result = record.result()
                result.update(
                    {
                        "ok": False,
                        "state": "starting_timeout",
                        "readiness_timeout": True,
                        "process_state": record.state.value,
                    }
                )
                return result
            await asyncio.sleep(0.05)

    async def _probe_readiness(
        self,
        record: TerminalRecord,
        probe: Mapping[str, object],
    ) -> str | None:
        marker = probe.get("marker")
        if isinstance(marker, str) and marker and marker in (record.stdout + record.stderr):
            return f"output marker matched: {redact_text(marker)}"
        tcp = probe.get("tcp")
        if isinstance(tcp, Mapping):
            host = tcp.get("host", "127.0.0.1")
            port = tcp.get("port")
            if isinstance(host, str) and isinstance(port, int) and not isinstance(port, bool):
                if not _is_loopback(host) and not bool(probe.get("allow_external", False)):
                    return None
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=0.5
                    )
                    writer.close()
                    await writer.wait_closed()
                    _ = reader
                    return f"tcp connected: {host}:{port}"
                except (OSError, asyncio.TimeoutError):
                    return None
        url = probe.get("url")
        if isinstance(url, str) and url:
            from urllib.parse import urlparse  # noqa: PLC0415

            parsed = urlparse(url)
            host = parsed.hostname
            if host is None or (
                not _is_loopback(host) and not bool(probe.get("allow_external", False))
            ):
                return None
            try:
                import httpx  # noqa: PLC0415

                async with httpx.AsyncClient(follow_redirects=True, timeout=0.5) as client:
                    response = await client.get(url)
                expected = probe.get("status", 200)
                if isinstance(expected, int) and response.status_code == expected:
                    return f"http {response.status_code}: {redact_text(url)}"
                if expected == "any" and response.is_success:
                    return f"http {response.status_code}: {redact_text(url)}"
            except (OSError, ValueError, ImportError):
                return None
            except Exception as exc:  # noqa: BLE001
                if type(exc).__module__.startswith("httpx"):
                    return None
                raise
        return None

    async def wait(self, terminal_id: str, *, timeout: float = 0.0) -> dict[str, object]:
        """Observe one owned terminal without stopping it on observer timeout.

        ``timeout`` belongs to the observation operation.  It never changes
        the process lifecycle; callers must use :meth:`stop` for that.
        """

        try:
            timeout = validate_timeout(timeout)
        except ValueError as exc:
            return {
                "ok": False,
                "background": True,
                "terminal_id": terminal_id,
                "state": TerminalState.REJECTED.value,
                "error": str(exc),
            }

        record = self._records.get(terminal_id)
        if record is None:
            return {
                "ok": False,
                "background": True,
                "terminal_id": terminal_id,
                "error": "unknown terminal",
            }
        if record.session_id != self.session_id:
            return {
                "ok": False,
                "background": True,
                "terminal_id": terminal_id,
                "error": "terminal belongs to another session",
            }
        entry = self._entries.get(terminal_id)
        self._wait_ids.append(terminal_id)
        self._notify()
        started = time.monotonic()
        try:
            if entry is None:
                stop_task = self._stop_tasks.get(terminal_id)
                if stop_task is not None and stop_task is not asyncio.current_task():
                    await asyncio.shield(stop_task)
                return record.result()
            while not entry.completion.done():
                self._notify()
                if timeout > 0 and time.monotonic() - started >= timeout:
                    snapshot = record.result()
                    snapshot.update(
                        {
                            "ok": False,
                            "state": "waiting",
                            "waiting": True,
                            "observer_timeout": timeout,
                            "termination_reason": None,
                        }
                    )
                    return snapshot
                try:
                    await asyncio.wait_for(asyncio.shield(entry.completion), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
            await asyncio.shield(entry.completion)
            stop_task = self._stop_tasks.get(terminal_id)
            if stop_task is not None and stop_task is not asyncio.current_task():
                await asyncio.shield(stop_task)
            return record.result()
        finally:
            try:
                self._wait_ids.remove(terminal_id)
            except ValueError:
                pass
            self._notify()

    async def stop(self, terminal_id: str, *, force: bool = False, reason: str = "user") -> bool:
        """Stop only a process group created by this manager."""

        entry = self._entries.get(terminal_id)
        if entry is None:
            return False
        record = entry.record
        if record.state not in _ACTIVE_STATES:
            return False
        entry.stop_requested = True
        entry.force_requested = force
        record.state = TerminalState.STOPPING
        record.stop_reason = reason
        self.store.upsert(record)
        self._signal(entry, force=force)
        self._notify()
        if not force:
            try:
                await asyncio.wait_for(
                    asyncio.shield(entry.completion), timeout=self.cancel_grace_s
                )
            except asyncio.TimeoutError:
                entry.force_requested = True
                self._signal(entry, force=True)
        try:
            await asyncio.wait_for(
                asyncio.shield(entry.completion), timeout=max(1.0, self.cancel_grace_s)
            )
        except asyncio.TimeoutError:
            record.state = TerminalState.ORPHANED
            record.finished_at = time.time()
            record.stop_reason = "process group did not exit after force stop"
            self.store.upsert(record)
            self._entries.pop(terminal_id, None)
            self._notify()
            return False
        return True

    def request_stop(self, terminal_id: str | None = None, *, force: bool = False) -> bool:
        """Schedule a stop from synchronous command handlers."""

        target = terminal_id or self.current_wait_id()
        if target is None:
            return False
        if target not in self._entries:
            return False
        task = asyncio.create_task(self.stop(target, force=force), name=f"stop-{target}")
        self._stop_tasks[target] = task

        def _forget(completed: asyncio.Task[bool]) -> None:
            if self._stop_tasks.get(target) is completed:
                self._stop_tasks.pop(target, None)

        task.add_done_callback(_forget)
        return True

    def request_stop_current(self, *, force: bool = False) -> bool:
        """Stop the terminal currently owning the foreground wait."""

        return self.request_stop(None, force=force)

    def request_stop_all(self, *, force: bool = False) -> int:
        targets = [record.terminal_id for record in self._visible_records() if record.active]
        for target in targets:
            self.request_stop(target, force=force)
        return len(targets)

    async def close(self) -> None:
        """Gracefully stop every live process owned by this manager."""

        if self._closed:
            return
        self._closed = True
        targets = list(self._entries)
        for terminal_id in targets:
            await self.stop(terminal_id, reason="session closed")
        if self._stop_tasks:
            await asyncio.gather(*self._stop_tasks.values(), return_exceptions=True)


def stop_persisted_session_terminals(
    session_id: str,
    *,
    store_root: str | Path | None = None,
    grace_s: float = 1.0,
) -> int:
    """Stop recorded child groups when a detached parent worker is cancelled.

    The only signal targets are process groups persisted by the terminal
    manager itself and associated with the exact parent session.  This is the
    synchronous bridge used by the PRD-141 supervisor; it never scans PIDs.
    """

    store = TerminalStore(store_root)
    records = [
        record for record in store.records() if record.session_id == session_id and record.active
    ]
    stopped = 0
    for record in records:
        if record.pgid is not None and os.name != "nt":
            try:
                os.killpg(record.pgid, signal.SIGTERM)
                stopped += 1
            except (ProcessLookupError, PermissionError, OSError):
                continue
        elif record.pid is not None:
            try:
                os.kill(record.pid, signal.SIGTERM)
                stopped += 1
            except (ProcessLookupError, PermissionError, OSError):
                continue
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not any(_pid_alive(record.pid) for record in records if record.pid is not None):
            break
        time.sleep(0.05)
    for record in records:
        if record.pgid is not None and os.name != "nt" and _pid_alive(record.pid):
            try:
                os.killpg(record.pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        alive = _pid_alive(record.pid)
        record.state = TerminalState.ORPHANED if alive else TerminalState.STOPPED
        record.finished_at = time.time()
        record.returncode = -15
        record.stop_reason = (
            "parent background session cancellation cleanup unproven"
            if alive
            else "parent background session cancelled"
        )
        record.termination_reason = record.stop_reason
        record.cleanup_result = "cleanup_unproven" if alive else "force_session_cancel"
        store.upsert(record)
    return stopped


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True
