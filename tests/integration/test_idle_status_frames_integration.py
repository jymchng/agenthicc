"""Integration coverage for PRD-164 store-to-workspace redraw boundaries."""

from __future__ import annotations

import asyncio
from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace import Workspace

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_idle_ticks_do_not_reach_live_update() -> None:
    state = AppState.create()
    console = Console(file=StringIO(), force_terminal=True, width=120)
    workspace = Workspace(state, console)
    workspace.start()
    live = workspace._live
    assert live is not None
    live.update = MagicMock(wraps=live.update)  # type: ignore[method-assign]

    try:
        for _ in range(7):
            state.conversation.tick()
            await asyncio.sleep(0)

        live.update.assert_not_called()
        assert state.conversation.frame() == 0
    finally:
        workspace.stop()


@pytest.mark.asyncio
async def test_active_animation_ticks_reach_live_update() -> None:
    state = AppState.create()
    console = Console(file=StringIO(), force_terminal=True, width=120)
    workspace = Workspace(state, console)
    workspace.start()
    live = workspace._live
    assert live is not None
    live.update = MagicMock(wraps=live.update)  # type: ignore[method-assign]

    try:
        state.conversation.begin_turn("agent", "turn-1")
        await asyncio.sleep(0)
        live.update.reset_mock()

        state.conversation.tick()
        await asyncio.sleep(0)

        assert state.conversation.frame() == 1
        live.update.assert_called_once()
    finally:
        workspace.stop()
