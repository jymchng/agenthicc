"""The agents hub wraps content; it never crops it.

A long option, reply, command or error used to be cut at the panel's edge: the
answer form showed ``…the shell stays l``, a reply showed its first line, an
approval showed the first 240 characters of a command. These tests hold the
rule for every content view: each word of the source is in the output, in
order, and no line is wider than the panel.
"""

from __future__ import annotations

import time

import pytest
from rich.cells import cell_len
from rich.text import Text

from agenthicc.background.inbox import BackgroundInboxItem
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.tui.workspace.agents_hub.answer import AnswerView, form_for_item
from agenthicc.tui.workspace.agents_hub.detail import (
    MESSAGE_CHARS,
    ChangeSummary,
    DetailData,
    TranscriptLine,
    approval_lines,
    transcript_line,
)
from agenthicc.tui.workspace.agents_hub.render import detail_lines, peek_lines
from agenthicc.tui.workspace.agents_hub.wrapping import (
    fold_word,
    hanging,
    wrap_cells,
)

pytestmark = pytest.mark.unit

WIDTHS = (24, 30, 40, 57, 58, 64, 80, 100, 140)
NOW = time.time()


def _words(text: str) -> list[str]:
    return text.split()


def _plain(lines: list[Text]) -> list[str]:
    return [line.plain for line in lines]


def assert_whole(source: str, rendered: list[str]) -> None:
    """Every character of *source* (spaces aside) is in *rendered*, in order."""
    want = "".join(source.split())
    have = "".join("".join(rendered).split())
    assert want in have, f"text was lost:\n  want {want[:120]}\n  have {have[:120]}"


# ── wrap_cells ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("width", WIDTHS)
def test_wrapped_lines_fit_and_keep_every_word(width: int) -> None:
    text = (
        "A — One screen, interleaved (recommended): the shell stays live, and Run "
        "output prints into the same terminal you are typing in, so nothing is hidden."
    )
    lines = wrap_cells(text, width)
    assert all(cell_len(line) <= width for line in lines)
    assert _words(" ".join(lines)) == _words(text)


@pytest.mark.parametrize("width", WIDTHS)
def test_a_path_longer_than_the_line_is_folded_not_cut(width: int) -> None:
    path = (
        "/root/typescript_projects/pyapps/research/responsive/_shots/all-widths-sweep-report.json"
    )
    lines = wrap_cells(f"wrote {path} today", width)
    assert all(cell_len(line) <= width for line in lines)
    assert "".join("".join(lines).split()) == "".join(f"wrote {path} today".split())


def test_a_long_word_breaks_after_a_separator_when_one_is_near() -> None:
    pieces = fold_word("/root/typescript_projects/pyapps/research/responsive/_shots", 30)
    assert pieces[0] == "/root/typescript_projects/"
    assert "".join(pieces) == "/root/typescript_projects/pyapps/research/responsive/_shots"
    assert all(cell_len(piece) <= 30 for piece in pieces)


