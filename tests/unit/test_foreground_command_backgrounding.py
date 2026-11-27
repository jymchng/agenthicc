"""Moving a running foreground command to a background terminal (Ctrl+B).

``run_bash`` and ``run_command`` register the command they wait on with the
session's :class:`TerminalManager`.  A request to background it hands the live
process to the manager: the tool call returns a receipt at once and the process
keeps running as an owned terminal.  Every test uses a real subprocess and
synchronises on a flag file, never on a sleep.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agenthicc.background.terminals import ForegroundCommand, TerminalManager, TerminalState
from agenthicc.tools.exec import RunBashTool, RunCommandTool

if TYPE_CHECKING:
    from agenthicc.runners.tui_session import TUISession

pytestmark = pytest.mark.unit

#: Prints ``before``, waits for the flag file named by argv[1], prints ``after``.
_WAIT_FOR_FLAG = (
    "import pathlib, sys, time\n"
    "print('before', flush=True)\n"
    "while not pathlib.Path(sys.argv[1]).exists():\n"
    "    time.sleep(0.02)\n"
    "print('after')\n"
)


def _manager(tmp_path: Path, **kwargs: object) -> TerminalManager:
    return TerminalManager(
        session_id="session-test",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
        **kwargs,
    )


def _context(tmp_path: Path, manager: TerminalManager) -> dict[str, object]:
    return {
        "workspace_root": str(tmp_path),
        "terminal_manager": manager,
        "tool_call_id": "call-1",
    }


async def _foreground(manager: TerminalManager) -> ForegroundCommand:
    """Wait until the tool call has registered the command it is waiting on."""
    for _ in range(500):
        commands = manager.foreground_commands()
        if commands:
            return commands[0]
        await asyncio.sleep(0.01)
    raise AssertionError("the command never registered as a foreground command")


async def _requested_handled(command: ForegroundCommand) -> None:
    """Wait until a refused request was withdrawn, so the user may ask again."""
    for _ in range(500):
        if not command.requested.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the refused request was never withdrawn")


async def test_a_foreground_command_is_registered_while_the_tool_call_waits(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    flag = tmp_path / "go"
    call = asyncio.create_task(
        RunCommandTool().execute(
            {"argv": [sys.executable, "-c", _WAIT_FOR_FLAG, str(flag)], "label": "waiter"},
            _context(tmp_path, manager),
        )
    )
    command = await _foreground(manager)
    assert command.tool_call_id == "call-1"
    assert command.label == "waiter"
    assert not call.done()

    flag.touch()
    result = await call

    assert result["ok"] is True and result["stdout"] == "before\nafter\n"
    assert "backgrounded" not in result
    assert manager.foreground_commands() == []
    assert manager.list_records() == []


async def test_a_command_moved_to_the_background_keeps_running_as_a_terminal(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    flag = tmp_path / "go"
    call = asyncio.create_task(
        RunCommandTool().execute(
            {"argv": [sys.executable, "-c", _WAIT_FOR_FLAG, str(flag)], "label": "waiter"},
            _context(tmp_path, manager),
        )
    )
    await _foreground(manager)

    assert manager.request_background_foreground() == 1
    receipt = await asyncio.wait_for(call, timeout=10)

    assert receipt["ok"] is True
    assert receipt["backgrounded"] is True and receipt["background"] is True
    assert receipt["state"] == "running"
    assert receipt["tool_call_id"] == "call-1"
    terminal_id = receipt["terminal_id"]
    assert isinstance(terminal_id, str) and terminal_id in str(receipt["message"])
    assert manager.foreground_commands() == []
    record = manager.get(terminal_id)
    assert record is not None and record.active and record.label == "waiter"

    flag.touch()
    finished = await asyncio.wait_for(manager.wait(terminal_id), timeout=10)
    assert finished["state"] == TerminalState.EXITED.value
    assert finished["returncode"] == 0
    # Everything the process wrote shows up once, whichever side read it.
    assert finished["stdout"] == "before\nafter\n"
    await manager.close()


async def test_a_shell_pipeline_can_be_moved_to_the_background(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    flag = tmp_path / "go"
    script = tmp_path / "waiter.py"
    script.write_text(_WAIT_FOR_FLAG, encoding="utf-8")
    command = f"{sys.executable} {script} {flag} | tail -n 80"
    call = asyncio.create_task(
        RunBashTool().execute({"command": command}, _context(tmp_path, manager))
    )
    await _foreground(manager)

    assert manager.request_background_foreground() == 1
    receipt = await asyncio.wait_for(call, timeout=10)
    assert receipt["backgrounded"] is True
    terminal_id = str(receipt["terminal_id"])

    flag.touch()
    finished = await asyncio.wait_for(manager.wait(terminal_id), timeout=10)
    assert finished["state"] == TerminalState.EXITED.value
    assert finished["stdout"] == "before\nafter\n"
    await manager.close()


async def test_the_commands_deadline_goes_with_it_to_the_background(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    call = asyncio.create_task(
        RunBashTool().execute(
            {"command": "sleep 60", "timeout": 2.0},
            _context(tmp_path, manager),
        )
    )
    await _foreground(manager)
    assert manager.request_background_foreground() == 1
    receipt = await asyncio.wait_for(call, timeout=10)
    terminal_id = str(receipt["terminal_id"])

    record = manager.get(terminal_id)
    assert record is not None
    # Backgrounding does not buy more time: what was left of the 2 s is what is left.
    assert 0.0 < record.timeout_s <= 2.0

    finished = await asyncio.wait_for(manager.wait(terminal_id), timeout=20)
    assert finished["state"] == TerminalState.TIMED_OUT.value
    await manager.close()


async def test_a_request_with_no_free_terminal_leaves_the_command_in_the_foreground(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, max_terminals=1)
    blocker = await manager.start(command="sleep 60", shell=True, label="blocker")
    assert blocker["ok"] is True
    assert manager.background_blocker() is not None

    flag = tmp_path / "go"
    call = asyncio.create_task(
        RunCommandTool().execute(
            {"argv": [sys.executable, "-c", _WAIT_FOR_FLAG, str(flag)]},
            _context(tmp_path, manager),
        )
    )
    command = await _foreground(manager)
    command.request_background()
    await _requested_handled(command)

    # The refusal changed nothing: still waiting, still registered, asked again later.
    assert not call.done()
    assert manager.foreground_commands() == [command]
    assert [record.terminal_id for record in manager.list_records()] == [blocker["terminal_id"]]

    flag.touch()
    result = await asyncio.wait_for(call, timeout=10)
    assert result["ok"] is True and result["stdout"] == "before\nafter\n"
    assert "backgrounded" not in result
    await manager.close()


async def test_a_command_that_already_finished_is_not_backgrounded(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "-c", "print('done')"]},
        _context(tmp_path, manager),
    )
    # Too late to ask: nothing is registered and the request has no one to reach.
    assert result["stdout"] == "done\n"
    assert manager.request_background_foreground() == 0


def _session_with(manager: TerminalManager) -> tuple[TUISession, list[str]]:
    """A TUI session over *manager*, and the notices its conversation shows."""
    from tests.unit.test_tui_session_coverage import _make_session

    session, ctx, _workspace, _input = _make_session()
    ctx.terminal_manager = manager
    notices: list[str] = []
    ctx.app_state.conversation.notify_transient = notices.append  # type: ignore[method-assign]
    return cast("TUISession", session), notices


async def test_ctrl_b_in_the_session_moves_the_running_command(tmp_path: Path) -> None:
    from agenthicc.tui.runtime.commands import BackgroundWorkCommand

    manager = _manager(tmp_path)
    session, notices = _session_with(manager)
    conversation = session._ctx.app_state.conversation
    flag = tmp_path / "go"
    call = asyncio.create_task(
        RunCommandTool().execute(
            {"argv": [sys.executable, "-c", _WAIT_FOR_FLAG, str(flag)]},
            _context(tmp_path, manager),
        )
    )
    await _foreground(manager)

    # While the command runs the session knows Ctrl+B has something to move.
    session._sync_terminal_status()
    assert conversation.foreground_commands() and conversation.backgroundable()

    await session._background_work_command(BackgroundWorkCommand())
    assert notices == ["◈ Backgrounding 1 command"]
    receipt = await asyncio.wait_for(call, timeout=10)
    assert receipt["backgrounded"] is True

    # Once it is a terminal, Ctrl+B has nothing left to move.
    session._sync_terminal_status()
    assert not conversation.foreground_commands() and not conversation.backgroundable()
    await session._background_work_command(BackgroundWorkCommand())
    assert notices[-1] == "Nothing in the foreground to background"

    flag.touch()
    await asyncio.wait_for(manager.wait(str(receipt["terminal_id"])), timeout=10)
    await manager.close()


async def test_ctrl_b_says_why_a_command_cannot_be_moved(tmp_path: Path) -> None:
    from agenthicc.tui.runtime.commands import BackgroundWorkCommand

    manager = _manager(tmp_path, max_terminals=1)
    blocker = await manager.start(command="sleep 60", shell=True, label="blocker")
    assert blocker["ok"] is True
    session, notices = _session_with(manager)
    flag = tmp_path / "go"
    call = asyncio.create_task(
        RunCommandTool().execute(
            {"argv": [sys.executable, "-c", _WAIT_FOR_FLAG, str(flag)]},
            _context(tmp_path, manager),
        )
    )
    await _foreground(manager)

    await session._background_work_command(BackgroundWorkCommand())

    assert len(notices) == 1 and "session terminal limit reached (1)" in notices[0]
    assert not call.done()  # it keeps running in the foreground, untouched
    flag.touch()
    assert (await asyncio.wait_for(call, timeout=10))["stdout"] == "before\nafter\n"
    await manager.close()


async def test_without_a_manager_the_command_is_an_ordinary_foreground_run(
    tmp_path: Path,
) -> None:
    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "-c", "print('plain')"]},
        {"workspace_root": str(tmp_path)},
    )
    assert result["ok"] is True and result["stdout"] == "plain\n"
