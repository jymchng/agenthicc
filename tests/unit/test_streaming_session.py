"""Unit tests for agenthicc.tui.input.streaming.StreamingSession (PRD-57 §10.2).

The session is driven by patching ``select.select`` and ``os.read`` to simulate
keystrokes without a real TTY, and by injecting a fake ``raw_mode`` context manager
to avoid termios calls.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenthicc.tui.input.streaming import StreamingSession

pytestmark = pytest.mark.unit


def _make_console() -> MagicMock:
    console = MagicMock()
    console.print = MagicMock()
    return console


def _make_state() -> MagicMock:
    state = MagicMock()
    state.clear = MagicMock()
    state.update = MagicMock()
    return state


@contextmanager
def _fake_raw(fd):
    yield fd


async def _run_with_bytes(byte_sequences: list[bytes], n_ticks: int = 0) -> tuple[list[str], MagicMock]:
    """Run a streaming session, feeding *byte_sequences* as input bytes.

    Returns ``(pending_queue, console_mock)``.
    """
    pending_queue: list[str] = []
    console = _make_console()
    state = _make_state()

    session = StreamingSession(state, pending_queue, console)

    # Build an iterator: each call to select returns data-ready, then the
    # corresponding byte is returned by os.read.
    byte_iter = iter(byte_sequences)
    call_count = 0
    max_calls = len(byte_sequences) + n_ticks

    def fake_select(rlist, wlist, xlist, timeout):
        nonlocal call_count
        call_count += 1
        if call_count > max_calls:
            raise asyncio.CancelledError()
        try:
            return (rlist, [], [])
        except StopIteration:
            return ([], [], [])

    def fake_os_read(fd, n):
        try:
            return next(byte_iter)
        except StopIteration:
            raise asyncio.CancelledError()

    with (
        patch("agenthicc.tui.input.streaming.raw_mode", _fake_raw),
        patch("agenthicc.tui.input.streaming.select.select", side_effect=fake_select),
        patch("agenthicc.tui.input.streaming.os.read", side_effect=fake_os_read),  # type: ignore[attr-defined]
        patch("sys.stdin.fileno", return_value=42),
    ):
        try:
            session.start()
            await asyncio.sleep(0.1)
            session.stop()
        except Exception:
            pass

    return pending_queue, console


# ── basic key handling ────────────────────────────────────────────────────────

class TestStreamingSession:
    @pytest.mark.asyncio
    async def test_start_and_stop_no_error(self) -> None:
        session = StreamingSession(_make_state(), [], _make_console())
        with patch("agenthicc.tui.input.streaming.raw_mode", _fake_raw), \
             patch("sys.stdin.fileno", return_value=42):
            session.start()
            await asyncio.sleep(0.05)
            session.stop()
        # no exception → test passes

    @pytest.mark.asyncio
    async def test_stop_without_start_no_error(self) -> None:
        session = StreamingSession(_make_state(), [], _make_console())
        session.stop()  # should be a no-op

    @pytest.mark.asyncio
    async def test_start_clears_state(self) -> None:
        state = _make_state()
        session = StreamingSession(state, [], _make_console())
        with patch("agenthicc.tui.input.streaming.raw_mode", _fake_raw), \
             patch("sys.stdin.fileno", return_value=42):
            session.start()
            await asyncio.sleep(0.05)
            session.stop()
        state.clear.assert_called()

    @pytest.mark.asyncio
    async def test_uses_input_buffer(self) -> None:
        """Session uses InputBuffer internally (not a bare list)."""
        session = StreamingSession(_make_state(), [], _make_console())
        from agenthicc.tui.input.buffer import InputBuffer
        assert isinstance(session._buf, InputBuffer)
