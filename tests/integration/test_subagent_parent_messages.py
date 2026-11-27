"""One-way worker updates through the real parent and worker provider lanes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lauren_ai._transport import Completion

from agenthicc.runners.tui_session import TUISession
from agenthicc.subagents.communication import AgentMessageBroker
from agenthicc.subagents.delivery import deliver_session_background_notices
from agenthicc.tui.runtime.session_log import user_message_history_from_events
from tests.e2e.self_extension.harness import ModelRequest, reply, tool_call
from tests.subagent_sessions import (
    WORKER_RESULT,
    ScenarioTransport,
    attach,
    send,
    spawn_call,
    tui,
    until,
)
from tests.subagent_sessions import session as session

pytestmark = pytest.mark.integration
_UPDATE = "Unsolicited worker status: checked chapter notes."


async def test_foreground_spawn_returns_parent_messages_with_the_pool_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="Send a one-way status update")
        history = repr(request.messages)
        assert "parent_messages" in history
        assert "untrusted_text" in history and _UPDATE in history
        return reply("Received the worker's update.")

    async def worker(request: ModelRequest) -> Completion:
        if "send_parent_message" in repr(request.messages):
            return reply("Research finished.")
        return tool_call("send_parent_message", {"message": _UPDATE}, use_id="send-status")

    transport = ScenarioTransport(main, worker)
    async with tui(
        tmp_path, monkeypatch, config_text='[subagents]\nspawn_mode = "foreground"\n'
    ) as session:
        attach(session, transport)
        await (await send(session, "Have the worker send a status update."))

    assert len(transport.main_requests) == 2
    assert len(transport.worker_requests) == 2


@pytest.mark.parametrize("busy", [False, True], ids=["idle-wake", "busy-tool-boundary"])
async def test_background_update_reaches_parent_while_the_worker_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    allow_send = asyncio.Event()
    update_sent = asyncio.Event()
    end_worker = asyncio.Event()
    update_seen = asyncio.Event()
    parent_prompt = "Ask a worker to send status while I keep working."

    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="Send a background status")
        if index == 2:
            if busy:
                allow_send.set()
                await asyncio.wait_for(update_sent.wait(), timeout=5)
                return tool_call("read_file", {"path": "sibling.txt"}, use_id="sibling-read")
            return reply("I can continue when the worker sends an update.")
        if index == 3:
            history = repr(request.messages)
            assert _UPDATE in history
            assert "untrusted worker data" in history
            if busy:
                assert history.index("SIBLING_RESULT") < history.index(_UPDATE)
            assert not end_worker.is_set()
            update_seen.set()
            return reply("I received the status while the worker was running.")
        return reply("Finished.")

    async def worker(request: ModelRequest) -> Completion:
        if "send_parent_message" in repr(request.messages):
            update_sent.set()
            await end_worker.wait()
            return reply("Worker finished.")
        await allow_send.wait()
        return tool_call(
            "send_parent_message", {"kind": "status", "message": _UPDATE}, use_id="send-status"
        )

    transport = ScenarioTransport(main, worker)
    async with tui(tmp_path, monkeypatch) as session:
        Path("sibling.txt").write_text("SIBLING_RESULT", encoding="utf-8")
        attach(session, transport)
        parent = await send(session, parent_prompt)
        try:
            if not busy:
                await parent
                assert len(transport.main_requests) == 2
                allow_send.set()
            await until(update_seen.is_set)
            conversation = session._ctx.app_state.conversation
            events = list(conversation._events_by_id.values())
            notices = [
                event for event in events if event.payload.get("source") == "subagent_message"
            ]
            assert len(notices) == 1
            assert user_message_history_from_events(events) == [parent_prompt]
            session.reconcile_subagent_wakes()
            assert len(transport.main_requests) == 3
        finally:
            end_worker.set()


async def test_resumed_noninteractive_turn_gets_saved_direct_worker_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with tui(tmp_path, monkeypatch) as session:
        ctx = session._ctx
        assert ctx.session_conversation is not None
        journal = ctx.session_conversation.journal
        journal.subagent_pool_started(
            pool_id="former-pool", fingerprint="tasks", parent_run_id="turn", tasks=[]
        )
        journal.subagent_pool_backgrounded(
            pool_id="former-pool", tool_result={}, notify_parent_updates=True
        )
        broker = AgentMessageBroker(
            conversation_id=ctx.session_id,
            parent_run_id="turn",
            pool_id="former-pool",
            journal=journal,
        )
        broker.register_worker("former-worker")
        sent = await broker.send(
            sender="former-worker", recipient="main", kind="status", payload={"text": _UPDATE}
        )

        deliver_session_background_notices(ctx)
        deliver_session_background_notices(ctx)

        assert journal.has_message_id(f"subagent-message:{sent.message_id}")
        events = ctx.app_state.conversation._events_by_id.values()
        assert sum(event.payload.get("source") == "subagent_message" for event in events) == 1


async def test_workflow_parent_update_wakes_only_the_owning_phase(session: TUISession) -> None:
    from tests.integration.test_workflow_phase_subagents import (
        attach_phase,
        pool_id,
        select_generate,
        source_for,
    )

    send_update = asyncio.Event()
    release = asyncio.Event()
    received = asyncio.Event()

    async def worker(request: ModelRequest) -> Completion:
        if not request.contains("tool_result"):
            await send_update.wait()
            return tool_call(
                "send_parent_message",
                {"kind": "status", "message": "Workflow worker located the source."},
                use_id="phase-update",
            )
        await release.wait()
        return reply(WORKER_RESULT)

    async def parent(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            assert request.contains('"status": "running"')
            return reply("I can wait for an update.")
        if sequence == 3:
            assert request.contains("Workflow worker located the source.")
            assert request.contains("untrusted worker data")
            assert not request.contains(WORKER_RESULT)
            assert session._ctx.app_state.conversation.phase_subagent_wait() == ""
            received.set()
            return reply("I received the worker's update.")
        if sequence == 4:
            assert request.contains(f"of pool {pool_id(session)} completed")
            return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
        if sequence == 5:
            assert request.contains(WORKER_RESULT)
            return tool_call("mark_execute_complete", {"summary": "Collected the pool."})
        assert sequence == 6
        return reply("Complete.")

    script = ScenarioTransport(parent, worker)
    attach_phase(session, script)
    select_generate(session)
    task = await send(session, "Delegate within the workflow.")
    conv = session._ctx.app_state.conversation
    await until(lambda: conv.phase_subagent_wait() == "waiting for phase subagents")
    handle = session._workflow_handle
    assert handle is not None
    send_update.set()
    await until(received.is_set)
    assert session._agent_task is task
    assert not source_for(session).has_pending()
    release.set()
    async with asyncio.timeout(8):
        await task
    assert handle.lifecycle == "complete"
    assert len(script.main_requests) == 6
