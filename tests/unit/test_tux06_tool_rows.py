"""TUX-06: transcript density and tool rows.

Every test renders real events through ``ScrollBufferAppender`` into a
recording Rich console and asserts on the text a user would see.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.config import AgenthiccConfig, UISettings, load_config
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.runtime.session_log import _decode_event, migrate_event_payload
from agenthicc.tui.workspace.appender import (
    ScrollBufferAppender,
    session_tool_detail,
    set_session_tool_detail,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tux06"
_AGT_TOOLS = _FIXTURES / "agt_tools_session.jsonl"

#: Rows the pre-TUX-06 renderer printed for ``agt_tools_session.jsonl`` at 120
#: columns (recorded with this file's recording console before the change).
_BASELINE_ROWS = 86


@pytest.fixture(autouse=True)
def _utc() -> Iterator[None]:
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()
    set_session_tool_detail(None)


def _appender(
    width: int = 120, *, ui: UISettings | None = None
) -> tuple[ScrollBufferAppender, Console]:
    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    console = Console(file=StringIO(), record=True, width=width, color_system=None)
    return ScrollBufferAppender(state, console), console


def _render(events: list[ConversationEvent], width: int = 120, level: str = "compact") -> str:
    appender, console = _appender(width)
    set_session_tool_detail(level)
    for event in events:
        appender._render_one(event)
    appender.flush()
    return "\n".join(row.rstrip() for row in console.export_text().splitlines()) + "\n"


def _event(kind: str, payload: dict[str, object], ts: float = 1_767_243_843.0) -> ConversationEvent:
    return ConversationEvent(event_id=f"{kind}-{ts}", kind=kind, payload=payload, timestamp=ts)


def _tool(name: str, summary: str, **fields: object) -> ConversationEvent:
    payload: dict[str, object] = {
        "tool_use_id": "t",
        "name": name,
        "success": True,
        "state": None,
        "ready": None,
        "summary": summary,
        "summary_detail": "",
        "args": {},
        "duration_ms": 5,
        "output_lines": [],
        "output_head": 0,
        "output_total": 0,
        "exit_code": None,
        "error_code": None,
    }
    payload.update(fields)
    return _event("tool_complete", payload)


def _fixture_events() -> list[ConversationEvent]:
    lines = _AGT_TOOLS.read_text(encoding="utf-8").splitlines()
    return [event for event in (_decode_event(line, rendered=False) for line in lines) if event]


# ── §4.1 density ──────────────────────────────────────────────────────────────


def test_the_agt_tools_replay_prints_at_most_forty_percent_of_the_old_rows() -> None:
    events = _fixture_events()
    assert sum(event.kind == "tool_complete" for event in events) == 10
    assert sum(event.kind == "text" for event in events) == 2
    rows = _render(events).splitlines()
    assert len(rows) <= 0.4 * _BASELINE_ROWS, "\n".join(rows)


def test_success_is_one_quiet_row_without_completed_or_output() -> None:
    text = _render(
        [_tool("read_file", "src/app.py", output_lines=["a", "b"], output_head=2, output_total=2)]
    )
    assert text.splitlines() == ["● Read src/app.py"]
    assert "Completed" not in text


def test_consecutive_tool_rows_have_no_blank_line_and_text_gets_one() -> None:
    text = _render(
        [
            _tool("read_file", "a.py"),
            _tool("grep_files", '"x" in src/', summary_detail="→ 3 matches"),
            _event("text", {"text": "Done."}),
        ]
    )
    assert text.splitlines()[:4] == [
        "● Read a.py",
        '● Search "x" in src/  → 3 matches',
        "",
        "Done.",
    ]


def test_the_duration_is_right_aligned_only_from_one_second() -> None:
    quick, slow = (
        _render([_tool("read_file", "a.py", duration_ms=999)]),
        _render([_tool("read_file", "a.py", duration_ms=2_400)]),
    )
    assert quick.rstrip() == "● Read a.py"
    row = slow.splitlines()[0]
    assert row.startswith("● Read a.py") and row.endswith("2.4s") and len(row) == 120


# ── §4.3 commands ─────────────────────────────────────────────────────────────


def test_a_cd_chain_shows_its_last_command_and_the_directory() -> None:
    from agenthicc.runners.agent_turn import _capture_tool_result
    from agenthicc.tui.tool_summary import bounded_args

    command = "cd /very/long/path && npm test"
    capture = _capture_tool_result(
        "run_bash",
        {"command": command},
        {"ok": True, "state": "exited", "returncode": 0, "stdout": "ok\n", "stderr": ""},
        Path("/w"),
        None,
    )
    event = _tool(
        "run_bash",
        capture.summary.target,
        summary_detail=capture.summary.detail,
        args=bounded_args({"command": command}),
        exit_code=capture.exit_code,
    )
    assert event.payload["args"] == {"command": command}
    assert _render([event]).splitlines()[0] == "● $ npm test  in path/"


def test_a_command_adds_one_result_row_with_a_parsed_test_summary() -> None:
    output = ["collected 1287 items", "", "===== 1284 passed, 3 skipped in 2.40s ====="]
    text = _render(
        [
            _tool(
                "run_bash",
                "uv run pytest tests/unit -q",
                output_lines=output,
                output_head=3,
                output_total=3,
                exit_code=0,
                duration_ms=2_400,
            )
        ]
    )
    rows = text.splitlines()
    assert rows[0].startswith("● $ uv run pytest tests/unit -q") and rows[0].endswith("2.4s")
    assert rows[1:] == ["  └ 1284 passed, 3 skipped"]


@pytest.mark.parametrize(
    ("output", "summary"),
    [
        (["Tests:       1 failed, 3 passed, 4 total"], "1 failed, 3 passed, 4 total"),
        ([" Test Files  1 passed (1)", "      Tests  12 passed (12)"], "12 passed"),
        (
            ["test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured"],
            "3 passed, 0 failed, 1 ignored",
        ),
        (["ok  \tgithub.com/x/y\t0.01s", "ok  \tgithub.com/x/z\t0.02s"], "2 packages ok"),
    ],
)
def test_jest_vitest_cargo_and_go_summaries_are_recognised(output: list[str], summary: str) -> None:
    from agenthicc.tui.tool_summary import test_summary

    assert test_summary(output) == summary


def test_a_command_result_row_is_the_last_line_only_when_it_is_known() -> None:
    known = _render(
        [_tool("run_bash", "ls", output_lines=["a", "b", ""], output_head=3, output_total=3)]
    )
    truncated = _render(
        [_tool("run_bash", "ls", output_lines=["a", "b"], output_head=2, output_total=90)]
    )
    assert known.splitlines() == ["● $ ls", "  └ b"]
    assert truncated.splitlines() == ["● $ ls"]


# ── §4.4 failures ─────────────────────────────────────────────────────────────


def test_a_failed_command_shows_its_last_eight_lines_exit_code_and_hint() -> None:
    lines = [f"line {index}" for index in range(1, 9)] + [f"tail {index}" for index in range(1, 33)]
    event = _tool(
        "run_bash",
        "npm test",
        summary_detail="in objective-create-a/",
        state="failed",
        output_lines=lines,
        output_head=8,
        output_total=49,
        exit_code=1,
        duration_ms=8_100,
    )
    rows = _render([event]).splitlines()
    assert rows[0].startswith("✗ $ npm test  in objective-create-a/")
    assert rows[0].endswith("exit 1 · 8.1s")
    assert rows[1:9] == [f"  │ tail {index}" for index in range(25, 33)]
    assert rows[9] == "  └ 41 more lines · ctrl+t to expand"


def test_a_tool_error_code_marks_a_failure() -> None:
    rows = _render(
        [
            _tool(
                "read_file",
                "nope.py",
                error_code="not_found",
                output_lines=["not found: nope.py"],
                output_head=1,
                output_total=1,
            )
        ]
    ).splitlines()
    assert rows[0].startswith("✗ Read nope.py") and rows[0].endswith("not found")
    assert rows[1:] == ["  │ not found: nope.py", "  └ ctrl+t to expand"]


@pytest.mark.parametrize(
    ("state", "ready", "word"),
    [("running", False, "running"), ("running", True, "ready"), ("waiting", None, "waiting")],
)
def test_running_and_waiting_states_keep_their_words(
    state: str, ready: bool | None, word: str
) -> None:
    row = _render([_tool("run_bash", "npm run dev", state=state, ready=ready)]).splitlines()[0]
    assert row.startswith("● $ npm run dev") and row.endswith(word)


@pytest.mark.parametrize(
    ("state", "word"), [("timed_out", "timed out"), ("cancelled", "cancelled")]
)
def test_timed_out_and_cancelled_keep_their_words(state: str, word: str) -> None:
    row = _render([_tool("run_bash", "sleep 99", state=state)]).splitlines()[0]
    assert row.startswith("✗ $ sleep 99") and row.endswith(word)


# ── §4.5 memory reads ─────────────────────────────────────────────────────────


def test_a_memory_read_is_one_human_row_without_json() -> None:
    from agenthicc.runners.agent_turn import _capture_tool_result

    result = {"found": False, "value": None}
    capture = _capture_tool_result("memory_read", {"key": "deploy notes"}, result, Path("/w"), None)
    event = _tool(
        "memory_read",
        capture.summary.target,
        summary_detail=capture.summary.detail,
        output_lines=list(capture.output.lines),
        output_head=capture.output.head,
        output_total=capture.output.total,
    )
    for level in ("compact", "normal"):
        text = _render([event], level=level)
        assert text.splitlines() == ['● Read memory: "deploy notes"  not found']
        assert '{"' not in text


# ── §4.7 missing mentions ─────────────────────────────────────────────────────


async def test_a_missing_mention_is_a_warning_chip_not_a_failed_tool(tmp_path: Path) -> None:
    from agenthicc.mentions.injector import InjectionConfig, build_context_prefix

    text = "@nope.md hello"
    prefix, injected = await build_context_prefix(text, cfg=InjectionConfig(cwd=tmp_path))
    assert "nope.md" in prefix  # the model still gets the unresolved-mention note
    chips = [item.chip() for item in injected]
    rendered = _render(
        [_event("user_message", {"text": text}), _event("mention_chips", {"chips": chips})]
    )
    rows = rendered.splitlines()
    assert rows[0].rstrip() == "❯ @nope.md hello"
    assert rows[1] == "  ! @nope.md not found"
    assert "✗" not in rendered


def test_resolved_mentions_share_one_chip_row() -> None:
    chips = [
        {"raw": "@src/app.py", "kind": "file", "ok": True},
        {"raw": "@docs/", "kind": "directory", "ok": True},
    ]
    rows = _render(
        [_event("user_message", {"text": "look"}), _event("mention_chips", {"chips": chips})]
    ).splitlines()
    assert rows[1] == "  @src/app.py  @docs/"


# ── §4.8 detail levels ────────────────────────────────────────────────────────


def _details(args: str = "") -> str:
    from agenthicc.commands.builtins import _cmd_details
    from agenthicc.commands.command import CommandContext

    console = Console(file=StringIO(), record=True, width=80, color_system=None)
    _cmd_details(
        CommandContext(
            text=f"/details {args}".strip(),
            args=args,
            model="m",
            console=console,
            config=AgenthiccConfig(),
        )
    )
    return console.export_text().strip()


def test_details_cycles_the_level_and_only_the_next_call_changes() -> None:
    appender, console = _appender()
    read = _tool(
        "read_file",
        "src/app.py",
        output_lines=["one", "two", "three", "four", "five"],
        output_head=5,
        output_total=60,
    )
    appender._render_one(read)
    before = console.export_text(clear=False)
    assert before.splitlines() == ["● Read src/app.py"]

    assert _details() == "Tool detail: normal"
    assert session_tool_detail() == "normal"
    appender._render_one(read)
    after = console.export_text(clear=False)
    assert after.startswith(before)  # earlier rows are never reprinted
    assert after[len(before) :].splitlines() == [
        "● Read src/app.py",
        "  │ one",
        "  │ two",
        "  │ three",
        "  │ four",
        "  └ 56 more lines",
    ]
    assert _details() == "Tool detail: verbose"
    assert _details() == "Tool detail: compact"
    assert _details("bogus").startswith("/details takes one of")
    assert _details("verbose") == "Tool detail: verbose"


def test_verbose_prints_the_bounded_output_with_the_gap_marked() -> None:
    lines = ["h1", "h2", "t1", "t2"]
    rows = _render(
        [_tool("run_bash", "make", output_lines=lines, output_head=2, output_total=10)],
        level="verbose",
    ).splitlines()
    assert rows == [
        "● $ make",
        "  │ h1",
        "  │ h2",
        "  │ … 6 lines omitted",
        "  │ t1",
        "  │ t2",
        "  └ t2",
    ]


def test_the_configured_level_applies_without_details() -> None:
    appender, console = _appender(ui=UISettings(tool_detail="normal"))
    appender._render_one(
        _tool("run_bash", "ls", output_lines=["a", "b"], output_head=2, output_total=2)
    )
    assert console.export_text().splitlines() == ["● $ ls", "  │ a", "  │ b", "  └ b"]


def test_status_shows_the_time_zone_the_headers_leave_out() -> None:
    from agenthicc.commands.builtins import _cmd_status
    from agenthicc.commands.command import CommandContext

    console = Console(file=StringIO(), record=True, width=80, color_system=None)
    _cmd_status(
        CommandContext(
            text="/status", args="", model="m", console=console, config=AgenthiccConfig()
        )
    )
    assert "Time zone: UTC +0000" in console.export_text()


def test_tool_detail_is_validated_and_parsed_from_config(tmp_path: Path) -> None:
    config = tmp_path / "agenthicc.toml"
    config.write_text('[ui]\ntool_detail = "Verbose"\n', encoding="utf-8")
    assert load_config(config_path=str(config)).ui.tool_detail == "verbose"
    assert AgenthiccConfig().ui.tool_detail == "compact"
    with pytest.raises(ValueError, match="ui.tool_detail"):
        UISettings(tool_detail="loud")


# ── exploration groups ────────────────────────────────────────────────────────


def _explored(name: str, summary: str, **presentation: object) -> ConversationEvent:
    return _tool(name, summary, presentation={"exploratory": True, **presentation})


def test_an_exploration_group_collapses_to_one_row() -> None:
    events = [
        _explored("read_file", "a.py"),
        _explored("batch_read", "3 files", file_count=3),
        _explored("grep_files", '"x"'),
        _explored("list_directory", "src/"),
        _event("text", {"text": "Found it."}),
    ]
    rows = _render(events).splitlines()
    assert rows[0] == '● Explored 4 files, 1 search, 1 listing  a.py, 3 files, "x", src/'
    assert rows[1:3] == ["", "Found it."]
    verbose = _render(events, level="verbose").splitlines()
    assert verbose[1:5] == ["  └ Read a.py", "  └ Read 3 files", '  └ Search "x"', "  └ List src/"]


def test_invisible_events_do_not_split_an_exploration_group() -> None:
    events = [
        _explored("read_file", "a.py"),
        _event("tokens", {"input_tokens": 1}),
        _explored("read_file", "b.py"),
    ]
    assert _render(events).splitlines() == ["● Explored 2 files  a.py, b.py"]


# ── §2.5 headers and the user message ─────────────────────────────────────────


def _turn(agent: str, model: str, ts: float) -> ConversationEvent:
    return _event("turn_start", {"agent_name": "x", "agent": agent, "model": model}, ts)


def test_the_default_agent_on_an_unchanged_model_has_no_header() -> None:
    t0 = 1_767_243_843.0
    text = _render(
        [
            _event("user_message", {"text": "hi"}, t0),
            _turn("default", "m1", t0 + 1),
            _event("text", {"text": "Hello."}, t0 + 2),
            _event("user_message", {"text": "again"}, t0 + 60),
            _turn("default", "m1", t0 + 61),
            _event("text", {"text": "Sure."}, t0 + 62),
        ]
    )
    assert "◆" not in text
    assert "assistant" not in text


def test_a_header_marks_a_changed_agent_or_model_or_a_long_gap() -> None:
    t0 = 1_767_243_843.0  # 2026-01-01 05:04:03 UTC
    text = _render(
        [
            _turn("default", "m1", t0),
            _event("text", {"text": "One."}, t0 + 1),
            _turn("reviewer", "sonnet-4.6", t0 + 2),
            _event("text", {"text": "Two."}, t0 + 3),
            _turn("reviewer", "sonnet-4.6", t0 + 4),
            _event("text", {"text": "Three."}, t0 + 5),
            _turn("reviewer", "sonnet-4.6", t0 + 5 + 31 * 60),
            _event("text", {"text": "Four."}, t0 + 6 + 31 * 60),
        ]
    )
    headers = [row for row in text.splitlines() if row.startswith("◆")]
    assert headers == ["◆ reviewer · sonnet-4.6 · 05:04", "◆ reviewer · sonnet-4.6 · 05:35"]


def test_a_header_waits_for_the_users_mention_chips() -> None:
    chips = [{"raw": "@a.py", "kind": "file", "ok": True}]
    rows = _render(
        [
            _event("user_message", {"text": "see @a.py"}),
            _turn("reviewer", "m", 1_767_243_843.0),
            _event("mention_chips", {"chips": chips}),
            _event("text", {"text": "Ok."}),
        ]
    ).splitlines()
    assert [row.rstrip() for row in rows[:5]] == [
        "❯ see @a.py",
        "  @a.py",
        "",
        "◆ reviewer · m · 05:04",
        "Ok.",
    ]


def test_the_user_message_wraps_with_a_hanging_indent_and_condenses_long_pastes() -> None:
    rows = [
        row.rstrip()
        for row in _render([_event("user_message", {"text": "word " * 12})], width=24).splitlines()
    ]
    assert rows[0].startswith("❯ word")
    assert all(row.startswith("  word") for row in rows[1:])
    long = "\n".join(f"line {index}" for index in range(30))
    rows = [row.rstrip() for row in _render([_event("user_message", {"text": long})]).splitlines()]
    assert rows == ["❯ line 0", "  line 1", "  line 2", "  line 3", "  [+26 more lines]"]


def test_the_user_band_uses_the_user_bg_and_accent_tokens() -> None:
    from agenthicc.tui.theme import palette

    state = AppState.create()
    console = Console(file=StringIO(), record=True, width=40, color_system="truecolor")
    appender = ScrollBufferAppender(state, console)
    appender._render_one(_event("user_message", {"text": "hi"}))
    styled = console.export_text(styles=True)
    accent = palette().accent.lstrip("#")
    user_bg = palette().user_bg.lstrip("#")
    red, green, blue = (int(accent[i : i + 2], 16) for i in (0, 2, 4))
    assert f"38;2;{red};{green};{blue}" in styled
    red, green, blue = (int(user_bg[i : i + 2], 16) for i in (0, 2, 4))
    assert f"48;2;{red};{green};{blue}" in styled


# ── §2.6 replay separators ────────────────────────────────────────────────────


def test_replay_separators_frame_history_with_its_own_time() -> None:
    appender, console = _appender(width=60)
    appender.replay_boundary(since=1_767_243_843.0, events=827)
    appender.replay_boundary()
    rows = console.export_text().splitlines()
    assert rows[0].startswith("── replayed from 1 Jan 05:04 · 827 events ──")
    assert rows[1].startswith("── now ──")
    assert all(len(row) == 60 for row in rows)


# ── §2.1 migration of old payloads ────────────────────────────────────────────


def test_old_tool_payloads_migrate_once_on_read() -> None:
    old = {
        "name": "read_file",
        "success": True,
        "args_str": "[dim](path='scenes/intro.js', offset=1, limit=80)[/dim]",
        "dur_str": "  [dim]3ms[/dim]",
        "output_lines": ["1", "2", "3", "4"],
        "output_more": 76,
    }
    migrated = migrate_event_payload("tool_complete", old)
    assert (
        "args_str" not in migrated and "dur_str" not in migrated and "output_more" not in migrated
    )
    assert migrated["summary"] == "scenes/intro.js:1-80"
    assert migrated["duration_ms"] == 3
    assert (migrated["output_head"], migrated["output_total"]) == (4, 80)
    assert migrated["args"] == {"path": "scenes/intro.js", "offset": 1, "limit": 80}
    assert migrate_event_payload("tool_complete", migrated) is migrated


def test_old_single_argument_and_escaped_markup_migrate() -> None:
    command = migrate_event_payload(
        "tool_complete",
        {"name": "run_bash", "args_str": "[dim]('cd /a/b && npm test')[/dim]", "dur_str": ""},
    )
    assert (command["summary"], command["summary_detail"], command["duration_ms"]) == (
        "npm test",
        "in b/",
        None,
    )
    plugin = migrate_event_payload(
        "tool_complete", {"name": "x_tool", "args_str": "[dim](q='\\[1, 2]')[/dim]", "dur_str": ""}
    )
    assert plugin["summary"] == "[1, 2]"


def test_old_turn_starts_gain_agent_and_model() -> None:
    migrated = migrate_event_payload(
        "turn_start", {"agent_name": "assistant (deepseek-v4.1-flash)"}
    )
    assert (migrated["agent"], migrated["model"]) == ("default", "deepseek-v4.1-flash")


def test_the_loader_applies_the_migration() -> None:
    raw = json.dumps(
        {
            "event_id": "e",
            "kind": "tool_complete",
            "payload": {
                "name": "memory_read",
                "args_str": "[dim]('topic_brief')[/dim]",
                "dur_str": "  [dim]8ms[/dim]",
            },
            "timestamp": 1.0,
        }
    )
    event = _decode_event(raw, rendered=False)
    assert event is not None
    assert event.payload["summary"] == 'memory: "topic_brief"'
    assert "args_str" not in event.payload


# ── file edits ────────────────────────────────────────────────────────────────


def test_an_edit_is_one_header_row_and_a_diff_capped_at_twenty_rows() -> None:
    old = [f"line {index}" for index in range(60)]
    new = [f"changed {index}" if index % 2 else line for index, line in enumerate(old)]
    event = _event(
        "file_modified",
        {"path": "src/app/main.py", "old_lines": old, "new_lines": new, "tool": "patch_file"},
    )
    rows = _render([event]).splitlines()
    assert rows[0] == "● Edit src/app/main.py  +30 −30"
    assert len(rows) == 1 + 20 + 1
    assert "more diff lines" in rows[-1]
    assert len(_render([event], level="verbose").splitlines()) > 22
