"""Captured-terminal regression coverage for PRD-165."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from rich.console import Console

from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

pytestmark = pytest.mark.e2e


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


def _overlay(
    kind: str,
    request: ApprovalRequest,
    service: ApprovalService,
    close: Callable[[], None],
) -> Overlay:
    if kind == "plan_review":
        return PlanApprovalOverlay(request, service, close)
    if kind == "questions":
        return QuestionsOverlay(request, service, close)
    return ApprovalOverlay(request, service, close)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "header"),
    [
        ("tool", "Tool Approval Required"),
        ("plan_review", "Plan Review"),
        ("questions", "Questions"),
    ],
)
async def test_waiting_ticks_do_not_append_duplicate_prompt_surfaces(
    kind: str,
    header: str,
) -> None:
    """ANSI-insensitive captures contain one prompt surface after many ticks."""
    app_state = AppState.create()
    app_state.conversation.begin_turn("agent", f"turn-{kind}")
    service = ApprovalService(app_state)
    console = Console(record=True, force_terminal=True, width=120)
    workspace = Workspace(app_state, console)
    workspace.start()
    request = _request(kind)
    request_task = asyncio.create_task(service.request_approval(request))

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        workspace.overlays.show(_overlay(kind, request, service, workspace.overlays.hide))
        await asyncio.sleep(0)

        live = workspace._live
        assert live is not None
        updates: list[object] = []
        original_update = live.update

        def _record_update(renderable: object, *, refresh: bool = False) -> None:
            updates.append(renderable)
            original_update(renderable, refresh=refresh)

        live.update = _record_update  # type: ignore[method-assign]
        updates.clear()

        for _ in range(7):
            app_state.conversation.tick()
            await asyncio.sleep(0)

        assert updates == []
    finally:
        workspace.overlays.hide()
        if not request_task.done():
            service.respond(allowed=False)
        await request_task
        workspace.stop()

    assert console.export_text().count(header) == 1
