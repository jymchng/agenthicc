"""Integration coverage for PRD-189 question scrolling and response flow."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.overlay import OverlayHost
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_overlay_host_scrolls_long_question_without_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agenthicc.tui.workspace.overlays.questions.shutil.get_terminal_size",
        lambda _fallback=(80, 24): os.terminal_size((40, 24)),
    )
    app_state = AppState.create()
    service = ApprovalService(app_state)
    request = ApprovalRequest(
        tool_name="ask_user",
        tool_use_id="integration-ask",
        tool_input={
            "questions": [
                {
                    "id": "scope",
                    "text": " ".join(f"scope-constraint-{i}" for i in range(60)),
                    "options": ["Everything"],
                }
            ]
        },
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="questions",
    )
    request_task = asyncio.create_task(service.request_approval(request))
    host = OverlayHost(app_state)

    try:
        await asyncio.sleep(0)
        overlay = QuestionsOverlay(request, service, host.hide)
        host.show(overlay)
        host.render()
        for _ in range(100):
            host.handle_key(Key.CHAR, "]")
        assert overlay._states[0].question_scroll > 0
        assert not request_task.done()

        host.handle_key(Key.ENTER, "")
        response = await request_task
        assert response.allowed is True
        assert json.loads(response.message) == {"scope": "Everything"}
    finally:
        if not request_task.done():
            service.respond(allowed=False)
            await request_task
