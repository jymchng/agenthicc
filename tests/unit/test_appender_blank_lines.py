"""One blank line is the spacing between blocks; the scrollback never shows two.

Blocks space themselves in three ways: a gap owed before the next block, a
blank line after a block, and a blank an event with nothing to show still
opened. Several of them can meet, so the appender prints a blank line only if
the previous output was not one (operator request: "one blank space after
``✾ Worked for …`` is sufficient").
"""

from __future__ import annotations

import random
from io import StringIO

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from tests.unit.test_renderer_coverage import CASES

pytestmark = pytest.mark.unit

_T = 1_767_243_843.0


def _appender(width: int = 80) -> tuple[ScrollBufferAppender, Console]:
    console = Console(file=StringIO(), record=True, width=width, color_system=None, highlight=False)
    return ScrollBufferAppender(AppState.create(), console), console


def _feed(appender: ScrollBufferAppender, kind: str, payload: dict[str, object]) -> None:
    appender._render_one(
        ConversationEvent(event_id=f"{kind}-1", kind=kind, payload=payload, timestamp=_T)
    )


def _rows(console: Console) -> list[str]:
    return [row.rstrip() for row in console.export_text().split("\n")]


def _blank_run_after(rows: list[str], marker: str) -> int:
    """How many blank rows follow the first row containing *marker*."""
    start = next(i for i, row in enumerate(rows) if marker in row)
    count = 0
    for row in rows[start + 1 :]:
        if row:
            break
        count += 1
    return count


def _double_blanks(rows: list[str]) -> list[int]:
    body = rows[:-1] if rows and not rows[-1] else rows  # the export ends with a newline
    return [i for i in range(1, len(body)) if not body[i] and not body[i - 1]]


# ── the case in the report ───────────────────────────────────────────────────


def test_one_blank_row_follows_the_worked_for_row_before_the_next_block() -> None:
    appender, console = _appender()
    _feed(appender, "turn_complete", {"elapsed_s": 178.0})
    _feed(appender, "activity_complete", {"elapsed_s": 178.0})
    _feed(appender, "system", {"text": "Compacting"})
    assert _blank_run_after(_rows(console), "Worked for 2m 58s") == 1


def test_events_that_print_nothing_do_not_add_blank_rows_after_it() -> None:
    """A blank line is owed once; an event with nothing to show must not collect it twice."""
    appender, console = _appender()
    _feed(appender, "turn_complete", {"elapsed_s": 178.0})
    _feed(appender, "activity_complete", {"elapsed_s": 178.0})
    for _ in range(4):
        _feed(appender, "text", {"text": "   "})  # whitespace: nothing to show
        _feed(appender, "system", {"text": ""})
        _feed(appender, "compaction_status", {"status": "started"})
    _feed(appender, "text", {"text": "next reply"})
    rows = _rows(console)
    assert _blank_run_after(rows, "Worked for 2m 58s") == 1
    assert _double_blanks(rows) == []


def test_a_block_that_ends_in_a_blank_and_one_that_opens_with_one_meet_in_one() -> None:
    appender, console = _appender()
    _feed(appender, "system", {"text": "first notice"})  # prints its own trailing blank
    appender._gap_pending = True  # and the next block is owed one as well
    _feed(appender, "system", {"text": "second notice"})
    rows = _rows(console)
    assert _blank_run_after(rows, "first notice") == 1
    assert _double_blanks(rows) == []


def test_the_first_blank_of_a_session_is_printed() -> None:
    appender, console = _appender()
    appender._gap_pending = True
    _feed(appender, "system", {"text": "hello"})
    assert _rows(console)[0] == ""  # nothing came before, so the owed blank still shows


def test_a_content_print_resets_the_guard() -> None:
    """A blank, content, a blank: the second blank is not swallowed by the first."""
    appender, console = _appender()
    appender._blank()
    appender._print("content")
    appender._blank()
    appender._blank()
    assert console.export_text().split("\n") == ["", "content", "", ""]


# ── the property: no two blank rows, whatever the events ─────────────────────

_KINDS = sorted(kind for kind in CASES if kind != "replay_separator")


@pytest.mark.parametrize("seed", range(40))
def test_no_mix_of_events_leaves_two_blank_rows_in_a_row(seed: int) -> None:
    rng = random.Random(seed)
    appender, console = _appender(width=60 + seed % 40)
    events: list[tuple[str, dict[str, object]]] = []
    for _ in range(rng.randint(6, 14)):
        kind = rng.choice(_KINDS)
        events.append((kind, rng.choice(CASES[kind])))
    for kind, payload in events:
        _feed(appender, kind, payload)
    rows = _rows(console)
    assert _double_blanks(rows) == [], [kind for kind, _ in events]


def test_every_kind_alone_followed_by_a_system_notice_has_no_double_blank() -> None:
    for kind, payloads in CASES.items():
        for payload in payloads:
            appender, console = _appender()
            _feed(appender, kind, payload)
            _feed(appender, "system", {"text": "after"})
            assert _double_blanks(_rows(console)) == [], kind
