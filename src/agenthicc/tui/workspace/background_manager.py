"""Interactive manager for durable background sessions (PRD-141)."""

from __future__ import annotations

import asyncio
import json
import threading
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


@dataclass(frozen=True)
class ViewportBudget:
    """Bounded vertical layout calculated from the current terminal size.

    The manager deliberately budgets the complete ``Group`` rather than
    budgeting table rows in isolation.  Rich panels and table headers consume
    vertical space even when the underlying session list is large.
    """

    width: int
    height: int
    table_rows: int
    detail_height: int
    compact: bool = False

    @classmethod
    def from_terminal(
        cls,
        width: int,
        height: int,
        configured_page_size: int | None = None,
    ) -> "ViewportBudget":
        width = max(1, int(width))
        height = max(1, int(height))
        # For genuinely tiny terminals a four-line compact projection is the
        # only honest way to retain both the selected identity and controls.
        if height <= 6:
            return cls(width, height, 1, 1, compact=True)

        detail_height = 3 if height <= 10 else (4 if height <= 14 else 6)
        # Header=1, table header/bottom=2, details panel, footer=1.
        available = max(1, height - 1 - 2 - detail_height - 1)
        if configured_page_size is not None:
            available = min(available, max(1, configured_page_size))
        return cls(width, height, available, detail_height)


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
        page_size: int | None = None,
    ) -> None:
        self.console = console
        self.store = store or BackgroundStore()
        self.supervisor = supervisor or BackgroundSupervisor(self.store)
        self.refresh_s = max(0.1, refresh_s)
        self.input_provider = input_provider
        self._configured_page_size = page_size
        self._selected_index = 0
        self._selected_session_id: str | None = None
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
        self._interactive_delete = False
        self._deletion_thread: threading.Thread | None = None
        self._deleting_ids: tuple[str, ...] = ()
        self._deletion_result: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        self.marked_ids: set[str] = set()
        self.filter_mode = False
        self.filter_buffer = ""
        self.last_refresh = 0.0
        self._sessions: list[BackgroundSession] = []
        self._seen_activity: dict[str, float] = {}
        self.activity_offset = 0
        self._activity_cache: dict[str, tuple[tuple[int, int, int], str | None]] = {}
        self._last_maintenance = 0.0
        self.maintenance_s = max(1.0, self.refresh_s)
        self._last_render_key: tuple[object, ...] | None = None
        self._last_renderable: RenderableType | None = None

    @property
    def selected(self) -> int:
        """Compatibility index view; actions are internally ID-targeted."""

        if self._selected_session_id:
            for index, session in enumerate(self._sessions):
                if session.session_id == self._selected_session_id:
                    self._selected_index = index
                    break
        if not self._sessions:
            return 0
        return min(max(self._selected_index, 0), len(self._sessions) - 1)

    @selected.setter
    def selected(self, value: int) -> None:
        self._selected_index = max(0, int(value))
        if self._sessions:
            index = min(self._selected_index, len(self._sessions) - 1)
            self._selected_index = index
            self._selected_session_id = self._sessions[index].session_id

    @property
    def selected_session_id(self) -> str | None:
        """Return the durable identity currently targeted by the UI."""

        _ = self.selected
        return self._selected_session_id

    @property
    def viewport_budget(self) -> ViewportBudget:
        try:
            width = int(self.console.width)
            height = int(self.console.height)
        except (AttributeError, TypeError, ValueError):
            width, height = 80, 25
        return ViewportBudget.from_terminal(width, height, self._configured_page_size)

    @property
    def page_size(self) -> int:
        """Return the number of session rows that fit on one manager page.

        The manager also renders a detail panel and a footer, so reserve space
        for those persistent sections.  Tests and embedding callers may pass a
        fixed value to make pagination deterministic.
        """

        return self.viewport_budget.table_rows

    @property
    def page_count(self) -> int:
        """Return the number of pages in the currently filtered session list."""

        count = len(self._sessions)
        return max(1, (count + self.page_size - 1) // self.page_size)

    def _page_bounds(self) -> tuple[int, int]:
        """Return the half-open slice for the page containing the selection."""

        page = self.selected // self.page_size
        start = page * self.page_size
        return start, min(len(self._sessions), start + self.page_size)

    @property
    def sessions(self) -> list[BackgroundSession]:
        self.refresh()
        return list(self._sessions)

    @property
    def selected_session(self) -> BackgroundSession | None:
        # Actions can be invoked immediately after construction, before the
        # first render has populated the snapshot.  This one bootstrap read is
        # still cached and does not make render itself impure.
        if not self._sessions and not self.paused:
            self.refresh(force=True)
        if not self._sessions:
            return None
        index = self.selected
        return self._sessions[index]

    def maintain(self, *, force: bool = False) -> list[BackgroundSession]:
        """Run stale-worker maintenance outside the render hot path.

        Recovery can inspect processes and append lifecycle events.  It is
        therefore intentionally explicit and rate-limited; ``render`` never
        calls it.  A caller may use ``force=True`` for an operator-requested
        refresh.
        """

        now = time.monotonic()
        if not force and now - self._last_maintenance < self.maintenance_s:
            return []
        self._last_maintenance = now
        recover = getattr(self.supervisor, "recover_stale", None)
        if not callable(recover):
            return []
        try:
            changed = recover()
        except (OSError, RuntimeError, ValueError):
            return []
        self.refresh(force=True)
        return list(changed) if isinstance(changed, list) else []

    def _reconcile_selection(self, previous_id: str | None, previous_index: int) -> None:
        if not self._sessions:
            self._selected_session_id = None
            self._selected_index = 0
            return
        if previous_id:
            for index, session in enumerate(self._sessions):
                if session.session_id == previous_id:
                    self._selected_index = index
                    self._selected_session_id = previous_id
                    return
        index = min(max(previous_index, 0), len(self._sessions) - 1)
        self._selected_index = index
        self._selected_session_id = self._sessions[index].session_id

    def refresh(self, *, force: bool = False) -> list[BackgroundSession]:
        if self.paused and not force:
            return self._sessions
        if force or time.monotonic() - self.last_refresh >= self.refresh_s:
            previous_id = self._selected_session_id
            previous_index = self.selected
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
            self._reconcile_selection(previous_id, previous_index)
            self.last_refresh = time.monotonic()
        return self._sessions

    def set_query(self, query: str) -> None:
        self.query = query.strip()
        self._selected_session_id = None
        self._selected_index = 0
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
        self._selected_session_id = None
        self._selected_index = 0
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
        """Return the newest bounded, redacted text event from the journal.

        Lifecycle and tool events are intentionally excluded.  The journal is
        append-only and can be very large, so only its bounded tail is read;
        a size/mtime/inode fingerprint avoids rereading it during idle Rich
        repaints.
        """

        from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

        path = Path(session.artifact_dir).expanduser() / "conversation.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return []
        fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = self._activity_cache.get(session.session_id)
        if cached is not None and cached[0] == fingerprint:
            return [cached[1]] if cached[1] is not None else []
        try:
            raw_lines = path.read_bytes()[-64_000:].decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        redactor = _Redactor()
        latest: str | None = None
        # ``assistant_message`` is retained for older journals; new journals
        # use ``text`` and ``user_message``.  No summary/message fallback is
        # used because those fields commonly contain tool/lifecycle noise.
        text_kinds = {"text", "user_message", "assistant_message"}
        for line in reversed(raw_lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            kind = str(record.get("kind", "event"))
            if kind not in text_kinds:
                continue
            payload = record.get("payload")
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            detail = redactor.value(text, "text")
            latest = f"{kind}: {str(detail)[:512]}"
            break
        self._activity_cache[session.session_id] = (fingerprint, latest)
        return [latest] if latest is not None else []

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

    def _activity_fingerprint(self, session: BackgroundSession) -> tuple[int, int, int]:
        path = Path(session.artifact_dir).expanduser() / "conversation.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return (0, 0, 0)
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    @staticmethod
    def _workspace_name(cwd: str) -> str:
        """Return only the workspace directory name for compact UI display."""

        path = Path(cwd)
        return path.name or path.anchor or cwd

    def _render_key(
        self,
        *,
        all_sessions: bool,
        budget: ViewportBudget,
        start: int,
        end: int,
        selected: BackgroundSession | None,
    ) -> tuple[object, ...]:
        visible = self._sessions if all_sessions else self._sessions[start:end]
        session_key = tuple(
            (
                item.session_id,
                item.status.value,
                item.last_active,
                item.current_phase,
                item.error,
                item.latest_activity,
                item.pinned,
            )
            for item in visible
        )
        return (
            all_sessions,
            budget,
            start,
            end,
            self._selected_session_id,
            session_key,
            self.query,
            self.help_visible,
            self.pending_delete,
            self.pending_delete_ids,
            self._deleting_ids,
            tuple(sorted(self.marked_ids)),
            self.new_activity,
            self.activity_offset,
            self._activity_fingerprint(selected) if selected is not None else None,
        )

    def render(self, *, all_sessions: bool = False) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from rich import box  # noqa: PLC0415

        all_records = self.refresh()
        budget = self.viewport_budget
        if self.help_visible:
            help_text = (
                "↑/k ↓/j select   PgUp/PgDn page   Home/End first/last\n"
                "Enter attach exact session   r refresh   ? close help\n"
                "c cancel   a archive   Ctrl+X delete   u restore   t trash\n"
                "/ filter   v mark   C/A bulk action   i input   q/Esc quit"
            )
            if budget.compact:
                return Text("? help  ↑↓ select  Enter attach  q quit")
            return Panel(
                help_text,
                title="Background Sessions — Keyboard Help",
                border_style="cyan",
                height=min(budget.height, 8),
            )
        start, end = self._page_bounds()
        sessions = all_records if all_sessions else all_records[start:end]
        page_number = start // self.page_size + 1
        if all_sessions:
            page_label = f"all · {len(all_records)} sessions"
            range_label = f"showing 1–{len(all_records)} of {len(all_records)}"
        elif all_records:
            range_label = f"showing {start + 1}–{end} of {len(all_records)}"
            page_label = f"page {page_number}/{self.page_count} · {range_label}"
        else:
            range_label = "showing 0–0 of 0"
            page_label = f"page 1/1 · {range_label}"

        selected = self.selected_session
        key = self._render_key(
            all_sessions=all_sessions,
            budget=budget,
            start=start,
            end=end,
            selected=selected,
        )
        if key == self._last_render_key and self._last_renderable is not None:
            return self._last_renderable

        if budget.compact:
            compact_title = Text(f"Background Sessions · {page_label}", style="bold cyan")
            if selected is None:
                row = Text("No background sessions")
                compact_detail = Text("No selected session")
            else:
                row = Text(
                    f"▸ {selected.status.value}  {selected.title[: max(1, budget.width - 24)]}"
                )
                compact_detail = Text(f"{selected.session_id}  {selected.current_phase or '—'}")
            footer = Text("↑↓ select  Enter attach  ? help  q/Esc quit", style="dim")
            rendered: RenderableType = Group(compact_title, row, compact_detail, footer)
            self._last_render_key, self._last_renderable = key, rendered
            return rendered

        header = Text(f"Background Sessions · {page_label}", style="bold cyan")
        table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), show_edge=False)
        table.add_column("", width=2)
        table.add_column("State", no_wrap=True, overflow="ellipsis")
        table.add_column("Title", no_wrap=True, overflow="ellipsis")
        table.add_column("Workflow", no_wrap=True, overflow="ellipsis")
        table.add_column("WS", no_wrap=True, overflow="ellipsis")
        table.add_column("Activity", no_wrap=True, overflow="ellipsis")
        if not sessions:
            table.add_row(
                "",
                "—",
                "No background sessions",
                "",
                "",
                "Start one with agenthicc run --background",
            )
        for local_index, session in enumerate(sessions):
            marker = "▸" if session.session_id == self._selected_session_id else " "
            if session.last_active > self._seen_activity.get(
                session.session_id, session.last_active
            ):
                marker = "●" if marker == " " else "◆"
            title = session.title
            if session.pinned:
                title = "★ " + title
            if session.session_id in self.marked_ids:
                title = "☑ " + title
            if session.session_id == self._selected_session_id:
                # Keep selection visible even when Rich compresses the narrow
                # marker column on a small terminal.
                title = "▶ " + title
            if session.error:
                activity = session.error[:80]
            else:
                activity = session.latest_activity[:80]
            workspace = self._workspace_name(session.cwd)
            table.add_row(
                marker,
                f"[{self._status_style(session.status)}]{session.status.value}[/]",
                title[:50],
                session.workflow_name or "direct",
                workspace,
                activity,
            )
        detail_lines: list[str] = []
        if selected is not None:
            detail_lines.extend(
                [
                    f"[bold]ID[/bold] {selected.session_id}",
                    f"[bold]Dir:[/bold] {self._workspace_name(selected.cwd)}",
                    f"[bold]State[/bold] [{self._status_style(selected.status)}]{selected.status.value}[/]"
                    f"  [bold]Phase[/bold] {selected.current_phase or '—'}",
                ]
            )
            if selected.error:
                detail_lines.append(f"[red]Error[/red] {selected.error[:180]}")
            else:
                activity_lines = self._activity_lines(selected)
                if activity_lines:
                    detail_lines.append("[bold]Latest text[/bold] " + activity_lines[0])
                else:
                    detail_lines.append("[dim]Latest text: no text activity[/dim]")

            # Optional context is added only when the panel has room.  The
            # required identity/state/phase/activity lines always win over
            # history or timestamps, so a short viewport cannot hide the
            # useful status behind an overflowed details panel.
            content_capacity = max(1, budget.detail_height - 2)
            if selected.phase_history and len(detail_lines) < content_capacity:
                detail_lines.append(
                    "[bold]History[/bold] " + " → ".join(selected.phase_history[-4:])
                )
            if len(detail_lines) < content_capacity:
                detail_lines.append(
                    "[bold]Updated[/bold] "
                    + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(selected.last_active))
                )
            if (
                selected.session_id in self._seen_activity
                and selected.last_active > self._seen_activity[selected.session_id]
                and len(detail_lines) < content_capacity
            ):
                detail_lines.append("[yellow]New activity available[/yellow]")
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
        if self._deleting_ids:
            detail_lines.append(
                f"[yellow]Deleting {len(self._deleting_ids)} session(s) in the background…[/yellow]"
            )
        detail = Panel(
            "\n".join(detail_lines) or "Select a session to inspect it.",
            title="Details · selected" if selected is not None else "Details",
            height=budget.detail_height,
            padding=(0, 1),
            expand=True,
        )
        footer = Text(
            "↑/k ↓/j select  PgUp/PgDn page  Home/End  Enter attach  r refresh  "
            "c cancel  v mark  Ctrl+X delete  ? help  q quit",
            style="dim",
        )
        if self.new_activity:
            footer.append("  • new activity", style="yellow")
        rendered = Group(header, table, detail, footer)
        self._last_render_key, self._last_renderable = key, rendered
        return rendered

    def _delete_sessions(self, ids: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        deleted: list[str] = []
        errors: list[str] = []
        for session_id in ids:
            try:
                self.supervisor.delete(session_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{session_id}: {type(exc).__name__}: {exc}")
            else:
                deleted.append(session_id)
        return tuple(deleted), tuple(errors)

    def _start_async_delete(self, ids: tuple[str, ...]) -> None:
        self._deleting_ids = ids
        self._deletion_result = None

        def run_delete() -> None:
            self._deletion_result = self._delete_sessions(ids)

        self._deletion_thread = threading.Thread(
            target=run_delete,
            name="agenthicc-background-delete",
            daemon=True,
        )
        self._deletion_thread.start()

    def _poll_async_delete(self) -> None:
        thread = self._deletion_thread
        if thread is None or thread.is_alive():
            return
        thread.join()
        deleted, errors = self._deletion_result or ((), ("delete worker stopped unexpectedly",))
        self._deletion_thread = None
        self._deletion_result = None
        self._deleting_ids = ()
        self.marked_ids.difference_update(deleted)
        if errors:
            self.console.print("Delete failed: " + "; ".join(errors)[:2_000])
        self.refresh(force=True)

    def _confirm_delete(self, session: BackgroundSession) -> ManagerResult | None:
        ids = self.pending_delete_ids or (session.session_id,)
        if self._interactive_delete:
            self.pending_delete = False
            self.pending_delete_ids = ()
            self._start_async_delete(ids)
            return None
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
        # The raw terminal backend delivers Ctrl+C as CTRL_C (signals are
        # disabled while the manager owns cbreak mode).  Handle it before any
        # modal state so it always exits the agents screen, including while a
        # filter or delete confirmation is visible.
        if value == "CTRL_C" or ch == "\x03":
            return ManagerResult("exit")
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
        if value == "HOME":
            self.refresh()
            self.selected = 0
            self.activity_offset = 0
            return None
        if value == "END":
            self.refresh()
            self.selected = max(0, len(self._sessions) - 1)
            self.activity_offset = 0
            return None
        if value == "CTRL_X" or ch == "\x18":
            if self._deleting_ids:
                return None
            selected = self.selected_session
            marked = self.marked_sessions()
            if marked or selected is not None:
                self.pending_delete = True
                targets = marked if marked else ([selected] if selected is not None else [])
                self.pending_delete_ids = tuple(item.session_id for item in targets)
            return None
        if value == "ENTER":
            # Reconcile immediately before an identity-sensitive action.  A
            # row disappearing must never cause Enter to target its successor.
            self.refresh(force=True)
            selected = self.selected_session
            self.mark_selected_seen()
            return ManagerResult("attach", selected.session_id) if selected is not None else None
        if value in {"PAGE_UP", "PAGEUP"}:
            self.refresh()
            self.selected = max(0, self.selected - self.page_size)
            self.activity_offset = 0
            return None
        if ch == "[":
            self.activity_offset += 6
            return None
        if value in {"PAGE_DOWN", "PAGEDOWN"}:
            self.refresh()
            self.selected = min(max(0, len(self._sessions) - 1), self.selected + self.page_size)
            self.activity_offset = 0
            return None
        if ch == "]":
            self.activity_offset = max(0, self.activity_offset - 6)
            return None
        if value == "ESC" or ch.lower() == "q":
            return ManagerResult("exit")
        if ch == "?":
            self.help_visible = not self.help_visible
            return None
        if ch.lower() == "r":
            self.maintain(force=True)
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
            # A redirected manager is a diagnostic listing, not a viewport;
            # retain the historical behavior of printing every record.
            self.console.print(self.render(all_sessions=True))
            return ManagerResult("exit")
        self.maintain(force=True)
        self.refresh(force=True)
        initial = self.render()
        self._interactive_delete = True
        try:
            with Live(initial, console=self.console, auto_refresh=False) as live:
                live.refresh()
                with backend.enter_raw_mode():
                    loop = asyncio.get_running_loop()
                    read_future = loop.run_in_executor(None, backend.read_key)
                    while True:
                        done, _ = await asyncio.wait({read_future}, timeout=self.refresh_s)
                        if read_future in done:
                            key, ch = read_future.result()
                            result = self.handle_key(key, ch)
                            self._poll_async_delete()
                            if result is not None:
                                return result
                            read_future = loop.run_in_executor(None, backend.read_key)

                        # Polling/recovery is deliberately independent of
                        # rendering.  An unchanged snapshot returns the cached
                        # renderable and does not issue a Rich update.
                        self._poll_async_delete()
                        self.refresh()
                        self.maintain()
                        rendered = self.render()
                        if rendered is not initial:
                            live.update(rendered, refresh=True)
                            initial = rendered
        finally:
            self._interactive_delete = False


async def run_background_manager(
    console: Console,
    *,
    store: BackgroundStore | None = None,
    supervisor: BackgroundSupervisor | None = None,
) -> ManagerResult:
    """Convenience entry point used by CLI aliases and tests."""

    return await BackgroundManager(console, store=store, supervisor=supervisor).run()
