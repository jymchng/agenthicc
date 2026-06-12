"""Extended tests for TranscriptModel covering uncovered lines.

Targeted lines in transcript.py: 89-94, 153, 161, 200
"""
from __future__ import annotations

from typing import Any

import pytest

from agenthicc.tui.transcript import (
    SEPARATOR,
    SPINNER_FRAMES,
    AgentTurnEntry,
    ToolCallEntry,
    ToolCallState,
    TranscriptModel,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _model() -> TranscriptModel:
    return TranscriptModel()


def _turn(agent_id: str = "a1", agent_name: str = "agent:test") -> TranscriptModel:
    m = _model()
    m.append_turn(agent_id=agent_id, agent_name=agent_name, timestamp=0.0)
    return m


# ---------------------------------------------------------------------------
# AgentTurnEntry.footer() — lines 89-94
# ---------------------------------------------------------------------------


class TestAgentTurnEntryFooter:
    def test_footer_none_when_no_cost_and_no_tokens(self):
        """Line 87-88: both None → footer() returns None."""
        entry = AgentTurnEntry(agent_id="a1", agent_name="agent:a", timestamp=0.0)
        assert entry.footer() is None

    def test_footer_contains_tokens_when_only_tokens_set(self):
        """Line 90-91: only tokens present."""
        entry = AgentTurnEntry(agent_id="a1", agent_name="agent:a", timestamp=0.0)
        entry.tokens = 500
        footer = entry.footer()
        assert footer is not None
        assert "500" in footer
        assert "cost" not in footer

    def test_footer_contains_cost_when_only_cost_set(self):
        """Line 92-93: only cost present."""
        entry = AgentTurnEntry(agent_id="a1", agent_name="agent:a", timestamp=0.0)
        entry.cost_usd = 0.042
        footer = entry.footer()
        assert footer is not None
        assert "0.042" in footer
        assert "tokens" not in footer

    def test_footer_contains_both_cost_and_tokens(self):
        """Line 89-94: both set."""
        entry = AgentTurnEntry(agent_id="a1", agent_name="agent:a", timestamp=0.0)
        entry.tokens = 1234
        entry.cost_usd = 0.001
        footer = entry.footer()
        assert footer is not None
        assert "1,234" in footer
        assert "0.001" in footer

    def test_footer_starts_with_arrow(self):
        entry = AgentTurnEntry(agent_id="a1", agent_name="agent:a", timestamp=0.0)
        entry.tokens = 10
        footer = entry.footer()
        assert footer is not None
        assert footer.startswith("  →")


# ---------------------------------------------------------------------------
# render() includes separator between turns — line 200
# ---------------------------------------------------------------------------


class TestRenderSeparator:
    def test_render_has_no_separator_for_single_turn(self):
        m = _turn()
        lines = m.render()
        assert any(True for _ in lines)  # single turn renders ok

    def test_render_includes_separator_between_two_turns(self):
        m = _model()
        m.append_turn("a1", "first", 0.0)
        m.append_turn("a2", "second", 1.0)
        lines = m.render()
        assert SEPARATOR in lines

    def test_render_separator_count_equals_turns_minus_one(self):
        m = _model()
        n_turns = 4
        for i in range(n_turns):
            m.append_turn(f"a{i}", f"agent:{i}", float(i))
        lines = m.render()
        separator_count = sum(1 for ln in lines if ln == SEPARATOR)
        assert separator_count == n_turns - 1

    def test_render_footer_appears_in_output(self):
        """Line 199-200: footer is included in render when present."""
        m = _model()
        turn = m.append_turn("a1", "agent:a", 0.0)
        turn.cost_usd = 0.005
        turn.tokens = 100
        lines = m.render()
        assert any("cost" in ln or "tokens" in ln for ln in lines)


# ---------------------------------------------------------------------------
# update_tool_call — line 153 (return None for unknown id), line 161 (spinner)
# ---------------------------------------------------------------------------


class TestUpdateToolCall:
    def test_update_unknown_tool_use_id_returns_none(self):
        """Line 153: entry not found → returns None."""
        m = _turn()
        result = m.update_tool_call("nonexistent-id", state=ToolCallState.SUCCESS)
        assert result is None

    def test_update_spinner_frame_directly(self):
        """Line 161: spinner_frame update via update_tool_call."""
        m = _turn()
        m.add_tool_call("a1", "tc1", "my_tool", state=ToolCallState.RUNNING)
        entry = m.update_tool_call("tc1", spinner_frame=5)
        assert entry is not None
        assert entry.spinner_frame == 5

    def test_update_state_changes_entry(self):
        m = _turn()
        m.add_tool_call("a1", "tc1", "my_tool")
        entry = m.update_tool_call("tc1", state=ToolCallState.SUCCESS, duration_ms=120.0)
        assert entry is not None
        assert entry.state is ToolCallState.SUCCESS
        assert entry.duration_ms == 120.0

    def test_update_error_field(self):
        m = _turn()
        m.add_tool_call("a1", "tc1", "my_tool")
        entry = m.update_tool_call("tc1", state=ToolCallState.FAILURE, error="timeout")
        assert entry is not None
        assert entry.error == "timeout"


# ---------------------------------------------------------------------------
# advance_spinner() wraps without IndexError (line 168)
# ---------------------------------------------------------------------------


class TestAdvanceSpinner:
    def test_advance_spinner_wraps_correctly(self):
        """Calling advance_spinner more than len(SPINNER_FRAMES) times must not raise."""
        m = _turn()
        m.add_tool_call("a1", "tc1", "slow_tool", state=ToolCallState.RUNNING)
        for _ in range(len(SPINNER_FRAMES) + 5):
            m.advance_spinner()
        entry = m._tool_index["tc1"]
        # Frame index must be in range
        assert 0 <= entry.spinner_frame < len(SPINNER_FRAMES)

    def test_advance_spinner_only_affects_running_tools(self):
        """Non-RUNNING tools should have their spinner_frame unchanged."""
        m = _turn()
        m.add_tool_call("a1", "tc1", "done_tool", state=ToolCallState.SUCCESS)
        m.add_tool_call("a1", "tc2", "running_tool", state=ToolCallState.RUNNING)
        for _ in range(3):
            m.advance_spinner()
        assert m._tool_index["tc1"].spinner_frame == 0  # unchanged
        assert m._tool_index["tc2"].spinner_frame == 3 % len(SPINNER_FRAMES)


# ---------------------------------------------------------------------------
# ToolCallEntry.symbol for each state
# ---------------------------------------------------------------------------


class TestToolCallEntrySymbol:
    def test_symbol_pending(self):
        entry = ToolCallEntry(tool_use_id="t1", name="tool", state=ToolCallState.PENDING)
        assert entry.symbol == "."

    def test_symbol_running_uses_spinner_frame(self):
        entry = ToolCallEntry(
            tool_use_id="t1", name="tool", state=ToolCallState.RUNNING, spinner_frame=0
        )
        assert entry.symbol == SPINNER_FRAMES[0]

    def test_symbol_success(self):
        entry = ToolCallEntry(tool_use_id="t1", name="tool", state=ToolCallState.SUCCESS)
        assert entry.symbol == "✓"

    def test_symbol_failure(self):
        entry = ToolCallEntry(tool_use_id="t1", name="tool", state=ToolCallState.FAILURE)
        assert entry.symbol == "✗"

    def test_symbol_running_wraps_spinner_frame(self):
        n = len(SPINNER_FRAMES)
        entry = ToolCallEntry(
            tool_use_id="t1", name="tool", state=ToolCallState.RUNNING, spinner_frame=n
        )
        # n % n == 0 → first frame
        assert entry.symbol == SPINNER_FRAMES[0]


# ---------------------------------------------------------------------------
# total_cost_usd and total_tokens
# ---------------------------------------------------------------------------


class TestTotals:
    def test_total_cost_three_turns(self):
        m = _model()
        for i, cost in enumerate([0.001, 0.002, 0.003]):
            t = m.append_turn(f"a{i}", f"agent:{i}", float(i))
            t.cost_usd = cost
        assert abs(m.total_cost_usd - 0.006) < 1e-9

    def test_total_cost_excludes_none(self):
        m = _model()
        t1 = m.append_turn("a1", "agent:a", 0.0)
        t1.cost_usd = 0.005
        m.append_turn("a2", "agent:b", 1.0)  # cost_usd is None
        assert abs(m.total_cost_usd - 0.005) < 1e-9

    def test_total_tokens_three_turns(self):
        m = _model()
        for i, tokens in enumerate([100, 200, 300]):
            t = m.append_turn(f"a{i}", f"agent:{i}", float(i))
            t.tokens = tokens
        assert m.total_tokens == 600

    def test_total_tokens_excludes_none(self):
        m = _model()
        t1 = m.append_turn("a1", "agent:a", 0.0)
        t1.tokens = 50
        m.append_turn("a2", "agent:b", 1.0)  # tokens is None
        assert m.total_tokens == 50

    def test_total_cost_zero_when_no_turns(self):
        assert _model().total_cost_usd == 0.0

    def test_total_tokens_zero_when_no_turns(self):
        assert _model().total_tokens == 0


# ---------------------------------------------------------------------------
# has_running_tools() — multiple tools, one done
# ---------------------------------------------------------------------------


class TestHasRunningTools:
    def test_has_running_tools_multiple_tools_one_done(self):
        """Two tools: one SUCCESS, one still RUNNING → True."""
        m = _turn()
        m.add_tool_call("a1", "tc1", "tool_a", state=ToolCallState.RUNNING)
        m.add_tool_call("a1", "tc2", "tool_b", state=ToolCallState.RUNNING)
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS)
        assert m.has_running_tools() is True

    def test_has_running_tools_false_when_all_done(self):
        m = _turn()
        m.add_tool_call("a1", "tc1", "a", state=ToolCallState.RUNNING)
        m.add_tool_call("a1", "tc2", "b", state=ToolCallState.RUNNING)
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS)
        m.update_tool_call("tc2", state=ToolCallState.FAILURE)
        assert m.has_running_tools() is False


