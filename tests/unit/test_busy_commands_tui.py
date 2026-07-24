"""TUISession regression tests for commands submitted during active runs."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.commands import BusyPolicy, Command, build_builtin_registry
from agenthicc.tui.runtime.commands import SendMessageCommand

from .test_tui_session_coverage import _make_session

pytestmark = pytest.mark.unit


async def _live_task() -> None:
    await asyncio.Event().wait()


async def _stop(task: asyncio.Task[object]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_background_lookup_can_use_the_latest_plain_submission() -> None:
    from agenthicc.background.integration import _last_user_text

    session, _ctx, _workspace, _input = _make_session()
    session._last_submitted_text = "review the auth flow"

    assert _last_user_text(session) == "review the auth flow"


@pytest.mark.asyncio
async def test_usage_runs_now_during_active_run_without_queue_or_user_event() -> None:
    session, ctx, _workspace, _input = _make_session()
    usage = build_builtin_registry().get("/usage")
    assert usage is not None
    ctx.cmd_registry.register(usage)
    ctx.app_state.conversation.set_tokens(123, 45, 0.6789)
    active = asyncio.create_task(_live_task())
    session._agent_task = active

    await session.handle_send(SendMessageCommand(text="/usage"))

    output = ctx.console.export_text()
    assert "Usage: input=123 output=45 total=168 cost=$0.6789 state=running" in output
    assert session._msg_queue == []
    assert all(
        event.kind != "user_message"
        for turn in ctx.app_state.conversation.turns()
        for event in turn.events
    )
    await _stop(active)


@pytest.mark.asyncio
async def test_read_only_and_control_commands_are_immediate_but_messages_queue() -> None:
    session, ctx, _workspace, _input = _make_session()
    invoked: list[str] = []
    ctx.cmd_registry.register(
        Command(
            "/inspect",
            "Inspect local state",
            busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
            handler=lambda command_ctx: invoked.append(command_ctx.args) or True,
        )
    )
    ctx.cmd_registry.register(
        Command(
            "/defer",
            "Deferred mutation",
            handler=lambda _ctx: invoked.append("deferred") or True,
        )
    )
    active = asyncio.create_task(_live_task())
    session._agent_task = active

    await session.handle_send(SendMessageCommand(text="/inspect local"))
    await session.handle_send(SendMessageCommand(text="/defer"))
    await session.handle_send(SendMessageCommand(text="follow-up"))

    assert invoked == ["local"]
    assert session._msg_queue == ["/defer", "follow-up"]
    assert "Queued #2: follow-up" in (ctx.app_state.conversation.notification() or "")
    await _stop(active)


@pytest.mark.asyncio
async def test_cancel_alias_uses_the_same_task_owner_as_interrupt() -> None:
    session, ctx, _workspace, _input = _make_session()
    ctx.cmd_registry.register(
        Command(
            "/cancel",
            "Cancel",
            aliases=("/interrupt",),
            source_id="builtin",
            busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
            handler=lambda command_ctx: (
                command_ctx.cancel_active is not None and command_ctx.cancel_active()
            ),
        )
    )
    active = asyncio.create_task(_live_task())
    session._agent_task = active

    await session.handle_send(SendMessageCommand(text="/interrupt"))
    await asyncio.sleep(0)

    assert active.cancelled()
    assert session._msg_queue == []


@pytest.mark.asyncio
async def test_queued_command_is_revalidated_when_registry_changes() -> None:
    session, ctx, _workspace, _input = _make_session()
    active = asyncio.create_task(_live_task())
    session._agent_task = active
    await session.handle_send(SendMessageCommand(text="/will-be-removed"))
    assert session._msg_queue == ["/will-be-removed"]

    session._agent_task = None
    session.advance()

    assert session._msg_queue == []
    assert "no longer exists" in (ctx.app_state.conversation.notification() or "")
    await _stop(active)
