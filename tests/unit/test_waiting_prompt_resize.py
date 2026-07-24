"""Regression coverage for stable Plan Review rendering while waiting."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay

pytestmark = pytest.mark.unit


def _render_text(console: Console, renderable: object) -> str:
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_plan_review_stays_single_and_stable_when_animation_is_paused() -> None:
    app_state = AppState.create()
    app_state.conversation.begin_turn("agent", "turn-1")
    request = ApprovalRequest(
        tool_name="plan_review",
        tool_use_id="tool-1",
        tool_input={"plan": "# Plan\n\nReview this plan."},
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="plan_review",
    )
    app_state.pending_approval.set(request)
    console = Console(force_terminal=False, width=100)
    workspace = Workspace(app_state, console)
    workspace.overlays.show(PlanApprovalOverlay(request, ApprovalService(app_state), lambda: None))

    first = _render_text(console, workspace._build())
    frame_before = app_state.conversation.frame()
    app_state.conversation.tick(paused=app_state.pending_approval() is not None)
    second = _render_text(console, workspace._build())

    assert app_state.conversation.frame() == frame_before
    assert first == second
    assert first.count("Plan Review") == 1
