"""Coverage for status, composer, and footer rendering variants."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agenthicc.runners.startup import StartupCoordinator
from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.keymap import Keymap
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
    conv.current_directory.return_value = "/workspace/agenthicc"
    conv.turn_count.return_value = 2
    conv.background_needs_you.return_value = 0
    conv.background_running.return_value = 0
    conv.background_dnd.return_value = False
    conv.background_toast.return_value = None
    conv.cost_usd.return_value = 1.25
    conv.workflow_override.return_value = "daily [safe]"
    conv.notification.return_value = "first\nsecond"
    state.active_mode.return_value = SimpleNamespace(name="Auto", badge="A", color="green")
    state.keymap.return_value = Keymap.build(DEFAULT_ACTIONS)
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
    state.pending_approval.return_value = None
    return state


def test_status_running_compacting_and_helper_variants() -> None:
    state = _state()
    status = components.StatusComponent(state)
    rendered = status.render()
    assert "Compacting" in rendered.renderables[1].plain
    assert status.height(80) == 6
    assert components._fmt_elapsed(61.2) == "1m 1s"
    assert "bold" in components._thinking_markup(10)
    assert components._hint_row((("X", "one word"),), 10)


def test_status_keeps_full_identity_values_and_wraps_them_by_terminal_cells() -> None:
    from rich.text import Text

    from agenthicc.tui.rendering import visible_len

    state = _state()
    session_id = "session-" + "x" * 48
    current_directory = "/workspace/" + "nested/" * 8 + "project"
    state.conversation.session_id.return_value = session_id
    state.conversation.current_directory.return_value = current_directory
    component = components.StatusComponent(state)

    wide = [Text.from_markup(row).plain for row in component.markup_lines(120)]
    assert session_id in wide
    assert current_directory in wide

    narrow_rows = component.markup_lines(24)
    narrow = [Text.from_markup(row).plain for row in narrow_rows]
    assert all(visible_len(row) <= 24 for row in narrow_rows)
    assert session_id in "".join(narrow)
    assert current_directory in "".join(narrow)


def test_needs_you_footer_row_precedes_submit_hints() -> None:
    from rich.text import Text

    from agenthicc.tui.workspace.components import FooterComponent

    state = _state()
    conv = state.conversation
    conv.is_running.return_value = False
    conv.background_needs_you.return_value = 4
    conv.background_running.return_value = 3
    conv.background_dnd.return_value = False
    conv.subagent_pool_state.return_value = None
    conv.notification.return_value = None
    conv.loop_status.return_value = None
    conv.background_toast.return_value = None
    state.agent.return_value = "reviewer"

    rows = FooterComponent(state).markup_lines(120)
    plain = [Text.from_markup(row).plain for row in rows]

    assert "reviewer |" in plain[0]
    assert "◆ 4 need you  |  ● 3 running" in plain[1]
    assert "Enter submit" in plain[2]


def test_status_reports_deferred_startup_without_claiming_readiness() -> None:
    coordinator = StartupCoordinator()
    coordinator.begin("extensions", deferred=True)

    rendered = components.StatusComponent(_state(), coordinator).render()

    assert "Startup" in rendered.renderables[-1].plain
    assert "loading" in rendered.renderables[-1].plain


def test_status_preserves_priority_and_fits_display_cells() -> None:
    from rich.text import Text

    from agenthicc.subagents.pool import SubagentPoolState, WorkerState
    from agenthicc.tui.rendering import visible_len

    state = _state()
    conv = state.conversation
    conv.agent_state.return_value = SimpleNamespace(name="THINKING")
    conv.is_running.return_value = True
    conv.compaction_active.return_value = False
    conv.model_name.return_value = "openai/deepseek-v4.1-flash"
    conv.activity_elapsed_s = 91.0
    conv.active_tool.return_value = "run_tests tests/unit/test_status_layout.py"
    conv.tokens_in.return_value = 230_044_280
    conv.tokens_out.return_value = 1_506_692
    conv.usage_status.return_value = "partial"
    conv.cost_status.return_value = "unavailable"
    conv.subagent_pool_state.return_value = SubagentPoolState(
        "pool",
        4,
        (
            WorkerState("done", "tester", "done"),
            WorkerState("failed", "tester", "failed"),
            WorkerState("running", "tester", "running"),
            WorkerState("queued", "tester", "pending"),
        ),
    )
    state.workflow_run.return_value = None

    component = components.StatusComponent(state)
    for width in (40, 48, 64, 80, 120, 200):
        rows = component.markup_lines(width)
        assert all(visible_len(row) <= width for row in rows)
        assert "Thinking" in Text.from_markup(rows[0]).plain
        assert any("usage partial" in Text.from_markup(row).plain for row in rows)
        assert any("cost unknown" in Text.from_markup(row).plain for row in rows)

    wide_rows = [Text.from_markup(row).plain for row in component.markup_lines(120)]
    assert "1/4 workers" in wide_rows[0]  # failures do not count as successful completions
    assert "run_tests" in wide_rows[0]
    assert "230M" in wide_rows[0] and "1.5M" in wide_rows[0]
    assert " | " in wide_rows[0]
    assert "openai | deepseek-v4.1-flash" in wide_rows[1]


def test_unavailable_usage_stays_on_secondary_metadata_row() -> None:
    from rich.text import Text

    state = _state()
    conv = state.conversation
    conv.agent_state.return_value = SimpleNamespace(name="THINKING")
    conv.is_running.return_value = True
    conv.compaction_active.return_value = False
    conv.tokens_in.return_value = 0
    conv.tokens_out.return_value = 0
    conv.usage_status.return_value = "unavailable"
    conv.cost_status.return_value = "unavailable"
    conv.subagent_pool_state.return_value = None
    state.workflow_run.return_value = None

    rows = [
        Text.from_markup(row).plain for row in components.StatusComponent(state).markup_lines(120)
    ]

    assert "usage unavailable" not in rows[0]
    assert any("usage unavailable" in row for row in rows[1:])
    assert all("↑ 0" not in row and "↓ 0" not in row for row in rows)


def test_screen_reader_status_spells_out_exact_usage_and_quality() -> None:
    from rich.text import Text

    from agenthicc.config import UISettings
    from agenthicc.tui.rendering import visible_len

    state = _state()
    state.ui = UISettings(screen_reader=True)
    conv = state.conversation
    conv.agent_state.return_value = SimpleNamespace(name="THINKING")
    conv.is_running.return_value = True
    conv.activity_elapsed_s = 91.0
    conv.tokens_in.return_value = 230_044_280
    conv.tokens_out.return_value = 1_506_692
    conv.usage_status.return_value = "partial"
    conv.cost_status.return_value = "unavailable"
    state.workflow_run.return_value = None

    rows = components.StatusComponent(state).markup_lines(48)
    plain = [Text.from_markup(row).plain for row in rows]

    assert all(visible_len(row) <= 48 for row in rows)
    assert any("230,044,280" in row and "Input tokens" in row for row in plain)
    assert any("1,506,692" in row and "Output tokens" in row for row in plain)
    assert any("usage partial" in row for row in plain)
    assert all("↑" not in row and "↓" not in row and "?" not in row for row in plain)


def test_idle_header_uses_compact_usage_quality_and_width(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import shutil
    from io import StringIO

    from agenthicc.tui.workspace.appender import ScrollBufferAppender
    from agenthicc.tui.rendering import visible_len

    state = _state()
    conv = state.conversation
    conv.session_id.return_value = "1234567890"
    conv.turn_count.return_value = 12
    conv.cost_usd.return_value = 0.1234
    conv.cost_status.return_value = "estimated"
    conv.tokens_in.return_value = 18_100
    conv.tokens_out.return_value = 260_300
    conv.usage_status.return_value = "partial"

    for width in (40, 48, 64, 80, 120, 200):
        monkeypatch.setattr(
            shutil,
            "get_terminal_size",
            lambda *_args, columns=width: os.terminal_size((columns, 24)),
        )
        console = Console(file=StringIO(), record=True, width=width, color_system=None)
        ScrollBufferAppender(state, console).print_idle_header()
        rows = console.export_text().splitlines()
        assert rows and visible_len(rows[0]) <= width
        assert set(rows[-1]) == {"─"}
        assert any("usage partial" in row for row in rows)

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda *_args: os.terminal_size((120, 24)),
    )
    console = Console(file=StringIO(), record=True, width=120, color_system=None)
    ScrollBufferAppender(state, console).print_idle_header()
    rows = console.export_text().splitlines()
    header = "\n".join(rows[:-1])
    assert "18.1k" in header and "260.3k" in header
    assert "~$0.123" in header and "1234567890" in header
    assert "/workspace/agenthicc" in header


def test_unavailable_idle_usage_never_looks_like_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import shutil
    from io import StringIO

    from agenthicc.tui.workspace.appender import ScrollBufferAppender

    state = _state()
    state.conversation.tokens_in.return_value = 0
    state.conversation.tokens_out.return_value = 0
    state.conversation.usage_status.return_value = "unavailable"
    state.conversation.cost_status.return_value = "unavailable"
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda *_args: os.terminal_size((80, 24)),
    )
    console = Console(file=StringIO(), record=True, width=80, color_system=None)

    ScrollBufferAppender(state, console).print_idle_header()

    header = "\n".join(console.export_text().splitlines()[:-1])
    assert "usage unavailable" in header and "cost unknown" in header
    assert "↑ 0" not in header and "↓ 0" not in header


def test_composer_condensed_multiline_and_height() -> None:
    state = _state()
    composer = components.ComposerComponent(state)
    state.input.paste_condensed.return_value = True
    state.input.paste_label.return_value = "pasted content"
    console = Console(width=80, record=True, color_system=None)
    console.print(composer.render())
    assert "pasted content" in console.export_text()
    assert composer.height(80) == 1
    state.input.paste_condensed.return_value = False
    state.input.buf.return_value = list("a\nb")
    state.input.cursor.return_value = 1
    assert len(composer.lines(80)) == 2
    # At four columns each row holds two cells after the prompt.
    assert composer.height(4) == 2


def test_footer_notification_paste_hints_workflow_and_pool() -> None:
    state = _state()
    footer = components.FooterComponent(state)
    rendered = footer.render()
    assert len(rendered.renderables) >= 4
    assert footer.height(80) == 4  # the workers are the subagent panel's (AGX-08)

    state.conversation.notification.return_value = None
    state.input.paste_condensed.return_value = True
    state.conversation.subagent_pool_state.return_value = None
    state.workflow_run.return_value = None
    assert footer.render().renderables[1].plain.startswith("Ctrl+V expand paste")

    # Hints are the composer bindings that apply now (AGX-13).
    state.input.paste_condensed.return_value = False
    running = footer.render().renderables[1].plain
    assert "Esc pause" in running and "expand paste" not in running
    state.conversation.is_running.return_value = False
    assert "pause" not in footer.render().renderables[1].plain


def test_footer_wraps_complete_workflow_run_ids_instead_of_ellipsizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state.workflow_run.return_value = None
    state.conversation.subagent_pool_state.return_value = None
    run_ids = (
        "542c8a3e-4f1b-4d2c-9a7e-123456789abc",
        "7f6e5d4c-3b2a-1908-fedc-ba9876543210",
    )
    message = f"2 workflows can be resumed. Use /workflow resume <run-id>: {', '.join(run_ids)}"
    state.conversation.notification.return_value = message
    monkeypatch.setattr(components, "_get_cols", lambda: 40)

    footer = components.FooterComponent(state)
    rendered = footer.render()
    notification_text = "\n".join(
        renderable.plain for renderable in rendered.renderables[1:] if hasattr(renderable, "plain")
    )

    assert message in notification_text.replace("\n", "")
    assert all(run_id in notification_text for run_id in run_ids)
    assert "…" not in notification_text
    assert footer.height(40) == len(components._wrap_notification(message, 40)) + 1
