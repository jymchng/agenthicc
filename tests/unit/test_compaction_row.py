"""The inline compaction indicator: ring, time, optional bar, result row.

``◜ Refreshing context · 2s`` while the conversation is compacted, a thin bar
under it only when the compaction measured real progress, and
``✓ Context refreshed · 849 tokens · 3s`` (or ``✗ Context refresh failed · 3s``)
in the transcript when it ends.
"""

from __future__ import annotations

import time
from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text

from agenthicc.config import UISettings
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, glyphs, palette, use_theme
from agenthicc.tui.workspace.announcer import Announcer
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.running_rows import (
    BAR_WIDTH,
    RING_FRAMES,
    compaction_bar,
    compaction_row,
    running_rows,
)

pytestmark = pytest.mark.unit


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def _compacting(*, seconds: float = 0.0) -> AppState:
    state = AppState.create()
    state.conversation.begin_compaction()
    state.conversation._compaction_started_at = time.monotonic() - seconds
    return state


def _frame(state: AppState, frame: int) -> str:
    state.conversation.frame.set(frame)
    return _plain(compaction_row(state))


# ── the spinner row ──────────────────────────────────────────────────────────


def test_the_row_reads_ring_label_and_elapsed_time() -> None:
    state = _compacting(seconds=2.4)
    assert _plain(compaction_row(state)) == "◜ Refreshing context · 2s"


def test_the_ring_turns_once_per_second_at_the_frame_clock() -> None:
    state = _compacting()
    ring = glyphs().ring
    shown = [_frame(state, frame)[0] for frame in range(RING_FRAMES)]
    # Every arc appears, in order, and the turn then starts again.
    assert list(dict.fromkeys(shown)) == list(ring)
    assert _frame(state, RING_FRAMES)[0] == _frame(state, 0)[0]
    # 20 ticks at 20 Hz: a rotation takes a second, inside 800-1200 ms.
    assert RING_FRAMES / 20 == pytest.approx(1.0)


