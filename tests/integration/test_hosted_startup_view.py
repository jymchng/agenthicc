"""The hosted runtime's controller windows start when the session can serve a controller."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agenthicc.background.store import BackgroundStore
from agenthicc.background.supervisor import BackgroundSupervisor
from agenthicc.runners import hosted_transport
from agenthicc.runners.hosted_transport import ControllerEndpoint, connect_controller
from agenthicc.runners.hosted_view import HostedView
from tests.subagent_sessions import tui

pytestmark = pytest.mark.integration


async def test_a_slow_build_keeps_the_controller_informed_and_is_not_closed_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session takes longer to build than the first-controller window lasts.

    The window used to open when the endpoint was published, so the runtime closed itself
    before the session was ready, and the controller timed out in silence.
    """
    monkeypatch.setattr(hosted_transport, "STARTING_INTERVAL_S", 0.05)
    monkeypatch.setattr(hosted_transport, "INITIAL_CONTROLLER_WINDOW_S", 0.4)
    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        view = HostedView(BackgroundSupervisor(BackgroundStore(tmp_path / "background")), 160, 40)
        session._hosted_view = view
        lease = session._ctx.owner_lease
        assert lease is not None
        await view.publish(tmp_path / "controller", lease.path.parent / ".controller")
        stream = await connect_controller(ControllerEndpoint.load(tmp_path / "controller"))
        try:
            await stream.send({"type": "open", "columns": 160, "rows": 40})
            await asyncio.sleep(1.0)  # the build outlasts the window
            assert not view._closed.is_set(), "the window opened before the session was ready"
            session._tasks.start(view.run(session), name="late-hosted-view")
            seen: list[object] = []
            async with asyncio.timeout(8):
                while (frame := await stream.receive())["type"] != "ready":
                    seen.append(frame["type"])
            assert seen and set(seen) == {"starting"}
            assert not view._closed.is_set()
        finally:
            await stream.close()


async def test_a_ready_runtime_nobody_attaches_to_closes_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window still ends a runtime whose launcher died: it just starts at ready."""
    monkeypatch.setattr(hosted_transport, "INITIAL_CONTROLLER_WINDOW_S", 0.2)
    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        view = HostedView(BackgroundSupervisor(BackgroundStore(tmp_path / "background")), 160, 40)
        session._hosted_view = view
        lease = session._ctx.owner_lease
        assert lease is not None
        await view.publish(tmp_path / "controller", lease.path.parent / ".controller")
        session._tasks.start(view.run(session), name="idle-hosted-view")
        async with asyncio.timeout(5):
            await view._closed.wait()
