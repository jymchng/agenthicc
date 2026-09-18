"""Deterministic local user journey for an expired Questions overlay."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.overlay import OverlayHost
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_unanswered_question_expires_closes_overlay_and_returns_fallback_result() -> None:
    app = AppState.create()
    service = ApprovalService(app, question_timeout_s=0.02)
    host = OverlayHost(app)
    request = ApprovalRequest(
        tool_name="ask_user",
        tool_use_id="e2e-question",
        tool_input={
            "questions": [{"id": "choice", "text": "Choose an option", "options": ["Default"]}]
        },
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="questions",
        question_fingerprint="e2e-question-fingerprint",
    )

    def sync_overlay() -> None:
        if app.pending_approval() is None:
            host.hide()

    app.pending_approval.subscribe(sync_overlay)
    task = asyncio.create_task(service.request_approval(request))
    await asyncio.sleep(0)
    host.show(QuestionsOverlay(request, service, host.hide))

    response = await asyncio.wait_for(task, timeout=1)

    assert response.timed_out is True
    assert response.decision_required is True
    assert host.active is False
    assert app.pending_approval() is None
    assert "choice" not in response.message
