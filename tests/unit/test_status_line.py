"""TUX-02: the live region is a framed composer, one status line and one hint row."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text

from agenthicc.config import UISettings
from agenthicc.tui.conversation_store import AgentState, AppState, session_title_from
from agenthicc.tui.theme import (
    ASCII_GLYPHS,
    BUILTIN_THEMES,
    current_theme,
    palette,
    use_theme,
)
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.components import ComposerFrame, StatusLine
from agenthicc.tui.workspace.live_region import Line, lines_text
from agenthicc.tui.workspace.overlay import Overlay
from tests.tui_frames import settle_frames

pytestmark = pytest.mark.unit

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SESSION_ID = "2e7204ff-1203-4a40-aa79-61e538c4d3c6"


@pytest.fixture(autouse=True)
def _default_theme() -> Iterator[None]:
    before = current_theme()
    use_theme(BUILTIN_THEMES["dark"])
    yield
    use_theme(before)


def _state(*, ui: UISettings | None = None) -> AppState:
    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    conv = state.conversation
    conv.model_name.set("anthropic/claude-opus-4-8")
    conv.session_id.set(_SESSION_ID)
    conv.current_directory.set("/root/python_projects/agenthicc")
    return state


def _workspace(state: AppState, width: int = 120, height: int = 40) -> Workspace:
    console = Console(file=StringIO(), force_terminal=True, width=width, height=height)
    return Workspace(state, console)


def _rows(state: AppState, width: int = 120) -> list[str]:
    return lines_text(_workspace(state, width)._build_lines())


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def _snapshot_state() -> AppState:
    state = _state()
    conv = state.conversation
    conv.session_title.set("fix flaky scheduler test")
    conv.context_window.set(1_000_000)
    conv.context_tokens.set(120_000)
    conv.background_needs_you.set(2)
    conv.git_branch.set("main")
    conv.git_dirty.set(True)
    conv.tokens_in.set(12_400)
    conv.tokens_out.set(3_100)
    conv.usage_status.set("complete")
    conv.cost_usd.set(0.42)
    conv.cost_status.set("authoritative")
    return state


# ── 1. Row budget ─────────────────────────────────────────────────────────────


def test_idle_live_region_is_three_composer_rows_and_one_status_line() -> None:
    state = _state()
    workspace = _workspace(state, 120, 40)
    assert workspace.frame.height(120) == 3
    assert workspace.status.height(120) == 1
    assert workspace.frame.hint_row(120) is None
    rows = lines_text(workspace._build_lines())
    assert len(rows) == 4
    # Claude Code's input panel: a rule ending "title · mode ─", the prompt, a rule.
    assert rows[0].startswith("──") and rows[0].endswith(" new session · Safe ─")
    assert rows[1].startswith("❯ ")
    assert set(rows[2]) == {"─"}
    assert rows[3].strip().startswith("● Idle")


def test_a_running_turn_adds_exactly_one_hint_row() -> None:
    state = _state()
    state.conversation.begin_turn("agent", "turn-1")
    rows = _rows(state)
    assert len(rows) == 5
    assert "Thinking" in rows[3]
    assert "Esc pause" in rows[4] and "Ctrl+C cancel" in rows[4]
    # Idle-only hints (submit, newline, commands, agents) are not repeated.
    assert "submit" not in rows[4] and "agents" not in rows[4]


def test_a_condensed_paste_shows_only_the_paste_keys() -> None:
    state = _state()
    state.input.update(list("[pasted 40 lines]"), 17, paste_condensed=True, paste_label="x")
    hint = _plain(ComposerFrame(state, _workspace(state).composer).hint_row(120) or "")
    assert "Ctrl+V expand paste" in hint and "Esc delete paste" in hint
    assert "submit" not in hint


def test_the_empty_composer_shows_a_muted_placeholder() -> None:
    state = _state()
    workspace = _workspace(state)
    (line,) = workspace.composer.lines(116)
    placeholder = line[-1]
    assert placeholder.text == "ask, or type / for commands, @ for files"
    assert placeholder.style is not None and placeholder.style.color is not None
    state.input.update(list("hi"), 2)
    assert "ask," not in "".join(lines_text(workspace.composer.lines(116)))


# ── 2. No placeholder metadata ────────────────────────────────────────────────


def _states_to_check() -> Iterator[tuple[str, AppState]]:
    idle = _state()
    yield "idle", idle

    running = _state()
    running.conversation.begin_turn("agent", "turn-1")
    running.conversation.set_tool("bash")
    yield "running", running

    error = _state()
    error.conversation.agent_state.set(AgentState.ERROR)
    yield "error", error

    terminal = _state()
    terminal.conversation.begin_turn("agent", "turn-1")
    terminal.conversation.set_terminal_wait(
        terminal_id="t1", label="pytest", elapsed_s=12.0, running_count=2
    )
    yield "terminal wait", terminal

    partial = _state()
    partial.conversation.usage_status.set("partial")
    partial.conversation.tokens_in.set(1_000)
    yield "partial usage", partial


@pytest.mark.parametrize("width", [40, 50, 80, 120, 200])
def test_no_frame_shows_placeholder_metadata(width: int) -> None:
    for name, state in _states_to_check():
        text = "\n".join(_rows(state, width))
        assert "cost unknown" not in text, name
        assert "usage unavailable" not in text, name
        assert not _UUID.search(text), name
        assert not re.search(r"[↑↓]\s*\d+\S*\s*\?", text), name
        assert " ?" not in text.replace("? shortcuts", ""), name


def test_partial_usage_is_marked_approximate() -> None:
    state = _state()
    conv = state.conversation
    conv.usage_status.set("partial")
    conv.tokens_in.set(12_000)
    conv.tokens_out.set(3_000)
    line = _plain(StatusLine(state).markup(200))
    assert "≈↑12.0k ↓3.0k" in line


def test_cost_appears_only_when_reported() -> None:
    state = _state()
    conv = state.conversation
    conv.tokens_in.set(100)
    conv.usage_status.set("complete")
    assert "$" not in _plain(StatusLine(state).markup(200))
    conv.cost_usd.set(0.4249)
    conv.cost_status.set("estimated")
    assert "~$0.42" in _plain(StatusLine(state).markup(200))


# ── 3. The context meter ──────────────────────────────────────────────────────


class _Usage(SimpleNamespace):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None
    cache_write_tokens: int | None


@pytest.mark.parametrize(
    ("percent", "band"),
    [(30, "success"), (59, "success"), (60, "warning"), (84, "warning"), (85, "danger")],
)
def test_the_context_meter_follows_reported_input_tokens(percent: int, band: str) -> None:
    from agenthicc.config import AgenthiccConfig
    from agenthicc.runners.usage_ledger import UsageLedger

    state = _state()
    window = AgenthiccConfig().execution.effective_context_window()
    state.conversation.context_window.set(window)
    used = window * percent // 100
    assert "ctx" not in _plain(StatusLine(state).markup(200))  # before any response

    ledger = UsageLedger("session-1")
    ledger.bind_conversation_store(state.conversation)
    call = ledger.begin_call(run_id="run-1")
    # Cached input counts: the context the call sent is input + cache.
    ledger.complete(
        call,
        _Usage(
            input_tokens=used - 1_000,
            output_tokens=50,
            cache_read_tokens=1_000,
            cache_write_tokens=None,
        ),
    )
    markup = StatusLine(state).markup(200)
    assert f"ctx {percent}%" in _plain(markup)
    assert getattr(palette(), band) in markup


def test_the_context_meter_ignores_subagent_and_compaction_calls() -> None:
    from agenthicc.runners.usage_ledger import UsageCategory, UsageLedger

    ledger = UsageLedger("session-1")
    main = ledger.begin_call(run_id="run-1")
    ledger.complete(main, _Usage(input_tokens=10_000, output_tokens=1))
    worker = ledger.begin_call(run_id="run-2", category=UsageCategory.SUBAGENT)
    ledger.complete(worker, _Usage(input_tokens=90_000, output_tokens=1))
    assert ledger.context_tokens() == 10_000


def test_the_meter_bar_fills_by_fifths() -> None:
    state = _state()
    state.conversation.context_window.set(1_000_000)
    state.conversation.context_tokens.set(120_000)
    assert "ctx 12% ▰▱▱▱▱" in _plain(StatusLine(state).markup(200))
    state.conversation.context_tokens.set(420_000)
    assert "ctx 42% ▰▰▱▱▱" in _plain(StatusLine(state).markup(200))


# ── 4. Width ──────────────────────────────────────────────────────────────────

_SNAPSHOTS: dict[int, list[str]] = {
    40: [
        "────── fix flaky scheduler test · Safe ─",
        "❯ ▌ask, or type / for commands",
        "─" * 40,
        " ● Idle · ◆ 2 sess. ← · ctx 12%",
    ],
    50: [
        "──────────────── fix flaky scheduler test · Safe ─",
        "❯ ▌ask, or type / for commands, @ for files",
        "─" * 50,
        " ● Idle · ◆ 2 sessions need you ← · ctx 12% ▰▱▱▱▱",
    ],
    80: [
        "─" * 46 + " fix flaky scheduler test · Safe ─",
        "❯ ▌ask, or type / for commands, @ for files",
        "─" * 80,
        " ● Idle · ◆ 2 sessions need you ← · ctx 12% ▰▱▱▱▱ · opus-4.8               /help",
    ],
    120: [
        "─" * 86 + " fix flaky scheduler test · Safe ─",
        "❯ ▌ask, or type / for commands, @ for files",
        "─" * 120,
        " ● Idle · ◆ 2 sessions need you ← · ctx 12% ▰▱▱▱▱ · opus-4.8 · ↑12.4k ↓3.1k · "
        "$0.42 · main*                        /help",
    ],
}


@pytest.mark.parametrize("width", sorted(_SNAPSHOTS))
def test_width_snapshot(width: int) -> None:
    rows = _rows(_snapshot_state(), width)
    assert rows == _SNAPSHOTS[width]
    assert all(len(row) <= width for row in rows)


def test_at_fifty_columns_only_state_and_context_show_whole() -> None:
    state = _snapshot_state()
    state.conversation.background_needs_you.set(0)
    line = _plain(StatusLine(state).markup(50))
    assert line == " ● Idle · ctx 12% ▰▱▱▱▱"


def test_below_forty_columns_only_the_state_shows() -> None:
    line = _plain(StatusLine(_snapshot_state()).markup(39))
    assert line == " ● Idle"


def test_a_long_title_is_cut_with_an_ellipsis() -> None:
    state = _snapshot_state()
    rows = _rows(state, 30)
    assert rows[0] == "─── fix flaky schedu… · Safe ─"


def test_the_box_frame_is_configurable() -> None:
    state = _state(ui=UISettings(composer_frame="box"))
    rows = _rows(state, 120)
    assert rows[0].startswith("╭─ new session ") and rows[0].endswith(" Safe ─╮")
    assert rows[1].startswith("│ ❯ ") and rows[1].endswith(" │")
    assert rows[2].startswith("╰") and rows[2].endswith("╯")


def test_rules_are_the_default_frame() -> None:
    assert UISettings().composer_frame == "rules"


def test_mode_badge_uses_its_mode_token() -> None:
    from agenthicc.tui.runtime.mode_manager import build_safe_mode

    state = _state()
    state.active_mode.set(build_safe_mode())
    top = ComposerFrame(state, _workspace(state).composer).top(120)
    assert palette().mode_safe in top


def test_ascii_glyphs_draw_nothing_above_ascii() -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    state = _snapshot_state()
    state.conversation.begin_turn("agent", "turn-1")
    for width in (40, 80, 120):
        rows = _rows(state, width)
        assert all(row.isascii() for row in rows), rows


# ── 5. The user status line ───────────────────────────────────────────────────


async def test_a_user_command_replaces_segments_three_to_seven(tmp_path: Path) -> None:
    from agenthicc.tui.statusline import StatusLineCommand

    state = _snapshot_state()
    runner = StatusLineCommand("printf '⎇ main · 42%%\\n'", state, str(tmp_path))
    started = time.monotonic()
    runner.start()
    try:
        while not state.conversation.statusline_text():
            assert time.monotonic() - started < 1.0, "no status line within one second"
            await asyncio.sleep(0.01)
    finally:
        runner.stop()
    line = _plain(StatusLine(state).markup(120))
    assert "⎇ main · 42%" in line
    assert "◆ 2 sessions need you" in line  # segment 2 stays
    assert "opus-4.8" not in line and "ctx" not in line and "main*" not in line


async def test_a_slow_command_is_killed_and_the_builtin_line_returns(tmp_path: Path) -> None:
    from agenthicc.tui.statusline import StatusLineCommand

    state = _snapshot_state()
    state.conversation.statusline_text.set("stale")
    runner = StatusLineCommand("sleep 2", state, str(tmp_path))
    started = time.monotonic()
    await runner.run_once()
    assert time.monotonic() - started < 1.5
    await runner.run_once()
    assert runner.failures == 2
    assert state.conversation.statusline_text() == ""
    assert "opus-4.8" in _plain(StatusLine(state).markup(120))
    notice = state.conversation.notification() or ""
    assert notice.count("Status line command `sleep 2` failed (timed out after 500 ms)") == 1


async def test_the_command_reads_the_state_as_json(tmp_path: Path) -> None:
    import json

    from agenthicc.tui.statusline import StatusLineCommand

    state = _snapshot_state()
    target = tmp_path / "input.json"
    runner = StatusLineCommand(f"cat > {target}; echo ok", state, str(tmp_path))
    await runner.run_once()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data) == {
        "session_id",
        "title",
        "cwd",
        "git_branch",
        "model",
        "provider",
        "mode",
        "state",
        "context_used_pct",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "agents_need_you",
        "workers_running",
    }
    assert data["model"] == "claude-opus-4-8" and data["provider"] == "anthropic"
    assert data["context_used_pct"] == 12 and data["agents_need_you"] == 2
    unknown = _state()
    assert StatusLineCommand("true", unknown, str(tmp_path))  # constructible
    from agenthicc.tui.statusline import statusline_input

    blank = statusline_input(unknown)
    assert blank["title"] is None and blank["tokens_in"] is None and blank["cost_usd"] is None
    assert blank["context_used_pct"] is None


def test_command_output_keeps_only_sgr() -> None:
    from agenthicc.tui.statusline import sanitize_status_output

    raw = b"\n\x1b[1mok\x1b[0m \x1b]0;title\x07\x1b[2Jdone\x07\ttab\nsecond line\n"
    assert sanitize_status_output(raw) == "\x1b[1mok\x1b[0m done tab"


def test_an_untrusted_project_cannot_configure_the_command(tmp_path: Path) -> None:
    from agenthicc.config import load_config
    from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust

    project = tmp_path / "agenthicc.toml"
    project.write_text('[ui.statusline]\ncommand = "echo hi"\n', encoding="utf-8")
    user = tmp_path / "user.toml"
    user.write_text("", encoding="utf-8")
    untrusted = ExtensionTrust(store_path=tmp_path / "trust.json", trusted_roots=())
    with override_extension_trust(untrusted):
        assert load_config(project, user, env_overrides=False).ui.statusline_command == ""
        untrusted.trust(project, kind="statusline")
        assert load_config(project, user, env_overrides=False).ui.statusline_command == "echo hi"


async def test_the_workspace_runs_the_configured_command(tmp_path: Path) -> None:
    state = _state(ui=UISettings(statusline_command="echo from-user"))
    state.conversation.current_directory.set(str(tmp_path))
    workspace = _workspace(state)
    workspace.start()
    try:
        for _ in range(100):
            if state.conversation.statusline_text():
                break
            await asyncio.sleep(0.01)
        await settle_frames(workspace)
        assert any("from-user" in row for row in lines_text(workspace._live.lines))  # type: ignore[union-attr]
    finally:
        workspace.stop()


# ── 6. The hub ────────────────────────────────────────────────────────────────


class _HubLike(Overlay):
    """An overlay that owns the bottom rows, with its own hint row."""

    name = "agents"
    owns_bottom_rows = True

    def render(self) -> Text:
        return Text("Running  Needs you  History\n  session-a\nEnter open · Esc close")

    def handle_key(self, key: object, ch: str) -> bool:  # type: ignore[override]
        return True


def test_the_agents_hub_overlay_owns_the_bottom_rows() -> None:
    from agenthicc.tui.workspace.agents_hub.overlay import AgentsHubOverlay

    assert AgentsHubOverlay.owns_bottom_rows is True
    assert Overlay.owns_bottom_rows is False


def test_with_the_hub_open_the_frame_has_one_hint_row() -> None:
    state = _state()
    state.conversation.begin_turn("agent", "turn-1")  # the composer would hint
    workspace = _workspace(state)
    workspace.overlays._overlay = _HubLike()
    rows = lines_text(workspace._build_lines())
    hint_rows = [row for row in rows if "Esc" in row]
    assert hint_rows == ["Enter open · Esc close"]
    assert not any(row.startswith(("╭", "│", "╰", "❯")) for row in rows)


def test_closing_the_hub_restores_the_composer_exactly() -> None:
    state = _state()
    state.input.update(list("draft\nsecond"), 3, paste_condensed=False)
    workspace = _workspace(state)
    before = workspace._build_lines()
    workspace.overlays.show(_HubLike())
    assert workspace._build_lines() != before
    workspace.overlays.hide()
    after: list[Line] = workspace._build_lines()
    assert lines_text(after) == lines_text(before)
    assert state.input.cursor() == 3


# ── 7. /status ────────────────────────────────────────────────────────────────


def _run_status(state: AppState) -> str:
    from agenthicc.commands.builtins import _cmd_status
    from agenthicc.commands.command import CommandContext
    from agenthicc.config import AgenthiccConfig

    console = Console(record=True, width=200, color_system=None)
    context = CommandContext(
        text="/status",
        args="",
        model="anthropic/claude-opus-4-8",
        console=console,
        config=AgenthiccConfig(),
        session_id=_SESSION_ID,
        app_state=state,
    )
    assert _cmd_status(context) is True
    return console.export_text()


def test_status_prints_the_session_and_explains_absent_cost() -> None:
    state = _state()
    state.conversation.context_window.set(1_000_000)
    text = _run_status(state)
    assert _SESSION_ID in text
    assert "/root/python_projects/agenthicc" in text
    assert "not reported by anthropic" in text
    assert "Mode:" in text and "Safe" in text
    assert "no response yet" in text
    assert "conversation.jsonl" in text and "conversation-journal.jsonl" in text


def test_status_reports_known_usage_and_context() -> None:
    state = _snapshot_state()
    text = _run_status(state)
    assert "12,400 in, 3,100 out" in text
    assert "$0.4200" in text
    assert "12% (120,000 of 1,000,000 tokens)" in text
    assert "main (uncommitted changes)" in text


def test_status_is_in_the_picker_and_help() -> None:
    from agenthicc.commands import build_builtin_registry
    from agenthicc.tui.trigger import TriggerContext
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.workspace.overlays.help import HelpOverlay

    registry = build_builtin_registry()
    matches = SlashCommandTrigger(registry).get_matches("sta", TriggerContext(cwd=Path.cwd()))
    assert "/status" in [match.value for match in matches]
    help_overlay = HelpOverlay(registry, on_close=lambda: None)
    assert any(getattr(row, "name", "") == "/status" for row in help_overlay._rows)


# ── 8. Screen reader ──────────────────────────────────────────────────────────


async def test_screen_reader_has_no_frame_and_says_the_status_on_change() -> None:
    state = _state(ui=UISettings(screen_reader=True))
    output = StringIO()
    workspace = Workspace(state, Console(file=output, force_terminal=True, width=100, height=30))
    workspace.start()
    try:
        await settle_frames(workspace)
        rows = lines_text(workspace._live.lines)  # type: ignore[union-attr]
        assert not any(ch in "".join(rows) for ch in "╭╮╰╯─│")
        assert rows[-1] == "Idle, opus-4.8"
        state.conversation.context_window.set(1_000_000)
        state.conversation.context_tokens.set(120_000)
        state.conversation.context_tokens.set(120_000)
        state.conversation.agent_state.set(AgentState.COMPLETE)
        await settle_frames(workspace)
        await asyncio.sleep(0.01)
    finally:
        workspace.stop()
    text = output.getvalue()
    assert text.count("Idle. Opus 4.8.") == 2  # before and after the context change
    assert text.count("Idle. Opus 4.8. Context 12 percent.") == 1


# ── session title ─────────────────────────────────────────────────────────────


def test_the_first_prompt_titles_the_session() -> None:
    state = _state()
    conv = state.conversation
    conv.append_event("user_message", {"text": "/model"})
    assert conv.session_title() == ""
    conv.append_event("user_message", {"text": "\n  fix   flaky\nscheduler test"})
    conv.append_event("user_message", {"text": "something else"})
    assert conv.session_title() == "fix flaky"
    assert session_title_from("x" * 200) == "x" * 80


def test_a_resumed_transcript_restores_the_title() -> None:
    from agenthicc.tui.conversation_store import ConversationEvent

    state = _state()
    state.conversation.remember_events(
        [ConversationEvent("e1", "user_message", {"text": "port the parser"})]
    )
    assert state.conversation.session_title() == "port the parser"
    assert _rows(state)[0].endswith(" port the parser · Safe ─")


# ── performance (AGX-11) ──────────────────────────────────────────────────────


def test_segments_are_rebuilt_only_when_their_signals_change() -> None:
    from agenthicc.tui.workspace import components

    state = _snapshot_state()
    state.conversation.begin_turn("agent", "turn-1")
    line = StatusLine(state)
    line.markup(120)
    components._static_parts.cache_clear()
    line.markup(120)
    for _ in range(10):
        state.conversation.frame.set(state.conversation.frame() + 1)
        line.markup(120)
    info = components._static_parts.cache_info()
    assert info.misses == 1 and info.hits == 10
    state.conversation.git_branch.set("feature")
    line.markup(120)
    assert components._static_parts.cache_info().misses == 2


def test_git_branch_is_read_from_head(tmp_path: Path) -> None:
    from agenthicc.tui.statusline import read_git_branch

    assert read_git_branch(tmp_path) == ""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    assert read_git_branch(nested) == "feature/x"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    gitdir = tmp_path / "gitdirs" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("0123456789abcdef\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert read_git_branch(worktree) == "0123456"


# ── the flower and the bold letter (kept by request) ─────────────────────────


def _thinking_markup(state: AppState, frame: int) -> str:
    state.conversation.agent_state.set(AgentState.THINKING)
    state.conversation.frame.set(frame)
    return StatusLine(state).markup(120)


def test_thinking_turns_the_flower_and_sweeps_one_bold_letter() -> None:
    state = _state()
    first = _thinking_markup(state, 0)
    second = _thinking_markup(state, 1)
    assert "✿" in first and "❀" in second
    assert "[bold]T[/bold]hinking" in first
    assert "T[bold]h[/bold]inking" in second
    assert _plain(StatusLine(state).markup(120)).lstrip().startswith("❀ Thinking")


def test_the_bold_letter_bounces_back_at_the_end_of_the_word() -> None:
    state = _state()
    # "Thinking" has 8 letters: frames 0–7 go right, 8–13 come back.
    assert "Thinkin[bold]g[/bold]" in _thinking_markup(state, 7)
    assert "Thinki[bold]n[/bold]g" in _thinking_markup(state, 8)
    assert "[bold]T[/bold]hinking" in _thinking_markup(state, 14)


def test_reduced_motion_keeps_the_word_and_drops_the_animation() -> None:
    state = _state(ui=UISettings(reduced_motion=True))
    markup = _thinking_markup(state, 3)
    assert "[bold]" not in markup.split("Thinking")[0][-12:]
    assert "Thinking" in _plain(markup) and "✿" not in markup
