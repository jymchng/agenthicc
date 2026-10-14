"""Interactive repository schedule manager (PRD-228)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agenthicc.schedules import (
    OccurrenceState,
    Schedule,
    ScheduleManager,
    ScheduleServiceLease,
    ScheduleStore,
    format_timestamp,
)
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType


class ScheduleJobsOverlay(Overlay):
    """Paginated table with run-now, pause/resume, inspect, and delete actions."""

    name = "schedules"
    _PAGE_SIZE = 10

    def __init__(
        self,
        store: ScheduleStore,
        manager: ScheduleManager,
        repository_root: str,
        on_close: Callable[[], None],
        set_input_text: Callable[[str], None],
    ) -> None:
        self.store = store
        self.manager = manager
        self.repository_root = repository_root
        self._on_close = on_close
        self._set_input_text = set_input_text
        self._records: list[Schedule] = []
        self._selected = 0
        self._confirm_delete = False
        self._message = ""
        self._expanded = False

    @property
    def records(self) -> list[Schedule]:
        self._refresh()
        return list(self._records)

    @property
    def selected_record(self) -> Schedule | None:
        self._refresh()
        if not self._records:
            return None
        self._selected = min(max(self._selected, 0), len(self._records) - 1)
        return self._records[self._selected]

    @property
    def page_count(self) -> int:
        return max(1, (len(self._records) + self._PAGE_SIZE - 1) // self._PAGE_SIZE)

    def _refresh(self) -> None:
        selected_id = self._records[self._selected].schedule_id if self._records else ""
        try:
            self._records = self.store.list(repository_root=self.repository_root)
        except Exception as exc:  # noqa: BLE001
            self._records = []
            self._message = f"Unable to load schedules: {type(exc).__name__}: {exc}"
            return
        self._selected = next(
            (i for i, item in enumerate(self._records) if item.schedule_id == selected_id),
            min(self._selected, max(0, len(self._records) - 1)),
        )

    def _status(self, record: Schedule) -> str:
        if record.deleted:
            return "deleted"
        if not record.enabled:
            return "paused"
        if record.recurrence_kind.value == "once" and record.next_run_at is None:
            return "completed"
        latest = self.store.history(record.schedule_id, limit=1)
        if latest and latest[0].state is OccurrenceState.BLOCKED:
            return "blocked"
        return (
            "armed" if ScheduleServiceLease.is_running(self.store) is True else "awaiting service"
        )

    def render(self) -> RenderableType:
        from rich import box  # noqa: PLC0415
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        self._refresh()
        service_running = ScheduleServiceLease.is_running(self.store) is True
        page = self._selected // self._PAGE_SIZE + 1
        table = Table(
            title=f"Repository Schedules · page {page}/{self.page_count} · {len(self._records)} total",
            box=box.SIMPLE,
            expand=True,
        )
        table.add_column("", width=2)
        table.add_column("Schedule", no_wrap=True)
        table.add_column("Workflow", no_wrap=True)
        table.add_column("State", no_wrap=True)
        table.add_column("Next run", no_wrap=True)
        table.add_column("Recurrence", no_wrap=True)
        start = (self._selected // self._PAGE_SIZE) * self._PAGE_SIZE
        for index in range(start, min(len(self._records), start + self._PAGE_SIZE)):
            item = self._records[index]
            table.add_row(
                "▶" if index == self._selected else "",
                item.schedule_id[:14],
                item.workflow_name,
                self._status(item),
                format_timestamp(item.next_run_at, item.timezone),
                f"{item.recurrence_kind.value}:{item.recurrence_spec}",
            )
        if not self._records:
            table.add_row("", "—", "—", "—", "—", "No repository schedules")

        selected = self.selected_record
        if selected is None:
            detail = "Create a schedule with n, or use `agenthicc schedules create` from the CLI."
        else:
            detail = (
                f"[bold]ID[/bold] {selected.schedule_id}  [bold]Revision[/bold] {selected.revision}\n"
                f"[bold]State[/bold] {self._status(selected)}  [bold]Service[/bold] "
                f"{'running' if service_running else 'awaiting service'}\n"
                f"[bold]Workspace[/bold] {selected.repository_root}\n"
                f"[bold]Profile / ceiling[/bold] {selected.profile_name} / {selected.permission_ceiling}\n"
                f"[bold]Timezone[/bold] {selected.timezone}  [bold]Next[/bold] "
                f"{format_timestamp(selected.next_run_at, selected.timezone)}\n"
                f"[bold]Prompt[/bold] {selected.prompt if self._expanded else _preview(selected.prompt, 110)}"
            )
            history = self.store.history(selected.schedule_id, limit=3)
            if history:
                detail += "\n[bold]Recent runs[/bold] " + " · ".join(
                    f"{item.state.value}:{item.session_id[:8] or '—'}" for item in history
                )
        if self._confirm_delete and selected is not None:
            detail += "\n\n[bold yellow]Delete this schedule? Press Enter/y to confirm, Esc/n to cancel.[/bold yellow]"
        if self._message:
            detail += f"\n\n[dim]{self._message}[/dim]"
        hint = (
            "↑↓/j/k select   Enter run now   p pause/resume   i expand   n new schedule   "
            "d delete   r refresh   Esc close"
        )
        return Group(
            table,
            Panel(detail, title="Schedule Details", border_style="cyan"),
            Text(hint, style="dim"),
        )

    def _move(self, delta: int) -> None:
        if self._records:
            self._selected = min(max(self._selected + delta, 0), len(self._records) - 1)

    def handle_key(self, key: Key, ch: str) -> bool:
        self._refresh()
        selected = self.selected_record
        if self._confirm_delete:
            if key == Key.ESC or (key == Key.CHAR and ch.casefold() == "n"):
                self._confirm_delete = False
                self._message = "Deletion cancelled."
            elif key == Key.ENTER or (key == Key.CHAR and ch.casefold() == "y"):
                self._confirm_delete = False
                if selected is not None:
                    try:
                        self.manager.delete(selected.schedule_id, actor="tui")
                        self._message = (
                            "Schedule deleted; run history and artifacts were preserved."
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._message = f"Delete failed: {type(exc).__name__}: {exc}"
                    self._refresh()
            return True

        match key:
            case Key.ESC:
                self._on_close()
            case Key.UP:
                self._move(-1)
            case Key.DOWN:
                self._move(1)
            case Key.PAGE_UP:
                self._move(-self._PAGE_SIZE)
            case Key.PAGE_DOWN:
                self._move(self._PAGE_SIZE)
            case Key.HOME:
                self._selected = 0
            case Key.END:
                self._selected = max(0, len(self._records) - 1)
            case Key.CHAR if ch.casefold() == "k":
                self._move(-1)
            case Key.CHAR if ch.casefold() == "j":
                self._move(1)
            case Key.CHAR if ch.casefold() == "r":
                self._refresh()
                self._message = "Schedules refreshed."
            case Key.CHAR if ch.casefold() == "i":
                self._expanded = not self._expanded
            case Key.CHAR if ch.casefold() == "n":
                self._set_input_text("/schedule create ")
                self._on_close()
            case Key.CHAR if ch.casefold() == "d":
                if selected is not None:
                    self._confirm_delete = True
                    self._message = ""
            case Key.CHAR if ch.casefold() == "p":
                if selected is not None:
                    try:
                        if selected.enabled:
                            updated = self.manager.pause(selected.schedule_id, actor="tui")
                            self._message = f"Paused at revision {updated.revision}."
                        else:
                            next_run = format_timestamp(selected.next_run_at, selected.timezone)
                            updated = self.manager.resume(
                                selected.schedule_id,
                                confirm_next_run=next_run,
                                actor="tui",
                            )
                            self._message = f"Enabled for {next_run} [{updated.timezone}]."
                    except Exception as exc:  # noqa: BLE001
                        self._message = f"Pause/resume failed: {type(exc).__name__}: {exc}"
            case Key.ENTER:
                if selected is not None:
                    try:
                        run = self.manager.run_now(selected.schedule_id, actor="tui")
                        self._message = (
                            f"Run accepted · session {run.session_id}; service will dispatch it."
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._message = f"Run now failed: {type(exc).__name__}: {exc}"
        return True


def _preview(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"
