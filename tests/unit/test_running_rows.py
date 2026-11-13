"""Running tools and background terminals: live rows with a blinking dot.

Also covers the status line's workflow segment following the workflow handle,
whichever runner drives it (operator requests, 2026-11-07).
"""

from __future__ import annotations

import time
from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text

from agenthicc.config import UISettings
from agenthicc.tui.conversation_store import AgentState, AppState, RunningTool
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.live_region import lines_text
from agenthicc.tui.workspace.running_rows import BLINK_FRAMES, MAX_ROWS, running_rows

pytestmark = pytest.mark.unit


def _state(ui: UISettings | None = None) -> AppState:
    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    state.conversation.model_name.set("anthropic/claude-opus-4-8")
    return state


def _plain(rows: list[str]) -> list[str]:
    return [Text.from_markup(row).plain for row in rows]


def _tool(key: str, target: str, age: float, *, background: bool = False) -> RunningTool:
    return RunningTool(key, "$", target, time.time() - age, background=background)


def test_nothing_runs_nothing_is_drawn() -> None:
    assert running_rows(_state(), 80) == []


def test_a_running_call_and_a_background_terminal_each_get_a_row() -> None:
    state = _state()
    state.conversation.start_running_tool(_tool("t1", "uv run pytest -q", 490))
    state.conversation.background_tools.set(
        (_tool("terminal:a", "npm run dev", 182, background=True),)
    )
    rows = _plain(running_rows(state, 72))
    assert rows[0] == "" and rows[-1] == ""  # set apart from transcript and composer
    # The elapsed time follows the call in parentheses (operator request).
    assert rows[1] == "● $ uv run pytest -q (8m 10s)"
    assert rows[2] == "● $ npm run dev (background · 3m 2s)"
    assert all(len(row) <= 72 for row in rows)


def test_the_dot_blinks_on_the_frame_clock() -> None:
    state = _state()
    state.conversation.start_running_tool(_tool("t1", "make", 1))
    on = running_rows(state, 80)[1]
    state.conversation.frame.set(BLINK_FRAMES)
    off = running_rows(state, 80)[1]
    state.conversation.frame.set(2 * BLINK_FRAMES)
    assert on != off and running_rows(state, 80)[1] == on
    assert "bold" in on.split("●")[0] and "bold" not in off.split("●")[0]


@pytest.mark.parametrize("ui", [UISettings(reduced_motion=True), UISettings(screen_reader=True)])
def test_reduced_motion_keeps_the_dot_still(ui: UISettings) -> None:
    state = _state(ui)
    state.conversation.start_running_tool(_tool("t1", "make", 1))
    first = running_rows(state, 80)[1]
    state.conversation.frame.set(BLINK_FRAMES)
    assert running_rows(state, 80)[1] == first


def test_a_long_command_is_cut_at_its_end_and_extra_rows_are_counted() -> None:
    state = _state()
    for index in range(MAX_ROWS + 2):
        state.conversation.start_running_tool(_tool(f"t{index}", "x" * 200, 5))
    rows = _plain(running_rows(state, 60))
    assert rows[1].startswith("● $ xxx") and rows[1].endswith("… (5s)")
    assert rows[-2] == "  +2 more running"
    assert all(len(row) <= 60 for row in rows)


def test_finishing_a_call_removes_its_row_and_a_closed_turn_clears_them() -> None:
    conv = _state().conversation
    conv.start_running_tool(_tool("t1", "a", 1))
    conv.start_running_tool(_tool("t2", "b", 1))
    conv.finish_running_tool("t1")
    assert [item.key for item in conv.running_tools()] == ["t2"]
    conv.begin_turn("agent", "turn-1")
    conv.close_turn()
    assert conv.running_tools() == ()


def test_running_rows_keep_the_frame_clock_ticking_while_idle() -> None:
    conv = _state().conversation
    conv.tick()
    assert conv.frame() == 0  # an idle session does not animate
    conv.background_tools.set((_tool("terminal:a", "npm run dev", 1, background=True),))
    conv.tick()
    assert conv.frame() == 1


