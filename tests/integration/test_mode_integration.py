"""Integration coverage for canonical mode identity, aliases, and commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agenthicc.commands.builtins import _cmd_mode
from agenthicc.commands.command import CommandContext
from agenthicc.modes import discover_mode_plugins
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager

pytestmark = pytest.mark.integration


def _command_context(manager: ModeManager, args: str, console: MagicMock) -> CommandContext:
    return CommandContext(
        text=f"/mode {args}".strip(),
        args=args,
        model=MagicMock(),
        console=console,
        config=MagicMock(),
        session_id="mode-integration",
        mode_manager=manager,
    )


def test_runtime_manager_starts_safe_and_cycles_only_selectable_modes() -> None:
    app = AppState.create()
    manager = ModeManager(app_state=app)

    assert manager.active_name == "Safe"
    assert app.active_mode().name == "Safe"
    assert [manager.cycle().name for _ in range(4)] == ["Plan", "Yolo", "Safe", "Plan"]
    assert [mode.name for mode in manager.registry.all()] == ["Safe", "Plan", "Yolo"]


@pytest.mark.parametrize(
    ("input_name", "canonical"),
    [("Auto", "Yolo"), ("Guard", "Safe"), ("Ask", "Safe"), ("Review", "Plan")],
)
def test_runtime_manager_migrates_legacy_aliases(input_name: str, canonical: str) -> None:
    manager = ModeManager()

    selected = manager.set_by_name(input_name)

    assert selected is not None
    assert selected.name == canonical
    assert manager.active_name == canonical


def test_runtime_manager_rejects_debug_and_reports_canonical_choices() -> None:
    manager = ModeManager()
    console = MagicMock()

    _cmd_mode(_command_context(manager, "Debug", console))

    assert manager.active_name == "Safe"
    output = " ".join(str(call.args[0]) for call in console.print.call_args_list)
    assert "Safe" in output and "Plan" in output and "Yolo" in output
    assert "Debug" not in manager.registry.selectable_names()


def test_mode_command_lists_only_the_three_user_modes() -> None:
    manager = ModeManager()
    console = MagicMock()

    _cmd_mode(_command_context(manager, "", console))

    table = console.print.call_args_list[0].args[0]
    assert [cell for cell in table.columns[0]._cells] == ["Safe", "Plan", "Yolo"]
    assert "Replay" not in table.columns[0]._cells


def test_legacy_mode_plugin_is_discoverable_but_does_not_replace_policy(
    tmp_path: Path,
) -> None:
    modes_dir = tmp_path / ".agenthicc" / "modes"
    modes_dir.mkdir(parents=True)
    (modes_dir / "custom.py").write_text(
        "from agenthicc.modes import Mode\nMODE = Mode('Custom', 'CUSTOM', 'Custom mode')\n",
        encoding="utf-8",
    )

    plugins = discover_mode_plugins(project_dir=tmp_path)

    assert not plugins.failed
    assert [mode.name for mode in plugins.all_modes] == ["Custom"]


def test_mode_command_accepts_auto_alias_as_yolo() -> None:
    manager = ModeManager()
    console = MagicMock()

    _cmd_mode(_command_context(manager, "Auto", console))

    assert manager.active_name == "Yolo"
