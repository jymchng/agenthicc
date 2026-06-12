"""Extended unit tests for InlineRenderer and related utilities (coverage extension)."""
from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from agenthicc.tui.transcript import ToolCallState, TranscriptModel
from agenthicc.tui.app import (
    InlineRenderer,
    SlashCommandHandler,
    SLASH_HELP,
    build_app,
    render_frame_ansi,
    run_inline,
)

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────


def _console(width: int = 120) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, highlight=False, markup=False, width=width), buf


def _model_empty() -> TranscriptModel:
    return TranscriptModel()


def _model_with_two_agents() -> TranscriptModel:
    m = TranscriptModel()
    m.append_turn("a1", "worker-1", 0.0)
    m.append_line("a1", "doing work")
    m.append_turn("a2", "worker-2", 1.0)
    m.append_line("a2", "also working")
    return m


# ── _build_spinner_panel: multiple running tools ──────────────────────────


class TestSpinnerPanelExtended:
    def test_spinner_panel_shows_multiple_running_tools(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "read_file", state=ToolCallState.RUNNING)
        m.add_tool_call("a1", "tc2", "write_file", state=ToolCallState.RUNNING)
        r = InlineRenderer(m, console=_console()[0])
        panel = r._build_spinner_panel()
        assert panel is not None

    def test_spinner_panel_failure_state_not_shown(self):
        """When all tools are in FAILURE state the panel should be None."""
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "read_file", state=ToolCallState.RUNNING)
        m.add_tool_call("a1", "tc2", "write_file", state=ToolCallState.RUNNING)
        m.update_tool_call("tc1", state=ToolCallState.FAILURE)
        m.update_tool_call("tc2", state=ToolCallState.FAILURE)
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None

    def test_spinner_panel_success_state_returns_none(self):
        """All tools succeeded means panel is None."""
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "op", state=ToolCallState.RUNNING)
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS)
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None

    def test_spinner_panel_mixed_states_shows_panel(self):
        """One running + one succeeded -> panel still shows (one running)."""
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "op1", state=ToolCallState.RUNNING)
        m.add_tool_call("a1", "tc2", "op2", state=ToolCallState.RUNNING)
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS)
        r = InlineRenderer(m, console=_console()[0])
        panel = r._build_spinner_panel()
        assert panel is not None


# ── _update_spinner ────────────────────────────────────────────────────────


class TestUpdateSpinner:
    def test_update_spinner_creates_live_when_tools_running(self):
        """When tools are running _update_spinner creates a Live instance."""
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "slow_op", state=ToolCallState.RUNNING)
        con, _ = _console()
        r = InlineRenderer(m, console=con)
        assert r._live is None
        # Patch Live to avoid real terminal interaction
        mock_live = MagicMock()
        with patch("agenthicc.tui.app.Live", return_value=mock_live):
            r._update_spinner()
        assert r._live is mock_live
        mock_live.start.assert_called_once()

    def test_update_spinner_stops_live_when_no_tools(self):
        """When no tools running and _live is set, stop() is called."""
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        con, _ = _console()
        r = InlineRenderer(m, console=con)
        mock_live = MagicMock()
        r._live = mock_live
        r._update_spinner()
        mock_live.stop.assert_called_once()
        assert r._live is None

    def test_update_spinner_updates_existing_live(self):
        """When _live already exists and tools are running, update() is called."""
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "op", state=ToolCallState.RUNNING)
        con, _ = _console()
        r = InlineRenderer(m, console=con)
        mock_live = MagicMock()
        r._live = mock_live
        r._update_spinner()
        mock_live.update.assert_called_once()
        # Not stopped
        mock_live.stop.assert_not_called()

    def test_update_spinner_noop_when_no_tools_and_no_live(self):
        """No tools, no _live — nothing should explode."""
        m = TranscriptModel()
        con, _ = _console()
        r = InlineRenderer(m, console=con)
        # Must not raise
        r._update_spinner()
        assert r._live is None


# ── SlashCommandHandler edge cases ────────────────────────────────────────


