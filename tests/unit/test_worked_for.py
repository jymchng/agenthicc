"""Tests for compact turn and activity completion recaps."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

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
    @staticmethod
    def _make_console() -> tuple[Console, StringIO, MagicMock]:
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False, no_color=True)
        stub = MagicMock()
        stub._console = console
        return console, buf, stub

    def test_turn_row_uses_compact_duration_without_trailing_blank(self) -> None:
        from agenthicc.tui.conversation_store import ConversationEvent
        from agenthicc.tui.workspace.appender import _render_turn_complete

        _, buf, stub = self._make_console()
        _render_turn_complete(
            stub,
            ConversationEvent(
                event_id="turn",
                kind="turn_complete",
                payload={"elapsed_s": 65.0},
            ),
        )

        assert buf.getvalue() == "✾ Turn · 1m 5s\n"

    def test_turn_row_is_omitted_below_one_second(self) -> None:
        from agenthicc.tui.conversation_store import ConversationEvent
        from agenthicc.tui.workspace.appender import _render_turn_complete

        _, buf, stub = self._make_console()
        _render_turn_complete(
            stub,
            ConversationEvent(
                event_id="turn",
                kind="turn_complete",
                payload={"elapsed_s": 0.5},
            ),
        )

        assert buf.getvalue() == ""

    def test_activity_total_uses_compact_duration_and_one_separator(self) -> None:
        from agenthicc.tui.conversation_store import ConversationEvent
        from agenthicc.tui.workspace.appender import _render_activity_complete

        _, buf, stub = self._make_console()
        _render_activity_complete(
            stub,
            ConversationEvent(
                event_id="activity",
                kind="activity_complete",
                payload={"elapsed_s": 65.0},
            ),
        )

        assert buf.getvalue() == "  Total · 1m 5s\n"

    def test_subsecond_activity_total_is_omitted(self) -> None:
        from agenthicc.tui.conversation_store import ConversationEvent
        from agenthicc.tui.workspace.appender import _render_activity_complete

        _, buf, stub = self._make_console()
        _render_activity_complete(
            stub,
            ConversationEvent(
                event_id="activity",
                kind="activity_complete",
                payload={"elapsed_s": 0.5},
            ),
        )

        assert buf.getvalue() == ""

    def test_turns_and_total_render_as_one_group(self) -> None:
        from agenthicc.tui.conversation_store import AppState, ConversationEvent
        from agenthicc.tui.workspace.appender import ScrollBufferAppender

        console = Console(record=True, markup=False, no_color=True)
        appender = ScrollBufferAppender(AppState.create(), console)
        events = (
            ConversationEvent(
                event_id="turn-1",
                kind="turn_complete",
                payload={"elapsed_s": 18.0},
            ),
            ConversationEvent(
                event_id="turn-2",
                kind="turn_complete",
                payload={"elapsed_s": 29.0},
            ),
            ConversationEvent(
                event_id="activity",
                kind="activity_complete",
                payload={"elapsed_s": 54.0},
            ),
        )
        for event in events:
            appender._render_one(event)

        assert console.export_text() == ("✾ Turn · 18s\n✾ Turn · 29s\n  Total · 54s\n")

    def test_error_timing_rows_do_not_claim_success(self) -> None:
        from agenthicc.tui.conversation_store import AppState, ConversationEvent
        from agenthicc.tui.workspace.appender import ScrollBufferAppender

        console = Console(record=True, markup=False, no_color=True)
        appender = ScrollBufferAppender(AppState.create(), console)
        appender._render_one(
            ConversationEvent(
                event_id="error",
                kind="error",
                payload={"message": "boom"},
            )
        )
        appender._render_one(
            ConversationEvent(
                event_id="turn",
                kind="turn_complete",
                payload={"elapsed_s": 2.0},
            )
        )
        appender._render_one(
            ConversationEvent(
                event_id="activity",
                kind="activity_complete",
                payload={"elapsed_s": 3.0},
            )
        )

        output = console.export_text()
        assert "ERROR boom" in output
        assert "✾ Turn · 2s" in output
        assert "  Total · 3s" in output
        assert "Completed" not in output
        assert "since last IDLE" not in output

    def test_activity_total_then_error_has_one_blank_line(self) -> None:
        from agenthicc.tui.conversation_store import AppState, ConversationEvent
        from agenthicc.tui.workspace.appender import ScrollBufferAppender

        console = Console(record=True, markup=False, no_color=True)
        appender = ScrollBufferAppender(AppState.create(), console)
        appender._render_one(
            ConversationEvent(
                event_id="activity",
                kind="activity_complete",
                payload={"elapsed_s": 2.0},
            )
        )
        appender._render_one(
            ConversationEvent(
                event_id="error",
                kind="error",
                payload={"message": "MCP server 'asyncmove' failed to start"},
            )
        )

        rendered = console.export_text()
        suffix = rendered.split("  Total · 2s", 1)[1]
        assert suffix.startswith("\n\nERROR MCP server 'asyncmove' failed to start")
        assert not suffix.startswith("\n\n\n")

    def test_missing_turn_elapsed_does_not_render_a_row(self) -> None:
        from agenthicc.tui.conversation_store import ConversationEvent
        from agenthicc.tui.workspace.appender import _render_turn_complete

        _, buf, stub = self._make_console()
        _render_turn_complete(
            stub,
            ConversationEvent(event_id="turn", kind="turn_complete", payload={}),
        )

        assert buf.getvalue() == ""