def test_a_word_with_no_separator_is_folded_at_the_width() -> None:
    assert fold_word("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]


def test_wide_characters_count_two_cells() -> None:
    text = "界" * 20 + " " + "世界" * 10
    for width in (11, 20, 33):
        lines = wrap_cells(text, width)
        assert all(cell_len(line) <= width for line in lines), (width, lines)
        assert "".join("".join(lines).split()) == "".join(text.split())


def test_line_breaks_and_paragraphs_are_kept() -> None:
    assert wrap_cells("first\n\nsecond   line\nthird", 40) == ["first", "", "second line", "third"]


def test_text_with_no_content_is_one_empty_line() -> None:
    assert wrap_cells("", 20) == [""]
    assert wrap_cells("   \n  \n", 20) == [""]


# ── hanging ──────────────────────────────────────────────────────────────────


def test_continuation_lines_sit_under_the_text_not_under_the_lead() -> None:
    lead = Text("   ✓ assistant ")
    lines = _plain(hanging(lead, "one two three four five six seven eight nine ten", 32))
    assert lines[0].startswith("   ✓ assistant one")
    assert all(line.startswith(" " * cell_len(lead.plain)) for line in lines[1:])
    assert all(cell_len(line) <= 32 for line in lines)


def test_a_cap_counts_exactly_the_lines_it_left_out() -> None:
    body = " ".join(["word"] * 100)
    full = hanging(Text("  "), body, 30)
    capped = hanging(Text("  "), body, 30, max_lines=4)
    assert len(capped) == 5
    assert _plain(capped[:4]) == _plain(full[:4])
    assert capped[4].plain.strip() == f"… {len(full) - 4} more lines"
    assert hanging(Text("  "), "short", 30, max_lines=4)[-1].plain == "  short"


def test_one_hidden_line_is_not_plural() -> None:
    body = " ".join(["word"] * 12)
    lines = hanging(Text(""), body, 20, max_lines=len(hanging(Text(""), body, 20)) - 1)
    assert lines[-1].plain.strip() == "… 1 more line"


@pytest.mark.parametrize("width", (14, 16, 20, 24))
def test_a_narrow_panel_shrinks_the_indent_before_the_words(width: int) -> None:
    lead = Text("   ✓ assistant ")  # 15 cells: no room beside it at these widths
    body = "the sweep works but the heuristic is catching links"
    lines = _plain(hanging(lead, body, width))
    assert all(cell_len(line) <= width for line in lines), lines
    assert "".join("".join(lines[1:]).split()) == "".join(body.split())
    assert lines[0].strip() == "✓ assistant"  # the lead stands alone when nothing fits beside it


# ── the answer form ──────────────────────────────────────────────────────────

OPTIONS = [
    "A — One screen, interleaved (recommended): the shell stays live, and Run output "
    "prints into the same terminal you are typing in",
    "B — One panel, two screens with an Output / Shell toggle: hopping between them "
    "keeps each one's own scrollback",
    "C — Only the shell: run everything (including the Run button) inside the terminal, "
    "so there is no separate output view",
]


def _inbox_item(options: list[str], question: str = "Which do you want?") -> BackgroundInboxItem:
    return BackgroundInboxItem(
        item_id="i",
        session_id="s",
        attempt=1,
        request_id="r",
        kind="question",
        created_at=NOW,
        expires_at=None,
        target={"questions": [{"id": "q", "text": question, "options": options}]},
        status="pending",
        version=1,
        required_scope="",
        project_root="",
        workspace="",
    )


def _answer(options: list[str] = OPTIONS, **kwargs: object) -> AnswerView:
    item = _inbox_item(options)
    return AnswerView(item, form_for_item(item), title="AsyncMove PlayGround Linux", **kwargs)  # type: ignore[arg-type]


def _answer_lines(view: AnswerView, width: int, rows: int | None = None) -> list[Text]:
    return [row for row in view.render(width, rows=rows) if isinstance(row, Text)]


def _option_rows(view: AnswerView, width: int, rows: int | None = None) -> list[str]:
    """The rows between the question and the hint: the options, as the form built them.

    The header and the hint are one-line chrome that ends in an ellipsis on a
    narrow panel; the options are content and must fit by wrapping.
    """
    lines = [row.plain for row in _answer_lines(view, width, rows)]
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("   ") and "▌" in line or line.startswith("     ")
    )
    return lines[start:-1]


@pytest.mark.parametrize("width", WIDTHS)
def test_every_option_of_a_question_is_shown_whole(width: int) -> None:
    rows = _option_rows(_answer(), width)
    assert all(cell_len(row) <= width for row in rows), rows
    for option in OPTIONS:
        assert_whole(option, rows)


def test_the_report_from_the_field_is_no_longer_cut_at_the_edge() -> None:
    """``A — One screen, interleaved (recommended): the shell stays l`` at 64 columns."""
    rows = _option_rows(_answer(), 64)
    flat = " ".join(" ".join(rows).split())
    assert "(recommended): the shell stays live, and Run output" in flat
    assert flat.count("typing in") == 1
    first = next(i for i, row in enumerate(rows) if "One screen, interleaved" in row)
    assert rows[first + 1].startswith("     ")  # continues under the option's own text


def test_the_highlight_covers_every_row_of_the_option_under_the_cursor() -> None:
    from agenthicc.tui.theme import palette

    lines = _answer_lines(_answer(), 50)
    background = palette().on("accent_bg")
    rows = [line for line in lines if any(str(span.style) == background for span in line.spans)]
    first_option = " ".join(row.plain for row in rows)
    assert "stays live" in first_option and "typing in" in first_option
    assert len(rows) >= 3
    # The other options are not highlighted.
    assert not any("Only the shell" in row.plain for row in rows)


def test_an_option_that_was_chosen_keeps_its_check_on_the_first_row_only() -> None:
    view = _answer()
    view.form.state.label = OPTIONS[1]
    view.form.state.answered = True
    lines = _option_rows(view, 50)
    chosen = [i for i, line in enumerate(lines) if "B — One panel" in line][0]
    assert "✓" in lines[chosen]
    assert "✓" not in lines[chosen + 1]


def _many(count: int) -> list[str]:
    return [
        f"Option number {n}: " + "a rather long description of what it does " * 3
        for n in range(count)
    ]


