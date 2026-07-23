"""Interactive manager for durable background sessions (PRD-141)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)

if TYPE_CHECKING:
    from rich.console import Console, RenderableType


@dataclass(frozen=True)
class ManagerResult:
    """Result returned when the manager loop yields control to its caller."""

    action: str
    session_id: str | None = None


def _key_value(key: object) -> str:
    value = getattr(key, "value", key)
    return str(value)


class BackgroundManager:
    """Rich-rendered, keyboard-driven background session control surface."""

    def __init__(
        self,
        console: Console,
        *,
        store: BackgroundStore | None = None,
        supervisor: BackgroundSupervisor | None = None,
        refresh_s: float = 1.0,
        input_provider: Callable[[str], str] | None = None,
    ) -> None:
        self.console = console
        self.store = store or BackgroundStore()
        self.supervisor = supervisor or BackgroundSupervisor(self.store)
        self.refresh_s = max(0.1, refresh_s)
        self.input_provider = input_provider
        self.selected = 0
        self.query = ""
        self.include_archived = True
        self.include_deleted = False
        self.status_filter: SessionStatus | None = None
        self.project_filter: str | None = None
        self.workflow_filter: str | None = None
        self.paused = False
        self.new_activity = False
        self.help_visible = False
        self.pending_delete = False
        self.pending_delete_ids: tuple[str, ...] = ()
        self.marked_ids: set[str] = set()
        self.filter_mode = False
        self.filter_buffer = ""
        self.last_refresh = 0.0
        self._sessions: list[BackgroundSession] = []
        self._seen_activity: dict[str, float] = {}
        self.activity_offset = 0

    @property
    def sessions(self) -> list[BackgroundSession]:
        self.refresh()
        return list(self._sessions)

    @property
    def selected_session(self) -> BackgroundSession | None:
        sessions = self.sessions
        if not sessions:
            return None
        self.selected = min(max(self.selected, 0), len(sessions) - 1)
        return sessions[self.selected]

    def refresh(self, *, force: bool = False) -> list[BackgroundSession]:
        if self.paused and not force:
            return self._sessions
        if force or time.monotonic() - self.last_refresh >= self.refresh_s:
            recover = getattr(self.supervisor, "recover_stale", None)
            if callable(recover):
                try:
                    recover()
                except (OSError, RuntimeError, ValueError):
                    pass
            previous = {item.session_id: item.last_active for item in self._sessions}
            self._sessions = self.store.list(
                include_archived=self.include_archived,
                include_deleted=self.include_deleted,
                cwd=self.project_filter,
                workflow_name=self.workflow_filter,
                query=self.query,
                status=self.status_filter,
            )
            for item in self._sessions:
                self._seen_activity.setdefault(item.session_id, item.last_active)
                if (
                    item.session_id in previous
                    and item.last_active > self._seen_activity[item.session_id]
                ):
                    self.new_activity = True
            self.selected = min(self.selected, max(0, len(self._sessions) - 1))
            self.last_refresh = time.monotonic()
        return self._sessions

    def set_query(self, query: str) -> None:
        self.query = query.strip()
        self.selected = 0
        self.refresh(force=True)

    def set_input_provider(self, provider: Callable[[str], str] | None) -> None:
        """Set the local prompt used by the ``i`` manager action."""

        self.input_provider = provider

    def toggle_mark_selected(self) -> None:
        selected = self.selected_session
        if selected is None:
            return
        if selected.session_id in self.marked_ids:
            self.marked_ids.remove(selected.session_id)
        else:
            self.marked_ids.add(selected.session_id)

    def marked_sessions(self) -> list[BackgroundSession]:
        return [item for item in self.sessions if item.session_id in self.marked_ids]

    def _bulk_action(self, action: str) -> None:
        """Apply a safe bulk action to marked records and refresh once."""

        records = self.marked_sessions()
        if not records:
            return
        operation = getattr(self.supervisor, action)
        for record in records:
            try:
                operation(record.session_id)
            except Exception as exc:  # noqa: BLE001
                self.console.print(f"{action} failed: {type(exc).__name__}: {exc}")
        self.marked_ids.clear()
        self.refresh(force=True)

    def bulk_cancel(self) -> None:
        """Cancel all marked sessions, preserving per-session errors."""

        self._bulk_action("cancel")

    def bulk_archive(self) -> None:
        """Archive all marked terminal sessions, preserving per-session errors."""

        self._bulk_action("archive")

    def set_filters(
        self,
        *,
        status: SessionStatus | None = None,
        project: str | None = None,
        workflow: str | None = None,
    ) -> None:
        """Set deterministic manager filters without changing worker state."""

        self.status_filter = status
        self.project_filter = project
        self.workflow_filter = workflow
        self.selected = 0
        self.refresh(force=True)

    def mark_selected_seen(self) -> None:
        selected = self.selected_session
        if selected is not None:
            self._seen_activity[selected.session_id] = selected.last_active
            self.new_activity = any(
                item.last_active > self._seen_activity.get(item.session_id, 0.0)
                for item in self._sessions
            )

    def _activity_lines(self, session: BackgroundSession) -> list[str]:
        """Return bounded, redacted event summaries from the canonical journal."""

        from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

        path = Path(session.artifact_dir).expanduser() / "conversation.jsonl"
        if not path.exists():
            return []
        try:
            raw_lines = path.read_bytes()[-64_000:].decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        redactor = _Redactor()
        summaries: list[str] = []
        for line in raw_lines[-12:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            kind = str(record.get("kind", "event"))
            payload = record.get("payload")
            if isinstance(payload, dict):
                text = payload.get("text") or payload.get("summary") or payload.get("message")
            else:
                text = None
            detail = redactor.value(text, "text") if isinstance(text, str) else ""
            summaries.append(f"{kind}: {str(detail)[:120]}" if detail else kind)
        end = max(0, len(summaries) - self.activity_offset)
        return summaries[max(0, end - 12) : end]

    def _status_style(self, status: SessionStatus) -> str:
        return {
            SessionStatus.RUNNING: "green",
            SessionStatus.WAITING_APPROVAL: "yellow",
            SessionStatus.WAITING_INPUT: "yellow",
            SessionStatus.FAILED: "red",
            SessionStatus.ORPHANED: "red",
            SessionStatus.COMPLETED: "cyan",
            SessionStatus.CANCELLED: "dim",
            SessionStatus.ARCHIVED: "dim",
        }.get(status, "white")

    def render(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        sessions = self.refresh()
        if self.help_visible:
            return Panel(
                "↑/k previous   ↓/j next   Enter attach/follow   r refresh\n"
                "c cancel   a archive   Ctrl+X delete   u restore   t trash\n"
                "/ filter   v mark   C/A bulk cancel/archive   i input\n"
                "PageUp/[ and PageDown/] scroll transcript\n"
                "p pin   Space pause refresh   q quit   ? close help",
                title="Background Sessions — Keyboard Help",
                border_style="cyan",
            )
        table = Table(title="Background Sessions", expand=True)
        table.add_column("", width=2)
        table.add_column("State", no_wrap=True)
        table.add_column("Title")
        table.add_column("Workflow")
        table.add_column("Project")
        table.add_column("Activity")
        if not sessions:
            table.add_row(
                "",
                "—",
                "No background sessions",
                "",
                "",
                "Start one with agenthicc run --background",
            )
        for index, session in enumerate(sessions):
            marker = "▸" if index == self.selected else " "
            if session.last_active > self._seen_activity.get(
                session.session_id, session.last_active
            ):
                marker = "●" if marker == " " else "◆"
            title = session.title
            if session.pinned:
                title = "★ " + title
            if session.session_id in self.marked_ids:
                title = "☑ " + title
            if session.error:
                activity = session.error[:80]
            else:
                activity = session.latest_activity[:80]
            table.add_row(
                marker,
                f"[{self._status_style(session.status)}]{session.status.value}[/]",
                title[:50],
                session.workflow_name or "direct",
                session.cwd,
                activity,
            )
        selected = self.selected_session
        detail_lines: list[str] = []
        if selected is not None:
            detail_lines.extend(
                [
                    f"[bold]ID[/bold] {selected.session_id}",
                    f"[bold]State[/bold] [{self._status_style(selected.status)}]{selected.status.value}[/]",
                    f"[bold]Phase[/bold] {selected.current_phase or '—'}",
                    f"[bold]Updated[/bold] {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(selected.last_active))}",
                ]
            )
            if selected.error:
                detail_lines.append(f"[red]Error[/red] {selected.error[:180]}")
            if selected.phase_history:
                detail_lines.append(
                    "[bold]Phase history[/bold] "
                    + " → ".join(selected.phase_history[-16:])
                )
            if (
                selected.session_id in self._seen_activity
                and selected.last_active > self._seen_activity[selected.session_id]
            ):
                detail_lines.append("[yellow]New activity available[/yellow]")
            activity_lines = self._activity_lines(selected)
            if activity_lines:
                detail_lines.append("[bold]Recent activity[/bold]")
                detail_lines.extend(activity_lines)
                if self.activity_offset:
                    detail_lines.append("[dim]Transcript offset: {}[/dim]".format(self.activity_offset))
        if self.pending_delete:
            pending_titles = [
                item.title
                for item in sessions
                if item.session_id
                in (self.pending_delete_ids or (selected.session_id if selected else "",))
            ]
            detail_lines.append(
                "[bold yellow]Delete "
                f"{len(self.pending_delete_ids) or 1} session(s)"
                f" ({', '.join(pending_titles)[:160]})? Press y/Enter to confirm, n/Esc to cancel.[/bold yellow]"
            )
        detail = Panel(
            "\n".join(detail_lines) or "Select a session to inspect it.", title="Details"
        )
        footer = Text(
            "Enter follow  c cancel  v mark  Ctrl+X delete  / filter  i input  ? help  q quit",
            style="dim",
        )
        if self.new_activity:
            footer.append("  • new activity", style="yellow")
        return Group(table, detail, footer)

    def _confirm_delete(self, session: BackgroundSession) -> ManagerResult:
        ids = self.pending_delete_ids or (session.session_id,)
        try:
            for session_id in ids:
                self.supervisor.delete(session_id)
        except Exception as exc:  # noqa: BLE001
            self.pending_delete = False
            self.pending_delete_ids = ()
            self.console.print(f"Delete failed: {type(exc).__name__}: {exc}")
            return ManagerResult("error", session.session_id)
        self.pending_delete = False
        self.pending_delete_ids = ()
        self.marked_ids.difference_update(ids)
        self.refresh(force=True)
        return ManagerResult("deleted", session.session_id)

    def handle_key(self, key: object, ch: str = "") -> ManagerResult | None:
        """Handle one logical terminal key; exposed for deterministic TUI tests."""

        value = _key_value(key)
        if self.filter_mode:
            if value == "ESC":
                self.filter_mode = False
                self.filter_buffer = ""
                return None
            if value == "ENTER":
                self.filter_mode = False
                self.set_query(self.filter_buffer)
                self.filter_buffer = ""
                return None
            if value == "BACKSPACE" or ch == "\x7f":
                self.filter_buffer = self.filter_buffer[:-1]
                return None
            if value == "CHAR" and ch:
                self.filter_buffer += ch
            return None
        if self.pending_delete:
            if value == "ESC" or ch.lower() == "n":
                self.pending_delete = False
                return None
            if value == "ENTER" or ch.lower() == "y":
                selected = self.selected_session
                return self._confirm_delete(selected) if selected is not None else None
            return None
        if value in {"UP", "CHAR"} and (value == "UP" or ch.lower() == "k"):
            self.refresh()
            self.selected = max(0, self.selected - 1)
            return None
        if value in {"DOWN", "CHAR"} and (value == "DOWN" or ch.lower() == "j"):
            self.refresh()
            self.selected = min(max(0, len(self._sessions) - 1), self.selected + 1)
            return None
        if value == "CTRL_X" or ch == "\x18":
            selected = self.selected_session
            marked = self.marked_sessions()
            if marked or selected is not None:
                self.pending_delete = True
                targets = marked if marked else ([selected] if selected is not None else [])
                self.pending_delete_ids = tuple(item.session_id for item in targets)
            return None
        if value == "ENTER":
            selected = self.selected_session
            self.mark_selected_seen()
            return ManagerResult("attach", selected.session_id) if selected is not None else None
        if value in {"PAGE_UP", "PAGEUP"} or ch == "[":
            self.activity_offset += 6
            return None
        if value in {"PAGE_DOWN", "PAGEDOWN"} or ch == "]":
            self.activity_offset = max(0, self.activity_offset - 6)
            return None
        if value == "ESC" or ch.lower() == "q":
            return ManagerResult("exit")
        if ch == "?":
            self.help_visible = not self.help_visible
            return None
        if ch.lower() == "r":
            self.refresh(force=True)
            return None
        if ch == "/":
            self.filter_mode = True
            self.filter_buffer = self.query
            return None
        if ch.lower() == "v" or ch.lower() == "m":
            self.toggle_mark_selected()
            return None
        if ch == "C":
            self.bulk_cancel()
            return None
        if ch == "A":
            self.bulk_archive()
            return None
        if value == "SPACE" or ch == " ":
            self.paused = not self.paused
            return None
        if ch.lower() == "t":
            self.include_deleted = not self.include_deleted
            self.refresh(force=True)
            return None
        selected = self.selected_session
        if selected is None:
            return None
        if ch.lower() in {"y", "n"} and selected.status == SessionStatus.WAITING_APPROVAL:
            try:
                self.supervisor.approve(selected.session_id, ch.lower() == "y")
            except Exception as exc:  # noqa: BLE001
                self.console.print(f"Approval failed: {type(exc).__name__}: {exc}")
            self.refresh(force=True)
            return None
        if ch.lower() == "i" and selected.status == SessionStatus.WAITING_INPUT:
            if self.input_provider is None:
                self.console.print(
                    "Provide input with: agenthicc jobs input " + selected.session_id
                )
            else:
                try:
                    self.supervisor.provide_input(
                        selected.session_id, self.input_provider(selected.input_request)
                    )
                except Exception as exc:  # noqa: BLE001
                    self.console.print(f"Input failed: {type(exc).__name__}: {exc}")
            self.refresh(force=True)
            return None
        if ch.lower() == "c":
            try:
                self.supervisor.cancel(selected.session_id)
            except Exception as exc:  # noqa: BLE001
                self.console.print(f"Cancel failed: {type(exc).__name__}: {exc}")
            self.refresh(force=True)
        elif ch.lower() == "a":
            try:
                self.supervisor.archive(selected.session_id)
            except Exception as exc:  # noqa: BLE001
                self.console.print(f"Archive failed: {type(exc).__name__}: {exc}")
            self.refresh(force=True)
        elif ch.lower() == "u":
            try:
                self.supervisor.restore_deleted(selected.session_id)
            except Exception:
                return None
            self.refresh(force=True)
        elif ch.lower() == "p":
            try:
                self.store.update(selected.session_id, pinned=not selected.pinned)
            except Exception:
                return None
            self.refresh(force=True)
        return None

    async def run(self) -> ManagerResult:
        """Run the manager until quit, attach, or a non-interactive fallback."""

        from agenthicc.tui.terminal.backend import get_backend  # noqa: PLC0415
        from rich.live import Live  # noqa: PLC0415

        backend = get_backend()
        if not backend.is_interactive():
            self.console.print(self.render())
            return ManagerResult("exit")
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


async def run_background_manager(
    console: Console,
    *,
    store: BackgroundStore | None = None,
    supervisor: BackgroundSupervisor | None = None,
) -> ManagerResult:
    """Convenience entry point used by CLI aliases and tests."""

    return await BackgroundManager(console, store=store, supervisor=supervisor).run()
