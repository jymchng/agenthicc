"""End-to-end coverage for PRD-164 idle Live-frame suppression."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.workspace import Workspace

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_idle_ticks_do_not_append_duplicate_status_frames() -> None:
    """Captured terminals must not receive one unchanged panel per idle tick."""
    state = AppState.create()
    state.conversation.model_name.set("openai/deepseek-v4-flash")
    console = Console(record=True, force_terminal=True, width=120)
    workspace = Workspace(state, console)
    workspace.start()

    try:
        await asyncio.sleep(0)
        for _ in range(7):
            state.conversation.tick()
            await asyncio.sleep(0)

        assert state.conversation.frame() == 0
    finally:
        workspace.stop()

    assert console.export_text().count("✿ Idle") == 1


@pytest.mark.asyncio
async def test_replayed_transcript_idle_boundary_does_not_start_idle_redraw_loop() -> None:
    """Resume-style replay preserves one notification and stops idle repainting."""

    async def run_replay(idle_ticks: int) -> str:
        # Build a fresh state for each capture so the baseline and regression
        # paths cannot influence one another through reactive signals.
        state = AppState.create()
        state.conversation.model_name.set("openai/deepseek-v4-flash")
        state.conversation.notification.set(
            "Session had an in-progress 'code_plan' workflow. Send a message …"
        )
        console = Console(record=True, force_terminal=True, width=120)
        workspace = Workspace(state, console)
        workspace.start()
        try:
            await workspace.replay_transcript(
                [
                    ConversationEvent(
                        event_id="resume-user-1",
                        kind="user_message",
                        payload={"text": "continue"},
                    )
                ]
            )
            for _ in range(idle_ticks):
                state.conversation.tick()
                await asyncio.sleep(0)
        finally:
            workspace.stop()
        return console.export_text()

    baseline = await run_replay(0)
    with_idle_ticks = await run_replay(7)

    assert "Session had an in-progress 'code_plan' workflow." in baseline
    assert with_idle_ticks.count("✿ Idle") == baseline.count("✿ Idle")
    assert with_idle_ticks.count("Loading transcript") == baseline.count("Loading transcript")
