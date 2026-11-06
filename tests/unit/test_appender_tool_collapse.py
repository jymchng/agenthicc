"""Unit tests: tool rows and tool-group collapse in ScrollBufferAppender (TUX-06).

Groups of ≤5 consecutive successful tool_complete events print one row each.
Beyond that the rest are counted, and a "…and N more tool calls" row closes
the group at the next boundary.  A failed call always prints.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import ConversationEvent, ConversationStore
from agenthicc.tui.theme import palette
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.unit

_LIMIT = 5  # the default max_live_tool_calls


# ── helpers ───────────────────────────────────────────────────────────────────


def _ev(kind: str, **payload: object) -> ConversationEvent:
    return ConversationEvent(event_id="x", kind=kind, payload=dict(payload))


def _tool(
    name: str = "read_file", success: bool = True, summary: str = "a.py"
) -> ConversationEvent:
    return _ev(
        "tool_complete",
        name=name,
        summary=summary,
        success=success,
        duration_ms=5,
        output_lines=[],
    )


def _make_appender() -> tuple[ScrollBufferAppender, Console]:
    app_state = MagicMock()
    app_state.conversation.on_event.return_value = lambda: None
    console = Console(file=StringIO(), record=True, width=100, color_system=None)
    return ScrollBufferAppender(app_state, console), console


def _markup_appender() -> tuple[ScrollBufferAppender, MagicMock]:
    app_state = MagicMock()
    console = MagicMock()
    console.__enter__ = MagicMock(return_value=console)
    console.__exit__ = MagicMock(return_value=False)
    return ScrollBufferAppender(app_state, console), console


def _flush(appender: ScrollBufferAppender, events: list[ConversationEvent]) -> None:
    appender._pending = events
    appender._flush_scheduled = True
    appender._flush_batch()


def _rows(console: Console) -> list[str]:
    return [row.rstrip() for row in console.export_text(clear=False).splitlines()]


def _str_calls(console: MagicMock) -> list[str]:
    return [
        c.args[0] for c in console.print.call_args_list if c.args and isinstance(c.args[0], str)
    ]


def _tool_rows(rows: list[str]) -> list[str]:
    return [row for row in rows if row.startswith(("● ", "✗ "))]


# ── group counting ────────────────────────────────────────────────────────────


class TestGroupCount:
    def test_below_limit_all_printed(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT - 1)])
        assert len(_tool_rows(_rows(console))) == _LIMIT - 1
        assert appender._group_count == _LIMIT - 1

    def test_exactly_limit_all_printed(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT)])
        assert len(_tool_rows(_rows(console))) == _LIMIT

    def test_over_limit_only_five_printed(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 4)])
        assert len(_tool_rows(_rows(console))) == _LIMIT
        assert appender._group_count == _LIMIT + 4
        assert appender._group_hidden == 4

    def test_a_failure_beyond_the_limit_still_prints(self) -> None:
        appender, console = _make_appender()
        events = [_tool() for _ in range(_LIMIT + 2)] + [_tool(success=False, summary="bad.py")]
        _flush(appender, events + [_ev("text", text="done.")])
        rows = _rows(console)
        assert len(_tool_rows(rows)) == _LIMIT + 1
        assert any(row.startswith("✗ Read bad.py") for row in rows)
        assert "  └ …and 2 more tool calls" in rows

    def test_turn_start_resets_count(self) -> None:
        appender, _console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT)])
        _flush(appender, [_ev("turn_start", agent="default", model="m")])
        assert appender._group_count == 0


# ── rows and rhythm ───────────────────────────────────────────────────────────


class TestRows:
    @pytest.mark.parametrize(
        ("name", "operation"),
        [("read_file", "Read"), ("grep_files", "Search"), ("git_diff", "Git"), ("run_bash", "$")],
    )
    def test_every_tool_family_uses_its_operation(self, name: str, operation: str) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool(name=name, summary="x")])
        assert _rows(console) == [f"● {operation} x"]

    def test_unknown_tools_title_case_their_name(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool(name="frobnicate_widget", summary="w")])
        assert _rows(console) == ["● Frobnicate Widget w"]

    def test_consecutive_rows_have_no_blank_and_text_follows_after_one(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool(), _tool(), _ev("text", text="done."), _tool()])
        assert _rows(console) == ["● Read a.py", "● Read a.py", "", "done.", "", "● Read a.py"]

    def test_no_summary_at_limit(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT)])
        _flush(appender, [_ev("text", text="done.")])
        assert not any("more tool" in row for row in _rows(console))

    def test_summary_appears_on_text(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 3)])
        _flush(appender, [_ev("text", text="done.")])
        rows = _rows(console)
        assert rows[_LIMIT] == "  └ …and 3 more tool calls"
        assert rows[_LIMIT + 1 : _LIMIT + 3] == ["", "done."]

    def test_summary_appears_on_error(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 2)])
        _flush(appender, [_ev("error", message="boom")])
        assert "  └ …and 2 more tool calls" in _rows(console)

    def test_singular_grammar(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 1)])
        _flush(appender, [_ev("text", text="done.")])
        assert "  └ …and 1 more tool call" in _rows(console)

    def test_group_resets_after_text(self) -> None:
        appender, _console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 2)])
        _flush(appender, [_ev("text", text="first response.")])
        assert appender._group_count == 0

    def test_llm_response_has_trailing_blank_line(self) -> None:
        appender, console = _markup_appender()
        _flush(appender, [_ev("text", text="LLM response.")])
        assert console.print.call_args_list[-1].args == ()
        assert console.print.call_args_list[-1].kwargs == {}

    def test_empty_text_does_not_emit_spurious_blank_line(self) -> None:
        appender, console = _markup_appender()
        _flush(appender, [_ev("text", text="   ")])
        assert console.print.call_args_list == []

    def test_second_group_independent(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(7)])
        _flush(appender, [_ev("text", text="response 1.")])
        before = len(_rows(console))
        _flush(appender, [_tool() for _ in range(3)])
        _flush(appender, [_ev("text", text="response 2.")])
        rows = _rows(console)[before:]
        assert len(_tool_rows(rows)) == 3
        assert not any("more tool" in row for row in rows)

    def test_summary_count_accurate_across_batches(self) -> None:
        appender, console = _make_appender()
        for _ in range(8):
            _flush(appender, [_tool()])
        assert appender._group_count == 8
        _flush(appender, [_ev("text", text="done.")])
        rows = _rows(console)
        assert len(_tool_rows(rows)) == _LIMIT
        assert "  └ …and 3 more tool calls" in rows

    def test_overflow_summary_precedes_the_next_user_message(self) -> None:
        appender, console = _make_appender()
        events = [_tool(name="run_bash", summary="ls") for _ in range(_LIMIT + 32)]
        events.extend(
            [
                _ev("turn_complete", elapsed_s=1.0),
                _ev("user_message", text="continue"),
                _ev("turn_start", agent="reviewer", model="m"),
                _ev("text", text="ok"),
            ]
        )
        _flush(appender, events)
        rows = _rows(console)
        summary_index = rows.index("  └ …and 32 more tool calls")
        user_index = next(i for i, row in enumerate(rows) if row.startswith("❯ continue"))
        header_index = next(i for i, row in enumerate(rows) if row.startswith("◆ reviewer"))
        assert summary_index < user_index < header_index

    def test_flush_persists_overflow_when_interrupted_without_boundary_event(self) -> None:
        appender, console = _make_appender()
        _flush(appender, [_tool(name="run_bash") for _ in range(_LIMIT + 4)])
        appender.flush()
        assert "  └ …and 4 more tool calls" in _rows(console)
        assert appender._group_count == 0
        assert appender._state.conversation.live_tool_overflow.set.call_args.args == (0,)


# ── workflow notices (BEAU-5) ─────────────────────────────────────────────────


class TestNotices:
    def test_goal_mutation_notice_never_coalesces_into_tool_group_or_rewrites_state(self) -> None:
        appender, console = _make_appender()
        event = _ev("goal_list_mutated", operation="insert", index=1, goal_count=3)
        _flush(appender, [_tool(), event, _ev("text", text="continue")])
        assert sum("Plan updated" in row for row in _rows(console)) == 1
        assert appender._group_count == 0
        assert event.payload["index"] == 1
        assert event.payload["goal_count"] == 3

    def test_phase_control_events_render_bounded_notices(self) -> None:
        appender, console = _markup_appender()
        phase_id = "a" * 200
        reason = "r" * 400
        _flush(
            appender,
            [
                _ev("phase_control_requested", operation="skip", phase_id=phase_id, reason=reason),
                _ev(
                    "phase_plan_mutated",
                    operation="delete",
                    phase_id=phase_id,
                    disposition="deleted",
                    reason=reason,
                ),
                _ev(
                    "phase_control_rejected",
                    operation="postpone",
                    phase_id=phase_id,
                    error_code="phase_id_unknown",
                ),
            ],
        )
        lines = _str_calls(console)
        assert any("Skipped phase" in line and "safe boundary" in line for line in lines)
        assert any("Deleted phase" in line and "checkpointed" in line for line in lines)
        assert any("Phase control rejected" in line for line in lines)
        assert all(len(line) < 260 for line in lines)
        assert "a" * 200 not in "\n".join(lines)
        assert "r" * 400 not in "\n".join(lines)

    def test_goal_insert_notice_uses_human_position_and_separate_continuation(self):
        appender, console = _markup_appender()

        _flush(
            appender,
            [
                _ev(
                    "goal_list_mutated",
                    operation="insert",
                    index=7,
                    goal_count=13,
                    goal_id="secret-goal-id",
                    goal="do not render this goal",
                )
            ],
        )

        lines = _str_calls(console)
        assert lines[0] == f"  [dim]⎿[/dim] [{palette().accent}]Plan updated[/]"
        assert "Inserted a goal · position 8 of 13" in lines[1]
        assert "↳ Current goal continues" in lines[2]
        assert all("at 7" not in line for line in lines)
        assert all("(13 total)" not in line for line in lines)
        assert all("; continuing" not in line for line in lines)
        assert all("secret-goal-id" not in line for line in lines)
        assert all("do not render this goal" not in line for line in lines)
        assert console.print.call_args_list[-1].args == ()

    @pytest.mark.parametrize(
        ("operation", "index", "count", "expected"),
        [
            ("append", 12, 13, "Added a goal to the end · position 13 of 13"),
            ("insert", 0, 1, "Inserted a goal · position 1 of 1"),
            ("future-operation", 2, 4, "Added a goal · position 3 of 4"),
        ],
    )
    def test_goal_mutation_notice_has_safe_operation_copy(self, operation, index, count, expected):
        appender, console = _markup_appender()

        _flush(
            appender,
            [_ev("goal_list_mutated", operation=operation, index=index, goal_count=count)],
        )

        lines = _str_calls(console)
        assert expected in lines[1]
        assert "Current goal continues" in lines[2]

    @pytest.mark.parametrize(
        "payload",
        [
            {"operation": "insert", "index": -1, "goal_count": 4},
            {"operation": "append", "index": 0, "goal_count": 4},
            {"operation": "insert", "index": True, "goal_count": 4},
            {"operation": "insert", "index": 7, "goal_count": 0},
            {"operation": "insert", "index": 10_000_001, "goal_count": 10_000_002},
            {"operation": "insert", "index": "7", "goal_count": "13"},
        ],
    )
    def test_goal_mutation_notice_handles_incoherent_metadata_without_raw_values(self, payload):
        appender, console = _markup_appender()

        _flush(appender, [_ev("goal_list_mutated", **payload)])

        lines = _str_calls(console)
        assert lines[0] == f"  [dim]⎿[/dim] [{palette().accent}]Plan updated[/]"
        assert "Current goal continues" in lines[2]
        assert all("10_000_001" not in line for line in lines)
        assert all("at " not in line for line in lines)
        assert console.print.call_args_list[-1].args == ()


# ── ConversationStore signal ──────────────────────────────────────────────────


class TestToolGroupCountSignal:
    def test_increments_on_tool_complete(self):
        store = ConversationStore()
        store.begin_turn("assistant")
        for _ in range(3):
            store.append_event(
                "tool_complete",
                {"name": "read_file", "summary": "", "success": True, "output_lines": []},
            )
        assert store.tool_group_count() == 3

    def test_resets_on_text(self):
        store = ConversationStore()
        store.begin_turn("assistant")
        store.append_event(
            "tool_complete",
            {"name": "x", "summary": "", "success": True, "output_lines": []},
        )
        store.append_event("text", {"text": "hello"})
        assert store.tool_group_count() == 0

    def test_resets_on_turn_start_event(self):
        store = ConversationStore()
        store.begin_turn("assistant")
        store.append_event(
            "tool_complete",
            {"name": "x", "summary": "", "success": True, "output_lines": []},
        )
        assert store.tool_group_count() == 1
        # Production code always calls append_event("turn_start") after begin_turn()
        store.append_event("turn_start", {"agent_name": "assistant", "turn_id": "t2"})
        assert store.tool_group_count() == 0

    def test_signal_fires_on_each_tool(self):
        store = ConversationStore()
        store.begin_turn("assistant")
        counts: list[int] = []
        store.tool_group_count.subscribe(lambda: counts.append(store.tool_group_count()))
        for _ in range(3):
            store.append_event(
                "tool_complete",
                {"name": "x", "summary": "", "success": True, "output_lines": []},
            )
        assert counts == [1, 2, 3]
