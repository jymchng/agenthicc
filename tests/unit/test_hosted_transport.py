"""Run the authenticated controller protocol and process probe on the host platform."""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.runners.hosted_transport import (
    LOOPBACK,
    MAX_FRAME_BYTES,
    ControllerEndpoint,
    ControllerProtocolError,
    ControllerStream,
    authenticate_controller,
    connect_controller,
)
from agenthicc.runners.process_lease import process_identity
from agenthicc.runners.session_lease import SessionOwnerStore

pytestmark = pytest.mark.unit


async def test_controller_endpoint_authenticates_both_sides_and_exchanges_frames(
    tmp_path: Path,
) -> None:
    secret = secrets.token_hex(32)
    stopped = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = ControllerStream(reader, writer)
        try:
            await authenticate_controller(stream, secret)
            data = await stream.receive()
            await stream.send({"echo": data})
        except (ControllerProtocolError, asyncio.IncompleteReadError):
            pass
        finally:
            await stream.close()
            stopped.set()

    server = await asyncio.start_server(handle, LOOPBACK, 0, limit=MAX_FRAME_BYTES)
    async with server:
        endpoint = ControllerEndpoint(server.sockets[0].getsockname()[1], secret)
        path = tmp_path / "controller"
        endpoint.publish(path)
        assert ControllerEndpoint.load(path) == endpoint
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o600
        stream = await connect_controller(endpoint)
        try:
            await stream.send({"text": "hello 👋"})
            assert await stream.receive() == {"echo": {"text": "hello 👋"}}
            with pytest.raises(ControllerProtocolError, match="too large"):
                stream.write({"text": "x" * MAX_FRAME_BYTES})
        finally:
            await stream.close()
            await stopped.wait()
        stopped.clear()
        with pytest.raises(ControllerProtocolError, match="Runtime authentication failed"):
            await connect_controller(ControllerEndpoint(endpoint.port, secrets.token_hex(32)))
        await stopped.wait()


@pytest.mark.parametrize("wire", [b"[0]\n", b"not json\n", b'"too long"\n'])
async def test_invalid_controller_frame_keeps_it_out_of_dispatch(wire: bytes) -> None:
    handled = asyncio.Event()
    errors: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = ControllerStream(reader, writer)
        try:
            await stream.receive()
        except ControllerProtocolError as exc:
            errors.append(str(exc))
        finally:
            await stream.close()
            handled.set()

    server = await asyncio.start_server(handle, LOOPBACK, 0, limit=8)
    async with server:
        _, writer = await asyncio.open_connection(LOOPBACK, server.sockets[0].getsockname()[1])
        writer.write(wire)
        await writer.drain()
        await handled.wait()
        writer.close()
        await writer.wait_closed()
    assert errors


def test_endpoint_rejects_missing_or_malformed_metadata(tmp_path: Path) -> None:
    path = tmp_path / "missing"
    with pytest.raises(ControllerProtocolError, match="Invalid controller endpoint"):
        ControllerEndpoint.load(path)
    path.write_text('{"port": 123, "secret": "not a token"}', encoding="utf-8")
    with pytest.raises(ControllerProtocolError, match="Invalid controller authentication token"):
        ControllerEndpoint.load(path)


def test_current_process_identity_matches_session_lease(tmp_path: Path) -> None:
    if sys.platform not in {"linux", "win32"}:
        pytest.skip("Exact process-start identity is available on Linux and Windows")
    identity = process_identity(os.getpid())
    assert identity is not None and identity[0] != "Z" and identity[1]
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        child_identity = process_identity(child.pid)
        assert child_identity is not None and child_identity[0] != "Z"
        assert child_identity[1] != identity[1]
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert process_identity(child.pid) in {None, ("Z", ""), ("Z", child_identity[1])}
    owners = SessionOwnerStore(tmp_path)
    with owners.acquire("hosted-session", entrypoint="hosted-tui") as lease:
        assert lease.info.process_start_token == identity[1]
        assert owners.inspect(lease.session_id).state == "active"


