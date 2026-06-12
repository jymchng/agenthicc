"""Integration: InlineRenderer subscribes to a live EventProcessor (PRD-09)."""
from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.events import TUIEventAdapter
from agenthicc.tui.app import InlineRenderer

pytestmark = pytest.mark.integration


@pytest.fixture
async def proc(tmp_path):
    state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "s.json"),
        ),
        policy=SecurityPolicy(),
    )
    p = EventProcessor(initial_state=state, persist=False)
    t = asyncio.create_task(p.run())
    yield p
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)


async def test_new_lines_printed_after_ui_update(proc):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)
    renderer = InlineRenderer(model, adapter, console=con)

    await proc.emit(Event.create(
        "UIUpdate",
        {"content": "hello from integration test", "ui_type": "message"},
        source_agent_id="a1",
    ))
    await proc.drain()

    adapter.sync()
    renderer._flush_new_lines()
    assert "hello from integration test" in buf.getvalue()


async def test_tool_running_then_complete(proc):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)
    renderer = InlineRenderer(model, adapter, console=con)

    await proc.emit(Event.create(
        "AgentSpawnRequest",
        {"agent_id": "a1", "agent_type": "T"},
        source_agent_id="a1",
    ))
    await proc.emit(Event.create(
        "ToolCallStarted",
        {"tool_name": "read_file", "tool_use_id": "tc1", "agent_id": "a1"},
        source_agent_id="a1",
    ))
    await proc.drain()
    adapter.sync()
    assert renderer.has_running_tools()

    await proc.emit(Event.create(
        "ToolCallComplete",
        {
            "tool_name": "read_file",
            "tool_use_id": "tc1",
            "agent_id": "a1",
            "success": True,
            "duration_ms": 8.0,
        },
    ))
    await proc.drain()
    adapter.sync()
    assert not renderer.has_running_tools()
