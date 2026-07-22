"""Regression tests for coalesced Live-region redraws."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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
    restore_token = object()
    live_render = SimpleNamespace(
        _shape=(80, 24),
        restore_cursor=MagicMock(return_value=restore_token),
    )
    workspace._live = SimpleNamespace(_live_render=live_render)  # type: ignore[assignment]
    workspace._console = MagicMock()

    workspace._reset_live_after_resize()

    workspace._console.control.assert_called_once_with(restore_token)
    live_render.restore_cursor.assert_called_once_with()
    assert live_render._shape is None


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