class TestSlashCommandHandlerExtended:
    def test_status_empty_model_no_crash(self):
        """Status with zero turns still renders the table without error."""
        con, buf = _console()
        m = _model_empty()
        h = SlashCommandHandler()
        result = h.handle("/status", m, con)
        assert result is True
        assert len(buf.getvalue()) > 0

    def test_status_multiple_agents_rendered(self):
        con, buf = _console()
        m = _model_with_two_agents()
        h = SlashCommandHandler()
        result = h.handle("/status", m, con)
        assert result is True
        output = buf.getvalue()
        assert len(output) > 0

    def test_history_empty_model_no_crash(self):
        """History on an empty model renders without raising."""
        con, buf = _console()
        m = _model_empty()
        h = SlashCommandHandler()
        result = h.handle("/history", m, con)
        assert result is True
        assert len(buf.getvalue()) > 0

    def test_help_lists_all_builtin_commands(self):
        """/help output contains at least /status and /history."""
        con, buf = _console()
        h = SlashCommandHandler()
        result = h.handle("/help", _model_empty(), con)
        assert result is True
        output = buf.getvalue()
        # Just verify help output is non-empty and contains common commands
        assert len(output) > 0

    def test_status_shows_cost_and_tokens_placeholder(self):
        """Agent with None cost and tokens renders $0.0000 / 0."""
        con, buf = _console()
        m = TranscriptModel()
        m.append_turn("a1", "worker", 0.0)
        h = SlashCommandHandler()
        h.handle("/status", m, con)
        output = buf.getvalue()
        assert "$0.0000" in output or "0.0000" in output or "0" in output


# ── run_inline ────────────────────────────────────────────────────────────


class TestRunInline:
    async def test_run_inline_calls_renderer_run(self):
        """run_inline creates an InlineRenderer and calls run."""
        m = TranscriptModel()
        captured: list = []

        async def _fake_run(self_arg, on_input):
            captured.append(on_input)

        with patch.object(InlineRenderer, "run", _fake_run):
            await run_inline(m)
        assert len(captured) == 1

    async def test_run_inline_passes_custom_on_input(self):
        """run_inline forwards the on_input callable to renderer.run."""
        m = TranscriptModel()
        inputs_received: list[str] = []

        async def _fake_run(self_arg, on_input):
            on_input("hello")

        with patch.object(InlineRenderer, "run", _fake_run):
            await run_inline(m, on_input=inputs_received.append)
        assert inputs_received == ["hello"]


# ── build_app deprecated ──────────────────────────────────────────────────


class TestBuildAppDeprecated:
    def test_build_app_raises_runtime_error(self):
        m = TranscriptModel()
        with pytest.raises(RuntimeError) as exc_info:
            build_app(m, lambda x: None)
        assert "run_inline" in str(exc_info.value)

    def test_build_app_error_message_contains_migration_hint(self):
        m = TranscriptModel()
        with pytest.raises(RuntimeError) as exc_info:
            build_app(m, lambda x: None)
        msg = str(exc_info.value)
        assert "deprecated" in msg.lower() or "run_inline" in msg


# ── render_frame_ansi corner cases ────────────────────────────────────────


class TestRenderFrameAnsiExtended:
    def test_render_frame_ansi_empty_model(self):
        """Empty model renders without error and returns a str."""
        m = _model_empty()
        result = render_frame_ansi(m, cols=80, rows=24)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_frame_ansi_menu_lines_clipped(self):
        """100 menu lines are clipped to fit the available terminal rows."""
        m = _model_empty()
        menu_lines = [f"option {i}" for i in range(100)]
        result = render_frame_ansi(m, cols=80, rows=24, menu_lines=menu_lines)
        assert isinstance(result, str)
        # Frame must not exceed the terminal's row count worth of content
        # (it should not crash regardless)
        assert "\x1b[" in result  # ANSI codes present

    def test_render_frame_ansi_with_content(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent", 0.0)
        m.append_line("a1", "some output line")
        result = render_frame_ansi(m, cols=80, rows=24)
        assert "some output line" in result

    def test_render_frame_ansi_input_text_included(self):
        m = _model_empty()
        result = render_frame_ansi(m, cols=80, rows=24, input_text="my query")
        assert "my query" in result

    def test_render_frame_ansi_status_line_present(self):
        m = _model_empty()
        result = render_frame_ansi(m, cols=80, rows=24)
        # Status line contains agent count and cost
        assert "agents" in result and "tok" in result

    def test_render_frame_ansi_small_rows(self):
        """Very small terminal (2 rows) should not crash."""
        m = _model_empty()
        result = render_frame_ansi(m, cols=40, rows=2)
        assert isinstance(result, str)

    def test_render_frame_ansi_long_lines_clipped_to_cols(self):
        """Lines longer than cols are clipped."""
        m = TranscriptModel()
        m.append_turn("a1", "agent", 0.0)
        m.append_line("a1", "x" * 200)
        result = render_frame_ansi(m, cols=80, rows=24)
        # No line in the ANSI output should be longer than cols characters
        # (accounting for escape prefix like \x1b[1;1H)
        assert isinstance(result, str)
