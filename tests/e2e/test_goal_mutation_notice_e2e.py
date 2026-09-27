"""End-to-end presentation journey for dynamic goal mutations."""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_goal_plan_update_is_readable_and_not_a_restart() -> None:
    state = AppState.create()
    output = StringIO()
    appender = ScrollBufferAppender(
        state,
        Console(file=output, force_terminal=False, color_system=None),
    )
    appender.mount()
    try:
        state.conversation.begin_turn("assistant", turn_id="goal-plan-journey")
        state.conversation.append_event(
            "goal_list_mutated",
            {"operation": "insert", "index": 7, "goal_count": 13, "goal_id": "not-displayable"},
            event_id="goal-plan-insert",
        )
        state.conversation.append_event("text", {"text": "The active goal remains in progress."})
        await asyncio.sleep(0)
    finally:
        appender.unmount()

    rendered = output.getvalue()
    assert "Plan updated" in rendered
    assert "Inserted a goal · position 8 of 13" in rendered
    assert "↳ Current goal continues" in rendered
    assert "at 7 (13 total)" not in rendered
    assert "; continuing the current goal" not in rendered
    assert "not-displayable" not in rendered
    assert "restarting" not in rendered.casefold()
    assert rendered.index("Plan updated") < rendered.index("The active goal remains in progress.")
