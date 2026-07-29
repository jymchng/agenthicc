"""End-to-end plan overlay → approval service → planner-tool scenarios."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.tools.approval import ApprovalService
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay
from agenthicc.workflows.code_plan.phase_tools import make_planner_tools

pytestmark = pytest.mark.e2e


async def _wait_for_pending(app: AppState) -> object:
    for _ in range(100):
        request = app.pending_approval()
        if request is not None:
            return request
        await asyncio.sleep(0)
    raise AssertionError("planner did not publish a plan approval request")


@pytest.mark.asyncio
async def test_overlay_yolo_choice_reaches_planner_and_finalize() -> None:
    app = AppState.create()
    service = ApprovalService(app)
    plan_event = asyncio.Event()
    plan_data: dict[str, object] = {}
    request_plan_approval, finalize_plan = make_planner_tools(
        service,
        plan_event,
        plan_data,
    )[:2]

    request_task = asyncio.create_task(request_plan_approval("ship the change"))
    request = await _wait_for_pending(app)
    closed: list[bool] = []
    overlay = PlanApprovalOverlay(request, service, lambda: closed.append(True))  # type: ignore[arg-type]
    overlay.on_mount()
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")

    approval = await request_task
    assert approval["approved"] is True
    assert approval["execute_mode"] == "Yolo"
    assert closed == [True]

    finalized = await finalize_plan("ship the change")
    assert finalized["ok"] is True
    assert plan_data["execute_mode"] == "Yolo"
    assert plan_event.is_set()
