"""Integration coverage for diff previews in the conversation renderer."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_file_modified_event_renders_a_bounded_addition_preview() -> None:
    state = AppState.create()
    console = Console(record=True, width=120, color_system=None)
    appender = ScrollBufferAppender(state, console)
    appender.mount()
    try:
        state.conversation.begin_turn("assistant")
        state.conversation.append_event(
            "file_modified",
            {
                "path": "README.md",
                "tool": "write_file",
                "old_lines": ["title"],
                "new_lines": ["title", *[f"added {index}" for index in range(1, 11)]],
            },
        )
        await asyncio.sleep(0)
    finally:
        appender.unmount()

    output = console.export_text()
    assert "● Update(README.md)" in output
    assert "Added 10 lines" in output
    assert "added 1" in output
    assert "added 10" in output
    assert "added 5" not in output
    assert "..." in output
    assert "4 more diff lines" in output
