"""Cross-boundary PRD-149 coverage for commands and the reactive TUI."""

from __future__ import annotations

import asyncio
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, CommandDispatcher, build_builtin_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.background.terminals import TerminalManager
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.components import StatusComponent
from agenthicc.tui.workspace.overlays.terminals import TerminalListOverlay

pytestmark = pytest.mark.integration


def _context(manager: TerminalManager, console: Console) -> CommandContext:
    return CommandContext(
        text="",
        args="",
        model="test/model",
        console=console,
        config=AgenthiccConfig(),
        session_id=manager.session_id,
        terminal_manager=manager,
    )


async def test_ps_json_and_stop_are_immediate_owned_controls(tmp_path: Path) -> None:
    manager = TerminalManager(
        session_id="integration-session",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
        cancel_grace_s=0.1,
    )
    started = await manager.start(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        label="integration long run",
    )
    terminal_id = str(started["terminal_id"])
    output = StringIO()
    console = Console(file=output, force_terminal=False, markup=False)
    dispatcher = CommandDispatcher(build_builtin_registry())
    context = _context(manager, console)

    assert dispatcher.dispatch("/ps --json", context) is True
    assert terminal_id in output.getvalue()
    assert dispatcher.dispatch(f"/stop {terminal_id}", context) is True
    await asyncio.sleep(0.2)
    assert manager.get(terminal_id) is not None
    assert manager.get(terminal_id).state.value == "stopped"
    await manager.close()


async def test_stop_without_terminal_id_stops_all_owned_terminals(tmp_path: Path) -> None:
    manager = TerminalManager(
        session_id="stop-all-session",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
        cancel_grace_s=0.1,
    )
    started = [
        await manager.start(
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            label=f"all-{index}",
        )
        for index in range(2)
    ]
    terminal_ids = [str(item["terminal_id"]) for item in started]
    output = StringIO()
    console = Console(file=output, force_terminal=False, markup=False)
    dispatcher = CommandDispatcher(build_builtin_registry())
    context = _context(manager, console)

    assert dispatcher.dispatch("/stop", context) is True
    await asyncio.gather(*(manager.wait(terminal_id) for terminal_id in terminal_ids))
    assert all(manager.get(terminal_id).state.value == "stopped" for terminal_id in terminal_ids)
    assert "Stop requested for 2 background terminal(s)." in output.getvalue()
    await manager.close()


async def test_terminal_overlay_can_select_and_stop_a_live_handle(tmp_path: Path) -> None:
    manager = TerminalManager(
        session_id="overlay-session",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
        cancel_grace_s=0.1,
    )
    started = await manager.start(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        label="overlay task",
    )
    terminal_id = str(started["terminal_id"])
    closed: list[bool] = []
    overlay = TerminalListOverlay(manager, lambda: closed.append(True), selected_id=terminal_id)
    overlay.on_mount()
    rendered = StringIO()
    Console(file=rendered, force_terminal=False).print(overlay.render())
    assert terminal_id in rendered.getvalue()
    overlay.handle_key(Key.CHAR, "s")
    await asyncio.sleep(0.2)
    assert manager.get(terminal_id) is not None
    assert manager.get(terminal_id).state.value == "stopped"
    overlay.handle_key(Key.ESC, "")
    assert closed
    overlay.on_unmount()
    await manager.close()


def test_wait_status_is_width_safe_and_contains_control_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenthicc.tui.workspace.components as components

    monkeypatch.setattr(components, "_get_cols", lambda: 200)
    state = AppState.create()
    state.conversation.model_name.set("test/model")
    state.conversation.session_id.set("session")
    state.conversation.set_terminal_wait(
        terminal_id="term-1234",
        label="uv run pytest tests/unit -q",
        elapsed_s=371.0,
        running_count=2,
    )
    output = StringIO()
    Console(file=output, force_terminal=False, markup=False, width=200).print(
        StatusComponent(state).render()
    )
    text = output.getvalue()
    assert "Waiting for background terminal (6m 11s" in text
    assert "Esc to interrupt" in text
    assert "2 background terminals running" in text
    assert "/ps to view" in text
    assert "/stop to stop all" in text
    assert "└ uv run pytest tests/unit -q" in text
    assert StatusComponent(state).height(200) == 4
