"""End-to-end coverage for bounded file-change rendering."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_conversation_stream_preserves_summary_and_collapses_large_replace() -> None:
    state = AppState.create()
    console = Console(record=True, width=120, color_system=None)
    appender = ScrollBufferAppender(state, console)
    appender.mount()
    try:
        state.conversation.begin_turn("assistant")
        state.conversation.append_event(
            "file_modified",
            {
                "path": "src/example.py",
                "tool": "write_file",
                "old_lines": [f"old {index}" for index in range(1, 13)],
                "new_lines": [f"new {index}" for index in range(1, 13)],
            },
        )
        state.conversation.append_event("text", {"text": "finished"})
        await asyncio.sleep(0)
    finally:
        appender.unmount()

    output = console.export_text()
    assert "● Update(src/example.py)" in output
    assert "Added 12 lines, removed 12 lines" in output
    assert output.count("...") == 1
    assert "18 more diff lines" in output
    assert "old 1" in output and "old 3" in output
    assert "new 10" in output and "new 12" in output
    assert "old 4" not in output and "new 9" not in output
    assert "finished" in output
