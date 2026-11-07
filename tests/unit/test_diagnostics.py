"""TUX-08 §2.3 / AC 6: stray stderr and warnings go to the session log, not the screen."""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import warnings
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, build_builtin_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.runners.tui_session import TUISession
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.diagnostics import (
    DIAGNOSTICS_NOTICE,
    DiagnosticsLog,
    diagnostics_log_path,
    log_tail,
    stderr_sink,
    uninstall_stderr_sink,
)
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.notice import Notice
from agenthicc.tui.workspace.overlays.log_tail import LogTailOverlay
from tests.tui_frames import settle_frames

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "agenthicc"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    yield tmp_path
    uninstall_stderr_sink()


def _session(app_state: AppState, session_id: str = "sess-1") -> TUISession:
    """The real ``_install_diagnostics_log`` on a session with only what it reads."""
    session = object.__new__(TUISession)
    session._ctx = SimpleNamespace(app_state=app_state, session_id=session_id)  # type: ignore[assignment]
    return session


def _workspace(state: AppState) -> tuple[Workspace, StringIO]:
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=100, height=30)
    return Workspace(state, console), output


def _a_tool_that_prints_to_stderr(text: str) -> str:
    """A tool body, run on a worker thread as tool execution is."""
    print(text, file=sys.stderr)
    return "ok"


async def test_stray_stderr_reaches_the_log_and_one_notice_not_the_screen() -> None:
    state = AppState.create()
    events: list[object] = []
    state.conversation.on_event(events.append)
    _session(state)._install_diagnostics_log()
    workspace, output = _workspace(state)
    original = sys.stderr
    workspace.start()
    try:
        await settle_frames(workspace)
        assert sys.stderr is stderr_sink()

        assert await asyncio.to_thread(_a_tool_that_prints_to_stderr, "boom") == "ok"
        await asyncio.sleep(0)  # the notice is set on the loop, from the tool's thread
        await asyncio.sleep(0)
        notice = state.conversation.transient_notice()
        assert notice == Notice("info", DIAGNOSTICS_NOTICE)

        state.conversation.transient_notice.set(None)
        _a_tool_that_prints_to_stderr("boom again")
        await asyncio.sleep(0)
        assert state.conversation.transient_notice() is None, "the notice is once per session"
        await settle_frames(workspace)
    finally:
        workspace.stop()
        uninstall_stderr_sink()
    assert sys.stderr is original
    assert "boom" not in output.getvalue()
    assert diagnostics_log_path("sess-1").read_text(encoding="utf-8") == "boom\nboom again\n"
    # Nothing reached the scrollback either: no conversation event was added.
    assert events == []


async def test_warnings_go_to_the_log() -> None:
    state = AppState.create()
    _session(state)._install_diagnostics_log()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("an old API", DeprecationWarning, stacklevel=1)
    uninstall_stderr_sink()
    logged = diagnostics_log_path("sess-1").read_text(encoding="utf-8")
    assert "DeprecationWarning: an old API" in logged
    await asyncio.sleep(0)
    assert state.conversation.transient_notice() == Notice("info", DIAGNOSTICS_NOTICE)


async def test_debug_keeps_stderr_on_the_screen() -> None:
    state = AppState.create()
    state.cli_flags = SimpleNamespace(debug=True)  # type: ignore[assignment]
    _session(state)._install_diagnostics_log()
    assert stderr_sink() is None
    workspace, output = _workspace(state)
    workspace.start()
    try:
        await settle_frames(workspace)
        print("developer output", file=sys.stderr)
    finally:
        workspace.stop()
    assert "developer output" in output.getvalue()
    assert not diagnostics_log_path("sess-1").exists()


def test_a_child_given_the_sink_writes_into_the_log(tmp_path: Path) -> None:
    sink = DiagnosticsLog(tmp_path / "logs" / "child.log")
    try:
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('from the child')"],
            stderr=sink,
            check=True,
        )
    finally:
        sink.close()
    assert (tmp_path / "logs" / "child.log").read_text(encoding="utf-8") == "from the child"


# Spawns that may use the real terminal: none runs while the live block is up.
_TERMINAL_SPAWNS = {
    # The TUI suspends the live region before handing the terminal over.
    "agents/external_editor.py",
    # CLI subcommands and developer scripts, never part of a TUI session.
    "background/coverage_gate.py",
    "cli/commands/keybindings.py",
    "cli/registry.py",
}
_SPAWN_CALLS = {
    "Popen",
    "call",
    "check_call",
    "check_output",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "run",
}


def test_no_subprocess_started_by_a_session_inherits_fd_2() -> None:
    """Every child captures or redirects stderr, so none can print over the TUI."""
    inheriting: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            module = node.func.value
            if not (isinstance(module, ast.Name) and module.id in {"subprocess", "asyncio"}):
                continue
            if node.func.attr not in _SPAWN_CALLS or (module.id, node.func.attr) == (
                "asyncio",
                "run",
            ):
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            if keywords & {"stderr", "capture_output"} or None in keywords:
                continue
            if relative not in _TERMINAL_SPAWNS:
                inheriting.append(f"{relative}:{node.lineno}")
    assert inheriting == []


def test_log_tail_keeps_the_last_whole_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    assert log_tail(path) == []
    path.write_text("".join(f"line {index}\n" for index in range(500)), encoding="utf-8")
    assert log_tail(path, max_lines=3) == ["line 497", "line 498", "line 499"]
    tail = log_tail(path, max_lines=1000, max_bytes=40)
    assert tail[-1] == "line 499"
    assert all(line.startswith("line ") for line in tail)


def test_the_log_path_is_per_session_and_safe() -> None:
    assert diagnostics_log_path("abc") == Path.home() / ".agenthicc" / "logs" / "abc.log"
    assert diagnostics_log_path("../../etc/passwd").name == "....etcpasswd.log"
    assert diagnostics_log_path("").name == "session.log"


def _logs_context(console: Console, **fields: object) -> CommandContext:
    return CommandContext(
        text="/logs",
        args="",
        model="test/model",
        console=console,
        config=AgenthiccConfig(),
        session_id="sess-1",
        **fields,  # type: ignore[arg-type]
    )


def test_logs_opens_the_tail_or_says_the_log_is_empty() -> None:
    command = build_builtin_registry().get("/logs")
    assert command is not None and command.handler is not None
    console = Console(record=True, width=80, force_terminal=False, color_system=None)
    notices: list[tuple[str, str]] = []
    shown: list[object] = []

    command.handler(_logs_context(console, transient_notify=lambda s, t: notices.append((s, t))))
    assert notices == [("info", "The diagnostics log is empty.")]

    path = diagnostics_log_path("sess-1")
    path.parent.mkdir(parents=True)
    path.write_text("first\nsecond\n", encoding="utf-8")
    command.handler(_logs_context(console, set_pending_menu=shown.append))
    assert len(shown) == 1 and isinstance(shown[0], LogTailOverlay)
    overlay_console = Console(record=True, width=80, force_terminal=False, color_system=None)
    overlay_console.print(shown[0].render())
    assert overlay_console.export_text().splitlines()[1:3] == [" first", " second"]

    command.handler(_logs_context(console))
    assert console.export_text().splitlines()[-2:] == ["first", "second"]