def test_the_rows_sit_under_the_transcript_above_the_composer() -> None:
    state = _state()
    state.conversation.agent_state.set(AgentState.RUNNING)
    state.conversation.start_running_tool(_tool("t1", "uv run pytest -q", 3))
    console = Console(file=StringIO(), force_terminal=True, width=80, height=30)
    rows = lines_text(Workspace(state, console)._build_lines())
    command = next(i for i, row in enumerate(rows) if row.startswith("● $ uv run pytest"))
    rule = next(i for i, row in enumerate(rows) if row.endswith("· Safe ─"))
    assert rows[command - 1] == "" and command < rule


# ── the workflow segment follows the handle (every runner) ───────────────────


class _Handle:
    """The handle attributes the session's listener reads."""

    def __init__(self) -> None:
        self.run_id = "run-1"
        self.workflow_name = "goal_flow"
        self.original_intent = "save main"
        self.lifecycle = "running"
        self.current_phase: str | None = None
        self.phase_index = 0
        self.topology_phase_names = ("clarify", "decide_goals", "implement_goal", "summarize")
        self.workflow = None


def _session(state: AppState) -> object:
    from types import SimpleNamespace

    from agenthicc.runners.tui_session import TUISession

    session = TUISession.__new__(TUISession)
    session._ctx = SimpleNamespace(app_state=state)  # type: ignore[attr-defined]
    return session


def test_a_runner_that_never_sets_workflow_run_still_shows_its_phase() -> None:
    from agenthicc.tui.workspace.components import StatusLine

    state = _state()
    session = _session(state)
    handle = _Handle()
    session._publish_workflow_progress(handle)  # type: ignore[attr-defined]
    assert state.workflow_run() is None  # no phase yet: nothing to show
    handle.current_phase, handle.phase_index = "decide_goals", 1
    session._publish_workflow_progress(handle)  # type: ignore[attr-defined]
    run = state.workflow_run()
    assert run is not None and run.status == "running" and run.total_phases == 4
    line = Text.from_markup(StatusLine(state).markup(100)).plain
    assert "◆ goal_flow → Decide goals 2/4" in line

    handle.lifecycle = "paused"
    session._publish_workflow_progress(handle)  # type: ignore[attr-defined]
    assert "paused" in Text.from_markup(StatusLine(state).markup(100)).plain

    handle.lifecycle = "complete"
    session._publish_workflow_progress(handle)  # type: ignore[attr-defined]
    assert "goal_flow" not in Text.from_markup(StatusLine(state).markup(100)).plain


def test_setting_the_handle_installs_the_listener() -> None:
    state = _state()
    session = _session(state)
    handle = _Handle()
    session._workflow_handle = handle  # type: ignore[attr-defined]
    assert handle.on_change == session._publish_workflow_progress  # type: ignore[attr-defined]


def test_the_real_handle_announces_phase_and_lifecycle_changes(tmp_path: object) -> None:
    from agenthicc.runners.workflow_handle import WorkflowRunHandle

    seen: list[tuple[str | None, str]] = []
    handle = WorkflowRunHandle.__new__(WorkflowRunHandle)
    handle.on_change = lambda h: seen.append((h.current_phase, h.lifecycle))
    handle.context = None
    handle.lifecycle = "running"
    handle.current_phase = None
    handle.phase_index = 0
    handle.phase_iteration = 0
    handle.checkpoint_supported = False
    handle.workflow = None
    handle.topology_phase_names = ()
    handle.update_phase("clarify", 0, 1)
    handle.mark_paused(reason="escape")
    handle.mark_resuming()
    handle.mark_terminal("complete")
    assert seen == [
        ("clarify", "running"),
        ("clarify", "paused"),
        ("clarify", "resuming"),
        ("clarify", "complete"),
    ]


def test_a_failing_listener_never_breaks_the_workflow() -> None:
    from agenthicc.runners.workflow_handle import WorkflowRunHandle

    def boom(_handle: WorkflowRunHandle) -> None:
        raise RuntimeError("presentation bug")

    handle = WorkflowRunHandle.__new__(WorkflowRunHandle)
    handle.on_change = boom
    handle.context = None
    handle.lifecycle = "running"
    handle.current_phase = None
    handle.phase_index = 0
    handle.phase_iteration = 0
    handle.checkpoint_supported = False
    handle.workflow = None
    handle.topology_phase_names = ()
    handle.update_phase("clarify", 0, 1)  # does not raise
    assert handle.current_phase == "clarify"
