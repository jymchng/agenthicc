"""Schedule definition operations and occurrence admission policy."""

from __future__ import annotations

import getpass
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from agenthicc.schedules.model import (
    CoalescePolicy,
    Occurrence,
    OccurrenceState,
    RecurrenceKind,
    Schedule,
)
from agenthicc.schedules.recurrence import first_run_at, validate_recurrence
from agenthicc.schedules.store import ScheduleStore


_SESSION_NAMESPACE = uuid.UUID("5a2df5fa-56d3-4cc4-90e5-1e3f6e82b6a9")


def occurrence_session_id(occurrence_id: str) -> str:
    """Stable background-session identity, enabling replay-safe dispatch."""

    return uuid.uuid5(_SESSION_NAMESPACE, occurrence_id).hex


class ScheduleManager:
    """Create and control repository schedules without executing model work."""

    def __init__(self, store: ScheduleStore, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def create(
        self,
        *,
        repository_root: Path | str,
        workflow_name: str,
        prompt: str,
        profile_name: str,
        permission_ceiling: str,
        recurrence_kind: RecurrenceKind,
        recurrence_spec: str,
        timezone: str,
        coalesce_policy: CoalescePolicy = CoalescePolicy.SKIP,
        max_in_flight: int = 1,
        creator: str | None = None,
        config_path: str = "",
    ) -> Schedule:
        now = self.clock()
        root = str(Path(repository_root).expanduser().resolve())
        from agenthicc.tui.runtime.mode_manager import canonical_mode_name  # noqa: PLC0415

        permission_ceiling = canonical_mode_name(permission_ceiling)
        if permission_ceiling not in {"Safe", "Plan", "Yolo"}:
            raise ValueError("permission ceiling must be Safe, Plan, or Yolo")
        validate_recurrence(recurrence_kind, recurrence_spec, timezone)
        first_run = first_run_at(recurrence_kind, recurrence_spec, timezone, now)
        schedule = Schedule(
            schedule_id=uuid.uuid4().hex,
            repository_root=root,
            workflow_name=workflow_name,
            prompt=prompt,
            profile_name=profile_name,
            permission_ceiling=permission_ceiling,
            recurrence_kind=recurrence_kind,
            recurrence_spec=recurrence_spec,
            timezone=timezone,
            coalesce_policy=coalesce_policy,
            max_in_flight=max_in_flight,
            next_run_at=first_run,
            enabled=False,
            creator=(creator or getpass.getuser())[:256],
            revision=1,
            created_at=now,
            updated_at=now,
            config_path=str(Path(config_path).expanduser().resolve()) if config_path else "",
        )
        return self.store.create(schedule)

    def pause(self, schedule_id: str, *, actor: str = "cli") -> Schedule:
        schedule = self.store.get(schedule_id)
        if not schedule.enabled:
            return schedule
        return self.store.revise(
            schedule_id,
            expected_revision=schedule.revision,
            action="paused",
            actor=actor,
            now=self.clock(),
            enabled=False,
        )

    def resume(
        self,
        schedule_id: str,
        *,
        confirm_next_run: str,
        actor: str = "cli",
    ) -> Schedule:
        from agenthicc.schedules.recurrence import format_timestamp  # noqa: PLC0415

        schedule = self.store.get(schedule_id)
        if schedule.recurrence_kind is RecurrenceKind.ONCE and schedule.next_run_at is None:
            raise ValueError("one-shot schedule has already run and cannot be resumed")
        expected = format_timestamp(schedule.next_run_at, schedule.timezone)
        if not confirm_next_run or confirm_next_run != expected:
            raise ValueError(
                "schedule remains paused; confirm the displayed next run with "
                f"--confirm-next-run {expected!r}"
            )
        if schedule.enabled:
            return schedule
        return self.store.revise(
            schedule_id,
            expected_revision=schedule.revision,
            action="resumed",
            actor=actor,
            now=self.clock(),
            enabled=True,
        )

    def delete(self, schedule_id: str, *, actor: str = "cli") -> Schedule:
        schedule = self.store.get(schedule_id)
        return self.store.revise(
            schedule_id,
            expected_revision=schedule.revision,
            action="deleted",
            actor=actor,
            now=self.clock(),
            enabled=False,
            deleted_at=self.clock(),
        )

    def update(
        self,
        schedule_id: str,
        *,
        actor: str = "cli",
        changes: Mapping[str, object],
    ) -> Schedule:
        """Revise a definition, pause it, and recompute its next-run preview.

        Any edit invalidates the previous confirmation. The updated revision is
        always paused; callers must explicitly resume it after reviewing the
        exact new next-run timestamp.
        """

        schedule = self.store.get(schedule_id)
        normalized_changes = dict(changes)
        normalized_changes.pop("confirm", None)
        allowed = {
            "workflow_name",
            "prompt",
            "profile_name",
            "permission_ceiling",
            "recurrence_kind",
            "recurrence_spec",
            "timezone",
            "coalesce_policy",
            "max_in_flight",
            "config_path",
        }
        unsupported = set(normalized_changes) - allowed
        if unsupported:
            raise ValueError("unsupported schedule fields: " + ", ".join(sorted(unsupported)))
        kind = normalized_changes.get("recurrence_kind", schedule.recurrence_kind)
        spec = normalized_changes.get("recurrence_spec", schedule.recurrence_spec)
        zone = normalized_changes.get("timezone", schedule.timezone)
        if (
            not isinstance(kind, RecurrenceKind)
            or not isinstance(spec, str)
            or not isinstance(zone, str)
        ):
            raise ValueError("invalid recurrence update")
        validate_recurrence(kind, spec, zone)
        if "permission_ceiling" in normalized_changes:
            from agenthicc.tui.runtime.mode_manager import canonical_mode_name  # noqa: PLC0415

            mode = canonical_mode_name(str(normalized_changes["permission_ceiling"]))
            if mode not in {"Safe", "Plan", "Yolo"}:
                raise ValueError("permission ceiling must be Safe, Plan, or Yolo")
            normalized_changes["permission_ceiling"] = mode
        if "max_in_flight" in normalized_changes:
            value = normalized_changes["max_in_flight"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("max_in_flight must be an integer")
        if "config_path" in normalized_changes:
            value = normalized_changes["config_path"]
            if not isinstance(value, str):
                raise ValueError("config_path must be a string")
            normalized_changes["config_path"] = (
                str(Path(value).expanduser().resolve()) if value else ""
            )
        next_run = first_run_at(kind, spec, zone, self.clock())
        normalized_changes["next_run_at"] = next_run
        normalized_changes["enabled"] = False
        return self.store.revise(
            schedule_id,
            expected_revision=schedule.revision,
            action="updated",
            actor=actor,
            now=self.clock(),
            **normalized_changes,
        )

    def run_now(self, schedule_id: str, *, actor: str = "cli") -> Occurrence:
        schedule = self.store.get(schedule_id)
        occurrence_id = uuid.uuid4().hex
        session_id = occurrence_session_id(occurrence_id)
        now = self.clock()
        return self.store.create_manual_occurrence(
            schedule=schedule,
            occurrence_id=occurrence_id,
            occurrence_key=f"manual:{occurrence_id}",
            scheduled_for=now,
            session_id=session_id,
            now=now,
            actor=actor,
            receipt={
                "accepted": True,
                "accepted_at": now,
            },
        )

    def retry(self, schedule_id: str, occurrence_id: str, *, actor: str = "cli") -> Occurrence:
        original = self.store.get_occurrence(occurrence_id)
        if original.schedule_id != schedule_id:
            raise ValueError("occurrence does not belong to this schedule")
        if original.state not in {OccurrenceState.FAILED, OccurrenceState.BLOCKED}:
            raise ValueError("only failed or blocked occurrences may be retried")
        snapshot = self.store.get_revision(schedule_id, original.schedule_revision)
        if snapshot.deleted:
            raise ValueError("cannot retry an occurrence from a deleted schedule")
        occurrence_id_new = uuid.uuid4().hex
        session_id = occurrence_session_id(occurrence_id_new)
        now = self.clock()
        return self.store.create_occurrence(
            schedule=snapshot,
            occurrence_id=occurrence_id_new,
            occurrence_key=f"retry:{occurrence_id_new}",
            kind="retry",
            scheduled_for=now,
            session_id=session_id,
            state=OccurrenceState.AWAITING_SERVICE,
            now=now,
            retry_of=occurrence_id,
            attempt=original.attempt + 1,
            receipt={
                "accepted": True,
                "accepted_at": now,
                "accepted_by": actor,
                "retry_of": occurrence_id,
                "session_id": session_id,
            },
        )


def occurrence_status_payload(occurrence: Occurrence) -> dict[str, object]:
    """Bounded machine-readable receipt safe for CLI inspection."""

    return {
        "occurrence_id": occurrence.occurrence_id,
        "schedule_id": occurrence.schedule_id,
        "schedule_revision": occurrence.schedule_revision,
        "kind": occurrence.kind,
        "state": occurrence.state.value,
        "scheduled_for": occurrence.scheduled_for,
        "session_id": occurrence.session_id,
        "base_commit": occurrence.base_commit,
        "worktree": dict(occurrence.worktree) if occurrence.worktree is not None else None,
        "retry_of": occurrence.retry_of,
        "attempt": occurrence.attempt,
        "coalesced_count": occurrence.coalesced_count,
        "error": occurrence.error[:1000],
        "receipt": dict(occurrence.receipt or {}),
        "outcome": dict(occurrence.outcome or {}),
    }
