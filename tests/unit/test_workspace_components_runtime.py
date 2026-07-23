"""Coverage for status, composer, and footer rendering variants."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agenthicc.tui.workspace import components

pytestmark = pytest.mark.unit


def _state() -> MagicMock:
    state = MagicMock()
    conv = state.conversation
    conv.frame.return_value = 11
    conv.is_running.return_value = True
    conv.agent_state.return_value = SimpleNamespace(name="RECOVERING")
    conv.compaction_active.return_value = True
    conv.elapsed_s = 61.2
    conv.tokens_in.return_value = 1000
    conv.tokens_out.return_value = 2000
    conv.subagent_pool_state.return_value = SimpleNamespace(
        done=1,
        total=3,
        workers=[
            SimpleNamespace(status="pending", label="one"),
            SimpleNamespace(status="running", label="two"),
            SimpleNamespace(status="done", label="three"),
            SimpleNamespace(status="failed", label="four"),
            SimpleNamespace(status="unknown", label="five"),
        ],
    )
    conv.model_name.return_value = "provider/model [x]"
    conv.session_id.return_value = "sid"
    conv.turn_count.return_value = 2
    conv.cost_usd.return_value = 1.25
    conv.workflow_override.return_value = "daily [safe]"
    conv.notification.return_value = "first\nsecond"
    state.active_mode.return_value = SimpleNamespace(name="Auto", badge="A", color="green")
    state.input.paste_condensed.return_value = False
    state.input.paste_label.return_value = ""
    state.input.buf.return_value = list("one\ntwo")
    state.input.cursor.return_value = 5
    state.workflow_run.return_value = SimpleNamespace(
        status="running",
        current_phase="execute",
        current_phase_index=1,
        total_phases=3,
        current_phase_model="phase-model",
        workflow_name="flow",
    )
    return state


def test_status_running_compacting_and_helper_variants() -> None:
    state = _state()
    status = components.StatusComponent(state)
    rendered = status.render()
    assert "Compacting" in rendered.renderables[1].plain
    assert status.height(80) == 4
    assert components._fmt_elapsed(61.2) == "1m 1s"
    assert "bold" in components._thinking_markup(10)
    assert components._build_hints("X  one word", 10)
    assert "five" in components._build_worker_grid(state.conversation.subagent_pool_state(), 200)


def test_composer_condensed_multiline_and_height() -> None:
    state = _state()
    composer = components.ComposerComponent(state)
    state.input.paste_condensed.return_value = True
    state.input.paste_label.return_value = "pasted content"
    assert composer.render().plain
    assert composer.height(80) == 1
    state.input.paste_condensed.return_value = False
    state.input.buf.return_value = list("a\nb")
    state.input.cursor.return_value = 1
    group = composer.render()
    assert len(group.renderables) == 2
    assert composer.height(4) >= 2
    assert components._render_multiline(list("abc"), 99)


def test_footer_notification_paste_hints_workflow_and_pool() -> None:
    state = _state()
    footer = components.FooterComponent(state)
    rendered = footer.render()
    assert len(rendered.renderables) >= 4
    assert footer.height(80) == 5

    state.conversation.notification.return_value = None
    state.input.paste_condensed.return_value = True
    state.conversation.subagent_pool_state.return_value = None
    state.workflow_run.return_value = None
    assert "Expand" in footer.render().renderables[1].plain

    state.input.paste_condensed.return_value = False
    state.conversation.agent_state.return_value = SimpleNamespace(name="ERROR")
    assert "Retry" in footer.render().renderables[1].plain
