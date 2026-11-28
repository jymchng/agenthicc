"""COMM-03: a parent's message reaches a worker between its tool calls.

Before, a worker read its mail at the start of each provider run, or when its
model chose to call ``poll_agent_messages``.  A worker partway through a run kept
working on a plan the parent had already replaced.  These tests send the message
while the worker is busy and assert it arrives in the worker's *next* provider
request, after the tool result and with no poll from the model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lauren_ai._transport import Completion
from rich.text import Text

from agenthicc.subagents.communication import registry_for_session
from agenthicc.subagents.transcripts import list_pools, read_transcript
from agenthicc.tui.workspace.overlays.subagents import transcript_lines
from tests.e2e.self_extension.harness import ModelRequest, reply, tool_call
from tests.subagent_sessions import ScenarioTransport, attach, send, spawn_call, tui, until

pytestmark = pytest.mark.integration

_MAIL = "Stop: the API changed, use v2 and ignore the old plan."


async def test_a_parent_message_reaches_a_busy_worker_between_its_tool_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_started = asyncio.Event()
    mail_sent = asyncio.Event()
    worker_histories: list[str] = []

    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="Read the note, then report")
        return reply("The worker is running.")

    async def worker(request: ModelRequest) -> Completion:
        if len(transport.worker_requests) == 1:
            worker_started.set()
            await asyncio.wait_for(mail_sent.wait(), timeout=8)
            return tool_call("read_file", {"path": "note.txt"}, use_id="read-note")
        worker_histories.append(repr(request.messages))
        return reply("Reported against the v2 API.")

    transport = ScenarioTransport(main, worker)
    async with tui(tmp_path, monkeypatch) as session:
        Path("note.txt").write_text("NOTE_BODY", encoding="utf-8")
        attach(session, transport)
        await (await send(session, "Start a worker on the note."))
        await until(worker_started.is_set)
        registry = registry_for_session(
            session._ctx.session_id, session._ctx.app_state.conversation
        )
        (handle,) = registry.handles()
        (worker_id,) = handle.broker.members - {"main"}

        # The worker is mid-run, between its first request and its first tool call.
        envelope = await handle.broker.send(
            sender="main", recipient=worker_id, kind="instruction", payload={"text": _MAIL}
        )
        mail_sent.set()
        await until(lambda: handle.task.done())

        assert len(worker_histories) == 1
        history = worker_histories[0]
        assert _MAIL in history and "UNTRUSTED CONTEXT" in history
        # The tool result is committed first, then the mail; the model never polled.
        assert history.index("NOTE_BODY") < history.index(_MAIL)
        assert "poll_agent_messages" not in history  # the scripted worker never polled
        # Delivered once (the envelope repeats its id as ``correlation_id``, so count
        # the text, not the id) and acknowledged: nothing is left to redeliver.
        assert history.count(_MAIL) == 1
        assert envelope.message_id in history
        assert await handle.broker.undelivered_count(worker_id) == 0

        (pool,) = list_pools(session._subagents_root())
        mail = [
            event
            for event in read_transcript(pool.transcript_path(worker_id))
            if event.kind.startswith("message_")
        ]
        assert [event.kind for event in mail] == ["message_delivered", "message_acknowledged"]
        # The transcript says who sent it and how it begins, not just an id.
        delivered = mail[0]
        assert delivered.source == "main" and delivered.text == _MAIL
        assert delivered.request_id == envelope.message_id
        # And the worker transcript view the operator reads shows exactly that.
        shown = [Text.from_markup(line).plain for line in transcript_lines(mail, 120)]
        assert f"mail from main: {_MAIL}" in shown[0]
        assert "mail acknowledged" in shown[1]


async def test_mail_delivered_at_one_boundary_is_not_added_again_at_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_started = asyncio.Event()
    mail_sent = asyncio.Event()
    worker_histories: list[str] = []

    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="Read the note twice, then report")
        return reply("The worker is running.")

    async def worker(request: ModelRequest) -> Completion:
        count = len(transport.worker_requests)
        worker_histories.append(repr(request.messages))
        if count == 1:
            worker_started.set()
            await asyncio.wait_for(mail_sent.wait(), timeout=8)
        if count <= 2:
            return tool_call("read_file", {"path": "note.txt"}, use_id=f"read-{count}")
        return reply("Reported.")

    transport = ScenarioTransport(main, worker)
    async with tui(tmp_path, monkeypatch) as session:
        Path("note.txt").write_text("NOTE_BODY", encoding="utf-8")
        attach(session, transport)
        await (await send(session, "Start a worker."))
        await until(worker_started.is_set)
        registry = registry_for_session(
            session._ctx.session_id, session._ctx.app_state.conversation
        )
        (handle,) = registry.handles()
        (worker_id,) = handle.broker.members - {"main"}
        await handle.broker.send(
            sender="main", recipient=worker_id, kind="instruction", payload={"text": _MAIL}
        )
        mail_sent.set()
        await until(lambda: handle.task.done())

        assert len(worker_histories) == 3
        assert _MAIL not in worker_histories[0]
        # Added at the first boundary, then carried in the history, never added again.
        assert worker_histories[1].count(_MAIL) == 1
        assert worker_histories[2].count(_MAIL) == 1
        assert worker_histories[2].count("AGENT MESSAGES (UNTRUSTED CONTEXT)") == 1


async def test_a_transport_retry_delivers_the_mail_again_after_the_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry rolls the worker's memory back, dropping mail added in the failed attempt."""
    from lauren_ai._exceptions import TransientTransportError

    worker_started = asyncio.Event()
    mail_sent = asyncio.Event()
    worker_histories: list[str] = []

    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="Read the note, then report")
        return reply("The worker is running.")

    async def worker(request: ModelRequest) -> Completion:
        count = len(transport.worker_requests)
        worker_histories.append(repr(request.messages))
        if count == 1:
            worker_started.set()
            await asyncio.wait_for(mail_sent.wait(), timeout=8)
        if count == 2:
            # The request that carries the mail fails: the retry restarts the run.
            raise TransientTransportError("provider hiccup", status_code=503)
        if count in (1, 3):
            return tool_call("read_file", {"path": "note.txt"}, use_id=f"read-{count}")
        return reply("Reported.")

    transport = ScenarioTransport(main, worker)
    async with tui(
        tmp_path, monkeypatch, config_text="[execution]\ntransport_retry_base_delay_s = 0.01\n"
    ) as session:
        Path("note.txt").write_text("NOTE_BODY", encoding="utf-8")
        attach(session, transport)
        await (await send(session, "Start a worker."))
        await until(worker_started.is_set)
        registry = registry_for_session(
            session._ctx.session_id, session._ctx.app_state.conversation
        )
        (handle,) = registry.handles()
        (worker_id,) = handle.broker.members - {"main"}
        await handle.broker.send(
            sender="main", recipient=worker_id, kind="instruction", payload={"text": _MAIL}
        )
        mail_sent.set()
        await until(lambda: handle.task.done())

        assert len(worker_histories) == 4
        assert worker_histories[1].count(_MAIL) == 1  # the attempt that failed carried it
        assert _MAIL not in worker_histories[2]  # the retry starts from a rolled-back memory
        assert worker_histories[3].count(_MAIL) == 1  # and is given the mail again
        assert await handle.broker.undelivered_count(worker_id) == 0


