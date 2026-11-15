"""TUX-09 §2.1 ``y`` and ``e``: OSC 52, the system clipboard fallback and the pager."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agenthicc.tui.transcript.clipboard import (
    MAX_COPY_BYTES,
    copy_text,
    osc52_sequence,
    pager_command,
    run_pager,
)

pytestmark = pytest.mark.unit


def test_the_sequence_is_osc52_with_the_base64_of_the_text() -> None:
    sequence = osc52_sequence("héllo\nworld")
    assert sequence.startswith("\x1b]52;c;") and sequence.endswith("\x07")
    payload = sequence[len("\x1b]52;c;") : -1]
    assert base64.b64decode(payload).decode("utf-8") == "héllo\nworld"
    assert "\x1b[" not in sequence  # no SGR


def test_a_capable_terminal_gets_osc52_and_nothing_else() -> None:
    written: list[str] = []
    ran: list[list[str]] = []
    result = copy_text(
        "copy me",
        write=written.append,
        env={"TERM": "xterm-256color"},
        which=lambda name: f"/bin/{name}",
        run=lambda command, data: ran.append(command) or True,
    )
    assert result.method == "osc52" and result.cut is False
    assert written == [osc52_sequence("copy me")] and ran == []


def test_a_terminal_that_cannot_take_osc52_uses_the_first_clipboard_command_found() -> None:
    written: list[str] = []
    ran: list[tuple[list[str], bytes]] = []
    result = copy_text(
        "copy me",
        write=written.append,
        env={"TERM": "linux"},
        which=lambda name: "/bin/wl-copy" if name == "wl-copy" else None,
        run=lambda command, data: ran.append((command, data)) or True,
    )
    assert result.method == "system"
    assert ran == [(["wl-copy"], b"copy me")] and written == []


def test_when_no_clipboard_command_works_osc52_is_still_tried() -> None:
    written: list[str] = []
    result = copy_text(
        "x",
        write=written.append,
        env={"TERM": "dumb"},
        which=lambda name: "/bin/pbcopy" if name == "pbcopy" else None,
        run=lambda command, data: False,
    )
    assert result.method == "osc52" and written == [osc52_sequence("x")]


def test_a_text_past_the_osc52_limit_is_cut_and_says_so() -> None:
    written: list[str] = []
    text = "é" * MAX_COPY_BYTES  # two bytes each
    result = copy_text(text, write=written.append, env={"TERM": "xterm"})
    assert result.cut is True
    payload = base64.b64decode(written[0][len("\x1b]52;c;") : -1])
    assert len(payload) <= MAX_COPY_BYTES


def test_pager_takes_pager_then_editor_read_only_then_less(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "item.txt"
    assert pager_command({"PAGER": "less -R"}, path) == ["less", "-R", str(path)]
    assert pager_command({"PAGER": "bat --paging=always", "EDITOR": "vim"}, path)[:1] == ["bat"]
    # An editor opens read-only: -R for the vi family, the file itself is mode 0400.
    assert pager_command({"EDITOR": "vim"}, path) == ["vim", "-R", str(path)]
    assert pager_command({"VISUAL": "nvim -u NONE", "EDITOR": "nano"}, path) == [
        "nvim",
        "-u",
        "NONE",
        "-R",
        str(path),
    ]
    assert pager_command({"EDITOR": "nano"}, path) == ["nano", str(path)]
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/less" if name == "less" else None)
    assert pager_command({}, path) == ["less", str(path)]
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert pager_command({}, path) is None


def test_the_pager_gets_a_private_read_only_copy_that_is_removed_afterwards(
    tmp_path: Path,
) -> None:
    seen = tmp_path / "seen.txt"
    script = tmp_path / "pager.sh"
    script.write_text(f'#!/bin/sh\ncp "$1" {seen}\nstat -c %a "$1" > {seen}.mode\n')
    script.chmod(0o755)
    assert run_pager("the full text", {"PAGER": str(script)}) == ""
    assert seen.read_text() == "the full text\n"
    assert (tmp_path / "seen.txt.mode").read_text().strip() == "400"


def test_a_missing_pager_is_reported_not_raised() -> None:
    assert run_pager("x", {"PAGER": "/nonexistent/pager"}).startswith("could not start")