@pytest.mark.parametrize("cursor", (0, 4, 9, 10))
def test_options_scroll_around_the_cursor_when_the_panel_is_short(cursor: int) -> None:
    options = _many(10)  # the form adds "Other", so there are 11 rows to choose from
    view = _answer(options)
    view.form.state.cursor = cursor
    lines = [line.plain for line in _answer_lines(view, 50, rows=16)]
    assert len(lines) <= 16
    cursor_row = next(line for line in lines if "▌" in line)
    assert (f"Option number {cursor}:" in cursor_row) or (cursor == 10 and "Other" in cursor_row)
    assert lines[-1].lstrip().startswith(("↑↓", "^v"))  # the hint is still the last row
    texts = [f"Option number {n}:" for n in range(10)] + ["Other — type"]
    shown = [n for n, text in enumerate(texts) if any(text in line for line in lines)]
    assert shown == list(range(min(shown), max(shown) + 1))  # whole options, no gaps
    up = [line for line in lines if line.strip().startswith("↑ ")]
    down = [line for line in lines if line.strip().startswith("↓ ")]
    assert (int(up[0].split()[1]) if up else 0) == min(shown)
    assert (int(down[0].split()[1]) if down else 0) == len(texts) - 1 - max(shown)


def test_an_option_taller_than_the_panel_is_cut_to_fit_not_the_hint() -> None:
    view = _answer(["x " * 200, "short"])
    lines = [line.plain for line in _answer_lines(view, 40, rows=12)]
    assert len(lines) <= 12 and lines[-1].lstrip().startswith(("↑↓", "^v"))
    assert any("▌" in line for line in lines)  # the cursor is still on screen


def test_with_room_nothing_scrolls_and_no_more_rows_appear() -> None:
    lines = [line.plain for line in _answer_lines(_answer(), 80, rows=60)]
    assert not any(line.strip().startswith(("↑ ", "↓ ")) for line in lines)


def test_a_typed_other_answer_is_shown_in_full_up_to_a_marked_limit() -> None:
    view = _answer(["yes", "no"])
    typed = "word " * 30
    view.form.answer_typed(typed, typed)
    lines = _option_rows(view, 50)
    assert " ".join(lines).count("word") == 30


# ── approvals, transcripts and details ───────────────────────────────────────


def _session(**kwargs: object) -> BackgroundSession:
    return BackgroundSession.create(
        "s", title="AsyncMove PlayGround Linux", cwd="/tmp/p", workflow_name="goal_flow", intent="x"
    ).evolve(status=SessionStatus.RUNNING, attempt=1, **kwargs)  # type: ignore[arg-type]


def _approval(command: str) -> BackgroundInboxItem:
    return BackgroundInboxItem(
        item_id="i",
        session_id="s",
        attempt=1,
        request_id="r",
        kind="approval",
        created_at=NOW,
        expires_at=None,
        target={"tool_name": "run_bash", "tool_input": {"command": command}},
        status="pending",
        version=1,
        required_scope="",
        project_root="",
        workspace="",
    )


LONG_COMMAND = (
    "cd /root/typescript_projects/pyapps && timeout 300 python3 research/responsive/tools/resp.py "
    "--out research/responsive/_shots --widths 320,360,390,414,768,1024,1280,1440,1920 "
    "--bisect 1px && echo done"
)


@pytest.mark.parametrize("width", WIDTHS)
def test_an_approval_shows_the_whole_command_it_asks_about(width: int) -> None:
    item = _approval(LONG_COMMAND)
    detail = _plain(detail_lines(_session(), None, item, width, now=NOW).lines)
    assert all(cell_len(line) <= width for line in detail)
    assert_whole(LONG_COMMAND, detail)  # the detail view never abbreviates a request
    peek = _plain(peek_lines(_session(), None, item, width, now=NOW))
    assert all(cell_len(line) <= width for line in peek)
    if any("more line" in line for line in peek):
        # A narrow panel may keep only the start of a long command in the list, and
        # then it says so (Enter opens the whole request).
        assert_whole(LONG_COMMAND[:30], peek) if width >= 40 else None
    else:
        assert_whole(LONG_COMMAND, peek)


def test_a_multi_line_command_keeps_its_lines_in_the_request() -> None:
    script = "python3 - <<'EOF'\nimport json\nprint(json.dumps({'a': 1}))\nEOF"
    rows = dict(approval_lines(_approval(script)))
    assert rows["run_bash"] == script


def test_a_command_too_long_for_the_peek_panel_says_how_much_it_hides() -> None:
    command = "echo " + " ".join(f"argument{n}" for n in range(300))
    peek = _plain(peek_lines(_session(), None, _approval(command), 60, now=NOW))
    assert any("more line" in line for line in peek)
    assert len(peek) <= 12
    detail = _plain(detail_lines(_session(), None, _approval(command), 60, now=NOW).lines)
    assert not any("more line" in line for line in detail)
    assert_whole(command, detail)


