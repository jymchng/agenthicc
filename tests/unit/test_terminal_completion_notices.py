"""COMM-02: a background terminal that ends is announced to the agent, once.

Real subprocesses and a real journal.  A terminal's outcome reaches the
conversation as a runtime notice through the same source, delivery and
acknowledgement the subagent notices use.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from agenthicc.background.terminals import (
    TerminalManager,
    TerminalRecord,
    TerminalState,
    TerminalStore,
)
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.tui_session import TUISession
from agenthicc.subagents.communication import PoolContinuationRegistry
from agenthicc.subagents.delivery import (
    MAX_TERMINAL_NOTICES,
    RUNTIME_NOTICE_SOURCES,
    JournalNoticeSource,
    PendingNotice,
    deliver_notices,
)
from agenthicc.subagents.phase import PhaseOwner
from agenthicc.tools.exec import InspectTerminalTool, StopTerminalTool, WaitTerminalTool
from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.unit


def _manager(tmp_path: Path, **kwargs: object) -> TerminalManager:
    return TerminalManager(
        session_id="comm-02", cwd=tmp_path, store_root=tmp_path / "registry", **kwargs
    )


def _conversation(tmp_path: Path) -> SessionConversation:
    return SessionConversation.open(
        "comm-02", max_tokens=20_000, journal_path=tmp_path / "journal.jsonl"
    )


def _source(
    conversation: SessionConversation, manager: TerminalManager, **kwargs: object
) -> JournalNoticeSource:
    return JournalNoticeSource(
        conversation.journal,
        PoolContinuationRegistry(),
        terminals=manager,
        **kwargs,  # type: ignore[arg-type]
    )


async def _finished(manager: TerminalManager, terminal_id: str) -> TerminalRecord:
    for _ in range(1000):
        record = manager.get(terminal_id)
        if record is not None and not record.active:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"{terminal_id} never finished")


async def _run(
    manager: TerminalManager, code: str, *, label: str = "job", timeout: float = 0.0
) -> TerminalRecord:
    """Start a python snippet as a background terminal and let it finish unobserved."""
    started = await manager.start(argv=[sys.executable, "-c", code], label=label, timeout=timeout)
    assert started["ok"] is True, started
    return await _finished(manager, str(started["terminal_id"]))


def _only(notices: tuple[PendingNotice, ...]) -> PendingNotice:
    assert len(notices) == 1, notices
    return notices[0]


async def test_a_finished_terminal_is_announced_once_with_a_bounded_untrusted_tail(
    tmp_path: Path,
) -> None:
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    try:
        source = _source(conversation, manager)
        record = await _run(
            manager, "print('A' * 5000 + 'END'); import sys; print('warn', file=sys.stderr)"
        )

        notice = _only(source.claim())

        assert notice.kind == "terminal" and notice.source == "terminal_notice"
        assert notice.source in RUNTIME_NOTICE_SOURCES
        assert notice.message_id == f"terminal:{record.terminal_id}:exited"
        assert record.terminal_id in notice.text and "exited successfully" in notice.text
        assert "untrusted process data" in notice.text
        assert f'inspect_terminal(terminal_id="{record.terminal_id}")' in notice.text
        # Only the tail of what it printed is included, and it is the tail.
        assert "END" in notice.text and notice.text.count("A") < 1300
        assert "warn" in notice.text

        deliver_notices((notice,), cast("object", conversation.memory), None)  # type: ignore[arg-type]
        assert conversation.journal.has_message_id(notice.message_id)
        assert source.claim() == ()  # delivered once, never again
    finally:
        conversation.close()
        await manager.close()


async def test_the_journal_not_memory_keeps_a_delivered_notice_from_repeating(
    tmp_path: Path,
) -> None:
    """The manager's own memory of what it announced is lost on a restart; the journal is not."""
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    try:
        await _run(manager, "print('done')")
        notice = _only(_source(conversation, manager).claim())
        deliver_notices((notice,), cast("object", conversation.memory), None)  # type: ignore[arg-type]
    finally:
        conversation.close()
    manager._observed.clear()  # what a fresh process would not remember
    reopened = _conversation(tmp_path)  # the same journal file, read back
    try:
        assert reopened.journal.has_message_id(notice.message_id)
        assert _source(reopened, manager).claim() == ()
    finally:
        reopened.close()
        await manager.close()


