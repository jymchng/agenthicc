"""Regression tests for coalesced Live-region redraws."""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pyte
import pytest
from rich.console import Console
from rich.live import Live
from rich.text import Text

from agenthicc.tui.workspace.workspace import Workspace

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_redraw_coalesces_multiple_signal_callbacks() -> None:
    workspace = Workspace(MagicMock(), MagicMock())
    workspace._live = MagicMock()
    workspace._build = MagicMock(return_value="frame")

    workspace._redraw()
    workspace._redraw()
    workspace._redraw()
    workspace._live.update.assert_not_called()

    await asyncio.sleep(0)

    workspace._live.update.assert_called_once_with("frame", refresh=True)


def test_redraw_is_suppressed_until_resize_repaint() -> None:
    workspace = Workspace(MagicMock(), MagicMock())
    workspace._live = MagicMock()
    workspace._build = MagicMock(return_value="frame")
    workspace._resize_pending = True

    workspace._redraw()
    workspace._flush_redraw()

    workspace._live.update.assert_not_called()

    workspace._resize_pending = False
    workspace._flush_redraw()
    workspace._live.update.assert_called_once_with("frame", refresh=True)


def test_resize_reset_clears_rich_live_shape() -> None:
    workspace = Workspace(MagicMock(), MagicMock())
    position_token = object()
    live_render = SimpleNamespace(
        _shape=(80, 24),
        position_cursor=MagicMock(return_value=position_token),
    )
    workspace._live = SimpleNamespace(_live_render=live_render)  # type: ignore[assignment]
    workspace._console = MagicMock()

    workspace._reset_live_after_resize()

    workspace._console.control.assert_called_once_with(position_token)
    live_render.position_cursor.assert_called_once_with()
    assert live_render._shape is None


def test_resize_reset_preserves_last_scroll_row() -> None:
    """Repainting Live must not erase the permanent row immediately above it."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        width=80,
        height=10,
        color_system=None,
    )
    live = Live(
        Text("LIVE0\nLIVE1\nLIVE2\nLIVE3"),
        console=console,
        auto_refresh=False,
        transient=True,
        vertical_overflow="crop",
    )
    screen = pyte.Screen(80, 10)
    stream = pyte.Stream(screen)

    def feed_output() -> None:
        data = output.getvalue()
        output.seek(0)
        output.truncate(0)
        # A real terminal's output processing turns LF into CRLF.  StringIO
        # bypasses that driver, so model it for the screen emulator.
        stream.feed(data.replace("\n", "\r\n"))

    live.start(refresh=True)
    try:
        feed_output()
        console.print("SCROLL-LAST")
        feed_output()

        # Model a height change before the workspace's SIGWINCH repaint path.
        console._height = 8  # type: ignore[attr-defined]
        workspace = Workspace(MagicMock(), console)
        workspace._live = live  # type: ignore[assignment]
        workspace._reset_live_after_resize()
        live.refresh()
        feed_output()

        assert "SCROLL-LAST" in "\n".join(screen.display)
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
