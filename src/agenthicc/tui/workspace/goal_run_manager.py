"""Interactive projection for one durable goal run (PRD-204)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agenthicc.runs.manager import GoalRunManager
from agenthicc.runs.model import GoalRun

if TYPE_CHECKING:
    from rich.console import Console, RenderableType


@dataclass(frozen=True)
class GoalRunManagerResult:
    """Action returned when the goal-run view yields to its caller."""

    action: str
    run_id: str


def _key_value(key: object) -> str:
    from agenthicc.tui.cbreak_reader import Key  # noqa: PLC0415

    if isinstance(key, Key):
        return key.value
    if isinstance(key, str):
        return key
    return str(key)


class GoalRunManagerView:
    """Small run-aware view that consumes the same projection as the CLI."""

    def __init__(self, console: "Console", manager: GoalRunManager, run_id: str) -> None:
        self.console = console
        self.manager = manager
        self.run_id = run_id
        self._goal_run: GoalRun | None = None
        self.help_visible = False

    def refresh(self) -> GoalRun:
        self._goal_run = self.manager.projection(self.run_id)
        return self._goal_run

    def render(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415

        run = self._goal_run or self.refresh()
        if self.help_visible:
            return Panel(
                "Enter attach  r refresh  c cancel  q/Esc exit\n"
                "The view is a projection; sessions, checkpoints, manifests, and Git remain authoritative.",
                title="Goal Run — Help",
                border_style="cyan",
            )
        agents = Table(title=f"Agents — {run.run_id}", expand=True)
        agents.add_column("Role")
        agents.add_column("Status")
        agents.add_column("Task")
        agents.add_column("Phase")
        agents.add_column("PID")
        agents.add_column("Exit")
        agents.add_column("Worktree")
        for agent in run.agents:
            agents.add_row(
                agent.role,
                agent.status,
                agent.task_id or "—",
                agent.current_phase or "—",
                str(agent.process_id) if agent.process_id is not None else "—",
                agent.exit_reason or "—",
                agent.worktree_id or "—",
            )
        if not run.agents:
            agents.add_row(
                "main",
                run.status.value,
                "—",
                "—",
                str(run.worker_pid) if run.worker_pid is not None else "—",
                run.worker_exit_reason or "—",
                "—",
            )

        tasks = Table(title="Tasks and worktrees", expand=True)
        tasks.add_column("Task")
        tasks.add_column("State")
        tasks.add_column("Branch")
        tasks.add_column("Git")
        for task in run.tasks:
            worktree = next(
                (item for item in run.worktrees if item.worktree_id == task.worktree_id),
                None,
            )
            tasks.add_row(
                task.task_id,
                task.status,
                worktree.branch if worktree is not None else "—",
                (
                    "dirty"
                    if worktree is not None and worktree.dirty
                    else worktree.status
                    if worktree is not None
                    else "—"
                ),
            )
        if not run.tasks:
            tasks.add_row("—", "No worker tasks", "—", "—")

        summary = (
            f"[bold]Goal[/bold] {run.goal}\n"
            f"[bold]Repository[/bold] {run.repository_root}\n"
            f"[bold]Status[/bold] {run.status.value}\n"
            f"[bold]Workflow[/bold] {run.workflow_name}"
            + (f"  [bold]Phase[/bold] {run.workflow_run_id}" if run.workflow_run_id else "")
        )
        if run.worker_pid is not None:
            summary += f"\n[bold]Worker PID[/bold] {run.worker_pid}"
        if run.worker_exit_reason:
            summary += f"\n[bold]Exit[/bold] {run.worker_exit_reason}"
        if run.attention_reasons:
            summary += "\n[bold yellow]Attention[/bold yellow] " + "; ".join(
                run.attention_reasons[:8]
            )
        return Group(
            Panel(summary, title="Goal Run"),
            agents,
            tasks,
            "Enter attach · r refresh · c cancel · q exit",
        )

    def handle_key(self, key: object, ch: str = "") -> GoalRunManagerResult | None:
        value = _key_value(key)
        if value == "ENTER":
            return GoalRunManagerResult("attach", self.run_id)
        if value == "ESC" or ch.lower() == "q":
            return GoalRunManagerResult("exit", self.run_id)
        if ch == "?":
            self.help_visible = not self.help_visible
        elif ch.lower() == "r":
            self.refresh()
        elif ch.lower() == "c":
            self.manager.cancel(self.run_id)
            self.refresh()
        return None

    async def run(self) -> GoalRunManagerResult:
        from agenthicc.tui.terminal.backend import get_backend  # noqa: PLC0415
        from rich.live import Live  # noqa: PLC0415

        backend = get_backend()
        if not backend.is_interactive():
            self.console.print(self.render())
            return GoalRunManagerResult("exit", self.run_id)
        with Live(self.render(), console=self.console, refresh_per_second=4) as live:
            with backend.enter_raw_mode():
                while True:
                    key, ch = await asyncio.get_running_loop().run_in_executor(
                        None, backend.read_key
                    )
                    result = self.handle_key(key, ch)
                    live.update(self.render(), refresh=True)
                    if result is not None:
                        return result


async def run_goal_run_manager(
    console: "Console", *, manager: GoalRunManager, run_id: str
) -> GoalRunManagerResult:
    """Run the goal projection in interactive or diagnostic mode."""

    return await GoalRunManagerView(console, manager, run_id).run()
