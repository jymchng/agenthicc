"""Integration coverage for PRD-144 prompt ownership and timing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.conversation_store import AppState

pytestmark = pytest.mark.integration


def _request(kind: str) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name=kind,
        tool_use_id=f"{kind}-1",
        tool_input={},
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind=kind,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool", "plan_review", "questions", "future_kind"])
async def test_real_approval_service_freezes_and_resumes_cached_display_clock(
    kind: str,
) -> None:
    app_state = AppState.create()
    service = ApprovalService(app_state)
    conversation = app_state.conversation
    conversation.begin_turn("agent", f"turn-{kind}")

    request_task = asyncio.create_task(service.request_approval(_request(kind)))
    await asyncio.sleep(0)
    assert app_state.pending_approval() is not None

    # A prompt set by the real service synchronously pauses both frame and
    # cached duration, even when the caller does not pass paused=True.
    before = conversation.display_elapsed_s()
    conversation.tick()
    conversation.tick()
    assert conversation.display_elapsed_s() == before

    service.respond(allowed=True)
    response = await request_task
    assert response.allowed is True
    assert app_state.pending_approval() is None

    conversation.tick()
    # Resume is observable through the normal tick path; no second timing
    # owner or explicit render callback is needed.
    assert conversation.frame() > 0


@pytest.mark.asyncio
async def test_cancelled_approval_releases_pending_modal_and_display_pause() -> None:
    app_state = AppState.create()
    service = ApprovalService(app_state)
    conversation = app_state.conversation
    conversation.begin_turn("agent", "turn-cancelled")
    request_task = asyncio.create_task(service.request_approval(_request("tool")))
    await asyncio.sleep(0)
    assert app_state.pending_approval() is not None

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert app_state.pending_approval() is None
    conversation.tick()
    assert conversation.frame() == 1


def test_malformed_prompt_object_still_owns_display_wait() -> None:
    app_state = AppState.create()
    conversation = app_state.conversation
    conversation.begin_turn("agent", "turn-malformed")
    app_state.pending_approval.set(SimpleNamespace())  # type: ignore[arg-type]

    before = conversation.display_elapsed_s()
    conversation.tick()

    assert conversation.display_elapsed_s() == before
