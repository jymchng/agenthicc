"""Extended unit tests for TUIEventAdapter covering previously uncovered branches."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agenthicc.kernel import AppState, Event, SecurityPolicy, SystemSettings
from agenthicc.kernel.processor import EventProcessor
from agenthicc.tui.transcript import ToolCallState, TranscriptModel
from agenthicc.tui.events import TUIEventAdapter

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────


def _ev(event_type: str, payload: dict, source_agent_id: str | None = None) -> Event:
    return Event.create(event_type, payload, source_agent_id=source_agent_id)


def _fresh() -> tuple[TranscriptModel, TUIEventAdapter]:
    m = TranscriptModel()
    return m, TUIEventAdapter(m)


def _make_processor() -> EventProcessor:
    state = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    return EventProcessor(initial_state=state, persist=False)


# ── line 48: _on_application_log ─────────────────────────────────────────


class TestApplicationLog:
    def test_application_log_creates_line(self):
        """Applying ApplicationLog appends a formatted log line to the model."""
        m, adapter = _fresh()
        adapter.apply(_ev("ApplicationLog", {"level": "debug", "message": "started"}, source_agent_id="a1"))
        rendered = "\n".join(m.render())
        assert "DEBUG" in rendered
        assert "started" in rendered

    def test_application_log_default_level(self):
        """Missing level defaults to INFO."""
        m, adapter = _fresh()
        adapter.apply(_ev("ApplicationLog", {"message": "no level field"}, source_agent_id="a1"))
        rendered = "\n".join(m.render())
        assert "INFO" in rendered
        assert "no level field" in rendered

    def test_application_log_empty_message(self):
        """Empty message string is handled without crash."""
        m, adapter = _fresh()
        adapter.apply(_ev("ApplicationLog", {"level": "warn", "message": ""}, source_agent_id="a1"))
        # At least one line rendered (even if message is blank)
        assert len(m.render()) >= 1


# ── line 59: _on_agent_spawn ──────────────────────────────────────────────


class TestAgentSpawn:
    def test_agent_spawn_creates_turn(self):
        """AgentSpawnRequest creates a new turn in the model."""
        m, adapter = _fresh()
        adapter.apply(_ev("AgentSpawnRequest", {"agent_id": "a42", "agent_type": "PlannerAgent"}))
        rendered = "\n".join(m.render())
        # The turn header includes the agent_name (agent_type) or agent_id
        assert "a42" in rendered or "PlannerAgent" in rendered

    def test_agent_spawn_uses_agent_type_as_name(self):
        """agent_name derived from agent_type field."""
        m, adapter = _fresh()
        adapter.apply(_ev("AgentSpawnRequest", {"agent_id": "b1", "agent_type": "ExecutorAgent"}))
        # The turn should exist in model.turns
        assert len(m.turns) == 1
        assert m.turns[0].agent_name == "ExecutorAgent"

    def test_agent_spawn_falls_back_to_agent_id_when_no_type(self):
        """agent_name falls back to agent_id when agent_type is absent."""
        m, adapter = _fresh()
        adapter.apply(_ev("AgentSpawnRequest", {"agent_id": "c9"}))
        assert len(m.turns) == 1
        assert m.turns[0].agent_name == "c9"


# ── lines 69-73: _on_node_status ─────────────────────────────────────────


class TestNodeStatus:
    def test_node_status_complete(self):
        """WorkflowNodeStatusChanged with status=complete appends a line."""
        m, adapter = _fresh()
        adapter.apply(_ev(
            "WorkflowNodeStatusChanged",
            {"node_id": "node-1", "status": "complete"},
            source_agent_id="a1",
        ))
        rendered = "\n".join(m.render())
        assert "node-1" in rendered
        assert "complete" in rendered

    def test_node_status_failed_with_error(self):
        """WorkflowNodeStatusChanged with status=failed and error appends error info."""
        m, adapter = _fresh()
        adapter.apply(_ev(
            "WorkflowNodeStatusChanged",
            {"node_id": "node-2", "status": "failed", "error": "timeout"},
            source_agent_id="a1",
        ))
        rendered = "\n".join(m.render())
        assert "node-2" in rendered
        assert "failed" in rendered
        assert "timeout" in rendered

    def test_node_status_no_error_field(self):
        """WorkflowNodeStatusChanged without error field still renders cleanly."""
        m, adapter = _fresh()
        adapter.apply(_ev(
            "WorkflowNodeStatusChanged",
            {"node_id": "node-3", "status": "running"},
            source_agent_id="a1",
        ))
        rendered = "\n".join(m.render())
        assert "node-3" in rendered


# ── lines 110-116: _on_tool_started / _on_tool_complete missing fields ────


class TestToolEvents:
    def test_tool_started_missing_tool_use_id_no_crash(self):
        """ToolCallStarted without tool_use_id is silently ignored."""
        m, adapter = _fresh()
        m.append_turn("a1", "agent", 0.0)
        before = list(m.render())
        adapter.apply(_ev("ToolCallStarted", {"name": "some_tool"}, source_agent_id="a1"))
        # No new tool calls registered
        assert not m.has_running_tools()

    def test_tool_complete_missing_tool_use_id_no_crash(self):
        """ToolCallComplete without tool_use_id is silently ignored."""
        m, adapter = _fresh()
        # Should not raise
        adapter.apply(_ev("ToolCallComplete", {"success": True}))

    def test_tool_complete_uses_tool_call_id_fallback(self):
        """ToolCallComplete with tool_call_id (not tool_use_id) is resolved."""
        m, adapter = _fresh()
        m.append_turn("a1", "agent", 0.0)
        # Register a tool via started event using tool_use_id
        adapter.apply(_ev("ToolCallStarted", {"tool_use_id": "tid1", "name": "op"}, source_agent_id="a1"))
        assert m.has_running_tools()
        # Complete it using the same ID via tool_call_id key
        adapter.apply(_ev("ToolCallComplete", {"tool_call_id": "tid1", "success": True}))
        assert not m.has_running_tools()

    # ── line 125: tool complete with success=False ─────────────────────────

    def test_tool_complete_failure_marks_failure_state(self):
        """ToolCallComplete with success=False transitions tool to FAILURE."""
        m, adapter = _fresh()
        m.append_turn("a1", "agent", 0.0)
        adapter.apply(_ev("ToolCallStarted", {"tool_use_id": "tc-fail", "name": "risky_op"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallComplete", {
            "tool_use_id": "tc-fail",
            "success": False,
            "error": "disk full",
        }))
        entry = m._tool_index.get("tc-fail")
        assert entry is not None
        assert entry.state == ToolCallState.FAILURE

    def test_tool_complete_with_error_implies_failure(self):
        """When error is set and success is absent, tool is marked FAILURE."""
        m, adapter = _fresh()
        m.append_turn("a1", "agent", 0.0)
        adapter.apply(_ev("ToolCallStarted", {"tool_use_id": "tc-err", "name": "op"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallComplete", {
            "tool_use_id": "tc-err",
            "error": "oops",
        }))
        entry = m._tool_index.get("tc-err")
        assert entry is not None
        assert entry.state == ToolCallState.FAILURE

    def test_tool_complete_success_marks_success_state(self):
        m, adapter = _fresh()
        m.append_turn("a1", "agent", 0.0)
        adapter.apply(_ev("ToolCallStarted", {"tool_use_id": "tc-ok", "name": "op"}, source_agent_id="a1"))
        adapter.apply(_ev("ToolCallComplete", {
            "tool_use_id": "tc-ok",
            "success": True,
            "duration_ms": 42.0,
        }))
        entry = m._tool_index.get("tc-ok")
        assert entry is not None
        assert entry.state == ToolCallState.SUCCESS


# ── line 140: _on_ad_update with malformed payload ────────────────────────


class TestAdUpdate:
    def test_ad_update_malformed_payload_no_crash(self):
        """UIAdUpdate with empty payload sets a blank AdRecord without raising."""
        m, adapter = _fresh()
        adapter.apply(_ev("UIAdUpdate", {}))
        # Model set_current_ad was called; no exception raised
        ad = m.current_ad()
        assert ad is not None
        assert ad.ad_id == ""
        assert ad.text == ""

    def test_ad_update_full_payload(self):
        """UIAdUpdate with a full payload populates the ad record."""
        m, adapter = _fresh()
        adapter.apply(_ev("UIAdUpdate", {
            "ad_id": "ad-123",
            "text": "Try our new feature!",
            "cta_url": "https://example.com",
        }))
        ad = m.current_ad()
        assert ad is not None
        assert ad.ad_id == "ad-123"
        assert ad.text == "Try our new feature!"
        assert ad.cta_url == "https://example.com"

    def test_ad_update_partial_payload_no_crash(self):
        """UIAdUpdate with only ad_id populates defaults for missing fields."""
        m, adapter = _fresh()
        adapter.apply(_ev("UIAdUpdate", {"ad_id": "only-id"}))
        ad = m.current_ad()
        assert ad is not None
        assert ad.ad_id == "only-id"
        assert ad.text == ""


# ── unknown event type ────────────────────────────────────────────────────


class TestUnknownEvent:
    def test_unknown_event_no_crash(self):
        """An unrecognised event type is silently ignored."""
        m, adapter = _fresh()
        before = list(m.render())
        adapter.apply(_ev("SomeTotallyUnknownEvent", {"foo": "bar"}))
        assert list(m.render()) == before

    def test_non_dict_payload_handled_gracefully(self):
        """Even if payload attr is not a dict, the adapter falls back to {}."""
        m, adapter = _fresh()
        # Craft an event whose payload is a list, not a dict
        ev = MagicMock()
        ev.event_type = "UIUpdate"
        ev.payload = ["not", "a", "dict"]
        adapter.apply(ev)
        # No crash — payload was coerced to {}

    def test_event_with_no_payload_attr(self):
        """Event-like object with no payload attribute does not crash."""
        m, adapter = _fresh()
        ev = MagicMock(spec=[])  # no attributes
        ev.event_type = "UIUpdate"
        # getattr(ev, "payload", None) returns None, coerced to {}
        adapter.apply(ev)


# ── sync incremental cursor ───────────────────────────────────────────────


class TestSyncIncrementalCursor:
    def test_sync_incremental_cursor_advances(self):
        """Cursor advances correctly across two sync calls."""
        m, adapter = _fresh()
        processor = _make_processor()
        adapter.subscribe_to(processor)

        # Add first batch
        processor._event_log.append(_ev("UIUpdate", {"content": "msg1", "ui_type": "message"}, source_agent_id="a1"))
        c1 = adapter.sync()
        assert c1 == 1

        # Add second batch
        processor._event_log.append(_ev("UIUpdate", {"content": "msg2", "ui_type": "message"}, source_agent_id="a1"))
        processor._event_log.append(_ev("UIUpdate", {"content": "msg3", "ui_type": "message"}, source_agent_id="a1"))
        c2 = adapter.sync()
        assert c2 == 2

        rendered = "\n".join(m.render())
        assert "msg1" in rendered
        assert "msg2" in rendered
        assert "msg3" in rendered

    def test_sync_without_processor_returns_zero(self):
        """sync() with no attached processor returns 0."""
        m, adapter = _fresh()
        assert adapter.sync() == 0

    def test_sync_repeated_empty_returns_zero(self):
        """Repeated sync when no new events returns 0."""
        m, adapter = _fresh()
        processor = _make_processor()
        adapter.subscribe_to(processor)
        processor._event_log.append(_ev("UIUpdate", {"content": "x"}, source_agent_id="a1"))
        adapter.sync()
        c = adapter.sync()
        assert c == 0


# ── consume queue ────────────────────────────────────────────────────────


class TestConsumeQueue:
    async def test_consume_processes_events_until_sentinel(self):
        """consume() drains the queue until None sentinel is received."""
        m, adapter = _fresh()
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(_ev("ApplicationLog", {"level": "info", "message": "from queue"}, source_agent_id="a1"))
        q.put_nowait(None)  # sentinel
        await adapter.consume(q)
        rendered = "\n".join(m.render())
        assert "from queue" in rendered
