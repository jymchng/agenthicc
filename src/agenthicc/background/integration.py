"""Small runtime bridge for foreground ``/bg`` handoff (PRD-141).

The bridge is installed during CLI discovery, before the interactive session
is constructed.  It keeps the handoff command in the existing command/input
pipeline while the background service remains the single lifecycle owner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .model import ACTIVE_STATUSES, SessionStatus
from .settings import background_enabled, load_background_settings
from .supervisor import BackgroundSupervisor
from .store import BackgroundStore

if TYPE_CHECKING:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.input.capabilities import _ExitSentinel
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime.commands import SendMessageCommand
    from agenthicc.runners.session_lease import SessionOwnerLease

_INSTALLED = False


def _last_user_text(session: object) -> str:
    remembered = getattr(session, "_background_intent", "")
    if isinstance(remembered, str) and remembered.strip():
        return remembered.strip()

    # TUISession records accepted plain submissions even when the optional
    # compatibility bridge was installed after the session was constructed.
    # This keeps /bg usable for the first active request as well as resumed
    # sessions, without treating the /bg command itself as the intent.
    submitted = getattr(session, "_last_submitted_text", "")
    if isinstance(submitted, str) and submitted.strip() and not submitted.lstrip().startswith("/"):
        return submitted.strip()

    conversation = getattr(getattr(session, "_ctx"), "app_state").conversation
    turns = conversation.turns()
    for turn in reversed(turns):
        for event in reversed(turn.events):
            if event.kind == "user_message":
                value = event.payload.get("text")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _restore_foreground_lease(ctx: object, owner_lease: object | None) -> bool:
    """Reclaim the same session lease after a confirmed worker launch failure."""
    from agenthicc.runners.session_lease import (  # noqa: PLC0415
        SessionOpenCoordinator,
        SessionOwnerLease,
    )

    if not isinstance(owner_lease, SessionOwnerLease):
        return False
    session_id = vars(ctx).get("session_id")
    if not isinstance(session_id, str):
        return False

    try:
        restored = SessionOpenCoordinator(owner_lease.path.parent.parent).acquire_existing(
            session_id,
            entrypoint=owner_lease.info.entrypoint,
        )
    except Exception:  # noqa: BLE001
        return False
    setattr(ctx, "owner_lease", restored)
    return True


def _reclaim_foreground_lease(
    ctx: object,
    supervisor: BackgroundSupervisor,
    session_id: str,
    owner_lease: "SessionOwnerLease | None",
) -> "SessionOwnerLease | None":
    """Refresh a stale TUI lease before reconciling a proven-dead worker.

    The normal ``--continue`` path already passes a current lease. This is a
    recovery guard for sessions whose lease handle became stale while the
    foreground TUI remained alive. The coordinator still performs the
    exclusive, process-aware claim; if another live owner holds the session,
    this function leaves the original handle untouched and the supervisor
    fails closed as before.
    """
    from agenthicc.background.queue import is_pending_queue_record  # noqa: PLC0415
    from agenthicc.background.supervisor import _WorkerProcessState  # noqa: PLC0415
    from agenthicc.runners.session_lease import (  # noqa: PLC0415
        SessionOpenCoordinator,
        SessionOwnerLease,
        default_sessions_dir,
    )

    try:
        record = supervisor.store.get(session_id)
    except KeyError:
        return owner_lease
    if (
        record.status not in ACTIVE_STATUSES
        or is_pending_queue_record(record)
        or supervisor._worker_process_state(record)
        not in {_WorkerProcessState.DEAD, _WorkerProcessState.MISMATCHED}
        or (
            isinstance(owner_lease, SessionOwnerLease)
            and supervisor._foreground_lease_is_current(session_id, owner_lease)
        )
    ):
        return owner_lease

    sessions_dir = (
        owner_lease.path.parent.parent
        if isinstance(owner_lease, SessionOwnerLease)
        else default_sessions_dir()
    )
    try:
        refreshed = SessionOpenCoordinator(sessions_dir).acquire_existing(
            session_id,
            entrypoint="tui",
        )
    except Exception:  # noqa: BLE001
        # Keep the existing lease (if any) and let the supervisor report its
        # normal ownership error. Acquisition failures must not authorize a
        # takeover or mutate the background record.
        return owner_lease

    if isinstance(owner_lease, SessionOwnerLease) and owner_lease.session_id == session_id:
        owner_lease.release()
    setattr(ctx, "owner_lease", refreshed)
    return refreshed


def _handoff(session: object) -> bool:
    ctx = getattr(session, "_ctx")
    session_values = vars(session)
    handoff_state = session_values.get("_background_handoff_state", "idle")
    if handoff_state in {"preparing", "started"}:
        # A repeated alias/key event must not prepare or launch another attempt.
        return True
    if handoff_state == "prepared":
        # A prepared request is durable and may be retried after a confirmed
        # launch failure without advancing the session attempt a second time.
        prepared_supervisor = session_values.get("_background_prepared_supervisor")
        prepared_session_id = session_values.get("_background_prepared_session_id")
        if isinstance(prepared_supervisor, BackgroundSupervisor) and isinstance(
            prepared_session_id, str
        ):
            try:
                record = prepared_supervisor.start(prepared_session_id)
            except Exception as exc:  # noqa: BLE001
                ctx.console.print(
                    f"Unable to start prepared background session: {type(exc).__name__}: {exc}"
                )
                return True
            if record.status is SessionStatus.FAILED:
                setattr(session, "_background_handoff_state", "idle")
                ctx.console.print(record.error or "Background worker launch failed.")
                return True
            setattr(session, "_background_handoff_state", "started")
            ctx.app_state.conversation.notify_transient(
                f"Backgrounded session {record.session_id[:12]}… ({record.status.value})"
            )
            setattr(session_values["_input_session"], "_background_exit_requested", True)
            return True
        setattr(session, "_background_handoff_state", "idle")

    intent = _last_user_text(session)
    if not intent:
        ctx.console.print("Cannot background this session before it has a user request.")
        return True
    mode = ctx.app_state.active_mode()
    workflow_name = str(
        getattr(session, "_workflow_override", "") or getattr(mode, "default_workflow", "") or ""
    )
    settings = load_background_settings(config=getattr(ctx.cfg, "background", None))
    if not background_enabled(settings):
        ctx.console.print("Background sessions are disabled by configuration.")
        return True
    root_value = settings.store_path
    root = Path(root_value).expanduser() if isinstance(root_value, str) and root_value else None
    store = BackgroundStore(root)
    supervisor = BackgroundSupervisor(
        store,
        max_workers=settings.max_workers,
        max_workers_per_project=settings.max_workers_per_project,
        cancel_grace_s=settings.cancel_grace_s,
        wall_timeout_s=settings.wall_timeout_s,
        max_activity_bytes=settings.max_activity_bytes,
        trash_retention_days=settings.trash_retention_days,
        queue_enabled=settings.queue_enabled,
        max_queue_length=settings.max_queue_length,
        max_workers_per_user=settings.max_workers_per_user,
        priority_aging_interval_s=settings.priority_aging_interval_s,
        dispatch_lease_s=settings.dispatch_lease_s,
        notification_settings=settings.notifications,
    )
    mode_name_value = vars(mode).get("name")
    mode_name = mode_name_value if isinstance(mode_name_value, str) else None
    cfg_values = vars(ctx).get("cfg")
    execution_values = vars(cfg_values).get("execution") if cfg_values is not None else None
    profile_value = vars(execution_values).get("profile", "") if execution_values else ""
    requested_profile_name = (
        profile_value.strip() if isinstance(profile_value, str) and profile_value.strip() else None
    )
    config_path_value = vars(ctx).get("config_path")
    config_path = config_path_value if isinstance(config_path_value, str) else None
    owner_lease = cast("SessionOwnerLease | None", vars(ctx).get("owner_lease"))
    setattr(session, "_background_handoff_state", "preparing")
    owner_released = False
    try:
        goal_run_id = str(vars(ctx).get("goal_run_id", "") or "")
        task = vars(session).get("_agent_task")
        resume_existing_only = not (isinstance(task, asyncio.Task) and not task.done())
        owner_lease = _reclaim_foreground_lease(
            ctx,
            supervisor,
            ctx.session_id,
            owner_lease,
        )
        prepared = True
        try:
            record = supervisor.handoff(
                session_id=ctx.session_id,
                intent=intent,
                workflow_name=workflow_name,
                cwd=str(Path.cwd()),
                config_path=config_path,
                set_overrides=tuple(vars(ctx).get("cfg_overrides", ())),
                set_secret_overrides=tuple(vars(ctx).get("cfg_secret_overrides", ())),
                dangerously_skip_permissions=bool(
                    vars(flags).get("dangerously_skip_permissions", False)
                    if (flags := vars(ctx.app_state).get("cli_flags")) is not None
                    else False
                ),
                run_id=goal_run_id,
                start=False,
                mode_name=mode_name,
                resume_existing_only=resume_existing_only,
                requested_profile_name=requested_profile_name,
                foreground_owner_lease=owner_lease,
            )
        except TypeError as exc:
            if "resume_existing_only" in str(exc):
                # Preserve the two-stage owner handoff for embedders that
                # support prepared starts but predate the resume-only option.
                record = supervisor.handoff(
                    session_id=ctx.session_id,
                    intent=intent,
                    workflow_name=workflow_name,
                    cwd=str(Path.cwd()),
                    config_path=config_path,
                    set_overrides=tuple(vars(ctx).get("cfg_overrides", ())),
                    set_secret_overrides=tuple(vars(ctx).get("cfg_secret_overrides", ())),
                    dangerously_skip_permissions=bool(
                        vars(flags).get("dangerously_skip_permissions", False)
                        if (flags := vars(ctx.app_state).get("cli_flags")) is not None
                        else False
                    ),
                    run_id=goal_run_id,
                    start=False,
                    mode_name=mode_name,
                    requested_profile_name=requested_profile_name,
                    foreground_owner_lease=owner_lease,
                )
            elif "start" in str(exc):
                # Keep compatibility with embedders that supplied the pre-204
                # supervisor seam in tests or plugins. Such a supervisor has
                # already launched the request, so it cannot use the two-stage
                # owner-safe handoff below.
                prepared = False
                record = supervisor.handoff(
                    session_id=ctx.session_id,
                    intent=intent,
                    workflow_name=workflow_name,
                    cwd=str(Path.cwd()),
                    config_path=config_path,
                    set_overrides=tuple(vars(ctx).get("cfg_overrides", ())),
                    set_secret_overrides=tuple(vars(ctx).get("cfg_secret_overrides", ())),
                    dangerously_skip_permissions=bool(
                        vars(flags).get("dangerously_skip_permissions", False)
                        if (flags := vars(ctx.app_state).get("cli_flags")) is not None
                        else False
                    ),
                    run_id=goal_run_id,
                    mode_name=mode_name,
                    requested_profile_name=requested_profile_name,
                    foreground_owner_lease=owner_lease,
                )
            else:
                raise
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
        if prepared:
            prepared_request = supervisor.store.root / "requests" / f"{record.session_id}.json"
            if not prepared_request.is_file():
                # A compatibility seam may accept ``start=False`` through a
                # ``**kwargs`` lambda while still launching immediately.
                prepared = False
        if prepared:
            # The request and session record are durable now. Release the
            # foreground lease before the worker is allowed to claim it.
            record_path = supervisor.store.root / "requests" / f"{record.session_id}.json"
            if record_path.is_file():
                setattr(session, "_background_handoff_state", "prepared")
                setattr(session, "_background_prepared_supervisor", supervisor)
                setattr(session, "_background_prepared_session_id", record.session_id)
            if owner_lease is not None:
                owner_lease.release()
                owner_released = True
            record = supervisor.start(record.session_id)
            if record.status is SessionStatus.FAILED:
                restored = _restore_foreground_lease(ctx, owner_lease)
                if restored:
                    setattr(session, "_background_handoff_state", "idle")
                    setattr(session, "_background_prepared_supervisor", None)
                    setattr(session, "_background_prepared_session_id", None)
                    ctx.console.print(record.error or "Background worker launch failed.")
                    return True
                setattr(session, "_background_handoff_state", "started")
                setattr(session_values["_input_session"], "_background_exit_requested", True)
                ctx.console.print(
                    "Background worker launch failed and foreground ownership could not "
                    "be safely restored; reopen the same session with `agenthicc --continue`."
                )
                return True
        if goal_run_id and record.status in ACTIVE_STATUSES:
            from agenthicc.runs import RunStore  # noqa: PLC0415
            from agenthicc.runs.model import GoalRunStatus  # noqa: PLC0415

            try:
                RunStore().update(
                    goal_run_id,
                    status=GoalRunStatus.RUNNING,
                    main_session_id=record.session_id,
                    main_agent_id=record.session_id,
                )
            except KeyError:
                # The foreground session may have been created by an older
                # client. The background handoff remains valid, but it is not
                # safe to fabricate a run record from the slash command.
                pass
    except Exception as exc:  # noqa: BLE001
        if not owner_released:
            setattr(session, "_background_handoff_state", "idle")
        else:
            current = None
            try:
                current = supervisor.store.get(ctx.session_id, include_deleted=True)
            except Exception:  # noqa: BLE001
                pass
            if current is not None and current.status not in ACTIVE_STATUSES:
                if _restore_foreground_lease(ctx, owner_lease):
                    setattr(session, "_background_handoff_state", "idle")
                else:
                    setattr(session, "_background_handoff_state", "started")
                    setattr(session_values["_input_session"], "_background_exit_requested", True)
            else:
                # The child may have started but failed before its result was
                # observed. Leave the terminal instead of retaining an
                # unowned foreground runtime that could race the worker.
                setattr(session, "_background_handoff_state", "started")
                setattr(session_values["_input_session"], "_background_exit_requested", True)
        ctx.console.print(f"Unable to background session: {type(exc).__name__}: {exc}")
        return True
    setattr(session, "_background_handoff_state", "started")
    ctx.app_state.conversation.notify_transient(
        f"Backgrounded session {record.session_id[:12]}… ({record.status.value})"
    )
    setattr(getattr(session, "_input_session"), "_background_exit_requested", True)
    return True


def install_tui_handoff() -> None:
    """Install the two foreground aliases once per interpreter."""

    global _INSTALLED
    if _INSTALLED:
        return
    from agenthicc.runners.tui_session import TUISession  # noqa: PLC0415
    from agenthicc.tui.input.unified_session import UnifiedInputSession  # noqa: PLC0415
    from agenthicc.tui.input.capabilities import _EXIT  # noqa: PLC0415
    from agenthicc.commands.builtins import BUILTIN_COMMANDS  # noqa: PLC0415
    from agenthicc.commands.command import BusyPolicy, Command  # noqa: PLC0415

    if not any(command.name == "/background" for command in BUILTIN_COMMANDS):
        BUILTIN_COMMANDS.append(
            Command(
                name="/background",
                aliases=("/bg",),
                description="Detach the active session into the background manager",
                argument_hint="(alias: /bg)",
                busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
            )
        )
    if not any(command.name == "/detach" for command in BUILTIN_COMMANDS):
        BUILTIN_COMMANDS.append(
            Command(
                name="/detach",
                description="Detach the active session into the background manager",
                argument_hint="",
                busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
            )
        )

    original_dispatch = TUISession.dispatch_slash

    def dispatch_slash(self: TUISession, text: str) -> bool:
        command = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        if command in {"/bg", "/background", "/detach"}:
            return _handoff(self)
        return original_dispatch(self, text)

    original_handle_send = TUISession.handle_send

    async def handle_send(self: TUISession, cmd: SendMessageCommand) -> None:
        text = str(getattr(cmd, "text", "")).strip()
        if text.startswith("/") and text.split(maxsplit=1)[0] in {
            "/bg",
            "/background",
            "/detach",
        }:
            self.dispatch_slash(text)
            return
        if text:
            # TUISession emits user_message before opening the agent turn, so
            # the event is intentionally not owned by a ConversationTurn.
            # Remember the accepted input on the session for /background.
            setattr(self, "_background_intent", text)
        return await original_handle_send(self, cmd)

    original_dispatch_key = UnifiedInputSession._dispatch

    async def dispatch_key(self: UnifiedInputSession, key: Key, ch: str) -> _ExitSentinel | None:
        result = await original_dispatch_key(self, key, ch)
        if getattr(self, "_background_exit_requested", False):
            return _EXIT
        return result

    TUISession.dispatch_slash = dispatch_slash  # type: ignore[method-assign]
    TUISession.handle_send = handle_send  # type: ignore[method-assign]
    setattr(UnifiedInputSession, "_dispatch", dispatch_key)
    _INSTALLED = True
