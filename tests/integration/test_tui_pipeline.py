"""Integration tests for TUI event pipeline (PRD-06)."""
from __future__ import annotations
import asyncio
import time
import pytest
from agenthicc.kernel import Event, EventProcessor, AppState, SecurityPolicy, SystemSettings
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.events import TUIEventAdapter

pytestmark = pytest.mark.integration

@pytest.fixture
async def proc(tmp_path):
    state = AppState.create(settings=SystemSettings(event_log_path=str(tmp_path/"ev.jsonl"), snapshot_path=str(tmp_path/"s.json")), policy=SecurityPolicy())
    p = EventProcessor(initial_state=state, persist=False)
    t = asyncio.create_task(p.run())
    yield p
    t.cancel(); await asyncio.gather(t, return_exceptions=True)

async def test_events_appear_in_transcript(proc):
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)

    await proc.emit(Event.create("AgentSpawnRequest", {"agent_id": "a1", "agent_type": "W"}, source_agent_id="a1"))
    await proc.emit(Event.create("UIUpdate", {"content": "pipeline working", "ui_type": "message"}, source_agent_id="a1"))
    await proc.emit(Event.create("ToolCallStarted", {"tool_name": "read_file", "tool_use_id": "tc1", "agent_id": "a1"}, source_agent_id="a1"))
    await proc.emit(Event.create("ToolCallComplete", {"tool_name": "read_file", "tool_use_id": "tc1", "agent_id": "a1", "success": True, "duration_ms": 5.0}))
    await proc.drain()
    adapter.sync()

    rendered = "\n".join(model.render())
    assert "pipeline working" in rendered
    assert "read_file" in rendered
    assert not model.has_running_tools()

async def test_100_events_render_under_200ms(proc):
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)

    for i in range(100):
        await proc.emit(Event.create("UIUpdate", {"content": f"line {i}", "ui_type": "message"}, source_agent_id="agent-0"))
    await proc.drain()

    start = time.perf_counter()
    adapter.sync()
    _ = model.render()
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 200, f"render took {elapsed_ms:.1f}ms"

async def test_tool_lifecycle_visible_in_transcript(proc):
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)

    await proc.emit(Event.create("AgentSpawnRequest", {"agent_id": "a2", "agent_type": "Tester"}, source_agent_id="a2"))
    await proc.emit(Event.create("ToolCallStarted", {"tool_name": "run_tests", "tool_use_id": "tc-x", "agent_id": "a2"}, source_agent_id="a2"))
    await proc.drain(); adapter.sync()
    assert model.has_running_tools()

    await proc.emit(Event.create("ToolCallComplete", {"tool_name": "run_tests", "tool_use_id": "tc-x", "agent_id": "a2", "success": False, "error": "3 failures", "duration_ms": 42.0}))
    await proc.drain(); adapter.sync()
    assert not model.has_running_tools()
    assert any("run_tests" in l for l in model.render())
