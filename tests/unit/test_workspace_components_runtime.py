"""Coverage for status line, composer, frame and notice rendering variants."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agenthicc.runners.startup import StartupCoordinator
from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.conversation_store import AppState
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


def _real(width_model: str = "anthropic/claude-opus-4-8") -> AppState:
    state = AppState.create()
    state.conversation.model_name.set(width_model)
    return state


def _plain(markup: str) -> str:
    from rich.text import Text

    return Text.from_markup(markup).plain


def test_compaction_shows_in_the_state_segment() -> None:
    state = _real()
    state.conversation.begin_turn("agent", "turn-1")
    state.conversation.compaction_active.set(True)
    assert "Compacting" in _plain(components.StatusLine(state).markup(80))
    assert components._fmt_elapsed(61.2) == "1m 1s"


def test_attention_and_running_sessions_are_status_segments() -> None:
    state = _real()
    conv = state.conversation
    conv.background_needs_you.set(4)
    conv.background_running.set(3)
    state.agent.set("reviewer")

    line = _plain(components.StatusLine(state).markup(160))
    assert "◆ 4 sessions need you ←" in line
    assert "● 3 sessions running" in line
    frame = components.ComposerFrame(state, components.ComposerComponent(state))
    assert "reviewer · Safe" in _plain(frame.top(120))


def test_status_reports_deferred_startup_without_claiming_readiness() -> None:
    coordinator = StartupCoordinator()
    coordinator.begin("extensions", deferred=True)

    line = _plain(components.StatusLine(_real(), coordinator).markup(120))

    assert "extensions loading" in line


def test_status_preserves_priority_and_fits_display_cells() -> None:
    from agenthicc.subagents.pool import SubagentPoolState, WorkerState
    from agenthicc.tui.rendering import visible_len

    state = _real("openai/deepseek-v4.1-flash")
    conv = state.conversation
    conv.begin_turn("agent", "turn-1")
    conv.set_tool("run_tests tests/unit/test_status_layout.py")
    conv.tokens_in.set(230_044_280)
    conv.tokens_out.set(1_506_692)
    conv.usage_status.set("partial")
    conv.subagent_pool_state.set(
        SubagentPoolState(
            "pool",
            4,
            (
                WorkerState("done", "tester", "done"),
                WorkerState("failed", "tester", "failed"),
                WorkerState("running", "tester", "running"),
                WorkerState("queued", "tester", "pending"),
            ),
        )
    )

    line = components.StatusLine(state)
    for width in (40, 48, 64, 80, 120, 200):
        row = line.markup(width)
        assert visible_len(row) <= width
        assert "Running" in _plain(row)

    wide = _plain(line.markup(200))
    assert "Running run_tests tests/unit/test_status_layout.py" in wide
    assert "1/4 workers" in wide  # failures do not count as successful completions
    assert "≈↑230M ↓1.5M" in wide
    assert "openai/deepseek-v4.1-flash" in wide
    # Narrow: the tool argument goes before the state segment is cut.
    assert "Running run_tests" in _plain(line.markup(40))


def test_unavailable_usage_is_simply_absent() -> None:
    state = _real()
    state.conversation.begin_turn("agent", "turn-1")
    line = _plain(components.StatusLine(state).markup(200))
    assert "↑" not in line and "↓" not in line and "usage" not in line and "cost" not in line


def test_screen_reader_status_spells_out_exact_usage() -> None:
    from agenthicc.config import UISettings
    from agenthicc.tui.rendering import visible_len

    state = _real()
    state.ui.set(UISettings(screen_reader=True))
    conv = state.conversation
    conv.tokens_in.set(230_044_280)
    conv.tokens_out.set(1_506_692)
    conv.usage_status.set("partial")

    line = components.StatusLine(state)
    wide = _plain(line.markup(200))
    assert "about 230,044,280 tokens in, 1,506,692 out" in wide
    assert all(glyph not in wide for glyph in "↑↓●·")
    assert visible_len(line.markup(48)) <= 48


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


def test_notices_replace_the_hint_row_and_hints_follow_the_state() -> None:
    from agenthicc.tui.workspace.live_region import lines_text

    state = _real()
    console = Console(file=__import__("io").StringIO(), width=80, height=30, color_system=None)
    from agenthicc.tui.workspace.workspace import Workspace

    workspace = Workspace(state, console)
    state.conversation.begin_turn("agent", "turn-1")
    state.conversation.notification.set("first\nsecond")
    rows = lines_text(workspace._build_lines())
    assert rows[-2:] == ["first", "second"]

    state.conversation.notification.set(None)
    running = lines_text(workspace._build_lines())[-1]
    assert "Esc pause" in running and "expand paste" not in running

    state.input.update(list("x"), 1, paste_condensed=True, paste_label="x")
    assert "Ctrl+V expand paste" in lines_text(workspace._build_lines())[-1]

    state.input.update([], 0)
    state.conversation.close_turn()
    assert "Idle" in lines_text(workspace._build_lines())[-1]


def test_workflow_phase_is_a_status_segment() -> None:
    state = _real()
    state.update_workflow_phase(
        workflow_name="flow",
        phase_name="execute",
        phase_index=1,
        total_phases=3,
        run_id="run",
        intent="x",
        model_id="anthropic/claude-sonnet-4-5",
    )
    line = _plain(components.StatusLine(state).markup(160))
    # The workflow's name and phase (operator request), kept with the state.
    assert "◆ flow → Execute 2/3" in line
    assert "sonnet-4.5" in line  # the phase's model


def test_notices_wrap_complete_workflow_run_ids_instead_of_ellipsizing() -> None:
    state = _real()
    run_ids = (
        "542c8a3e-4f1b-4d2c-9a7e-123456789abc",
        "7f6e5d4c-3b2a-1908-fedc-ba9876543210",
    )
    message = f"2 workflows can be resumed. Use /workflow resume <run-id>: {', '.join(run_ids)}"
    state.conversation.notification.set(message)

    rows = components.notice_rows(state, 40)
    text = "\n".join(_plain(row) for row in rows)

    assert message in text.replace("\n", "")
    assert all(run_id in text for run_id in run_ids)
    assert "…" not in text
    assert len(rows) == len(components._wrap_notification(message, 40))
