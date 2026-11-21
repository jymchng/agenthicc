"""TUX-10 §2.2 / AC 3: how a turn ends in the transcript.

The rows keep the operator's format (``✾ Worked for 24s | 10:20 AM``); a turn
that did something adds one muted line under it, an interrupted turn says so
and says whether anything was changed, and a quick answer has no row.
"""

from __future__ import annotations

import time

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent, ConversationStore
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.unit

#: 2026-01-01 10:20 local time, so every row ends ``| 10:20 AM``.
_TS = time.mktime((2026, 1, 1, 10, 20, 0, 0, 0, -1))

Event = tuple[str, dict[str, object]]


def _render(*events: Event) -> str:
    console = Console(record=True, markup=False, no_color=True, width=100)
    appender = ScrollBufferAppender(AppState.create(), console)
    for index, (kind, payload) in enumerate(events):
        appender._render_one(ConversationEvent(f"e{index}", kind, payload, _TS))
    return console.export_text()


def _edit(path: str, old: list[str], new: list[str]) -> Event:
    return (
        "file_modified",
        {"path": path, "old_lines": old, "new_lines": new, "tool": "patch_file"},
    )


def _read(path: str) -> Event:
    return (
        "tool_complete",
        {"name": "read_file", "success": True, "summary": path, "state": "exited"},
    )


def _write_call(path: str, *, success: bool = True) -> Event:
    return (
        "tool_complete",
        {"name": "write_file", "success": success, "summary": path, "state": "exited"},
    )


_START: Event = ("turn_start", {"agent": "default", "model": "m"})


def test_a_turn_that_edits_three_files_ends_with_done_and_the_file_totals() -> None:
    """AC 3.1: ``Worked for … `` and ``edited 3 files +N −M``."""
    output = _render(
        _START,
        _edit("a.py", ["x"], ["x", "y", "z"]),
        _edit("b.py", ["1", "2"], ["1"]),
        _edit("c.py", [], ["new"]),
        ("turn_complete", {"elapsed_s": 72.0}),
    )
    rows = [row for row in output.splitlines() if "Worked for" in row or "edited" in row]
    assert rows[0] == "✾ Worked for 1m 12s | 10:20 AM"
    # +2 (a.py) +0 (b.py) +1 (c.py); -0, -1, -0: the sum of the turn's own edits.
    assert rows[1] == "  └ 3 tools · edited 3 files +3 −1"


def test_an_edited_file_is_counted_once_however_often_it_changed() -> None:
    output = _render(
        _START,
        _edit("a.py", ["x"], ["x", "y"]),
        _edit("a.py", ["x", "y"], ["x", "y", "z"]),
        ("turn_complete", {"elapsed_s": 30.0}),
    )
    assert "edited 1 file +2 −0" in output


def test_an_interrupted_turn_with_no_writes_says_nothing_was_changed() -> None:
    """AC 3.2."""
    output = _render(
        _START,
        _read("a.py"),
        _read("b.py"),
        ("turn_complete", {"elapsed_s": 22.0, "interrupted": True}),
    )
    assert "✾ Interrupted after 22s | 10:20 AM" in output
    assert "  └ 2 tools ran · nothing was changed" in output


def test_an_interrupted_turn_that_wrote_reports_what_changed_instead() -> None:
    output = _render(
        _START,
        _edit("a.py", ["x"], ["y"]),
        ("turn_complete", {"elapsed_s": 22.0, "interrupted": True}),
    )
    assert "Interrupted after 22s" in output
    assert "1 tool ran · edited 1 file +1 −1" in output
    assert "nothing was changed" not in output


def test_a_failed_write_does_not_count_as_a_change() -> None:
    output = _render(
        _START,
        _write_call("a.py", success=False),
        ("turn_complete", {"elapsed_s": 12.0, "interrupted": True}),
    )
    assert "nothing was changed" in output


def test_a_two_second_answer_has_no_trailer() -> None:
    """AC 3.3."""
    assert _render(_START, ("turn_complete", {"elapsed_s": 2.0})) == ""
    assert _render(_START, ("turn_complete", {"elapsed_s": 5.0})).startswith("✾ Worked for 5s")


def test_a_short_turn_that_ran_a_tool_still_has_its_row() -> None:
    output = _render(_START, _read("a.py"), ("turn_complete", {"elapsed_s": 2.0}))
    assert "✾ Worked for 2s | 10:20 AM" in output
    assert "  └ 1 tool\n" in output


def test_a_failed_turn_is_stopped_not_worked_and_the_request_is_not_done() -> None:
    output = _render(
        _START,
        ("error", {"message": "rate limited"}),
        ("turn_complete", {"elapsed_s": 8.0}),
        ("activity_complete", {"elapsed_s": 9.0}),
    )
    assert "✾ Stopped after 8s | 10:20 AM" in output
    assert "Worked for" not in output and "Done in" not in output


def test_each_turn_counts_only_its_own_tools() -> None:
    output = _render(
        _START,
        _read("a.py"),
        ("turn_complete", {"elapsed_s": 10.0}),
        _START,
        ("turn_complete", {"elapsed_s": 10.0}),
    )
    assert output.count("1 tool") == 1


def test_the_closing_row_is_followed_by_one_blank_line() -> None:
    output = _render(
        _START,
        _read("a.py"),
        ("turn_complete", {"elapsed_s": 10.0}),
        ("activity_complete", {"elapsed_s": 10.0}),
        ("user_message", {"text": "next"}),
    )
    after = output.split("└ 1 tool\n", 1)[1]
    assert after.startswith("\n") and not after.startswith("\n\n")


def test_closing_a_turn_as_interrupted_marks_its_event() -> None:
    """The store is where the cancellation path says it: ``interrupted`` on ``turn_complete``."""
    store = ConversationStore()
    store.begin_turn("agent", "t1")
    store.close_turn(interrupted=True)
    event = next(e for e in store.turns()[0].events if e.kind == "turn_complete")
    assert event.payload["interrupted"] is True
    store.begin_turn("agent", "t2")
    store.close_turn()
    event = next(e for e in store.turns()[1].events if e.kind == "turn_complete")
    assert "interrupted" not in event.payload
