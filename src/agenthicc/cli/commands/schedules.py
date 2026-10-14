"""Repository-owned scheduled workflow operations (PRD-228)."""

from __future__ import annotations

import json as json_module
import os
from pathlib import Path

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group
from agenthicc.schedules import (
    CoalescePolicy,
    OccurrenceState,
    RecurrenceKind,
    ScheduleManager,
    ScheduleNotFound,
    ScheduleServiceLease,
    ScheduleStore,
    format_timestamp,
    parse_recurrence,
)
from agenthicc.schedules.manager import occurrence_status_payload


@group("schedules", help="Manage repository-scoped scheduled workflows")
def _schedules_group() -> None: ...


def _store() -> ScheduleStore:
    return ScheduleStore()


def _repository(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _effective_config_path(ctx: CLIContext, repository: Path) -> str:
    if ctx.config_path:
        return str(Path(ctx.config_path).expanduser().resolve())
    environment_config = os.environ.get("AGENTHICC_CONFIG", "").strip()
    if environment_config:
        return str(Path(environment_config).expanduser().resolve())
    project_config = repository / ".agenthicc" / "agenthicc.toml"
    return str(project_config.resolve()) if project_config.is_file() else ""


def _validate_workflow_and_profile(
    ctx: CLIContext,
    repository: Path,
    workflow_name: str,
    profile_name: str,
    *,
    config_path: str | None = None,
) -> None:
    from agenthicc.config import load_config  # noqa: PLC0415
    from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415

    registry = build_workflow_registry(
        project_dir=repository / ".agenthicc",
        user_dir=Path.home() / ".agenthicc",
    )
    if registry.get(workflow_name) is None:
        raise ValueError(f"workflow {workflow_name!r} is not registered for {repository}")
    selected_config_path = config_path if config_path is not None else ctx.config_path
    config = load_config(
        project_path=repository / ".agenthicc" / "agenthicc.toml",
        config_path=selected_config_path,
        cli_overrides=list(ctx.set_overrides),
        cli_secret_overrides=list(ctx.set_secret_overrides),
    )
    if profile_name not in config.providers:
        available = ", ".join(sorted(config.providers)) or "none"
        raise ValueError(
            f"unknown provider profile {profile_name!r}; configured profiles: {available}"
        )


def _parse_recurrence(value: str) -> tuple[RecurrenceKind, str]:
    return parse_recurrence(value)


@command("schedules", help="List repository schedules; use subcommands to manage them")
def schedules_root(ctx: CLIContext) -> None:
    schedules_list(ctx)


@command("schedules", "list", help="List schedules for a repository")
def schedules_list(
    ctx: CLIContext,
    repository_root: str = ".",
    all_: bool = False,
    json: bool = False,
) -> None:
    """List only the current repository unless ``--all`` is selected."""

    store = _store()
    repository = None if all_ else str(_repository(repository_root))
    records = store.list(repository_root=repository)
    service = ScheduleServiceLease.is_running(store)
    payload: list[dict[str, object]] = []
    for schedule in records:
        if schedule.deleted:
            continue
        history = store.history(schedule.schedule_id, limit=1)
        if not schedule.enabled:
            status = "paused"
        elif schedule.recurrence_kind is RecurrenceKind.ONCE and schedule.next_run_at is None:
            status = "completed"
        elif service is True:
            status = (
                "blocked" if history and history[0].state is OccurrenceState.BLOCKED else "armed"
            )
        else:
            status = "awaiting service"
        payload.append(
            {
                "schedule_id": schedule.schedule_id,
                "repository_root": schedule.repository_root,
                "workflow": schedule.workflow_name,
                "recurrence": f"{schedule.recurrence_kind.value}:{schedule.recurrence_spec}",
                "timezone": schedule.timezone,
                "next_run": schedule.next_run_at,
                "next_run_local": format_timestamp(schedule.next_run_at, schedule.timezone),
                "enabled": schedule.enabled,
                "status": status,
                "revision": schedule.revision,
                "profile": schedule.profile_name,
                "permission_ceiling": schedule.permission_ceiling,
                "mutating": schedule.permission_ceiling.casefold() != "plan",
                "coalesce_policy": schedule.coalesce_policy.value,
            }
        )
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload:
        print("No scheduled workflows for this repository.")
        print(
            "Create one with: agenthicc schedules create WORKFLOW PROFILE MODE RECURRENCE TIMEZONE PROMPT"
        )
        return
    print(
        "ID                STATE            WORKFLOW             NEXT RUN                         ZONE       POLICY"
    )
    for item in payload:
        print(
            f"{str(item['schedule_id'])[:16]:16}  {str(item['status']):15}  "
            f"{str(item['workflow'])[:20]:20}  {str(item['next_run_local']):30}  "
            f"{str(item['timezone']):10} {item['coalesce_policy']}"
        )
    if service is not True:
        print("Scheduler service is not running; enabled schedules are awaiting service.")


@command("schedules", "create", help="Create a disabled repository schedule")
def schedules_create(
    ctx: CLIContext,
    workflow_name: str,
    profile_name: str,
    permission_ceiling: str,
    recurrence: str,
    timezone: str,
    prompt: str,
    repository_root: str = ".",
    coalesce_policy: str = "skip",
    max_in_flight: int = 1,
) -> None:
    """Create a disabled definition and print its first-run preview.

    Recurrence is ``once:2026-10-15T09:00``, ``interval:6h`` or a quoted
    ``cron:0 9 * * 1-5``. Schedules are created disabled; enabling is a
    separate confirmation against the exact displayed next-run timestamp.
    """

    repository = _repository(repository_root)
    try:
        _validate_workflow_and_profile(ctx, repository, workflow_name, profile_name)
        from agenthicc.tui.runtime.mode_manager import canonical_mode_name  # noqa: PLC0415

        mode = canonical_mode_name(permission_ceiling)
        if mode not in {"Safe", "Plan", "Yolo"}:
            raise ValueError("permission ceiling must be Safe, Plan, or Yolo")
        kind, spec = _parse_recurrence(recurrence)
        policy = CoalescePolicy(coalesce_policy)
        manager = ScheduleManager(_store())
        schedule = manager.create(
            repository_root=repository,
            workflow_name=workflow_name,
            prompt=prompt,
            profile_name=profile_name,
            permission_ceiling=mode,
            recurrence_kind=kind,
            recurrence_spec=spec,
            timezone=timezone,
            coalesce_policy=policy,
            max_in_flight=max_in_flight,
            config_path=_effective_config_path(ctx, repository),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to create schedule: {exc}") from exc
    print(f"Created disabled schedule {schedule.schedule_id} (revision {schedule.revision}).")
    print(
        f"Workflow: {schedule.workflow_name}; profile: {schedule.profile_name}; policy ceiling: {schedule.permission_ceiling}"
    )
    print(
        f"First execution preview: {format_timestamp(schedule.next_run_at, schedule.timezone)} [{schedule.timezone}]"
    )
    print(
        "Review the preview, then enable with schedules resume ID --confirm-next-run '<timestamp>'."
    )
    if mode != "Plan":
        print("Mutating-capable runs will use a fresh isolated worktree and will never auto-merge.")


@command("schedules", "show", help="Inspect a schedule and its recent run history")
def schedules_show(ctx: CLIContext, schedule_id: str, json: bool = False) -> None:
    store = _store()
    try:
        schedule = store.get(schedule_id, include_deleted=True)
    except ScheduleNotFound:
        raise SystemExit(f"Schedule not found: {schedule_id}") from None
    payload = schedule.to_dict()
    payload["service_running"] = ScheduleServiceLease.is_running(store)
    payload["history"] = [occurrence_status_payload(item) for item in store.history(schedule_id)]
    payload["revisions"] = store.revisions(schedule_id)
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Schedule: {schedule.schedule_id}  revision {schedule.revision}")
    print(f"Repository: {schedule.repository_root}")
    print(f"Workflow: {schedule.workflow_name}  Profile: {schedule.profile_name}")
    print(f"Permission ceiling: {schedule.permission_ceiling}")
    print(
        f"Recurrence: {schedule.recurrence_kind.value}:{schedule.recurrence_spec} [{schedule.timezone}]"
    )
    print(f"Next run: {format_timestamp(schedule.next_run_at, schedule.timezone)}")
    print(
        f"State: {'deleted' if schedule.deleted else 'enabled' if schedule.enabled else 'paused'}"
    )
    print(f"Prompt: {schedule.prompt}")
    print("Recent occurrences:")
    for occurrence in store.history(schedule_id, limit=20):
        print(
            f"  {occurrence.occurrence_id[:14]} {occurrence.state.value:18} "
            f"{format_timestamp(occurrence.scheduled_for, schedule.timezone)} "
            f"session={occurrence.session_id or '—'}"
        )


@command("schedules", "pause", help="Pause future occurrences")
def schedules_pause(ctx: CLIContext, schedule_id: str) -> None:
    try:
        schedule = ScheduleManager(_store()).pause(schedule_id)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to pause schedule: {exc}") from exc
    print(f"Schedule {schedule.schedule_id} paused at revision {schedule.revision}.")


@command("schedules", "resume", help="Resume after confirming its next execution time")
def schedules_resume(ctx: CLIContext, schedule_id: str, confirm_next_run: str = "") -> None:
    manager = ScheduleManager(_store())
    try:
        schedule = manager.store.get(schedule_id)
        expected = format_timestamp(schedule.next_run_at, schedule.timezone)
        if not confirm_next_run:
            print(f"Next execution preview: {expected} [{schedule.timezone}]")
            print(f"Re-run with --confirm-next-run {expected!r} to enable this schedule.")
            return
        resumed = manager.resume(schedule_id, confirm_next_run=confirm_next_run)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to resume schedule: {exc}") from exc
    print(f"Schedule {resumed.schedule_id} enabled at revision {resumed.revision}.")


@command("schedules", "run-now", help="Queue an auditable immediate occurrence")
def schedules_run_now(ctx: CLIContext, schedule_id: str, json: bool = False) -> None:
    try:
        occurrence = ScheduleManager(_store()).run_now(schedule_id)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to queue schedule run: {exc}") from exc
    payload = occurrence_status_payload(occurrence)
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Run occurrence {occurrence.occurrence_id} accepted as {occurrence.state.value}; "
            f"session {occurrence.session_id}. The schedule's recurrence cursor is unchanged."
        )


@command("schedules", "retry", help="Retry a failed occurrence as a new auditable attempt")
def schedules_retry(
    ctx: CLIContext, schedule_id: str, occurrence_id: str, json: bool = False
) -> None:
    try:
        occurrence = ScheduleManager(_store()).retry(schedule_id, occurrence_id)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to retry occurrence: {exc}") from exc
    payload = occurrence_status_payload(occurrence)
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Retry accepted as occurrence {occurrence.occurrence_id} "
            f"(attempt {occurrence.attempt}, session {occurrence.session_id})."
        )


