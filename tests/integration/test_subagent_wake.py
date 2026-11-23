"""Durable wake, provider boundary, operator controls, and recovery scenarios."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from lauren_ai._transport import Completion
from rich.text import Text

from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.tool_history import ToolHistoryRecoveryState
from agenthicc.runners.tui_session import TUISession
from agenthicc.runners.workflow_stack import WorkflowCall
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.unified_session import InputMode
from agenthicc.tui.runtime.commands import InterruptAgentCommand, SendMessageCommand
from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
from agenthicc.workflows.plugin import WorkflowPlugin
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


def record_terminal(journal: ConversationJournal, pool_id: str) -> None:
    journal.subagent_pool_started(
        pool_id=pool_id,
        fingerprint=pool_id,
        parent_run_id="origin",
        tasks=[{"task_id": "task-0", "agent_type": "explorer"}],
    )
    journal.subagent_pool_backgrounded(pool_id=pool_id, tool_result={"status": "running"})
    journal.subagent_pool_finished(
        pool_id=pool_id,
        total=1,
        succeeded=1,
        failed=0,
        text=WORKER_RESULT,
    )


def notices(request: ModelRequest) -> list[str]:
    return [
        message["content"]
        for message in request.messages
        if message["role"] == "user"
        and isinstance(message["content"], str)
        and "Background subagent pool" in message["content"]
    ]


async def unused_worker(request: ModelRequest) -> Completion:
    pytest.fail(f"Unexpected worker request for {request.model}")


@pytest.mark.parametrize("completion", ["pending", "at-boundary", "deferred"])
async def test_a3_a5_queued_input_and_completion_follow_the_tool_results(
    session: TUISession,
    completion: str,
) -> None:
    """Real tool results precede the notice, then queued input in provider memory."""
    Path("source.txt").write_text("read evidence", encoding="utf-8")
    release = asyncio.Event()
    main_entered = asyncio.Event()
    release_main = asyncio.Event()
    pool_id = ""

    async def worker(_request: ModelRequest) -> Completion:
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            assert request.contains('"status": "running"')
            return reply("Worker is running.")
        if sequence == 3:
            main_entered.set()
            await release_main.wait()
            return tool_call("read_file", {"path": "source.txt"}, use_id="read")
        assert request.contains("read evidence")
        transcript = repr(request.messages)
        assert WORKER_RESULT not in transcript
        if completion == "pending":
            assert not notices(request)
            assert transcript.index("read evidence") < transcript.index("Add this detail")
        elif completion == "at-boundary":
            assert len(notices(request)) == 1 and pool_id in notices(request)[0]
            assert transcript.index("read evidence") < transcript.index(notices(request)[0])
            assert transcript.index(notices(request)[0]) < transcript.index("Add this detail")
        else:
            assert len(notices(request)) == 1
        return reply("Read and responded.")

    script = ScenarioTransport(main, worker)
    attach(session, script)
    await (await send(session, "Start a pool."))
    source = session._install_subagent_notices()
    assert source is not None
    (handle,) = source.registry.handles()
    pool_id = handle.pool_id
    if completion == "deferred":
        session._ctx.cfg = replace(
            session._ctx.cfg,
            execution=replace(session._ctx.cfg.execution, max_agent_turns=1),
        )
    turn = await send(session, "Read the sample while it runs.")
    await until(main_entered.is_set)
    if completion != "deferred":
        await session.handle_send(SendMessageCommand(text="Add this detail"))
    if completion != "pending":
        release.set()
        await handle.task
        assert source.has_pending()
        assert session._agent_task is turn
        assert len(script.main_requests) == 3
    release_main.set()
    await turn
    if completion == "deferred":
        await until(lambda: len(script.main_requests) == 4)
        assert (
            script.main_requests[-1].messages[-1]["content"] == notices(script.main_requests[-1])[0]
        )
        assert source.journal.delivered_subagent_pool_notifications() == {pool_id}
    else:
        assert session._agent_task is None
        assert not session._msg_queue
        assert len(script.main_requests) == 4
        if completion == "pending":
            assert not handle.task.done()
    release.set()


async def test_a4_repeated_idle_wakes_have_no_missed_or_duplicate_receipts(
    session: TUISession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the real scheduler and durable delivery 200 times with a fake clock."""
    from agenthicc.subagents.communication import AgentMessageBroker, PoolHandle

    source = session._install_subagent_notices()
    assert source is not None
    seen: set[str] = set()
    schedule: list[float] = []
    clock = 0.0

    async def capture(text: str, resume: object = None, *, wake_key: str | None = None) -> None:
        assert text == "" and resume is None and wake_key is not None
        message = session._ctx.session_conversation.memory._messages[-1]
        assert message["role"] == "user" and WORKER_RESULT not in str(message["content"])
        assert wake_key not in seen
        seen.add(wake_key)
        schedule.append(clock)

    monkeypatch.setattr(session, "run_turn", capture)
    for index in range(200):
        pool_id = f"repeat-{index}"
        broker = AgentMessageBroker(
            conversation_id=session._ctx.session_id, parent_run_id="origin", pool_id=pool_id
        )

        async def finish(identity: str = pool_id) -> None:
            record_terminal(source.journal, identity)

        task = asyncio.create_task(finish())
        source.registry.register(PoolHandle(pool_id, session._ctx.session_id, broker, task))
        source.registry.mark_backgrounded(pool_id)
        await task
        # A fresh notice is visible in this scheduling tick.
        terminal_at = clock
        while len(seen) <= index and clock - terminal_at < 0.1:
            await asyncio.sleep(0)
            clock += 0.001
        assert len(seen) == index + 1
        assert schedule[-1] - terminal_at < 0.1
        assert source.journal.delivered_subagent_pool_notifications() == {
            f"repeat-{done}" for done in range(index + 1)
        }
        assert session._agent_task is None
        clock += 1
    assert len(seen) == 200
    assert len(session._ctx.session_conversation.memory._messages) == 200


