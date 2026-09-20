"""Integration coverage for loop persistence and command wiring."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, CommandDispatcher, build_builtin_registry
from agenthicc.config import AgenthiccConfig, LoopSettings
from agenthicc.runners.loop_scheduler import (
    LoopLifecycle,
    LoopManager,
    LoopPayloadKind,
    LoopRecord,
    LoopStore,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


def _context(manager: LoopManager, config: AgenthiccConfig) -> CommandContext:
    return CommandContext(
        text="",
        args="",
        model="test-model",
        console=Console(record=True),
        config=config,
        session_id="integration-session",
        command_registry=build_builtin_registry(),
        loop_manager=manager,
    )


@pytest.mark.asyncio
async def test_command_dispatch_creates_and_rehydrates_the_same_session_loop(tmp_path) -> None:
    clock = _Clock()
    config = AgenthiccConfig(
        loops=LoopSettings(
            default_interval_s=60,
            min_interval_s=1,
            max_interval_s=300,
            max_age_s=600,
        )
    )
    store = LoopStore(tmp_path, "integration-session")
    dispatched: list[tuple[str, str]] = []

    async def dispatch(record: LoopRecord) -> None:
        dispatched.append((record.conversation_id, record.payload))

    first = LoopManager(
        session_id="integration-session",
        settings=config.loops,
        store=store,
        is_idle=lambda: True,
        dispatch=dispatch,
        event_sink=lambda kind, payload: None,
        clock=clock,
    )
    dispatcher = CommandDispatcher(build_builtin_registry())
    assert dispatcher.dispatch("/loop 1m inspect fixture", _context(first, config)) is True
    assert first.record is not None
    assert first.record.state is LoopLifecycle.SCHEDULED
    assert store.load() == first.record

    second = LoopManager(
        session_id="integration-session",
        settings=config.loops,
        store=store,
        is_idle=lambda: True,
        dispatch=dispatch,
        event_sink=lambda kind, payload: None,
        clock=clock,
    )
    await second.start(rehydrate=True)
    try:
        assert second.record is not None
        assert second.record.loop_id == first.record.loop_id
        assert second.record.next_due_at <= clock.now
        await second.tick_once()
        assert dispatched == [("integration-session", "inspect fixture")]
    finally:
        await second.shutdown()


def test_loops_command_opens_job_table_and_enter_requests_run(tmp_path) -> None:
    config = AgenthiccConfig(
        loops=LoopSettings(default_interval_s=60, min_interval_s=1, max_interval_s=300)
    )
    store = LoopStore(tmp_path, "integration-session")
    record = LoopRecord(
        loop_id="integration-loop",
        session_id="integration-session",
        conversation_id="integration-session",
        payload_kind=LoopPayloadKind.PROMPT,
        payload="inspect fixture",
        interval_s=60,
        created_at=1.0,
        updated_at=1.0,
        next_due_at=100.0,
        expires_at=1_000.0,
    )
    store.save(record)
    manager = LoopManager(
        session_id="integration-session",
        settings=config.loops,
        store=store,
        is_idle=lambda: True,
        dispatch=lambda current: asyncio.sleep(0),
        event_sink=lambda kind, payload: None,
    )
    pending: list[object] = []
    closed: list[bool] = []
    context = _context(manager, config)
    context.set_pending_menu = pending.append
    context.close_overlay = lambda: closed.append(True)

    assert CommandDispatcher(build_builtin_registry()).dispatch("/loops", context)
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.overlays.loops import LoopJobsOverlay

    assert isinstance(pending[-1], LoopJobsOverlay)
    pending[-1].handle_key(Key.ENTER, "")
    assert manager.record is not None
    assert manager.record.next_due_at <= manager.clock()
    assert closed == [True]