async def test_mail_polled_by_the_model_is_not_injected_a_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_started = asyncio.Event()
    mail_sent = asyncio.Event()
    worker_histories: list[str] = []

    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="Poll for instructions")
        return reply("The worker is running.")

    async def worker(request: ModelRequest) -> Completion:
        count = len(transport.worker_requests)
        if count == 1:
            worker_started.set()
            await asyncio.wait_for(mail_sent.wait(), timeout=8)
            return tool_call("poll_agent_messages", {}, use_id="poll")
        worker_histories.append(repr(request.messages))
        return reply("Done.")

    transport = ScenarioTransport(main, worker)
    async with tui(tmp_path, monkeypatch) as session:
        attach(session, transport)
        await (await send(session, "Start a worker."))
        await until(worker_started.is_set)
        registry = registry_for_session(
            session._ctx.session_id, session._ctx.app_state.conversation
        )
        (handle,) = registry.handles()
        (worker_id,) = handle.broker.members - {"main"}
        await handle.broker.send(
            sender="main", recipient=worker_id, kind="instruction", payload={"text": _MAIL}
        )
        mail_sent.set()
        await until(lambda: handle.task.done())

        # The poll tool result carries it once; the boundary adds no second copy.
        assert worker_histories[0].count(_MAIL) == 1
