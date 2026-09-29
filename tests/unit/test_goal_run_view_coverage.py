"""Behavior coverage for the goal-run projection view."""

from __future__ import annotations

import pytest
from rich.console import Console

from agenthicc.runs.model import (
    GoalRun,
    GoalRunStatus,
    RunAgentRecord,
    RunTaskRecord,
    RunWorktreeRecord,
)
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.goal_run_manager import (
    GoalRunManagerView,
    run_goal_run_manager,
)


class _Manager:
    def __init__(self, run: GoalRun) -> None:
        self.run = run
        self.cancelled = False

    def projection(self, run_id: str) -> GoalRun:
        assert run_id == self.run.run_id
        return self.run

    def cancel(self, run_id: str) -> GoalRun:
        assert run_id == self.run.run_id
        self.cancelled = True
        self.run = self.run.evolve(status=GoalRunStatus.CANCELLED)
        return self.run


def _run() -> GoalRun:
    return GoalRun.create(
        "Build the feature",
        repository="/repo",
        run_id="run_view",
        workflow_name="goal_flow",
    ).evolve(
        status=GoalRunStatus.NEEDS_ATTENTION,
        workflow_run_id="workflow-1",
        attention_reasons=("worker needs review",),
        agents=(
            RunAgentRecord(
                agent_id="agent-1",
                run_id="run_view",
                role="worker",
                task_id="task-1",
                status="running",
                current_phase="implement",
                worktree_id="wt-1",
                process_id=4242,
                exit_reason="workflow_complete",
            ),
        ),
        tasks=(RunTaskRecord(task_id="task-1", status="running", worktree_id="wt-1"),),
        worktrees=(
            RunWorktreeRecord(
                worktree_id="wt-1",
                branch="agenthicc/task-1",
                status="active",
                dirty=True,
            ),
        ),
    )


def test_goal_run_view_renders_and_handles_actions() -> None:
    manager = _Manager(_run())
    view = GoalRunManagerView(Console(width=120), manager, "run_view")

    assert view.refresh().run_id == "run_view"
    assert view.render() is not None
    assert view.handle_key(Key.ENTER) == view.handle_key("ENTER")
    assert view.handle_key(Key.ESC).action == "exit"  # type: ignore[union-attr]
    assert view.handle_key("CHAR", "?") is None
    assert view.help_visible
    assert view.render() is not None
    assert view.handle_key("CHAR", "?") is None
    assert not view.help_visible
    assert view.handle_key("CHAR", "r") is None
    assert view.handle_key("CHAR", "c") is None
    assert manager.cancelled
    assert view.handle_key("unknown") is None
    assert view.handle_key("CHAR", "Q").action == "exit"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_goal_run_view_noninteractive_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Backend:
        def is_interactive(self) -> bool:
            return False

    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: _Backend())
    result = await run_goal_run_manager(
        Console(width=120), manager=_Manager(_run()), run_id="run_view"
    )
    assert result.action == "exit"


@pytest.mark.asyncio
async def test_goal_run_view_interactive_mode_exits_on_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RawMode:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    class _Backend:
        def is_interactive(self) -> bool:
            return True

        def enter_raw_mode(self) -> _RawMode:
            return _RawMode()

        def read_key(self) -> tuple[Key, str]:
            return (Key.ESC, "")

    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: _Backend())
    result = await run_goal_run_manager(
        Console(width=120), manager=_Manager(_run()), run_id="run_view"
    )
    assert result == type(result)("exit", "run_view")


def test_goal_run_key_value_accepts_non_string_values() -> None:
    manager = _Manager(_run())
    view = GoalRunManagerView(Console(), manager, "run_view")
    assert view.handle_key(123) is None
