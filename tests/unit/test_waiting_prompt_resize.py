"""Regression coverage for stable Plan Review rendering while waiting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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


def test_waiting_render_is_stable_when_wall_clock_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415
    from agenthicc.tui.workspace.components import StatusComponent  # noqa: PLC0415

    now = 10.0
    monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
    app_state = AppState.create()
    app_state.conversation.begin_turn("agent", "turn-1")
    now = 12.0
    app_state.conversation.tick()
    app_state.pending_approval.set(SimpleNamespace(kind="plan_review"))  # type: ignore[arg-type]

    status = StatusComponent(app_state)
    console = Console(force_terminal=False, width=100)
    first = _render_text(console, status.render())
    now = 60.0
    second = _render_text(console, status.render())

    assert first == second
    assert "Waiting for plan approval" in first
    assert "2s" in first
