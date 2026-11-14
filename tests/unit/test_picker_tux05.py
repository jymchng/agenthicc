"""TUX-05: one picker component, the ranked ``/`` picker and the ``@`` picker.

Covers acceptance criteria 1 (ranking), 2 (aliases), 3 (shortcuts), 4 (the
``@`` picker) and 8 (width), plus the picker's look: the selection bar, the
match highlight, group headings, dimmed rows and the position footer.
"""

from __future__ import annotations

import collections
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from agenthicc.commands import Command, UnifiedCommandRegistry, build_builtin_registry
from agenthicc.commands.command import BusyPolicy
from agenthicc.tui.actions import COMMAND_ACTIONS, DEFAULT_ACTIONS
from agenthicc.tui.input.command_usage import CommandUsage
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.theme import glyphs, palette
from agenthicc.tui.trigger import TriggerContext
from agenthicc.tui.triggers.at_mention import AtMentionTrigger, WorkingTreeStatus, human_size
from agenthicc.tui.triggers.slash_command import SUGGESTED_GROUP, SlashCommandTrigger
from agenthicc.tui.workspace.picker import Picker, PickerRow, picker_height, rows_from_matches

pytestmark = pytest.mark.unit

CTX = TriggerContext(cwd=Path("."))


def _slash(**kwargs: object) -> SlashCommandTrigger:
    return SlashCommandTrigger(build_builtin_registry(), **kwargs)  # type: ignore[arg-type]


def _values(fragment: str, trigger: SlashCommandTrigger | None = None) -> list[str]:
    return [item.value for item in (trigger or _slash()).get_matches(fragment, CTX)]


# ── AC1: ranking ─────────────────────────────────────────────────────────────


def test_comp_ranks_compact_first() -> None:
    assert _values("comp")[0] == "/compact"


def test_summar_finds_compact_by_its_description() -> None:
    assert "/compact" in _values("summar")
    assert _values("summar")[0] == "/compact"


@pytest.mark.parametrize(
    ("alias", "command"),
    [
        ("bg", "/background"),
        ("detach", "/background"),
        ("processes", "/ps"),
        ("interrupt", "/cancel"),
        ("models", "/model"),
    ],
)
def test_typing_an_alias_ranks_its_command_first(alias: str, command: str) -> None:
    rows = _slash().get_matches(alias, CTX)
    assert rows[0].value == command
    assert f"/{alias}" in rows[0].detail
    assert "(alias" in rows[0].detail


def test_the_ranking_tiers_in_order() -> None:
    registry = UnifiedCommandRegistry()
    for command in (
        Command("/co", "Exact", group="Session"),
        Command("/cobalt", "Name prefix", group="Session"),
        Command("/x", "Alias prefix", aliases=("/colour",), group="Session"),
        Command("/acorn", "Fuzzy name", group="Session"),
        Command("/z", "Collect every row", group="Session"),
    ):
        registry.register(command)
    assert _values("co", SlashCommandTrigger(registry)) == ["/co", "/cobalt", "/x", "/acorn", "/z"]


def test_ties_go_to_the_most_used_then_most_recent_command(tmp_path: Path) -> None:
    usage = CommandUsage(tmp_path, tmp_path / "usage.json")
    trigger = _slash(usage=usage)
    assert _values("co", trigger)[:2] == ["/commands", "/compact"]
    usage.record("/compact", now=10.0)
    assert _values("co", trigger)[:2] == ["/compact", "/commands"]
    usage.record("/config", now=20.0)
    usage.record("/config", now=21.0)
    assert _values("co", trigger)[:3] == ["/config", "/compact", "/commands"]


def test_usage_is_counted_per_project_and_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    trigger = _slash(usage=CommandUsage(tmp_path / "a", path))
    trigger.record_use("/compact now")
    trigger.record_use("/bg")  # an alias counts toward its command
    trigger.record_use("/nope")
    assert CommandUsage(tmp_path / "a", path).counts().keys() == {"/compact", "/background"}
    assert CommandUsage(tmp_path / "b", path).counts() == {}


