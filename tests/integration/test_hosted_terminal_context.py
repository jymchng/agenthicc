"""A turn started from a hosted controller's keys can own background terminals.

The report (agents hub detail page): ``run_bash`` answered ``background terminals are
unavailable in this execution context`` for an agent in a hosted session. The runtime opens
its controller server in ``_serve`` and only then builds the session, which binds the
session's terminal manager to a context variable; asyncio creates a connection's handler
task in the context the server was started in, so the handler, and every turn it started,
saw no manager.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agenthicc.background.store import BackgroundStore
from agenthicc.background.supervisor import BackgroundSupervisor
from agenthicc.background.terminals import (
    TerminalManager,
    get_current_terminal_manager,
    reset_current_terminal_manager,
    set_current_terminal_manager,
)
from agenthicc.runners.hosted_transport import ControllerEndpoint, connect_controller
from agenthicc.runners.hosted_view import HostedView
from agenthicc.tui.cbreak_reader import Key
from tests.subagent_sessions import tui

pytestmark = pytest.mark.integration


async def test_keys_from_a_hosted_controller_run_with_the_sessions_terminal_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        manager = session._ctx.terminal_manager
        assert isinstance(manager, TerminalManager)
        seen: list[TerminalManager | None] = []

        async def dispatch(_key: Key, _text: str) -> bool:
            seen.append(get_current_terminal_manager())
            return True

        monkeypatch.setattr(session._input_session, "dispatch_hosted_key", dispatch)
        view = HostedView(BackgroundSupervisor(BackgroundStore(tmp_path / "background")), 160, 40)
        session._hosted_view = view
        lease = session._ctx.owner_lease
        assert lease is not None

        # The order ``hosted_session._serve`` uses: the controller server first, the session
        # (and its terminal manager binding) after.
        await view.publish(tmp_path / "controller", lease.path.parent / ".controller")
        token = set_current_terminal_manager(manager)
        try:
            session._tasks.start(view.run(session), name="hosted-view")
            stream = await connect_controller(ControllerEndpoint.load(tmp_path / "controller"))
            try:
                await stream.send({"type": "open", "columns": 160, "rows": 40})
                ready = await stream.receive()
                assert ready["type"] == "ready"
                await stream.send(
                    {
                        "type": "key",
                        "generation": ready["generation"],
                        "key": Key.CHAR.value,
                        "text": "x",
                    }
                )
                async with asyncio.timeout(5):
                    while not seen:
                        await asyncio.sleep(0.02)
            finally:
                await stream.close()
        finally:
            reset_current_terminal_manager(token)
        assert seen == [manager]
