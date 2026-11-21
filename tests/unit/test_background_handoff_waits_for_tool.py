"""``/bg``, ``/background`` and ``/detach`` wait for the running tool to finish.

The handoff happens at the next tool boundary, once the call's result is
committed, or when the turn ends; with no tool running it is immediate
(operator request, 2026-11-07).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from agenthicc.tui.conversation_store import AppState, RunningTool

pytestmark = pytest.mark.unit


class _Queue:
    def claim_message(self) -> str | None:
        return None


def _session(monkeypatch: pytest.MonkeyPatch) -> tuple[object, AppState, list[str]]:
    from agenthicc.background import integration
    from agenthicc.runners.tui_session import TUISession

    calls: list[str] = []
    monkeypatch.setattr(integration, "_handoff", lambda session: calls.append("handoff") or True)
    state = AppState.create()
    session = TUISession.__new__(TUISession)
    session._ctx = SimpleNamespace(app_state=state)  # type: ignore[attr-defined]
    session._msg_queue = _Queue()  # type: ignore[attr-defined]
    session._agent_task = None  # type: ignore[attr-defined]
    session._pending_model = None  # type: ignore[attr-defined]
    return session, state, calls


@pytest.mark.parametrize("command", ["/bg", "/background", "/detach"])
async def test_a_running_tool_delays_the_handoff_to_the_next_boundary(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    session, state, calls = _session(monkeypatch)
    blocker = asyncio.Event()
    session._agent_task = asyncio.create_task(blocker.wait())  # type: ignore[attr-defined]
    state.conversation.start_running_tool(RunningTool("t1", "$", "make test", time.time()))
    try:
        assert session.dispatch_slash(command) is True  # type: ignore[attr-defined]
        assert calls == []  # the tool is still running
        # A blinking row in the live region says the hand-off is waiting.
        assert state.conversation.handoff_pending() is True
        assert state.conversation.notification() is None
        assert (
            session.dispatch_slash("/bg") is True
        )  # a repeat does not queue twice  # type: ignore[attr-defined]

        # The tool finishes: at the boundary the queued-input hook schedules it.
        assert session._next_queued_message() is None  # type: ignore[attr-defined]
        assert calls == []  # not before the results are committed
        await asyncio.sleep(0)
        assert calls == ["handoff"]
        assert state.conversation.handoff_pending() is False
        assert session._handoff_pending is False  # type: ignore[attr-defined]
    finally:
        blocker.set()
        await session._agent_task  # type: ignore[attr-defined]


async def test_a_turn_that_ends_first_hands_off_when_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, state, calls = _session(monkeypatch)
    blocker = asyncio.Event()
    session._agent_task = asyncio.create_task(blocker.wait())  # type: ignore[attr-defined]
    state.conversation.start_running_tool(RunningTool("t1", "$", "make", time.time()))
    session.dispatch_slash("/detach")  # type: ignore[attr-defined]
    blocker.set()
    await session._agent_task  # type: ignore[attr-defined]
    session.advance()  # type: ignore[attr-defined]
    assert calls == ["handoff"]


def test_with_no_tool_running_the_handoff_is_immediate(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _state, calls = _session(monkeypatch)
    assert session.dispatch_slash("/background") is True  # type: ignore[attr-defined]
    assert calls == ["handoff"]


def test_without_a_running_turn_a_stale_tool_row_does_not_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, state, calls = _session(monkeypatch)
    state.conversation.start_running_tool(RunningTool("t1", "$", "make", time.time()))
    session.dispatch_slash("/bg")  # type: ignore[attr-defined]
    assert calls == ["handoff"]
