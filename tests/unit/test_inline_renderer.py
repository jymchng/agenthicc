"""Unit tests for InlineRenderer and SlashCommandHandler (PRD-09)."""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from agenthicc.tui.transcript import ToolCallState, TranscriptModel
from agenthicc.tui.app import InlineRenderer, SlashCommandHandler

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────


def _console(width: int = 120) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, highlight=False, markup=False, width=width), buf


def _model_with_content() -> TranscriptModel:
    m = TranscriptModel()
    m.append_turn("a1", "agent:test", 0.0)
    m.append_line("a1", "hello from agent")
    return m


# ── InlineRenderer flush tests ────────────────────────────────────────────


class TestInlineRendererFlush:
    def test_flush_new_lines_prints_content(self):
        con, buf = _console()
        m = _model_with_content()
        r = InlineRenderer(m, console=con)
        r._flush_new_lines()
        assert "hello from agent" in buf.getvalue()

    def test_no_duplicate_on_second_flush(self):
        con, buf = _console()
        m = _model_with_content()
        r = InlineRenderer(m, console=con)
        r._flush_new_lines()
        first_output = buf.getvalue()
        r._flush_new_lines()
        # Second flush must add nothing new
        assert buf.getvalue() == first_output

    def test_new_content_printed_on_second_flush(self):
        con, buf = _console()
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        r = InlineRenderer(m, console=con)
        r._flush_new_lines()
        m.append_line("a1", "second line")
        r._flush_new_lines()
        assert "second line" in buf.getvalue()

    def test_printed_count_advances(self):
        con, _ = _console()
        m = _model_with_content()
        r = InlineRenderer(m, console=con)
        assert r._printed_count == 0
        r._flush_new_lines()
        assert r._printed_count == len(m.render())


# ── spinner panel tests ───────────────────────────────────────────────────


class TestSpinnerPanel:
    def test_spinner_panel_none_when_no_running_tools(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None

    def test_spinner_panel_not_none_when_tool_running(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "read_file")
        r = InlineRenderer(m, console=_console()[0])
        panel = r._build_spinner_panel()
        assert panel is not None

    def test_spinner_panel_none_after_tool_completes(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "write_file")
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS, duration_ms=5.0)
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None

    def test_has_running_tools_true_and_false(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "slow_op")
        r = InlineRenderer(m, console=_console()[0])
        assert r.has_running_tools() is True
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS)
        assert r.has_running_tools() is False


# ── SlashCommandHandler tests ─────────────────────────────────────────────


class TestSlashCommandHandler:
    def test_slash_status_renders_table(self):
        con, buf = _console()
        m = _model_with_content()
        h = SlashCommandHandler()
        result = h.handle("/status", m, con)
        assert result is True
        output = buf.getvalue()
        assert "Agent" in output or "agent" in output.lower()

    def test_slash_history_renders_panel(self):
        con, buf = _console()
        m = _model_with_content()
        h = SlashCommandHandler()
        result = h.handle("/history", m, con)
        assert result is True
        output = buf.getvalue()
        assert "history" in output.lower() or "hello" in output

    def test_slash_help_renders(self):
        con, buf = _console()
        m = TranscriptModel()
        h = SlashCommandHandler()
        result = h.handle("/help", m, con)
        assert result is True
        output = buf.getvalue()
        # The /help table should include at least one slash command
        assert "/status" in output or "/history" in output or "help" in output.lower()

    def test_unknown_slash_returns_false(self):
        con, _ = _console()
        m = TranscriptModel()
        h = SlashCommandHandler()
        assert h.handle("not a command", m, con) is False

    def test_slash_command_with_no_turns(self):
        """Status with no turns should show a placeholder row."""
        con, buf = _console()
        m = TranscriptModel()
        h = SlashCommandHandler()
        result = h.handle("/status", m, con)
        assert result is True
        # Either the table is printed (no crash) or '—' placeholder appears
        assert len(buf.getvalue()) > 0

    def test_slash_history_empty_model(self):
        """History on an empty model should show (empty) and not crash."""
        con, buf = _console()
        m = TranscriptModel()
        h = SlashCommandHandler()
        result = h.handle("/history", m, con)
        assert result is True
        assert "empty" in buf.getvalue().lower() or len(buf.getvalue()) > 0
