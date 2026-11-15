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
    stderr_sink,
    uninstall_stderr_sink,
)
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.notice import Notice
from tests.tui_frames import settle_frames

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "agenthicc"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    yield tmp_path
    uninstall_stderr_sink()


def _workspace(state: AppState) -> tuple[Workspace, StringIO]:
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=100, height=30)
    return Workspace(state, console), output


def _session(
    app_state: AppState, workspace: Workspace | None = None, session_id: str = "sess-1"
) -> TUISession:
    """The real ``_install_diagnostics_log`` on a session with only what it reads."""
    session = object.__new__(TUISession)
    session._ctx = SimpleNamespace(app_state=app_state, session_id=session_id)  # type: ignore[assignment]
    session._workspace = workspace or _workspace(app_state)[0]
    return session


def _a_tool_that_prints_to_stderr(text: str) -> str:
    """A tool body, run on a worker thread as tool execution is."""
    print(text, file=sys.stderr)
    return "ok"


async def test_stray_stderr_reaches_the_log_and_one_notice_not_the_screen() -> None:
    state = AppState.create()
    events: list[object] = []
    state.conversation.on_event(events.append)
    workspace, output = _workspace(state)
    original = sys.stderr
    # The session's order: the first frame, then the diagnostics log.
    workspace.start()
    _session(state, workspace)._install_diagnostics_log()
    try:
        await settle_frames(workspace)
        assert sys.stderr is stderr_sink()
        # An external editor hands the terminal over and back; the log stays.
        workspace.suspend_for_external_process()
        assert sys.stderr is original
        workspace.resume_after_external_process()
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
    workspace, output = _workspace(state)
    workspace.start()
    _session(state, workspace)._install_diagnostics_log()
    assert stderr_sink() is None
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
    # Ctrl+X Ctrl+E (TUX-04): the composer suspends the live region and hands
    # the terminal to $EDITOR.
    "tui/input/unified_session.py",
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


def test_logs_opens_the_transcript_view_on_the_diagnostics_log() -> None:
    """``/logs`` asks the session for the one transcript view (TUX-09); no pager of its own."""
    command = build_builtin_registry().get("/logs")
    assert command is not None and command.handler is not None
    console = Console(record=True, width=80, force_terminal=False, color_system=None)
    opened: list[str] = []
    command.handler(_logs_context(console, open_transcript=opened.append))
    assert opened == ["logs"]

    notices: list[tuple[str, str]] = []
    command.handler(_logs_context(console, transient_notify=lambda s, t: notices.append((s, t))))
    assert notices == [("warning", "The log view is available only in an interactive session.")]
