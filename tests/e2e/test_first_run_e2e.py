"""TUX-03: the first run, end to end.

Two kinds of test:

- an in-process session with a scripted provider transport, which proves that
  a prompt sent while the credential check has failed makes **no provider
  request** (acceptance criterion 3);
- the real ``agenthicc`` CLI in a pseudo-terminal, read through ``pyte``, which
  proves what the user sees: the header, the missing-key notice, the
  ``/release-notes`` pager, and the one-time "Work in <directory>?" dialog
  (criteria 1, 3, 5, 6).
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from collections.abc import Callable
from importlib.metadata import version as installed_version
from pathlib import Path

import pyte
import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport._mock import MockTransport
from rich.console import Console

from agenthicc.commands.builtins import build_builtin_registry
from agenthicc.tui.runtime.commands import SendMessageCommand
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout
from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.e2e

_MISSING_KEY = (
    "No API key for anthropic. Set ANTHROPIC_API_KEY, or run /model to pick another provider."
)


# ── in-process: no provider request without a key ─────────────────────────────


async def test_a_prompt_with_no_key_makes_no_provider_request_and_opens_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    sessions.mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", sessions)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSION_INDEX", sessions / "index.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.chdir(project)

    session, ctx, _workspace, _input = _make_session()
    ctx.console = Console(file=io.StringIO(), width=100, record=True, force_terminal=False)
    ctx.model_label = "anthropic/claude-opus-4-8"
    model = build_builtin_registry().get("/model")
    assert model is not None
    ctx.cmd_registry.register(model)
    transport = MockTransport()
    ctx.agent_runner = AgentRunnerBase(transport=transport, signals=SignalBus())
    dispatched: list[str] = []
    real_dispatch = session.dispatch_slash

    def spy(text: str) -> bool:
        dispatched.append(text)
        return real_dispatch(text)

    session.dispatch_slash = spy  # type: ignore[method-assign]
    session._input_session.set_text = lambda text: None  # type: ignore[attr-defined]

    await session._show_welcome()
    await session.handle_send(SendMessageCommand(text="hello"))

    assert transport.calls == []  # no provider request
    assert session._agent_task is None
    assert dispatched == ["/model"]
    printed = ctx.console.export_text()
    assert printed.count(_MISSING_KEY) == 2  # under the header, then in answer to "hello"
    assert printed.index("agenthicc") < printed.index(_MISSING_KEY)


# ── the real CLI in a pseudo-terminal ─────────────────────────────────────────

_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")

_COLUMNS, _ROWS = 120, 40


class _Terminal:
    """The CLI running in a pseudo-terminal, with its screen kept by pyte."""

    def __init__(self, project: Path, home: Path, extra_env: dict[str, str]) -> None:
        import fcntl
        import pty
        import struct
        import termios

        tools = home.parent / "tools"
        tools.mkdir(exist_ok=True)
        (tools / "rg").write_text("#!/bin/sh\n", encoding="utf-8")
        (tools / "rg").chmod(0o755)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"}
            and not key.startswith("AGENTHICC_")
        }
        env.update(
            HOME=str(home),
            PATH=str(tools),
            PYTHONPATH=str(Path(__file__).parents[2] / "src"),
            TERM="xterm-256color",
        )
        env.pop("COVERAGE_PROCESS_START", None)
        env.pop("COVERAGE_PROCESS_CONFIG", None)
        env.update(extra_env)
        self.screen = pyte.Screen(_COLUMNS, _ROWS)
        self._stream = pyte.ByteStream(self.screen)
        self.pid, self._fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - replaced by the CLI
            os.chdir(project)
            os.execve(sys.executable, [sys.executable, "-m", "agenthicc"], env)
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", _ROWS, _COLUMNS, 0, 0))

    def lines(self) -> list[str]:
        return [row.rstrip() for row in self.screen.display]

    def text(self) -> str:
        return "\n".join(self.lines())

    def pump(self, seconds: float) -> None:
        import select

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self._fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self._fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                self._stream.feed(data)

    def wait_for(self, condition: Callable[[str], bool], what: str) -> None:
        deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S / 2
        while time.monotonic() < deadline:
            self.pump(0.1)
            if condition(self.text()):
                return
        pytest.fail(f"never saw {what}; the screen was:\n{self.text()}")

    def wait_for_input(self) -> None:
        """Wait until the composer is drawn and the TUI reads keys (typing earlier is lost)."""
        self.wait_for(
            lambda text: "❯" in text and "time_to_interactive_input" not in text,
            "a composer that accepts input",
        )
        self.pump(0.3)

    def send(self, data: str) -> None:
        os.write(self._fd, data.encode())

    def close(self) -> None:
        try:
            os.kill(self.pid, 9)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass


def _repository(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    return project


def _header_rows(screen_text: str) -> list[str]:
    """The screen's rows above the first rule: the header and its notices."""
    rows: list[str] = []
    for row in screen_text.splitlines():
        if row.startswith("─"):
            break
        rows.append(row)
    return [row for row in rows if row.strip()]


