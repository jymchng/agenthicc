"""Runtime branches for the Rich workspace lifecycle and resize handling."""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agenthicc.tui.workspace.frames import FrameStats
from agenthicc.tui.workspace.live_region import LiveRegion, lines_text
from agenthicc.tui.workspace.workspace import Workspace

pytestmark = pytest.mark.unit


def _state() -> MagicMock:
    state = MagicMock()
    conv = state.conversation
    conv.live_tool_overflow.return_value = 2
    conv.frame.return_value = 0
    conv.is_running.return_value = False
    conv.agent_state.return_value = SimpleNamespace(name="IDLE")
    conv.model_name.return_value = "model"
    conv.elapsed_s = 0.0
    conv.tokens_in.return_value = 0
    conv.tokens_out.return_value = 0
    conv.session_id.return_value = "session"
    conv.turn_count.return_value = 0
    conv.cost_usd.return_value = 0.0
    conv.notification.return_value = None
    conv.workflow_override.return_value = None
    conv.compaction_active.return_value = False
    conv.subagent_pool_state.return_value = None
    state.overlay.return_value = None
    state.pending_approval.return_value = None
    state.workflow_run.return_value = None
    state.active_mode.return_value = SimpleNamespace(
        badge="A", name="Auto", color="green", blocked_capabilities=frozenset()
    )
    inp = state.input
    inp.paste_condensed.return_value = False
    inp.paste_label.return_value = ""
    inp.buf.return_value = []
    inp.cursor.return_value = 0
    return state


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=True, width=80, height=30, color_system=None)


def test_workspace_start_stop_wires_signals_and_overlay() -> None:
    state = _state()
    workspace = Workspace(state, _console())
    workspace.scroll.mount = MagicMock()
    workspace.scroll.unmount = MagicMock()
    workspace.overlays._overlay = MagicMock()
    workspace.overlays.render = MagicMock(return_value="overlay")

    workspace.start()

    assert isinstance(workspace._live, LiveRegion)
    assert "overlay" in lines_text(workspace._live.lines)
    assert workspace.overlays.render.called
    assert len(workspace._unsubs) > 5
    workspace.stop()
    assert workspace._live is None
    workspace.scroll.mount.assert_called_once_with()
    workspace.scroll.unmount.assert_called_once_with()


def test_build_handles_overflow_failure_and_plain_composer() -> None:
    state = _state()
    state.conversation.live_tool_overflow.side_effect = RuntimeError("stale signal")
    workspace = Workspace(state, _console())
    workspace.overlays._overlay = None
    rows = lines_text(workspace._build_lines())
    assert len(rows) >= 5
    assert not any("more tool" in row for row in rows)


def test_redraw_direct_and_error_paths(capsys: pytest.CaptureFixture[str]) -> None:
    workspace = Workspace(_state(), _console())
    workspace._live = MagicMock()
    workspace._live.stats = FrameStats()
    workspace._build_lines = MagicMock(return_value=["frame"])
    # Without a running event loop a frame is drawn at once.
    workspace._redraw()
    workspace._live.update.assert_called_once_with(["frame"])

    workspace._live.update.side_effect = OSError("not a tty")
    workspace._flush_redraw()
    workspace._build_lines.side_effect = RuntimeError("render failed")
    workspace._flush_redraw()
    assert "render failed" in capsys.readouterr().err


def test_resize_and_sigwinch_non_running_or_broken_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = Workspace(_state(), MagicMock())
    workspace._live = MagicMock()
    workspace._reset_live_after_resize = MagicMock()
    workspace._flush_redraw = MagicMock()
    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: (_ for _ in ()).throw(RuntimeError("no loop")),
    )
    workspace._schedule_resize_redraw()
    workspace._reset_live_after_resize.assert_called_once_with()
    workspace._flush_redraw.assert_called_once_with()

    workspace._resize_handle = MagicMock()
    workspace._flush_resize_redraw()
    assert workspace._resize_handle is None

    monkeypatch.setattr(
        asyncio,
        "get_event_loop",
        lambda: SimpleNamespace(is_running=lambda: False),
    )
    workspace._reset_live_after_resize.reset_mock()
    workspace._flush_redraw.reset_mock()
    workspace._on_sigwinch(28, None)
    workspace._reset_live_after_resize.assert_called_once_with()
    workspace._flush_redraw.assert_called_once_with()

    monkeypatch.setattr(
        asyncio,
        "get_event_loop",
        lambda: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    workspace._on_sigwinch(28, None)
