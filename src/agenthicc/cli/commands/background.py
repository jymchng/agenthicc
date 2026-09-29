"""Background session CLI and manager entry points (PRD-141)."""

from __future__ import annotations

import json as json_module
from pathlib import Path

from agenthicc.background import (
    BackgroundStore,
    BackgroundSupervisor,
    background_enabled,
    load_background_settings,
)
from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group

from agenthicc.background.integration import install_tui_handoff

install_tui_handoff()


@group("jobs", help="Manage durable background sessions")
def _jobs_group() -> None: ...


def _config(ctx: CLIContext) -> object:
    from agenthicc.config import load_config  # noqa: PLC0415

    return load_config(
        cli_overrides=list(ctx.set_overrides),
        cli_secret_overrides=list(ctx.set_secret_overrides),
        config_path=ctx.config_path,
    )


def _store_and_supervisor(ctx: CLIContext) -> tuple[BackgroundStore, BackgroundSupervisor]:
    cfg = _config(ctx)
    settings = load_background_settings(
        config_path=ctx.config_path,
        overrides=ctx.set_overrides,
        config=getattr(cfg, "background", None),
    )
    if not background_enabled(settings):
        raise RuntimeError("Background sessions are disabled by configuration")
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
    )
    return store, supervisor


def _public_session(session: object) -> dict[str, object]:
    from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

    raw = getattr(session, "to_dict")()
    if isinstance(raw, dict):
        result = dict(raw)
        result.pop("intent", None)
        result.pop("lease_token", None)
        redactor = _Redactor()
        result.pop("input_value", None)
        for key in ("title", "latest_activity", "error", "approval_request", "input_request"):
            if key in result:
                result[key] = redactor.value(result[key], key)
        return result
    return {}


async def _open_manager(ctx: CLIContext) -> None:
    from rich.console import Console  # noqa: PLC0415

    from agenthicc.tui.workspace.background_manager import run_background_manager  # noqa: PLC0415

    store, supervisor = _store_and_supervisor(ctx)
    result = await run_background_manager(
        Console(highlight=False), store=store, supervisor=supervisor
    )
    if result.action == "attach" and result.session_id:
        await attach_background_session(
            ctx,
            result.session_id,
            store=store,
            supervisor=supervisor,
            fail_on_error=False,
        )


async def attach_background_session(
    ctx: CLIContext,
    session_id: str,
    *,
    json_output: bool = False,
    fail_on_error: bool = True,
    store: BackgroundStore | None = None,
    supervisor: BackgroundSupervisor | None = None,
) -> bool:
    """Attach one exact background session through the canonical TUI path.

    Both the manager's Enter action and the public ``attach`` command call
    this function.  Keeping the handoff here prevents a second session lookup
    or an accidental goal-run/last-session fallback.
    """

    from rich.console import Console  # noqa: PLC0415

    if store is None or supervisor is None:
        store, supervisor = _store_and_supervisor(ctx)
    try:
        record = store.get(session_id, include_deleted=True)
        if record.status.value == "deleted":
            raise ValueError("deleted background sessions cannot be attached")
        foreground = supervisor.attach_foreground(session_id)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        message = f"Unable to attach background session {session_id}: {exc}"
        if json_output:
            print(
                json_module.dumps({"session_id": session_id, "status": "error", "error": str(exc)})
            )
        else:
            Console(highlight=False).print(message)
        if fail_on_error:
            raise SystemExit(1) from exc
        return False

    if json_output:
        print(
            json_module.dumps(
                {
                    "session_id": session_id,
                    "status": "handoff_ready",
                    "background_status": foreground.status.value,
                    "cwd": foreground.cwd,
                },
                sort_keys=True,
            )
        )
        return True

    from agenthicc.runners.tui_session import _run_tui_session  # noqa: PLC0415

    try:
        await _run_tui_session(
            resume_id=session_id,
            cli_overrides=list(ctx.set_overrides),
            cli_secret_overrides=list(ctx.set_secret_overrides),
            record_cassette=ctx.record_cassette,
            cli_flags=ctx.flags,
            config_path=ctx.config_path,
            cwd=foreground.cwd,
            config=ctx.config,
        )
    except Exception as exc:  # noqa: BLE001
        Console(highlight=False).print(
            f"Unable to open foreground session {session_id}: {type(exc).__name__}: {exc}"
        )
        if fail_on_error:
            raise SystemExit(1) from exc
        return False
    return True