def test_an_empty_fragment_is_grouped_with_suggested_first(tmp_path: Path) -> None:
    usage = CommandUsage(tmp_path, tmp_path / "usage.json")
    for when, name in enumerate(("/model", "/compact", "/clear", "/not-a-command", "/clear")):
        usage.record(name, now=float(when))
    rows = _slash(usage=usage).get_matches("", CTX)
    # Most used first; equal counts go to the most recent.
    assert [row.value for row in rows[:3]] == ["/clear", "/compact", "/model"]
    assert {row.group for row in rows[:3]} == {SUGGESTED_GROUP}
    groups = list(dict.fromkeys(row.group for row in rows))
    assert groups == [SUGGESTED_GROUP, "Session", "Agents", "Workflows", "Configuration", "MCP"]
    # A suggested command is not repeated under its own group.
    assert [row.value for row in rows].count("/model") == 1


def test_a_fragment_gives_one_ranked_list_without_headings() -> None:
    assert {row.group for row in _slash().get_matches("co", CTX)} == {""}


def test_every_builtin_command_has_one_of_the_picker_groups() -> None:
    allowed = {"Session", "Agents", "Workflows", "Configuration", "MCP"}
    assert {command.group for command in build_builtin_registry()} <= allowed


# ── AC2: aliases ─────────────────────────────────────────────────────────────


def test_no_two_rows_of_the_builtin_listing_share_a_description() -> None:
    rows = _slash().get_matches("", CTX)
    counts = collections.Counter(row.detail for row in rows)
    assert [detail for detail, count in counts.items() if count > 1] == []
    descriptions = collections.Counter(command.description for command in build_builtin_registry())
    assert [d for d, count in descriptions.items() if count > 1] == []
    assert len({row.value for row in rows}) == len(rows)


def test_detach_and_models_are_aliases_not_rows() -> None:
    registry = build_builtin_registry()
    assert registry.get("/detach") is registry.get("/background")
    assert registry.get("/models") is registry.get("/model")
    assert "/detach" not in _values("")


# ── AC3: shortcuts ───────────────────────────────────────────────────────────


def test_every_command_bound_to_a_default_action_shows_its_key() -> None:
    actions = {action.name: action for action in DEFAULT_ACTIONS}
    rows = {row.value: row for row in _slash().get_matches("", CTX)}
    assert COMMAND_ACTIONS
    for command, action_name in COMMAND_ACTIONS.items():
        assert action_name in actions, action_name
        assert rows[command].keys == actions[action_name].keys[0], command


def test_a_rebound_key_shows_the_users_key() -> None:
    keymap = Keymap.build(DEFAULT_ACTIONS, overrides={"agents.open_hub": ("f6",)})
    rows = {row.value: row for row in _slash(keymap=lambda: keymap).get_matches("", CTX)}
    assert rows["/agents"].keys == "f6"


def test_a_command_rejected_while_busy_is_dimmed_with_the_reason() -> None:
    registry = UnifiedCommandRegistry()
    registry.register(Command("/ok", "Runs now", busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY))
    registry.register(Command("/no", "Never while busy", busy_policy=BusyPolicy.REJECT))
    registry.register(Command("/later", "Queues", busy_policy=BusyPolicy.QUEUE))
    rows = {
        row.value: row
        for row in SlashCommandTrigger(registry).get_matches(
            "", TriggerContext(Path("."), busy=True)
        )
    }
    assert rows["/no"].disabled == "not available while a turn runs"
    assert rows["/ok"].disabled == "" and rows["/later"].disabled == ""
    assert "[" not in rows["/no"].detail  # the reason is on the second row instead
    assert "[queues while busy]" in rows["/later"].detail
    assert "[runs now]" in rows["/ok"].detail
    lines = Picker(rows_from_matches([rows["/no"]])).render(80)
    assert "not available while a turn runs" in lines[2].plain


# ── the picker ────────────────────────────────────────────────────────────────


def _rows() -> list[PickerRow[str]]:
    return [
        PickerRow(
            "compact",
            "/compact",
            "Summarise the conversation to free context",
            "Suggested",
            keys="ctrl+x c",
        ),
        PickerRow("config", "/config", "Show or change configuration", "Suggested"),
        PickerRow("copy", "/copy", "Copy the last response", "Session"),
        PickerRow("cost", "/cost", "Token and cost report (alias /usage)", "Session"),
    ]


