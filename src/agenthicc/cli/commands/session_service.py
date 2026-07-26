"""Client-neutral session commands (PRD-150)."""

from __future__ import annotations

import asyncio
import json as json_module
from pathlib import Path
from typing import Any, Coroutine, TypeVar

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group
from agenthicc.session_service import (
    LocalSessionServer,
    SessionCommand,
    SessionError,
    SessionService,
)


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

    service = _service()
    server = LocalSessionServer(
        service,
        host=host,
        port=port,
        auth_token=auth_token or None,
    )
    url = await server.start()
    print(json_module.dumps({"status": "ready", "url": url}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await service.close()