async def _open_goal_manager(ctx: CLIContext, run_id: str) -> None:
    """Open the run projection while preserving the existing attach path."""

    from rich.console import Console  # noqa: PLC0415

    from agenthicc.runs.cli import _manager  # noqa: PLC0415
    from agenthicc.tui.workspace.goal_run_manager import (  # noqa: PLC0415
        run_goal_run_manager,
    )

    manager = _manager(ctx)
    try:
        result = await run_goal_run_manager(
            Console(highlight=False), manager=manager, run_id=run_id
        )
    except KeyError:
        print(f"Run not found: {run_id}")
        raise SystemExit(1) from None
    if result.action != "attach":
        return
    try:
        foreground = manager.attach(run_id)
        from agenthicc.runners.tui_session import _run_tui_session  # noqa: PLC0415

        await _run_tui_session(
            resume_id=foreground.session_id,
            cli_overrides=list(ctx.set_overrides),
            cli_secret_overrides=list(ctx.set_secret_overrides),
            record_cassette=ctx.record_cassette,
            cli_flags=ctx.flags,
            config_path=ctx.config_path,
            cwd=foreground.cwd,
            config=ctx.config,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to attach {run_id}: {exc}")
        raise SystemExit(1) from exc


@command("agents", help="Open the background sessions manager")
async def agents(
    ctx: CLIContext,
    run: str = "",
    json: bool = False,
) -> None:
    """Open the manager, or inspect agents linked to one goal run."""

    if run:
        from agenthicc.runs.cli import _manager  # noqa: PLC0415

        try:
            payload = _manager(ctx).projection(run).to_dict()
        except KeyError:
            print(f"Run not found: {run}")
            raise SystemExit(1) from None
        if json:
            print(json_module.dumps(payload.get("agents", []), indent=2, sort_keys=True))
            return
        await _open_goal_manager(ctx, run)
        return

    if json:
        store, _supervisor = _store_and_supervisor(ctx)
        session_payload = [_public_session(item) for item in store.list()]
        print(json_module.dumps(session_payload, indent=2, sort_keys=True))
        return

    await _open_manager(ctx)


@command("jobs", help="Open the background sessions manager")
async def jobs(ctx: CLIContext) -> None:
    """Open the background session manager."""

    await _open_manager(ctx)


@command("run", help="Start an agent turn or workflow")
async def run(
    ctx: CLIContext,
    background: bool = False,
    workflow: str = "",
    intent: str = "",
    title: str = "",
) -> None:
    """Run one request; ``--background`` returns after accepting a worker."""

    if not background:
        print("Use --background to start a durable background session.")
        return
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.submit(
            intent=intent,
            workflow_name=workflow,
            title=title,
            cwd=str(Path.cwd()),
            config_path=ctx.config_path,
            set_overrides=ctx.set_overrides,
            dangerously_skip_permissions=ctx.flags.dangerously_skip_permissions,
            set_secret_overrides=ctx.set_secret_overrides,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Unable to start background session: {exc}")
        raise SystemExit(1) from exc
    print(f"Background session {session.session_id} accepted ({session.status.value}).")


@command("jobs", "list", help="List background sessions")
def jobs_list(
    ctx: CLIContext,
    json: bool = False,
    all: bool = False,
    trash: bool = False,
) -> None:
    """List current, archived, or recoverable-trash sessions."""

    store, _supervisor = _store_and_supervisor(ctx)
    sessions = store.list(include_archived=all, include_deleted=trash)
    payload = [_public_session(item) for item in sessions]
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload:
        print("No background sessions.")
        return
    for item in payload:
        print(
            f"{str(item.get('session_id', ''))[:16]}  "
            f"{str(item.get('status', 'unknown')):16}  "
            f"{str(item.get('title', ''))[:48]}  "
            f"{item.get('workflow_name') or 'direct'}"
        )


@command("jobs", "status", help="Show one background session")
def jobs_status(ctx: CLIContext, session_id: str, json: bool = False) -> None:
    """Show redacted status for SESSION_ID."""

    store, _supervisor = _store_and_supervisor(ctx)
    try:
        payload = _public_session(store.get(session_id, include_deleted=True))
    except KeyError:
        payload = {"session_id": session_id, "status": "not_found"}
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Session: {payload.get('session_id', session_id)}")
        print(f"State: {payload.get('status', 'not_found')}")
        print(f"Title: {payload.get('title', '')}")
        print(f"Activity: {payload.get('latest_activity', '')}")
        if payload.get("error"):
            print(f"Error: {payload['error']}")


def _mutate(ctx: CLIContext, session_id: str, action: str) -> None:
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        if action == "cancel":
            session = supervisor.cancel(session_id)
        elif action == "resume":
            session = supervisor.resume(session_id)
        elif action == "retry":
            session = supervisor.retry(session_id)
        elif action == "archive":
            session = supervisor.archive(session_id)
        elif action == "delete":
            session = supervisor.delete(session_id)
        elif action == "restore":
            session = supervisor.restore_deleted(session_id)
        else:
            raise ValueError(f"unknown job operation: {action}")
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to {action} {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"{action}: {session.session_id} → {session.status.value}")


@command("jobs", "cancel", help="Cancel a background session")
def jobs_cancel(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "cancel")


@command("jobs", "resume", help="Resume a background session")
def jobs_resume(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "resume")


@command("jobs", "retry", help="Retry a failed background session")
def jobs_retry(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "retry")


@command("jobs", "approve", help="Approve a waiting background session")
def jobs_approve(ctx: CLIContext, session_id: str) -> None:
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.approve(session_id, True)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to approve {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"approve: {session.session_id} → {session.status.value}")


@command("jobs", "reject", help="Reject a waiting background session")
def jobs_reject(ctx: CLIContext, session_id: str) -> None:
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.approve(session_id, False)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to reject {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"reject: {session.session_id} → {session.status.value}")


@command("jobs", "input", help="Provide input to a waiting background session")
def jobs_input(ctx: CLIContext, session_id: str, value: str) -> None:
    """Deliver VALUE to a session currently waiting for user input."""

    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.provide_input(session_id, value)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to provide input to {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"input: {session.session_id} → {session.status.value}")


@command("jobs", "rename", help="Rename a background session")
def jobs_rename(ctx: CLIContext, session_id: str, title: str) -> None:
    """Set a bounded, local-only display title."""

    store, _supervisor = _store_and_supervisor(ctx)
    try:
        session = store.rename(session_id, title)
    except (KeyError, ValueError) as exc:
        print(f"Unable to rename {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"rename: {session.session_id} → {session.title}")


@command("jobs", "labels", help="Set comma-separated labels on a background session")
def jobs_labels(ctx: CLIContext, session_id: str, labels: str = "") -> None:
    """Replace user labels; labels are not sent to providers."""

    store, _supervisor = _store_and_supervisor(ctx)
    values = tuple(item.strip() for item in labels.split(",") if item.strip())
    try:
        session = store.set_labels(session_id, values)
    except (KeyError, ValueError) as exc:
        print(f"Unable to set labels on {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"labels: {session.session_id} → {', '.join(session.labels) or 'none'}")


@command("jobs", "purge", help="Permanently remove expired background trash")
def jobs_purge(ctx: CLIContext) -> None:
    """Apply the configured recoverable-trash retention policy."""

    _store, supervisor = _store_and_supervisor(ctx)
    removed = supervisor.purge_expired_trash()
    print(f"purged: {len(removed)}")


@command("jobs", "archive", help="Archive a background session")
def jobs_archive(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "archive")


@command("jobs", "delete", help="Move a background session to recoverable trash")
def jobs_delete(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "delete")


@command("jobs", "restore", help="Restore a deleted background session")
def jobs_restore(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "restore")
