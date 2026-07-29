"""Integration coverage for the plan-review mode handoff."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.tools.approval import ApprovalResponse, ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.code_plan.phase_tools import make_planner_tools

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_approval_service_carries_yolo_into_finalize() -> None:
    app = AppState.create()
    service = ApprovalService(app)
    real_request_approval = service.request_approval

    async def approve(request: object) -> ApprovalResponse:
        # The real service owns the pending request and event.  Respond on the
        # next loop turn, just as the TUI does after a key press.
        asyncio.get_running_loop().call_soon(lambda: service.respond(True, mode="Yolo"))
        return await real_request_approval(request)  # type: ignore[arg-type]

    service.request_approval = approve  # type: ignore[method-assign]
    plan_event = asyncio.Event()
    plan_data: dict[str, object] = {}
    request_plan_approval, finalize_plan = make_planner_tools(
        service,
        plan_event,
        plan_data,
    )[:2]

    approval = await request_plan_approval("implement the approved plan")
    assert approval["approved"] is True
    assert approval["execute_mode"] == "Yolo"

    finalized = await finalize_plan("implement the approved plan")
    assert finalized["ok"] is True
    assert plan_event.is_set()
    assert plan_data == {
        "plan": "implement the approved plan",
        "execute_mode": "Yolo",
    }
