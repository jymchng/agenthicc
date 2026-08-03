"""Integration coverage for PRD-165 approval-wait redraw suppression."""

from __future__ import annotations

import asyncio
from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace import Workspace

pytestmark = pytest.mark.integration


def _request(kind: str) -> ApprovalRequest:
    tool_input: dict[str, object] = {}
    if kind == "plan_review":
        tool_input = {"plan": "# Plan\n\nReview this plan."}
    elif kind == "questions":
        tool_input = {
            "questions": [{"id": "scope", "text": "What should be included?", "options": ["Core"]}]
        }
    return ApprovalRequest(
        tool_name=kind,
        tool_use_id=f"{kind}-1",
        tool_input=tool_input,
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind=kind,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool", "plan_review", "questions"])
async def test_prompt_ticks_do_not_repaint_live_surface(kind: str) -> None:
    """A real Workspace must stay quiet during a stable prompt wait."""
    app_state = AppState.create()
    app_state.conversation.begin_turn("agent", f"turn-{kind}")
    service = ApprovalService(app_state)
    console = Console(file=StringIO(), force_terminal=True, width=120)
    workspace = Workspace(app_state, console)
    workspace.start()
    request_task = asyncio.create_task(service.request_approval(_request(kind)))

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert app_state.pending_approval() is not None
        live = workspace._live
        assert live is not None
        live.update = MagicMock(wraps=live.update)  # type: ignore[method-assign]
        live.update.reset_mock()

        # A compatibility writer may still publish a stale timer value; the
        # workspace guard must keep that from repainting the waiting surface.
        app_state.conversation.activity_elapsed_s.set(1.0)
        await asyncio.sleep(0)
        assert live.update.call_count == 0

        for _ in range(7):
            app_state.conversation.tick()
            await asyncio.sleep(0)

        assert app_state.conversation.frame() == 0
        assert live.update.call_count == 0

        # A real input mutation remains a legitimate redraw reason while the
        # approval/question owns the terminal.
        app_state.input.buf.set(["x"])
        await asyncio.sleep(0)
        assert live.update.call_count == 1
    finally:
        workspace.stop()
        if not request_task.done():
            service.respond(allowed=False)
        await request_task
