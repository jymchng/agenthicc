from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.agents.external_editor import open_definition_in_editor
from agenthicc.agents.library import agent_directory
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import _EXIT, UnifiedInputSession
from agenthicc.tui.runtime.commands import CommandBus

pytestmark = pytest.mark.unit


def test_external_editor_uses_argv_and_rejects_paths_outside_definition_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    directory = agent_directory("project", project_root=project)
    directory.mkdir(parents=True)
    path = directory / "reviewer.md"
    path.write_text("---\nname: reviewer\ndescription: Reviews code\n---\n", encoding="utf-8")
    monkeypatch.setenv("EDITOR", "editor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    invoked: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> SimpleNamespace:
        invoked.append(command)
        assert check is False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)

    open_definition_in_editor(path, scope="project", project_root=project)

    assert invoked == [["editor", "--wait", str(path)]]
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError, match="scope"):
        open_definition_in_editor(outside, scope="project", project_root=project)


@pytest.mark.asyncio
async def test_terminal_handoff_runs_outside_raw_mode_and_resumes_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState.create()
    session = UnifiedInputSession(state, CommandBus())
    loop = asyncio.get_running_loop()
    wake = threading.Event()
    active_raw_modes = 0
    reads = 0
    invoked: list[bool] = []

    class Backend:
        def is_interactive(self) -> bool:
            return True

        def enter_raw_mode(self) -> object:
            class RawMode:
                def __enter__(self) -> None:
                    nonlocal active_raw_modes
                    active_raw_modes += 1

                def __exit__(self, *args: object) -> None:
                    nonlocal active_raw_modes
                    active_raw_modes -= 1

            return RawMode()

        def read_key(self) -> tuple[Key, str]:
            nonlocal reads
            reads += 1
            if reads == 1:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(session.run_terminal_handoff(edit))
                )
                if not wake.wait(timeout=2):
                    raise TimeoutError("terminal handoff did not wake the input reader")
                raise OSError("terminal input stopped")
            return Key.UNKNOWN, ""

        def restore(self) -> None:
            wake.set()

        def close(self) -> None:
            wake.clear()

        def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
            return None

    async def edit() -> None:
        invoked.append(active_raw_modes == 0)

    dispatches = 0

    async def dispatch(_key: Key, _char: str) -> object:
        nonlocal dispatches
        dispatches += 1
        return _EXIT if dispatches == 1 else None

    backend = Backend()
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: backend)
    monkeypatch.setattr(session, "_dispatch", dispatch)

    await session.run()

    assert invoked == [True]
    assert active_raw_modes == 0
    assert reads == 2
