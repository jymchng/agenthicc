"""A background handoff never cancels live Explorers in an ordinary session.

The reported failure: ``/bg`` printed ``Successfully backgrounded`` and every
Explorer row then read ``parent_cancelled``. Cancelling the foreground turn
makes ``SubagentPool.run`` abort its workers, and only workflow-phase pools
were guarded. These cases pin the invariant for a direct (workflow-free)
session: a hosted runtime hands over with the workers still running, and a
terminal-owned runtime refuses before it cancels anything.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from lauren_ai._transport import Completion

from agenthicc.background.store import BackgroundStore
from agenthicc.runners.tui_session import TUISession
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
        self.wakes: list[int] = []

    def release(self, *indexes: int) -> None:
        for index in indexes:
            self.gates[index].set()

    async def worker(self, request: ModelRequest) -> Completion:
        match = re.search(r"inspect (\d)", str(request.messages[0]["content"]))
        assert match is not None
        self.entered += 1
        await self.gates[int(match[1])].wait()
        return reply("explorer evidence")

    async def main(self, request: ModelRequest, sequence: int) -> Completion:
        self.parent_requests.append(request)
        if sequence == 1:
            return spawn_call(EXPLORERS, kind="explorer")
        if request.contains("Background subagent pool") and not self.wakes:
            self.wakes.append(sequence)
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


async def _assert_parent_notified_once(
    session: TUISession, script: _Explorers, source: JournalNoticeSource
) -> None:
    """The parent is woken once, after the last worker, and the notice is in its transcript."""
    await until(lambda: len(script.parent_requests) >= 3)
    await asyncio.sleep(0.2)
    task = session._agent_task
    if task is not None:
        async with asyncio.timeout(8):
            await asyncio.gather(task, return_exceptions=True)
    (pool,) = (handle.pool_id for handle in source.registry.handles())
    assert len(script.parent_requests) == 3, "one wake for the pool, not one per Explorer"
    assert script.wakes == [3]
    assert script.parent_requests[2].contains(f"Background subagent pool {pool}")
    assert not source.has_pending(), "the notice is acknowledged once it reaches the parent"
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
            if event.kind == "user_message" and event.payload.get("source") == "subagent_completion"
        ]

    await until(lambda: len(replayed()) == 1)
    assert len(replayed()) == 1, "a reattached terminal replays the notice the parent received"


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

        script.release(*range(EXPLORERS))
        await _assert_all_finished_ok(source)
        await _assert_parent_notified_once(session, script, source)


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

        # Four of five finish: the parent stays parked, because the pool is not done.
        for index in range(EXPLORERS - 1):
            script.release(index)
        await until(lambda: len(source.journal.fold_subagent_worker_results()) == EXPLORERS - 1)
        await asyncio.sleep(0.2)
        assert len(script.parent_requests) == 2 and not source.has_pending()

        script.release(EXPLORERS - 1)
        await _assert_all_finished_ok(source)
        await _assert_parent_notified_once(session, script, source)
