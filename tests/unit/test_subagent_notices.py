"""Notice projection, composer hints, and interruptible collection."""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.runners.agent_turn_context import TurnInputReason, bind_turn_notices
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.tui_session import TUISession
from agenthicc.subagents.communication import (
    AgentMessageBroker,
    PoolHandle,
    make_parent_communication_tools,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import InputMode, UnifiedInputSession
from agenthicc.tui.runtime.commands import CommandBus
from agenthicc.tui.runtime.session_log import user_message_history_from_events
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from tests.integration.test_subagent_wake import record_terminal
from tests.unit.test_tui_session_coverage import _make_session


def test_a13_control_b_requires_foreground_work_in_a_running_turn() -> None:
    app = AppState.create()
    session = UnifiedInputSession(app, CommandBus())
    session.set_mode(InputMode.STREAMING)
    # A pool already running in the background is not foreground work.
    app.conversation.subagent_background_running.set(1)
    keymap = app.keymap()
    assert keymap.resolve("composer", "ctrl+b", session.conditions()) is None

    app.conversation.subagent_foreground.set(frozenset({"foreground-pool"}))
    action = keymap.resolve("composer", "ctrl+b", session.conditions())
    assert action is not None and action.name == "work.background"
    session.set_mode(InputMode.IDLE)
    assert keymap.resolve("composer", "ctrl+b", session.conditions()) is None
    session.set_mode(InputMode.STREAMING)
    app.conversation.subagent_foreground.set(frozenset())
    assert "backgroundable" not in session.conditions()

    # A command a tool call waits on is foreground work on its own, with no pool.
    app.conversation.foreground_commands.set(frozenset({"fg-1"}))
    action = keymap.resolve("composer", "ctrl+b", session.conditions())
    assert action is not None and action.name == "work.background"
    app.conversation.foreground_commands.set(frozenset())
    assert keymap.resolve("composer", "ctrl+b", session.conditions()) is None


@pytest.mark.parametrize("source", ["subagent_worker", "subagent_question", "terminal_notice"])
@pytest.mark.parametrize("verbose", [True, False], ids=["verbose-transcript", "live"])
def test_a14_automated_notice_rendering_and_user_history(source: str, verbose: bool) -> None:
    app = AppState.create()
    console = Console(file=StringIO(), width=120)
    appender = ScrollBufferAppender(
        app, console, fixed_detail="verbose" if verbose else None, wrap=verbose
    )
    appender.mount()
    with console.capture() as output:
        if verbose:
            event = app.conversation.append_event(
                "user_message",
                {"text": "Automatic provider message", "source": source, "summary": "Pool notice"},
            )
            appender.replay((event,))
        else:
            event = app.conversation.append_event(
                "user_message",
                {"text": "Automatic provider message", "source": source, "summary": "Pool notice"},
            )
        appender.flush()
    appender.unmount()
    assert "↩ Pool notice" in output.get()
    assert "❯" not in output.get()
    assert user_message_history_from_events((event,)) == []
    assert not app.conversation.session_title()


@pytest.mark.parametrize("reason", ["user_input", "notice"])
async def test_a15_collect_wait_yields_to_pending_input(
    tmp_path: Path,
    reason: TurnInputReason,
) -> None:
    session, ctx, _workspace, _input = _make_session()
    assert isinstance(session, TUISession)
    conversation = SessionConversation.open(
        ctx.session_id,
        max_tokens=5_000,
        journal_path=tmp_path / "journal.jsonl",
    )
    ctx.session_conversation = conversation
    source = session._install_subagent_notices()
    assert source is not None
    broker = AgentMessageBroker(
        conversation_id=ctx.session_id, parent_run_id="turn", pool_id="pool"
    )
    pool = asyncio.create_task(asyncio.Event().wait())
    source.registry.register(PoolHandle("pool", ctx.session_id, broker, pool))
    tools = make_parent_communication_tools(source.registry, ctx.session_id, source.journal)
    collect = next(tool for tool in tools if tool.__name__ == "collect_subagent_results")
    with bind_turn_notices(source, session._subscribe_subagent_turn_input):
        waiting = asyncio.create_task(collect("pool", wait=True, timeout_s=60))
    try:
        await asyncio.sleep(0)
        assert not waiting.done()
        assert len(session._subagent_input_listeners) == 1
        if reason == "user_input":
            ctx.app_state.conversation.queue.append("Queued input")
        else:
            record_terminal(conversation.journal, "finished")
            session._notify_subagent_turn_input()
        await asyncio.sleep(0)
        assert waiting.done()
        result = waiting.result()
        assert not session._subagent_input_listeners
        assert result["status"] == "running"
        assert result["interrupted"] == reason
        assert not result.get("timed_out", False)
        assert not pool.done()
    finally:
        waiting.cancel()
        pool.cancel()
        await asyncio.gather(waiting, pool, return_exceptions=True)
        conversation.close()
