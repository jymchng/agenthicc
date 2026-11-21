"""TUX-10 §2.2 / AC 3: how a turn ends in the transcript.

The rows keep the operator's format (``✾ Worked for 24s | 10:20 AM``); a turn
that did something adds one muted line under it, an interrupted turn says so
and says whether anything was changed, and a quick answer has no row. The
store summarises the turn when it closes, so the rows below come from the
events a real turn emits.
"""

from __future__ import annotations

import re

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent, ConversationStore
from agenthicc.tui.turn_summary import summarize_turn
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.unit

Event = tuple[str, dict[str, object]]
_CLOCK = re.compile(r"\d{1,2}:\d\d [AP]M")


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


def _turn(
    monkeypatch: pytest.MonkeyPatch,
    events: list[Event],
    seconds: float,
    *,
    interrupted: bool = False,
    error: str | None = None,
    after: list[Event] | None = None,
) -> str:
    """Run one turn of *seconds* in a real store and print what it emitted."""
    now = [100.0]
    monkeypatch.setattr("agenthicc.tui.conversation_store.time.monotonic", lambda: now[0])
    store = ConversationStore()
    seen: list[ConversationEvent] = []
    store.on_event(seen.append)
    store.begin_turn("agent", "t1")
    store.append_event("turn_start", {"agent": "default", "model": "m"})
    for kind, payload in events:
        store.append_event(kind, payload)
    now[0] += seconds
    if error is not None:
        store.append_event("error", {"message": error})
    store.close_turn(interrupted=interrupted)
    for kind, payload in after or []:
        store.append_event(kind, payload)
    console = Console(record=True, markup=False, no_color=True, width=100)
    appender = ScrollBufferAppender(AppState.create(), console)
    for event in seen:
        appender._render_one(event)
    return _CLOCK.sub("CLOCK", console.export_text())


def test_a_turn_that_edits_three_files_ends_with_done_and_the_file_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3.1: ``Worked for …`` and ``edited 3 files +N −M``."""
    output = _turn(
        monkeypatch,
        [
            _edit("a.py", ["x"], ["x", "y", "z"]),
            _edit("b.py", ["1", "2"], ["1"]),
            _edit("c.py", [], ["new"]),
        ],
        72.0,
    )
    rows = [row for row in output.splitlines() if "Worked for" in row or "edited" in row]
    assert rows[0] == "✾ Worked for 1m 12s | CLOCK"
    # +2 (a.py) +0 (b.py) +1 (c.py); -0, -1, -0: the sum of the turn's own edits.
    assert rows[1] == "  └ 3 tools · edited 3 files +3 −1"


def test_an_edited_file_is_counted_once_however_often_it_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _turn(
        monkeypatch,
        [_edit("a.py", ["x"], ["x", "y"]), _edit("a.py", ["x", "y"], ["x", "y", "z"])],
        30.0,
    )
    assert "edited 1 file +2 −0" in output


def test_an_interrupted_turn_with_no_writes_says_nothing_was_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3.2."""
    output = _turn(monkeypatch, [_read("a.py"), _read("b.py")], 22.0, interrupted=True)
    assert "✾ Interrupted after 22s | CLOCK" in output
    assert "  └ 2 tools ran · nothing was changed" in output


def test_an_interrupted_turn_that_wrote_reports_what_changed_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _turn(monkeypatch, [_edit("a.py", ["x"], ["y"])], 22.0, interrupted=True)
    assert "Interrupted after 22s" in output
    assert "1 tool ran · edited 1 file +1 −1" in output
    assert "nothing was changed" not in output


def test_a_write_that_failed_is_not_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _turn(monkeypatch, [_write_call("a.py", success=False)], 12.0, interrupted=True)
    assert "nothing was changed" in output
    output = _turn(monkeypatch, [_write_call("a.py")], 12.0, interrupted=True)
    assert "nothing was changed" not in output  # it may have been: say nothing false


def test_a_two_second_answer_has_no_trailer(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 3.3."""
    assert _turn(monkeypatch, [], 2.0) == ""
    assert _turn(monkeypatch, [], 5.0).startswith("✾ Worked for 5s")


def test_a_short_turn_that_ran_a_tool_still_has_its_row(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _turn(monkeypatch, [_read("a.py")], 2.0)
    assert "✾ Worked for 2s | CLOCK" in output
    assert "  └ 1 tool\n" in output


def test_a_failed_turn_is_stopped_not_worked_and_the_request_is_not_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _turn(monkeypatch, [], 8.0, error="rate limited")
    assert "✾ Stopped after 8s | CLOCK" in output
    assert "  └ nothing was changed" in output
    assert "Worked for" not in output and "Done in" not in output


def test_the_closing_row_is_followed_by_one_blank_line(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _turn(monkeypatch, [_read("a.py")], 10.0, after=[("user_message", {"text": "next"})])
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


def test_the_summary_is_data_on_the_event_so_a_replay_prints_the_same_row() -> None:
    store = ConversationStore()
    store.begin_turn("agent", "t1")
    store.append_event(*_read("a.py"))
    store.append_event(*_edit("a.py", ["x"], ["y", "z"]))
    store.close_turn()
    payload = next(e for e in store.turns()[0].events if e.kind == "turn_complete").payload
    assert (payload["tools"], payload["files"], payload["added"], payload["removed"]) == (
        2,
        1,
        2,
        1,
    )
    # Rendered with no appender state at all, as the transcript view does.
    console = Console(record=True, markup=False, no_color=True, width=100)
    appender = ScrollBufferAppender(AppState.create(), console)
    appender._render_one(ConversationEvent("c", "turn_complete", {**payload, "elapsed_s": 30.0}))
    assert "└ 2 tools · edited 1 file +2 −1" in console.export_text()


def test_a_turn_with_nothing_to_summarise_says_so_plainly() -> None:
    assert summarize_turn([]) == {"tools": 0, "wrote": False}
