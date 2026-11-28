"""COMM-04: a worker of one pool messages a worker of a pool running beside it.

Two real pools in one real session. Before, a worker's mail could only go to its
own pool, so the two workers could only coordinate by the parent relaying.
"""

from __future__ import annotations

import asyncio
import re
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

_MAIL = "Heads up: the schema moved to v2, rebase your tests on it."


async def test_a_worker_messages_a_worker_of_a_sibling_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha_started = asyncio.Event()
    mail_sent = asyncio.Event()
    alpha_histories: list[str] = []
    beta_tools: list[str] = []
    counts = {"ALPHA": 0, "BETA": 0}

    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="ALPHA waits for mail")
        if index == 2:
            return spawn_call(kind="researcher", task="BETA writes to alpha")
        return reply("Both workers are running.")

    async def worker(request: ModelRequest) -> Completion:
        who = "ALPHA" if "ALPHA" in str(request.messages[0]["content"]) else "BETA"
        counts[who] += 1
        step = counts[who]
        history = repr(request.messages)
        if who == "ALPHA":
            if step == 1:
                alpha_started.set()
                await asyncio.wait_for(mail_sent.wait(), timeout=8)
                return tool_call("read_file", {"path": "note.txt"}, use_id="read-note")
            alpha_histories.append(history)
            return reply("Alpha is done.")
        if step == 1:
            await asyncio.wait_for(alpha_started.wait(), timeout=8)
            return tool_call("list_reachable_workers", {}, use_id="who")
        if step == 2:
            beta_tools.append(history)
            found = re.search(r'"address": "([^"]+)"', history)
            assert found is not None, history
            return tool_call(
                "send_peer_message",
                {"recipient": found.group(1), "message": _MAIL, "requires_response": True},
                use_id="tell-alpha",
            )
        mail_sent.set()
        return reply("Beta told alpha.")

    transport = ScenarioTransport(main, worker)
    async with tui(tmp_path, monkeypatch) as session:
        Path("note.txt").write_text("NOTE_BODY", encoding="utf-8")
        attach(session, transport)
        await (await send(session, "Start two workers and let them coordinate."))
        registry = registry_for_session(
            session._ctx.session_id, session._ctx.app_state.conversation
        )
        await until(lambda: len(registry.handles()) == 2)
        alpha_pool, beta_pool = registry.handles()
        await until(lambda: alpha_pool.task.done() and beta_pool.task.done())

        # Beta found alpha through the directory, with the address to use.
        (alpha_worker,) = alpha_pool.broker.members - {"main"}
        address = f"{alpha_pool.pool_id}/{alpha_worker}"
        assert f'"address": "{address}"' in beta_tools[0]
        assert '"relation": "sibling_pool"' in beta_tools[0]

        # Alpha read it between its tool calls, framed as untrusted mail from beta.
        assert len(alpha_histories) == 1
        history = alpha_histories[0]
        assert _MAIL in history and "UNTRUSTED CONTEXT" in history
        assert history.index("NOTE_BODY") < history.index(_MAIL)
        (beta_worker,) = beta_pool.broker.members - {"main"}
        beta_address = f"{beta_pool.pool_id}/{beta_worker}"
        assert beta_address in history
        assert history.count(_MAIL) == 1
        assert await alpha_pool.broker.undelivered_count(alpha_worker) == 0

        # What the operator reads in alpha's transcript: who wrote, and how it began.
        pools = {pool.pool_id: pool for pool in list_pools(session._subagents_root())}
        events = read_transcript(pools[alpha_pool.pool_id].transcript_path(alpha_worker))
        delivered = [event for event in events if event.kind == "message_delivered"]
        assert [(event.source, event.text) for event in delivered] == [(beta_address, _MAIL)]
        shown = Text.from_markup(transcript_lines(delivered, 160)[0]).plain
        assert f"mail from {beta_address}: {_MAIL}" in shown


async def test_a_lone_pool_has_nobody_to_reach_and_an_address_that_is_not_there_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is refused, not guessed. (A pool that exists but is unrelated is covered
    with the relation rule in ``tests/unit/test_cross_pool_mail.py``.)"""
    from agenthicc.subagents.communication import CommunicationError

    beta_started = asyncio.Event()
    release = asyncio.Event()

    async def main(request: ModelRequest, index: int) -> Completion:
        if index == 1:
            return spawn_call(kind="researcher", task="ALPHA idles")
        return reply("Running.")

    async def worker(request: ModelRequest) -> Completion:
        beta_started.set()
        await asyncio.wait_for(release.wait(), timeout=8)
        return reply("Done.")

    transport = ScenarioTransport(main, worker)
    async with tui(tmp_path, monkeypatch) as session:
        attach(session, transport)
        await (await send(session, "Start a worker."))
        registry = registry_for_session(
            session._ctx.session_id, session._ctx.app_state.conversation
        )
        await until(lambda: len(registry.handles()) == 1 and beta_started.is_set())
        (handle,) = registry.handles()
        (worker_id,) = handle.broker.members - {"main"}
        directory = handle.broker.directory
        assert directory is not None
        try:
            with pytest.raises(CommunicationError, match="^unknown_pool"):
                await directory.send(
                    sender_pool=handle.pool_id,
                    sender_worker=worker_id,
                    recipient="pool-not-here/task-0",
                    message="anyone?",
                )
            assert [w["address"] for w in directory.reachable(handle.pool_id, worker_id)] == []
        finally:
            release.set()
            await until(lambda: handle.task.done())
