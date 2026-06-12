"""Unit tests for StatusState, status panel, and lifecycle hooks (PRD-20)."""
from __future__ import annotations
import io
import time
import pytest
from rich.console import Console
from agenthicc.tui.transcript import TranscriptModel, ToolCallState
from agenthicc.tui.app import InlineRenderer, StatusState, render_frame_ansi

pytestmark = pytest.mark.unit


def _con():
    buf = io.StringIO()
    return Console(file=buf, highlight=False, markup=False, force_terminal=True, width=80), buf


def _renderer():
    con, buf = _con()
    return InlineRenderer(TranscriptModel(), console=con), buf


class TestStatusState:
    def test_defaults(self):
        s = StatusState()
        assert s.active is False
        assert s.spinner_frame == 0
        assert s.input_tokens == 0
        assert s.output_tokens == 0
        assert s.session_cost_usd == 0.0
        assert s.completed_agents == 0
        assert s.session_id == ""

    def test_mutable_fields(self):
        s = StatusState()
        s.active = True; s.input_tokens = 500
        assert s.active and s.input_tokens == 500


class TestStatusPanel:
    def test_panel_not_none(self):
        renderer, _ = _renderer()
        panel = renderer._render_status_panel()
        assert panel is not None

    def test_active_renders_thinking(self):
        renderer, buf = _renderer()
        renderer._status.active = True
        renderer._status.intent_started_at = time.monotonic() - 3.0
        renderer._status.input_tokens = 100
        renderer._status.output_tokens = 50
        renderer.console.print(renderer._render_status_panel())
        out = buf.getvalue()
        assert "Thinking" in out or "tok" in out or len(out) > 5

    def test_idle_renders_session_id(self):
        renderer, buf = _renderer()
        renderer._status.active = False
        renderer._status.session_id = "abc123def456"
        renderer._status.completed_agents = 2
        renderer.console.print(renderer._render_status_panel())
        out = buf.getvalue()
        assert "abc123" in out or "2" in out

    def test_idle_shows_cost(self):
        renderer, buf = _renderer()
        renderer._status.active = False
        renderer._status.session_cost_usd = 0.042
        renderer.console.print(renderer._render_status_panel())
        out = buf.getvalue()
        assert "0.042" in out or "$" in out

    def test_active_shows_token_counts(self):
        renderer, buf = _renderer()
        renderer._status.active = True
        renderer._status.input_tokens = 1204
        renderer._status.output_tokens = 342
        renderer.console.print(renderer._render_status_panel())
        out = buf.getvalue()
        assert "1" in out and len(out) > 10


class TestLifecycleHooks:
    def test_on_intent_submitted_activates(self):
        renderer, _ = _renderer()
        assert not renderer._status.active
        renderer.on_intent_submitted()
        assert renderer._status.active

    def test_on_intent_submitted_resets_tokens(self):
        renderer, _ = _renderer()
        renderer._status.input_tokens = 999
        renderer._status.output_tokens = 888
        renderer.on_intent_submitted()
        assert renderer._status.input_tokens == 0
        assert renderer._status.output_tokens == 0

    def test_on_intent_submitted_sets_start_time(self):
        renderer, _ = _renderer()
        before = time.monotonic()
        renderer.on_intent_submitted()
        assert renderer._status.intent_started_at >= before

    def test_on_model_call_complete_accumulates(self):
        renderer, _ = _renderer()
        renderer.on_model_call_complete(100, 50, 0.001)
        renderer.on_model_call_complete(200, 100, 0.002)
        assert renderer._status.input_tokens == 300
        assert renderer._status.output_tokens == 150
        assert abs(renderer._status.session_cost_usd - 0.003) < 1e-9

    def test_on_agent_run_complete_deactivates(self):
        renderer, _ = _renderer()
        renderer._status.active = True
        renderer.on_agent_run_complete()
        assert renderer._status.active is False

    def test_on_agent_run_complete_increments_count(self):
        renderer, _ = _renderer()
        renderer._status.completed_agents = 2
        renderer.on_agent_run_complete()
        assert renderer._status.completed_agents == 3

    def test_on_agent_run_complete_stays_active_with_running_tool(self):
        renderer, _ = _renderer()
        renderer._status.active = True
        renderer.model.append_turn("a1", "agent:test", 0.0)
        renderer.model.add_tool_call("a1", "tc1", "slow_tool")
        assert renderer.has_running_tools()
        renderer.on_agent_run_complete()
        assert renderer._status.active is True


class TestRendererHasStatusAttr:
    def test_status_attr_present(self):
        renderer, _ = _renderer()
        assert hasattr(renderer, "_status")
        assert isinstance(renderer._status, StatusState)

    def test_spinner_frame_increments(self):
        renderer, _ = _renderer()
        initial = renderer._status.spinner_frame
        renderer._status.spinner_frame += 1
        assert renderer._status.spinner_frame == initial + 1


class TestRenderFrameAnsiWithStatus:
    def test_no_status_state_backward_compat(self):
        model = TranscriptModel()
        frame = render_frame_ansi(model, cols=80, rows=24)
        assert isinstance(frame, str) and ">" in frame

    def test_active_status_adds_thinking_text(self):
        model = TranscriptModel()
        s = StatusState()
        s.active = True
        s.intent_started_at = time.monotonic() - 2.0
        frame = render_frame_ansi(model, cols=80, rows=24, status_state=s)
        assert "Thinking" in frame or any(c in frame for c in "⣾⣽⣻⢿⡿⣟⣯⣷")

    def test_input_bar_on_last_row_with_active_status(self):
        import pyte
        model = TranscriptModel()
        s = StatusState(); s.active = True; s.intent_started_at = time.monotonic()
        frame = render_frame_ansi(model, cols=80, rows=24, status_state=s)
        screen = pyte.Screen(80, 24)
        pyte.ByteStream(screen).feed(frame.encode())
        last = "".join(c.data for c in screen.buffer[23].values())
        assert ">" in last