async def test_outcomes_are_named_and_failures_are_not_reported_as_success(
    tmp_path: Path,
) -> None:
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    try:
        failed = await _run(manager, "raise SystemExit(3)", label="build")
        slow = await _run(manager, "import time; time.sleep(60)", label="slow", timeout=0.5)
        by_id = {notice.message_id: notice for notice in _source(conversation, manager).claim()}

        failed_text = by_id[f"terminal:{failed.terminal_id}:failed"].text
        slow_text = by_id[f"terminal:{slow.terminal_id}:timed_out"].text
        assert "failed with exit code 3" in failed_text and "(build)" in failed_text
        assert "time limit expired" in slow_text
    finally:
        conversation.close()
        await manager.close()


@pytest.mark.parametrize("how", ["wait", "inspect", "stop"])
async def test_a_terminal_the_agent_already_read_is_not_announced(tmp_path: Path, how: str) -> None:
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    context = {"workspace_root": str(tmp_path), "terminal_manager": manager}
    try:
        if how == "stop":
            started = await manager.start(
                argv=[sys.executable, "-c", "import time; time.sleep(60)"]
            )
            terminal_id = str(started["terminal_id"])
            await StopTerminalTool().execute({"terminal_id": terminal_id}, context)
        else:
            terminal_id = (await _run(manager, "print('x')")).terminal_id
            tool = WaitTerminalTool() if how == "wait" else InspectTerminalTool()
            await tool.execute({"terminal_id": terminal_id}, context)

        assert (await _finished(manager, terminal_id)).state is not TerminalState.RUNNING
        assert _source(conversation, manager).claim() == ()
    finally:
        conversation.close()
        await manager.close()


async def test_a_terminal_that_ends_while_the_agent_waits_on_it_is_not_announced(
    tmp_path: Path,
) -> None:
    """The wait is about to hand the agent the result; a notice would tell it twice."""
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    flag = tmp_path / "go"
    script = (
        "import pathlib, sys, time\nwhile not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.02)"
    )
    context = {"workspace_root": str(tmp_path), "terminal_manager": manager}
    try:
        started = await manager.start(argv=[sys.executable, "-c", script, str(flag)])
        terminal_id = str(started["terminal_id"])
        waiting = asyncio.create_task(
            WaitTerminalTool().execute({"terminal_id": terminal_id}, context)
        )
        for _ in range(500):
            if manager.current_wait_id() == terminal_id:
                break
            await asyncio.sleep(0.01)
        assert manager.current_wait_id() == terminal_id

        flag.touch()
        result = await asyncio.wait_for(waiting, timeout=10)

        assert result["state"] == TerminalState.EXITED.value
        assert _source(conversation, manager).claim() == ()
    finally:
        conversation.close()
        await manager.close()


async def test_a_refused_or_unspawnable_start_is_not_announced(tmp_path: Path) -> None:
    """The tool call that asked for it was handed that outcome directly."""
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    try:
        result = await manager.start(argv=["/nonexistent/binary-xyz"], label="bad")
        assert result["state"] == TerminalState.SPAWN_FAILED.value
        assert _source(conversation, manager).claim() == ()
    finally:
        conversation.close()
        await manager.close()


