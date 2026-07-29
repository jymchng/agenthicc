"""End-to-end coverage for response rendering through the conversation stream."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_response_then_next_user_event_has_visual_separator() -> None:
    state = AppState.create()
    console = Console(record=True, width=80)
    appender = ScrollBufferAppender(state, console)
    appender.mount()
    try:
        state.conversation.begin_turn("assistant")
        state.conversation.append_event("text", {"text": "LLM response."})
        state.conversation.append_event("user_message", {"text": "next request"})
        await asyncio.sleep(0)
    finally:
        appender.unmount()

    lines = console.export_text().splitlines()
    response_index = next(index for index, line in enumerate(lines) if "LLM response." in line)
    request_index = next(index for index, line in enumerate(lines) if "next request" in line)
    assert lines[response_index].rstrip() == "LLM response."
    assert lines[response_index + 1] == ""
    assert response_index < request_index
