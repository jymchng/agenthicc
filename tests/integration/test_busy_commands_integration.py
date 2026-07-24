"""Integration coverage for PRD-143 command routing during a slow run."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.commands import build_builtin_registry
from agenthicc.runners import tui_session as tui_session_module
from agenthicc.tui.runtime.commands import SendMessageCommand

from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_slow_agent_run_accepts_usage_immediately_then_releases_fifo_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, ctx, _workspace, _input = _make_session()
    usage = build_builtin_registry().get("/usage")
    assert usage is not None
    ctx.cmd_registry.register(usage)

    bus = ctx.command_bus
    bus.register(SendMessageCommand, session.handle_send)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_agent_turn(text: str, *args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append(text)
        if text == "first request":
            started.set()
            await release.wait()

    monkeypatch.setattr(tui_session_module, "_run_agent_turn", fake_agent_turn)
    monkeypatch.setattr(tui_session_module, "_make_session_tools", lambda *args, **kwargs: [])

    await bus.dispatch_async(SendMessageCommand(text="first request"))
    first_task = session._agent_task
    assert first_task is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    ctx.app_state.conversation.set_tokens(10, 4, 0.12)
    await bus.dispatch_async(SendMessageCommand(text="/usage"))
    await bus.dispatch_async(SendMessageCommand(text="second request"))

    assert (
        "Usage: input=10 output=4 total=14 cost=$0.1200 state=running" in ctx.console.export_text()
    )
    assert session._msg_queue == ["second request"]
    assert calls == ["first request"]

    release.set()
    await first_task
    second_task = session._agent_task
    if second_task is not None:
        await second_task

    assert calls == ["first request", "second request"]
    assert session._msg_queue == []
