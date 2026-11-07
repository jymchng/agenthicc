"""Tests for compact turn and activity completion recaps."""

from __future__ import annotations


import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import ConversationStore
from agenthicc.tui.workspace.formatters import compact_duration

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m"),
        (61, "1m 1s"),
        (3_599, "59m 59s"),
        (3_600, "1h"),
        (3_724, "1h 2m 4s"),
        (1.9, "1s"),
        (-1, "0s"),
    ],
)
def test_compact_duration(elapsed: float, expected: str) -> None:
    assert compact_duration(elapsed) == expected


# ── close_turn emits elapsed ──────────────────────────────────────────────────


class TestCloseTurnElapsed:
    def test_turn_complete_event_carries_elapsed_s(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.close_turn()

        events = [e for e in conv.turns()[0].events if e.kind == "turn_complete"]
        assert events, "turn_complete event must be emitted"
        assert "elapsed_s" in events[0].payload

    def test_elapsed_s_is_float(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.close_turn()
        ev = next(e for e in conv.turns()[0].events if e.kind == "turn_complete")
        assert isinstance(ev.payload["elapsed_s"], float)

    def test_no_turn_complete_when_no_active_turn(self) -> None:
        conv = ConversationStore()
        conv.close_turn()  # idempotent — no active turn
        assert not conv.turns()

    def test_error_path_also_emits_turn_complete(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.close_turn(error="Something broke")

        kinds = [e.kind for e in conv.turns()[0].events]
        assert "error" in kinds
        assert "turn_complete" in kinds

    def test_error_turn_complete_comes_after_error_event(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.close_turn(error="boom")
        kinds = [e.kind for e in conv.turns()[0].events]
        assert kinds.index("error") < kinds.index("turn_complete")

    def test_start_time_cleared_after_close(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.close_turn()
        assert conv._start_time == 0.0
        assert conv.elapsed_s == 0.0


class TestCompletionRecapRenderers:
    """``✾ Worked for 37s`` per turn; the request's total only when it adds to it."""

    @staticmethod
    def _render(*events: tuple[str, dict[str, object]]) -> str:
        from agenthicc.tui.conversation_store import AppState, ConversationEvent
        from agenthicc.tui.workspace.appender import ScrollBufferAppender

        console = Console(record=True, markup=False, no_color=True, width=100)
        appender = ScrollBufferAppender(AppState.create(), console)
        for index, (kind, payload) in enumerate(events):
            appender._render_one(ConversationEvent(f"e{index}", kind, payload))
        return console.export_text()

    def test_a_turn_reads_worked_for_with_a_compact_duration(self) -> None:
        assert self._render(("turn_complete", {"elapsed_s": 65.0})) == "✾ Worked for 1m 5s\n"

    def test_turn_row_is_omitted_below_one_second(self) -> None:
        assert self._render(("turn_complete", {"elapsed_s": 0.5})) == ""

    def test_missing_turn_elapsed_does_not_render_a_row(self) -> None:
        assert self._render(("turn_complete", {})) == ""

    def test_one_turn_does_not_repeat_its_time_as_a_total(self) -> None:
        output = self._render(
            ("turn_complete", {"elapsed_s": 24.0}), ("activity_complete", {"elapsed_s": 24.4})
        )
        assert output == "✾ Worked for 24s\n"
        assert "Total" not in output and "Done" not in output

    def test_several_turns_end_with_the_requests_total(self) -> None:
        output = self._render(
            ("turn_complete", {"elapsed_s": 18.0}),
            ("turn_complete", {"elapsed_s": 29.0}),
            ("activity_complete", {"elapsed_s": 54.0}),
        )
        assert output == ("✾ Worked for 18s\n✾ Worked for 29s\n✾ Done in 54s across 2 turns\n")

    def test_a_total_longer_than_its_one_turn_is_shown(self) -> None:
        output = self._render(
            ("turn_complete", {"elapsed_s": 10.0}), ("activity_complete", {"elapsed_s": 40.0})
        )
        assert output == "✾ Worked for 10s\n✾ Done in 40s\n"

    def test_subsecond_activity_total_is_omitted(self) -> None:
        assert self._render(("activity_complete", {"elapsed_s": 0.5})) == ""

    def test_the_count_restarts_with_each_request(self) -> None:
        output = self._render(
            ("turn_complete", {"elapsed_s": 5.0}),
            ("turn_complete", {"elapsed_s": 5.0}),
            ("activity_complete", {"elapsed_s": 11.0}),
            ("turn_complete", {"elapsed_s": 7.0}),
            ("activity_complete", {"elapsed_s": 7.0}),
        )
        assert output.count("across") == 1 and output.endswith("✾ Worked for 7s\n")

    def test_error_timing_rows_do_not_claim_success(self) -> None:
        output = self._render(
            ("error", {"message": "boom"}),
            ("turn_complete", {"elapsed_s": 2.0}),
            ("activity_complete", {"elapsed_s": 3.0}),
        )
        assert "boom" in output
        assert "✾ Worked for 2s" in output
        assert "Completed" not in output
        assert "since last IDLE" not in output

    def test_activity_total_then_error_has_one_blank_line(self) -> None:
        output = self._render(
            ("turn_complete", {"elapsed_s": 1.0}),
            ("turn_complete", {"elapsed_s": 1.0}),
            ("activity_complete", {"elapsed_s": 2.0}),
            ("error", {"message": "MCP server 'asyncmove' failed to start"}),
        )
        suffix = output.split("✾ Done in 2s across 2 turns", 1)[1]
        assert suffix.startswith("\n\n") and not suffix.startswith("\n\n\n")
        assert "MCP server 'asyncmove' failed to start" in suffix
