"""Integration coverage for permanent TUI response spacing."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_conversation_text_event_renders_with_a_blank_line_after_response() -> None:
    state = AppState.create()
    console = Console(record=True, width=80)
    appender = ScrollBufferAppender(state, console)
    appender.mount()
    try:
        state.conversation.begin_turn("assistant")
        state.conversation.append_event("text", {"text": "LLM response."})
        await asyncio.sleep(0)
    finally:
        appender.unmount()

    lines = console.export_text().splitlines()
    response_index = next(index for index, line in enumerate(lines) if "LLM response." in line)
    assert lines[response_index].rstrip() == "LLM response."
    assert lines[response_index + 1] == ""
