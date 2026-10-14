"""Typed durable records for repository-scoped scheduled workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from pathlib import Path
from typing import Mapping


class RecurrenceKind(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class CoalescePolicy(StrEnum):
    SKIP = "skip"
    QUEUE_ONE = "queue_one"
    ALLOW_BOUNDED = "allow_bounded"


class OccurrenceState(StrEnum):
    AWAITING_SERVICE = "awaiting_service"
    QUEUED = "queued"
    PREPARING = "preparing"
    WAITING_CAPACITY = "waiting_capacity"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


_IN_FLIGHT = frozenset(
    {
        OccurrenceState.AWAITING_SERVICE.value,
        OccurrenceState.QUEUED.value,
        OccurrenceState.PREPARING.value,
        OccurrenceState.WAITING_CAPACITY.value,
        OccurrenceState.RUNNING.value,
    }
)


def _text(value: object, field: str, *, required: bool = False, limit: int = 16_384) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{field} must be a {'non-empty ' if required else ''}string")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field} exceeds {limit} UTF-8 bytes")
    return value


@dataclass(frozen=True, slots=True)
class Schedule:
    """One versioned schedule definition.

    ``prompt`` is intentionally retained exactly (including surrounding
    whitespace); validation checks that it contains non-whitespace text.
    Provider credentials are never part of this record.
    """

    schedule_id: str
    repository_root: str
    workflow_name: str
    prompt: str
    profile_name: str
    permission_ceiling: str
    recurrence_kind: RecurrenceKind
    recurrence_spec: str
    timezone: str
    coalesce_policy: CoalescePolicy
    max_in_flight: int
    next_run_at: float | None
    enabled: bool
    creator: str
    revision: int
    created_at: float
    updated_at: float
    config_path: str = ""
    deleted_at: float | None = None

    def __post_init__(self) -> None:
        _text(self.schedule_id, "schedule_id", required=True, limit=128)
        root = _text(self.repository_root, "repository_root", required=True, limit=4096)
        if str(Path(root).expanduser().resolve()) != root:
            raise ValueError("repository_root must be an absolute canonical path")
        _text(self.workflow_name, "workflow_name", required=True, limit=128)
        _text(self.prompt, "prompt", required=True)
        _text(self.profile_name, "profile_name", required=True, limit=128)
        _text(self.permission_ceiling, "permission_ceiling", required=True, limit=128)
        if self.permission_ceiling not in {"Safe", "Plan", "Yolo"}:
            raise ValueError("permission_ceiling must be canonical Safe, Plan, or Yolo")
        _text(self.recurrence_spec, "recurrence_spec", required=True, limit=512)
        _text(self.timezone, "timezone", required=True, limit=128)
        _text(self.creator, "creator", required=True, limit=256)
        _text(self.config_path, "config_path", limit=4096)
        if (
            isinstance(self.max_in_flight, bool)
            or not isinstance(self.max_in_flight, int)
            or self.max_in_flight < 1
            or self.max_in_flight > 32
        ):
            raise ValueError("max_in_flight must be between 1 and 32")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("revision must be positive")
        timestamps = (self.created_at, self.updated_at)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in timestamps
        ):
            raise ValueError("schedule timestamps must be finite numbers")
        if self.next_run_at is not None and (
            isinstance(self.next_run_at, bool)
            or not isinstance(self.next_run_at, (int, float))
            or not math.isfinite(self.next_run_at)
        ):
            raise ValueError("next_run_at must be a finite number or None")
        if self.deleted_at is not None and (
            isinstance(self.deleted_at, bool)
            or not isinstance(self.deleted_at, (int, float))
            or not math.isfinite(self.deleted_at)
        ):
            raise ValueError("deleted_at must be a finite number or None")
        if not isinstance(self.recurrence_kind, RecurrenceKind):
            raise ValueError("unsupported recurrence kind")
        if not isinstance(self.coalesce_policy, CoalescePolicy):
            raise ValueError("unsupported coalescing policy")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "schedule_id": self.schedule_id,
            "repository_root": self.repository_root,
            "workflow_name": self.workflow_name,
            "prompt": self.prompt,
            "profile_name": self.profile_name,
            "permission_ceiling": self.permission_ceiling,
            "recurrence_kind": self.recurrence_kind.value,
            "recurrence_spec": self.recurrence_spec,
            "timezone": self.timezone,
            "coalesce_policy": self.coalesce_policy.value,
            "max_in_flight": self.max_in_flight,
            "next_run_at": self.next_run_at,
            "enabled": self.enabled,
            "creator": self.creator,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config_path": self.config_path,
            "deleted_at": self.deleted_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Schedule:
        def number(name: str, optional: bool = False) -> float | None:
            value = raw.get(name)
            if optional and value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"{name} must be numeric")
            return float(value)

        def required_number(name: str) -> float:
            value = number(name)
            if value is None:
                raise ValueError(f"{name} must be numeric")
            return value

        def integer(name: str) -> int:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            return value

        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        try:
            raw_kind = raw.get("recurrence_kind")
            recurrence_kind = (
                raw_kind
                if isinstance(raw_kind, RecurrenceKind)
                else RecurrenceKind(_text(raw_kind, "recurrence_kind"))
            )
            raw_policy = raw.get("coalesce_policy")
            coalesce_policy = (
                raw_policy
                if isinstance(raw_policy, CoalescePolicy)
                else CoalescePolicy(_text(raw_policy, "coalesce_policy"))
            )
            return cls(
                schedule_id=_text(raw.get("schedule_id"), "schedule_id", required=True),
                repository_root=_text(
                    raw.get("repository_root"), "repository_root", required=True, limit=4096
                ),
                workflow_name=_text(raw.get("workflow_name"), "workflow_name", required=True),
                prompt=_text(raw.get("prompt"), "prompt", required=True),
                profile_name=_text(raw.get("profile_name"), "profile_name", required=True),
                permission_ceiling=_text(
                    raw.get("permission_ceiling"), "permission_ceiling", required=True
                ),
                recurrence_kind=recurrence_kind,
                recurrence_spec=_text(raw.get("recurrence_spec"), "recurrence_spec", required=True),
                timezone=_text(raw.get("timezone"), "timezone", required=True),
                coalesce_policy=coalesce_policy,
                max_in_flight=integer("max_in_flight"),
                next_run_at=number("next_run_at", optional=True),
                enabled=enabled,
                creator=_text(raw.get("creator"), "creator", required=True),
                revision=integer("revision"),
                created_at=required_number("created_at"),
                updated_at=required_number("updated_at"),
                config_path=_text(raw.get("config_path", ""), "config_path", limit=4096),
                deleted_at=number("deleted_at", optional=True),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid schedule: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Occurrence:
    """Immutable identity plus mutable receipt state for one schedule run."""

    occurrence_id: str
    schedule_id: str
    schedule_revision: int
    occurrence_key: str
    kind: str
    scheduled_for: float
    created_at: float
    updated_at: float
    state: OccurrenceState
    session_id: str = ""
    worktree: Mapping[str, object] | None = None
    base_commit: str = ""
    retry_of: str = ""
    coalesced_count: int = 0
    attempt: int = 1
    receipt: Mapping[str, object] | None = None
    outcome: Mapping[str, object] | None = None
    error: str = ""

    @property
    def in_flight(self) -> bool:
        return self.state.value in _IN_FLIGHT

    def to_dict(self) -> dict[str, object]:
        return {
            "occurrence_id": self.occurrence_id,
            "schedule_id": self.schedule_id,
            "schedule_revision": self.schedule_revision,
            "occurrence_key": self.occurrence_key,
            "kind": self.kind,
            "scheduled_for": self.scheduled_for,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state.value,
            "session_id": self.session_id,
            "worktree": dict(self.worktree) if self.worktree is not None else None,
            "base_commit": self.base_commit,
            "retry_of": self.retry_of,
            "coalesced_count": self.coalesced_count,
            "attempt": self.attempt,
            "receipt": dict(self.receipt) if self.receipt is not None else None,
            "outcome": dict(self.outcome) if self.outcome is not None else None,
            "error": self.error,
        }
