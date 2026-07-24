"""End-to-end input-to-session coverage for PRD-143."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.commands import build_builtin_registry
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime.commands import SendMessageCommand

from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_input_submit_runs_usage_before_slow_active_task_finishes() -> None:
    session, ctx, _workspace, _input = _make_session()
    usage = build_builtin_registry().get("/usage")
    assert usage is not None
    ctx.cmd_registry.register(usage)

    bus = ctx.command_bus
    bus.register(SendMessageCommand, session.handle_send)
    input_session = UnifiedInputSession(ctx.app_state, bus)
    active = asyncio.create_task(asyncio.Event().wait())
    session._agent_task = active
    ctx.app_state.conversation.set_tokens(7, 8, 0.005)

    await input_session._submit("/usage")
    await input_session._submit("keep this queued")

    output = ctx.console.export_text()
    assert "Usage: input=7 output=8 total=15 cost=$0.0050 state=running" in output
    assert session._msg_queue == ["keep this queued"]
    assert all(
        event.kind != "user_message"
        for turn in ctx.app_state.conversation.turns()
        for event in turn.events
    )

    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
