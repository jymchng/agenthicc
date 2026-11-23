"""``/bg``, ``/background`` and ``/detach`` wait for the running tool to finish.

The handoff happens at the next tool boundary, once the call's result is
committed, or when the turn ends; with no tool running it is immediate
(operator request, 2026-11-07).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agenthicc.tui.conversation_store import AppState, RunningTool
from agenthicc.runners.tui_session import TUISession
from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.unit


def _session(monkeypatch: pytest.MonkeyPatch) -> tuple[TUISession, AppState, list[str]]:
    from agenthicc.background import integration

    calls: list[str] = []
    monkeypatch.setattr(integration, "_handoff", lambda session: calls.append("handoff") or True)
    session, ctx, _workspace, _input = _make_session()
    assert isinstance(session, TUISession)
    return session, ctx.app_state, calls


@pytest.mark.parametrize("command", ["/bg", "/background", "/detach"])
async def test_a_running_tool_delays_the_handoff_to_the_next_boundary(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    session, state, calls = _session(monkeypatch)
    blocker = asyncio.Event()
    turn = asyncio.create_task(blocker.wait())
    session._agent_task = turn
    state.conversation.start_running_tool(RunningTool("t1", "$", "make test", time.time()))
    try:
        assert session.dispatch_slash(command) is True
        assert calls == []  # the tool is still running
        # A blinking row in the live region says the hand-off is waiting.
        assert state.conversation.handoff_pending() is True
        assert state.conversation.notification() is None
        assert session.dispatch_slash("/bg") is True  # a repeat does not queue twice

        # The tool finishes: at the boundary the queued-input hook schedules it.
        assert session._next_queued_message() is None
        assert calls == []  # not before the results are committed
        await asyncio.sleep(0)
        assert calls == ["handoff"]
        assert state.conversation.handoff_pending() is False
        assert session._handoff_pending is False
    finally:
        blocker.set()
        await turn


async def test_a_turn_that_ends_first_hands_off_when_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, state, calls = _session(monkeypatch)
    blocker = asyncio.Event()
    turn = asyncio.create_task(blocker.wait())
    session._agent_task = turn
    state.conversation.start_running_tool(RunningTool("t1", "$", "make", time.time()))
    session.dispatch_slash("/detach")
    blocker.set()
    await turn
    session.advance()
    assert calls == ["handoff"]


def test_with_no_tool_running_the_handoff_is_immediate(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _state, calls = _session(monkeypatch)
    assert session.dispatch_slash("/background") is True
    assert calls == ["handoff"]


def test_without_a_running_turn_a_stale_tool_row_does_not_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, state, calls = _session(monkeypatch)
    state.conversation.start_running_tool(RunningTool("t1", "$", "make", time.time()))
    session.dispatch_slash("/bg")
    assert calls == ["handoff"]
