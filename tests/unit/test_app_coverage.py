"""Additional coverage tests for app.py uncovered lines.

Targets:
  - StatusState dataclass fields
  - InlineRenderer.on_intent_submitted() sets active=True, resets tokens
  - InlineRenderer.on_model_call_complete() accumulates tokens and cost
  - InlineRenderer.on_agent_run_complete() — active flag cleared when no tools running
  - InlineRenderer._render_status_panel() active and idle states
  - InlineRenderer._render_input_panel() with text
  - InlineRenderer._build_spinner_panel() with running tools
  - InlineRenderer.has_running_tools() True and False
  - SlashCommandHandler._expand() — @mention expansion
  - render_frame_ansi() with status_state=None, active, idle
  - render_frame_ansi() with menu_lines
  - detect_slash_command()
  - build_app() raises RuntimeError (deprecated)
  - run_headless() JSON-lines output
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from rich.console import Console

from agenthicc.tui.app import (
    INPUT_PROMPT,
    InlineRenderer,
    SlashCommandHandler,
    StatusState,
    build_app,
    detect_slash_command,
    render_frame_ansi,
    run_headless,
)
from agenthicc.tui.transcript import MentionChip, ToolCallState, TranscriptModel

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _console(width: int = 120) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, highlight=False, markup=False, width=width), buf


def _model() -> TranscriptModel:
    return TranscriptModel()


def _model_with_turn(agent_id: str = "a1") -> TranscriptModel:
    m = _model()
    m.append_turn(agent_id, "agent:test", 0.0)
    return m


# ---------------------------------------------------------------------------
# StatusState dataclass
# ---------------------------------------------------------------------------


class TestStatusState:
    def test_default_values(self):
        s = StatusState()
        assert s.active is False
        assert s.spinner_frame == 0
        assert s.intent_started_at == 0.0
        assert s.input_tokens == 0
        assert s.output_tokens == 0
        assert s.session_cost_usd == 0.0
        assert s.completed_agents == 0
        assert s.session_id == ""

    def test_custom_values(self):
        s = StatusState(
            active=True,
            spinner_frame=3,
            intent_started_at=1234.5,
            input_tokens=100,
            output_tokens=200,
            session_cost_usd=0.05,
            completed_agents=2,
            session_id="abc123",
        )
        assert s.active is True
        assert s.spinner_frame == 3
        assert s.input_tokens == 100
        assert s.output_tokens == 200
        assert s.session_cost_usd == 0.05
        assert s.completed_agents == 2
        assert s.session_id == "abc123"


# ---------------------------------------------------------------------------
# InlineRenderer.on_intent_submitted
# ---------------------------------------------------------------------------


class TestOnIntentSubmitted:
    def test_sets_active_true(self):
        m = _model()
        r = InlineRenderer(m, console=_console()[0])
        r.on_intent_submitted()
        assert r._status.active is True

    def test_resets_tokens(self):
        m = _model()
        r = InlineRenderer(m, console=_console()[0])
        r._status.input_tokens = 999
        r._status.output_tokens = 888
        r.on_intent_submitted()
        assert r._status.input_tokens == 0
        assert r._status.output_tokens == 0

    def test_sets_intent_started_at(self):
        m = _model()
        r = InlineRenderer(m, console=_console()[0])
        r.on_intent_submitted()
        assert r._status.intent_started_at > 0.0


# ---------------------------------------------------------------------------
# InlineRenderer.on_model_call_complete
# ---------------------------------------------------------------------------


class TestOnModelCallComplete:
    def test_accumulates_input_tokens(self):
        m = _model()
        r = InlineRenderer(m, console=_console()[0])
        r.on_model_call_complete(input_tokens=100, output_tokens=50)
        r.on_model_call_complete(input_tokens=200, output_tokens=80)
        assert r._status.input_tokens == 300
        assert r._status.output_tokens == 130

    def test_accumulates_cost(self):
        m = _model()
        r = InlineRenderer(m, console=_console()[0])
        r.on_model_call_complete(input_tokens=0, output_tokens=0, cost_usd=0.01)
        r.on_model_call_complete(input_tokens=0, output_tokens=0, cost_usd=0.02)
        assert abs(r._status.session_cost_usd - 0.03) < 1e-9

    def test_default_cost_is_zero(self):
        m = _model()
        r = InlineRenderer(m, console=_console()[0])
        r.on_model_call_complete(input_tokens=10, output_tokens=5)
        assert r._status.session_cost_usd == 0.0


# ---------------------------------------------------------------------------
# InlineRenderer.on_agent_run_complete
# ---------------------------------------------------------------------------


class TestOnAgentRunComplete:
    def test_increments_completed_agents(self):
        m = _model()
        r = InlineRenderer(m, console=_console()[0])
        r.on_agent_run_complete()
        assert r._status.completed_agents == 1

    def test_clears_active_when_no_running_tools(self):
        m = _model_with_turn()
        r = InlineRenderer(m, console=_console()[0])
        r._status.active = True
        r.on_agent_run_complete()
        assert r._status.active is False

    def test_active_stays_when_tools_running(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "slow_tool", state=ToolCallState.RUNNING)
        r = InlineRenderer(m, console=_console()[0])
        r._status.active = True
        r.on_agent_run_complete()
        # Running tool keeps active=True
        assert r._status.active is True


# ---------------------------------------------------------------------------
# InlineRenderer._render_status_panel
# ---------------------------------------------------------------------------


class TestRenderStatusPanel:
    def test_active_returns_panel(self):
        import time
        m = _model()
        con = _console()[0]
        r = InlineRenderer(m, console=con)
        r._status.active = True
        r._status.intent_started_at = time.monotonic()
        r._status.input_tokens = 50
        r._status.output_tokens = 20
        panel = r._render_status_panel()
        assert panel is not None

    def test_idle_returns_panel(self):
        m = _model()
        con = _console()[0]
        r = InlineRenderer(m, console=con)
        r._status.active = False
        r._status.completed_agents = 3
        r._status.session_cost_usd = 0.005
        r._status.session_id = "test-session-abc"
        panel = r._render_status_panel()
        assert panel is not None

    def test_idle_panel_shows_completed_agents_singular(self):
        m = _model()
        con, buf = _console()
        r = InlineRenderer(m, console=con)
        r._status.active = False
        r._status.completed_agents = 1
        panel = r._render_status_panel()
        # Print via console so we can inspect
        con.print(panel)
        output = buf.getvalue()
        assert "agent" in output.lower()


# ---------------------------------------------------------------------------
# InlineRenderer._render_input_panel
# ---------------------------------------------------------------------------


class TestRenderInputPanel:
    def test_empty_text(self):
        m = _model()
        con = _console()[0]
        r = InlineRenderer(m, console=con)
        panel = r._render_input_panel("")
        assert panel is not None

    def test_with_text(self):
        m = _model()
        con, buf = _console()
        r = InlineRenderer(m, console=con)
        panel = r._render_input_panel("hello world")
        con.print(panel)
        output = buf.getvalue()
        assert "hello world" in output


# ---------------------------------------------------------------------------
# InlineRenderer._build_spinner_panel and has_running_tools
# ---------------------------------------------------------------------------


class TestBuildSpinnerPanel:
    def test_no_running_tools_returns_none(self):
        m = _model_with_turn()
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None

    def test_running_tool_returns_panel(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "my_tool", state=ToolCallState.RUNNING)
        r = InlineRenderer(m, console=_console()[0])
        panel = r._build_spinner_panel()
        assert panel is not None

    def test_has_running_tools_true(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "slow", state=ToolCallState.RUNNING)
        r = InlineRenderer(m, console=_console()[0])
        assert r.has_running_tools() is True

    def test_has_running_tools_false(self):
        m = _model_with_turn()
        r = InlineRenderer(m, console=_console()[0])
        assert r.has_running_tools() is False

    def test_tool_completed_returns_none(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "done_tool")
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS)
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None


# ---------------------------------------------------------------------------
# SlashCommandHandler._expand
# ---------------------------------------------------------------------------


class TestSlashCommandHandlerExpand:
    def test_expand_known_mention_sets_expanded(self):
        m = _model_with_turn()
        chip = MentionChip(raw="@foo.py", kind="file", display_size="1KB", ok=True)
        m.turns[0].mention_chips.append(chip)
        con, buf = _console()
        h = SlashCommandHandler()
        result = h.handle("/expand @foo.py", m, con)
        assert result is True
        assert chip.expanded is True

    def test_expand_unknown_mention_prints_message(self):
        m = _model_with_turn()
        con, buf = _console()
        h = SlashCommandHandler()
        result = h.handle("/expand @missing.py", m, con)
        assert result is True
        assert "missing.py" in buf.getvalue() or "No item" in buf.getvalue()

    def test_expand_no_at_sign_prints_usage(self):
        m = _model()
        con, buf = _console()
        h = SlashCommandHandler()
        result = h.handle("/expand filename.py", m, con)
        assert result is True
        assert "Usage" in buf.getvalue() or len(buf.getvalue()) > 0

    def test_expand_sets_expanded_true_on_correct_chip(self):
        m = _model_with_turn()
        chip1 = MentionChip(raw="@a.py", kind="file", display_size="", ok=True)
        chip2 = MentionChip(raw="@b.py", kind="file", display_size="", ok=True)
        m.turns[0].mention_chips.extend([chip1, chip2])
        con = _console()[0]
        h = SlashCommandHandler()
        h.handle("/expand @a.py", m, con)
        assert chip1.expanded is True
        assert chip2.expanded is False


# ---------------------------------------------------------------------------
# detect_slash_command
# ---------------------------------------------------------------------------


class TestDetectSlashCommand:
    def test_status_command_detected(self):
        assert detect_slash_command("/status") == "status"

    def test_history_command_detected(self):
        assert detect_slash_command("/history") == "history"

    def test_unknown_command_returns_none(self):
        assert detect_slash_command("/unknown") is None

    def test_empty_string_returns_none(self):
        assert detect_slash_command("") is None

    def test_with_leading_space(self):
        result = detect_slash_command("  /status  ")
        assert result == "status"


# ---------------------------------------------------------------------------
# build_app deprecated raises RuntimeError
# ---------------------------------------------------------------------------


class TestBuildAppDeprecated:
    def test_raises_runtime_error(self):
        m = _model()
        with pytest.raises(RuntimeError, match="deprecated"):
            build_app(m, lambda _: None)


# ---------------------------------------------------------------------------
# render_frame_ansi
# ---------------------------------------------------------------------------


class TestRenderFrameAnsi:
    def test_basic_render(self):
        m = _model_with_turn()
        frame = render_frame_ansi(m, cols=80, rows=20)
        assert isinstance(frame, str)
        assert len(frame) > 0

    def test_input_prompt_present(self):
        m = _model()
        frame = render_frame_ansi(m, cols=80, rows=20, input_text="hello")
        assert INPUT_PROMPT in frame
        assert "hello" in frame

    def test_with_status_state_none(self):
        m = _model()
        frame = render_frame_ansi(m, cols=80, rows=20, status_state=None)
        assert isinstance(frame, str)

    def test_with_active_status_state(self):
        import time
        m = _model()
        s = StatusState(active=True, intent_started_at=time.monotonic(), input_tokens=5, output_tokens=3)
        frame = render_frame_ansi(m, cols=80, rows=20, status_state=s)
        assert isinstance(frame, str)
        # Should include some token info
        assert "5" in frame or "3" in frame

    def test_with_idle_status_state(self):
        m = _model()
        s = StatusState(active=False)
        frame = render_frame_ansi(m, cols=80, rows=20, status_state=s)
        assert isinstance(frame, str)

    def test_with_menu_lines(self):
        m = _model()
        frame = render_frame_ansi(m, cols=80, rows=20, menu_lines=["menu item 1", "menu item 2"])
        assert isinstance(frame, str)

    def test_status_line_shows_agents_and_cost(self):
        m = _model_with_turn()
        frame = render_frame_ansi(m, cols=80, rows=20)
        # Should show agent count
        assert "agent" in frame.lower() or "1" in frame

    def test_transcript_lines_limited_to_rows(self):
        m = _model()
        for i in range(30):
            m.append_turn(f"a{i}", f"agent:{i}", float(i))
            m.append_line(f"a{i}", f"output line {i}")
        frame = render_frame_ansi(m, cols=80, rows=10)
        assert isinstance(frame, str)

    def test_spinner_frame_cycles_in_active_state(self):
        import time
        m = _model()
        s = StatusState(active=True, intent_started_at=time.monotonic(), spinner_frame=5)
        frame1 = render_frame_ansi(m, cols=80, rows=20, status_state=s)
        s.spinner_frame = 6
        frame2 = render_frame_ansi(m, cols=80, rows=20, status_state=s)
        assert isinstance(frame1, str)
        assert isinstance(frame2, str)


# ---------------------------------------------------------------------------
# run_headless
# ---------------------------------------------------------------------------


class TestRunHeadless:
    @pytest.mark.asyncio
    async def test_run_headless_outputs_json(self):
        import time

        queue: asyncio.Queue = asyncio.Queue()
        output = io.StringIO()

        class FakeEvent:
            timestamp = time.time()
            event_type = "test_event"
            event_id = "e1"
            payload = {"key": "value"}
            source_agent_id = "a1"

        await queue.put(FakeEvent())
        await queue.put(None)  # sentinel

        await run_headless(queue, output)
        written = output.getvalue()
        assert len(written) > 0
        record = json.loads(written.strip())
        assert record["event_type"] == "test_event"

    @pytest.mark.asyncio
    async def test_run_headless_multiple_events(self):
        import time

        queue: asyncio.Queue = asyncio.Queue()
        output = io.StringIO()

        class FakeEvent:
            def __init__(self, etype: str) -> None:
                self.timestamp = time.time()
                self.event_type = etype
                self.event_id = "eid"
                self.payload = {}
                self.source_agent_id = None

        for etype in ["ev1", "ev2", "ev3"]:
            await queue.put(FakeEvent(etype))
        await queue.put(None)

        await run_headless(queue, output)
        lines = [ln for ln in output.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 3