async def test_native_runtime_process_answers_over_the_real_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows and POSIX launchers use their native process and socket adapters."""
    if sys.platform not in {"linux", "win32"}:
        pytest.skip("Hosted startup requires exact process-start identity")
    from agenthicc.background.supervisor import BackgroundSupervisor
    from agenthicc.runners.hosted_session import discover_interactive_terminal

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "config").write_text('{"size":[100,24]}', encoding="utf-8")
    process = await asyncio.to_thread(BackgroundSupervisor.launch_interactive_runtime, bootstrap)
    try:
        path = bootstrap / "endpoint"
        async with asyncio.timeout(30):
            while not path.is_file():
                assert process.poll() is None, "The detached runtime exited before publishing"
                await asyncio.sleep(0.02)
            stream = await connect_controller(ControllerEndpoint.load(path))
            try:
                await stream.send({"type": "open", "columns": 100, "rows": 24})
                ready = await stream.receive()
                session_id = ready.get("session_id")
                assert isinstance(session_id, str) and ready["type"] == "ready"
                runtime = await discover_interactive_terminal(session_id)
                assert runtime is not None
                # The lease records the interpreter that owns the session. A
                # Windows virtual-environment launcher is a different process
                # from the interpreter it starts, so ``process.pid`` is not
                # comparable there.
                owner = (
                    SessionOwnerStore(home / ".agenthicc" / "sessions").inspect(session_id).owner
                )
                assert owner is not None and runtime["pid"] == owner.pid
                if sys.platform != "win32":
                    assert owner.pid == process.pid
                assert runtime["attached"] is True and runtime["generation"] == 0
            finally:
                await stream.close()
        assert await asyncio.to_thread(process.wait, 10) == 0
        assert (
            SessionOwnerStore(home / ".agenthicc" / "sessions").inspect(session_id).state
            == "available"
        )
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 10)


@pytest.mark.parametrize(
    "frame,message",
    [
        ({"type": "finished", "switch": {"action": "bad"}}, "Invalid session switch"),
        ({"type": "output", "text": 4}, "Invalid terminal output"),
        (
            {"type": "editor", "path": "/tmp/draft", "cwd": "/tmp", "scope": "bad"},
            "Invalid terminal editor operation",
        ),
        ({"type": "detached", "session_id": 9}, "Invalid detach acknowledgement"),
        ({"type": "error", "message": "fixture rejection"}, "fixture rejection"),
        ({"type": "unknown"}, "Unknown terminal output"),
    ],
)
async def test_terminal_display_rejects_invalid_controller_output(
    frame: dict[str, object], message: str
) -> None:
    from agenthicc.runners.hosted_session import _display
    from agenthicc.tui.terminal.backend import get_backend

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = ControllerStream(reader, writer)
        try:
            await stream.send({"type": "reporting", "focus": False, "mouse": False})
            await stream.send({"type": "rejected", "code": "stale_controller"})
            await stream.send(frame)
        finally:
            await stream.close()

    server = await asyncio.start_server(handle, LOOPBACK, 0, limit=MAX_FRAME_BYTES)
    async with server:
        reader, writer = await asyncio.open_connection(LOOPBACK, server.sockets[0].getsockname()[1])
        stream = ControllerStream(reader, writer)
        try:
            with pytest.raises(ControllerProtocolError, match=message):
                await _display(stream, get_backend())
        finally:
            await stream.close()


def test_terminal_switch_and_editor_targets_are_scoped_to_the_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.runners.hosted_session import _EditRequest, _switch
    from agenthicc.runners.session_lease import SessionOwnerStore
    from agenthicc.runners.tui_session import SessionSwitch
    from agenthicc.tui.input import unified_session

    assert _switch(None) is None
    command = _switch(
        {"action": "attach", "target": "target", "origin": "source", "origin_cwd": "."}
    )
    assert command == SessionSwitch("attach", "target", "source", ".")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    session_dir = SessionOwnerStore().session_path("test-editor")
    session_dir.mkdir(parents=True)
    prompt = session_dir / "prompt-test.md"
    calls: list[Path] = []

    def editor(path: Path) -> int:
        calls.append(path)
        return 12

    monkeypatch.setattr(unified_session, "_run_editor", editor)
    assert _EditRequest(prompt, tmp_path, "prompt").run("test-editor") == 12
    assert calls == [prompt]
    with pytest.raises(ValueError, match="Invalid prompt editor path"):
        _EditRequest(tmp_path / "prompt-test.md", tmp_path, "prompt").run("test-editor")
