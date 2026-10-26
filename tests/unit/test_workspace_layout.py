"""Unit tests for PRD-73 — Workspace blank separator and dynamic status height."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agenthicc.tui.workspace.workspace import Workspace

pytestmark = pytest.mark.unit


def _make_conv(model_name: str = "anthropic/claude-opus-4-8") -> MagicMock:
    """Return a ConversationStore-shaped mock."""
    conv = MagicMock()
    conv.model_name.return_value = model_name
    conv.agent_state.return_value = MagicMock(name="IDLE")
    conv.agent_state().name = "IDLE"
    conv.is_running.return_value = False
    conv.elapsed_s = 0.0
    conv.active_tool.return_value = ""
    conv.session_id.return_value = "abc-123"
    conv.turn_count.return_value = 0
    conv.cost_usd.return_value = 0.0
    conv.tokens_in.return_value = 0
    conv.tokens_out.return_value = 0
    conv.frame.return_value = 0
    conv.compaction_active.return_value = False
    return conv


def _make_app_state(model_name: str = "anthropic/claude-opus-4-8") -> MagicMock:
    state = MagicMock()
    state.conversation = _make_conv(model_name)
    # RuntimeMode attributes used by build_mode_str and FooterComponent.
    mode = state.active_mode.return_value
    mode.badge = "⏵⏵"
    mode.name = "Auto"
    mode.color = "green"
    return state


def _rendered_line_count(renderable) -> int:
    """Count the number of Text lines inside a Group or Text renderable."""
    from rich.console import Group
    from rich.text import Text

    if isinstance(renderable, Text):
        return 1
    if isinstance(renderable, Group):
        return sum(_rendered_line_count(r) for r in renderable.renderables)
    # Unknown renderable — assume 1 line
    return 1


# ── height() with model set ───────────────────────────────────────────────────


def test_status_height_with_model_is_four():
    from agenthicc.tui.workspace.components import StatusComponent

    comp = StatusComponent(_make_app_state("anthropic/claude-opus-4-8"))
    assert comp.height(80) == 4


def test_status_height_without_model_is_two():
    from agenthicc.tui.workspace.components import StatusComponent

    comp = StatusComponent(_make_app_state(""))
    assert comp.height(80) == 2


# ── height() matches render() line count (invariant I-10) ────────────────────


def test_status_height_matches_render_with_model():
    """height() == 1 (blank separator) + lines in render()."""
    from agenthicc.tui.workspace.components import StatusComponent

    comp = StatusComponent(_make_app_state("anthropic/claude-opus-4-8"))
    render_lines = _rendered_line_count(comp.render())
    # height() includes 1 blank separator not in render() itself
    assert comp.height(80) == render_lines + 1


def test_status_height_matches_render_without_model():
    from agenthicc.tui.workspace.components import StatusComponent

    comp = StatusComponent(_make_app_state(""))
    render_lines = _rendered_line_count(comp.render())
    assert comp.height(80) == render_lines + 1


# ── the live block starts with a blank separator ─────────────────────────────


def _workspace() -> Workspace:
    from io import StringIO

    from rich.console import Console

    console = Console(file=StringIO(), width=80, color_system=None)
    state = _make_app_state()
    state.input = MagicMock()
    state.input.paste_condensed.return_value = False
    state.input.paste_label.return_value = ""
    state.input.buf.return_value = []
    state.input.cursor.return_value = 0
    state.overlay.return_value = ""
    state.conversation.notification.return_value = None
    state.conversation.loop_status.return_value = None
    state.conversation.workflow_override.return_value = None
    state.conversation.subagent_pool_state.return_value = None
    state.conversation.live_tool_overflow.return_value = 0
    state.workflow_run.return_value = None
    ws = Workspace(state, console)
    ws.overlays._overlay = None  # no active overlay
    return ws


def test_build_first_line_is_blank():
    """The live block's first row is the blank separator."""
    from agenthicc.tui.workspace.live_region import lines_text

    assert lines_text(_workspace()._build_lines())[0] == ""


def test_build_total_live_block_height_with_model():
    """With a model: blank + 3 status rows + border + composer + border + 2
    footer rows + bottom margin."""
    from agenthicc.tui.workspace.live_region import lines_text

    rows = lines_text(_workspace()._build_lines())
    assert rows[0] == ""
    assert "abc-123" in rows[3]  # the third status row: session and cost
    assert set(rows[4]) == {"─"}
    assert len(rows) == 1 + 3 + 1 + 1 + 1 + 2 + 1