@pytest.mark.parametrize("seam", ["terminal", "message", "ack"])
async def test_a6_process_death_at_delivery_seams_reopens_without_waking(
    session: TUISession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    conversation = session._ctx.session_conversation
    session_id = session._ctx.session_id
    journal_path = conversation.journal.path
    conversation.close()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        str(journal_path),
        seam,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    try:
        assert proc.stdout is not None
        assert await asyncio.wait_for(proc.stdout.readline(), timeout=8) == b"ready\n"
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    assert proc.returncode != 0

    async def main(request: ModelRequest, sequence: int) -> Completion:
        assert sequence == 1
        assert len(notices(request)) == 1
        assert "crash-pool" in notices(request)[0]
        assert WORKER_RESULT not in repr(request.messages)
        return reply("Recovered the notice.")

    async with tui(tmp_path, monkeypatch, resume_id=session_id) as resumed:
        script = ScenarioTransport(main, unused_worker)
        attach(resumed, script)
        source = resumed._install_subagent_notices()
        assert source is not None
        resumed.reconcile_subagent_wakes()
        assert resumed._agent_task is None and not script.main_requests
        await (await send(resumed, "Continue from disk."))
        assert len(script.main_requests) == 1
        assert source.journal.delivered_subagent_pool_notifications() == {"crash-pool"}


async def test_a7_dropped_terminal_callback_reconciles_and_reports_perf(
    session: TUISession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agenthicc.runners import tui_session
    from agenthicc.subagents.communication import AgentMessageBroker, PoolHandle

    source = session._install_subagent_notices()
    assert source is not None
    source.registry.on_terminal(None)
    requested_sleeps: list[float] = []
    tick = asyncio.Event()

    async def sleep_until_tick(seconds: float) -> None:
        requested_sleeps.append(seconds)
        await tick.wait()
        tick.clear()

    monkeypatch.setattr(tui_session, "_sleep_for_subagent_reconciliation", sleep_until_tick)
    wake_seen = asyncio.Event()

    async def main(request: ModelRequest, sequence: int) -> Completion:
        assert sequence == 1 and len(notices(request)) == 1
        wake_seen.set()
        return reply("Reconciled.")

    attach(session, ScenarioTransport(main, unused_worker))
    release = asyncio.Event()

    async def finish() -> None:
        await release.wait()
        record_terminal(source.journal, "missed")

    task = asyncio.create_task(finish())
    broker = AgentMessageBroker(
        conversation_id=session._ctx.session_id,
        parent_run_id="origin",
        pool_id="missed",
    )
    source.registry.register(PoolHandle("missed", session._ctx.session_id, broker, task))
    source.registry.mark_backgrounded("missed")
    await until(lambda: bool(requested_sleeps))
    assert requested_sleeps == [30.0]
    release.set()
    await task
    assert not wake_seen.is_set() and session._agent_task is None
    with caplog.at_level(logging.INFO, logger=tui_session.__name__):
        tick.set()
        await until(wake_seen.is_set)
    assert "subagent_wake_recovered pool=missed" in caplog.text
    assert session.route("/perf")
    assert "Subagent wakes recovered: 1" in session._ctx.console.export_text()


async def test_a8_a9_esc_pauses_automatic_wakes_without_submitting_the_draft(
    session: TUISession,
) -> None:
    source = session._install_subagent_notices()
    assert source is not None
    waiting = asyncio.Event()

    async def main(request: ModelRequest, sequence: int) -> Completion:
        assert "half-typed draft" not in repr(request.messages)
        if sequence == 1:
            waiting.set()
            await asyncio.Event().wait()
        assert sequence == 2 and len(notices(request)) == 2
        assert request.contains("I am back")
        return reply("Both notices received.")

    script = ScenarioTransport(main, unused_worker)
    attach(session, script)
    session._input_session.set_text("half-typed draft")
    record_terminal(source.journal, "first-pool")
    session.advance(True)
    task = session._agent_task
    assert task is not None
    await until(waiting.is_set)
    assert session._input_session._mode == InputMode.STREAMING
    session.handle_interrupt(InterruptAgentCommand(source="escape"))
    await task
    assert "".join(session._ctx.app_state.input.buf()) == "half-typed draft"
    assert session._input_session._mode == InputMode.IDLE
    record_terminal(source.journal, "second-pool")
    session.advance(True)
    assert session._agent_task is None and len(script.main_requests) == 1
    assert source.has_pending()
    status = Text.from_markup(session._workspace.status.markup(160)).plain
    assert "subagent wakes paused" in status
    await (await send(session, "I am back"))
    assert session._ctx.app_state.conversation.subagent_wake_paused() is False
    assert not source.has_pending()


@pytest.mark.parametrize("busy", [False, True], ids=["idle", "mid-turn"])
async def test_a10_background_approval_uses_the_named_overlay_and_denial_is_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    busy: bool,
) -> None:
    request_approval = asyncio.Event()
    denied = asyncio.Event()
    finish_worker = asyncio.Event()
    parent_entered = asyncio.Event()
    finish_parent = asyncio.Event()

    async def worker(request: ModelRequest) -> Completion:
        if not request.contains("tool_result"):
            await request_approval.wait()
            return tool_call(
                "write_file", {"path": "approval.txt", "content": "approved"}, use_id="write"
            )
        assert not Path("approval.txt").exists()
        denied.set()
        await finish_worker.wait()
        return reply("The edit was denied.")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call(kind="implementer")
        if busy and sequence == 3:
            parent_entered.set()
            await finish_parent.wait()
        return reply("Continuing the main session.")

    async with tui(tmp_path, monkeypatch, mode="Safe") as session:
        session._wire_approval_overlay()
        script = ScenarioTransport(main, worker)
        attach(session, script)
        await (await send(session, "Delegate the edit."))
        parent = None
        if busy:
            parent = await send(session, "Meanwhile explain the plan.")
            await until(parent_entered.is_set)
        source = session._install_subagent_notices()
        assert source is not None
        (handle,) = source.registry.handles()
        request_approval.set()
        await until(lambda: isinstance(session._workspace.overlays.widget, ApprovalOverlay))
        request = session._ctx.app_state.pending_approval()
        assert request is not None and request.requester is not None
        assert request.requester.label == "implementer #1"
        assert request.requester.pool_id == handle.pool_id
        assert session._agent_task is parent and not handle.task.done()
        overlay = session._workspace.overlays.widget
        assert isinstance(overlay, ApprovalOverlay)
        with session._ctx.console.capture() as capture:
            session._ctx.console.print(overlay.render())
        assert "implementer #1" in capture.get()
        assert overlay.handle_key(Key.ESC, "")
        await until(denied.is_set)
        assert not handle.task.done() and not Path("approval.txt").exists()
        finish_parent.set()
        finish_worker.set()
        if parent is not None:
            await parent
        await handle.task


async def test_a11_a_question_wakes_and_answer_resumes_the_worker(session: TUISession) -> None:
    ask = asyncio.Event()

    async def worker(request: ModelRequest) -> Completion:
        if request.contains("Use the blue input."):
            return reply(WORKER_RESULT)
        if request.contains("tool_result"):
            return reply("Waiting for clarification.")
        await ask.wait()
        return tool_call("ask_parent", {"question": "private question body"}, use_id="ask")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            return reply("I will wait for the notice.")
        source = session._subagent_notices
        assert source is not None
        (handle,) = source.registry.handles()
        if sequence == 3:
            assert request.contains("is waiting for your answer to question")
            assert not request.contains("private question body")
            return tool_call("collect_subagent_results", {"pool_id": handle.pool_id})
        if sequence == 4:
            assert request.contains("private question body")
            (question,) = handle.broker.pending_questions(recipient="main")
            return tool_call(
                "answer_subagent",
                {
                    "pool_id": handle.pool_id,
                    "question_id": question["question_id"],
                    "answer": "Use the blue input.",
                },
            )
        return reply("The worker received the answer.")

    script = ScenarioTransport(main, worker)
    attach(session, script)
    await (await send(session, "Delegate the ambiguous work."))
    assert len(script.main_requests) == 2 and session._agent_task is None
    ask.set()
    await until(lambda: len(script.worker_requests) == 3)
    await until(lambda: session._agent_task is None)
    assert script.worker_requests[-1].contains("Use the blue input.")
    assert (
        sum(
            event.payload.get("source") == "subagent_question"
            for event in session._ctx.app_state.conversation._events_by_id.values()
        )
        == 1
    )


@pytest.mark.parametrize("guard", ["busy", "paused-frame", "history", "handoff"])
async def test_a21_each_guard_defers_the_wake_until_it_clears(
    session: TUISession,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    class Inspect(WorkflowPlugin):
        name = "inspect-guard"

    source = session._install_subagent_notices()
    assert source is not None
    stack = session._ctx.workflow_stack
    memory = session._ctx.session_conversation.memory
    busy = None
    frame = None
    if guard == "busy":
        busy = asyncio.create_task(asyncio.sleep(300))
        session._agent_task = busy
    elif guard == "paused-frame":
        frame = stack.push(WorkflowCall(Inspect, "Inspect.", "user"))
        stack.pause(frame.frame_id)
    elif guard == "history":
        memory._tool_history_recovery = ToolHistoryRecoveryState(
            "repair_required", ("invalid",), True
        )
    else:
        session._handoff_pending = True
        monkeypatch.setattr(session, "_run_background_handoff", lambda: False)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        assert sequence == 1 and len(notices(request)) == 1
        return reply("The guard cleared.")

    script = ScenarioTransport(main, unused_worker)
    attach(session, script)
    record_terminal(source.journal, "guarded")
    session.advance(True)
    assert session._agent_task is busy
    assert source.has_pending() and not script.main_requests
    if busy is not None:
        busy.cancel()
        await asyncio.gather(busy, return_exceptions=True)
    if frame is not None:
        stack.abandon(frame.frame_id)
    memory._tool_history_recovery = ToolHistoryRecoveryState()
    session._handoff_pending = False
    session.reconcile_subagent_wakes()
    task = session._agent_task
    assert task is not None
    await task
    assert source.journal.delivered_subagent_pool_notifications() == {"guarded"}
    session.reconcile_subagent_wakes()
    assert session._agent_task is None and len(script.main_requests) == 1


if __name__ == "__main__":
    from agenthicc.memory.journaled import JournaledShortTermMemory
    from agenthicc.subagents.delivery import JournalNoticeSource, deliver_notices
    from agenthicc.subagents.communication import PoolContinuationRegistry

    journal = ConversationJournal(Path(sys.argv[1]))
    record_terminal(journal, "crash-pool")
    if sys.argv[2] != "terminal":
        memory = JournaledShortTermMemory(journal)
        source = JournalNoticeSource(journal, PoolContinuationRegistry())
        if sys.argv[2] == "message":
            (notice,) = source.claim()
            memory.add_user_once(notice.text, message_id=notice.message_id)
        else:
            deliver_notices(source.claim(), memory, None)
    print("ready", flush=True)
    sys.stdin.read()