@_posix_only
@cli_test_timeout(2)
def test_a_first_run_without_a_key_shows_the_header_the_notice_and_holds_the_prompt(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = _repository(tmp_path)
    terminal = _Terminal(project, home, {})
    try:
        terminal.wait_for(lambda text: _MISSING_KEY in text, "the missing-key notice")
        terminal.wait_for_input()
        rows = _header_rows(terminal.text())

        # Criterion 3: the header is followed by the notice.
        assert f"agenthicc {installed_version('agenthicc')}" in rows[0]
        assert rows[-1].strip() == f"! {_MISSING_KEY}"
        # The first run on a machine shows the mascot once (four rows).
        assert "state-driven agent operating system" in rows[1]
        assert len(rows) == 5  # four header rows and one notice row
        assert "~" not in rows[0] and str(project) in rows[3]

        # Submitting a prompt shows the same notice again and opens /model.
        terminal.send("hello\r")
        terminal.wait_for(lambda text: text.count(_MISSING_KEY) == 2, "the notice a second time")
        terminal.wait_for(
            lambda text: "Model" in text and "no restart" in text, "the /model picker"
        )
        picker = terminal.text()
        # TUX-05's picker says which providers cannot be chosen and why.
        assert "set ANTHROPIC_API_KEY" in picker and "set OPENAI_API_KEY" in picker
        assert "llama3.2" in picker  # Ollama needs no key

        # Closing the picker returns to the composer with what was typed.
        terminal.send("\x1b")
        terminal.wait_for(lambda text: "❯ hello" in text, "the prompt kept after the picker")
    finally:
        terminal.close()

    state = json.loads((home / ".agenthicc" / "state.json").read_text(encoding="utf-8"))
    assert state["version"] == installed_version("agenthicc")


@_posix_only
@cli_test_timeout(2)
def test_a_later_start_is_three_rows_and_whats_new_appears_only_after_an_upgrade(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".agenthicc").mkdir(parents=True)
    state_file = home / ".agenthicc" / "state.json"
    state_file.write_text(
        json.dumps({"version": "0.0.1", "release_notes_seen": "0.0.1", "tip_index": 4}),
        encoding="utf-8",
    )
    project = _repository(tmp_path)
    key = {"ANTHROPIC_API_KEY": "sk-test-not-sent"}

    upgraded = _Terminal(project, home, key)
    try:
        upgraded.wait_for(lambda text: "what's new: /release-notes" in text, "the what's new hint")
        upgraded.wait_for_input()
        rows = _header_rows(upgraded.text())
        assert len(rows) == 3  # criterion 1: three rows, no notice
        assert rows[0].rstrip().endswith("what's new: /release-notes")
        assert "state-driven" not in upgraded.text()  # the mascot was the first run's only

        # Criterion 5: /release-notes pages the packaged changelog.
        upgraded.send("/release-notes\r")
        upgraded.wait_for(lambda text: "Release notes" in text, "the release notes pager")
        assert "Unreleased" in upgraded.text()
        upgraded.send("\x1b")
        upgraded.wait_for(lambda text: "Release notes" not in text, "the pager closing")
    finally:
        upgraded.close()

    again = _Terminal(project, home, key)
    try:
        again.wait_for(lambda text: "agenthicc " in text, "the header")
        again.wait_for_input()
        assert "what's new" not in again.text()
        assert len(_header_rows(again.text())) == 3
    finally:
        again.close()


@_posix_only
@cli_test_timeout(2)
def test_banner_off_prints_nothing_before_the_composer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = _repository(tmp_path)
    terminal = _Terminal(
        project,
        home,
        {"ANTHROPIC_API_KEY": "sk-test-not-sent", "AGENTHICC_UI_BANNER": "off"},
    )
    try:
        terminal.wait_for_input()
        terminal.pump(1.0)
        screen = terminal.text()
        assert "agenthicc 0." not in screen
        assert "!" not in screen.split("❯")[0]
        assert _header_rows(screen) == []
    finally:
        terminal.close()


@_posix_only
@cli_test_timeout(2)
def test_an_unknown_directory_asks_once_and_remembers_the_answer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    key = {"ANTHROPIC_API_KEY": "sk-test-not-sent"}

    first = _Terminal(downloads, home, key)
    try:
        first.wait_for(lambda text: "Work in ~/Downloads?" in text, "the directory dialog")
        first.pump(1.0)  # the key loop starts after the dialog is drawn
        screen = first.text()
        assert "agenthicc can read and change files here." in screen
        for option in ("Yes", "Yes, in Plan mode", "Choose another directory…"):
            assert option in screen
        first.send("1")
        first.wait_for(lambda text: "Work in" not in text, "the dialog closing")
    finally:
        first.close()

    trust = json.loads((home / ".agenthicc" / "trust.json").read_text(encoding="utf-8"))
    assert [(entry["path"], entry["decision"]) for entry in trust["workspaces"]] == [
        (str(downloads.resolve()), "allow")
    ]

    second = _Terminal(downloads, home, key)
    try:
        second.wait_for(lambda text: "agenthicc " in text, "the header")
        second.wait_for_input()
        second.pump(1.0)
        assert "Work in" not in second.text()
    finally:
        second.close()
