"""A background handoff never cancels live Explorers, and each one notifies the parent.

The reported failure: ``/bg`` printed ``Successfully backgrounded`` and every
Explorer row then read ``parent_cancelled``. Cancelling the foreground turn
makes ``SubagentPool.run`` abort its workers, and only workflow-phase pools
were guarded. These cases pin the invariant for a direct (workflow-free)
session: a hosted runtime hands over with the workers still running, a
terminal-owned runtime refuses before it cancels anything, and every Explorer
that finishes wakes the parent with a notice of its own.
"""

from __future__ import annotations

import asyncio
import re
from functools import partial
from pathlib import Path

import pytest
from lauren_ai._transport import Completion

from agenthicc.background.store import BackgroundStore
from agenthicc.runners.tui_session import TUISession
from agenthicc.subagents.communication import make_parent_communication_tools
from agenthicc.subagents.delivery import JournalNoticeSource
from agenthicc.tui.runtime.session_log import load_recent_transcript_page
from tests.e2e.self_extension.harness import ModelRequest, reply
from tests.subagent_sessions import (
    ScenarioTransport,
    attach,
    detach_controller,
    send,
    spawn_call,
    tui,
    until,
)

pytestmark = pytest.mark.integration

EXPLORERS = 5


class _Explorers:
    """Five Explorers, each gated on its own release, and a parent that yields."""

    def __init__(self) -> None:
        self.gates = [asyncio.Event() for _ in range(EXPLORERS)]
        self.entered = 0
        self.parent_requests: list[ModelRequest] = []

    def release(self, *indexes: int) -> None:
        for index in indexes:
            self.gates[index].set()

    async def worker(self, request: ModelRequest) -> Completion:
        match = re.search(r"inspect (\d)", str(request.messages[0]["content"]))
        assert match is not None
        self.entered += 1
        await self.gates[int(match[1])].wait()
        return reply(f"explorer evidence {match[1]}")

    async def main(self, request: ModelRequest, sequence: int) -> Completion:
        self.parent_requests.append(request)
        if sequence == 1:
            return spawn_call(EXPLORERS, kind="explorer")
        return reply("The explorers are reading in the background.")


def _source(session: TUISession) -> JournalNoticeSource:
    source = session._install_subagent_notices()
    assert source is not None
    return source


_Identity = list[tuple[str, float | None]]


def _identities(source: JournalNoticeSource) -> _Identity:
    (handle,) = source.registry.handles()
    assert handle.pool is not None
    return [(worker.worker_id, worker.started_at) for worker in handle.pool.state.workers]


async def _start_explorers(session: TUISession) -> tuple[_Explorers, JournalNoticeSource]:
    script = _Explorers()
    attach(session, ScenarioTransport(script.main, script.worker))
    task = await send(session, "Explore the repository with five explorers.")
    source = _source(session)
    await until(lambda: script.entered == EXPLORERS)
    async with asyncio.timeout(8):
        await task
    (handle,) = source.registry.handles()
    assert handle.pool is not None
    assert [worker.live for worker in handle.pool.state.workers] == [True] * EXPLORERS
    return script, source


def _assert_workers_untouched(source: JournalNoticeSource, before: _Identity) -> None:
    """The same five attempts are still running, none stopped or settled."""
    (handle,) = source.registry.handles()
    assert handle.pool is not None and not handle.task.done()
    assert all(worker.status == "running" for worker in handle.pool.state.workers)
    assert _identities(source) == before
    assert not source.journal.fold_subagent_worker_results()


async def _assert_all_finished_ok(source: JournalNoticeSource) -> None:
    (handle,) = source.registry.handles()
    async with asyncio.timeout(8):
        await asyncio.shield(handle.task)
    results = source.journal.fold_subagent_worker_results()
    assert len(results) == EXPLORERS and all(row["ok"] is True for row in results)
    assert "parent_cancelled" not in repr(source.journal.fold_subagent_pool_lifecycle())
    assert "parent_cancelled" not in repr(results)


def _has_requests(script: _Explorers, count: int) -> bool:
    return len(script.parent_requests) == count


