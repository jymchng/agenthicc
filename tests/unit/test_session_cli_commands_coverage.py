"""Coverage for client-neutral session CLI command adapters."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.session_service import (
    CommandResult,
    SessionError,
    SessionEvent,
    SessionSnapshot,
    SessionState,
)

pytestmark = pytest.mark.unit


def _snapshot() -> SessionSnapshot:
    return SessionSnapshot(
        schema_version=1,
        session_id="session-1",
        project_root="/project",
        created_at=1.0,
        updated_at=2.0,
        state=SessionState.IDLE,
        workflow={"name": "demo"},
        agent={"name": "default"},
        queue={"depth": 0},
        last_event_sequence=2,
    )


def _event() -> SessionEvent:
    return SessionEvent.create(
        session_id="session-1",
        sequence=3,
        source="test",
        kind="turn_completed",
        payload={"ok": True},
    )


class _FakeService:
    def __init__(self, *, error: bool = False) -> None:
        self.error = error
        self.snapshot_value = _snapshot()
        self.events_value = [_event()]

    async def create_session(self, **_kwargs: object) -> SessionSnapshot:
        return self.snapshot_value

    async def list_sessions(self, **_kwargs: object) -> list[SessionSnapshot]:
        return [] if self.error else [self.snapshot_value]

    async def snapshot(self, *_args: object, **_kwargs: object) -> SessionSnapshot:
        if self.error:
            raise SessionError("missing", "session missing", status=404)
        return self.snapshot_value

    async def events(self, *_args: object, **_kwargs: object) -> list[SessionEvent]:
        if self.error:
            raise SessionError("missing", "session missing", status=404)
        return self.events_value

    async def submit(self, *_args: object, **_kwargs: object) -> CommandResult:
        if self.error:
            raise SessionError("rejected", "command rejected")
        return CommandResult(True, "cmd-1", "session-1", data={"accepted": True})

    async def close(self) -> None:
        return None


def test_session_cli_commands_print_json_and_human_views(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import agenthicc.cli.commands.session_service as commands

    service = _FakeService()
    monkeypatch.setattr(commands, "_service", lambda: service)
    ctx = SimpleNamespace()
    commands.session_create(ctx, project_root=str(tmp_path), agent="default", workflow="demo")
    assert "session-1" in capsys.readouterr().out
    commands.session_list(ctx, project_root=str(tmp_path), json=True)
    assert json.loads(capsys.readouterr().out)[0]["state"] == "idle"
    commands.session_list(ctx)
    assert "session-1" in capsys.readouterr().out
    commands.session_show(ctx, "session-1", json=True)
    assert json.loads(capsys.readouterr().out)["queue"]["depth"] == 0
    commands.session_show(ctx, "session-1")
    assert "Session: session-1" in capsys.readouterr().out
    commands.session_events(ctx, "session-1", after=1)
    assert "turn_completed" in capsys.readouterr().out
    commands.session_send(ctx, "session-1", "hello")
    assert json.loads(capsys.readouterr().out)["ok"] is True
    commands.session_control(ctx, "session-1", "cancel", '{"reason":"user"}')
    assert json.loads(capsys.readouterr().out)["ok"] is True

    output = tmp_path / "export.json"
    commands.session_export(ctx, "session-1", str(output))
    assert "Exported session" in capsys.readouterr().out
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["schema_version"] == 1
    assert exported["events"][0]["kind"] == "turn_completed"


def test_session_cli_commands_handle_empty_and_service_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import agenthicc.cli.commands.session_service as commands

    empty = _FakeService()
    empty.list_sessions = lambda **_kwargs: _empty()  # type: ignore[method-assign]
    monkeypatch.setattr(commands, "_service", lambda: empty)
    ctx = SimpleNamespace()

    async def empty_list(**_kwargs: object) -> list[SessionSnapshot]:
        return []

    empty.list_sessions = empty_list  # type: ignore[method-assign]
    commands.session_list(ctx)
    assert "No client-neutral sessions" in capsys.readouterr().out
    commands.session_control(ctx, "session-1", "cancel", "[]")
    assert "payload must be a JSON object" in capsys.readouterr().out
    commands.session_control(ctx, "session-1", "cancel", "not-json")
    assert "Expecting" in capsys.readouterr().out

    failing = _FakeService(error=True)
    monkeypatch.setattr(commands, "_service", lambda: failing)
    commands.session_show(ctx, "missing")
    assert "session missing" in capsys.readouterr().out
    commands.session_export(ctx, "missing")
    assert "session missing" in capsys.readouterr().out
    commands.session_events(ctx, "missing")
    assert "session missing" in capsys.readouterr().out
    commands.session_control(ctx, "session-1", "cancel", "{}")
    assert "command rejected" in capsys.readouterr().out


async def _empty() -> list[SessionSnapshot]:
    return []


@pytest.mark.asyncio
async def test_session_serve_stops_server_on_cancellation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import agenthicc.cli.commands.session_service as commands

    class Server:
        async def start(self) -> str:
            return "http://127.0.0.1:1"

        async def stop(self) -> None:
            self.stopped = True

    service = _FakeService()
    server = Server()
    monkeypatch.setattr(commands, "_service", lambda: service)
    monkeypatch.setattr(commands, "LocalSessionServer", lambda *_args, **_kwargs: server)

    async def wait() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(commands.asyncio, "Event", lambda: SimpleNamespace(wait=wait))
    with pytest.raises(asyncio.CancelledError):
        await commands.session_serve(SimpleNamespace())
    assert server.stopped is True
    assert "ready" in capsys.readouterr().out