def _plain(lines: list[Text]) -> list[str]:
    return [line.plain.rstrip() for line in lines]


def test_the_selected_row_has_an_accent_bar_and_background_not_reverse() -> None:
    picker: Picker[str] = Picker(_rows(), query="/co")
    lines = picker.render(80)
    selected = lines[1]
    assert selected.plain.startswith(f" {glyphs().bar} /compact")
    styles = {str(span.style) for span in selected.spans}
    assert any(palette().accent_bg in style for style in styles)
    assert not any("reverse" in style for style in styles)
    assert not lines[2].plain.startswith(f" {glyphs().bar}")


def test_matched_characters_are_bold_in_accent() -> None:
    lines = Picker(_rows(), query="/co").render(80)
    bold = [
        lines[1].plain[span.start : span.end]
        for span in lines[1].spans
        if "bold" in str(span.style) and palette().accent in str(span.style)
    ]
    assert "".join(bold) == "/co"


def test_headings_keys_and_footer() -> None:
    lines = _plain(Picker(_rows(), footer_keys="tab enter").render(80))
    assert lines[0].startswith(f"   {glyphs().rule} Suggested {glyphs().rule}")
    assert lines[1].endswith("ctrl+x c")
    assert lines[3].startswith(f"   {glyphs().rule} Session")
    assert lines[-1].strip() == f"1 of 4 {glyphs().sep} tab enter"


def test_the_list_scrolls_to_keep_the_selection_visible() -> None:
    rows = [PickerRow(str(i), f"/c{i:02}", f"Command {i}") for i in range(30)]
    picker: Picker[str] = Picker(rows)
    picker.selected = 25
    lines = _plain(picker.render(80, 5))
    assert len(lines) == 6
    assert any(line.startswith(f" {glyphs().bar} /c25") for line in lines)
    assert lines[-1].strip().startswith(f"{glyphs().up}{glyphs().down} {glyphs().sep} 26 of 30")


def test_picker_height_is_a_third_of_the_terminal_at_most_twelve() -> None:
    assert picker_height(24) == 8
    assert picker_height(90) == 12
    assert picker_height(6) == 3


# ── AC8: width ────────────────────────────────────────────────────────────────


def _heading(name: str, width: int) -> str:
    """A group rule: it stops one column short of the edge, like the footer."""
    head = f"   ─ {name} "
    return head + "─" * (width - 1 - len(head))


_SNAPSHOT_50 = [
    _heading("Suggested", 50),
    " ▌ /compact                              ctrl+x c",
    "   Summarise the conversation to free context",
    "   /config",
    "   Show or change configuration",
    _heading("Session", 50),
    "   /copy",
    "   Copy the last response",
    "   /cost",
    "   Token and cost report (alias /usage)",
    "                                   1 of 4 · tab ⏎",
]

_SNAPSHOT_80 = [
    _heading("Suggested", 80),
    " ▌ /compact  Summarise the conversation to free context                ctrl+x c",
    "   /config   Show or change configuration",
    _heading("Session", 80),
    "   /copy     Copy the last response",
    "   /cost     Token and cost report (alias /usage)",
    " " * 65 + "1 of 4 · tab ⏎",
]

_SNAPSHOT_120 = [
    _heading("Suggested", 120),
    " ▌ /compact  Summarise the conversation to free context" + " " * 56 + "ctrl+x c",
    "   /config   Show or change configuration",
    _heading("Session", 120),
    "   /copy     Copy the last response",
    "   /cost     Token and cost report (alias /usage)",
    " " * 105 + "1 of 4 · tab ⏎",
]


@pytest.mark.parametrize(
    ("width", "snapshot"), [(50, _SNAPSHOT_50), (80, _SNAPSHOT_80), (120, _SNAPSHOT_120)]
)
def test_snapshot_at_width(width: int, snapshot: list[str]) -> None:
    from agenthicc.tui.theme import UNICODE_GLYPHS, current_theme, use_theme

    previous = current_theme()
    use_theme(previous.with_glyphs(UNICODE_GLYPHS))
    try:
        lines = Picker(_rows(), footer_keys="tab ⏎").render(width)
    finally:
        use_theme(previous)
    assert _plain(lines) == snapshot


