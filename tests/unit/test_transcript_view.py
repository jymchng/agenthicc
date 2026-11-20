"""TUX-09 §2.1: the transcript view's keys, search, expansion and rendering."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, use_theme
from agenthicc.tui.transcript.sources import (
    DocumentSource,
    SessionLogSource,
    TextFileSource,
)
from agenthicc.tui.transcript.text import find_all, matches, plain_text
from agenthicc.tui.transcript.view import OutcomeKind, TranscriptView
from tests.transcript_support import (
    failed_tool,
    keys,
    scheduler_session,
    use_sessions_dir,
    write_session,
)

pytestmark = pytest.mark.unit

KEYMAP = Keymap.build(DEFAULT_ACTIONS)


def _view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, dict[str, object]]],
    **options: object,
) -> TranscriptView:
    use_sessions_dir(monkeypatch, tmp_path / "sessions")
    write_session(tmp_path / "sessions", "s", events)
    source = SessionLogSource("s")
    assert source.wait_ready(10)
    view = TranscriptView(source, KEYMAP, **options)  # type: ignore[arg-type]
    view.render(100, 24)
    return view


def _press(view: TranscriptView, *entries: tuple[Key, str]) -> None:
    for key, ch in entries:
        view.handle_key(key, ch)
    view.render(100, 24)


def _wait_for_search(view: TranscriptView) -> None:
    deadline = time.monotonic() + 10
    while view.job is not None and not view.job.done:
        assert time.monotonic() < deadline, "the search did not finish"
        time.sleep(0.01)
    view.tick()
    view.render(100, 24)


def _screen(view: TranscriptView, width: int = 100, height: int = 24) -> list[str]:
    return [row.plain for row in view.render(width, height)]


# ── AC 2: turn jumps and search on a 50-turn fixture ──────────────────────────


def test_a_bracket_moves_exactly_one_user_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(50))
    source = view.source
    turns = list(source.turn_items())
    assert len(turns) == 50
    _press(view, (Key.HOME, ""))
    assert view.selected == turns[0]
    for expected in turns[1:6]:
        _press(view, (Key.CHAR, "]"))
        assert view.selected == expected
    _press(view, (Key.CHAR, "["))
    assert view.selected == turns[4]
    # Past the last turn the selection stays and the footer says so.
    _press(view, (Key.END, ""))
    _press(view, (Key.CHAR, "]"))
    assert view.selected == turns[-1] or view.selected > turns[-1]
    assert "no later turn" in view.notice


def test_user_turns_are_numbered_from_one_in_the_gutter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(12))
    _press(view, (Key.HOME, ""))
    rows = _screen(view)
    first = next(row for row in rows if "turn 1: look at" in row)
    assert first[1:5].strip() == "1"
    _press(view, *keys("]]"))
    assert any(row[1:5].strip() == "3" and "turn 3:" in row for row in _screen(view))


def test_search_cycles_every_hit_in_order_and_the_count_matches_a_direct_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = scheduler_session(50)
    view = _view(tmp_path, monkeypatch, events)
    _press(view, (Key.HOME, ""))
    _press(view, (Key.CHAR, "/"), *keys("scheduler"), (Key.ENTER, ""))
    _wait_for_search(view)
    job = view.job
    assert job is not None and job.done

    # The direct scan: every item whose own text holds the query, in log order.
    expected = [
        index
        for index, (kind, payload) in enumerate(events)
        if matches("scheduler", plain_text(kind, payload))
    ]
    # Every item of this fixture prints, so log lines and item numbers coincide.
    assert job.hits == expected
    # 16 prompts, 50 ``read_file src/scheduler.py`` rows and 10 failing test commands.
    assert len(expected) == 16 + 50 + 10
    visited = [view.selected]
    for _ in range(len(job.hits) - 1):
        _press(view, (Key.CHAR, "n"))
        visited.append(view.selected)
    assert visited == job.hits  # in order, none skipped
    _press(view, (Key.CHAR, "n"))
    assert view.selected == job.hits[0]  # wraps
    _press(view, (Key.CHAR, "N"))
    assert view.selected == job.hits[-1]


def test_search_is_case_insensitive_unless_the_query_has_capitals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = [
        ("user_message", {"text": "the Scheduler fails"}),
        ("text", {"text": "scheduler again"}),
        ("text", {"text": "SCHEDULER shouting"}),
    ]
    view = _view(tmp_path, monkeypatch, events)
    _press(view, (Key.CHAR, "/"), *keys("scheduler"), (Key.ENTER, ""))
    _wait_for_search(view)
    assert view.job is not None and len(view.job.hits) == 3
    _press(view, (Key.CHAR, "/"), (Key.CTRL_U, ""), *keys("Scheduler"), (Key.ENTER, ""))
    _wait_for_search(view)
    assert view.job is not None and len(view.job.hits) == 1
    assert matches("abc", "xABCx") and not matches("Abc", "xABCx")
    assert find_all("ab", "abab AB") == [(0, 2), (2, 4), (5, 7)]


def test_search_covers_user_text_assistant_text_tool_summaries_and_loaded_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = [
        ("user_message", {"text": "needle in the prompt"}),
        ("text", {"text": "needle in the answer"}),
        ("tool_complete", {**failed_tool(10, command="grep needle src"), "output_lines": ["x"]}),
        ("tool_complete", {**failed_tool(10), "output_lines": ["first", "needle in the output"]}),
        ("text", {"text": "nothing here"}),
    ]
    view = _view(tmp_path, monkeypatch, events)
    _press(view, (Key.CHAR, "/"), *keys("needle"), (Key.ENTER, ""))
    _wait_for_search(view)
    assert view.job is not None and view.job.hits == [0, 1, 2, 3]


def test_hits_are_highlighted_in_the_accent_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, [("user_message", {"text": "find the scheduler"})])
    _press(view, (Key.CHAR, "/"), *keys("scheduler"), (Key.ENTER, ""))
    _wait_for_search(view)
    from agenthicc.tui.theme import palette

    rows = view.render(100, 24)
    row = next(row for row in rows if "scheduler" in row.plain)
    start = row.plain.index("scheduler")
    styles = {str(span.style) for span in row.spans if span.start <= start < span.end}
    assert any(palette().accent in style or "underline" in style for style in styles)


# ── AC 1 (part): expand ───────────────────────────────────────────────────────


def test_enter_expands_the_failed_command_to_its_full_stored_output_and_again_collapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = [
        ("user_message", {"text": "run the suite"}),
        ("tool_complete", failed_tool(30)),  # 30 lines: all stored
        ("text", {"text": "it failed"}),
    ]
    view = _view(tmp_path, monkeypatch, events)
    # Collapsed: the failure's last eight lines and the hint to expand it.
    collapsed = "\n".join(_screen(view, height=60))
    assert "output line 029" in collapsed and "output line 000" not in collapsed
    assert "enter to expand" in collapsed
    # Select the command, expand it: every stored line, then the arguments.
    _press(view, (Key.CHAR, "["), (Key.END, ""), (Key.UP, ""))
    assert view.source.kind(view.selected) == "tool_complete"
    _press(view, (Key.ENTER, ""))
    expanded = "\n".join(_screen(view, height=80))
    assert all(f"output line {i:03d}" in expanded for i in range(30))
    assert "[expanded]" in expanded
    assert "command: pytest tests/unit/test_scheduler.py -q" in expanded
    _press(view, (Key.ENTER, ""))
    assert "output line 000" not in "\n".join(_screen(view, height=80))


def test_expanded_output_is_cut_at_64_kib_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = {
        **failed_tool(30),
        "output_lines": ["x" * 1000] * 100,
        "output_head": 8,
        "output_total": 5000,
    }
    view = _view(tmp_path, monkeypatch, [("user_message", {"text": "go"}), ("tool_complete", huge)])
    _press(view, (Key.END, ""))
    _press(view, (Key.ENTER, ""))
    # Every line wraps (nothing is cropped); the whole of it, and nothing past 64 KiB.
    text = "\n".join(_screen(view, 100, 1_500))
    assert "output truncated at 64 KiB" in text
    assert 60_000 < text.count("x") <= 64 * 1024


# ── navigation ────────────────────────────────────────────────────────────────


def test_braces_jump_between_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(20))
    failures = list(view.source.attention_items())
    assert len(failures) == 4
    _press(view, (Key.HOME, ""))
    seen = []
    for _ in failures:
        _press(view, (Key.CHAR, "}"))
        seen.append(view.selected)
    assert seen == failures
    _press(view, (Key.CHAR, "{"))
    assert view.selected == failures[-2]


def test_paging_home_end_and_arrows_move_the_selection_and_keep_it_on_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(30))
    _press(view, (Key.HOME, ""))
    assert view.selected == 0
    for _ in range(3):
        _press(view, (Key.DOWN, ""))
    assert view.selected > 0
    before = view.selected
    _press(view, (Key.PAGE_DOWN, ""))
    assert view.selected > before
    assert any("▌" in row for row in _screen(view))
    _press(view, (Key.CHAR, "G"))
    assert view.selected == view.source.count() - 1
    _press(view, (Key.CHAR, "k"))
    assert view.selected == view.source.count() - 2
    _press(view, (Key.CHAR, "g"))
    assert view.selected == 0


def test_the_view_opens_at_the_end_and_rows_never_exceed_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(10))
    for width, height in ((40, 12), (80, 24), (200, 50)):
        rows = view.render(width, height)
        assert len(rows) == height
        assert all(row.cell_len <= width for row in rows)
    assert any("Done with turn 10." in row for row in _screen(view))


def test_close_keys_and_the_footer_come_from_the_keymap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(3))
    for key, ch in ((Key.CHAR, "q"), (Key.ESC, ""), (Key.CTRL_T, "")):
        assert view.handle_key(key, ch).kind is OutcomeKind.CLOSE
    footer = _screen(view)[-1]
    for fragment in ("↑↓ move", "[] turns", "/ search", "y copy", "e pager"):
        assert fragment in footer
    # Rebinding a key moves the action and its hint.
    custom = Keymap.build(DEFAULT_ACTIONS, overrides={"transcript_view.copy": ("c",)})
    view2 = TranscriptView(view.source, custom)
    assert "c copy" in view2.render(100, 24)[-1].plain
    assert view2.handle_key(Key.CHAR, "c").kind is OutcomeKind.COPY
    assert view2.handle_key(Key.CHAR, "y").kind is OutcomeKind.NONE


def test_copy_and_pager_carry_the_selected_items_plain_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(3))
    _press(view, (Key.HOME, ""))
    copy = view.handle_key(Key.CHAR, "y")
    assert copy.kind is OutcomeKind.COPY and copy.text == "turn 1: look at the parser"
    pager = view.handle_key(Key.CHAR, "e")
    assert pager.kind is OutcomeKind.PAGER and pager.text == copy.text
    assert "\x1b" not in copy.text


def test_the_wheel_scrolls_the_view_without_moving_off_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.tui.cbreak_reader import MouseEvent

    view = _view(tmp_path, monkeypatch, scheduler_session(30))
    top = view.top
    wheel = MouseEvent(64, 1, 1, pressed=True).encode()
    view.handle_key(Key.MOUSE, wheel)
    assert (view.top, view.top_skip) < (top, 99)
    assert view.top <= top


# ── what each item shows ──────────────────────────────────────────────────────


def test_compaction_results_render_as_rows_and_started_statuses_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = [
        ("user_message", {"text": "go on"}),
        ("compaction_status", {"status": "started", "before_tokens": 9000}),
        (
            "compaction_status",
            {
                "status": "compacted_summary",
                "before_tokens": 9000,
                "after_tokens": 849,
                "elapsed_s": 3.0,
                "label": "",
            },
        ),
        ("compaction_status", {"status": "token_count_unavailable"}),
        (
            "compaction_status",
            {
                "status": "blocked_irreducible_context",
                "elapsed_s": 2.0,
                "label": "Compaction blocked: no protocol-safe history boundary can fit",
            },
        ),
    ]
    view = _view(tmp_path, monkeypatch, events)
    # Only the two result rows are items; neither ``started`` nor the missing
    # token count is, and no row is raw JSON.
    assert view.source.count() == 3
    text = "\n".join(_screen(view))
    assert "Context refreshed" in text and "849 tokens" in text
    assert "Context refresh failed" in text and "no protocol-safe history boundary" in text
    assert "{" not in text and "compaction_status" not in text
    assert view.source.index.attention_items[0] == 2


def test_events_the_scrollback_prints_nothing_for_are_not_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = [
        ("user_message", {"text": "hello"}),
        ("tokens", {"input_tokens": 10, "output_tokens": 5}),
        ("prompt_cache", {"hit": True}),
        ("text", {"text": "hi"}),
    ]
    view = _view(tmp_path, monkeypatch, events)
    assert view.source.count() == 2


def test_hidden_reasoning_and_private_fields_never_reach_search_copy_or_rows() -> None:
    payload: dict[str, object] = {
        "text": "visible",
        "reasoning_content": "hidden thoughts",
        "_private": "private",
        "analysis": "secret analysis",
    }
    assert plain_text("text", payload) == "visible"
    assert plain_text("unknown_kind", {"reasoning": "no", "detail": "yes"}) == "yes"


def test_an_event_in_the_old_log_format_is_migrated_before_it_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = {
        "name": "run_bash",
        "success": True,
        "args_str": "[dim](ls -la)[/dim]",
        "dur_str": "12ms",
        "output_lines": ["a", "b"],
    }
    view = _view(tmp_path, monkeypatch, [("tool_complete", legacy)])
    text = "\n".join(_screen(view))
    assert "ls -la" in text and "[dim]" not in text


# ── /rewind and /logs modes ───────────────────────────────────────────────────


def test_the_rewind_view_selects_user_turns_only_and_enter_names_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(6), rewind=True)
    turns = list(view.source.turn_items())
    assert view.selected == turns[-1]
    _press(view, (Key.UP, ""))
    assert view.selected == turns[-2]
    _press(view, (Key.HOME, ""))
    assert view.selected == turns[0]
    outcome = view.handle_key(Key.ENTER, "")
    assert outcome.kind is OutcomeKind.REWIND
    assert outcome.turn == 1 and outcome.text == "turn 1: look at the parser"
    assert "rewind here" in _screen(view)[-1]
    # Expansion is not offered while rewinding.
    assert "expand" not in _screen(view)[-1]


def test_the_log_view_has_lines_and_no_turn_structure(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    path.write_text(
        "starting\nWARNING careful now\nplain line\nERROR it broke\nTraceback (most recent)\n"
    )
    source = TextFileSource(path)
    assert source.wait_ready(5) and source.count() == 5
    view = TranscriptView(source, KEYMAP, subtitle=str(path))
    rows = _screen(view)
    assert "Log" in rows[0] and "turns" not in rows[0]
    assert "[] turns" not in rows[-1]
    assert list(source.attention_items()) == [1, 3, 4]
    _press(view, (Key.HOME, ""), (Key.CHAR, "}"))
    assert view.selected == 1
    _press(view, (Key.CHAR, "]"))  # no turns: the key does nothing
    assert view.selected == 1
    _press(view, (Key.CHAR, "/"), *keys("broke"), (Key.ENTER, ""))
    _wait_for_search(view)
    assert view.selected == 3
    assert view.handle_key(Key.CHAR, "y").text == "ERROR it broke"
    source.close()


def test_a_log_that_grows_is_followed_while_the_selection_is_at_its_end(tmp_path: Path) -> None:
    path = tmp_path / "grow.log"
    path.write_text("one\ntwo\n")
    source = TextFileSource(path)
    assert source.wait_ready(5)
    view = TranscriptView(source, KEYMAP)
    view.render(60, 12)
    assert view.selected == 1
    with path.open("a") as handle:
        handle.write("three\nfour\n")
    assert view.tick() is True
    assert view.selected == 3
    assert any("four" in row for row in _screen(view, 60, 12))
    source.close()


def test_a_document_is_one_item_per_row_and_copies_whole() -> None:
    from rich.text import Text

    source = DocumentSource("Edit a file", [Text("--- a/x\n+++ b/x\n-old\n+new")])
    view = TranscriptView(source, KEYMAP, subtitle="Edit a file", start="start")
    rows = _screen(view, 60, 14)
    assert "-old" in "\n".join(rows) and "+new" in "\n".join(rows)
    assert view.handle_key(Key.CHAR, "y").text == "--- a/x\n+++ b/x\n-old\n+new"


# ── rendering rules ───────────────────────────────────────────────────────────


def test_the_ascii_glyph_set_keeps_every_row_ascii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    theme = BUILTIN_THEMES["dark"]
    use_theme(replace(theme, glyphs=ASCII_GLYPHS))
    view = _view(tmp_path, monkeypatch, scheduler_session(6))
    _press(view, (Key.CHAR, "["), (Key.END, ""), (Key.UP, ""))
    for row in _screen(view):
        assert row.isascii(), row
    _press(view, (Key.CHAR, "/"), *keys("scheduler"))
    assert all(row.isascii() for row in _screen(view))


def test_a_theme_change_restyles_the_view_at_the_next_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(tmp_path, monkeypatch, scheduler_session(4))
    dark = [[str(span.style) for span in row.spans] for row in view.render(100, 24)]
    use_theme(BUILTIN_THEMES["light"])
    light = [[str(span.style) for span in row.spans] for row in view.render(100, 24)]
    assert dark != light


def test_long_lines_wrap_in_the_view_and_the_scrollback_still_cuts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view's own text wraps (nothing is cut with an ellipsis); the scrollback keeps its rows."""
    long_line = "E   AssertionError: " + " ".join(f"word{i}" for i in range(40))
    command = "pytest tests/unit/test_scheduler.py -q -k " + "very_long_test_name_" * 6
    failing = {
        **failed_tool(3, command=command),
        "output_lines": ["short", long_line],
        "output_head": 2,
        "output_total": 2,
        "args": {"command": command + "\nsecond line of the script"},
    }
    events: list[tuple[str, dict[str, object]]] = [
        ("user_message", {"text": "run it"}),
        ("tool_complete", failing),
    ]
    view = _view(tmp_path, monkeypatch, events)
    view.render(60, 40)
    for expanded in (False, True):
        if expanded:
            _press(view, (Key.END, ""), (Key.ENTER, ""))
        rows = [row.plain for row in view.render(60, 40)]
        flat = " ".join(" ".join(rows).split())
        assert "…" not in flat and all(f"word{i}" in flat for i in range(40))
        # The command folds mid-word at the width; read back without the gutter,
        # the indent and the right-hand "exit 1 · 3.2s", it is whole.
        solid = "".join("".join(rows).replace("▌", "").split()).replace("exit1·3.2s", "")
        assert "".join(command.split()) in solid
    text = "\n".join(rows)
    assert "second line of the script" in text  # a multi-line argument keeps its lines
    assert all(len(row) <= 60 for row in rows)

    # The scrollback prints the same event as one row per line, cut with an ellipsis.
    from io import StringIO

    from rich.console import Console

    from agenthicc.tui.conversation_store import AppState, ConversationEvent
    from agenthicc.tui.workspace.appender import ScrollBufferAppender

    console = Console(file=StringIO(), width=60, record=True, color_system=None)
    appender = ScrollBufferAppender(AppState.create(), console)
    appender.print_events([ConversationEvent("e", "tool_complete", dict(failing), 1.0)])
    printed = console.export_text().splitlines()
    assert all(len(line) <= 60 for line in printed) and any("…" in line for line in printed)


def test_a_long_user_message_is_condensed_until_it_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "\n".join(f"pasted line {i}" for i in range(30))
    view = _view(tmp_path, monkeypatch, [("user_message", {"text": message})])
    collapsed = "\n".join(_screen(view, 100, 50))
    assert "pasted line 3" in collapsed and "pasted line 4" not in collapsed
    assert "[+26 more lines]" in collapsed and "Enter expand" in collapsed
    _press(view, (Key.ENTER, ""))
    expanded = "\n".join(_screen(view, 100, 50))
    assert all(f"pasted line {i}" in expanded for i in range(30))
    assert "more lines]" not in expanded
