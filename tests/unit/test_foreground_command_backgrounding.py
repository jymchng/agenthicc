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

from agenthicc.background.terminals import (
    ForegroundCommand,
    TerminalManager,
    TerminalState,
    bind_foreground_offer,
    foreground_commands_offered,
)
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


async def test_a_command_run_where_commands_are_withheld_is_never_offered(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    flag, started = tmp_path / "go", tmp_path / "started"
    script = (
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[2]).touch()\n"
        "while not pathlib.Path(sys.argv[1]).exists():\n"
        "    time.sleep(0.02)\n"
        "print('ran')\n"
    )
    with bind_foreground_offer(False):
        assert foreground_commands_offered() is False
        # A task started inside the block inherits it, as a worker's tool calls do.
        call = asyncio.create_task(
            RunCommandTool().execute(
                {"argv": [sys.executable, "-c", script, str(flag), str(started)]},
                _context(tmp_path, manager),
            )
        )
    assert foreground_commands_offered() is True
    for _ in range(500):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.exists(), "the command never started"

    # It is running, yet nothing is registered, so Ctrl+B has nothing to move.
    assert manager.foreground_commands() == []
    assert manager.request_background_foreground() == 0

    flag.touch()
    assert (await asyncio.wait_for(call, timeout=10))["stdout"] == "ran\n"


async def test_a_subagent_workers_commands_are_not_offered_for_ctrl_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker is detached with its pool; its commands are not the turn's to move."""
    import agenthicc.subagents.pool as pool_module
    from agenthicc.subagents.pool import SubagentTask
    from agenthicc.subagents.types import SubagentTypeSpec
    from agenthicc.tools.surface import ToolSurface

    manager = _manager(tmp_path)
    flag, started = tmp_path / "go", tmp_path / "started"
    script = (
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[2]).touch()\n"
        "while not pathlib.Path(sys.argv[1]).exists():\n"
        "    time.sleep(0.02)\n"
    )
    seen: dict[str, object] = {}

    async def execute(_worker: object) -> str:
        seen["offered"] = foreground_commands_offered()
        call = asyncio.create_task(
            RunCommandTool().execute(
                {"argv": [sys.executable, "-c", script, str(flag), str(started)]},
                _context(tmp_path, manager),
            )
        )
        for _ in range(500):
            if started.exists():
                break
            await asyncio.sleep(0.01)
        seen["registered"] = len(manager.foreground_commands())
        flag.touch()
        await call
        return "done"

    monkeypatch.setattr(pool_module.SubagentWorker, "_execute", execute)
    worker = pool_module.SubagentWorker(
        task=SubagentTask("t1", "short", "run a command"),
        spec=SubagentTypeSpec(name="short", allowed_tools=frozenset(), system_prompt=""),
        index=1,
        parent_runner=object(),
        parent_model="model",
        tool_surface=ToolSurface(0, tuple([])),
        timeout_s=30,
    )

    result = await worker.run()

    assert result.ok is True
    assert seen == {"offered": False, "registered": 0}
    assert foreground_commands_offered() is True  # the worker's setting did not leak out


async def test_without_a_manager_the_command_is_an_ordinary_foreground_run(
    tmp_path: Path,
) -> None:
    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "-c", "print('plain')"]},
        {"workspace_root": str(tmp_path)},
    )
    assert result["ok"] is True and result["stdout"] == "plain\n"
