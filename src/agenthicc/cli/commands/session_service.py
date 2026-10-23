"""Client-neutral session commands (PRD-150)."""

from __future__ import annotations

import asyncio
import json as json_module
from pathlib import Path
from typing import Any, Coroutine, TypeVar

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group
from agenthicc.session_service import (
    SessionCommand,
    SessionError,
    SessionService,
)
from agenthicc.session_service.transport import LocalSessionServer


@group("session", help="Inspect, control, and attach to client-neutral sessions")
def _session_group() -> None: ...


def _service() -> SessionService:
    return SessionService()


_ResultT = TypeVar("_ResultT")


def _run(coro: Coroutine[Any, Any, _ResultT]) -> _ResultT:
    return asyncio.run(coro)


@command("session", "create", help="Create a client-neutral session")
def session_create(
    ctx: CLIContext,
    project_root: str = ".",
    agent: str = "",
    workflow: str = "",
) -> None:
    """Create a session and print its stable ID and snapshot."""

    service = _service()
    snapshot = _run(
        service.create_session(
            project_root=Path(project_root).expanduser(),
            client_id="cli",
            capabilities=frozenset({"read", "control", "workspace"}),
            agent={"name": agent} if agent else None,
            workflow={"name": workflow} if workflow else None,
        )
    )
    print(json_module.dumps(snapshot.to_dict(), indent=2, sort_keys=True))


@command("session", "list", help="List client-neutral sessions")
def session_list(ctx: CLIContext, project_root: str = "", json: bool = False) -> None:
    """List service sessions, optionally restricted to a project root."""

    service = _service()
    snapshots = _run(
        service.list_sessions(
            project_root=Path(project_root).expanduser() if project_root else None,
            capabilities=frozenset({"read", "workspace"}),
        )
    )
    if json:
        print(json_module.dumps([snapshot.to_dict() for snapshot in snapshots], indent=2))
        return
    if not snapshots:
        print("No client-neutral sessions.")
        return
    for snapshot in snapshots:
        print(f"{snapshot.session_id}  {snapshot.state.value:18}  {snapshot.project_root}")


