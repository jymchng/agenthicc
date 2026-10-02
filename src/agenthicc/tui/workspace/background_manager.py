"""Interactive manager for durable background sessions (PRD-141)."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, cast

from agenthicc.background import (
    ACTIVE_STATUSES,
    BackgroundManagerService,
    ManagerOperationResult,
    BackgroundPage,
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    DeleteFailure,
    DeleteResult,
    SessionStatus,
)
from agenthicc.background.settings import BackgroundManagerSettings

MAX_SESSION_ROWS_PER_PAGE = 10

if TYPE_CHECKING:
    from rich.console import Console, RenderableType
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.workspace.overlay import OverlayHost


@dataclass(frozen=True)
class ManagerResult:
    """Result returned when the manager loop yields control to its caller."""

    action: str
    session_id: str | None = None


def _consume_task_result(task: asyncio.Task[object]) -> None:
    """Retrieve a manager-owned task's eventual exception after bounded exit."""
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:  # noqa: BLE001
        pass


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

        # The normal details panel has enough content rows for identity,
        # activity, and one phase-history line (Dir is part of identity).
        detail_height = 3 if height <= 10 else (4 if height <= 14 else 7)
        # Header=1, table header/bottom=2, details panel, footer=1.
        available = max(1, height - 1 - 2 - detail_height - 1)
        available = min(available, MAX_SESSION_ROWS_PER_PAGE)
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
        manager_settings: BackgroundManagerSettings | None = None,
        service: BackgroundManagerService | None = None,
    ) -> None:
        self.console = console
        self.store = store or BackgroundStore()
        self.supervisor = supervisor or BackgroundSupervisor(self.store)
        self.manager_settings = manager_settings or BackgroundManagerSettings()
        self.refresh_s = (
            self.manager_settings.refresh_interval_s
            if manager_settings is not None
            else max(0.1, refresh_s)
        )
        self.input_provider = input_provider
        self._configured_page_size = page_size
        self._selected_index = 0
        self._selected_session_id: str | None = None
        self._detail_session_id: str | None = None
        self._detail_scroll = 0
        self.query = ""
        self.include_archived = True
        self.status_filter: SessionStatus | None = None
        self.project_filter: str | None = None
        self.workflow_filter: str | None = None
        self.paused = False
        self.new_activity = False
        self.help_visible = False
        self._deletion_task: asyncio.Task[DeleteResult] | None = None
        # Direct handle_key() callers outside an event loop are a supported
        # compatibility/testing surface. The real TUI always uses the task.
        self._deletion_future: Future[DeleteResult] | None = None
        self._deletion_executor: ThreadPoolExecutor | None = None
        self._deleting_ids: tuple[str, ...] = ()
        self._deletion_operation_id = ""
        self._deletion_phase = ""
        self._deletion_error = ""
        self.marked_ids: set[str] = set()
        self.filter_mode = False
        self.filter_buffer = ""
        self.last_refresh = 0.0
        self._sessions: list[BackgroundSession] = []
        self._seen_activity: dict[str, float] = {}
        self.activity_offset = 0
        self._activity_cache: dict[str, tuple[tuple[int, int, int, int, str], str | None]] = {}
        self._last_maintenance = 0.0
        self.maintenance_s = (
            self.manager_settings.maintenance_interval_s
            if manager_settings is not None
            else max(1.0, self.refresh_s)
        )
        self._last_render_key: tuple[object, ...] | None = None
        self._last_renderable: RenderableType | None = None
        self._service = service or BackgroundManagerService(
            self.store,
            self.supervisor,
            max_workers=self.manager_settings.max_in_flight_operations,
        )
        self._owns_service = service is None
        self._async_mode = False
        self._visible_sessions: list[BackgroundSession] = []
        self._total_count = 0
        self._projection_ready = False
        self._projection_stale = False
        self._page_start = 0
        self._projection_generation: tuple[int, int, int, int] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_pending = False
        self._refresh_request_generation = 0
        self._refresh_active_key: tuple[object, ...] | None = None
        self._projection_error = ""
        self._projection_started_at = 0.0
        self._refresh_failure_count = 0
        self._refresh_retry_at = 0.0
        self._maintenance_task: asyncio.Task[list[BackgroundSession]] | None = None
        self._maintenance_running = False
        self._operation_tasks: dict[str, asyncio.Task[object]] = {}
        self._operation_state: dict[str, tuple[str, str]] = {}
        self._attach_result: ManagerResult | None = None
        self._activity_task: asyncio.Task[None] | None = None
        self._activity_generation = 0
        self._activity_session_id: str | None = None
        self._activity_last_active: float | None = None
        self._activity_checked_at = 0.0
        self._activity_text: dict[str, str | None] = {}
        self._input_receipt_text: dict[str, str | None] = {}
        self._input_receipt_fingerprint: dict[str, tuple[int, int, int, int]] = {}
        self._dirty_causes: set[str] = {"projection_changed", "layout_changed"}
        self._notice = ""
        self._input_executor: ThreadPoolExecutor | None = None
        self._composer_target: BackgroundSession | None = None
        self._composer_open = False
        self._composer_state: AppState | None = None
        self._composer_input: UnifiedInputSession | None = None
        self._composer_overlay: OverlayHost | None = None
        self._composer_history: dict[str, list[str]] = {}
        self._input_history: dict[str, list[str]] = {}
        self._composer_task: asyncio.Task[object] | None = None
        self._composer_build_task: asyncio.Task[None] | None = None
        self._composer_building = False
        self._composer_pending_keys: list[tuple[object, str]] = []
        self._composer_submitting = False
        self._composer_submitted = False
        self._composer_receipt = ""
        self._last_maintenance_async = 0.0
        self._maintenance_retry_at = 0.0
        self._maintenance_failure_count = 0
        self._operation_semaphore: asyncio.Semaphore | None = None
        self._metrics_enabled = self.manager_settings.metrics
        self._metric_aggregates: dict[str, tuple[int, float, float]] = {}
        self._metric_samples: dict[str, list[float]] = {}
        self._last_key_received_at: float | None = None

    def _record_metric(self, name: str, duration_s: float = 0.0) -> None:
        if not self._metrics_enabled:
            return
        count, total, maximum = self._metric_aggregates.get(name, (0, 0.0, 0.0))
        duration_ms = max(0.0, duration_s * 1_000.0)
        self._metric_aggregates[name] = (
            count + 1,
            total + duration_ms,
            max(maximum, duration_ms),
        )
        samples = self._metric_samples.setdefault(name, [])
        samples.append(duration_ms)
        if len(samples) > 512:
            del samples[:-512]

    @staticmethod
    async def _resolve_operation(value: object) -> object:
        if inspect.isawaitable(value):
            return await cast(Awaitable[object], value)
        return value

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
        return ordered[index]

    @property
    def diagnostics(self) -> dict[str, object]:
        """Return redacted bounded manager diagnostics for tests/support."""

        aggregates = {
            name: {
                "count": count,
                "total_ms": total,
                "max_ms": maximum,
                "p50_ms": self._percentile(self._metric_samples.get(name, []), 0.50),
                "p95_ms": self._percentile(self._metric_samples.get(name, []), 0.95),
                "p99_ms": self._percentile(self._metric_samples.get(name, []), 0.99),
            }
            for name, (count, total, maximum) in self._metric_aggregates.items()
        }
        return {
            "projection_generation": self._projection_generation,
            "render_generation": len(self._dirty_causes),
            "refresh_pending": self._refresh_pending,
            "refresh_in_flight": self._refresh_task is not None and not self._refresh_task.done(),
            "maintenance_in_flight": self._maintenance_task is not None
            and not self._maintenance_task.done(),
            "activity_in_flight": self._activity_task is not None
            and not self._activity_task.done(),
            "queued_frame_count": int(bool(self._dirty_causes)),
            "metrics": aggregates,
        }

    @property
    def _deletion_thread(self) -> Future[DeleteResult] | None:
        """Legacy inspection alias for direct callers during the migration.

        Interactive deletion no longer owns a raw thread. The alias keeps old
        diagnostics/tests that poll for completion working while exposing the
        bounded compatibility future, if one exists outside the async TUI.
        """

        return self._deletion_future

    @property
    def selected(self) -> int:
        """Compatibility index view; actions are internally ID-targeted."""

        records = self._visible_sessions if self._async_mode else self._sessions
        offset = self._page_start if self._async_mode else 0
        if self._selected_session_id:
            for local_index, session in enumerate(records):
                if session.session_id == self._selected_session_id:
                    self._selected_index = offset + local_index
                    break
        count = self._total_count if self._async_mode else len(self._sessions)
        if not count:
            return 0
        return min(max(self._selected_index, 0), count - 1)

    @selected.setter
    def selected(self, value: int) -> None:
        self._selected_index = max(0, int(value))
        records = self._visible_sessions if self._async_mode else self._sessions
        count = self._total_count if self._async_mode else len(self._sessions)
        if self._async_mode:
            if count:
                self._selected_index = min(self._selected_index, count - 1)
                local_index = self._selected_index - self._page_start
                if 0 <= local_index < len(records):
                    self._selected_session_id = records[local_index].session_id
                else:
                    self._selected_session_id = None
            else:
                self._selected_index = 0
                self._selected_session_id = None
        elif records:
            local_index = (
                self._selected_index - self._page_start
                if self._async_mode
                else self._selected_index
            )
            local_index = min(max(local_index, 0), len(records) - 1)
            self._selected_index = (
                self._page_start + local_index if self._async_mode else local_index
            )
            self._selected_session_id = records[local_index].session_id
        elif count == 0:
            self._selected_index = 0
            self._selected_session_id = None

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

        count = self._total_count if self._async_mode else len(self._sessions)
        return max(1, (count + self.page_size - 1) // self.page_size)

    def _page_bounds(self) -> tuple[int, int]:
        """Return the half-open slice for the page containing the selection."""

        if self._async_mode:
            return self._page_start, self._page_start + len(self._visible_sessions)
        page = self.selected // self.page_size
        start = page * self.page_size
        return start, min(len(self._sessions), start + self.page_size)

    @property
    def sessions(self) -> list[BackgroundSession]:
        if self._async_mode:
            return list(self._visible_sessions)
        self.refresh()
        return list(self._sessions)

    @property
    def selected_session(self) -> BackgroundSession | None:
        # Actions can be invoked immediately after construction, before the
        # first render has populated the snapshot.  This one bootstrap read is
        # still cached and does not make render itself impure.
        if self._async_mode:
            for session in self._visible_sessions:
                if session.session_id == self._selected_session_id:
                    return session
            return None
        if not self._sessions and not self.paused:
            self.refresh(force=True)
        if not self._sessions:
            return None
        index = self.selected
        return self._sessions[index]

    def _mark_dirty(self, cause: str) -> None:
        self._dirty_causes.add(cause)

    def _request_render(self, cause: str) -> None:
        if cause not in {"poll", "timer"}:
            self._mark_dirty(cause)

    def _input_refresh(self) -> None:
        """Refresh synchronously for compatibility, or schedule it in the TUI."""

        if self._async_mode:
            self._request_async_refresh("input")
        else:
            self.refresh(force=True)

    def _selected_id_for_action(self) -> str | None:
        selected = self.selected_session
        return selected.session_id if selected is not None else None

    def _move_selection(self, value: int) -> None:
        old_page = self._requested_page() if self._async_mode else 0
        self.selected = value
        if self._detail_session_id != self._selected_session_id:
            self._detail_session_id = None
            self._detail_scroll = 0
        if self._async_mode and self._requested_page() != old_page:
            self._selected_session_id = None
            self._request_async_refresh("selection_changed", force=True)
        else:
            self._request_render("selection_changed")
            self._schedule_activity_read()

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
                cwd=self.project_filter,
                workflow_name=self.workflow_filter,
                query=self.query,
                status=self.status_filter,
            )
            self._total_count = len(self._sessions)
            self._visible_sessions = list(self._sessions)
            self._page_start = 0
            self._projection_generation = self.store.change_token()
            for item in self._sessions:
                self._seen_activity.setdefault(item.session_id, item.last_active)
                if (
                    item.session_id in previous
                    and item.last_active > self._seen_activity[item.session_id]
                ):
                    self.new_activity = True
            self._reconcile_selection(previous_id, previous_index)
            self.last_refresh = time.monotonic()
            self._mark_dirty("projection_changed")
        return self._sessions

    def _requested_page(self) -> int:
        return self.selected // max(1, self.page_size) + 1

    def _request_async_refresh(self, cause: str = "timer", *, force: bool = False) -> None:
        if not self._async_mode:
            self._mark_dirty(cause)
            return
        if (
            not force
            and cause in {"poll", "timer", "coalesced"}
            and time.monotonic() < self._refresh_retry_at
        ):
            return
        if self.paused and not force:
            if cause not in {"poll", "timer"}:
                self._mark_dirty(cause)
            return
        self._mark_dirty(cause)
        page = self._requested_page()
        self._record_metric("projection_requested")
        query_key: tuple[object, ...] = (
            page,
            self.include_archived,
            self.project_filter,
            self.workflow_filter,
            self.query,
            self.status_filter,
        )
        if self._refresh_task is not None and not self._refresh_task.done():
            if query_key != self._refresh_active_key:
                self._refresh_request_generation += 1
            self._refresh_pending = True
            return
        self._refresh_request_generation += 1
        generation = self._refresh_request_generation
        self._refresh_pending = False
        self._refresh_active_key = query_key
        if not self._projection_ready:
            self._projection_started_at = time.monotonic()
        self._refresh_task = asyncio.create_task(
            self._load_async_page(generation, page, query_key),
            name=f"agenthicc-background-refresh-{generation}",
        )

    async def _load_async_page(
        self,
        generation: int,
        page: int,
        query_key: tuple[object, ...],
    ) -> None:
        started = time.monotonic()
        try:
            result = await self._service.refresh_page_async(
                page=page,
                page_size=self.page_size,
                include_archived=self.include_archived,
                cwd=self.project_filter,
                workflow_name=self.workflow_filter,
                query=self.query,
                status=self.status_filter,
            )
            if generation != self._refresh_request_generation:
                return
            self._apply_async_page(result)
            self._refresh_failure_count = 0
            self._refresh_retry_at = 0.0
            self._projection_error = ""
            self._record_metric("projection_ready", time.monotonic() - started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if generation == self._refresh_request_generation:
                self._refresh_failure_count += 1
                delay = min(60.0, 0.5 * (2 ** min(7, self._refresh_failure_count - 1)))
                self._refresh_retry_at = time.monotonic() + delay
                self._projection_error = f"{type(exc).__name__}: {exc}"[:240]
                self._notice = f"Session index failed to load: {self._projection_error}"
                self._mark_dirty("notice_changed")
        finally:
            if self._refresh_task is asyncio.current_task():
                self._refresh_task = None
                self._refresh_active_key = None
            if self._refresh_pending:
                self._refresh_pending = False
                self._request_async_refresh("coalesced")

    def _apply_async_page(self, result: BackgroundPage) -> None:
        old_signature = (
            tuple(self._session_render_signature(item) for item in self._visible_sessions),
            self._total_count,
            self._projection_generation,
            self._projection_stale,
        )
        previous_id = self._selected_session_id
        previous_index = self._selected_index
        self._visible_sessions = list(result.sessions)
        self._total_count = result.total
        self._page_start = (result.page - 1) * result.page_size
        self._projection_generation = result.generation
        self._projection_stale = result.stale
        self._projection_ready = True
        self._projection_error = ""
        self._sessions = list(result.sessions)
        if result.total == 0:
            self._selected_index = 0
            self._selected_session_id = None
        else:
            local = next(
                (
                    index
                    for index, item in enumerate(result.sessions)
                    if item.session_id == previous_id
                ),
                None,
            )
            if local is None:
                absolute = min(max(previous_index, self._page_start), result.total - 1)
                local = min(max(absolute - self._page_start, 0), max(len(result.sessions) - 1, 0))
            if result.sessions:
                self._selected_index = self._page_start + local
                self._selected_session_id = result.sessions[local].session_id
        self.last_refresh = time.monotonic()
        if self._notice.startswith(
            (
                "Refresh failed:",
                "Maintenance failed:",
                "Session index failed to load:",
                "Session index retry requested",
                "Refresh requested",
            )
        ):
            self._notice = ""
        new_signature = (
            tuple(self._session_render_signature(item) for item in self._visible_sessions),
            self._total_count,
            self._projection_generation,
            self._projection_stale,
        )
        if old_signature != new_signature:
            self._mark_dirty("projection_changed")
        self._schedule_activity_read()

    @staticmethod
    def _session_render_signature(session: BackgroundSession) -> tuple[object, ...]:
        return (
            session.session_id,
            session.status,
            session.title,
            session.workflow_name,
            session.cwd,
            session.current_phase,
            session.phase_history,
            session.last_active,
            session.error,
            session.latest_activity,
            session.attempt,
            session.heartbeat_stale,
            session.pinned,
        )

    async def refresh_page_async(self, *, force: bool = False) -> None:
        """Request a coalesced page refresh for the interactive manager."""

        self._request_async_refresh("explicit", force=force)
        task = self._refresh_task
        if task is not None:
            await asyncio.shield(task)

    async def maintain_async(self, *, force: bool = False) -> list[BackgroundSession]:
        now = time.monotonic()
        if not force and now < self._maintenance_retry_at:
            return []
        if not force and now - self._last_maintenance_async < self.maintenance_s:
            return []
        current_task = asyncio.current_task()
        if self._maintenance_running:
            return []
        self._maintenance_running = True
        self._last_maintenance_async = now
        self._record_metric("maintenance_started")
        try:
            changed = await self._service.maintain_async(force=force)
            self._maintenance_failure_count = 0
            self._maintenance_retry_at = 0.0
            self._record_metric("maintenance", time.monotonic() - now)
        except Exception as exc:  # noqa: BLE001
            self._maintenance_failure_count += 1
            delay = min(60.0, self.maintenance_s * (2**self._maintenance_failure_count))
            self._maintenance_retry_at = time.monotonic() + delay
            self._notice = f"Maintenance failed: {type(exc).__name__}: {exc}"[:240]
            self._mark_dirty("notice_changed")
            return []
        finally:
            self._record_metric("maintenance_finished", time.monotonic() - now)
            self._maintenance_running = False
            if current_task is not None and self._maintenance_task is current_task:
                self._maintenance_task = None
        if changed:
            self._request_async_refresh("maintenance", force=True)
        return changed

    def _schedule_maintenance(
        self, *, force: bool, name: str
    ) -> asyncio.Task[list[BackgroundSession]]:
        current = self._maintenance_task
        if current is not None and not current.done():
            return current
        task = asyncio.create_task(self.maintain_async(force=force), name=name)
        self._maintenance_task = task
        return task

    def _pending_target(self, target_id: str) -> bool:
        return any(target == target_id for target, _phase in self._operation_state.values())

    def _start_operation(
        self,
        target_id: str,
        label: str,
        operation: Callable[[], object],
    ) -> None:
        if self._pending_target(target_id):
            return
        self._record_metric("action_dispatched")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = operation()
            if isinstance(result, ManagerOperationResult) and not result.ok:
                self.console.print(f"{label.title()} failed: {result.message}")
            self._input_refresh()
            return
        operation_id = uuid.uuid4().hex
        self._operation_state[operation_id] = (target_id, "starting")
        self._mark_dirty("operation_changed")
        task = asyncio.create_task(
            self._run_operation(operation_id, target_id, label, operation),
            name=f"agenthicc-background-{label}-{operation_id}",
        )
        self._operation_tasks[operation_id] = task

    async def _run_operation(
        self,
        operation_id: str,
        target_id: str,
        label: str,
        operation: Callable[[], object],
    ) -> None:
        self._operation_state[operation_id] = (target_id, "running")
        self._mark_dirty("operation_changed")
        try:
            semaphore = self._operation_semaphore
            if semaphore is None:
                value = operation()
                result = await self._resolve_operation(value)
            else:
                async with semaphore:
                    value = operation()
                    result = await self._resolve_operation(value)
            if isinstance(result, ManagerOperationResult) and not result.ok:
                self._notice = f"{label.title()} failed: {result.message}"[:240]
            else:
                self._notice = f"{label.title()} completed"
                if label == "attach":
                    self._attach_result = ManagerResult("attach", target_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._notice = f"{label.title()} failed: {type(exc).__name__}: {exc}"[:240]
        finally:
            self._operation_state.pop(operation_id, None)
            self._operation_tasks.pop(operation_id, None)
            self._mark_dirty("operation_changed")
            self._request_async_refresh("operation_complete", force=True)

    def _schedule_activity_read(self) -> None:
        if not self._async_mode:
            return
        selected = self.selected_session
        if selected is None:
            self._activity_session_id = None
            self._activity_last_active = None
            self._activity_generation += 1
            if self._activity_task is not None and not self._activity_task.done():
                self._activity_task.cancel()
            return
        same_activity_target = (
            selected.session_id == self._activity_session_id
            and selected.last_active == self._activity_last_active
        )
        if same_activity_target:
            if self._activity_task is not None and not self._activity_task.done():
                return
            check_after = max(0.25, self.manager_settings.refresh_interval_s)
            if (
                selected.session_id in self._activity_text
                and time.monotonic() - self._activity_checked_at < check_after
            ):
                return
        self._activity_generation += 1
        generation = self._activity_generation
        if self._activity_task is not None and not self._activity_task.done():
            self._activity_task.cancel()
        self._activity_session_id = selected.session_id
        self._activity_last_active = selected.last_active
        self._activity_checked_at = time.monotonic()
        self._activity_task = asyncio.create_task(
            self._load_activity(selected, generation),
            name=f"agenthicc-background-activity-{selected.session_id}",
        )

    async def _load_activity(self, session: BackgroundSession, generation: int) -> None:
        try:
            value = await self._service.run_blocking(self._load_detail_projection, session)
            if (
                generation != self._activity_generation
                or session.session_id != self._selected_session_id
            ):
                return
            lines: list[str] = []
            receipt: str | None = None
            history: list[str] = []
            if (
                isinstance(value, tuple)
                and len(value) == 3
                and isinstance(value[0], list)
                and (value[1] is None or isinstance(value[1], str))
                and isinstance(value[2], list)
            ):
                lines = [line for line in value[0] if isinstance(line, str)]
                receipt = value[1]
                history = [entry for entry in value[2] if isinstance(entry, str)]
            self._activity_text[session.session_id] = lines[0] if lines else None
            self._input_receipt_text[session.session_id] = receipt
            self._input_history[session.session_id] = history
            self._mark_dirty("activity_changed")
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _load_detail_projection(
        self, session: BackgroundSession
    ) -> tuple[list[str], str | None, list[str]]:
        """Read bounded activity, input receipt, and session input history."""
        from agenthicc.background.input_inbox import BackgroundInputInbox  # noqa: PLC0415
        from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415
        from agenthicc.tui.runtime.session_log import load_user_message_history  # noqa: PLC0415

        activity = self._activity_lines(session)
        history = self._input_history.get(session.session_id)
        if history is None:
            history = load_user_message_history(session.session_id, last_turns=20)
            self._input_history[session.session_id] = history
        inbox = BackgroundInputInbox(self.store)
        receipt_fingerprint = (*inbox.fingerprint(session.session_id), session.attempt)
        if (
            self._input_receipt_fingerprint.get(session.session_id) == receipt_fingerprint
            and session.session_id in self._input_receipt_text
        ):
            return activity, self._input_receipt_text[session.session_id], history
        try:
            receipts = inbox.receipts(session.session_id)
        except (OSError, ValueError):
            return activity, None, history
        if not receipts:
            self._input_receipt_text[session.session_id] = None
            self._input_receipt_fingerprint[session.session_id] = receipt_fingerprint
            return activity, None, history
        receipt = receipts[-1]
        labels = {
            "accepted": "waiting for recovery" if receipt.deferred else "queued",
            "claimed": "processing",
            "delivered": "delivered",
            "completed": "processed",
            "failed": "failed",
            "rejected": "not delivered",
        }
        state = labels.get(receipt.state, "pending")
        detail = f"{state} · {receipt.message_id[:12]}"
        if receipt.error:
            safe_error = _Redactor().value(receipt.error, "message")
            detail += f" · {str(safe_error)[:120]}"
        self._input_receipt_text[session.session_id] = detail
        self._input_receipt_fingerprint[session.session_id] = receipt_fingerprint
        return activity, detail, history

    def set_query(self, query: str) -> None:
        self.query = query.strip()
        self._selected_session_id = None
        self._selected_index = 0
        self._input_refresh()

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

        if self._async_mode:
            ids = tuple(sorted(self.marked_ids))
            for session_id in ids:

                async def operation(sid: str = session_id) -> ManagerOperationResult:
                    if action == "cancel":
                        return await self._service.cancel_async(sid)
                    return await self._service.archive_async(sid)

                self._start_operation(session_id, action, operation)
            self.marked_ids.clear()
            self._request_render("operation_changed")
            return
        records = self.marked_sessions()
        if not records:
            return
        for record in records:
            try:
                if action == "cancel":
                    self.supervisor.cancel(record.session_id)
                else:
                    self.supervisor.archive(record.session_id)
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
        self._input_refresh()

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
        fingerprint = self._activity_fingerprint(session)
        cached = self._activity_cache.get(session.session_id)
        if cached is not None and cached[0] == fingerprint:
            return [cached[1]] if cached[1] is not None else []
        try:
            start = max(0, stat.st_size - self.manager_settings.activity_tail_bytes)
            with path.open("rb") as handle:
                handle.seek(start)
                raw = handle.read(self.manager_settings.activity_tail_bytes)
            # A bounded tail may begin in the middle of a JSONL record. Drop
            # that partial first line; only complete records are candidates.
            if start:
                _partial, separator, remainder = raw.partition(b"\n")
                raw = remainder if separator else b""
            raw_lines = raw.decode("utf-8", errors="replace").splitlines()
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

    def _activity_fingerprint(self, session: BackgroundSession) -> tuple[int, int, int, int, str]:
        path = Path(session.artifact_dir).expanduser() / "conversation.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return (0, 0, 0, session.attempt, session.lease_token)
        return (
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            session.attempt,
            session.lease_token,
        )

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
        source = self._visible_sessions if self._async_mode else self._sessions
        visible = source if all_sessions or self._async_mode else source[start:end]
        session_key = tuple(
            (
                item.session_id,
                item.status.value,
                item.title,
                item.workflow_name,
                item.cwd,
                item.last_active,
                item.current_phase,
                item.phase_history,
                item.error,
                item.latest_activity,
                item.attempt,
                item.heartbeat_stale,
                item.attempt_history[-1] if item.attempt_history else None,
                item.pinned,
                item.created_at,
                item.started_at,
                item.completed_at,
                item.provider,
                item.model,
                item.source,
                item.failure_category,
                item.attempt,
                item.retry_count,
                item.run_id,
                item.parent_session_id,
                item.role,
                item.task_id,
                item.branch,
                item.base_commit,
                item.requested_mode_name,
                item.mode_name,
                item.mode_application_status,
                item.mode_application_attempt,
                item.mode_application_error,
            )
            for item in visible
        )
        return (
            all_sessions,
            budget,
            start,
            end,
            self._selected_session_id,
            self._detail_session_id,
            self._detail_scroll,
            session_key,
            self.query,
            self.help_visible,
            self._deleting_ids,
            self._deletion_operation_id,
            self._deletion_phase,
            self._deletion_error,
            tuple(sorted(self.marked_ids)),
            self.new_activity,
            self.activity_offset,
            self._activity_cache.get(selected.session_id) if selected is not None else None,
            self._activity_text.get(selected.session_id) if selected is not None else None,
            self._input_receipt_text.get(selected.session_id) if selected is not None else None,
            self._projection_generation,
            self._projection_stale,
            self._projection_error,
            int(max(0.0, time.monotonic() - self._projection_started_at))
            if not self._projection_ready
            else 0,
            tuple(sorted(self._operation_state.items())),
            self._notice,
            self._composer_open,
            self._composer_target.session_id if self._composer_target is not None else None,
            self._composer_target.attempt if self._composer_target is not None else None,
            self._composer_building,
            len(self._composer_pending_keys),
            tuple(self._composer_state.input.buf()) if self._composer_state is not None else (),
            self._composer_state.input.cursor() if self._composer_state is not None else 0,
            self._composer_state.input.paste_condensed()
            if self._composer_state is not None
            else False,
            self._composer_receipt,
            self._composer_submitting,
        )

    @staticmethod
    def _display_time(timestamp: float | None) -> str:
        if timestamp is None or timestamp <= 0:
            return "—"
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(timestamp))

    @staticmethod
    def _mode_label(session: BackgroundSession) -> str:
        if session.mode_application_status.value == "applied":
            return session.mode_name or "applied"
        requested = (
            session.requested_mode_name
            if session.requested_mode_name is not None
            else "default/persisted"
        )
        if requested == "":
            requested = "<empty>"
        return f"{session.mode_application_status.value}: {requested}"

    def _render_detail_page(
        self,
        session: BackgroundSession | None,
        budget: ViewportBudget,
    ) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        target_id = self._detail_session_id or "unknown"
        if session is None:
            rows = [
                ("Session ID", target_id),
                ("State", "No longer available in the current session index"),
            ]
        else:
            activity = self._activity_text.get(session.session_id) or session.latest_activity
            history = " → ".join(session.phase_history) or "—"
            requested_mode = (
                "<empty>"
                if session.requested_mode_name == ""
                else session.requested_mode_name or "—"
            )
            rows = [
                ("Title", session.title or "—"),
                ("Session ID", session.session_id),
                ("State", session.status.value),
                ("Dir", self._workspace_name(session.cwd)),
                ("Workflow", session.workflow_name or "direct"),
                ("Mode", self._mode_label(session)),
                ("Requested mode", requested_mode),
                (
                    "Mode application",
                    f"{session.mode_application_status.value} · attempt "
                    f"{session.mode_application_attempt}",
                ),
                ("Current phase", session.current_phase or "—"),
                ("Phase history", history),
                ("Provider", session.provider or "—"),
                ("Model", session.model or "—"),
                ("Source", session.source or "—"),
                ("Created", self._display_time(session.created_at)),
                ("Started", self._display_time(session.started_at)),
                ("Updated", self._display_time(session.last_active)),
                ("Completed", self._display_time(session.completed_at)),
                ("Attempt / retries", f"{session.attempt} / {session.retry_count}"),
                ("Latest activity", " ".join(activity.split()) or "—"),
            ]
            input_receipt = self._input_receipt_text.get(session.session_id)
            if input_receipt:
                rows.append(("Latest input", input_receipt))
            if session.heartbeat_stale:
                rows.append(("Heartbeat", "Delayed; worker identity is being reconciled"))
            if session.error and session.status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
                rows.append(("Error", session.error))
            if session.mode_application_error:
                rows.append(("Mode error", session.mode_application_error))
            if session.failure_category and session.status in {
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }:
                rows.append(("Failure category", session.failure_category))
            if session.attempt_history and session.attempt_history[-1].attempt < session.attempt:
                previous = session.attempt_history[-1]
                previous_text = previous.error or previous.latest_activity or "No error details"
                rows.append(
                    (
                        f"Previous attempt {previous.attempt} ({previous.status.value})",
                        previous_text,
                    )
                )
            elif session.error and session.status in ACTIVE_STATUSES:
                rows.append(("Legacy prior error", session.error))
            if session.exit_reason:
                rows.append(("Exit reason", session.exit_reason))
            if session.run_id:
                rows.append(("Run ID", session.run_id))
            if session.parent_session_id:
                rows.append(("Parent session", session.parent_session_id))
            worker_task = " / ".join(filter(None, (session.role, session.task_id)))
            if worker_task:
                rows.append(("Worker task", worker_task))
            if session.branch:
                rows.append(("Branch", session.branch))
            if session.base_commit:
                rows.append(("Base commit", session.base_commit))
            if session.status == SessionStatus.WAITING_APPROVAL:
                rows.append(("Pending action", "Waiting for approval"))
            elif session.status == SessionStatus.WAITING_INPUT:
                rows.append(("Pending action", "Waiting for user input"))

        # Keep the detail panel content-driven instead of stretching a fixed
        # frame to the terminal height. A full-height Rich Panel leaves the
        # page's attach/scroll help below the live viewport (the remaining
        # detail fields can then look unreachable even though the offset moves).
        # Bound the rows first, then let the frame wrap just that viewport so
        # the page heading and controls always have their own visible rows.
        max_visible = max(1, budget.height - 5)
        max_scroll = max(0, len(rows) - max_visible)
        self._detail_scroll = min(self._detail_scroll, max_scroll)
        visible = rows[self._detail_scroll : self._detail_scroll + max_visible]
        body = Text()
        for index, (label, value) in enumerate(visible):
            if index:
                body.append("\n")
            body.append(f"{label}: ", style="bold")
            one_line_value = " ".join(value.split())
            available = max(1, budget.width - len(label) - 5)
            if len(one_line_value) > available:
                one_line_value = one_line_value[: max(1, available - 1)] + "…"
            body.append(one_line_value)

        if budget.compact:
            return Group(
                Text("Session details", style="bold cyan"),
                body,
                Text(
                    "Esc back · Enter attach · i input · ↑↓/PgUp/PgDn scroll",
                    style="dim",
                ),
            )

        panel = Panel(
            body,
            title="Session Details" if session is not None else "Session Details · unavailable",
            padding=(0, 1),
            expand=True,
        )
        first = self._detail_scroll + 1 if rows else 0
        last = min(len(rows), self._detail_scroll + len(visible))
        footer_label = (
            "Esc · Enter attach · i input · ↑↓ PgUp/PgDn [ ] Home/End"
            if session is not None
            else "Esc back · r refresh · q quit"
        )
        footer = Text(f"{footer_label}  ·  {first}–{last}/{len(rows)}", style="dim")
        return Group(
            Text(
                f"Background Session · {self._workspace_name(session.cwd)}"
                if session is not None
                else "Background Session",
                style="bold cyan",
            ),
            panel,
            footer,
        )

    def _build_target_input_editor(self, target: BackgroundSession) -> None:
        """Create the canonical editor with registries scoped to *target*."""
        from agenthicc.commands.builtins import build_builtin_registry  # noqa: PLC0415
        from agenthicc.commands.plugin_loader import discover_command_plugins  # noqa: PLC0415
        from agenthicc.config import load_config  # noqa: PLC0415
        from agenthicc.skills.loader import discover_skills  # noqa: PLC0415
        from agenthicc.tui.conversation_store import AppState  # noqa: PLC0415
        from agenthicc.tui.input.unified_session import UnifiedInputSession  # noqa: PLC0415
        from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand  # noqa: PLC0415
        from agenthicc.tui.runtime.mode_manager import ModeManager  # noqa: PLC0415
        from agenthicc.tui.trigger import TriggerManager  # noqa: PLC0415
        from agenthicc.tui.triggers.at_mention import AtMentionTrigger  # noqa: PLC0415
        from agenthicc.tui.triggers.slash_command import (  # noqa: PLC0415
            SkillTrigger,
            SlashCommandTrigger,
        )
        from agenthicc.tui.workspace.overlay import OverlayHost  # noqa: PLC0415
        from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415

        target_root = Path(target.cwd).expanduser().resolve()
        project_agent_dir = target_root / ".agenthicc"
        user_agent_dir = Path.home() / ".agenthicc"
        config_path: str | None = None
        set_overrides: list[str] = []
        set_secret_overrides: list[str] = []
        try:
            request_path = Path(self.supervisor._request_path(target.session_id))
            request_value = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            request_value = {}
        if isinstance(request_value, dict):
            raw_config_path = request_value.get("config_path")
            config_path = raw_config_path if isinstance(raw_config_path, str) else None
            for key, destination in (
                ("set_overrides", set_overrides),
                ("set_secret_overrides", set_secret_overrides),
            ):
                raw_values = request_value.get(key, ())
                if isinstance(raw_values, (list, tuple)):
                    destination.extend(item for item in raw_values if isinstance(item, str))

        project_config = project_agent_dir / "agenthicc.toml"
        cfg = load_config(
            project_path=project_config if project_config.is_file() else None,
            config_path=config_path,
            cli_overrides=set_overrides,
            cli_secret_overrides=set_secret_overrides,
        )
        commands = build_builtin_registry()
        for command in discover_command_plugins(
            project_dir=project_agent_dir,
            user_dir=user_agent_dir,
        ).all_commands:
            commands.register(command)
        skills = discover_skills(project_dir=project_agent_dir, user_dir=user_agent_dir)
        from agenthicc.runners.tui_session import _build_skill_command  # noqa: PLC0415

        for slug, skill in skills.items():
            command = _build_skill_command(slug, skill)
            if commands.get(command.name) is None:
                commands.register(command)
        workflows = build_workflow_registry(
            project_dir=project_agent_dir,
            user_dir=user_agent_dir,
            load_external=True,
        )
        triggers = TriggerManager()
        triggers.register(AtMentionTrigger())
        triggers.register(SlashCommandTrigger(commands, workflows))
        triggers.register(SkillTrigger(commands))

        state = AppState()
        state.conversation.session_id.set(target.session_id)
        modes = ModeManager(app_state=state, default_name=target.mode_name or "Safe")
        overlay = OverlayHost(state)
        overlay.set_redraw_callback(lambda: self._mark_dirty("composer_overlay"))
        bus = CommandBus()
        target_key = (target.session_id, target.attempt)
        history_key = f"{target.session_id}:{target.attempt}"
        history = self._composer_history.get(history_key)
        if history is None:
            history = list(self._input_history.get(target.session_id, []))
            self._composer_history[history_key] = history

        async def submit(command: SendMessageCommand) -> ManagerOperationResult:
            result = await self._submit_target_input(target, target_key, command)
            # Trigger-picker submissions dispatch through this handler from a
            # separate task rather than through `_dispatch_composer_key`'s
            # normal Enter path. Close on acceptance here as well, so an
            # accepted `/workflow ...` selection cannot leave a hidden-open
            # composer whose stale submitted flag consumes the next keystroke.
            if result.ok:
                self._close_input_composer()
            return result

        bus.register(SendMessageCommand, submit)
        editor = UnifiedInputSession(
            app_state=state,
            command_bus=bus,
            trigger_registry=triggers,
            mode_manager=modes,
            overlay_host=overlay,
            cwd=target_root,
            cfg=cfg,
            history=history,
            busy=True,
            clear_after_acceptance=True,
        )
        # Mode changes must be applied by the target owner. Until the target
        # context exposes a live mode command, keep the shift-tab capability
        # out of this manager-local editor rather than mutating a local-only
        # copy of the worker's mode.
        from agenthicc.tui.input.capabilities import IDLE_CAPABILITIES, ModeCycleCapability

        editor._capabilities = [
            capability
            for capability in IDLE_CAPABILITIES
            if not isinstance(capability, ModeCycleCapability)
        ]
        self._composer_target = target
        self._composer_state = state
        self._composer_input = editor
        self._composer_overlay = overlay
        self._composer_receipt = ""

    async def _submit_target_input(
        self,
        target: BackgroundSession,
        target_key: tuple[str, int],
        command: object,
    ) -> ManagerOperationResult:
        """Submit only to the immutable detail target captured by the editor."""
        from agenthicc.tui.runtime.commands import SendMessageCommand  # noqa: PLC0415

        if not isinstance(command, SendMessageCommand):
            return ManagerOperationResult(
                "", target.session_id, "failed", False, message="Invalid input"
            )
        current = await self._service.run_blocking(
            self.store.get,
            target_key[0],
            include_deleted=True,
        )
        if not isinstance(current, BackgroundSession):
            return ManagerOperationResult(
                "", target.session_id, "failed", False, message="Target session is unavailable"
            )
        if current.attempt != target.attempt or current.lease_token != target.lease_token:
            return ManagerOperationResult(
                "",
                target.session_id,
                "failed",
                False,
                category="stale_owner",
                message="Session owner changed; reopen details and retry",
            )
        self._composer_submitting = True
        self._composer_submitted = False
        self._mark_dirty("composer_submitting")
        try:
            if current.status == SessionStatus.WAITING_INPUT:
                result = await self._service.run_blocking(
                    self.supervisor.provide_input,
                    current.session_id,
                    command.text,
                    expected_attempt=target.attempt,
                    expected_lease_token=target.lease_token,
                )
                if not isinstance(result, BackgroundSession):
                    return ManagerOperationResult(
                        "",
                        target.session_id,
                        "failed",
                        False,
                        message="Question response was not accepted",
                    )
                self._composer_receipt = f"Answer submitted for {target.session_id[:12]}"
                accepted = ManagerOperationResult(
                    command.command_id, target.session_id, "accepted", True, value=result
                )
            else:
                accepted = await self._service.enqueue_input_async(
                    target.session_id,
                    command.text,
                    expected_attempt=target.attempt,
                    expected_lease_token=target.lease_token,
                    message_id=command.command_id,
                )
                if accepted.ok:
                    from agenthicc.background.input_inbox import BackgroundInput  # noqa: PLC0415

                    receipt = accepted.value
                    if isinstance(receipt, BackgroundInput) and receipt.deferred:
                        if receipt.recovery_error:
                            self._composer_receipt = (
                                "Input saved; background recovery needs attention · "
                                f"{command.command_id[:12]}"
                            )
                        else:
                            self._composer_receipt = (
                                f"Accepted for background recovery · {command.command_id[:12]}"
                            )
                    else:
                        self._composer_receipt = (
                            f"Queued for {target.session_id[:12]} · {command.command_id[:12]}"
                        )
            self._composer_submitted = accepted.ok
            if not accepted.ok:
                self._notice = accepted.message or "Input was rejected; draft retained"
            else:
                self._notice = self._composer_receipt
            return accepted
        finally:
            self._composer_submitting = False
            self._mark_dirty("composer_submission_finished")

    def _open_input_composer(self, target: BackgroundSession) -> None:
        recoverable = target.status in {
            SessionStatus.ORPHANED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.ARCHIVED,
        }
        live_owner = (
            target.status
            in {
                SessionStatus.STARTING,
                SessionStatus.RUNNING,
                SessionStatus.WAITING_APPROVAL,
                SessionStatus.WAITING_INPUT,
            }
            and target.worker_pid is not None
            and bool(target.lease_token)
        )
        if not (recoverable or live_owner):
            self._notice = f"Input unavailable while session is {target.status.value}"
            self._mark_dirty("composer_rejected")
            return
        # A previous accepted submission closes the modal asynchronously but
        # leaves its acceptance flag set. Reset that per-open state before the
        # next editor key is dispatched, or the first character in a reopened
        # composer is mistaken for the tail of the prior submission.
        self._composer_submitted = False
        self._composer_submitting = False
        same_target = (
            self._composer_target is not None
            and self._composer_target.session_id == target.session_id
            and self._composer_target.attempt == target.attempt
            and self._composer_target.lease_token == target.lease_token
            and self._composer_input is not None
        )
        if not same_target:
            self._composer_target = target
            if self._async_mode:
                self._composer_building = True
                self._composer_receipt = "Preparing target-scoped commands and mentions…"
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    self._composer_building = False
                    self._build_target_input_editor(target)
                else:
                    task = loop.create_task(
                        self._build_input_composer_async(target),
                        name=f"agenthicc-background-input-context-{target.session_id}",
                    )
                    self._composer_build_task = task
                    task.add_done_callback(_consume_task_result)
            else:
                try:
                    self._build_target_input_editor(target)
                except Exception as exc:  # noqa: BLE001
                    self._notice = (
                        f"Target input context unavailable: {type(exc).__name__}: {exc}"
                    )[:240]
                    self._mark_dirty("composer_context_failed")
                    return
        self._composer_open = True
        self._notice = ""
        self._mark_dirty("composer_opened")

    async def _build_input_composer_async(self, target: BackgroundSession) -> None:
        """Prepare the target's plugin/config context off the UI event loop."""
        try:
            await self._service.run_blocking(self._build_target_input_editor, target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._notice = (f"Target input context unavailable: {type(exc).__name__}: {exc}")[:240]
            self._composer_receipt = "Target context unavailable; draft keys retained"
            self._composer_open = False
            self._mark_dirty("composer_context_failed")
            return
        finally:
            self._composer_building = False
        editor = self._composer_input
        if editor is not None:
            from agenthicc.tui.cbreak_reader import Key  # noqa: PLC0415
            from agenthicc.tui.input.capabilities import _EXIT  # noqa: PLC0415

            pending, self._composer_pending_keys = self._composer_pending_keys, []
            for key, char in pending:
                if not isinstance(key, Key):
                    continue
                result = await editor._dispatch(key, char)
                if result is _EXIT or self._composer_submitted:
                    self._close_input_composer()
                    break
        self._mark_dirty("composer_context_ready")

    def _close_input_composer(self) -> None:
        self._composer_open = False
        if self._composer_overlay is not None and self._composer_overlay.active:
            self._composer_overlay.hide()
        self._mark_dirty("composer_closed")

    def _dispatch_composer_key(self, key: object, ch: str) -> None:
        if self._composer_input is None:
            if self._composer_building:
                if len(self._composer_pending_keys) < 1_024:
                    self._composer_pending_keys.append((key, ch))
                else:
                    self._composer_receipt = "Input context is still loading; key queue is full"
                    self._mark_dirty("composer_key_queue_full")
            return
        from agenthicc.tui.cbreak_reader import Key

        if not isinstance(key, Key):
            return

        async def dispatch() -> object:
            editor = self._composer_input
            if editor is None:
                return None
            result = await editor._dispatch(key, ch)
            from agenthicc.tui.input.capabilities import _EXIT  # noqa: PLC0415

            if result is _EXIT:
                self._close_input_composer()
            if self._composer_submitted:
                self._close_input_composer()
            self._mark_dirty("composer_key")
            return result

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(dispatch())
        else:
            existing = self._composer_task
            if existing is not None and not existing.done():
                return
            task = loop.create_task(dispatch(), name="agenthicc-background-input-key")
            self._composer_task = task
            task.add_done_callback(_consume_task_result)

    def _render_input_composer(self, budget: ViewportBudget) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from agenthicc.tui.workspace.components import ComposerComponent  # noqa: PLC0415

        target = self._composer_target
        state = self._composer_state
        overlay = self._composer_overlay
        editor = self._composer_input
        target_id = target.session_id if target is not None else "unknown"
        content: list[RenderableType] = []
        if overlay is not None and overlay.active:
            rendered_overlay = overlay.render()
            if rendered_overlay is not None:
                content.append(rendered_overlay)
        if state is not None:
            content.append(ComposerComponent(state).render())
        elif self._composer_building:
            content.append(Text("Loading target-scoped input context…", style="yellow"))
        if self._composer_submitting:
            content.append(Text("Submitting to session owner…", style="yellow"))
        elif self._composer_receipt:
            content.append(Text(self._composer_receipt, style="green"))
        if editor is not None and editor._mode.name == "IDLE":
            content.append(Text("Ctrl+J newline · Esc back · Enter send", style="dim"))
        content.append(Text("Shift+Tab mode switching is unavailable from agents", style="dim"))
        return Group(
            Text(
                f"Background Session · {self._workspace_name(target.cwd) if target else ''}",
                style="bold cyan",
            ),
            Panel(
                Group(*content),
                title=f"Send input · target {target_id}",
                height=max(5, budget.height - 2),
                padding=(0, 1),
                expand=True,
            ),
        )

    def render(self, *, all_sessions: bool = False) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from rich import box  # noqa: PLC0415

        frame_started = time.monotonic()
        self._record_metric("frame_requested")
        all_records = self._visible_sessions if self._async_mode else self.refresh()
        budget = self.viewport_budget
        if self.help_visible:
            help_text = (
                "↑/k ↓/j select   PgUp/PgDn page   Home/End first/last\n"
                "Enter details   Enter again to attach   r refresh   ? close help\n"
                "c cancel   a archive   Ctrl+X delete\n"
                "[ ] scroll details   / filter   v mark   C/A bulk action   i input\n"
                "Esc backs out of details; q quits without stopping workers"
            )
            if budget.compact:
                self._dirty_causes.clear()
                return Text("? help  ↑↓ select  Enter details  q quit")
            rendered_help = Panel(
                help_text,
                title="Background Sessions — Keyboard Help",
                border_style="cyan",
                height=min(budget.height, 8),
            )
            self._dirty_causes.clear()
            self._record_metric("frame_rendered", time.monotonic() - frame_started)
            return rendered_help
        start, end = self._page_bounds()
        sessions = all_records if all_sessions or self._async_mode else all_records[start:end]
        total_count = self._total_count if self._async_mode else len(all_records)
        page_number = start // self.page_size + 1
        if all_sessions:
            page_label = f"all · {total_count} sessions"
            range_label = f"showing 1–{len(all_records)} of {total_count}"
        elif self._async_mode and not self._projection_ready and self._projection_error:
            page_label = "session index failed"
        elif self._async_mode and not self._projection_ready:
            started_at = self._projection_started_at or time.monotonic()
            elapsed = max(0.0, time.monotonic() - started_at)
            page_label = f"loading session index… {elapsed:.0f}s"
        elif self._async_mode and self._projection_stale:
            page_label = f"page {page_number}/{self.page_count} · refreshing…"
        elif total_count:
            visible_end = min(start + len(sessions), total_count)
            range_label = f"showing {start + 1}–{visible_end} of {total_count}"
            page_label = f"page {page_number}/{self.page_count} · {range_label}"
        else:
            range_label = "showing 0–0 of 0"
            page_label = f"page 1/1 · {range_label}"

        selected = self.selected_session
        if not self._async_mode and selected is not None and not selected.error:
            # Compatibility callers expect activity in a direct render. The
            # interactive path schedules this bounded read off-loop instead.
            self._activity_lines(selected)
        key = self._render_key(
            all_sessions=all_sessions,
            budget=budget,
            start=start,
            end=end,
            selected=selected,
        )
        if key == self._last_render_key and self._last_renderable is not None:
            self._dirty_causes.clear()
            self._record_metric("frame_rendered", time.monotonic() - frame_started)
            return self._last_renderable

        if self._detail_session_id is not None:
            detail_records = self._visible_sessions if self._async_mode else self._sessions
            detail_session = next(
                (item for item in detail_records if item.session_id == self._detail_session_id),
                None,
            )
            rendered_detail = (
                self._render_input_composer(budget)
                if self._composer_open
                else self._render_detail_page(detail_session, budget)
            )
            self._last_render_key, self._last_renderable = key, rendered_detail
            self._dirty_causes.clear()
            self._record_metric("frame_rendered", time.monotonic() - frame_started)
            return rendered_detail

        if budget.compact:
            compact_title = Text(f"Background Sessions · {page_label}", style="bold cyan")
            if selected is None:
                if self._async_mode and not self._projection_ready and self._projection_error:
                    label = "Session index failed — press r to retry"
                elif self._async_mode and not self._projection_ready:
                    label = "Loading sessions…"
                else:
                    label = "No background sessions"
                row = Text(label)
                compact_detail = Text(
                    self._projection_error[: max(1, budget.width - 1)]
                    if self._projection_error
                    else "No selected session"
                )
            else:
                row = Text(
                    f"▸ {selected.status.value}  {selected.title[: max(1, budget.width - 24)]}"
                )
                compact_detail = Text(f"{selected.session_id}  {selected.current_phase or '—'}")
            footer = Text("↑↓ select  Enter details  ? help  q/Esc quit", style="dim")
            rendered: RenderableType = Group(compact_title, row, compact_detail, footer)
            self._last_render_key, self._last_renderable = key, rendered
            self._dirty_causes.clear()
            self._record_metric("frame_rendered", time.monotonic() - frame_started)
            return rendered

        header = Text(f"Background Sessions · {page_label}", style="bold cyan")
        table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), show_edge=False)
        table.add_column("", width=2)
        table.add_column("State", no_wrap=True, overflow="ellipsis")
        table.add_column("Title", no_wrap=True, overflow="ellipsis")
        table.add_column("Workflow", no_wrap=True, overflow="ellipsis")
        table.add_column("WS", no_wrap=True, overflow="ellipsis")
        table.add_column("Mode", no_wrap=True, overflow="ellipsis")
        table.add_column("Activity", no_wrap=True, overflow="ellipsis")
        if not sessions:
            if self._async_mode and not self._projection_ready and self._projection_error:
                empty_title = "Session index failed to load — press r to retry"
            elif self._async_mode and not self._projection_ready:
                empty_title = "Loading sessions…"
            else:
                empty_title = "No background sessions"
            table.add_row(
                "",
                "—",
                empty_title,
                "",
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
            if session.error and session.status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
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
                self._mode_label(session),
                activity,
            )
        detail_lines: list[str] = []
        if self._projection_error and not self._projection_ready:
            detail_lines.extend(
                [
                    f"[red]Session index failed[/red] {self._projection_error[:160]}",
                    "[dim]Press r to retry; automatic retries use backoff.[/dim]",
                ]
            )
        if selected is not None:
            detail_lines.extend(
                [
                    f"[bold]ID[/bold] {selected.session_id}",
                    f"[bold]Dir:[/bold] {self._workspace_name(selected.cwd)}",
                    f"[bold]State[/bold] [{self._status_style(selected.status)}]{selected.status.value}[/]"
                    f"  [bold]Phase[/bold] {selected.current_phase or '—'}",
                    "[bold]Updated[/bold] "
                    + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(selected.last_active)),
                ]
            )
            if selected.phase_history:
                history = " → ".join(selected.phase_history[-4:])
                detail_lines[-1] += f" · History {history[: max(8, budget.width - 56)]}"
            # Activity is deliberately one physical line, and Updated is
            # placed before it. A long/multiline agent response must never
            # push stable session metadata below the details-panel viewport.
            content_capacity = max(1, budget.detail_height - 2)
            if selected.heartbeat_stale and len(detail_lines) < content_capacity:
                detail_lines.append(
                    "[yellow]Heartbeat delayed; worker is not yet classified as dead[/yellow]"
                )
            current_error = (
                selected.error
                if selected.status in {SessionStatus.FAILED, SessionStatus.CANCELLED}
                else None
            )
            activity_lines: list[str] = []
            if current_error and len(detail_lines) < content_capacity:
                detail_lines.append(f"[red]Error[/red] {current_error[:120]}")
            else:
                if self._async_mode:
                    cached_activity = self._activity_text.get(selected.session_id)
                    activity_lines = [cached_activity] if cached_activity else []
                else:
                    activity_lines = self._activity_lines(selected)
                if activity_lines and len(detail_lines) < content_capacity:
                    latest = " ".join(activity_lines[0].split())
                    # The detail panel is narrower than the terminal due to
                    # borders and padding; reserve room for the label and
                    # always crop rather than wrapping the transcript text.
                    from rich.markup import escape  # noqa: PLC0415

                    legacy_error = (
                        " ".join(selected.error.split())[: max(8, min(48, budget.width // 3))]
                        if selected.error and selected.status in ACTIVE_STATUSES
                        else ""
                    )
                    if legacy_error and not (
                        selected.attempt_history
                        and selected.attempt_history[-1].attempt < selected.attempt
                    ):
                        suffix = f" · Legacy prior error: {legacy_error}"
                        latest = latest[: max(1, budget.width - 32 - len(suffix))] + suffix
                    else:
                        latest = latest[: max(1, budget.width - 32)]
                    detail_lines.append("[bold]Latest text[/bold] " + escape(latest))
                elif len(detail_lines) < content_capacity:
                    detail_lines.append("[dim]Latest text: no text activity[/dim]")
            if (
                selected.attempt_history
                and selected.attempt_history[-1].attempt < selected.attempt
                and len(detail_lines) < content_capacity
            ):
                previous = selected.attempt_history[-1]
                previous_text = previous.error or previous.latest_activity or "No error details"
                previous_text = " ".join(previous_text.split())[: max(1, budget.width - 42)]
                from rich.markup import escape  # noqa: PLC0415

                detail_lines.append(
                    f"[dim]Previous attempt {previous.attempt} ({previous.status.value}): "
                    f"{escape(previous_text)}[/dim]"
                )
            elif (
                selected.error
                and selected.status in ACTIVE_STATUSES
                and not activity_lines
                and len(detail_lines) < content_capacity
            ):
                from rich.markup import escape  # noqa: PLC0415

                legacy_error = " ".join(selected.error.split())[: max(1, budget.width - 40)]
                detail_lines.append(f"[dim]Legacy prior error: {escape(legacy_error)}[/dim]")
            if (
                selected.session_id in self._seen_activity
                and selected.last_active > self._seen_activity[selected.session_id]
                and len(detail_lines) < content_capacity
            ):
                detail_lines.append("[yellow]New activity available[/yellow]")
        if self._deleting_ids:
            detail_lines.append(
                f"[yellow]Deleting {len(self._deleting_ids)} session(s): "
                f"{self._deletion_phase or 'starting'}…[/yellow]"
            )
        if self._deletion_error:
            detail_lines.append(f"[red]Delete failed[/red] {self._deletion_error[:240]}")
        duplicate_index_error = (
            not self._projection_ready
            and self._projection_error
            and self._notice.startswith("Session index failed to load:")
        )
        if self._notice and not duplicate_index_error:
            detail_lines.append(f"[yellow]{self._notice}[/yellow]")
        detail = Panel(
            "\n".join(detail_lines) or "Select a session to inspect it.",
            title="Details · selected" if selected is not None else "Details",
            height=budget.detail_height,
            padding=(0, 1),
            expand=True,
        )
        footer_text = (
            "Deleting…  Ctrl+C exit"
            if self._deleting_ids
            else "↑/k ↓/j select  PgUp/PgDn page  Home/End  Enter details  r refresh  "
            "c cancel  v mark  Ctrl+X delete  ? help  q/Esc quit"
        )
        footer = Text(footer_text, style="dim")
        if self.new_activity:
            footer.append("  • new activity", style="yellow")
        rendered = Group(header, table, detail, footer)
        self._last_render_key, self._last_renderable = key, rendered
        self._dirty_causes.clear()
        self._record_metric("frame_rendered", time.monotonic() - frame_started)
        return rendered

    def _delete_sessions(
        self, ids: tuple[str, ...], *, operation_id: str = ""
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        deleted: list[str] = []
        errors: list[str] = []
        for session_id in ids:
            try:
                if operation_id:
                    try:
                        self.supervisor.delete(session_id, operation_id=operation_id)
                    except TypeError as exc:
                        # Preserve compatibility with older/custom supervisors
                        # that only expose delete(session_id). Do not mask a
                        # TypeError raised by the operation itself.
                        if "operation_id" not in str(exc):
                            raise
                        self.supervisor.delete(session_id)
                else:
                    self.supervisor.delete(session_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{session_id}: {type(exc).__name__}: {exc}")
            else:
                deleted.append(session_id)
        return tuple(deleted), tuple(errors)

    def _delete_sessions_result(self, ids: tuple[str, ...], operation_id: str) -> DeleteResult:
        deleted, errors = self._delete_sessions(ids, operation_id=operation_id)
        failures = tuple(
            DeleteFailure(
                session_id=error.split(":", 1)[0],
                code="delete_failed",
                message=error,
            )
            for error in errors
        )
        return DeleteResult(operation_id=operation_id, deleted=deleted, failures=failures)

    async def _on_delete_progress(self, _session_id: str, phase: str) -> None:
        self._deletion_phase = phase
        self._mark_dirty("operation_changed")

    async def _run_delete_operation(self, ids: tuple[str, ...], operation_id: str) -> DeleteResult:
        return await self._service.delete_async(
            ids,
            operation_id=operation_id,
            requested_by="agents",
            progress=self._on_delete_progress,
        )

    def _start_async_delete(self, ids: tuple[str, ...]) -> None:
        if self._deletion_task is not None or self._deletion_future is not None:
            return
        self._deleting_ids = ids
        self._deletion_operation_id = uuid.uuid4().hex
        self._deletion_phase = "starting"
        self._deletion_error = ""
        self._mark_dirty("operation_changed")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Compatibility for direct synchronous handle_key() tests and
            # plugins. This path is never used by BackgroundManager.run().
            self._deletion_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="agenthicc-background-delete"
            )
            self._deletion_future = self._deletion_executor.submit(
                self._delete_sessions_result, ids, self._deletion_operation_id
            )
        else:
            self._deletion_task = loop.create_task(
                self._run_delete_operation(ids, self._deletion_operation_id),
                name=f"agenthicc-delete-{self._deletion_operation_id}",
            )

    def _poll_async_delete(self) -> None:
        task = self._deletion_task
        future = self._deletion_future
        if task is not None and not task.done():
            return
        if future is not None and not future.done():
            return
        if task is None and future is None:
            return
        try:
            if task is not None:
                result = task.result()
            else:
                assert future is not None
                result = future.result()
        except asyncio.CancelledError:
            result = DeleteResult(
                operation_id=self._deletion_operation_id,
                failures=(
                    DeleteFailure(
                        session_id=self._deleting_ids[0] if self._deleting_ids else "",
                        code="cancelled",
                        message="Deletion task was cancelled",
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            result = DeleteResult(
                operation_id=self._deletion_operation_id,
                failures=(
                    DeleteFailure(
                        session_id=self._deleting_ids[0] if self._deleting_ids else "",
                        code="delete_failed",
                        message=f"{type(exc).__name__}: {exc}",
                    ),
                ),
            )
        self._deletion_task = None
        self._deletion_future = None
        self._deleting_ids = ()
        self._deletion_phase = result.phase
        self.marked_ids.difference_update(result.deleted)
        if result.failures:
            self._deletion_error = "; ".join(
                f"{failure.session_id}: {failure.message}" for failure in result.failures
            )[:2_000]
            self.console.print("Delete failed: " + self._deletion_error)
        else:
            self._deletion_error = ""
            self.console.print(
                f"Deleted {len(result.deleted)} session(s) to recoverable trash "
                f"(operation {result.operation_id[:12]})."
            )
        self._deletion_operation_id = ""
        self._mark_dirty("projection_changed")
        if self._async_mode:
            self._request_async_refresh("delete_complete", force=True)
        else:
            self.refresh(force=True)
        if self._deletion_executor is not None:
            self._deletion_executor.shutdown(wait=False, cancel_futures=True)
            self._deletion_executor = None

    async def _shutdown_delete(self) -> None:
        """Briefly drain an owned deletion before leaving the TUI.

        Deletion requests are durable and recoverable, so closing the manager
        must not wait seconds for a slow filesystem operation to finish.
        """

        task = self._deletion_task
        if task is None:
            self._poll_async_delete()
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
        except asyncio.TimeoutError:
            # The supervisor claims the exact target before moving artifacts.
            # If a legacy filesystem call cannot be interrupted, cancellation
            # leaves the durable request for the next manager-open recovery.
            task.cancel()
            self._deletion_error = "Deletion continues in recovery state after manager shutdown"
        except asyncio.CancelledError:
            task.cancel()
        finally:
            self._poll_async_delete()

    def handle_key(self, key: object, ch: str = "") -> ManagerResult | None:
        """Handle one logical terminal key; exposed for deterministic TUI tests."""

        value = _key_value(key)
        # The raw terminal backend delivers Ctrl+C as CTRL_C (signals are
        # disabled while the manager owns cbreak mode).  Handle it before any
        # modal state so it always exits the agents screen, including while a
        # filter or deletion is in progress.
        if value == "CTRL_C" or ch == "\x03":
            return ManagerResult("exit")
        self._mark_dirty("input")
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
        if self._composer_open:
            if value == "ESC" and not (
                self._composer_overlay is not None and self._composer_overlay.active
            ):
                self._close_input_composer()
                return None
            if self._composer_submitting:
                return None
            self._dispatch_composer_key(key, ch)
            return None
        # Detail navigation is a separate viewport. Consume vertical keys
        # here, before the list-selection handlers below, so scrolling cannot
        # close details or silently move the selected target.
        if self._detail_session_id is not None:
            scroll_step = max(1, self.viewport_budget.height - 8)
            if value == "HOME":
                self._detail_scroll = 0
                self._request_render("detail_scrolled")
                return None
            if value == "END":
                # Rendering clamps this sentinel to the last valid viewport.
                self._detail_scroll += 1_000_000
                self._request_render("detail_scrolled")
                return None
            if value == "UP" or (value == "CHAR" and ch.lower() == "k"):
                self._detail_scroll = max(0, self._detail_scroll - 1)
                self._request_render("detail_scrolled")
                return None
            if value == "DOWN" or (value == "CHAR" and ch.lower() == "j"):
                self._detail_scroll += 1
                self._request_render("detail_scrolled")
                return None
            if value in {"PAGE_UP", "PAGEUP"}:
                self._detail_scroll = max(0, self._detail_scroll - scroll_step)
                self._request_render("detail_scrolled")
                return None
            if value in {"PAGE_DOWN", "PAGEDOWN"}:
                self._detail_scroll += scroll_step
                self._request_render("detail_scrolled")
                return None
            if ch == "[":
                self._detail_scroll = max(0, self._detail_scroll - 6)
                self._request_render("detail_scrolled")
                return None
            if ch == "]":
                self._detail_scroll += 6
                self._request_render("detail_scrolled")
                return None
        if value in {"UP", "CHAR"} and (value == "UP" or ch.lower() == "k"):
            if not self._async_mode:
                self.refresh()
            self._move_selection(max(0, self.selected - 1))
            return None
        if value in {"DOWN", "CHAR"} and (value == "DOWN" or ch.lower() == "j"):
            if not self._async_mode:
                self.refresh()
            total = self._total_count if self._async_mode else len(self._sessions)
            self._move_selection(min(max(0, total - 1), self.selected + 1))
            return None
        if value == "HOME":
            if not self._async_mode:
                self.refresh()
            self._move_selection(0)
            self.activity_offset = 0
            return None
        if value == "END":
            if not self._async_mode:
                self.refresh()
            total = self._total_count if self._async_mode else len(self._sessions)
            self._move_selection(max(0, total - 1))
            self.activity_offset = 0
            return None
        if value == "CTRL_X" or ch == "\x18":
            if self._deleting_ids:
                return None
            selected = self.selected_session
            if self._async_mode:
                ids = tuple(sorted(self.marked_ids))
                if not ids and selected is not None:
                    ids = (selected.session_id,)
            else:
                marked = self.marked_sessions()
                targets = marked if marked else ([selected] if selected is not None else [])
                ids = tuple(dict.fromkeys(item.session_id for item in targets))
            if ids:
                self._deletion_error = ""
                self._mark_dirty("operation_changed")
                self._start_async_delete(ids)
            return None
        if self._detail_session_id is not None and ch.lower() == "i":
            records = self._visible_sessions if self._async_mode else self._sessions
            target = next(
                (item for item in records if item.session_id == self._detail_session_id),
                None,
            )
            if target is None:
                self._notice = "Session details are stale; refresh and reopen the session"
                self._mark_dirty("composer_target_missing")
            else:
                self._open_input_composer(target)
            return None
        if value == "ENTER":
            # Enter is deliberately two-step: first inspect the selected
            # record, then attach from its detail page. Reconcile before both
            # steps so a disappearing row can never retarget the action.
            if not self._async_mode:
                self.refresh(force=True)
            selected = self.selected_session
            if self._detail_session_id is not None:
                if selected is None or selected.session_id != self._detail_session_id:
                    self._notice = "Selected session changed; return to the list and choose again"
                    self._mark_dirty("notice_changed")
                    return None
                self.mark_selected_seen()
                return ManagerResult("attach", selected.session_id)
            if selected is None:
                return None
            self._detail_session_id = selected.session_id
            self._detail_scroll = 0
            self.mark_selected_seen()
            self._schedule_activity_read()
            self._request_render("detail_opened")
            return None
        if value in {"PAGE_UP", "PAGEUP"}:
            if not self._async_mode:
                self.refresh()
            self._move_selection(max(0, self.selected - self.page_size))
            self.activity_offset = 0
            return None
        if ch == "[":
            self.activity_offset += 6
            return None
        if value in {"PAGE_DOWN", "PAGEDOWN"}:
            if not self._async_mode:
                self.refresh()
            total = self._total_count if self._async_mode else len(self._sessions)
            self._move_selection(min(max(0, total - 1), self.selected + self.page_size))
            self.activity_offset = 0
            return None
        if ch == "]":
            self.activity_offset = max(0, self.activity_offset - 6)
            return None
        if value == "ESC" and self._detail_session_id is not None:
            self._detail_session_id = None
            self._detail_scroll = 0
            self._request_render("detail_closed")
            return None
        if value == "ESC" or ch.lower() == "q":
            return ManagerResult("exit")
        if ch == "?":
            self.help_visible = not self.help_visible
            return None
        if ch.lower() == "r":
            if self._async_mode:
                self._request_async_refresh("manual", force=True)
                if self._projection_ready:
                    self._schedule_maintenance(
                        force=True,
                        name="agenthicc-background-manual-maintenance",
                    )
                self._notice = (
                    "Refresh requested"
                    if self._projection_ready
                    else "Session index retry requested"
                )
                self._mark_dirty("notice_changed")
            else:
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
        selected = self.selected_session
        if selected is None:
            return None
        if ch.lower() in {"y", "n"} and selected.status == SessionStatus.WAITING_APPROVAL:
            if self._async_mode:
                self._start_operation(
                    selected.session_id,
                    "approval",
                    lambda: self._service.approve_async(selected.session_id, ch.lower() == "y"),
                )
            else:
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
            elif self._async_mode:
                request = selected.input_request

                async def provide() -> ManagerOperationResult:
                    if self.input_provider is None:
                        return ManagerOperationResult(
                            "",
                            selected.session_id,
                            "failed",
                            False,
                            message="input provider unavailable",
                        )
                    value = await self._service.run_blocking(self.input_provider, request)
                    return await self._service.provide_input_async(selected.session_id, str(value))

                self._start_operation(selected.session_id, "input", provide)
            else:
                try:
                    self.supervisor.provide_input(
                        selected.session_id, self.input_provider(selected.input_request)
                    )
                except Exception as exc:  # noqa: BLE001
                    self.console.print(f"Input failed: {type(exc).__name__}: {exc}")
            if not self._async_mode:
                self.refresh(force=True)
            return None
        if ch.lower() == "c":
            if self._async_mode:
                self._start_operation(
                    selected.session_id,
                    "cancel",
                    lambda: self._service.cancel_async(selected.session_id),
                )
            else:
                try:
                    self.supervisor.cancel(selected.session_id)
                except Exception as exc:  # noqa: BLE001
                    self.console.print(f"Cancel failed: {type(exc).__name__}: {exc}")
                self.refresh(force=True)
        elif ch.lower() == "a":
            if self._async_mode:
                self._start_operation(
                    selected.session_id,
                    "archive",
                    lambda: self._service.archive_async(selected.session_id),
                )
            else:
                try:
                    self.supervisor.archive(selected.session_id)
                except Exception as exc:  # noqa: BLE001
                    self.console.print(f"Archive failed: {type(exc).__name__}: {exc}")
                self.refresh(force=True)
        elif ch.lower() == "p":
            if self._async_mode:
                self._start_operation(
                    selected.session_id,
                    "pin",
                    lambda: self._service.pin_async(selected.session_id, not selected.pinned),
                )
            else:
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

        manager_opened_at = time.monotonic()
        self._record_metric("manager_open")
        backend = get_backend()
        if not backend.is_interactive():
            # A redirected manager is a diagnostic listing, not a viewport;
            # retain the historical behavior of printing every record.
            self.console.print(self.render(all_sessions=True))
            return ManagerResult("exit")
        self._async_mode = True
        self._operation_semaphore = asyncio.Semaphore(
            self.manager_settings.max_in_flight_operations
        )
        self._projection_ready = False
        self._projection_started_at = time.monotonic()
        self._record_metric("first_frame_requested")
        first_frame_started = time.monotonic()
        initial = self.render()
        read_future: asyncio.Future[tuple[object, str]] | None = None
        read_finished = threading.Event()
        self._request_async_refresh("manager_open", force=True)
        # Do not race cold projection construction with stale-worker recovery:
        # both consult the same durable index, and the manager should get its
        # first usable session page before maintenance competes for I/O/CPU.
        maintenance_driver: asyncio.Task[list[BackgroundSession]] | None = None
        try:
            with Live(initial, console=self.console, auto_refresh=False) as live:
                live.refresh()
                self._record_metric("first_frame_rendered", time.monotonic() - manager_opened_at)
                self._record_metric(
                    "first_frame_render_cost", time.monotonic() - first_frame_started
                )
                with backend.enter_raw_mode():
                    loop = asyncio.get_running_loop()

                    def blocking_read_key() -> tuple[object, str]:
                        try:
                            return backend.read_key()
                        finally:
                            read_finished.set()

                    read_finished.clear()
                    read_future = loop.run_in_executor(None, blocking_read_key)
                    last_maintenance_request = time.monotonic()
                    while True:
                        done, _ = await asyncio.wait({read_future}, timeout=self.refresh_s)
                        if read_future in done:
                            key, ch = read_future.result()
                            key_received_at = time.monotonic()
                            self._last_key_received_at = key_received_at
                            self._record_metric("key_received")
                            result = self.handle_key(key, ch)
                            self._poll_async_delete()
                            if result is not None:
                                if result.action == "attach" and result.session_id:
                                    attach_target = result.session_id

                                    async def prepare_attach() -> ManagerOperationResult:
                                        return await self._service.attach_prepare_async(
                                            attach_target
                                        )

                                    self._start_operation(
                                        attach_target,
                                        "attach",
                                        prepare_attach,
                                    )
                                else:
                                    return result
                            read_finished.clear()
                            read_future = loop.run_in_executor(None, blocking_read_key)

                        if self._attach_result is not None:
                            return self._attach_result

                        self._poll_async_delete()
                        self._request_async_refresh("poll")
                        now = time.monotonic()
                        if self._projection_ready and maintenance_driver is None:
                            maintenance_driver = self._schedule_maintenance(
                                force=True,
                                name="agenthicc-background-maintenance-startup",
                            )
                            last_maintenance_request = now
                        elif (
                            self._projection_ready
                            and now - last_maintenance_request >= self.maintenance_s
                        ):
                            if self._maintenance_task is None or self._maintenance_task.done():
                                self._schedule_maintenance(
                                    force=False,
                                    name="agenthicc-background-maintenance",
                                )
                                last_maintenance_request = now
                        if self._dirty_causes:
                            if self.manager_settings.frame_debounce_ms:
                                await asyncio.sleep(
                                    self.manager_settings.frame_debounce_ms / 1_000.0
                                )
                                if read_future is not None and read_future.done():
                                    continue
                            rendered = self.render()
                            if rendered is not initial:
                                live.update(rendered, refresh=True)
                                if self._last_key_received_at is not None:
                                    self._record_metric(
                                        "key_to_frame",
                                        time.monotonic() - self._last_key_received_at,
                                    )
                                    self._last_key_received_at = None
                                initial = rendered
        finally:
            # POSIX read() is not cancellable by cancelling its asyncio Future.
            # Signal the backend first so a worker blocked in terminal input
            # exits rather than holding up the event loop's executor shutdown.
            try:
                backend.restore()
            except Exception:  # noqa: BLE001
                pass
            read_was_pending = read_future is not None and not read_future.done()
            if read_future is not None and read_was_pending:
                read_future.cancel()
                deadline = time.monotonic() + 0.25
                while not read_finished.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.005)
            if read_future is None or read_finished.is_set() or not read_was_pending:
                close_backend = getattr(backend, "close", None)
                if callable(close_backend):
                    close_backend()
            await self._shutdown_delete()
            owned_tasks = [
                task
                for task in (
                    self._refresh_task,
                    self._maintenance_task,
                    self._activity_task,
                    self._composer_build_task,
                    self._deletion_task,
                    *self._operation_tasks.values(),
                    maintenance_driver,
                )
                if task is not None and task is not asyncio.current_task() and not task.done()
            ]
            for task in owned_tasks:
                task.cancel()
            if owned_tasks:
                completed, pending = await asyncio.wait(owned_tasks, timeout=0.25)
                for task in completed:
                    if not task.cancelled():
                        try:
                            task.exception()
                        except Exception:  # noqa: BLE001
                            pass
                # These operations are persisted independently. Do not let a
                # slow or cancellation-resistant service operation keep the
                # interactive manager open; consume its eventual result.
                for task in pending:
                    task.add_done_callback(_consume_task_result)
            if self._owns_service:
                await self._service.close()
            if self._deletion_executor is not None:
                self._deletion_executor.shutdown(wait=False, cancel_futures=True)
                self._deletion_executor = None


async def run_background_manager(
    console: Console,
    *,
    store: BackgroundStore | None = None,
    supervisor: BackgroundSupervisor | None = None,
    manager_settings: BackgroundManagerSettings | None = None,
) -> ManagerResult:
    """Convenience entry point used by CLI aliases and tests."""

    return await BackgroundManager(
        console,
        store=store,
        supervisor=supervisor,
        manager_settings=manager_settings,
    ).run()
