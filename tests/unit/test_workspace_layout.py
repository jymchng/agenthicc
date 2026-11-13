"""The live block's layout (TUX-02): bridge row, framed composer, panel, status."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.live_region import lines_text
from agenthicc.tui.workspace.workspace import Workspace

pytestmark = pytest.mark.unit


def _workspace(width: int = 80) -> Workspace:
    state = AppState.create()
    state.conversation.model_name.set("anthropic/claude-opus-4-8")
    console = Console(file=StringIO(), width=width, height=30, color_system=None)
    return Workspace(state, console)


def test_the_block_is_the_framed_composer_then_the_status_line() -> None:
    rows = lines_text(_workspace()._build_lines())
    assert len(rows) == 4
    assert rows[0].endswith(" new session · Safe ─")
    assert rows[1].startswith("❯ ")
    assert set(rows[2]) == {"─"}
    assert "Idle" in rows[3] and "opus-4.8" in rows[3]


def test_the_overflow_bridge_row_sits_above_the_frame() -> None:
    workspace = _workspace()
    workspace._state.conversation.live_tool_overflow.set(3)
    rows = lines_text(workspace._build_lines())
    assert rows[0] == "  └ …and 3 more tool calls"
    assert rows[1].endswith(" new session · Safe ─")


def test_subagent_panel_is_below_the_frame_and_above_the_status_line() -> None:
    from agenthicc.subagents.pool import SubagentPoolState, WorkerState

    workspace = _workspace()
    workspace._state.conversation.subagent_pool_state.set(
        SubagentPoolState("pool", 1, (WorkerState("explorer #1", "explorer", "running"),))
    )
    rows = lines_text(workspace._build_lines())
    bottom = next(index for index, row in enumerate(rows) if row and set(row) == {"─"})
    summary = next(index for index, row in enumerate(rows) if "Subagents" in row)
    status = next(index for index, row in enumerate(rows) if "Idle" in row)
    assert bottom < summary < status
    assert "● Explorer 1" in rows[summary + 1]
    assert "0/1 workers" in rows[status]