@command("session", "show", help="Show a client-neutral session snapshot")
def session_show(ctx: CLIContext, session_id: str, json: bool = False) -> None:
    """Show one session's policy-filtered snapshot."""

    service = _service()
    try:
        snapshot = _run(
            service.snapshot(
                session_id,
                capabilities=frozenset({"read", "workspace"}),
            )
        )
    except SessionError as exc:
        print(str(exc))
        return
    if json:
        print(json_module.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Session: {snapshot.session_id}")
        print(f"State: {snapshot.state.value}")
        print(f"Project: {snapshot.project_root}")
        print(f"Sequence: {snapshot.last_event_sequence}")
        print(f"Queue: {snapshot.queue.get('depth', 0)}")


@command("session", "export", help="Export a redacted client-neutral session")
def session_export(ctx: CLIContext, session_id: str, output: str = "") -> None:
    """Write a schema-versioned snapshot and durable event export."""

    service = _service()
    try:
        snapshot = _run(
            service.snapshot(
                session_id,
                capabilities=frozenset({"read", "workspace"}),
            )
        )
        events = _run(
            service.events(
                session_id,
                capabilities=frozenset({"read", "workspace"}),
            )
        )
    except SessionError as exc:
        print(str(exc))
        return
    destination = Path(output) if output else Path(f"{session_id}.json")
    destination.write_text(
        json_module.dumps(
            {
                "schema_version": 1,
                "snapshot": snapshot.to_dict(),
                "events": [event.to_dict() for event in events],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Exported session {session_id} to {destination}")


@command("session", "events", help="Replay durable session events as JSON-lines")
def session_events(ctx: CLIContext, session_id: str, after: int = 0) -> None:
    """Replay events after a durable sequence cursor."""

    service = _service()
    try:
        events = _run(
            service.events(
                session_id,
                after_sequence=after,
                capabilities=frozenset({"read", "workspace"}),
            )
        )
    except SessionError as exc:
        print(str(exc))
        return
    for event in events:
        print(json_module.dumps(event.to_dict(), sort_keys=True))


@command("session", "send", help="Submit a message to a client-neutral session")
def session_send(ctx: CLIContext, session_id: str, text: str) -> None:
    """Queue one message using the shared command envelope."""

    service = _service()
    result = _run(
        service.submit(
            SessionCommand(
                kind="submit_message",
                session_id=session_id,
                client_id="cli",
                payload={"text": text},
                capabilities=frozenset({"read", "control", "workspace"}),
            )
        )
    )
    print(json_module.dumps(result.to_dict(), indent=2, sort_keys=True))


@command("session", "control", help="Submit a JSON session control command")
def session_control(ctx: CLIContext, session_id: str, kind: str, payload: str = "") -> None:
    """Submit cancel, resume, retry, archive, fork, or another command kind."""

    try:
        decoded = json_module.loads(payload) if payload else {}
        if not isinstance(decoded, dict):
            raise ValueError("payload must be a JSON object")
        result = _run(
            _service().submit(
                SessionCommand(
                    kind=kind,
                    session_id=session_id,
                    client_id="cli",
                    payload=decoded,
                    capabilities=frozenset({"read", "control", "workspace"}),
                )
            )
        )
    except (ValueError, SessionError) as exc:
        print(str(exc))
        return
    print(json_module.dumps(result.to_dict(), indent=2, sort_keys=True))


@command("session", "serve", help="Serve local session snapshots and events over HTTP/SSE")
async def session_serve(
    ctx: CLIContext,
    host: str = "127.0.0.1",
    port: int = 0,
    auth_token: str = "",
) -> None:
    """Run the local attachment transport until interrupted."""

    from agenthicc.background.inbox import BackgroundInteractionInbox
    from agenthicc.background.settings import load_background_settings
    from agenthicc.background.store import BackgroundStore

    settings = load_background_settings(
        config_path=getattr(ctx, "config_path", None),
        overrides=tuple(getattr(ctx, "set_overrides", ())),
        config=(
            vars(ctx.config).get("background") if getattr(ctx, "config", None) is not None else None
        ),
    )
    background_store = BackgroundStore(
        settings.store_root, projection_batch_size=settings.manager.projection_batch_size
    )
    inbox = BackgroundInteractionInbox(background_store)
    service = SessionService(background_inbox=inbox)
    server = LocalSessionServer(
        service,
        host=host,
        port=port,
        auth_token=auth_token or None,
    )
    url = await server.start()
    scheduler_task: asyncio.Task[None] | None = None
    scheduler_stop = asyncio.Event()
    scheduler_enabled = False
    if settings.enabled:
        from agenthicc.cli.commands.background import _store_and_supervisor
        from agenthicc.schedules import (
            ScheduleScheduler,
            ScheduleStore,
            background_lookup,
            build_background_dispatcher,
        )

        _, supervisor = _store_and_supervisor(ctx, settings=settings)
        schedule_store = ScheduleStore()
        scheduler = ScheduleScheduler(
            schedule_store,
            build_background_dispatcher(
                store=schedule_store,
                supervisor=supervisor,
                background_store=background_store,
            ),
            background_lookup=background_lookup(background_store),
        )
        scheduler_task = asyncio.create_task(
            scheduler.run(stop=scheduler_stop), name="agenthicc-repository-scheduler"
        )
        # ScheduleScheduler acquires its non-blocking singleton synchronously
        # before its first await. Surface owner conflicts before reporting ready.
        await asyncio.sleep(0)
        if scheduler_task.done():
            exception = scheduler_task.exception()
            await server.stop()
            await service.close()
            if exception is not None:
                raise RuntimeError(f"Unable to start schedule service: {exception}") from exception
            raise RuntimeError("Schedule service stopped during startup")
        scheduler_enabled = True
    print(
        json_module.dumps(
            {
                "status": "ready",
                "url": url,
                "schedule_service": "running" if scheduler_enabled else "disabled",
            }
        ),
        flush=True,
    )
    try:
        await asyncio.Event().wait()
    finally:
        if scheduler_task is not None:
            scheduler_stop.set()
            await scheduler_task
        await server.stop()
        await service.close()
