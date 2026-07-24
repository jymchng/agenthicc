"""End-to-end Rich workspace coverage for PRD-144."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from rich.console import Console

from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

pytestmark = pytest.mark.e2e


def _request(kind: str) -> ApprovalRequest:
    tool_input: dict[str, object] = {}
    if kind == "plan_review":
        tool_input = {"plan": "# Plan\n\nReview this plan while the terminal changes size."}
    elif kind == "questions":
        tool_input = {
            "questions": [
                {
                    "id": "scope",
                    "text": "What should be included?",
                    "options": ["Core", "Everything"],
                },
                {
                    "id": "format",
                    "text": "Which format should be used?",
                    "options": ["Markdown", "Plain text"],
                },
            ]
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
    close: Any,
) -> object:
    overlay_type = {
        "tool": ApprovalOverlay,
        "plan_review": PlanApprovalOverlay,
        "questions": QuestionsOverlay,
    }[kind]
    return overlay_type(request, service, close)


def _render_text(console: Console, renderable: object) -> str:
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "header"),
    [
        ("tool", "Tool Approval Required"),
        ("plan_review", "Plan Review"),
        ("questions", "Questions"),
    ],
)
async def test_resize_storm_repaints_one_current_waiting_modal(
    kind: str,
    header: str,
) -> None:
    app_state = AppState.create()
    service = ApprovalService(app_state)
    console = Console(record=True, force_terminal=False, width=100)
    workspace = Workspace(app_state, console)
    workspace.start()
    request = _request(kind)
    request_task = asyncio.create_task(service.request_approval(request))

    try:
        await asyncio.sleep(0)
        assert app_state.pending_approval() is request
        workspace.overlays.show(_overlay(kind, request, service, workspace.overlays.hide))
        await asyncio.sleep(0)
        if kind == "questions":
            # Exercise a live selection before the resize storm; the current
            # answer cursor must survive a geometry-only repaint.
            workspace.overlays.handle_key(Key.DOWN, "")
            workspace.overlays.handle_key(Key.ENTER, "")
            workspace.overlays.handle_key(Key.DOWN, "")
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

        # SIGWINCH can arrive in bursts while a user drags a terminal edge.
        for _ in range(12):
            workspace._on_sigwinch(28, None)
        await asyncio.sleep(0.12)

        assert len(updates) == 1
        rendered = _render_text(console, updates[0])
        assert rendered.count(header) == 1
        assert rendered.count(header) != 2
        if kind == "questions":
            assert "▶ Plain text" in rendered
            question_overlay = workspace.overlays.widget
            assert isinstance(question_overlay, QuestionsOverlay)
            assert question_overlay._current == 1
            assert question_overlay._states[0].answered is True
            assert question_overlay._states[0].answer == "Everything"
    finally:
        service.respond(allowed=True)
        await request_task
        workspace.stop()