@command("schedules", "update", help="Revise a schedule after explicit confirmation")
def schedules_update(
    ctx: CLIContext,
    schedule_id: str,
    field: str,
    value: str,
    confirm: bool = False,
) -> None:
    if not confirm:
        raise SystemExit("Schedule updates are disabled without --confirm; no changes were made.")
    try:
        changes: dict[str, object]
        if field == "recurrence":
            kind, spec = _parse_recurrence(value)
            changes = {"recurrence_kind": kind, "recurrence_spec": spec}
        elif field == "coalesce_policy":
            changes = {field: CoalescePolicy(value)}
        elif field == "max_in_flight":
            changes = {field: int(value)}
        elif field in {
            "workflow_name",
            "prompt",
            "profile_name",
            "permission_ceiling",
            "timezone",
            "config_path",
        }:
            changes = {field: value}
        else:
            raise ValueError(
                "unsupported field; allowed: workflow_name, prompt, profile_name, "
                "permission_ceiling, recurrence, timezone, coalesce_policy, "
                "max_in_flight, config_path"
            )
        manager = ScheduleManager(_store())
        current = manager.store.get(schedule_id)
        workflow_name = str(changes.get("workflow_name", current.workflow_name))
        profile_name = str(changes.get("profile_name", current.profile_name))
        repository = Path(current.repository_root)
        config_path = str(changes.get("config_path", current.config_path))
        if not config_path:
            config_path = _effective_config_path(ctx, repository)
        _validate_workflow_and_profile(
            ctx,
            repository,
            workflow_name,
            profile_name,
            config_path=config_path or None,
        )
        if "permission_ceiling" in changes:
            from agenthicc.tui.runtime.mode_manager import canonical_mode_name  # noqa: PLC0415

            mode = canonical_mode_name(str(changes["permission_ceiling"]))
            if mode not in {"Safe", "Plan", "Yolo"}:
                raise ValueError("permission ceiling must be Safe, Plan, or Yolo")
            changes["permission_ceiling"] = mode
        updated = manager.update(schedule_id, changes=changes)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to update schedule: {exc}") from exc
    print(f"Schedule {updated.schedule_id} updated to revision {updated.revision}.")
    print(
        f"Next execution preview: {format_timestamp(updated.next_run_at, updated.timezone)} [{updated.timezone}]"
    )
    print("The edited schedule is paused. Review the preview, then resume with confirmation.")


