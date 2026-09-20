"""Interactive manager for durable session-scoped ``/loop`` jobs."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.runners.loop_scheduler import LoopRecord


class LoopJobsOverlay(Overlay):
    """Table of all persisted loops with run-now and delete actions.

    The callbacks own scheduler and session-ownership policy.  This overlay
    only presents records and translates keys, so Enter cannot accidentally
    create a second provider or workflow execution path.
    """

    name = "loop-jobs"
    _PAGE_SIZE = 8

    def __init__(
        self,
        loader: Callable[[], list["LoopRecord"]],
        on_close: Callable[[], None],
        on_run: Callable[["LoopRecord"], str],
        on_delete: Callable[["LoopRecord"], str],
        *,
        session_id: str = "",
    ) -> None:
        self._loader = loader
        self._on_close = on_close
        self._on_run = on_run
        self._on_delete = on_delete
        self._session_id = session_id
        self._records: list[LoopRecord] = []
        self._selected = 0
        self._confirm_delete = False
        self._message = ""

    @property
    def records(self) -> list["LoopRecord"]:
        """Return the latest durable projection used by tests and callers."""

        self._refresh()
        return list(self._records)

    @property
    def selected_record(self) -> "LoopRecord | None":
        self._refresh()
        if not self._records:
            return None
        self._selected = max(0, min(self._selected, len(self._records) - 1))
        return self._records[self._selected]

    @property
    def page_count(self) -> int:
        return max(1, (len(self._records) + self._PAGE_SIZE - 1) // self._PAGE_SIZE)

    def _refresh(self) -> None:
        try:
            records = list(self._loader())
        except Exception as exc:  # noqa: BLE001
            self._records = []
            self._message = f"Unable to load loop jobs: {type(exc).__name__}: {exc}"
            return
        selected_id = None
        if self._records:
            selected_index = min(max(self._selected, 0), len(self._records) - 1)
            selected_id = self._records[selected_index].loop_id
        self._records = sorted(
            records,
            key=lambda record: (-record.updated_at, record.session_id, record.loop_id),
        )
        if selected_id:
            self._selected = next(
                (
                    index
                    for index, record in enumerate(self._records)
                    if record.loop_id == selected_id
                ),
                min(self._selected, max(0, len(self._records) - 1)),
            )
        else:
            self._selected = min(self._selected, max(0, len(self._records) - 1))

    def render(self) -> "RenderableType":
        from rich import box  # noqa: PLC0415
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from agenthicc.runners.loop_scheduler import (  # noqa: PLC0415
            _redacted_preview,
            format_loop_duration,
        )

        self._refresh()
        page = self._selected // self._PAGE_SIZE + 1
        table = Table(
            title=f"Scheduled Loops (page {page}/{self.page_count}; {len(self._records)})",
            box=box.SIMPLE,
            expand=True,
        )
        table.add_column("", width=2)
        table.add_column("Job ID", no_wrap=True)
        table.add_column("State", no_wrap=True)
        table.add_column("Every", no_wrap=True)
        table.add_column("Next", no_wrap=True)
        table.add_column("Session", no_wrap=True)
        table.add_column("Prompt / command")
        start = (self._selected // self._PAGE_SIZE) * self._PAGE_SIZE
        end = min(len(self._records), start + self._PAGE_SIZE)
        for index in range(start, end):
            record = self._records[index]
            next_text = "now" if record.pending else _format_time(record.next_due_at)
            session_text = (
                "current" if record.session_id == self._session_id else record.session_id[:12]
            )
            table.add_row(
                "▶" if index == self._selected else "",
                record.loop_id[:12],
                record.state.value,
                format_loop_duration(record.interval_s),
                next_text,
                session_text,
                _redacted_preview(record.payload, limit=72),
            )
        if not self._records:
            table.add_row("", "—", "—", "—", "—", "—", "No scheduled loop jobs")

        selected = self.selected_record
        if selected is None:
            detail = "No scheduled loop jobs are available."
        else:
            detail = (
                f"[bold]Job[/bold] {selected.loop_id}\n"
                f"[bold]Session[/bold] {selected.session_id}\n"
                f"[bold]State[/bold] {selected.state.value}\n"
                f"[bold]Runs[/bold] {selected.runs}  [bold]Failures[/bold] {selected.failures}\n"
                f"[bold]Payload[/bold] {_redacted_preview(selected.payload, limit=220)}"
            )
        if self._confirm_delete and selected is not None:
            detail += "\n\n[bold yellow]Delete this job permanently? Press Enter/y to confirm or Esc/n to cancel.[/bold yellow]"
        if self._message:
            detail += f"\n\n[dim]{self._message}[/dim]"
        return Group(
            table,
            Panel(detail, title="Selected Loop", border_style="cyan"),
            Text(
                "↑↓/j/k select   PgUp/PgDn page   Home/End   Enter run now   d delete   r refresh   Esc close",
                style="dim",
            ),
        )

    def _move(self, delta: int) -> None:
        if not self._records:
            return
        self._selected = max(0, min(len(self._records) - 1, self._selected + delta))

    def handle_key(self, key: Key, ch: str) -> bool:
        self._refresh()
        if self._confirm_delete:
            if key == Key.ESC or (key == Key.CHAR and ch.lower() == "n"):
                self._confirm_delete = False
                self._message = "Delete cancelled."
                return True
            if key == Key.ENTER or (key == Key.CHAR and ch.lower() == "y"):
                selected = self.selected_record
                self._confirm_delete = False
                if selected is not None:
                    self._message = self._on_delete(selected)
                    self._refresh()
                return True
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
            case Key.CHAR if ch.lower() == "k":
                self._move(-1)
            case Key.CHAR if ch.lower() == "j":
                self._move(1)
            case Key.CHAR if ch.lower() == "d":
                if self.selected_record is not None:
                    self._confirm_delete = True
                    self._message = ""
            case Key.CHAR if ch.lower() == "r":
                self._refresh()
            case Key.ENTER:
                selected = self.selected_record
                if selected is not None:
                    self._message = self._on_run(selected)
                    # The current job has been handed to the scheduler. Close
                    # so the user sees the normal session status immediately.
                    self._on_close()
        return True


def _format_time(value: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return "unknown"
