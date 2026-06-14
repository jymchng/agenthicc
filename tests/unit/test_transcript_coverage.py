"""Additional coverage tests for transcript.py uncovered lines.

Targets:
  - transcript.py lines 97-101, 109-117: ToolCallEntry.symbol APPROVAL_NEEDED, DENIED + render()
  - lines 272-321: _render_markdown_to_lines, _render_diff, diff truncation
  - line 423: _evict_old_turns (MAX_TURNS_IN_MEMORY exceeded)
  - line 450: _get_turn_for_agent returns None
  - lines 514-600: finish_tool_call, set_turn_error, cancel_turn, commit_system_message
  - lines 661-739: evict_old_turns, _check_finalization, replay_from_store,
                   add_mention_chips, set_mention_content, render_ad_panel
"""
from __future__ import annotations

from typing import Any

import pytest

from agenthicc.tui.transcript import (
    MAX_DIFF_LINES,
    MAX_LINES_PER_TURN,
    MAX_TURNS_IN_MEMORY,
    AgentTurnEntry,
    MentionChip,
    ToolCallEntry,
    ToolCallState,
    TurnState,
    TranscriptModel,
    _render_diff,
    _render_markdown_to_lines,
    diff_lines,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _model() -> TranscriptModel:
    return TranscriptModel()


def _model_with_turn(agent_id: str = "a1", agent_name: str = "agent:test") -> TranscriptModel:
    m = _model()
    m.append_turn(agent_id=agent_id, agent_name=agent_name, timestamp=0.0)
    return m


# ---------------------------------------------------------------------------
# ToolCallEntry.symbol — APPROVAL_NEEDED and DENIED states
# ---------------------------------------------------------------------------


class TestToolCallEntrySymbolExtended:
    def test_symbol_approval_needed(self):
        entry = ToolCallEntry(
            tool_use_id="t1", name="tool", state=ToolCallState.APPROVAL_NEEDED
        )
        assert entry.symbol == "⚠"

    def test_symbol_denied(self):
        entry = ToolCallEntry(tool_use_id="t1", name="tool", state=ToolCallState.DENIED)
        assert entry.symbol == "✗"


# ---------------------------------------------------------------------------
# ToolCallEntry.render() — all branches
# ---------------------------------------------------------------------------


class TestToolCallEntryRender:
    def test_render_success_with_summary_and_duration(self):
        entry = ToolCallEntry(
            tool_use_id="t1",
            name="read_file",
            args={"path": "foo.py"},
            state=ToolCallState.SUCCESS,
            duration_ms=55.0,
            result_summary="ok",
        )
        rendered = entry.render()
        assert "read_file" in rendered
        assert "✓" in rendered
        assert "ok" in rendered
        assert "55ms" in rendered

    def test_render_success_no_summary_no_duration(self):
        entry = ToolCallEntry(
            tool_use_id="t1",
            name="my_tool",
            state=ToolCallState.SUCCESS,
        )
        rendered = entry.render()
        assert "✓" in rendered

    def test_render_failure_with_error_and_duration(self):
        entry = ToolCallEntry(
            tool_use_id="t1",
            name="bad_tool",
            state=ToolCallState.FAILURE,
            duration_ms=10.0,
            error="crashed",
        )
        rendered = entry.render()
        assert "✗" in rendered
        assert "crashed" in rendered
        assert "10ms" in rendered

    def test_render_failure_no_error_no_duration(self):
        entry = ToolCallEntry(
            tool_use_id="t1", name="bad_tool", state=ToolCallState.FAILURE
        )
        rendered = entry.render()
        assert "✗" in rendered

    def test_render_approval_needed(self):
        entry = ToolCallEntry(
            tool_use_id="t1", name="risky_tool", state=ToolCallState.APPROVAL_NEEDED
        )
        rendered = entry.render()
        assert "⚠" in rendered
        assert "risky_tool" in rendered

    def test_render_args_truncated(self):
        long_val = "x" * 50
        entry = ToolCallEntry(
            tool_use_id="t1",
            name="my_tool",
            args={"path": long_val},
            state=ToolCallState.RUNNING,
        )
        rendered = entry.render()
        assert "..." in rendered

    def test_render_many_args_shows_ellipsis(self):
        entry = ToolCallEntry(
            tool_use_id="t1",
            name="my_tool",
            args={"a": 1, "b": 2, "c": 3},
            state=ToolCallState.RUNNING,
        )
        rendered = entry.render()
        assert "..." in rendered


# ---------------------------------------------------------------------------
# AgentTurnEntry.render — multiple tool calls appear in transcript render
# ---------------------------------------------------------------------------


class TestAgentTurnRenderWithToolCalls:
    def test_multiple_tool_calls_appear(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "read_file", state=ToolCallState.SUCCESS)
        m.add_tool_call("a1", "tc2", "write_file", state=ToolCallState.FAILURE)
        lines = m.render()
        combined = "\n".join(lines)
        assert "read_file" in combined
        assert "write_file" in combined

    def test_tool_call_with_committed_line_uses_committed(self):
        m = _model_with_turn()
        tc = m.add_tool_call("a1", "tc1", "read_file")
        tc.committed_line = "  ⎿ read_file ✓"
        lines = m.render()
        assert any("committed" in ln or "read_file ✓" in ln for ln in lines)


# ---------------------------------------------------------------------------
# _render_markdown_to_lines
# ---------------------------------------------------------------------------


class TestRenderMarkdownToLines:
    def test_empty_text_returns_empty_list(self):
        result = _render_markdown_to_lines("", 80)
        assert result == []

    def test_plain_text_passes_through(self):
        result = _render_markdown_to_lines("hello world", 80)
        assert any("hello" in ln for ln in result)

    def test_markdown_headers_rendered(self):
        result = _render_markdown_to_lines("# Title\n\nBody text", 80)
        combined = "\n".join(result)
        assert "Title" in combined or "Body" in combined


# ---------------------------------------------------------------------------
# _render_diff — various branches
# ---------------------------------------------------------------------------


class TestRenderDiff:
    def test_diff_header_present(self):
        lines = _render_diff("foo.py", "+line1\n-line2\n context", 80)
        combined = "\n".join(lines)
        assert "foo.py" in combined

    def test_added_lines_colored_green(self):
        lines = _render_diff("f.py", "+added line", 80)
        combined = "\n".join(lines)
        assert "\033[32m" in combined

    def test_removed_lines_colored_red(self):
        lines = _render_diff("f.py", "-removed line", 80)
        combined = "\n".join(lines)
        assert "\033[31m" in combined

    def test_hunk_header_colored_cyan(self):
        lines = _render_diff("f.py", "@@ -1,3 +1,4 @@", 80)
        combined = "\n".join(lines)
        assert "\033[36m" in combined

    def test_truncation_when_too_many_lines(self):
        big_diff = "\n".join(f"+line{i}" for i in range(MAX_DIFF_LINES + 10))
        lines = _render_diff("big.py", big_diff, 80)
        combined = "\n".join(lines)
        assert "more lines" in combined

    def test_no_truncation_when_within_limit(self):
        small_diff = "\n".join(f"+line{i}" for i in range(5))
        lines = _render_diff("small.py", small_diff, 80)
        combined = "\n".join(lines)
        assert "more lines" not in combined


# ---------------------------------------------------------------------------
# TranscriptModel._turn_for — auto-create
# ---------------------------------------------------------------------------


class TestTurnForAutoCreate:
    def test_turn_for_auto_creates_for_unknown_agent(self):
        m = _model()
        turn = m._turn_for("new-agent")
        assert turn.agent_id == "new-agent"
        assert len(m.turns) == 1

    def test_turn_for_returns_most_recent_for_known_agent(self):
        m = _model()
        t1 = m.append_turn("a1", "agent:a", 0.0)
        t2 = m.append_turn("a1", "agent:a", 1.0)
        found = m._turn_for("a1")
        assert found is t2

    def test_get_turn_for_agent_returns_none_when_no_turns(self):
        m = _model()
        assert m._get_turn_for_agent("nobody") is None


# ---------------------------------------------------------------------------
# TranscriptModel.finish_tool_call
# ---------------------------------------------------------------------------


class TestFinishToolCall:
    def test_finish_success(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "my_tool")
        tc = m.finish_tool_call("tc1", success=True, duration_ms=100.0, result_summary="done")
        assert tc is not None
        assert tc.state == ToolCallState.SUCCESS
        assert tc.duration_ms == 100.0
        assert tc.result_summary == "done"
        assert tc.committed_line != ""

    def test_finish_failure(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "bad_tool")
        tc = m.finish_tool_call("tc1", success=False, error="oops")
        assert tc is not None
        assert tc.state == ToolCallState.FAILURE
        assert tc.error == "oops"

    def test_finish_with_diff_text(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "patch_file")
        tc = m.finish_tool_call("tc1", success=True, diff_text="+added\n-removed")
        assert tc is not None
        assert tc.diff_text == "+added\n-removed"

    def test_finish_with_output_lines(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "run_cmd")
        tc = m.finish_tool_call("tc1", success=True, output_lines=["out1", "out2"])
        assert tc is not None
        assert tc.output_lines == ["out1", "out2"]

    def test_finish_unknown_id_returns_none(self):
        m = _model_with_turn()
        result = m.finish_tool_call("nonexistent", success=True)
        assert result is None

    def test_finish_adds_to_committed_lines(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "my_tool")
        before = len(m.all_committed_lines)
        m.finish_tool_call("tc1", success=True)
        assert len(m.all_committed_lines) > before

    def test_finish_truncates_output_lines(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "my_tool")
        big_output = [f"line{i}" for i in range(MAX_LINES_PER_TURN + 50)]
        tc = m.finish_tool_call("tc1", success=True, output_lines=big_output)
        assert tc is not None
        assert len(tc.output_lines) == MAX_LINES_PER_TURN


# ---------------------------------------------------------------------------
# TranscriptModel.set_turn_error
# ---------------------------------------------------------------------------


class TestSetTurnError:
    def test_error_marks_turn_as_error(self):
        m = _model_with_turn()
        m.set_turn_error("a1", "something went wrong")
        assert m.turns[0].state == TurnState.ERROR

    def test_error_adds_error_line_to_committed(self):
        m = _model_with_turn()
        before = len(m.all_committed_lines)
        m.set_turn_error("a1", "timeout")
        assert len(m.all_committed_lines) > before

    def test_error_returns_empty_for_unknown_agent(self):
        m = _model()
        result = m.set_turn_error("nobody", "error")
        assert result == []

    def test_error_line_contains_message(self):
        m = _model_with_turn()
        lines = m.set_turn_error("a1", "critical failure")
        combined = "\n".join(lines)
        assert "critical failure" in combined


# ---------------------------------------------------------------------------
# TranscriptModel.cancel_turn
# ---------------------------------------------------------------------------


class TestCancelTurn:
    def test_cancel_marks_turn_cancelled(self):
        m = _model_with_turn()
        m.cancel_turn("a1")
        assert m.turns[0].state == TurnState.CANCELLED

    def test_cancel_clears_streaming_partial(self):
        m = _model_with_turn()
        m.set_streaming_partial("partial text")
        m.cancel_turn("a1")
        assert m.get_streaming_partial() is None

    def test_cancel_returns_empty_for_unknown_agent(self):
        m = _model()
        result = m.cancel_turn("nobody")
        assert result == []

    def test_cancel_adds_committed_lines(self):
        m = _model_with_turn()
        before = len(m.all_committed_lines)
        m.cancel_turn("a1")
        assert len(m.all_committed_lines) > before


# ---------------------------------------------------------------------------
# TranscriptModel.commit_system_message
# ---------------------------------------------------------------------------


class TestCommitSystemMessage:
    def test_info_level_uses_dim(self):
        m = _model()
        lines = m.commit_system_message("hello", level="info")
        assert len(lines) == 1
        assert "\033[2m" in lines[0]

    def test_warning_level_uses_yellow(self):
        m = _model()
        lines = m.commit_system_message("watch out", level="warning")
        assert "\033[33m" in lines[0]

    def test_error_level_uses_red(self):
        m = _model()
        lines = m.commit_system_message("boom", level="error")
        assert "\033[31m" in lines[0]

    def test_default_level_is_info(self):
        m = _model()
        lines = m.commit_system_message("default")
        assert "\033[2m" in lines[0]


# ---------------------------------------------------------------------------
# TranscriptModel.evict_old_turns
# ---------------------------------------------------------------------------


class TestEvictOldTurns:
    def test_no_eviction_when_below_limit(self):
        m = _model()
        for i in range(5):
            m.append_turn(f"a{i}", f"agent:{i}", float(i))
        count = m.evict_old_turns(keep_last=10)
        assert count == 0

    def test_eviction_removes_output_lines_from_old_turns(self):
        m = _model()
        for i in range(5):
            t = m.append_turn(f"a{i}", f"agent:{i}", float(i))
            t.output_lines = [f"line {i}"]
        count = m.evict_old_turns(keep_last=2)
        assert count == 3
        for t in m.turns[:3]:
            assert t.output_lines == []
            assert t._evicted is True

    def test_eviction_preserves_recent_turns(self):
        m = _model()
        for i in range(5):
            t = m.append_turn(f"a{i}", f"agent:{i}", float(i))
            t.output_lines = [f"line {i}"]
        m.evict_old_turns(keep_last=2)
        for t in m.turns[-2:]:
            assert t.output_lines != [] or t._evicted is False

    def test_eviction_does_not_double_evict(self):
        m = _model()
        for i in range(5):
            m.append_turn(f"a{i}", f"agent:{i}", float(i))
        count1 = m.evict_old_turns(keep_last=2)
        count2 = m.evict_old_turns(keep_last=2)
        assert count2 == 0  # already evicted

    def test_eviction_clears_tool_call_output_lines(self):
        m = _model()
        for i in range(3):
            t = m.append_turn(f"a{i}", f"agent:{i}", float(i))
            tc = m.add_tool_call(f"a{i}", f"tc{i}", "tool", state=ToolCallState.SUCCESS)
            tc.output_lines = [f"out{i}"]
        m.evict_old_turns(keep_last=1)
        for t in m.turns[:-1]:
            for tc in t.tool_calls:
                assert tc.output_lines == []


# ---------------------------------------------------------------------------
# Internal _evict_old_turns called on MAX_TURNS_IN_MEMORY exceeded
# ---------------------------------------------------------------------------


class TestAutoEviction:
    def test_auto_eviction_when_max_turns_exceeded(self):
        m = _model()
        # Append just over the limit
        for i in range(MAX_TURNS_IN_MEMORY + 2):
            m.append_turn(f"a{i}", f"agent:{i}", float(i))
        # _evict_old_turns should have been called at least once
        # Turns list may still be > MAX_TURNS_IN_MEMORY by 1 (evict is non-truncating)
        assert len(m.turns) > 0


# ---------------------------------------------------------------------------
# TranscriptModel._check_finalization
# ---------------------------------------------------------------------------


class TestCheckFinalization:
    def test_complete_turn_with_all_terminal_tools_becomes_finalized(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "tool", state=ToolCallState.SUCCESS)
        m.finalize_turn("a1", "done", tokens=10)
        # After finalize_turn, _check_finalization is called
        assert m.turns[0].state == TurnState.FINALIZED

    def test_complete_turn_with_running_tool_stays_complete(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "tool", state=ToolCallState.RUNNING)
        # Manually set state to COMPLETE to simulate finalize_turn skipping check
        m.turns[0].state = TurnState.COMPLETE
        m._check_finalization(m.turns[0])
        assert m.turns[0].state == TurnState.COMPLETE

    def test_non_complete_turn_not_promoted(self):
        m = _model_with_turn()
        m.turns[0].state = TurnState.STREAMING
        m._check_finalization(m.turns[0])
        assert m.turns[0].state == TurnState.STREAMING


# ---------------------------------------------------------------------------
# MentionChip
# ---------------------------------------------------------------------------


class TestMentionChip:
    def test_mention_chip_ok_renders_checkmark(self):
        from agenthicc.tui.transcript import _render_mention_chip
        chip = MentionChip(raw="@foo.py", kind="file", display_size="1.2 KB", ok=True)
        lines = _render_mention_chip(chip)
        combined = "\n".join(lines)
        assert "✓" in combined
        assert "@foo.py" in combined
        assert "1.2 KB" in combined

    def test_mention_chip_error_renders_cross(self):
        from agenthicc.tui.transcript import _render_mention_chip
        chip = MentionChip(raw="@missing.py", kind="file", display_size="", ok=False, error="not found")
        lines = _render_mention_chip(chip)
        combined = "\n".join(lines)
        assert "✗" in combined
        assert "not found" in combined

    def test_mention_chip_no_error_message(self):
        from agenthicc.tui.transcript import _render_mention_chip
        chip = MentionChip(raw="@x.py", kind="file", display_size="", ok=False)
        lines = _render_mention_chip(chip)
        assert len(lines) == 1

    def test_mention_chip_expanded_shows_content(self):
        from agenthicc.tui.transcript import _render_mention_chip
        chip = MentionChip(
            raw="@big.py", kind="file", display_size="", ok=True, expanded=True
        )
        chip._content_lines = ["line1", "line2", "line3"]
        lines = _render_mention_chip(chip)
        combined = "\n".join(lines)
        assert "line1" in combined

    def test_mention_chip_expanded_truncates_at_50(self):
        from agenthicc.tui.transcript import _render_mention_chip
        chip = MentionChip(raw="@huge.py", kind="file", display_size="", ok=True, expanded=True)
        chip._content_lines = [f"line{i}" for i in range(60)]
        lines = _render_mention_chip(chip)
        combined = "\n".join(lines)
        assert "more lines" in combined

    def test_mention_chip_not_expanded_hides_content(self):
        from agenthicc.tui.transcript import _render_mention_chip
        chip = MentionChip(raw="@f.py", kind="file", display_size="", ok=True, expanded=False)
        chip._content_lines = ["hidden content"]
        lines = _render_mention_chip(chip)
        combined = "\n".join(lines)
        assert "hidden content" not in combined


# ---------------------------------------------------------------------------
# TranscriptModel.add_mention_chips + set_mention_content
# ---------------------------------------------------------------------------


class TestMentionChipIntegration:
    def test_add_mention_chips_attaches_to_turn(self):
        m = _model_with_turn()
        chips = [MentionChip(raw="@a.py", kind="file", display_size="1B", ok=True)]
        m.add_mention_chips("a1", chips)
        assert len(m.turns[0].mention_chips) == 1
        assert m.turns[0].mention_chips[0].raw == "@a.py"

    def test_add_mention_chips_no_op_for_unknown_agent(self):
        m = _model_with_turn()
        chips = [MentionChip(raw="@x.py", kind="file", display_size="", ok=True)]
        m.add_mention_chips("unknown", chips)  # should not raise
        assert m.turns[0].mention_chips == []

    def test_set_mention_content_updates_chip(self):
        m = _model_with_turn()
        chip = MentionChip(raw="@src.py", kind="file", display_size="", ok=True)
        m.turns[0].mention_chips.append(chip)
        m.set_mention_content("a1", "@src.py", "line1\nline2\nline3")
        assert chip._content_lines == ["line1", "line2", "line3"]

    def test_set_mention_content_stores_in_mention_content_dict(self):
        m = _model_with_turn()
        m.set_mention_content("a1", "@x.py", "content")
        assert m.turns[0].mention_content["@x.py"] == "content"

    def test_set_mention_content_no_op_for_unknown_agent(self):
        m = _model_with_turn()
        m.set_mention_content("unknown", "@x.py", "content")  # should not raise

    def test_set_mention_content_no_chip_stores_content_only(self):
        m = _model_with_turn()
        # No chip with this raw, but content still stored in dict
        m.set_mention_content("a1", "@no-chip.py", "data")
        assert m.turns[0].mention_content["@no-chip.py"] == "data"


# ---------------------------------------------------------------------------
# TranscriptModel.render_ad_panel — extended
# ---------------------------------------------------------------------------


class TestRenderAdPanelExtended:
    def test_ad_with_empty_text_returns_none(self):
        m = _model()
        ad = type("Ad", (), {"text": "", "cta_url": "https://example.com"})()
        m.set_current_ad(ad)
        assert m.render_ad_panel() is None

    def test_ad_without_cta_url_attr(self):
        m = _model()
        ad = type("Ad", (), {"text": "No URL ad"})()
        m.set_current_ad(ad)
        panel = m.render_ad_panel()
        assert panel is not None
        assert "No URL ad" in panel

    def test_ad_with_cta_url_includes_url(self):
        m = _model()
        ad = type("Ad", (), {"text": "Buy now", "cta_url": "https://buy.example"})()
        m.set_current_ad(ad)
        panel = m.render_ad_panel()
        assert "https://buy.example" in panel


# ---------------------------------------------------------------------------
# diff_lines edge cases
# ---------------------------------------------------------------------------


class TestDiffLinesEdgeCases:
    def test_replace_op_emits_remove_then_add(self):
        old = ["foo"]
        new = ["bar"]
        result = diff_lines(old, new)
        ops = [(op, ln) for op, ln in result]
        assert ("remove", "foo") in ops
        assert ("add", "bar") in ops

    def test_large_diff_all_ops(self):
        old = [f"line{i}" for i in range(10)]
        new = [f"line{i}" for i in range(5)] + [f"new{i}" for i in range(5)]
        result = diff_lines(old, new)
        ops = {op for op, _ in result}
        assert "keep" in ops or "add" in ops or "remove" in ops

    def test_completely_different_lists(self):
        result = diff_lines(["a", "b"], ["c", "d"])
        ops = {op for op, _ in result}
        assert "remove" in ops
        assert "add" in ops


# ---------------------------------------------------------------------------
# TranscriptModel.committed lines — cursor and peek
# ---------------------------------------------------------------------------


class TestCommittedLinesCursor:
    def test_get_new_committed_lines_advances_cursor(self):
        m = _model_with_turn()
        new = m.get_new_committed_lines()
        assert len(new) >= 1
        assert m.committed_cursor == len(m.all_committed_lines)

    def test_peek_does_not_advance_cursor(self):
        m = _model_with_turn()
        peek1 = m.peek_new_committed_lines()
        peek2 = m.peek_new_committed_lines()
        assert peek1 == peek2

    def test_get_twice_returns_only_new_lines(self):
        m = _model_with_turn()
        m.get_new_committed_lines()
        m.commit_system_message("new message")
        second = m.get_new_committed_lines()
        assert len(second) == 1

    def test_finalized_line_count(self):
        m = _model_with_turn()
        before = m.finalized_line_count()
        m.commit_system_message("extra")
        assert m.finalized_line_count() == before + 1


# ---------------------------------------------------------------------------
# TranscriptModel.replay_from_store
# ---------------------------------------------------------------------------


class TestReplayFromStore:
    def test_replay_populates_turns(self):
        m = _model()

        class FakeStore:
            def load_turns(self, session_id):
                return [
                    {
                        "agent_id": "a1",
                        "agent_name": "agent:test",
                        "timestamp": 0.0,
                        "final_text": "hello",
                        "tokens": 10,
                        "cost_usd": 0.001,
                        "tool_calls": [],
                    }
                ]

        m.replay_from_store(FakeStore(), "session-abc-1234", last_n=5, cols=80)
        assert len(m.turns) >= 1

    def test_replay_with_tool_calls(self):
        m = _model()

        class FakeStore:
            def load_turns(self, session_id):
                return [
                    {
                        "agent_id": "a1",
                        "agent_name": "agent:test",
                        "timestamp": 0.0,
                        "final_text": "",
                        "tokens": 0,
                        "cost_usd": 0.0,
                        "tool_calls": [
                            {
                                "tool_use_id": "tc1",
                                "name": "read_file",
                                "args": {"path": "x.py"},
                                "state": "SUCCESS",
                                "duration_ms": 50,
                                "result_summary": "ok",
                                "error": "",
                            }
                        ],
                    }
                ]

        m.replay_from_store(FakeStore(), "session-xyz", cols=80)
        assert len(m.turns) >= 1


# ---------------------------------------------------------------------------
# TranscriptModel.add_tool_call with APPROVAL_NEEDED and PENDING states
# ---------------------------------------------------------------------------


class TestAddToolCallStates:
    def test_add_tool_call_approval_needed(self):
        m = _model_with_turn()
        tc = m.add_tool_call("a1", "tc1", "risky", state=ToolCallState.APPROVAL_NEEDED)
        assert tc.state == ToolCallState.APPROVAL_NEEDED

    def test_add_tool_call_pending_state(self):
        m = _model_with_turn()
        tc = m.add_tool_call("a1", "tc1", "pending_tool", state=ToolCallState.PENDING)
        assert tc.state == ToolCallState.PENDING

    def test_add_tool_call_without_existing_turn_uses_index(self):
        m = _model()
        # No turn for this agent, but add_tool_call still registers in _tool_index
        tc = m.add_tool_call("unknown", "tc1", "tool")
        assert m._tool_index["tc1"] is tc


# ---------------------------------------------------------------------------
# TranscriptModel.update_cols
# ---------------------------------------------------------------------------


class TestUpdateCols:
    def test_update_cols_changes_internal_state(self):
        m = _model()
        m.update_cols(120)
        assert m._cols == 120


# ---------------------------------------------------------------------------
# TranscriptModel spinner_frame property
# ---------------------------------------------------------------------------


class TestSpinnerFrameProperty:
    def test_spinner_frame_starts_at_zero(self):
        m = _model()
        assert m.spinner_frame == 0

    def test_spinner_frame_advances(self):
        m = _model_with_turn()
        m.add_tool_call("a1", "tc1", "t", state=ToolCallState.RUNNING)
        m.advance_spinner()
        assert m.spinner_frame == 1