@command("schedules", "delete", help="Stop future occurrences and tombstone the definition")
def schedules_delete(ctx: CLIContext, schedule_id: str, confirm: bool = False) -> None:
    if not confirm:
        raise SystemExit("Schedule deletion requires --confirm; no changes were made.")
    try:
        schedule = ScheduleManager(_store()).delete(schedule_id)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to delete schedule: {exc}") from exc
    print(
        f"Schedule {schedule.schedule_id} deleted at revision {schedule.revision}; "
        "future ticks stopped. Run history, sessions, and worktree artifacts are preserved."
    )


@command("schedules", "service-status", help="Show whether schedule execution is armed")
def schedules_service_status(ctx: CLIContext) -> None:
    running = ScheduleServiceLease.is_running(_store())
    if running is True:
        print("Schedule service is running; enabled repository schedules are armed.")
    elif running is False:
        print("Schedule service is not running; enabled schedules are awaiting service.")
        print("Start it with: agenthicc session serve")
    else:
        print("Schedule service ownership could not be verified; schedules remain unarmed.")


@command("schedules", "history", help="Show bounded occurrence history")
def schedules_history(ctx: CLIContext, schedule_id: str, limit: int = 50) -> None:
    store = _store()
    try:
        schedule = store.get(schedule_id, include_deleted=True)
        occurrences = store.history(schedule_id, limit=limit)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to read schedule history: {exc}") from exc
    for occurrence in occurrences:
        print(
            f"{occurrence.occurrence_id} {occurrence.state.value:18} "
            f"{format_timestamp(occurrence.scheduled_for, schedule.timezone)} "
            f"session={occurrence.session_id or '—'} attempt={occurrence.attempt}"
        )
