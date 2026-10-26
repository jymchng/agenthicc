"""Regression tests for coalesced live-block redraws (PRD-164, AGX-11)."""

from __future__ import annotations

import asyncio
from io import StringIO
from unittest.mock import MagicMock

import pyte
import pytest
from rich.console import Console
from rich.segment import Segment

from agenthicc.tui.workspace.frames import FrameStats
from agenthicc.tui.workspace.live_region import LiveRegion
from agenthicc.tui.workspace.workspace import Workspace

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_redraw_coalesces_multiple_signal_callbacks() -> None:
    workspace = Workspace(MagicMock(), MagicMock())
    workspace._live = MagicMock()
    workspace._live.stats = FrameStats()
    workspace._build_lines = MagicMock(return_value=["frame"])

    workspace._redraw()
    workspace._redraw()
    workspace._redraw()
    workspace._live.update.assert_not_called()

    await asyncio.sleep(0)

    workspace._live.update.assert_called_once_with(["frame"])


@pytest.mark.asyncio
async def test_frames_are_capped_to_the_interval_but_typing_echoes_at_once() -> None:
    workspace = Workspace(MagicMock(), MagicMock(), frame_interval_s=0.2)
    workspace._live = MagicMock()
    workspace._live.stats = FrameStats()
    workspace._build_lines = MagicMock(return_value=["frame"])
    workspace._redraw()
    await asyncio.sleep(0)
    assert workspace._live.update.call_count == 1

    workspace._redraw()
    await asyncio.sleep(0.02)
    assert workspace._live.update.call_count == 1  # still inside the interval
    workspace._redraw_input()
    await asyncio.sleep(0)
    assert workspace._live.update.call_count == 2  # key echo is not delayed
    await asyncio.sleep(0.25)
    assert workspace._live.update.call_count == 2  # the timed frame merged into it


def test_frame_redraw_is_suppressed_for_static_idle_state() -> None:
    state = MagicMock()
    state.conversation.is_running.return_value = False
    state.conversation.compaction_active.return_value = False
    workspace = Workspace(state, MagicMock())
    workspace._redraw = MagicMock()

    workspace._redraw_frame()

    workspace._redraw.assert_not_called()


def test_frame_redraw_is_forwarded_for_active_animation() -> None:
    state = MagicMock()
    state.pending_approval.return_value = None
    state.conversation.is_running.return_value = True
    state.conversation.compaction_active.return_value = False
    workspace = Workspace(state, MagicMock())
    workspace._redraw = MagicMock()

    workspace._redraw_frame()

    workspace._redraw.assert_called_once_with()


def test_frame_redraw_is_forwarded_during_compaction() -> None:
    state = MagicMock()
    state.pending_approval.return_value = None
    state.conversation.is_running.return_value = False
    state.conversation.compaction_active.return_value = True
    workspace = Workspace(state, MagicMock())
    workspace._redraw = MagicMock()

    workspace._redraw_frame()

    workspace._redraw.assert_called_once_with()


def test_frame_redraw_is_suppressed_while_prompt_is_pending() -> None:
    state = MagicMock()
    state.pending_approval.return_value = object()
    state.conversation.is_running.return_value = True
    state.conversation.compaction_active.return_value = False
    workspace = Workspace(state, MagicMock())
    workspace._redraw = MagicMock()

    workspace._redraw_frame()

    workspace._redraw.assert_not_called()


def test_activity_redraw_is_suppressed_while_prompt_is_pending() -> None:
    state = MagicMock()
    state.pending_approval.return_value = object()
    workspace = Workspace(state, MagicMock())
    workspace._redraw = MagicMock()

    workspace._redraw_activity()

    workspace._redraw.assert_not_called()


def test_activity_redraw_is_forwarded_when_prompt_is_not_pending() -> None:
    state = MagicMock()
    state.pending_approval.return_value = None
    workspace = Workspace(state, MagicMock())
    workspace._redraw = MagicMock()

    workspace._redraw_activity()

    workspace._redraw.assert_called_once_with()


def test_redraw_is_suppressed_until_resize_repaint() -> None:
    workspace = Workspace(MagicMock(), MagicMock())
    workspace._live = MagicMock()
    workspace._live.stats = FrameStats()
    workspace._build_lines = MagicMock(return_value=["frame"])
    workspace._resize_pending = True

    workspace._redraw()
    workspace._flush_redraw()

    workspace._live.update.assert_not_called()

    workspace._resize_pending = False
    workspace._flush_redraw()
    workspace._live.update.assert_called_once_with(["frame"])


def test_resize_reset_repaints_the_whole_block_next_frame() -> None:
    workspace = Workspace(MagicMock(), MagicMock())
    workspace._live = MagicMock()

    workspace._reset_live_after_resize()

    workspace._live.invalidate.assert_called_once_with()


def test_resize_reset_preserves_last_scroll_row() -> None:
    """Repainting the block must not erase the permanent row immediately above it."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        width=80,
        height=10,
        color_system=None,
    )
    live = LiveRegion(console, transient=True)
    lines = [(Segment(f"LIVE{index}"),) for index in range(4)]
    screen = pyte.Screen(80, 10)
    stream = pyte.Stream(screen)

    def feed_output() -> None:
        data = output.getvalue()
        output.seek(0)
        output.truncate(0)
        # A real terminal's output processing turns LF into CRLF.  StringIO
        # bypasses that driver, so model it for the screen emulator.
        stream.feed(data.replace("\n", "\r\n"))

    live.start()
    try:
        live.update(lines)
        feed_output()
        console.print("SCROLL-LAST")
        feed_output()

        # Model a height change before the workspace's SIGWINCH repaint path.
        console._height = 8  # type: ignore[attr-defined]
        workspace = Workspace(MagicMock(), console)
        workspace._live = live
        workspace._reset_live_after_resize()
        live.update(lines)
        feed_output()

        display = "\n".join(screen.display)
        assert "SCROLL-LAST" in display
        assert display.count("LIVE3") == 1
    finally:
        live.stop()


@pytest.mark.asyncio
async def test_sigwinch_debounces_resize_repaints() -> None:
    workspace = Workspace(MagicMock(), MagicMock())
    workspace._live = MagicMock()
    workspace._reset_live_after_resize = MagicMock()
    workspace._flush_redraw = MagicMock()

    workspace._on_sigwinch(0, None)
    workspace._on_sigwinch(0, None)
    await asyncio.sleep(0)
    await asyncio.sleep(0.06)

    workspace._reset_live_after_resize.assert_called_once_with()
    workspace._flush_redraw.assert_called_once_with()
    assert workspace._resize_pending is False
