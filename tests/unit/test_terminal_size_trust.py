"""A real terminal's size beats a stale ``COLUMNS`` / ``LINES`` in the environment.

Rich takes ``$COLUMNS`` over the terminal's own answer, so a shell that exported
it once keeps the TUI wrapping for the old width after a resize or in a narrower
pane: lines are cut and the live block repaints over itself.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time

import pytest

from agenthicc.runners.tui_session import _trust_the_terminal_size

pytestmark = pytest.mark.unit


def test_a_terminal_drops_both_stale_variables() -> None:
    environ = {"COLUMNS": "65", "LINES": "30", "TERM": "xterm"}
    _trust_the_terminal_size(environ, is_terminal=True)
    assert environ == {"TERM": "xterm"}


def test_without_a_terminal_the_variables_stay_in_force() -> None:
    environ = {"COLUMNS": "100", "LINES": "40"}
    _trust_the_terminal_size(environ, is_terminal=False)
    assert environ == {"COLUMNS": "100", "LINES": "40"}


def test_nothing_to_drop_is_fine() -> None:
    environ: dict[str, str] = {}
    _trust_the_terminal_size(environ, is_terminal=True)
    assert environ == {}


_PROBE = (
    "import os, sys\n"
    "from agenthicc.runners.tui_session import _trust_the_terminal_size\n"
    "from rich.console import Console\n"
    "_trust_the_terminal_size(os.environ, is_terminal=sys.stdout.isatty())\n"
    "print('WIDTH', Console(force_terminal=True).width)\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
@pytest.mark.parametrize("stale", ["65", "200"])
def test_a_console_in_a_58_column_terminal_is_58_wide_whatever_columns_says(stale: str) -> None:
    environment = dict(os.environ, COLUMNS=stale, TERM="xterm-256color")
    environment.pop("LINES", None)
    pid, descriptor = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by the probe interpreter
        os.execve(sys.executable, [sys.executable, "-c", _PROBE], environment)
    fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 58, 0, 0))
    output = b""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready, _, _ = select.select([descriptor], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(descriptor, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        finished, _ = os.waitpid(pid, os.WNOHANG)
        if finished:
            break
    os.close(descriptor)
    assert b"WIDTH 58" in output, output


# ── /status says what the terminal is ────────────────────────────────────────


def _fake_size(monkeypatch: pytest.MonkeyPatch, columns: int, lines: int) -> None:
    monkeypatch.setattr(os, "get_terminal_size", lambda *_a: os.terminal_size((columns, lines)))


def test_status_names_the_terminals_size_term_and_multiplexer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.tui.statusline import terminal_description

    _fake_size(monkeypatch, 58, 30)
    env = {"TERM": "tmux-256color", "TMUX": "/tmp/tmux-1000/default,1,0"}
    assert terminal_description(env) == "58 columns × 30 rows · TERM tmux-256color · inside tmux"
    assert terminal_description({"STY": "1.pts"}) == "58 columns × 30 rows · inside screen"


def test_status_says_when_columns_overrides_the_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.tui.statusline import terminal_description

    _fake_size(monkeypatch, 58, 30)
    assert "$COLUMNS=65 overrides it" in terminal_description({"COLUMNS": "65"})
    assert "overrides" not in terminal_description({"COLUMNS": "58"})  # agreeing is not news
    assert "overrides" not in terminal_description({"COLUMNS": "wide"})  # not a number


def test_status_without_a_terminal_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.tui.statusline import terminal_description

    def refuse(*_a: object) -> os.terminal_size:
        raise OSError("not a tty")

    monkeypatch.setattr(os, "get_terminal_size", refuse)
    assert terminal_description({}) == "not a terminal"


def test_the_status_report_has_a_terminal_row() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.statusline import status_report

    labels = [label for label, _value in status_report(AppState.create())]
    assert "Terminal" in labels