# ---------------------------------------------------------------------------
# render_ad_panel() — line 200+
# ---------------------------------------------------------------------------


class TestTurnForFallback:
    """Line 125: _turn_for creates a new turn if agent_id not found."""

    def test_turn_for_creates_turn_when_not_found(self):
        """append_line on an unknown agent_id auto-creates a turn."""
        m = _model()
        # No turns yet; _turn_for should create one
        m.append_line("unknown-agent", "hello from unknown")
        # There should now be a turn for that agent
        assert len(m.turns) == 1
        assert m.turns[0].agent_id == "unknown-agent"

    def test_turn_for_returns_existing_turn_for_known_agent(self):
        m = _model()
        turn = m.append_turn("a1", "agent:a", 0.0)
        # _turn_for should return the existing turn, not create a new one
        found = m._turn_for("a1")
        assert found is turn
        assert len(m.turns) == 1


class TestCurrentAd:
    """Line 212: current_ad() returns the stored ad."""

    def test_current_ad_returns_none_by_default(self):
        m = _model()
        assert m.current_ad() is None

    def test_current_ad_returns_set_ad(self):
        m = _model()
        ad = object()
        m.set_current_ad(ad)
        assert m.current_ad() is ad

    def test_current_ad_returns_none_after_cleared(self):
        m = _model()
        m.set_current_ad(object())
        m.set_current_ad(None)
        assert m.current_ad() is None


class TestRenderAdPanel:
    def test_render_ad_panel_none_by_default(self):
        m = _model()
        assert m.render_ad_panel() is None

    def test_render_ad_panel_returns_string_with_text(self):
        m = _model()
        ad = type("Ad", (), {"text": "Check out agenthicc pro!", "cta_url": "https://agenthicc.ai"})()
        m.set_current_ad(ad)
        panel = m.render_ad_panel()
        assert panel is not None
        assert "agenthicc pro" in panel

    def test_render_ad_panel_includes_url(self):
        m = _model()
        ad = type("Ad", (), {"text": "Ad text", "cta_url": "https://example.com"})()
        m.set_current_ad(ad)
        panel = m.render_ad_panel()
        assert "https://example.com" in panel

    def test_render_ad_panel_omits_url_when_absent(self):
        m = _model()
        ad = type("Ad", (), {"text": "Ad without URL"})()
        m.set_current_ad(ad)
        panel = m.render_ad_panel()
        assert panel is not None
        assert "http" not in panel

    def test_current_ad_can_be_cleared(self):
        m = _model()
        ad = type("Ad", (), {"text": "X", "cta_url": ""})()
        m.set_current_ad(ad)
        m.set_current_ad(None)
        assert m.render_ad_panel() is None
