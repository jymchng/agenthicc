"""Interactive selector for durable foreground sessions."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from agenthicc.types import JsonObject

if TYPE_CHECKING:
    from rich.console import Console, RenderableType


SessionRecord = tuple[str, JsonObject]
SessionLoader = Callable[[], list[SessionRecord]]


@dataclass(frozen=True)
class SessionManagerResult:
    """Result returned when the session selector yields control."""

    action: str
    session_id: str | None = None


def _key_value(key: object) -> str:
    return str(getattr(key, "value", key))


def _timestamp(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


class SessionManager:
    """Paginated, keyboard-driven selector for resumable sessions."""

    def __init__(
        self,
        console: Console,
        *,
        loader: SessionLoader,
        initial_page: int = 1,
        page_size: int | None = None,
    ) -> None:
        if initial_page < 1:
            raise ValueError("initial_page must be at least 1")
        if page_size is not None and page_size < 1:
            raise ValueError("page_size must be at least 1")
        self.console = console
        self.loader = loader
        self._configured_page_size = page_size
        self._records: list[SessionRecord] = []
        self.selected = 0
        self.help_visible = False
        self.last_refresh = 0.0
        self.initial_page = initial_page
        self.refresh(force=True)
        if self._records:
            self.selected = min((initial_page - 1) * self.page_size, len(self._records) - 1)

    @property
    def page_size(self) -> int:
        """Return a viewport-sized row count, or the explicit test/CLI size."""

        if self._configured_page_size is not None:
            return max(1, self._configured_page_size)
        try:
            height = int(self.console.height)
        except (AttributeError, TypeError, ValueError):
            height = 25
        return max(1, min(12, height - 8))

    @property
    def page_count(self) -> int:
        return max(1, (len(self._records) + self.page_size - 1) // self.page_size)

    @property
    def selected_record(self) -> SessionRecord | None:
        if not self._records:
            return None
        self.selected = min(max(self.selected, 0), len(self._records) - 1)
        return self._records[self.selected]

    def refresh(self, *, force: bool = False) -> list[SessionRecord]:
        """Reload the index while retaining the selected session when possible."""

        if not force and time.monotonic() - self.last_refresh < 0.5:
            return list(self._records)
        previous = self.selected_record
        prior_id = previous[0] if previous is not None else None
        self._records = list(self.loader())
        self.last_refresh = time.monotonic()
        if prior_id is not None:
            self.selected = next(
                (
                    index
                    for index, (session_id, _record) in enumerate(self._records)
                    if session_id == prior_id
                ),
                min(self.selected, max(0, len(self._records) - 1)),
            )
        else:
            self.selected = min(self.selected, max(0, len(self._records) - 1))
        return list(self._records)

    def _page_bounds(self) -> tuple[int, int]:
        start = (self.selected // self.page_size) * self.page_size
        return start, min(len(self._records), start + self.page_size)

    def render(self, *, all_records: bool = False) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from agenthicc.runners.session_lease import (  # noqa: PLC0415
            SessionLeaseInspection,
            SessionOpenCoordinator,
        )

        owner_coordinator = SessionOpenCoordinator()
        self.refresh()
        if self.help_visible:
            return Panel(
                "↑/k previous   ↓/j next   PageUp/PageDown page\n"
                "Home/End first/last   Enter resume and open transcript\n"
                "r refresh   ? close help   q/Esc quit",
                title="Saved Sessions — Keyboard Help",
                border_style="cyan",
            )

        start, end = self._page_bounds()
        records = self._records if all_records else self._records[start:end]
        page_label = "all" if all_records else f"{start // self.page_size + 1}/{self.page_count}"
        table = Table(title=f"Saved Sessions (page {page_label})", expand=True)
        table.add_column("", width=2)
        table.add_column("Session ID", no_wrap=True)
        table.add_column("Last used", no_wrap=True)
        table.add_column("Project")
        table.add_column("Model")
        table.add_column("Owner", no_wrap=True)
        if not records:
            table.add_row("", "—", "", "No saved sessions", "", "")
        for local_index, (session_id, data) in enumerate(records):
            index = local_index if all_records else start + local_index
            cwd_value = data.get("cwd")
            model_value = data.get("model")
            cwd = cwd_value if isinstance(cwd_value, str) else ""
            model = model_value if isinstance(model_value, str) else ""
            try:
                owner = owner_coordinator.inspect(session_id)
            except ValueError:
                # Legacy project-local indexes may contain an identifier that
                # the durable owner namespace deliberately rejects.  The row
                # remains visible, but it cannot be safely classified or
                # opened through the ownership boundary.
                owner = SessionLeaseInspection(
                    session_id=str(session_id),
                    state="unknown",
                    reason="invalid_session_id",
                )
            marker = "▸" if index == self.selected else " "
            if cwd == "":
                cwd = "—"
            table.add_row(
                marker,
                session_id[:16],
                time.strftime(
                    "%Y-%m-%d %H:%M",
                    time.localtime(_timestamp(data.get("last_used", data.get("last_active")))),
                ),
                cwd,
                model or "—",
                owner.state,
            )

        detail_lines: list[str] = []
        selected = self.selected_record
        if selected is not None:
            session_id, data = selected
            detail_lines = [
                f"[bold]Session[/bold] {session_id}",
                f"[bold]Project[/bold] {data.get('cwd') or '—'}",
                f"[bold]Transcript[/bold] {data.get('log_path') or 'available on resume'}",
                "Press Enter to resume this session and load its transcript.",
            ]
        detail = Panel(
            "\n".join(detail_lines) or "Select a session to inspect it.", title="Details"
        )
        footer = Text(
            "↑/k ↓/j select  PgUp/PgDn page  Home/End  Enter resume  r refresh  ? help  q quit",
            style="dim",
        )
        if not all_records and self._records:
            footer.append(f"  • page {start // self.page_size + 1}/{self.page_count}", style="cyan")
        return Group(table, detail, footer)

    def handle_key(self, key: object, ch: str = "") -> SessionManagerResult | None:
        """Handle one logical terminal key."""

        value = _key_value(key)
        if value in {"UP", "CHAR"} and (value == "UP" or ch.lower() == "k"):
            self.refresh()
            self.selected = max(0, self.selected - 1)
            return None
        if value in {"DOWN", "CHAR"} and (value == "DOWN" or ch.lower() == "j"):
            self.refresh()
            self.selected = min(max(0, len(self._records) - 1), self.selected + 1)
            return None
        if value in {"PAGE_UP", "PAGEUP"}:
            self.refresh()
            self.selected = max(0, self.selected - self.page_size)
            return None
        if value in {"PAGE_DOWN", "PAGEDOWN"}:
            self.refresh()
            self.selected = min(max(0, len(self._records) - 1), self.selected + self.page_size)
            return None
        if value == "HOME":
            self.refresh()
            self.selected = 0
            return None
        if value == "END":
            self.refresh()
            self.selected = max(0, len(self._records) - 1)
            return None
        if value == "ENTER":
            selected = self.selected_record
            return SessionManagerResult("open", selected[0]) if selected is not None else None
        if ch.lower() == "r":
            self.refresh(force=True)
            return None
        if ch == "?":
            self.help_visible = not self.help_visible
            return None
        if value == "ESC" or ch.lower() == "q":
            return SessionManagerResult("exit")
        return None

    async def run(self) -> SessionManagerResult:
        """Run the selector until the user opens a session or exits."""

        from agenthicc.tui.terminal.backend import get_backend  # noqa: PLC0415
        from rich.live import Live  # noqa: PLC0415

        backend = get_backend()
        if not backend.is_interactive():
            self.console.print(self.render(all_records=True))
            return SessionManagerResult("exit")
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


async def run_session_manager(
    console: Console,
    *,
    initial_page: int = 1,
    page_size: int | None = None,
) -> SessionManagerResult:
    """Open the saved-session selector used by ``sessions list``."""

    from agenthicc.sessions import _ordered_session_records  # noqa: PLC0415

    manager = SessionManager(
        console,
        loader=_ordered_session_records,
        initial_page=initial_page,
        page_size=page_size,
    )
    return await manager.run()
