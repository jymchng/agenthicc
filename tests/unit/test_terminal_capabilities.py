"""The terminal is asked once about synchronized output; its answer is not a key (AGX-11)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from io import StringIO

import pytest

from agenthicc.tui.cbreak_reader import Key, raw_mode, read_key
from agenthicc.tui.terminal.capabilities import capabilities

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_capabilities() -> Iterator[None]:
    capabilities.reset()
    yield
    capabilities.reset()


def _read_after(data: bytes) -> tuple[Key, str]:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, data)
        return read_key(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    ("value", "supported"), [(1, True), (2, True), (3, True), (0, False), (4, False)]
)
def test_a_mode_report_is_recorded_instead_of_returned(value: int, supported: bool) -> None:
    assert _read_after(f"\x1b[?2026;{value}$y".encode() + b"a") == (Key.CHAR, "a")
    assert capabilities.synchronized_output is supported


def test_a_malformed_report_is_ignored() -> None:
    # A report without a value is consumed and ignored.
    assert _read_after(b"\x1b[?2026$yb") == (Key.CHAR, "b")
    assert capabilities.synchronized_output is False


def test_ordinary_escape_sequences_still_decode() -> None:
    assert _read_after(b"\x1b[A") == (Key.UP, "")
    assert _read_after(b"\x1b[Z") == (Key.SHIFT_TAB, "")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX raw mode")
def test_raw_mode_asks_the_terminal_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    master, slave = os.openpty()
    written = StringIO()
    monkeypatch.setattr(sys, "stdout", written)
    try:
        with raw_mode(slave):
            pass
        with raw_mode(slave):
            pass
    finally:
        os.close(master)
        os.close(slave)
    assert written.getvalue().count("\x1b[?2026$p") == 1