def _data(*entries: TranscriptLine, **kwargs: object) -> DetailData:
    return DetailData("s", 1, transcript=entries, **kwargs)  # type: ignore[arg-type]


REPLY = (
    'The sweep works but my "card" heuristic is catching non-card links, so I will fix the '
    "measurer and add 1px bisection around each breakpoint.\n\nAfter that I will re-run the "
    "sweep at every width and compare the two reports side by side."
)


@pytest.mark.parametrize("width", WIDTHS)
def test_the_transcript_shows_each_reply_and_tool_call_whole(width: int) -> None:
    tool = TranscriptLine(
        "run_bash",
        "timeout 240 python3 research/responsive/tools/resp.py --out research/responsive/_shots"
        " · /root/typescript_projects/pyapps/research/responsive/_shots/report.json written",
        "✓",
    )
    failed = TranscriptLine(
        "write_file",
        "research/responsive/tools/resp.py · resp.py changed on disk since it was last read",
        "✗",
    )
    view = detail_lines(
        _session(), _data(TranscriptLine("assistant", REPLY), tool, failed), None, width, now=NOW
    )
    lines = _plain(view.lines)
    assert all(cell_len(line) <= width for line in lines)
    for entry in (REPLY, tool.text, failed.text):
        assert_whole(entry, lines)


def test_an_entry_keeps_its_paragraphs() -> None:
    lines = _plain(
        detail_lines(
            _session(), _data(TranscriptLine("assistant", REPLY)), None, 100, now=NOW
        ).lines
    )
    start = next(i for i, line in enumerate(lines) if "The sweep works" in line)
    assert "" in lines[start : start + 4]  # the blank line between the two paragraphs


def test_an_error_text_is_not_cut_in_the_detail_view() -> None:
    error = (
        "ProviderError: " + "the provider rejected the request because " * 6 + "the key is wrong"
    )
    session = _session().evolve(status=SessionStatus.FAILED, error=error)
    lines = _plain(detail_lines(session, None, None, 50, now=NOW).lines)
    assert all(cell_len(line) <= 50 for line in lines)
    assert_whole(error, lines)


def test_changes_receipts_and_activity_wrap_too() -> None:
    receipt = "queued · " + "m" * 10 + " delivered to the worker through the inbox and acknowledged"
    stat = "src/agenthicc/tui/workspace/agents_hub/render.py | 120 ++++++++++++++++++-----"
    activity = (
        "reading src/agenthicc/tui/workspace/agents_hub/render.py to find where rows are cut off"
    )
    session = _session(latest_activity=activity)
    data = _data(
        receipt=receipt, changes=ChangeSummary(files=1, insertions=2, deletions=1, lines=(stat,))
    )
    lines = _plain(detail_lines(session, data, None, 48, now=NOW).lines)
    assert all(cell_len(line) <= 48 for line in lines)
    for text in (receipt, stat, activity):
        assert_whole(text, lines)


@pytest.mark.parametrize("width", WIDTHS)
def test_the_facts_under_the_title_wrap(width: int) -> None:
    session = _session(model="anthropic/claude-opus-4-8-with-a-rather-long-profile-name")
    lines = _plain(detail_lines(session, None, None, width, now=NOW).lines)
    assert all(cell_len(line) <= width for line in lines)
    assert_whole("anthropic/claude-opus-4-8-with-a-rather-long-profile-name", lines)


# ── the loader keeps what the view now shows ─────────────────────────────────


def test_a_reply_keeps_its_paragraphs_and_is_not_cut_at_240_characters() -> None:
    entry = transcript_line("text", {"text": REPLY * 3})
    assert entry is not None
    assert entry.text.count("\n\n") >= 1
    assert "compare the two reports side by side." in entry.text
    assert len(entry.text) > 240


def test_a_reply_beyond_the_limit_ends_in_a_marker() -> None:
    entry = transcript_line("text", {"text": "word " * (MESSAGE_CHARS // 3)})
    assert entry is not None
    assert entry.text.endswith(f"… (cut at {MESSAGE_CHARS:,} characters)")


def test_blank_runs_collapse_and_edges_are_trimmed() -> None:
    entry = transcript_line("user_message", {"text": "\n\n  hello   there \n\n\n\n second \n\n"})
    assert entry is not None and entry.text == "hello there\n\nsecond"


def test_a_tool_entry_keeps_its_summary_and_first_output_line() -> None:
    summary = "python3 research/responsive/tools/resp.py --out " + "x" * 400
    entry = transcript_line(
        "tool_complete",
        {
            "name": "run_bash",
            "summary": summary,
            "success": True,
            "state": "exited",
            "output_lines": ["/root/typescript_projects/pyapps/research/responsive/_shots/ok.json"],
        },
    )
    assert entry is not None
    assert summary in entry.text and entry.text.endswith("_shots/ok.json")
