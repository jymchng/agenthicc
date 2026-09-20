"""End-to-end loop journey using a deterministic fake session boundary."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, CommandDispatcher, build_builtin_registry
from agenthicc.config import AgenthiccConfig, LoopSettings
from agenthicc.runners.loop_scheduler import LoopLifecycle, LoopManager, LoopRecord, LoopStore


@pytest.mark.asyncio
async def test_user_creates_runs_and_stops_a_recurring_prompt(tmp_path) -> None:
    now = 100.0
    busy = False
    turns: list[str] = []

    def clock() -> float:
        return now

    async def dispatch(record: LoopRecord) -> None:
        turns.append(record.payload)
        await asyncio.sleep(0)

    config = AgenthiccConfig(
        loops=LoopSettings(
            default_interval_s=60,
            min_interval_s=1,
            max_interval_s=600,
            max_age_s=600,
        )
    )
    manager = LoopManager(
        session_id="e2e-session",
        settings=config.loops,
        store=LoopStore(tmp_path, "e2e-session"),
        is_idle=lambda: not busy,
        dispatch=dispatch,
        event_sink=lambda kind, payload: None,
        clock=clock,
    )
    context = CommandContext(
        text="/loop 1m inspect the fixture",
        args="1m inspect the fixture",
        model="test-model",
        console=Console(record=True),
        config=config,
        session_id="e2e-session",
        command_registry=build_builtin_registry(),
        loop_manager=manager,
    )
    assert CommandDispatcher(build_builtin_registry()).dispatch(context.text, context) is True
    assert await manager.tick_once() is True
    assert turns == ["inspect the fixture"]

    busy = True
    now += 60
    assert await manager.tick_once() is False
    assert manager.record is not None
    assert manager.record.state is LoopLifecycle.WAITING_FOR_IDLE
    busy = False
    assert await manager.tick_once() is True
    assert turns == ["inspect the fixture", "inspect the fixture"]

    assert manager.handle_command("stop") == "Loop stopped."
    now += 60
    assert await manager.tick_once() is False
    assert turns == ["inspect the fixture", "inspect the fixture"]


@pytest.mark.asyncio
async def test_user_opens_loops_table_runs_selected_job_and_deletes_it(tmp_path) -> None:
    turns: list[str] = []

    async def dispatch(record: LoopRecord) -> None:
        turns.append(record.payload)

    config = AgenthiccConfig(
        loops=LoopSettings(default_interval_s=60, min_interval_s=1, max_interval_s=600)
    )
    store = LoopStore(tmp_path, "e2e-session")
    manager = LoopManager(
        session_id="e2e-session",
        settings=config.loops,
        store=store,
        is_idle=lambda: True,
        dispatch=dispatch,
        event_sink=lambda kind, payload: None,
    )
    context = CommandContext(
        text="/loop 1m inspect the fixture",
        args="1m inspect the fixture",
        model="test-model",
        console=Console(record=True),
        config=config,
        session_id="e2e-session",
        command_registry=build_builtin_registry(),
        loop_manager=manager,
    )
    dispatcher = CommandDispatcher(build_builtin_registry())
    assert dispatcher.dispatch(context.text, context)
    assert manager.record is not None

    pending: list[object] = []
    closed: list[bool] = []
    context.set_pending_menu = pending.append
    context.close_overlay = lambda: closed.append(True)
    assert dispatcher.dispatch("/loops", context)

    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.overlays.loops import LoopJobsOverlay

    overlay = pending[-1]
    assert isinstance(overlay, LoopJobsOverlay)
    overlay.handle_key(Key.ENTER, "")
    assert closed == [True]
    assert await manager.tick_once()
    assert turns == ["inspect the fixture"]

    # Reopen the table and use the explicit d/Enter confirmation path.
    pending.clear()
    closed.clear()
    assert dispatcher.dispatch("/loops", context)
    overlay = pending[-1]
    assert isinstance(overlay, LoopJobsOverlay)
    overlay.handle_key(Key.CHAR, "d")
    overlay.handle_key(Key.ENTER, "")
    assert store.load() is None