async def _assert_partial_collect_reads_only_the_finished(
    session: TUISession, source: JournalNoticeSource, pool: str, finished: int
) -> None:
    """A notice can be acted on at once: the finished Explorers' reports are readable."""
    tools = make_parent_communication_tools(
        source.registry, session._ctx.session_id, source.journal
    )
    collect = next(tool for tool in tools if tool.__name__ == "collect_subagent_results")
    snapshot = await collect(pool)
    assert snapshot["status"] == "running" and snapshot["pool_id"] == pool
    report = snapshot["finished"]
    assert isinstance(report, dict)
    assert (report["count"], report["succeeded"], report["failed"]) == (finished, finished, 0)
    text = str(report["results"])
    for index in range(EXPLORERS):
        assert (f"explorer evidence {index}" in text) == (index < finished)


async def _finish_explorers_one_by_one(
    session: TUISession, script: _Explorers, source: JournalNoticeSource
) -> None:
    """Each Explorer that finishes wakes the parent with a notice of its own.

    The notice names the worker and its progress and never carries what the
    worker wrote: that is read with ``collect_subagent_results``.
    """
    (pool,) = (handle.pool_id for handle in source.registry.handles())
    for finished in range(1, EXPLORERS + 1):
        script.release(finished - 1)
        expected = 2 + finished
        await until(partial(_has_requests, script, expected))
        task = session._agent_task
        if task is not None:
            async with asyncio.timeout(8):
                await asyncio.gather(task, return_exceptions=True)
        notice = str(script.parent_requests[1 + finished].messages[-1]["content"])
        assert notice.startswith(f"Background subagent explorer #{finished} of pool {pool} ")
        assert f"({finished}/{EXPLORERS} workers of this pool have finished" in notice
        assert ("the pool is complete" in notice) == (finished == EXPLORERS)
        assert "explorer evidence" not in repr(script.parent_requests[1 + finished].messages)
        if finished < EXPLORERS:
            await _assert_partial_collect_reads_only_the_finished(session, source, pool, finished)
    await asyncio.sleep(0.2)
    assert len(script.parent_requests) == 2 + EXPLORERS, "one wake per Explorer, no extra"
    assert not source.has_pending(), "every notice is acknowledged once the parent has it"
    assert len(source.journal.delivered_subagent_worker_notifications()) == EXPLORERS

    behaviour = session._ctx.cfg.behaviour

    def replayed() -> list[object]:
        page = load_recent_transcript_page(
            session._ctx.session_id,
            last_turns=behaviour.resume_transcript_turns,
            max_events=behaviour.resume_transcript_max_events,
            max_bytes=behaviour.resume_transcript_max_bytes,
            max_event_bytes=behaviour.resume_transcript_max_event_bytes,
        )
        return [
            event
            for event in page.events
            if event.kind == "user_message" and event.payload.get("source") == "subagent_worker"
        ]

    await until(lambda: len(replayed()) == EXPLORERS)
    assert len(replayed()) == EXPLORERS, "a reattached terminal replays every notice"


async def test_terminal_owned_session_refuses_handoff_before_cancelling_explorers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with tui(tmp_path, monkeypatch) as session:
        script, source = await _start_explorers(session)
        before = _identities(source)

        assert not session._request_background_handoff()

        notice = session._ctx.app_state.conversation.notification()
        assert notice is not None and "runtime_not_hosted" in notice
        assert "live subagent pools" in notice
        _assert_workers_untouched(source, before)
        assert BackgroundStore(tmp_path / "home" / ".agenthicc" / "background").list() == []
        assert session._background_handoff_state == "idle"

        await _finish_explorers_one_by_one(session, script, source)
        await _assert_all_finished_ok(source)


async def test_hosted_session_detaches_with_five_explorers_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        script, source = await _start_explorers(session)
        before = _identities(source)
        pool = next(iter(source.registry.handles())).pool_id

        await detach_controller(session, tmp_path)

        _assert_workers_untouched(source, before)
        (handle,) = source.registry.handles()
        assert handle.pool_id == pool
        assert session._agent_task is None or session._agent_task.done()

        await _finish_explorers_one_by_one(session, script, source)
        await _assert_all_finished_ok(source)
