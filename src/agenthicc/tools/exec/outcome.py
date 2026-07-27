"""Shared command outcome and deadline contracts (PRD-151).

The execution tools intentionally serialize this small internal model at their
boundary.  Keeping the state calculation in one place prevents a subprocess
mapping with ``returncode=1`` from being mistaken for a successful coroutine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

__all__ = [
    "CommandKind",
    "CommandOutcome",
    "CommandState",
    "Deadline",
    "invalid_timeout_result",
    "resolve_deadline",
    "validate_timeout",
]


class CommandState(StrEnum):
    """Authoritative lifecycle states for one command invocation."""

    EXITED = "exited"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SPAWN_FAILED = "spawn_failed"
    REJECTED = "rejected"
    RUNNING = "running"
    WAITING = "waiting"
    ORPHANED = "orphaned"


class CommandKind(StrEnum):
    """Supported command invocation kinds."""

    SHELL = "shell"
    EXEC = "exec"


@dataclass(frozen=True, slots=True)
class Deadline:
    """The requested and effective operation deadline."""

    requested_s: float
    effective_s: float | None
    owner: str | None = None

    def to_dict(self) -> dict[str, object] | None:
        if self.effective_s is None:
            return None
        return {
            "requested_s": self.requested_s,
            "effective_s": self.effective_s,
            "owner": self.owner or "command",
        }


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """JSON-safe, canonical result for a finite command."""

    state: CommandState
    command_kind: CommandKind
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    termination_reason: str | None = None
    deadline: Deadline | None = None
    cleanup_result: str = "not_required"
    cancellation_source: str | None = None
    spawn_failure: str | None = None
    command: str | None = None
    argv: tuple[str, ...] | None = None
    cwd: str | None = None

    @property
    def ok(self) -> bool:
        """Return true only for a normal zero-exit completion."""

        return self.state is CommandState.EXITED and self.returncode == 0

    def to_dict(self) -> dict[str, object]:
        """Serialize the outcome while retaining legacy output fields."""

        result: dict[str, object] = {
            "ok": self.ok,
            "state": self.state.value,
            "command_kind": self.command_kind.value,
            "returncode": self.returncode if self.returncode is not None else -1,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "termination_reason": self.termination_reason,
            "deadline": self.deadline.to_dict() if self.deadline else None,
            "cleanup_result": self.cleanup_result,
            "cancellation_source": self.cancellation_source,
            "spawn_failure": self.spawn_failure,
        }
        if self.command is not None:
            result["command"] = self.command
        if self.argv is not None:
            result["argv"] = list(self.argv)
        if self.cwd is not None:
            result["cwd"] = self.cwd
        return result


def validate_timeout(value: object, *, name: str = "timeout") -> float:
    """Validate a public timeout measured in seconds.

    Zero deliberately means no deadline for the named operation.  Enclosing
    turn/session policies remain separate and are represented by their own
    deadline owner when they stop a command.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite, non-negative number of seconds")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"{name} must be a finite, non-negative number of seconds")
    return timeout


def resolve_deadline(
    requested_s: float,
    *,
    configured_s: float | None = None,
    configured_owner: str = "configured_ceiling",
) -> Deadline:
    """Choose the earliest positive deadline and record its owner."""

    requested = validate_timeout(requested_s)
    candidates: list[tuple[float, str]] = []
    if requested > 0:
        candidates.append((requested, "command"))
    if configured_s is not None:
        configured = validate_timeout(configured_s, name="configured timeout")
        if configured > 0:
            candidates.append((configured, configured_owner))
    if not candidates:
        return Deadline(requested_s=requested, effective_s=None, owner=None)
    effective, owner = min(candidates, key=lambda item: item[0])
    return Deadline(requested_s=requested, effective_s=effective, owner=owner)


def invalid_timeout_result(value: object, *, command_kind: CommandKind) -> dict[str, object]:
    """Return a structured pre-spawn validation failure."""

    try:
        validate_timeout(value)
        message = "invalid timeout"
    except ValueError as exc:
        message = str(exc)
    return {
        "ok": False,
        "state": CommandState.REJECTED.value,
        "command_kind": command_kind.value,
        "returncode": -1,
        "stdout": "",
        "stderr": "",
        "duration_ms": 0.0,
        "timed_out": False,
        "cancelled": False,
        "truncated": False,
        "termination_reason": message,
        "error": message,
        "deadline": None,
        "cleanup_result": "not_spawned",
    }


def outcome_from_mapping(raw: Mapping[str, object]) -> CommandOutcome | None:
    """Build an outcome from a serialized process-shaped mapping if possible."""

    state_raw = raw.get("state")
    returncode = raw.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        returncode = None
    if state_raw is None and returncode is None and "timed_out" not in raw:
        return None
    try:
        state = CommandState(str(state_raw)) if state_raw is not None else None
    except ValueError:
        state = None
    timed_out = bool(raw.get("timed_out", False))
    cancelled = bool(raw.get("cancelled", False))
    if state is None:
        if timed_out:
            state = CommandState.TIMED_OUT
        elif cancelled:
            state = CommandState.CANCELLED
        elif returncode == 0:
            state = CommandState.EXITED
        else:
            state = CommandState.FAILED
    kind_raw = str(raw.get("command_kind", CommandKind.SHELL.value))
    try:
        kind = CommandKind(kind_raw)
    except ValueError:
        kind = CommandKind.SHELL
    stdout = raw.get("stdout", "")
    stderr = raw.get("stderr", "")
    duration_raw = raw.get("duration_ms", 0.0)
    duration = (
        float(duration_raw)
        if isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool)
        else 0.0
    )
    termination_raw = raw.get("termination_reason")
    cleanup_raw = raw.get("cleanup_result")
    spawn_raw = raw.get("spawn_failure")
    return CommandOutcome(
        state=state,
        command_kind=kind,
        returncode=returncode,
        stdout=stdout if isinstance(stdout, str) else "",
        stderr=stderr if isinstance(stderr, str) else "",
        duration_ms=duration,
        timed_out=timed_out or state is CommandState.TIMED_OUT,
        cancelled=cancelled or state is CommandState.CANCELLED,
        truncated=bool(raw.get("truncated", False)),
        termination_reason=termination_raw if isinstance(termination_raw, str) else None,
        cleanup_result=cleanup_raw if isinstance(cleanup_raw, str) else "not_required",
        spawn_failure=spawn_raw if isinstance(spawn_raw, str) else None,
    )
