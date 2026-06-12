"""Unit tests for TranscriptModel and diff_lines (PRD-06)."""

from __future__ import annotations

import pytest

from agenthicc.tui.transcript import (
    SPINNER_FRAMES,
    AgentTurnEntry,
    ToolCallState,
    TranscriptModel,
    diff_lines,
)

pytestmark = pytest.mark.unit


def _model_with_turn(agent_id="a1", agent_name="agent:test"):
    m = TranscriptModel()
    m.append_turn(agent_id=agent_id, agent_name=agent_name, timestamp=0.0)
    return m


class TestTranscriptModelRender:
    def test_render_contains_agent_header(self):
        m = _model_with_turn()
        lines = m.render()
        assert any("●" in line for line in lines)

    def test_render_contains_agent_name(self):
        m = _model_with_turn(agent_name="agent:worker")
        assert any("agent:worker" in line for line in m.render())

    def test_append_line_appears_in_render(self):
        m = _model_with_turn()
        m.append_line("a1", "hello world output")
        assert any("hello world output" in line for line in m.render())

    def test_add_tool_call_appears_in_render(self):
        m = _model_with_turn()
        m.add_tool_call("a1", tool_use_id="tc1", name="read_file")
        lines = m.render()
        assert any("read_file" in line for line in lines)

    def test_update_tool_success_shows_checkmark(self):
        m = _model_with_turn()
        m.add_tool_call("a1", tool_use_id="tc1", name="write_file")
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS, duration_ms=42.0)
        lines = m.render()
        assert any("✓" in line for line in lines)

    def test_update_tool_failure_shows_cross(self):
        m = _model_with_turn()
        m.add_tool_call("a1", tool_use_id="tc1", name="run_tests")
        m.update_tool_call("tc1", state=ToolCallState.FAILURE, error="3 failures")
        lines = m.render()
        assert any("✗" in line for line in lines)

    def test_advance_spinner_cycles(self):
        m = _model_with_turn()
        m.add_tool_call("a1", tool_use_id="tc1", name="slow_tool")
        for _ in range(len(SPINNER_FRAMES) + 2):
            m.advance_spinner()
        # Just ensure no exception is raised and model remains consistent
        assert m.render() is not None

    def test_has_running_tools_true(self):
        m = _model_with_turn()
        m.add_tool_call("a1", tool_use_id="tc1", name="slow")
        # newly added tool is RUNNING
        assert m.has_running_tools()

    def test_has_running_tools_false_after_complete(self):
        m = _model_with_turn()
        m.add_tool_call("a1", tool_use_id="tc1", name="fast")
        m.update_tool_call("tc1", state=ToolCallState.SUCCESS)
        assert not m.has_running_tools()

    def test_total_cost_accumulates(self):
        m = TranscriptModel()
        t1 = m.append_turn("a1", "agent:a", 0.0)
        t1.cost_usd = 0.001
        t2 = m.append_turn("a2", "agent:b", 1.0)
        t2.cost_usd = 0.002
        assert abs(m.total_cost_usd - 0.003) < 1e-9

    def test_total_tokens_accumulates(self):
        m = TranscriptModel()
        t1 = m.append_turn("a1", "agent:a", 0.0)
        t1.tokens = 100
        t2 = m.append_turn("a2", "agent:b", 1.0)
        t2.tokens = 200
        assert m.total_tokens == 300

    def test_multiple_turns_all_in_render(self):
        m = TranscriptModel()
        for i in range(3):
            m.append_turn(f"a{i}", f"agent:{i}", float(i))
            m.append_line(f"a{i}", f"output {i}")
        rendered = "\n".join(m.render())
        for i in range(3):
            assert f"output {i}" in rendered

    def test_render_has_both_turns(self):
        m = TranscriptModel()
        m.append_turn("a1", "first", 0.0)
        m.append_turn("a2", "second", 1.0)
        lines = m.render()
        assert len(lines) >= 2  # both turns rendered

    def test_tool_call_running_shows_spinner(self):
        entry_obj = None
        m = _model_with_turn()
        entry_obj = m.add_tool_call("a1", tool_use_id="tc1", name="spinner_tool", state=ToolCallState.RUNNING)
        rendered = entry_obj.render()
        # Should contain a spinner frame character
        assert any(frame in rendered for frame in SPINNER_FRAMES)

    def test_tool_call_pending_shows_dot(self):
        m = _model_with_turn()
        entry_obj = m.add_tool_call("a1", tool_use_id="tc1", name="pending_tool", state=ToolCallState.PENDING)
        rendered = entry_obj.render()
        assert "." in rendered


class TestDiffLines:
    def test_identical_all_kept(self):
        lines = ["a", "b", "c"]
        ops = {op for op, _ in diff_lines(lines, lines)}
        assert ops == {"keep"}

    def test_added_line_detected(self):
        result = diff_lines(["a"], ["a", "b"])
        assert any(op == "add" and line == "b" for op, line in result)

    def test_removed_line_detected(self):
        result = list(diff_lines(["a", "b"], ["a"]))
        assert any(op == "remove" and line == "b" for op, line in result)

    def test_empty_to_content(self):
        ops = [op for op, _ in diff_lines([], ["x", "y"])]
        assert all(op == "add" for op in ops)

    def test_content_to_empty(self):
        ops = [op for op, _ in diff_lines(["x", "y"], [])]
        assert all(op == "remove" for op in ops)

    def test_keep_unchanged_line(self):
        result = diff_lines(["same"], ["same"])
        assert ("keep", "same") in result

    def test_mixed_ops(self):
        old = ["keep", "remove"]
        new = ["keep", "add"]
        result = diff_lines(old, new)
        ops = {op for op, _ in result}
        assert "keep" in ops
        assert "add" in ops
        assert "remove" in ops