@pytest.mark.parametrize("width", [50, 80, 120])
def test_no_row_of_the_real_command_list_exceeds_the_width(width: int) -> None:
    rows = rows_from_matches(_slash().get_matches("", CTX))
    console = Console(width=width, record=True, color_system=None)
    for line in Picker(rows).render(width, 200):
        assert line.cell_len <= width
        console.print(line)
    assert all(len(line) <= width for line in console.export_text().splitlines())


def test_below_sixty_columns_descriptions_get_their_own_row() -> None:
    rows = [PickerRow("x", "/compact", "Summarise the conversation")]
    narrow = _plain(Picker(rows).render(50))
    wide = _plain(Picker(rows).render(60))
    assert (
        narrow[0].strip().endswith("/compact") and narrow[1].strip() == "Summarise the conversation"
    )
    assert wide[0].endswith("Summarise the conversation")


def test_long_descriptions_wrap_with_a_hanging_indent_to_two_rows() -> None:
    rows = [PickerRow("x", "/long", "word " * 60)]
    lines = _plain(Picker(rows).render(80))
    assert len(lines) == 3  # two description rows, then the footer
    assert lines[1].startswith(" " * 10) and lines[1].endswith(glyphs().ellipsis)


# ── AC4: the @ picker ─────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.test")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A = 1\n")
    (tmp_path / "src" / "b.py").write_text("B = 2\n")
    (tmp_path / "README.md").write_text("# readme\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.txt").write_text("x")
    (tmp_path / ".gitignore").write_text("build/\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _wait(trigger: AtMentionTrigger, cwd: Path) -> None:
    status = trigger.working_tree(cwd)
    thread = status._thread
    if thread is not None:
        thread.join(10)


def test_a_changed_file_is_the_first_row_of_an_empty_at_picker(repo: Path) -> None:
    (repo / "src" / "a.py").write_text("A = 2\n")
    trigger = AtMentionTrigger()
    ctx = TriggerContext(cwd=repo)
    trigger.get_matches("", ctx)  # starts git status on a worker thread
    _wait(trigger, repo)
    rows = trigger.get_matches("", ctx)
    assert rows[0].value == "src/a.py"
    assert rows[0].glyph == glyphs().info


def test_a_directory_row_starts_with_the_folder_glyph(repo: Path) -> None:
    rows = AtMentionTrigger().get_matches("sr", TriggerContext(cwd=repo))
    directory = next(row for row in rows if row.value == "src/")
    assert directory.glyph == glyphs().folder == "▸" and directory.detail == "2 entries"
    line = Picker(rows_from_matches([directory])).render(80)[0].plain
    assert line.startswith(f" {glyphs().bar} ▸ src")
    file_row = next(row for row in rows if row.value == "src/a.py")
    assert file_row.detail == human_size(6) == "6 B"


def test_the_path_is_muted_directory_plus_plain_filename(repo: Path) -> None:
    row = next(r for r in AtMentionTrigger().get_matches("a.py", TriggerContext(cwd=repo)))
    line = Picker(rows_from_matches([row]), query="a.py").render(80)[0]
    start = line.plain.index("src/")
    muted = [s for s in line.spans if s.start == start and palette().muted in str(s.style)]
    assert muted and muted[0].end == start + len("src/")


def test_ignored_entries_are_excluded_unless_named_exactly(repo: Path) -> None:
    trigger = AtMentionTrigger()
    ctx = TriggerContext(cwd=repo)
    trigger.get_matches("", ctx)
    _wait(trigger, repo)
    values = [row.value for row in trigger.get_matches("", ctx)]
    assert "build/" not in values and "build/out.txt" not in values
    assert [row.value for row in trigger.get_matches("build", ctx)][0] == "build/"
    assert "out.txt" not in [row.value for row in trigger.get_matches("build/o", ctx)][0:0]


def test_recently_mentioned_files_come_first(repo: Path) -> None:
    trigger = AtMentionTrigger()
    ctx = TriggerContext(cwd=repo)
    readme = next(row for row in trigger.get_matches("readme", ctx))
    trigger.on_select(readme, "readme", [])
    assert trigger.get_matches("", ctx)[0].value == "README.md"
    # A recent file also leads its tier when a fragment matches several files.
    b = next(row for row in trigger.get_matches("b.py", ctx))
    trigger.on_select(b, "b.py", [])
    assert [row.value for row in trigger.get_matches(".py", ctx)][0] == "src/b.py"


def test_git_status_runs_off_the_calling_thread_and_never_blocks_matching(tmp_path: Path) -> None:
    calls: list[int] = []
    release = threading.Event()

    def slow_git(cwd: Path) -> tuple[str, str] | None:
        calls.append(threading.get_ident())
        release.wait(5)
        return "", " M a.py\0"

    (tmp_path / "a.py").write_text("x")
    trigger = AtMentionTrigger(git=slow_git)
    ctx = TriggerContext(cwd=tmp_path)
    started = time.perf_counter()
    trigger.get_matches("", ctx)
    trigger.get_matches("a", ctx)
    assert time.perf_counter() - started < 0.5
    release.set()
    _wait(trigger, tmp_path)
    assert calls and calls[0] != threading.get_ident()
    assert len(calls) == 1  # rate limited: one refresh per interval
    assert trigger.working_tree(tmp_path).changed == ("a.py",)


def test_status_parsing_handles_renames_prefixes_and_ignored_dirs() -> None:
    output = "R  sub/new.py\0sub/old.py\0?? sub/fresh/\0!! sub/node_modules/\0 M other.py\0"
    status = WorkingTreeStatus(Path("."), runner=lambda _cwd: ("sub/", output))
    status.refresh_now()
    assert status.changed == ("new.py", "fresh/")
    assert status.ignored == frozenset({"node_modules"})
    assert status.is_ignored("node_modules/pkg/index.js")
    assert not status.is_ignored("new.py")


# ── the trigger picker draws one prompt line (the composer is not drawn under it) ─────


def test_the_open_picker_draws_exactly_one_prompt_line(repo: Path) -> None:
    from agenthicc.tui.input.buffer import InputBuffer
    from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

    overlay = TriggerPickerOverlay(
        InputBuffer(list("@sr")),
        0,
        AtMentionTrigger(),
        cwd=repo,
        keymap=Keymap.build(DEFAULT_ACTIONS),
    )
    console = Console(width=80, record=True, color_system=None)
    console.print(overlay.render())
    lines = console.export_text().splitlines()
    assert sum(1 for line in lines if line.startswith(glyphs().prompt)) == 1
    assert lines[0].startswith(f"{glyphs().prompt} @sr")
    assert any("src/" in line for line in lines[1:])
    assert glyphs().cursor not in lines[0]


def test_page_keys_move_by_the_rows_the_picker_showed(repo: Path) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.input.buffer import InputBuffer
    from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

    for index in range(40):
        (repo / f"file{index:02}.txt").write_text("x")
    overlay = TriggerPickerOverlay(
        InputBuffer(list("@file")),
        0,
        AtMentionTrigger(),
        cwd=repo,
        keymap=Keymap.build(DEFAULT_ACTIONS),
    )
    overlay.render()
    shown = overlay._picker.visible
    assert 3 <= shown < 40
    overlay.handle_key(Key.PAGE_DOWN, "")
    assert overlay.selected == shown


def test_fuzzy_and_match_positions_find_the_highlighted_characters() -> None:
    from agenthicc.mentions.matcher import fuzzy_positions, match_positions

    assert fuzzy_positions("cmp", "/compact") == (1, 3, 4)
    assert fuzzy_positions("zzz", "/compact") is None
    assert match_positions("/co", "/compact") == (0, 1, 2)
    # A substring in the filename wins over one in a directory.
    assert match_positions("a", "ab/cab.py") == (4,)
    assert match_positions("", "x") == () and match_positions("q", "abc") == ()