async def test_a_terminal_from_an_earlier_run_is_history_but_an_orphan_is_news(
    tmp_path: Path,
) -> None:
    store = TerminalStore(tmp_path / "registry")
    old = TerminalRecord(
        terminal_id="term-old",
        session_id="comm-02",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        kind="exec",
        command="old",
        label="old job",
        state=TerminalState.EXITED,
        created_at=time.time() - 100,
        finished_at=time.time() - 50,
        returncode=0,
    )
    lost = TerminalRecord(
        terminal_id="term-lost",
        session_id="comm-02",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        kind="exec",
        command="lost",
        label="lost job",
        state=TerminalState.RUNNING,
        created_at=time.time() - 100,
        started_at=time.time() - 100,
    )
    store.upsert(old)
    store.upsert(lost)
    await asyncio.sleep(0.01)

    manager, conversation = _manager(tmp_path), _conversation(tmp_path)  # the restart
    try:
        notice = _only(_source(conversation, manager).claim())
        assert notice.message_id == "terminal:term-lost:orphaned"
        assert "orphaned" in notice.text and "may still be running" in notice.text
    finally:
        conversation.close()
        await manager.close()


async def test_a_burst_of_terminals_is_announced_in_bounded_batches(tmp_path: Path) -> None:
    total = MAX_TERMINAL_NOTICES + 3
    manager, conversation = (
        _manager(tmp_path, max_terminals=total, max_terminals_per_project=total),
        _conversation(tmp_path),
    )
    try:
        for index in range(total):
            await _run(manager, "pass", label=f"job {index}")
        source = _source(conversation, manager)

        first = source.claim()
        assert len(first) == MAX_TERMINAL_NOTICES
        deliver_notices(first, cast("object", conversation.memory), None)  # type: ignore[arg-type]
        second = source.claim()
        assert len(second) == 3
        assert not {n.message_id for n in first} & {n.message_id for n in second}
    finally:
        conversation.close()
        await manager.close()


async def test_a_workflow_phase_source_does_not_carry_session_terminals(tmp_path: Path) -> None:
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    try:
        await _run(manager, "pass")
        owner = PhaseOwner("comm-02", "run", "frame", "build", "invocation")
        assert _source(conversation, manager, phase_owner=owner).claim() == ()
        assert len(_source(conversation, manager).claim()) == 1
    finally:
        conversation.close()
        await manager.close()


async def test_a_headless_turn_boundary_delivers_a_finished_terminal(tmp_path: Path) -> None:
    from agenthicc.subagents.delivery import deliver_session_background_notices

    _session, ctx, _workspace, _input = _make_session()
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    ctx.session_conversation = conversation
    ctx.terminal_manager = manager
    try:
        record = await _run(manager, "print('built')", label="build")

        delivered = deliver_session_background_notices(ctx)

        assert delivered == ()  # the return value lists worker notices only
        message_id = f"terminal:{record.terminal_id}:exited"
        assert conversation.journal.has_message_id(message_id)
        # Once delivered it is not announced again at the next boundary.
        deliver_session_background_notices(ctx)
        assert _source(conversation, manager).claim() == ()
    finally:
        conversation.close()
        await manager.close()


async def test_the_session_wakes_once_when_a_terminal_ends_and_delivers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, ctx, _workspace, _input = _make_session()
    assert isinstance(session, TUISession)
    manager = _manager(tmp_path)
    conversation = _conversation(tmp_path)
    ctx.session_conversation = conversation
    ctx.terminal_manager = manager
    hints: list[bool] = []
    monkeypatch.setattr(session, "_hint_subagent_activity", lambda: hints.append(True))
    try:
        started = await manager.start(argv=[sys.executable, "-c", "print('built')"], label="build")
        record = await _finished(manager, str(started["terminal_id"]))

        # The status stream fires many times for one terminal; the session wakes once.
        for _ in range(3):
            session._sync_terminal_status()
        assert hints == [True]

        notices = session._pending_subagent_notices()
        assert [notice.kind for notice in notices] == ["terminal"]
        assert session._deliver_completed_background_subagents() == 1
        assert conversation.journal.has_message_id(f"terminal:{record.terminal_id}:exited")
        assert session._pending_subagent_notices() == ()
    finally:
        conversation.close()
        await manager.close()
