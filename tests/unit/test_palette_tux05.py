"""TUX-05 AC5: the Ctrl+P palette, and the pickers behind ``/model``, ``/mode`` and ``/agent``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.commands import UnifiedCommandRegistry, build_builtin_registry
from agenthicc.commands.command import Command
from agenthicc.tui.actions import DEFAULT_ACTIONS, PALETTE_ACTIONS
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.theme import glyphs
from agenthicc.tui.runtime.commands import CommandBus, OpenPaletteCommand
from agenthicc.tui.trigger import TriggerManager
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.workspace.agents_hub.overlay import AgentsHubOverlay
from agenthicc.tui.workspace.overlays.palette import (
    CommandPaletteOverlay,
    PaletteItem,
    palette_width,
)
from agenthicc.tui.workspace.picker import PickerRow
from agenthicc.workflows.registry import WorkflowRegistry
from tests.unit.test_agents_hub_in_session import _session

pytestmark = pytest.mark.unit


def _palette(
    tmp_path: Path, ran: list[PaletteItem], *, registry: UnifiedCommandRegistry | None = None
) -> CommandPaletteOverlay:
    return CommandPaletteOverlay(
        commands=SlashCommandTrigger(registry or build_builtin_registry(), WorkflowRegistry()),
        cwd=tmp_path,
        keymap=Keymap.build(DEFAULT_ACTIONS),
        actions=PALETTE_ACTIONS,
        themes=("dark", "light"),
        run=ran.append,
        close=lambda: ran.append(PaletteItem("insert", "<closed>")),
    )


def _type(overlay: CommandPaletteOverlay, text: str) -> None:
    for ch in text:
        overlay.handle_key(Key.CHAR, ch)


def _select(overlay: CommandPaletteOverlay, label: str) -> None:
    rows = overlay.picker.rows
    overlay.picker.selected = next(i for i, row in enumerate(rows) if row.label == label)


# ── Ctrl+P on the composer ───────────────────────────────────────────────────


def _composer() -> tuple[AppState, UnifiedInputSession, list[OpenPaletteCommand]]:
    state = AppState.create()
    bus = CommandBus()
    opened: list[OpenPaletteCommand] = []
    bus.register(OpenPaletteCommand, opened.append)
    return state, UnifiedInputSession(state, bus), opened


async def test_ctrl_p_on_an_empty_prompt_opens_the_palette() -> None:
    _state, session, opened = _composer()
    await session._dispatch(Key.CTRL_P, "")
    assert len(opened) == 1


async def test_ctrl_p_on_a_non_empty_prompt_keeps_its_history_meaning() -> None:
    state, session, opened = _composer()
    for ch in "ab":
        await session._dispatch(Key.CHAR, ch)
    await session._dispatch(Key.CTRL_P, "")
    assert opened == []
    assert "".join(state.input.buf()) == "ab"


def test_ctrl_p_is_bound_to_the_palette_action() -> None:
    action = Keymap.build(DEFAULT_ACTIONS).resolve("composer", "ctrl+p")
    assert action is not None and action.name == "composer.palette"


# ── the palette ──────────────────────────────────────────────────────────────


def test_typing_agents_finds_open_agents_hub_with_its_key(tmp_path: Path) -> None:
    ran: list[PaletteItem] = []
    overlay = _palette(tmp_path, ran)
    _type(overlay, "agents")
    row = next(r for r in overlay.picker.rows if r.label == "Open agents hub")
    assert row.keys == "ctrl+g"
    assert row.value == PaletteItem("action", "agents.open_hub")
    # The /agents command ranks first; the key action is in the same ranked list.
    assert overlay.picker.rows[0].label == "/agents"
    _select(overlay, "Open agents hub")
    overlay.handle_key(Key.ENTER, "")
    assert ran == [PaletteItem("insert", "<closed>"), PaletteItem("action", "agents.open_hub")]


def test_an_empty_palette_lists_commands_key_actions_and_themes(tmp_path: Path) -> None:
    overlay = _palette(tmp_path, [])
    labels = [row.label for row in overlay.picker.rows]
    assert "/compact" in labels and "Cycle mode" in labels
    assert "Theme: dark" in labels and "Theme: light" in labels
    groups = list(dict.fromkeys(row.group for row in overlay.picker.rows))
    assert groups.index("Session") < groups.index("Keys") < groups.index("Themes")
    cycle = next(r for r in overlay.picker.rows if r.label == "Cycle mode")
    assert cycle.keys == "shift+tab"


def test_enter_runs_a_command_a_theme_and_esc_closes(tmp_path: Path) -> None:
    ran: list[PaletteItem] = []
    overlay = _palette(tmp_path, ran)
    _type(overlay, "compact")
    overlay.handle_key(Key.ENTER, "")
    assert ran[-1] == PaletteItem("command", "/compact")
    ran.clear()
    overlay = _palette(tmp_path, ran)
    _type(overlay, "light")
    _select(overlay, "Theme: light")
    overlay.handle_key(Key.ENTER, "")
    assert ran[-1] == PaletteItem("theme", "light")
    ran.clear()
    overlay = _palette(tmp_path, ran)
    overlay.handle_key(Key.ESC, "")
    assert ran == [PaletteItem("insert", "<closed>")]


def test_a_command_that_needs_an_argument_is_inserted_not_run(tmp_path: Path) -> None:
    registry = UnifiedCommandRegistry()
    registry.register(Command("/open", "Open a thing", argument_hint="<name>", group="Session"))
    ran: list[PaletteItem] = []
    overlay = _palette(tmp_path, ran, registry=registry)
    _type(overlay, "open")
    overlay.handle_key(Key.ENTER, "")
    assert ran[-1] == PaletteItem("insert", "/open ")


def test_a_command_with_argument_completions_opens_them_in_the_same_modal(
    tmp_path: Path,
) -> None:
    registry = UnifiedCommandRegistry()
    registry.register(
        Command(
            "/pick",
            "Pick one",
            group="Session",
            completions_factory=lambda prefix: [
                n for n in ("alpha", "beta") if n.startswith(prefix)
            ],
        )
    )
    ran: list[PaletteItem] = []
    overlay = _palette(tmp_path, ran, registry=registry)
    _type(overlay, "pick")
    overlay.handle_key(Key.ENTER, "")
    assert overlay.command == "/pick" and ran == []
    assert [row.label for row in overlay.picker.rows] == ["/pick alpha", "/pick beta"]
    _type(overlay, "b")
    assert [row.label for row in overlay.picker.rows] == ["/pick beta"]
    overlay.handle_key(Key.ENTER, "")
    assert ran[-1] == PaletteItem("command", "/pick beta")


def test_palette_width_is_sixty_percent_clamped_to_sixty_to_one_hundred() -> None:
    assert palette_width(200) == 100
    assert palette_width(120) == 72
    assert palette_width(80) == 60
    assert palette_width(50) == 50  # never wider than the terminal


@pytest.mark.parametrize("columns", [50, 80, 120])
def test_no_palette_line_exceeds_the_terminal_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, columns: int
) -> None:
    overlay = _palette(tmp_path, [])
    overlay.set_viewport(columns, 40)
    console = Console(width=columns, record=True, color_system=None)
    console.print(overlay.render())
    lines = console.export_text().splitlines()
    assert lines and all(len(line) <= columns for line in lines)
    assert lines[0].strip().startswith("╭") or lines[0].strip()[0] in "+╭"


# ── opened from the session ──────────────────────────────────────────────────


async def test_the_session_palette_opens_the_agents_hub_on_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, monkeypatch)
    registry = build_builtin_registry()
    triggers = TriggerManager()
    triggers.register(SlashCommandTrigger(registry, WorkflowRegistry()))
    session._ctx.trigger_registry = triggers
    session._ctx.cmd_registry = registry
    session._ctx.workflow_registry = WorkflowRegistry()
    session._ctx.command_bus = CommandBus()
    session._ctx.app_state.keymap = lambda: Keymap.build(DEFAULT_ACTIONS)  # type: ignore[method-assign]
    monkeypatch.chdir(tmp_path)
    overlays = session._workspace.overlays

    session.open_palette()
    palette = overlays.widget
    assert isinstance(palette, CommandPaletteOverlay)
    _type(palette, "agents")
    _select(palette, "Open agents hub")
    palette.handle_key(Key.ENTER, "")
    hub = overlays.widget
    assert isinstance(hub, AgentsHubOverlay)
    hub.handle_key(Key.ESC, "")
    await asyncio.sleep(0.05)


# ── the choice pickers ───────────────────────────────────────────────────────


def test_a_choice_picker_chooses_on_enter_and_refuses_a_disabled_row() -> None:
    from agenthicc.tui.workspace.overlays.choice_picker import ChoicePickerOverlay

    chosen: list[str] = []
    closed: list[bool] = []

    def choose(value: str) -> str:
        chosen.append(value)
        return f"chose {value}"

    overlay: ChoicePickerOverlay[str] = ChoicePickerOverlay(
        "Mode",
        [
            PickerRow("blocked", "Blocked", disabled="not available"),
            PickerRow("safe", "Safe", "asks first"),
        ],
        choose=choose,
        close=lambda: closed.append(True),
    )
    overlay.handle_key(Key.ENTER, "")
    assert chosen == [] and overlay.status == "not available" and closed == []
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")
    assert chosen == ["safe"] and closed == [True]
    overlay.handle_key(Key.ESC, "")
    assert closed == [True, True]


def test_a_keep_open_choice_picker_runs_a_follow_up_key_on_the_choice() -> None:
    from agenthicc.tui.workspace.overlays.choice_picker import ChoicePickerOverlay

    saved: list[str] = []
    overlay: ChoicePickerOverlay[str] = ChoicePickerOverlay(
        "Model",
        [PickerRow("a", "a"), PickerRow("b", "b")],
        choose=lambda value: f"using {value}",
        close=lambda: None,
        keep_open=True,
        extra_keys={"s": ("save as default", lambda value: saved.append(value) or "saved")},
    )
    overlay.handle_key(Key.CHAR, "s")  # nothing chosen yet
    assert saved == []
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")
    assert overlay.status == "using b"
    overlay.handle_key(Key.CHAR, "s")
    assert saved == ["b"] and overlay.status == "saved"


def test_mode_picker_lists_the_modes_and_switches() -> None:
    from agenthicc.commands.command import CommandContext
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.runtime import ModeManager
    from agenthicc.tui.workspace.overlay import OverlayHost
    from agenthicc.tui.workspace.overlays.choice_picker import ChoicePickerOverlay

    state = AppState.create()
    modes = ModeManager(app_state=state)
    host = OverlayHost(state)
    ctx = CommandContext(
        text="/mode",
        args="",
        model="m",
        console=Console(record=True),
        config=AgenthiccConfig(),
        session_id="s",
        mode_manager=modes,
        set_pending_menu=host.show,
        close_overlay=host.hide,
    )
    assert build_builtin_registry().get("/mode").handler(ctx)  # type: ignore[union-attr]
    overlay = host.widget
    assert isinstance(overlay, ChoicePickerOverlay)
    labels = [row.label for row in overlay.picker.rows]
    assert {"Safe", "Plan", "Yolo"} <= set(labels)
    overlay.picker.selected = labels.index("Plan")
    overlay.handle_key(Key.ENTER, "")
    assert modes.active_name == "Plan"


def test_the_palette_search_line_uses_the_composers_block_cursor(tmp_path: Path) -> None:
    overlay = _palette(tmp_path, [])
    _type(overlay, "ag")
    console = Console(width=80, record=True, color_system=None)
    console.print(overlay.render())
    search = console.export_text().splitlines()[1]
    assert "ag" in search and glyphs().cursor not in search
    # The cursor is a reversed space, as build_prompt draws it.
    from agenthicc.tui.input.renderer import build_prompt

    prompt = build_prompt(list("ag"), 2)
    assert any(str(span.style) == "reverse" for span in prompt.spans)
