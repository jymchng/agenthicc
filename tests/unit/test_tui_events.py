"""Unit tests for TUIEventAdapter (PRD-06)."""
from __future__ import annotations
import asyncio
import pytest
from agenthicc.kernel import Event
from agenthicc.tui.transcript import ToolCallState, TranscriptModel
from agenthicc.tui.events import TUIEventAdapter

pytestmark = pytest.mark.unit

def _ev(event_type, payload, source_agent_id=None):
    return Event.create(event_type, payload, source_agent_id=source_agent_id)

def _fresh():
    m = TranscriptModel(); return m, TUIEventAdapter(m)

class TestTUIEventAdapter:
    def test_ui_update_appends_line(self):
        m, adapter = _fresh()
        adapter.apply(_ev("UIUpdate", {"content": "hello world", "ui_type": "message"}, source_agent_id="a1"))
        assert any("hello world" in l for l in m.render())

    def test_application_log_appends_line(self):
        m, adapter = _fresh()
        adapter.apply(_ev("ApplicationLog", {"level": "INFO", "message": "agent started"}, source_agent_id="a1"))
        rendered = "\n".join(m.render())
        assert "agent started" in rendered or len(m.render()) > 0

    def test_agent_spawn_creates_turn(self):
        m, adapter = _fresh()
        adapter.apply(_ev("AgentSpawnRequest", {"agent_id": "a99", "agent_type": "Worker"}, source_agent_id="a99"))
        rendered = "\n".join(m.render())
        assert "a99" in rendered or len(m.render()) > 0

    def test_tool_call_started_creates_entry(self):
        m, adapter = _fresh()
        adapter.apply(_ev("AgentSpawnRequest", {"agent_id": "a1", "agent_type": "T"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallStarted", {"tool_name": "read_file", "tool_use_id": "tc1", "agent_id": "a1"}, source_agent_id="a1"))
        assert any("read_file" in l for l in m.render())

    def test_tool_call_complete_success(self):
        m, adapter = _fresh()
        adapter.apply(_ev("AgentSpawnRequest", {"agent_id": "a1", "agent_type": "T"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallStarted", {"tool_name": "write_file", "tool_use_id": "tc2", "agent_id": "a1"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallComplete", {"tool_name": "write_file", "tool_use_id": "tc2", "agent_id": "a1", "success": True, "duration_ms": 12.0}))
        assert any("write_file" in l for l in m.render())
        assert not m.has_running_tools()

    def test_tool_call_complete_failure(self):
        m, adapter = _fresh()
        adapter.apply(_ev("AgentSpawnRequest", {"agent_id": "a1", "agent_type": "T"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallStarted", {"tool_name": "run_tests", "tool_use_id": "tc3", "agent_id": "a1"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallComplete", {"tool_name": "run_tests", "tool_use_id": "tc3", "agent_id": "a1", "success": False, "error": "3 failures", "duration_ms": 5.0}))
        rendered = "\n".join(m.render())
        assert "run_tests" in rendered

    def test_unknown_event_ignored(self):
        m, adapter = _fresh()
        before = m.render()
        adapter.apply(_ev("SomeUnknownEventXYZ", {"foo": "bar"}))
        # Model is unchanged (same number of lines)
        assert len(m.render()) == len(before)

    def test_subscribe_and_sync(self):
        m, adapter = _fresh()
        from agenthicc.kernel import AppState, SecurityPolicy, SystemSettings
        from agenthicc.kernel.processor import EventProcessor
        state = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
        processor = EventProcessor(initial_state=state, persist=False)
        # Manually add events to log without running the processor loop
        processor._event_log.append(Event.create("UIUpdate", {"content": "synced msg", "ui_type": "message"}, source_agent_id="a1"))
        adapter.subscribe_to(processor)
        count = adapter.sync()
        assert count == 1
        assert any("synced msg" in l for l in m.render())

    def test_sync_is_incremental(self):
        m, adapter = _fresh()
        from agenthicc.kernel import AppState, SecurityPolicy, SystemSettings
        from agenthicc.kernel.processor import EventProcessor
        state = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
        processor = EventProcessor(initial_state=state, persist=False)
        adapter.subscribe_to(processor)
        processor._event_log.append(Event.create("UIUpdate", {"content": "first", "ui_type": "message"}, source_agent_id="a1"))
        c1 = adapter.sync()
        processor._event_log.append(Event.create("UIUpdate", {"content": "second", "ui_type": "message"}, source_agent_id="a1"))
        c2 = adapter.sync()
        assert c1 == 1 and c2 == 1
