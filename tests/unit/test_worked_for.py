"""Tests for the 'Worked for' turn-completion line (close_turn → scroll buffer)."""

from __future__ import annotations

import pytest
from agenthicc.tui.conversation_store import ConversationStore
from agenthicc.tui.workspace.appender import _fmt_worked

pytestmark = pytest.mark.unit


# ── _fmt_worked ───────────────────────────────────────────────────────────────


class TestFmtWorked:
    def test_zero_seconds(self) -> None:
        assert _fmt_worked(0) == "0 seconds"

    def test_one_second_singular(self) -> None:
        assert _fmt_worked(1) == "1 second"

    def test_two_seconds_plural(self) -> None:
        assert _fmt_worked(2) == "2 seconds"

    def test_fifty_nine_seconds(self) -> None:
        assert _fmt_worked(59) == "59 seconds"

    def test_exactly_one_minute_no_seconds(self) -> None:
        assert _fmt_worked(60) == "1 min"

    def test_one_minute_singular(self) -> None:
        assert "1 min" in _fmt_worked(61)
        assert "min" in _fmt_worked(61)
        assert "mins" not in _fmt_worked(61)

    def test_one_minute_one_second(self) -> None:
        assert _fmt_worked(61) == "1 min 1 second"

    def test_one_minute_thirty_seconds(self) -> None:
        assert _fmt_worked(90) == "1 min 30 seconds"

    def test_two_minutes_plural_no_seconds(self) -> None:
        assert _fmt_worked(120) == "2 mins"

    def test_two_minutes_plural_with_seconds(self) -> None:
        assert _fmt_worked(121) == "2 mins 1 second"

    def test_fractional_seconds_truncated(self) -> None:
        assert _fmt_worked(1.9) == "1 second"
        assert _fmt_worked(59.9) == "59 seconds"


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


# ── renderer threshold ────────────────────────────────────────────────────────


class TestTurnCompleteRenderer:
    def _make_event(self, elapsed: float) -> object:
        from agenthicc.tui.conversation_store import ConversationEvent  # noqa: PLC0415

        return ConversationEvent(event_id="x", kind="turn_complete", payload={"elapsed_s": elapsed})

    def _render(self, elapsed: float) -> str:
        from io import StringIO  # noqa: PLC0415
        from rich.console import Console  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415
        from agenthicc.tui.workspace.appender import _render_turn_complete  # noqa: PLC0415

        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False, no_color=True)
        stub = MagicMock()
        stub._console = console
        _render_turn_complete(stub, self._make_event(elapsed))
        return buf.getvalue()

    def test_worked_for_shown_when_elapsed_ge_one(self) -> None:
        out = self._render(5.0)
        assert "Worked for" in out
        assert "5 seconds" in out

    def test_worked_for_not_shown_when_elapsed_lt_one(self) -> None:
        out = self._render(0.5)
        assert "Worked for" not in out

    def test_blank_line_always_appended(self) -> None:
        assert self._render(5.0).endswith("\n\n")
        assert self._render(0.3).endswith("\n")

    def test_no_elapsed_key_treated_as_zero(self) -> None:
        from io import StringIO  # noqa: PLC0415
        from rich.console import Console  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415
        from agenthicc.tui.conversation_store import ConversationEvent  # noqa: PLC0415
        from agenthicc.tui.workspace.appender import _render_turn_complete  # noqa: PLC0415

        ev = ConversationEvent(event_id="x", kind="turn_complete", payload={})
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False, no_color=True)
        stub = MagicMock()
        stub._console = console
        _render_turn_complete(stub, ev)
        assert "Worked for" not in buf.getvalue()
