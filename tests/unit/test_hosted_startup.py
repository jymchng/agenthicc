"""A hosted runtime that is busy building its session is waited for, and a dead one is named.

The report: attaching from the agents hub printed ``Unable to open foreground session ID:
TimeoutError:`` with no cause. Three independent 20 s deadlines all started when the runtime
published its endpoint, so the session's own build counted against them (a fresh, empty session
takes about 8 s on an idle host); the failure then escaped as a bare ``TimeoutError``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agenthicc.runners import hosted_transport
from agenthicc.runners.hosted_session import _await_ready, run_hosted_terminal
from agenthicc.runners.hosted_transport import ControllerStream, HostedStartupError

pytestmark = pytest.mark.unit


@asynccontextmanager
async def _runtime(
    behaviour: object,
) -> AsyncIterator[ControllerStream]:
    """A stand-in runtime whose frames come from *behaviour*, and the stream a client sees."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = ControllerStream(reader, writer)
        try:
            await behaviour(stream)  # type: ignore[operator]
        finally:
            await stream.close()

    server = await asyncio.start_server(handle, hosted_transport.LOOPBACK, 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection(hosted_transport.LOOPBACK, port)
    client = ControllerStream(reader, writer)
    try:
        yield client
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_a_working_runtime_is_waited_for_longer_than_the_silence_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait bounds silence, not the build: ``starting`` frames keep it alive."""
    monkeypatch.setattr(hosted_transport, "STARTUP_SILENCE_TIMEOUT_S", 0.2)

    async def builds_slowly(stream: ControllerStream) -> None:
        for _ in range(8):  # 0.8 s in total: four times the silence bound
            await asyncio.sleep(0.1)
            await stream.send({"type": "starting"})
        await stream.send({"type": "ready", "generation": 0, "session_id": "s"})

    async with _runtime(builds_slowly) as client:
        ready = await _await_ready(client)
    assert ready["type"] == "ready"


async def test_a_silent_runtime_is_reported_with_the_time_it_was_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hosted_transport, "STARTUP_SILENCE_TIMEOUT_S", 0.1)

    async def silent(_stream: ControllerStream) -> None:
        await asyncio.sleep(5)

    async with _runtime(silent) as client:
        with pytest.raises(HostedStartupError, match="sent nothing for 0.1 s"):
            await _await_ready(client)


async def test_a_runtime_that_stops_while_starting_is_named_not_a_bare_eof() -> None:
    async def dies(_stream: ControllerStream) -> None:
        return

    async with _runtime(dies) as client:
        with pytest.raises(HostedStartupError, match="stopped before it was ready"):
            await _await_ready(client)


async def test_a_launched_runtime_that_never_publishes_is_stopped_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child holds the session's lease, so a timeout must not leave it running."""
    from agenthicc.background.supervisor import BackgroundSupervisor
    from agenthicc.cli.context import CLIContext

    monkeypatch.setattr(hosted_transport, "LAUNCH_TIMEOUT_S", 0.3)
    launched: list[subprocess.Popen[bytes]] = []

    def launch(_bootstrap: Path) -> subprocess.Popen[bytes]:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        launched.append(process)
        return process

    monkeypatch.setattr(BackgroundSupervisor, "launch_interactive_runtime", staticmethod(launch))
    try:
        with pytest.raises(HostedStartupError, match="did not publish its controller within 0.3 s"):
            await run_hosted_terminal(CLIContext())
        assert launched[0].poll() is not None, "the runtime process was left running"
    finally:
        for process in launched:
            if process.poll() is None:
                process.kill()
                process.wait()


async def test_a_failed_attach_names_the_failure_and_the_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``Unable to open foreground session ID: TimeoutError:`` said nothing."""
    from types import SimpleNamespace

    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext
    from agenthicc.runners import tui_session

    record = SimpleNamespace(status=SimpleNamespace(value="cancelled"), live_handoff=None)
    store = SimpleNamespace(get=lambda *_a, **_k: record)
    supervisor = SimpleNamespace(
        attach_foreground=lambda _id: SimpleNamespace(
            status=SimpleNamespace(value="cancelled"), cwd="/work"
        )
    )

    async def fails(**_kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(tui_session, "_run_tui_session", fails)
    opened = await background.attach_background_session(
        CLIContext(), "sess-1", store=store, supervisor=supervisor, fail_on_error=False
    )
    out = capsys.readouterr().out
    assert opened is False
    assert "Unable to open foreground session sess-1: TimeoutError." in out
    assert "TimeoutError: " not in out
    assert "agenthicc attach sess-1" in out


# ── /bg ends in the agents hub ────────────────────────────────────────────────


async def test_a_detach_acknowledgement_hands_the_terminal_to_the_agents_hub(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/bg`` printed ``running in the background`` and returned to the shell.

    A terminal-owned session already ended in the hub with the new agent selected; the hosted
    client now says the same thing, so both paths agree.
    """
    import os

    from agenthicc.runners.hosted_session import _display
    from agenthicc.runners.tui_session import SessionSwitch
    from agenthicc.tui.terminal.backend import get_backend

    async def sends_detached(stream: ControllerStream) -> None:
        await stream.send({"type": "output", "text": ""})
        await stream.send(
            {"type": "detached", "session_id": "sess-9", "live_pools": 1, "live_workers": 5}
        )

    async with _runtime(sends_detached) as client:
        result = await _display(client, get_backend())
    assert result == SessionSwitch("hub", "sess-9", origin="sess-9", origin_cwd=os.getcwd())
    assert "Session sess-9 is running in the background" in capsys.readouterr().out


async def test_attaching_from_the_cli_returns_to_the_hub_after_a_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agenthicc attach ID`` (or Enter in the hub) then ``/bg`` lands back in the hub."""
    from types import SimpleNamespace

    from agenthicc.background.model import SessionStatus
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext
    from agenthicc.runners import hosted_session
    from agenthicc.runners.tui_session import SessionSwitch

    record = SimpleNamespace(
        status=SessionStatus.RUNNING, live_handoff=object(), cwd="/work", session_id="sess-9"
    )
    store = SimpleNamespace(get=lambda *_a, **_k: record)
    opened: list[dict[str, object]] = []

    async def attach(_record: object) -> SessionSwitch:
        return SessionSwitch("hub", "sess-9", origin="sess-9", origin_cwd="/work")

    async def hub(_ctx: object, **kwargs: object) -> None:
        opened.append(kwargs)

    monkeypatch.setattr(hosted_session, "attach_hosted_terminal", attach)
    monkeypatch.setattr(background, "_open_hub", hub)
    attached = await background.attach_background_session(
        CLIContext(), "sess-9", store=store, supervisor=SimpleNamespace()
    )
    assert attached is True
    assert opened == [{"select": "sess-9"}]


async def test_attaching_from_the_cli_ends_at_the_shell_after_an_ordinary_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from agenthicc.background.model import SessionStatus
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext
    from agenthicc.runners import hosted_session

    record = SimpleNamespace(status=SessionStatus.RUNNING, live_handoff=object(), cwd="/work")
    store = SimpleNamespace(get=lambda *_a, **_k: record)
    opened: list[object] = []

    async def attach(_record: object) -> None:
        return None

    async def hub(_ctx: object, **kwargs: object) -> None:
        opened.append(kwargs)

    monkeypatch.setattr(hosted_session, "attach_hosted_terminal", attach)
    monkeypatch.setattr(background, "_open_hub", hub)
    await background.attach_background_session(
        CLIContext(), "sess-9", store=store, supervisor=SimpleNamespace()
    )
    assert opened == []