def test_the_ring_wears_the_accent_and_the_time_the_muted_token() -> None:
    state = _compacting(seconds=1)
    console = Console(file=StringIO(), record=True, width=60, color_system="truecolor")
    console.print(compaction_row(state))
    styled = console.export_text(styles=True)
    red, green, blue = (int(palette().accent.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    assert f"38;2;{red};{green};{blue}" in styled


def test_the_time_shows_from_zero_so_the_row_never_changes_width() -> None:
    state = _compacting(seconds=0)
    widths = {
        len(_plain(compaction_row(state, monotonic_now=time.monotonic() + n))) for n in range(9)
    }
    assert widths == {len("◜ Refreshing context · 0s")}


def test_the_time_uses_minutes_after_a_minute() -> None:
    assert _plain(compaction_row(_compacting(seconds=71))).endswith("· 1m 11s")


@pytest.mark.parametrize(
    "settings", [UISettings(reduced_motion=True), UISettings(screen_reader=True)]
)
def test_reduced_motion_and_screen_readers_keep_the_ring_at_rest(settings: UISettings) -> None:
    state = _compacting()
    state.ui.set(settings)
    rows = {_frame(state, frame) for frame in range(RING_FRAMES)}
    assert rows == {"◌ Refreshing context · 0s"}


def test_the_ascii_ring_draws_every_frame_including_the_backslash() -> None:
    """A ``\\`` before the closing tag would have swallowed it as markup."""
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    state = _compacting()
    shown = [_frame(state, frame) for frame in (0, 5, 10, 15)]
    assert shown == [
        "| Refreshing context - 0s",
        "/ Refreshing context - 0s",
        "- Refreshing context - 0s",
        "\\ Refreshing context - 0s",
    ]


def test_the_row_stands_in_the_running_rows_framed_by_blank_rows() -> None:
    state = _compacting(seconds=1)
    rows = running_rows(state, 80)
    assert [_plain(row) for row in rows] == ["", "◜ Refreshing context · 1s", ""]


def test_nothing_is_drawn_when_no_compaction_runs() -> None:
    assert running_rows(AppState.create(), 80) == []


def test_the_row_count_does_not_change_while_the_compaction_runs() -> None:
    """No layout shift: the same rows at every frame and every second."""
    state = _compacting()
    counts = set()
    for second in range(4):
        state.conversation._compaction_started_at = time.monotonic() - second
        for frame in range(0, 40, 3):
            state.conversation.frame.set(frame)
            counts.add(len(running_rows(state, 80)))
    assert counts == {3}


# ── the progress bar ─────────────────────────────────────────────────────────


def test_there_is_no_bar_without_measured_progress() -> None:
    state = _compacting(seconds=30)
    for frame in range(0, 400, 7):
        state.conversation.frame.set(frame)
        assert compaction_bar(state, 80) is None
    assert len(running_rows(state, 80)) == 3  # the spinner alone, however long it takes


def test_the_bar_follows_the_calls_that_really_finished() -> None:
    state = _compacting()
    conv = state.conversation
    width = BAR_WIDTH
    seen: list[str] = []
    for done in range(0, 7):
        conv.report_compaction_progress(done, 6)
        bar = compaction_bar(state, 80)
        assert bar is not None
        seen.append(_plain(bar))
    assert all(len(row) == width for row in seen)
    fills = [row.count(glyphs().progress_fill) for row in seen]
    assert fills == [0, 5, 10, 15, 20, 25, 30]
    assert seen[0] == glyphs().progress_track * width
    assert seen[-1] == glyphs().progress_fill * width


def test_a_half_cell_step_ends_the_bar_on_the_half_glyph() -> None:
    state = _compacting()
    state.conversation.report_compaction_progress(1, 60)  # 0.5 cell of 30
    assert _plain(compaction_bar(state, 80) or "").startswith(glyphs().progress_half)


def test_the_bar_never_moves_back_when_more_calls_turn_out_to_be_needed() -> None:
    state = _compacting()
    conv = state.conversation
    conv.report_compaction_progress(5, 6)
    high = conv.compaction_progress()
    assert high is not None
    conv.report_compaction_progress(5, 10)  # another round of summaries was needed
    after = conv.compaction_progress()
    assert after is not None and after.fraction == high.fraction
    assert (after.done, after.total) == (5, 10)  # the counts stay the truth
    conv.report_compaction_progress(10, 10)
    final = conv.compaction_progress()
    assert final is not None and final.fraction == 1.0


def test_the_bar_has_a_muted_track_and_an_accent_fill_and_no_percentage() -> None:
    state = _compacting()
    state.conversation.report_compaction_progress(3, 6)
    bar = compaction_bar(state, 80)
    assert bar is not None
    assert "%" not in _plain(bar) and not any(ch.isdigit() for ch in _plain(bar))
    console = Console(file=StringIO(), record=True, width=60, color_system="truecolor")
    console.print(bar)
    styled = console.export_text(styles=True)
    accent = [int(palette().accent.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    muted = palette().muted
    assert f"38;2;{accent[0]};{accent[1]};{accent[2]}" in styled
    assert muted != palette().accent
    assert glyphs().progress_track in _plain(bar)


def test_the_bar_sits_under_the_row_and_shrinks_on_a_narrow_terminal() -> None:
    state = _compacting()
    state.conversation.report_compaction_progress(2, 4)
    rows = [_plain(row) for row in running_rows(state, 80)]
    assert rows[1].startswith("◜ Refreshing context")
    assert len(rows[2]) == BAR_WIDTH
    narrow = compaction_bar(state, 20)
    assert narrow is not None and len(_plain(narrow)) == 18
    assert compaction_bar(state, 8) is None  # too narrow to mean anything


def test_the_ascii_bar_uses_plain_characters() -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    state = _compacting()
    state.conversation.report_compaction_progress(3, 6)
    bar = _plain(compaction_bar(state, 80) or "")
    assert set(bar) <= {"=", "-", "."} and bar.startswith("=")


def test_progress_with_no_total_is_ignored() -> None:
    state = _compacting()
    state.conversation.report_compaction_progress(0, 0)
    assert state.conversation.compaction_progress() is None


# ── the store's compaction clock ─────────────────────────────────────────────


def test_begin_and_end_drive_the_active_flag_and_clear_progress() -> None:
    conv = AppState.create().conversation
    conv.begin_compaction()
    conv.report_compaction_progress(1, 3)
    assert conv.compaction_active() and conv.compaction_progress() is not None
    conv.end_compaction()
    assert not conv.compaction_active() and conv.compaction_progress() is None


def test_the_elapsed_time_outlives_the_row_so_the_result_can_state_it() -> None:
    conv = AppState.create().conversation
    assert conv.compaction_elapsed() == 0.0
    conv.begin_compaction()
    conv._compaction_started_at = time.monotonic() - 3
    conv.end_compaction()
    assert conv.compaction_elapsed() == pytest.approx(3, abs=0.2)


def test_each_compaction_restarts_the_clock_and_progress() -> None:
    conv = AppState.create().conversation
    conv.begin_compaction()
    conv._compaction_started_at = time.monotonic() - 9
    conv.report_compaction_progress(2, 3)
    conv.end_compaction()
    conv.begin_compaction()
    assert conv.compaction_elapsed() < 1 and conv.compaction_progress() is None


def test_the_tick_publishes_whole_seconds_only_when_they_change() -> None:
    conv = AppState.create().conversation
    seen: list[int] = []
    conv.compaction_seconds.subscribe(lambda: seen.append(conv.compaction_seconds()))
    conv.begin_compaction()
    conv._compaction_started_at = time.monotonic() - 0.2
    for _ in range(10):
        conv.tick()
    assert seen == []  # still inside the first second: no redraw
    conv._compaction_started_at = time.monotonic() - 1.2
    for _ in range(10):
        conv.tick()
    assert seen == [1]
    conv.end_compaction()
    conv.tick()
    assert seen == [1]  # a finished compaction publishes nothing


def test_the_frame_clock_runs_during_a_compaction() -> None:
    conv = AppState.create().conversation
    conv.begin_compaction()
    before = conv.frame()
    conv.tick()
    assert conv.frame() == before + 1


# ── the result row ───────────────────────────────────────────────────────────


def _appender(state: AppState | None = None) -> tuple[ScrollBufferAppender, Console]:
    console = Console(file=StringIO(), record=True, width=80, color_system=None, highlight=False)
    return ScrollBufferAppender(state or AppState.create(), console), console


def _status(appender: ScrollBufferAppender, **payload: object) -> None:
    appender._render_one(
        ConversationEvent(event_id="e", kind="compaction_status", payload=payload, timestamp=0.0)
    )


def test_a_successful_compaction_ends_in_a_check_row_with_tokens_and_time() -> None:
    appender, console = _appender()
    _status(appender, status="compacted_summary", after_tokens=849, elapsed_s=3.2)
    assert console.export_text().splitlines() == [" ✓ Context refreshed · 849 tokens · 3s"]


def test_a_failed_compaction_ends_in_a_cross_row_with_the_reason_underneath() -> None:
    appender, console = _appender()
    _status(
        appender,
        status="blocked_irreducible_context",
        elapsed_s=3.4,
        label="Compaction blocked: no protocol-safe history boundary can fit",
    )
    assert console.export_text().splitlines() == [
        " ✗ Context refresh failed · 3s",
        "   Compaction blocked: no protocol-safe history boundary can fit",
    ]


@pytest.mark.parametrize(
    "status",
    [
        "skipped_summary_unavailable",
        "skipped_concurrent_update",
        "blocked_durability_error",
        "blocked_irreducible_context",
    ],
)
def test_every_unsuccessful_status_is_a_failure_row(status: str) -> None:
    appender, console = _appender()
    _status(appender, status=status, elapsed_s=1.0)
    assert console.export_text().splitlines() == [" ✗ Context refresh failed · 1s"]


def test_a_summary_that_fell_back_is_a_success_that_says_so() -> None:
    appender, console = _appender()
    _status(
        appender,
        status="compacted_local_fallback",
        after_tokens=12_400,
        elapsed_s=71,
        label="Compaction fallback: retained recent complete exchanges",
    )
    assert console.export_text().splitlines() == [
        " ✓ Context refreshed · 12,400 tokens · 1m 11s",
        "   Compaction fallback: retained recent complete exchanges",
    ]


def test_the_start_and_advisory_statuses_print_nothing() -> None:
    appender, console = _appender()
    _status(appender, status="started", before_tokens=9000)
    _status(appender, status="token_count_unavailable", error_type="Timeout")
    assert console.export_text() == ""


def test_an_event_from_an_older_log_without_a_time_shows_no_time() -> None:
    appender, console = _appender()
    _status(appender, status="compacted_summary", after_tokens=743)
    assert console.export_text().splitlines() == [" ✓ Context refreshed · 743 tokens"]


def test_the_start_event_does_not_use_up_the_blank_line_a_block_owes() -> None:
    appender, console = _appender()
    appender._gap_pending = True
    _status(appender, status="started", before_tokens=9000)
    assert appender._gap_pending is True
    _status(appender, status="compacted_summary", after_tokens=5, elapsed_s=1)
    assert console.export_text().splitlines() == ["", " ✓ Context refreshed · 5 tokens · 1s"]


def test_a_blank_line_follows_the_result_row() -> None:
    appender, console = _appender()
    _status(appender, status="compacted_summary", after_tokens=5, elapsed_s=1)
    appender._render_one(
        ConversationEvent(event_id="n", kind="system", payload={"text": "next"}, timestamp=0.0)
    )
    assert console.export_text().splitlines()[:3] == [
        " ✓ Context refreshed · 5 tokens · 1s",
        "",
        " · next",
    ]


def test_screen_readers_get_words_not_glyphs() -> None:
    state = AppState.create()
    state.ui.set(UISettings(screen_reader=True))
    appender, console = _appender(state)
    _status(appender, status="compacted_summary", after_tokens=849, elapsed_s=3)
    _status(appender, status="skipped_summary_unavailable", elapsed_s=2)
    lines = console.export_text().splitlines()
    assert lines[0] == " Done: Context refreshed · 849 tokens · 3s"
    assert " Error: Context refresh failed · 2s" in lines


def test_ascii_glyphs_draw_plain_markers() -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    appender, console = _appender()
    _status(appender, status="compacted_summary", after_tokens=849, elapsed_s=3)
    _status(appender, status="skipped_summary_unavailable", elapsed_s=2)
    assert console.export_text().splitlines() == [
        " + Context refreshed - 849 tokens - 3s",
        "",
        " x Context refresh failed - 2s",
    ]


def test_a_system_line_from_an_older_log_still_renders() -> None:
    appender, console = _appender()
    appender._render_one(
        ConversationEvent(
            event_id="old",
            kind="system",
            payload={"text": "Compacted → ~743 tokens"},
            timestamp=0.0,
        )
    )
    assert console.export_text().splitlines()[0] == " · Compacted → ~743 tokens"


# ── screen-reader announcement ───────────────────────────────────────────────


def test_a_screen_reader_hears_that_the_context_is_being_refreshed_once() -> None:
    state = AppState.create()
    appender, _console = _appender(state)
    said: list[str] = []
    appender.announce = said.append  # type: ignore[method-assign]
    announcer = Announcer(state, appender)
    announcer.start()
    try:
        said.clear()  # the status line's own "Idle." on start
        state.conversation.begin_compaction()
        state.conversation.end_compaction()
        state.conversation.begin_compaction()  # a second compaction is announced again
        state.conversation.end_compaction()
    finally:
        announcer.stop()
    assert said.count("Refreshing context.") == 2
