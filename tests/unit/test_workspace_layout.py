"""Unit tests for PRD-73 — Workspace blank separator and dynamic status height."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

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


# ── _build() contains a blank separator as its first element ─────────────────


def test_build_first_element_is_blank():
    """Workspace._build() must start with Text('') as the blank separator."""
    from rich.text import Text
    from rich.console import Group
    from agenthicc.tui.workspace.workspace import Workspace

    console = MagicMock()
    state = _make_app_state()
    state.input = MagicMock()
    state.input.paste_condensed.return_value = False
    state.input.paste_label.return_value = ""
    state.input.buf.return_value = []
    state.input.cursor.return_value = 0
    state.overlay.return_value = ""

    ws = Workspace(state, console)
    ws.overlays._overlay = None  # no active overlay

    group = ws._build()
    assert isinstance(group, Group)
    first = group.renderables[0]
    assert isinstance(first, Text)
    assert first.plain == ""  # blank line


def test_build_total_live_block_height_with_model():
    """With model set: Live Block = 1 blank + 3 status + 1 border + 1 composer
    + 1 border + 2 footer = 9 lines (borders and composer may vary, but
    blank + status must be 4).
    """
    from agenthicc.tui.workspace.workspace import Workspace

    console = MagicMock()
    state = _make_app_state()
    state.input = MagicMock()
    state.input.paste_condensed.return_value = False
    state.input.paste_label.return_value = ""
    state.input.buf.return_value = []
    state.input.cursor.return_value = 0
    state.overlay.return_value = ""

    ws = Workspace(state, console)
    ws.overlays._overlay = None

    group = ws._build()
    # First element: blank separator
    assert group.renderables[0].plain == ""
    # Second element: status bar Group with 3 lines (model is set)
    status_rendered = group.renderables[1]
    assert _rendered_line_count(status_rendered) == 3
