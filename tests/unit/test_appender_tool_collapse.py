"""Unit tests: tool-group collapse in ScrollBufferAppender.

Groups of ≤5 consecutive tool_complete events are printed in full.
Groups of >5 print the first 5 and a "...and N more" summary when the
next text or error event closes the group.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agenthicc.tui.conversation_store import ConversationEvent, ConversationStore
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.unit

_LIMIT = 5  # must match the hard-coded limit inside _render_one


# ── helpers ───────────────────────────────────────────────────────────────────


def _ev(kind: str, **payload) -> ConversationEvent:
    return ConversationEvent(event_id="x", kind=kind, payload=dict(payload))


def _tool(name: str = "read_file", success: bool = True) -> ConversationEvent:
    return _ev(
        "tool_complete",
        name=name,
        args_str="",
        success=success,
        dur_str="  [dim]5ms[/dim]",
        output_lines=[],
    )


def _make_appender() -> tuple[ScrollBufferAppender, MagicMock]:
    app_state = MagicMock()
    app_state.conversation.on_event.return_value = lambda: None
    console = MagicMock()
    console.__enter__ = MagicMock(return_value=console)
    console.__exit__ = MagicMock(return_value=False)
    return ScrollBufferAppender(app_state, console), console


def _flush(appender: ScrollBufferAppender, events: list[ConversationEvent]) -> None:
    appender._pending = events
    appender._flush_scheduled = True
    appender._flush_batch()


def _str_calls(console: MagicMock) -> list[str]:
    """Return all string args passed to console.print()."""
    return [
        c.args[0] for c in console.print.call_args_list if c.args and isinstance(c.args[0], str)
    ]


def _tool_lines(lines: list[str]) -> list[str]:
    return [s for s in lines if "[green]●[/green]" in s or "[red]✗[/red]" in s]


# ── group counting ────────────────────────────────────────────────────────────


class TestGroupCount:
    def test_below_limit_all_printed(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT - 1)])
        # No text event yet — group open, all printed so far
        assert len(_tool_lines(_str_calls(console))) == _LIMIT - 1
        assert appender._group_count == _LIMIT - 1

    def test_exactly_limit_all_printed(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT)])
        assert len(_tool_lines(_str_calls(console))) == _LIMIT
        assert appender._group_count == _LIMIT

    def test_over_limit_only_five_printed(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 4)])
        assert len(_tool_lines(_str_calls(console))) == _LIMIT
        assert appender._group_count == _LIMIT + 4

    def test_turn_start_resets_count(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT)])
        _flush(appender, [_ev("turn_start", agent_name="assistant")])
        assert appender._group_count == 0


# ── summary line ──────────────────────────────────────────────────────────────


class TestSummaryLine:
    @pytest.mark.parametrize(
        ("name", "operation"),
        [
            ("read_file", "Read"),
            ("grep_files", "Search"),
            ("shell", "Run"),
            ("git_diff", "Diff"),
        ],
    )
    def test_all_tool_families_use_operation_header(self, name, operation):
        appender, console = _make_appender()

        _flush(appender, [_tool(name=name)])

        header = _str_calls(console)[0]
        assert f"[bold]{operation}[/bold]" in header

    def test_tool_uses_operation_header_and_summary(self):
        appender, console = _make_appender()
        event = _tool(name="read_file")
        event.payload["args_str"] = "[dim]('README.md')[/dim]"

        _flush(appender, [event])

        lines = _str_calls(console)
        assert "[bold]Read[/bold]" in lines[0]
        assert "[dim]└─[/dim]" in lines[1]
        assert "[green]Completed[/green]" in lines[1]

    def test_tool_output_preview_is_rendered(self):
        appender, console = _make_appender()
        event = _tool()
        event.payload["output_lines"] = ["first line", "second line"]
        event.payload["output_more"] = 3

        _flush(appender, [event])

        lines = _str_calls(console)
        assert any("first line" in line for line in lines)
        assert any("second line" in line for line in lines)
        assert any("[dim]   1[/dim]" in line for line in lines)
        assert any("+3 more lines" in line for line in lines)

    def test_no_summary_at_limit(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT)])
        _flush(appender, [_ev("text", text="done.")])
        assert not any("more tool" in s for s in _str_calls(console))

    def test_summary_appears_on_text(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 3)])
        _flush(appender, [_ev("text", text="done.")])
        lines = _str_calls(console)
        assert any("3 more tool calls" in s for s in lines)

    def test_summary_appears_on_error(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 2)])
        _flush(appender, [_ev("error", message="boom")])
        lines = _str_calls(console)
        assert any("2 more tool calls" in s for s in lines)

    def test_singular_grammar(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 1)])
        _flush(appender, [_ev("text", text="done.")])
        lines = _str_calls(console)
        summary = next(s for s in lines if "more tool" in s)
        assert "1 more tool call" in summary
        assert "calls" not in summary

    def test_group_resets_after_text(self):
        appender, console = _make_appender()
        _flush(appender, [_tool() for _ in range(_LIMIT + 2)])
        _flush(appender, [_ev("text", text="first response.")])
        assert appender._group_count == 0

    def test_second_group_independent(self):
        appender, console = _make_appender()
        # first group: 7 tools → summary on text
        _flush(appender, [_tool() for _ in range(7)])
        _flush(appender, [_ev("text", text="response 1.")])
        console.print.reset_mock()
        # second group: 3 tools → all shown, no summary
        _flush(appender, [_tool() for _ in range(3)])
        _flush(appender, [_ev("text", text="response 2.")])
        lines = _str_calls(console)
        assert len(_tool_lines(lines)) == 3
        assert not any("more tool" in s for s in lines)

    def test_summary_count_accurate_across_batches(self):
        """Tools arriving in separate batches are counted together in one group."""
        appender, console = _make_appender()
        for _ in range(8):
            _flush(appender, [_tool()])  # one tool per batch
        assert appender._group_count == 8
        _flush(appender, [_ev("text", text="done.")])
        lines = _str_calls(console)
        assert len(_tool_lines(lines)) == _LIMIT
        assert any("3 more tool calls" in s for s in lines)


# ── ConversationStore signal ──────────────────────────────────────────────────


class TestToolGroupCountSignal:
    def test_increments_on_tool_complete(self):
        store = ConversationStore()
        store.begin_turn("assistant")
        for i in range(3):
            store.append_event(
                "tool_complete",
                {
                    "name": "read_file",
                    "args_str": "",
                    "success": True,
                    "dur_str": "",
                    "output_lines": [],
                },
            )
        assert store.tool_group_count() == 3

    def test_resets_on_text(self):
        store = ConversationStore()
        store.begin_turn("assistant")
        store.append_event(
            "tool_complete",
            {
                "name": "x",
                "args_str": "",
                "success": True,
                "dur_str": "",
                "output_lines": [],
            },
        )
        store.append_event("text", {"text": "hello"})
        assert store.tool_group_count() == 0

    def test_resets_on_turn_start_event(self):
        store = ConversationStore()
        store.begin_turn("assistant")
        store.append_event(
            "tool_complete",
            {
                "name": "x",
                "args_str": "",
                "success": True,
                "dur_str": "",
                "output_lines": [],
            },
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
                {
                    "name": "x",
                    "args_str": "",
                    "success": True,
                    "dur_str": "",
                    "output_lines": [],
                },
            )
        assert counts == [1, 2, 3]
