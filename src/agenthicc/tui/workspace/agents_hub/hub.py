"""The agents hub: one surface for running work, attention, history and the library.

:class:`AgentsHub` is the single component behind ``agenthicc agents`` (full
screen) and ``/agents``, Ctrl+G or ← on an empty prompt (an overlay over the
conversation) — AGX-05. Its list is AGX-06's status-first, grouped and
searchable list; Enter opens AGX-07's detail view and Space its peek panel.

The hub never blocks the event loop: registry queries, inbox reads, journal
tails and ``git diff`` run on :class:`~agenthicc.background.AgentsService`'s
bounded executor, and an update — a registry or inbox change, a worker exit, a
finished operation — wakes the engine (AGX-04) instead of a polling timer.
Keys resolve through the key map's ``agents.list``, ``agents.peek`` and
``agents.detail`` contexts (AGX-13), so dispatch, footer hints and ``?`` help
come from one registry.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar, cast

from agenthicc.agents.definitions import (
    AgentSpec,
    DefinitionError,
    parse_definition,
    render_definition,
)
from agenthicc.agents.library import (
    DefinitionScope,
    agent_directory,
    delete_definition,
    write_definition,
)
from agenthicc.agents.selection import project_default, set_project_default
from agenthicc.background.agents_service import AgentsService, ManagerOperationResult
from agenthicc.background.deletion import DeleteFailure, DeleteResult
from agenthicc.background.inbox import BackgroundInboxItem, response_from_text
from agenthicc.background.index import SessionFilter
from agenthicc.background.maintenance import MaintenanceOutcome, MaintenanceTrigger
from agenthicc.background.model import ACTIVE_STATUSES, BackgroundSession, SessionStatus
from agenthicc.background.settings import AgentsHubSettings
from agenthicc.background.store import BackgroundStore
from agenthicc.background.supervisor import BackgroundSupervisor
from agenthicc.tui.cbreak_reader import Key, MouseEvent, key_name
from agenthicc.tui.keymap import Dispatcher, Hint, Keymap
from agenthicc.tui.workspace.agents_hub.detail import (
    TRANSCRIPT_EVENTS,
    DetailData,
    changes_summary,
    journal_fingerprint,
    journal_path,
    read_transcript,
)
from agenthicc.tui.workspace.agents_hub.editor import EditResult, LineEditor
from agenthicc.tui.workspace.agents_hub.model import (
    FINISHED_STATUSES,
    HISTORY_STATUSES,
    NEEDS_YOU_STATUSES,
    RECENT_WINDOW_S,
    RUNNING_TAB_STATUSES,
    HubRow,
    LibraryEntry,
    RowKind,
    SavedSession,
    ScheduledEntry,
    Search,
    Tab,
    history_rows,
    library_rows,
    needs_you_rows,
    parse_search,
    running_rows,
)
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs
from agenthicc.tui.workspace.agents_hub.sources import HubSources
from agenthicc.worktrees.environment import EnvironmentCommand, EnvironmentRunReceipt

if TYPE_CHECKING:
    from rich.console import Console, RenderableType
    from rich.text import Text

    from agenthicc.background.watch import FileWatcher, WatchChanges
    from agenthicc.config import UISettings
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.terminal.backend import TerminalBackend
    from agenthicc.tui.workspace.live_region import LiveRegion
    from agenthicc.tui.workspace.overlay import OverlayHost

__all__ = [
    "AgentsHub",
    "DispatchDefaults",
    "DispatchRequest",
    "HubResult",
    "parse_dispatch",
    "run_agents_hub",
]

_T = TypeVar("_T")

#: A maintenance pass prompted only by a registry or inbox change waits at least
#: this long after the previous pass; worker exits and refreshes do not.
MAINTENANCE_COALESCE_S = 1.0
#: While the first load runs the screen shows elapsed time and index progress.
LOADING_FRAME_S = 0.25
#: History rows read per window; scrolling near the end reads the next one.
HISTORY_CHUNK = 200
#: The first maintenance pass and the side loads (schedules, saved sessions,
#: definitions) start this long after the first rows, so the frame showing them
#: is drawn before their worker threads compete for the interpreter.
SIDE_LOAD_DELAY_S = 0.1
#: Loads the hub starts on its own, which respect a failed load's backoff.
_AUTOMATIC_CAUSES = frozenset({"registry_changed", "coalesced", "retry", "inbox_changed"})
#: Statuses ``r`` resumes (PRD-219).
_RESUMABLE = frozenset(
    {
        SessionStatus.ORPHANED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.ARCHIVED,
    }
)
_RETRYABLE = frozenset({SessionStatus.FAILED, SessionStatus.CANCELLED})
#: A worktree agent's change summary (``git diff``) is re-read at most this often.
CHANGES_INTERVAL_S = 5.0
#: A second click on the same row within this long opens it.
_DOUBLE_CLICK_S = 0.4
_TABS = tuple(Tab)


@dataclass(frozen=True)
class HubResult:
    """Why the hub handed control back to its caller."""

    action: Literal["exit", "attach", "open_saved", "subagent"]
    #: The session to attach or open; for ``subagent``, ``POOL_ID:WORKER_ID``.
    session_id: str | None = None


def _consume_task_result(task: asyncio.Task[object]) -> None:
    """Retrieve a hub-owned task's eventual exception after a bounded exit."""
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:  # noqa: BLE001
        pass


def _read_library_definition(path: Path, scope: DefinitionScope, project_root: Path) -> str:
    """Read a definition only when it is a regular file inside its scope."""
    expected = agent_directory(scope, project_root=project_root).resolve()
    if path.is_symlink() or path.parent.resolve() != expected or not path.is_file():
        raise ValueError("the definition path is not a regular file in its agent directory")
    return path.read_text(encoding="utf-8")


@dataclass
class _Prompt:
    """A one-line editor at the bottom of the hub, and what Enter does with it."""

    kind: Literal[
        "search", "dispatch", "rename", "labels", "priority", "answer", "feedback", "library"
    ]
    label: str
    editor: LineEditor
    target: str = ""
    item: BackgroundInboxItem | None = None


@dataclass
class _LibraryDraft:
    """Values collected by the Library's guided definition form."""

    operation: Literal["create", "duplicate", "generate"]
    scope: DefinitionScope = "project"
    source: AgentSpec | None = None
    name: str = ""
    description: str = ""
    mode: str = "all"
    model: str = ""
    tools: tuple[str, ...] | None = None
    prompt: str = ""


@dataclass
class _Choice:
    """A choice between a few options (an approval mode, switching sessions)."""

    kind: Literal[
        "approve_mode",
        "switch",
        "priority",
        "trust",
        "library_scope",
        "library_save",
        "library_delete",
    ]
    label: str
    options: tuple[str, ...]
    index: int = 0
    target: str = ""
    item: BackgroundInboxItem | None = None
    result: HubResult | None = None


@dataclass(frozen=True)
class DispatchDefaults:
    """How agents started from the hub's dispatch line are configured.

    The values ``agenthicc run --background`` would pass from its command
    line: the configuration file and ``--set`` overrides of the caller.
    """

    config_path: str | None = None
    set_overrides: tuple[str, ...] = ()
    set_secret_overrides: tuple[str, ...] = ()
    dangerously_skip_permissions: bool = False
    mode_name: str | None = None


@dataclass(frozen=True)
class DispatchRequest:
    """One parsed dispatch line (AGX-05 §5.3)."""

    intent: str
    cwd: Path
    workflow_name: str = ""
    mode_name: str | None = None
    model: str = ""


@dataclass
class _Geometry:
    """Where the last frame put things, for mouse clicks (AGX-13 §5.4)."""

    rows: dict[int, int] = field(default_factory=dict)
    tabs: list[tuple[int, int, Tab]] = field(default_factory=list)
    footer_line: int = -1
    footer: list[tuple[int, int, str]] = field(default_factory=list)


class AgentsHub:
    """Keyboard-, mouse- and event-driven control surface for every agent."""

    def __init__(
        self,
        console: Console,
        *,
        store: BackgroundStore,
        supervisor: BackgroundSupervisor,
        settings: AgentsHubSettings | None = None,
        service: AgentsService | None = None,
        sources: HubSources | None = None,
        app_state: AppState | None = None,
        prefs: HubPrefs | None = None,
        tab: Tab = Tab.RUNNING,
        open_session: str | None = None,
        select: str | None = None,
        run_id: str = "",
        cwd: Path | None = None,
        in_session: bool = False,
        dispatch: DispatchDefaults | None = None,
        generate_definition: Callable[[str, str], Awaitable[str]] | None = None,
        effective_tools: Callable[[AgentSpec, AppState], tuple[str, ...]] | None = None,
        external_editor: Callable[[Path, DefinitionScope], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from agenthicc.tui.conversation_store import AppState

        self.console = console
        self.store = store
        self.supervisor = supervisor
        self.settings = settings or AgentsHubSettings()
        self.maintenance_s = self.settings.maintenance_interval_s
        self._service = service or AgentsService(
            store,
            supervisor,
            max_workers=self.settings.max_in_flight_operations,
            poll_interval_s=self.maintenance_s,
        )
        self._owns_service = service is None
        self._sources = sources or HubSources()
        self._state = app_state or AppState.create()
        self._prefs = prefs or HubPrefs.load()
        self._clock = clock
        self.tab = tab
        self._run_id = run_id
        self._cwd = cwd or Path.cwd()
        self.in_session = in_session
        self._open_session = open_session
        self._dispatch_defaults = dispatch or DispatchDefaults()
        self._generate_definition = generate_definition
        self._effective_tools = effective_tools
        self._external_editor = external_editor
        self._terminal_handoff: (
            tuple[Callable[[], Awaitable[None]], asyncio.Future[None]] | None
        ) = None
        self._backend: TerminalBackend | None = None
        self._library_effective_task: asyncio.Task[None] | None = None
        self._library_effective_generation = 0
        #: ``/mode`` and ``/model`` typed alone set these for later dispatches.
        self._dispatch_mode: str | None = None
        self._dispatch_model = ""
        self._pending_dispatch: DispatchRequest | None = None

        # Records, as last loaded.
        self._running: list[BackgroundSession] = []
        self._inbox_items: tuple[BackgroundInboxItem, ...] = ()
        self._history: list[BackgroundSession] = []
        self._history_total = 0
        self._history_count = 0
        self._history_want = HISTORY_CHUNK
        self._history_task: asyncio.Task[None] | None = None
        self._saved: list[SavedSession] = []
        self._saved_loaded = False
        self._library: list[LibraryEntry] = []
        self._library_loaded = False
        self._library_draft: _LibraryDraft | None = None
        self._library_pending: tuple[AgentSpec, DefinitionScope] | None = None
        self._library_task: asyncio.Task[None] | None = None
        self._library_edit_entry: LibraryEntry | None = None
        self._library_editor_scroll = 0
        self._library_view_title = ""
        self._library_view_lines: list[str] | None = None
        self._library_view_scroll = 0
        self._scheduled: list[ScheduledEntry] = []
        self._queue_positions: dict[str, int] = {}
        self._sessions: dict[str, BackgroundSession] = {}
        self._rows: list[HubRow] = []
        self._row_index: dict[str, int] = {}
        self._counts: dict[Tab, int] = {}
        self._loaded = False
        self._load_error = ""
        self._provisional = False
        self._load_started_at = 0.0

        # Selection, scrolling, search and the other view state.
        # The selected row; ``select`` (after /background) picks an agent.
        self._selected_key: str | None = f"session:{select}" if select else None
        self._selected_by_tab: dict[Tab, str] = {}
        self._pending_attention_target: tuple[Tab, str] | None = None
        self._top = 0
        self._list_height = 10
        self._search = Search()
        self._search_before: str | None = None
        self._prompt: _Prompt | None = None
        self._choice: _Choice | None = None
        self._collapsed: set[str] = set()
        self.marked_ids: set[str] = set()
        self.paused = False
        self.help_visible = False
        self._peek = False
        self._detail_id: str | None = None
        self._detail_item_id: str | None = None
        self._detail_scroll = 0
        self._detail_height = 10
        self._follow = True
        self._full_transcript = False
        self._diff_lines: tuple[str, ...] | None = None
        self._diff_scroll = 0
        #: The detail line of the pending request in the last frame (``o``).
        self._request_line = 0
        self._details: dict[str, DetailData] = {}
        #: When each agent's change summary was last read (monotonic).
        self._changes_read_at: dict[str, float] = {}
        self._detail_task: asyncio.Task[None] | None = None
        self._detail_generation = 0
        self._detail_stale = False
        self._detail_watched: str | None = None
        self._notice = ""
        self._dispatcher: Dispatcher | None = None
        self._geometry = _Geometry()
        self._last_click: tuple[float, int] = (0.0, -1)
        self._attach_result: HubResult | None = None

        # Operations.
        self._operation_tasks: dict[str, asyncio.Task[object]] = {}
        #: Operation ID → (target, label, phase).
        self._operation_state: dict[str, tuple[str, str, str]] = {}
        self._operation_semaphore: asyncio.Semaphore | None = None

        # Deletion (PRD-208).
        self._deletion_task: asyncio.Task[DeleteResult] | None = None
        self._deleting_ids: tuple[str, ...] = ()
        self._deletion_operation_id = ""
        self._deletion_phase = ""
        self._deletion_error = ""

        # The target-scoped input composer (PRD-215–217).
        self._composer_target: BackgroundSession | None = None
        self._composer_open = False
        self._composer_state: AppState | None = None
        self._composer_input: UnifiedInputSession | None = None
        self._composer_overlay: OverlayHost | None = None
        self._composer_history: dict[str, list[str]] = {}
        self._composer_task: asyncio.Task[object] | None = None
        self._composer_build_task: asyncio.Task[None] | None = None
        self._composer_building = False
        self._composer_pending_keys: list[tuple[Key, str]] = []
        self._composer_submitting = False
        self._composer_submitted = False
        self._composer_receipt = ""

        # Worktree environment actions (PRD-229).
        self._environment_actions: tuple[EnvironmentCommand, ...] = ()
        self._environment_action_mode = ""
        self._environment_action_index = 0
        self._environment_manifest_digest = ""
        self._environment_workspace_root: Path | None = None
        self._environment_action_cancel_event: threading.Event | None = None
        #: Lines of the last action preview (its scroll limit).
        self._environment_rows = 0

        # The engine (AGX-04): what wakes it and what it watches.
        self._wakeup: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._watcher: FileWatcher | None = None
        self._watch_task: asyncio.Task[WatchChanges] | None = None
        self._wake_task: asyncio.Task[Literal[True]] | None = None
        self._load_task: asyncio.Task[None] | None = None
        self._load_pending = False
        self._load_generation = 0
        self._load_failures = 0
        self._load_retry_at = 0.0
        self._provisional_task: asyncio.Task[None] | None = None
        self._inbox_task: asyncio.Task[None] | None = None
        self._side_task: asyncio.Task[None] | None = None
        self._side_started = False
        self._unsubscribe_subagents: Callable[[], None] | None = None
        self._dirty: set[str] = {"opened"}

        # Maintenance: what was requested since the last pass.
        self._maintenance_task: asyncio.Task[list[BackgroundSession]] | None = None
        self._maintenance_running = False
        self._maintenance_requested_at: float | None = None
        self._maintenance_urgent = False
        self._maintenance_full = False
        self._maintenance_registry_changed = False
        self._maintenance_inbox_changed: set[str] | None = set()
        self._maintenance_exited: set[str] = set()
        self._maintenance_next_due: float | None = None
        self._maintenance_retry_at = 0.0
        self._maintenance_failures = 0
        self._last_maintenance_pass = 0.0
        self._exit_watched: dict[str, int] = {}

        # Metrics ([background.manager] metrics = true).
        self._metrics_enabled = self.settings.metrics
        self._metric_aggregates: dict[str, tuple[int, float, float]] = {}
        self._metric_samples: dict[str, list[float]] = {}
        self._last_key_at: float | None = None

    # ── metrics ──────────────────────────────────────────────────────────────

    def _record_metric(self, name: str, duration_s: float = 0.0) -> None:
        if not self._metrics_enabled:
            return
        count, total, maximum = self._metric_aggregates.get(name, (0, 0.0, 0.0))
        duration_ms = max(0.0, duration_s * 1_000.0)
        self._metric_aggregates[name] = (count + 1, total + duration_ms, max(maximum, duration_ms))
        samples = self._metric_samples.setdefault(name, [])
        samples.append(duration_ms)
        if len(samples) > 512:
            del samples[:-512]

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
        return ordered[index]

    @property
    def diagnostics(self) -> dict[str, object]:
        """Bounded, redacted hub diagnostics for tests and support."""
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
            "load_in_flight": self._load_task is not None and not self._load_task.done(),
            "maintenance_in_flight": self._maintenance_task is not None
            and not self._maintenance_task.done(),
            "detail_in_flight": self._detail_task is not None and not self._detail_task.done(),
            "rows": len(self._rows),
            "metrics": aggregates,
        }

    # ── read-only views ──────────────────────────────────────────────────────

    @property
    def keymap(self) -> Keymap:
        return self._state.keymap()

    @property
    def ui(self) -> UISettings | None:
        from agenthicc.tui.preferences import ui_settings

        return ui_settings(self._state)

    @property
    def rows(self) -> list[HubRow]:
        """The current tab's rows, as shown (search applied)."""
        return list(self._rows)

    @property
    def counts(self) -> dict[Tab, int]:
        return dict(self._counts)

    @property
    def search(self) -> Search:
        return self._search

    @property
    def notice(self) -> str:
        return self._notice

    @property
    def detail_session_id(self) -> str | None:
        return self._detail_id

    @property
    def peek_open(self) -> bool:
        return self._peek

    @property
    def loaded(self) -> bool:
        return self._loaded

    def selected_row(self) -> HubRow | None:
        index = self._row_index.get(self._selected_key or "")
        return self._rows[index] if index is not None else None

    @property
    def selected_session_id(self) -> str | None:
        row = self.selected_row()
        return row.session_id if row is not None else None

    def select_attention_target(self, session_id: str | None, item_id: str | None = None) -> None:
        """Select an attention row, retaining the request until the list loads."""
        if session_id is None:
            return
        key = f"inbox:{item_id}" if item_id else f"session:{session_id}"
        index = self._row_index.get(key)
        if index is None and not item_id:
            index = next(
                (
                    row_index
                    for row_index, row in enumerate(self._rows)
                    if row.selectable and row.session_id == session_id
                ),
                None,
            )
            if index is not None:
                key = self._rows[index].key
        if index is None:
            self._selected_key = key
            self._selected_by_tab[self.tab] = key
            self._pending_attention_target = (self.tab, key)
            self._mark_dirty("attention_target")
            return
        self._pending_attention_target = None
        self._select_index(index)

    def session(self, session_id: str | None) -> BackgroundSession | None:
        return self._sessions.get(session_id or "")

    def _row_session(self, row: HubRow | None) -> BackgroundSession | None:
        if row is None:
            return None
        if row.session is not None:
            return self._sessions.get(row.session.session_id, row.session)
        if row.item is not None:
            return self._sessions.get(row.item.session_id)
        return None

    def _pending_item(self, session: BackgroundSession | None) -> BackgroundInboxItem | None:
        """The inbox request *session* waits on, if the hub has loaded it."""
        if session is None:
            return None
        wanted = session.approval_item_id or session.input_item_id
        for item in self._inbox_items:
            if wanted and item.item_id == wanted:
                return item
        for item in self._inbox_items:
            if item.session_id == session.session_id and item.attempt == session.attempt:
                return item
        return None

    def _target(self) -> tuple[HubRow | None, BackgroundSession | None, BackgroundInboxItem | None]:
        """The row, agent and inbox request an action applies to."""
        if self._detail_id is not None:
            session = self._sessions.get(self._detail_id)
            item = None
            if self._detail_item_id is not None:
                item = next(
                    (i for i in self._inbox_items if i.item_id == self._detail_item_id), None
                )
            return self.selected_row(), session, item or self._pending_item(session)
        row = self.selected_row()
        session = self._row_session(row)
        item = row.item if row is not None and row.item is not None else self._pending_item(session)
        return row, session, item

    def _accepts_input(
        self, session: BackgroundSession | None, item: BackgroundInboxItem | None
    ) -> bool:
        if item is not None:
            return item.kind != "approval" or item.target.get("request_kind") == "plan_review"
        if session is None:
            return False
        recoverable = session.status in (
            SessionStatus.COMPLETED,
            SessionStatus.ORPHANED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.ARCHIVED,
        )
        live_owner = (
            session.status
            in (
                SessionStatus.STARTING,
                SessionStatus.RUNNING,
                SessionStatus.WAITING_APPROVAL,
                SessionStatus.WAITING_INPUT,
            )
            and session.worker_pid is not None
            and bool(session.lease_token)
        )
        return recoverable or live_owner

    def conditions(self) -> frozenset[str]:
        """The key-map conditions for the current tab and selection (AGX-13)."""
        flags: set[str] = set()
        if self.tab is Tab.RUNNING:
            flags.add("running_tab")
        if self.tab is Tab.LIBRARY:
            flags.add("library_tab")
            row = self.selected_row()
            entry = row.entry if row is not None else None
            if entry is not None:
                flags.add("definition")
                if entry.source in ("project", "user") and entry.path.endswith(".md"):
                    flags.add("editable_definition")
                if entry.spec is not None and entry.spec.primary:
                    flags.add("primary_definition")
        row, session, item = self._target()
        if row is not None and row.selectable:
            flags.add("row")
        if row is not None and row.kind is RowKind.SAVED:
            flags.add("saved")
        if row is not None and row.kind is RowKind.INBOX:
            flags.add("inbox")
        if session is not None:
            flags.update(("agent", "session"))
            if row is not None and row.kind is RowKind.INBOX and self._detail_id is None:
                flags.discard("session")
            status = session.status
            if status is SessionStatus.WAITING_APPROVAL or (
                item is not None and item.kind == "approval"
            ):
                flags.add("needs_approval")
            if self._accepts_input(session, item):
                flags.add("accepts_input")
            if status in _RESUMABLE:
                flags.add("resumable")
            if status in _RETRYABLE:
                flags.add("retryable")
            if status in ACTIVE_STATUSES or self.marked_ids:
                flags.add("stoppable")
            if status in FINISHED_STATUSES or self.marked_ids:
                flags.add("archivable")
            if status is not SessionStatus.DELETED:
                flags.add("attachable")
            if session.pinned:
                flags.add("pinned")
            if session.role == "worker":
                flags.add("worker")
            if self._detail_id is None and row is not None and row.kind is RowKind.SESSION:
                if row.hidden:
                    flags.add("collapsed")
                elif self._has_children(session.session_id):
                    flags.add("expanded")
        elif item is not None:
            flags.add("agent")
            if item.kind == "approval":
                flags.add("needs_approval")
            if self._accepts_input(None, item):
                flags.add("accepts_input")
        if self._detail_id is not None:
            flags.add("transcript")
            data = self._details.get(self._detail_id)
            if data is not None and data.changes is not None:
                flags.add("changes")
        return frozenset(flags)

    def _has_children(self, session_id: str) -> bool:
        return any(
            session.parent_session_id == session_id and session.session_id != session_id
            for session in self._running
        )

    @property
    def context(self) -> str:
        if self._detail_id is not None:
            return "agents.detail"
        if self._peek:
            return "agents.peek"
        return "agents.list"

    # ── waking and owned tasks ───────────────────────────────────────────────

    def _mark_dirty(self, cause: str) -> None:
        self._dirty.add(cause)
        self._wake()

    @property
    def dirty(self) -> bool:
        return bool(self._dirty)

    def _wake(self) -> None:
        """Wake the engine: something it shows or schedules has changed."""
        wakeup = self._wakeup
        if wakeup is None:
            return
        if threading.get_ident() == self._loop_thread:
            wakeup.set()
        elif self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(wakeup.set)

    def _wake_on_done(self, _task: object) -> None:
        self._wake()

    def _owned(self, task: asyncio.Task[_T]) -> asyncio.Task[_T]:
        """Wake the engine when *task* finishes, whether or not it changed anything."""
        task.add_done_callback(self._wake_on_done)
        return task

    # ── loading ──────────────────────────────────────────────────────────────

    def _request_load(self, cause: str, *, force: bool = False) -> None:
        """Reload what the hub shows; overlapping requests are merged."""
        if self._loop is None:
            self._mark_dirty(cause)
            return
        if not force and cause in _AUTOMATIC_CAUSES and time.monotonic() < self._load_retry_at:
            return
        if self.paused and not force:
            return
        self._mark_dirty(cause)
        self._record_metric("load_requested")
        if self._load_task is not None and not self._load_task.done():
            self._load_pending = True
            return
        self._load_pending = False
        self._load_generation += 1
        generation = self._load_generation
        if not self._loaded:
            self._load_started_at = time.monotonic()
            if self._provisional_task is None:
                self._provisional_task = self._owned(
                    asyncio.create_task(self._load_provisional(), name="agenthicc-hub-provisional")
                )
        self._load_task = self._owned(
            asyncio.create_task(self._load(generation), name=f"agenthicc-hub-load-{generation}")
        )

    async def _load_provisional(self) -> None:
        """Show recent agents from the log tail while the index rebuilds (AGX-03)."""
        await asyncio.sleep(0.3)
        if self._loaded or self.store.rebuild_progress() is None:
            return
        try:
            recent = await self._service.run_blocking(self.store.recent_from_log_tail, limit=50)
        except Exception:  # noqa: BLE001 - the provisional rows are best effort
            return
        if self._loaded or not isinstance(recent, tuple):
            return
        self._running = [item for item in recent if isinstance(item, BackgroundSession)]
        self._sessions = {item.session_id: item for item in self._running}
        self._provisional = True
        self._rebuild_rows()
        self._mark_dirty("provisional")

    def _history_filter(self) -> SessionFilter:
        return self._search.session_filter(
            SessionFilter(statuses=HISTORY_STATUSES, run_id=self._run_id or None)
        )

    async def _load(self, generation: int) -> None:
        started = time.monotonic()
        service = self._service
        run_id = self._run_id or None
        try:
            now = self._clock()
            active = await service.select_async(
                SessionFilter(statuses=RUNNING_TAB_STATUSES, run_id=run_id)
            )
            pinned = await service.select_async(
                SessionFilter(pinned=True, include_archived=False, run_id=run_id)
            )
            recent = await service.select_async(
                SessionFilter(
                    statuses=FINISHED_STATUSES,
                    active_since=now - RECENT_WINDOW_S,
                    run_id=run_id,
                )
            )
            items = await service.inbox_list_async(limit=500)
            history_count = await service.count_async(
                SessionFilter(statuses=HISTORY_STATUSES, run_id=run_id)
            )
            running: dict[str, BackgroundSession] = {}
            for session in (*active, *pinned, *recent):
                running.setdefault(session.session_id, session)
            positions: dict[str, int] = {}
            if any(item.status is SessionStatus.QUEUED for item in running.values()):
                try:
                    positions = await service.queue_positions_async()
                except Exception:  # noqa: BLE001 - positions are advisory decoration
                    positions = {}
            history: list[BackgroundSession] | None = None
            history_total = self._history_total
            if self.tab is Tab.HISTORY:
                window = await service.window_async(
                    self._history_filter(), offset=0, limit=self._history_want
                )
                history = list(window.sessions)
                history_total = window.total
            # History lists saved sessions among the agents; every other tab
            # shows its rows first and reads them afterwards (_load_side).
            if self.tab is Tab.HISTORY and not self._saved_loaded:
                await self._load_saved()
            if self.tab is Tab.LIBRARY and not self._library_loaded:
                await self._load_library()
            if generation != self._load_generation:
                return
            self._running = list(running.values())
            if run_id is not None:
                items = tuple(item for item in items if item.session_id in running)
            self._inbox_items = items
            self._queue_positions = positions
            self._history_count = history_count
            if history is not None:
                self._history = history
                self._history_total = history_total
            self._sessions = {**running, **{item.session_id: item for item in self._history}}
            self._loaded = True
            self._provisional = False
            self._load_error = ""
            self._load_failures = 0
            self._load_retry_at = 0.0
            if self._notice.startswith(("Refresh requested", "Agents failed to load")):
                self._notice = ""
            self._rebuild_rows()
            self._open_requested_session()
            self._record_metric("load_ready", time.monotonic() - started)
            self._mark_dirty("loaded")
            self._schedule_detail_read()
            if not self._side_started and self._loop is not None:
                self._side_started = True
                self._side_task = self._owned(asyncio.create_task(self._after_first_rows()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if generation == self._load_generation:
                self._load_failures += 1
                delay = min(60.0, 0.5 * (2 ** min(7, self._load_failures - 1)))
                self._load_retry_at = time.monotonic() + delay
                self._load_error = f"{type(exc).__name__}: {exc}"[:240]
                if self._loaded:
                    # Before the first rows the loading line says it already.
                    self._notice = f"Agents failed to load: {self._load_error}"
                self._mark_dirty("load_failed")
        finally:
            if self._load_task is asyncio.current_task():
                self._load_task = None
            if self._load_pending:
                self._load_pending = False
                self._request_load("coalesced")

    async def _load_saved(self) -> None:
        """Saved foreground sessions, without those that are background agents."""
        loaded = await self._service.run_blocking(self._sources.saved)
        records = (
            [item for item in loaded if isinstance(item, SavedSession)]
            if isinstance(loaded, list)
            else []
        )
        known = await self._service.run_blocking(
            self.store.known_ids, tuple(record.session_id for record in records)
        )
        background = known if isinstance(known, frozenset) else frozenset()
        self._saved = [record for record in records if record.session_id not in background]
        self._saved_loaded = True

    async def _load_library(self) -> None:
        try:
            loaded = await self._service.run_blocking(self._sources.library)
        except Exception as exc:  # noqa: BLE001 - a broken definition must not hide the hub
            self._notice = f"Agent definitions failed to load: {type(exc).__name__}: {exc}"[:240]
            loaded = []
        self._library = (
            [item for item in loaded if isinstance(item, LibraryEntry)]
            if isinstance(loaded, list)
            else []
        )
        self._library_loaded = True

    async def _after_first_rows(self) -> None:
        """Start the first maintenance pass and the side loads (AGX-05 §6)."""
        await asyncio.sleep(SIDE_LOAD_DELAY_S)
        # The first pass probes every active worker so each can be watched.
        self._request_maintenance(full=True)
        await self._load_side()

    async def _load_side(self) -> None:
        """What the first rows do not wait for, read after them and on refresh.

        The next scheduled runs, then saved sessions (which complete the
        History count), then agent definitions. A session reads definitions
        here, as its agent runtime has already imported what they need; the
        shell reads them when the Library tab is first shown.
        """
        try:
            loaded = await self._service.run_blocking(self._sources.scheduled)
        except Exception:  # noqa: BLE001 - upcoming runs are decoration
            loaded = []
        self._scheduled = (
            [item for item in loaded if isinstance(item, ScheduledEntry)]
            if isinstance(loaded, list)
            else []
        )
        self._rebuild_rows()
        self._mark_dirty("side_loaded")
        if not self._saved_loaded:
            try:
                await self._load_saved()
            except Exception as exc:  # noqa: BLE001 - saved sessions are one source of many
                self._notice = f"Saved sessions failed to load: {type(exc).__name__}: {exc}"[:240]
            self._rebuild_rows()
            self._mark_dirty("side_loaded")
        if not self._library_loaded and (self.in_session or self.tab is Tab.LIBRARY):
            await self._show_library()

    async def _show_library(self) -> None:
        await self._load_library()
        self._rebuild_rows()
        self._mark_dirty("side_loaded")

    async def _extend_history(self) -> None:
        """Read the next History window as the selection nears the loaded end."""
        try:
            want = self._history_want + HISTORY_CHUNK
            window = await self._service.window_async(self._history_filter(), offset=0, limit=want)
            self._history_want = want
            self._history = list(window.sessions)
            self._history_total = window.total
            self._sessions.update({item.session_id: item for item in self._history})
            self._rebuild_rows()
            self._mark_dirty("history_extended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._notice = f"More history failed to load: {type(exc).__name__}: {exc}"[:240]
            self._mark_dirty("notice_changed")

    def _visible_saved(self) -> list[SavedSession]:
        """Saved sessions in History: matching the search and inside the loaded window.

        While more agents remain to be read, a saved session older than the
        last loaded agent is held back so the merged order stays correct.
        """
        records = [record for record in self._saved if self._search.matches_saved(record)]
        if self._search.has_tokens:
            return []
        if len(self._history) < self._history_total and self._history:
            cutoff = self._history[-1].last_active
            records = [record for record in records if record.last_used >= cutoff]
        return records

    def _rebuild_rows(self) -> None:
        """Recompute the current tab's rows and every tab's count."""
        previous_index = self._row_index.get(self._selected_key or "", 0)
        search = self._search
        needs_sessions = [s for s in self._running if s.status in NEEDS_YOU_STATUSES]
        if self.tab is Tab.RUNNING:
            sessions = (
                [s for s in self._running if search.matches_session(s)]
                if search.active
                else self._running
            )
            rows = running_rows(
                sessions,
                sort=self._prefs.sort,
                group_by=self._prefs.group_by,
                collapsed=frozenset(self._collapsed),
                scheduled=() if search.active or self._run_id else self._scheduled,
                show_empty_groups=self._loaded and not self._running and not search.active,
                pin_order=self._prefs.pin_order,
                subagents=(
                    self._state.conversation.subagent_pool_state()
                    if self.in_session and not search.active and not self._run_id
                    else None
                ),
            )
        elif self.tab is Tab.NEEDS_YOU:
            items = [
                item for item in self._inbox_items if not search.active or self._item_matches(item)
            ]
            rows = needs_you_rows(
                items, [s for s in needs_sessions if not search.active or search.matches_session(s)]
            )
        elif self.tab is Tab.HISTORY:
            rows = history_rows(self._history, self._visible_saved())
        else:
            rows = library_rows([entry for entry in self._library if search.matches_entry(entry)])
        self._rows = rows
        self._row_index = {row.key: index for index, row in enumerate(rows)}
        self._counts = {
            Tab.RUNNING: len(self._running),
            Tab.NEEDS_YOU: len(needs_you_rows(self._inbox_items, needs_sessions)),
            Tab.HISTORY: self._history_count + len(self._saved),
        }
        if self._library_loaded:
            self._counts[Tab.LIBRARY] = len(self._library)
        self._reconcile_selection(previous_index)

    def _item_matches(self, item: BackgroundInboxItem) -> bool:
        session = self._sessions.get(item.session_id)
        if session is not None:
            return self._search.matches_session(session)
        return not self._search.has_tokens and self._search.matches_text(
            item.session_id, item.workspace, item.kind, str(item.target.get("tool_name", ""))
        )

    def _reconcile_selection(self, previous_index: int) -> None:
        """Keep the selection on the same agent; else the nearest selectable row."""
        pending = self._pending_attention_target
        if pending is not None and pending[0] is self.tab:
            pending_key = pending[1]
            pending_index = self._row_index.get(pending_key)
            if pending_index is not None and self._rows[pending_index].selectable:
                self._pending_attention_target = None
                self._select_index(pending_index)
                return
            if not self._loaded:
                self._selected_key = pending_key
                return
            self._pending_attention_target = None
        key = self._selected_key
        index = self._row_index.get(key or "")
        if index is not None and self._rows[index].selectable:
            return
        self._selected_key = self._nearest_selectable(previous_index)

    def _nearest_selectable(self, index: int) -> str | None:
        if not self._rows:
            return None
        index = min(max(index, 0), len(self._rows) - 1)
        # At or below the old position first, then above it.
        candidates: list[int] = list(range(index, len(self._rows)))
        candidates.extend(range(index - 1, -1, -1))
        for candidate in candidates:
            row = self._rows[candidate]
            if row.selectable:
                return row.key
        return None

    def _open_requested_session(self) -> None:
        """``agenthicc agents SESSION_ID`` opens that agent's detail once."""
        session_id = self._open_session
        if session_id is None or not self._loaded:
            return
        self._open_session = None
        key = f"session:{session_id}"
        if key not in self._row_index and session_id not in self._sessions:
            self._notice = f"Agent {session_id} is not in the {self.tab.label} tab"
            return
        if key in self._row_index:
            self._selected_key = key
        self._open_detail(session_id)

    # ── the engine ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Arm the watches and start the first load and maintenance pass."""
        from agenthicc.background.watch import FileWatcher

        if self._loop is not None:
            return
        self._record_metric("hub_open")
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._wakeup = asyncio.Event()
        self._operation_semaphore = asyncio.Semaphore(self.settings.max_in_flight_operations)
        self._dispatcher = Dispatcher(self.keymap)
        watcher = FileWatcher(fallback_interval_s=self.settings.refresh_interval_s)
        self._watcher = watcher
        watcher.watch("registry", self.store.events_path)
        # Reading any session's receipts creates the input inbox; create it now
        # so its watch is armed from the start.
        input_inbox = self.store.root / "input-inbox"
        input_inbox.mkdir(parents=True, exist_ok=True)
        watcher.watch_directory("inbox", input_inbox)
        if not watcher.reports_entry_names:
            # Only inotify says which inbox file changed; elsewhere an append to
            # an existing inbox file is found by a periodic rescan.
            self._service.scheduler.inbox_rescan_s = self.maintenance_s
        self._request_load("open", force=True)
        # The operator inbox creates its database on first use; do that while
        # the first rows load rather than inside the first user operation.
        self._inbox_task = self._owned(asyncio.create_task(self._service.prepare_inbox_async()))
        # The first maintenance pass starts after the first rows are shown
        # (_after_first_rows): it shares the store with the load.
        self._watch_task = asyncio.create_task(watcher.wait(None), name="agenthicc-hub-watch")
        self._wake_task = asyncio.create_task(self._wakeup.wait(), name="agenthicc-hub-wake")
        if self.in_session:
            # The turn's subagents are listed in Running (AGX-08).
            self._unsubscribe_subagents = self._state.conversation.subagent_pool_state.subscribe(
                self._subagents_changed
            )

    def _subagents_changed(self) -> None:
        if self.tab is Tab.RUNNING and self._detail_id is None:
            self._rebuild_rows()
            self._mark_dirty("subagents")

    async def step(
        self, extra: frozenset[asyncio.Future[tuple[Key, str]]] = frozenset()
    ) -> set[asyncio.Future[tuple[Key, str]]]:
        """Sleep until something happens, then react to it.

        Returns which of *extra* (the caller's own futures, such as a key
        read) finished. Nothing here polls: the engine wakes on a watched file,
        a worker exit, a finished task, a due maintenance pass or *extra*.
        """
        watcher = self._watcher
        wakeup = self._wakeup
        if watcher is None or wakeup is None or self._watch_task is None or self._wake_task is None:
            raise RuntimeError("the agents hub engine is not started")
        waiting: set[asyncio.Future[object]] = {
            cast("asyncio.Future[object]", self._watch_task),
            cast("asyncio.Future[object]", self._wake_task),
            *(cast("asyncio.Future[object]", future) for future in extra),
        }
        done, _pending = await asyncio.wait(
            waiting, timeout=self._idle_timeout(), return_when=asyncio.FIRST_COMPLETED
        )
        if self._wake_task in done:
            # Clear before reacting: a change made meanwhile wakes the next step.
            wakeup.clear()
            self._wake_task = asyncio.create_task(wakeup.wait(), name="agenthicc-hub-wake")
        if self._watch_task in done:
            self._apply_watch_changes(self._watch_task.result())
            self._watch_task = asyncio.create_task(watcher.wait(None), name="agenthicc-hub-watch")
        if not self._loaded:
            # Elapsed time and index progress while loading.
            self._dirty.add("loading")
        self._poll_delete()
        self._retry_failed_load()
        self._run_due_maintenance()
        return {future for future in extra if cast("asyncio.Future[object]", future) in done}

    async def settle(self, timeout_s: float = 10.0) -> None:
        """Wait until the loads, operations and reads in flight have finished.

        For tests and scripts: it repeats until nothing new was started, so a
        load that requests another one is waited for too.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            pending = [
                task
                for task in (
                    self._load_task,
                    self._side_task,
                    self._history_task,
                    self._detail_task,
                    self._library_task,
                    self._library_effective_task,
                    self._maintenance_task,
                    self._composer_build_task,
                    self._composer_task,
                    self._deletion_task,
                    *self._operation_tasks.values(),
                )
                if task is not None and not task.done()
            ]
            if not pending and not self._load_pending:
                self._poll_delete()
                return
            await asyncio.wait(
                pending or [asyncio.ensure_future(asyncio.sleep(0.01))],
                timeout=max(0.0, deadline - time.monotonic()),
            )
            self._poll_delete()
        raise TimeoutError("the agents hub did not settle")

    async def stop(self) -> None:
        """Cancel the hub's tasks and close its watches; durable work continues."""
        await self._shutdown_delete()
        # A first-time index rebuild can run for a while on a large log; stop it
        # instead of waiting for its worker thread on exit.
        self.store.cancel_rebuild()
        owned = [
            task
            for task in (
                self._watch_task,
                self._wake_task,
                self._load_task,
                self._inbox_task,
                self._side_task,
                self._history_task,
                self._provisional_task,
                self._maintenance_task,
                self._detail_task,
                self._library_task,
                self._library_effective_task,
                self._composer_build_task,
                self._deletion_task,
                *self._operation_tasks.values(),
            )
            if task is not None and task is not asyncio.current_task() and not task.done()
        ]
        for task in owned:
            task.cancel()
        if owned:
            completed, pending = await asyncio.wait(owned, timeout=0.25)
            for task in completed:
                if not task.cancelled():
                    try:
                        task.exception()
                    except Exception:  # noqa: BLE001
                        pass
            # Operations are persisted independently; a slow one must not keep
            # the hub open, so its eventual result is consumed instead.
            for task in pending:
                task.add_done_callback(_consume_task_result)
        if self._unsubscribe_subagents is not None:
            self._unsubscribe_subagents()
            self._unsubscribe_subagents = None
        if self._watcher is not None:
            self._watcher.close()
        self._watcher = None
        self._exit_watched.clear()
        self._wakeup = None
        self._loop = None
        # Closing the in-session hub must not stall the conversation's frames.
        await asyncio.to_thread(self._prefs.save)
        if self._owns_service:
            await self._service.close()

    async def serve(
        self,
        on_dirty: Callable[[], None],
        on_result: Callable[[HubResult], None] | None = None,
    ) -> None:
        """Run the engine for an overlay until cancelled.

        The session owns key input and drawing: *on_dirty* asks it to redraw,
        and *on_result* receives an attach handed back by an operation.
        """
        await self.start()
        try:
            while True:
                await self.step()
                if self._attach_result is not None and on_result is not None:
                    result, self._attach_result = self._attach_result, None
                    on_result(result)
                if self._dirty:
                    on_dirty()
        finally:
            await self.stop()

    def _idle_timeout(self) -> float | None:
        """Seconds until the engine has timed work, or ``None``."""
        now = time.monotonic()
        deadlines: list[float] = []
        if not self._loaded:
            deadlines.append(now + LOADING_FRAME_S)
        due = self._maintenance_due_at()
        if due is not None:
            deadlines.append(due)
        if self._load_failures:
            deadlines.append(self._load_retry_at)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def _retry_failed_load(self) -> None:
        if (
            self._load_failures
            and time.monotonic() >= self._load_retry_at
            and (self._load_task is None or self._load_task.done())
        ):
            self._request_load("retry")

    def _apply_watch_changes(self, changes: WatchChanges) -> None:
        """Turn file and process notifications into reloads and maintenance."""
        for key, names in changes.items():
            if key == "registry":
                self._request_load("registry_changed")
                self._request_maintenance(registry_changed=True)
            elif key == "inbox":
                # Each inbox read locks a sibling .lock file; only the .jsonl
                # logs carry input.
                logs = (
                    None if names is None else frozenset(n for n in names if n.endswith(".jsonl"))
                )
                if logs is not None and not logs:
                    continue
                self._request_maintenance(inbox_names=logs)
                watched = self._detail_watched
                if watched is not None and (logs is None or f"{watched}.jsonl" in logs):
                    self._detail_stale = True
            elif key == "detail":
                self._detail_stale = True
            elif key.startswith("pid:"):
                session_id = key.removeprefix("pid:")
                self._exit_watched.pop(session_id, None)
                self._request_maintenance(exited=(session_id,))
        if self._detail_stale:
            self._schedule_detail_read()

    # ── maintenance (AGX-04) ─────────────────────────────────────────────────

    async def maintain_async(self, *, force: bool = False) -> list[BackgroundSession]:
        """Run the requested maintenance now; ``force`` makes it a full pass.

        A failed pass keeps its requested work and backs off; only a forced
        pass retries before the backoff ends.
        """
        now = time.monotonic()
        if not force and now < self._maintenance_retry_at:
            return []
        if self._maintenance_running:
            return []
        current_task = asyncio.current_task()
        trigger = self._take_maintenance_trigger(full=force)
        self._maintenance_running = True
        self._record_metric("maintenance_started")
        try:
            outcome = await self._service.maintain_async(trigger)
            self._maintenance_failures = 0
            self._maintenance_retry_at = 0.0
            self._record_metric("maintenance", time.monotonic() - now)
        except Exception as exc:  # noqa: BLE001
            self._return_maintenance_trigger(trigger)
            self._maintenance_failures += 1
            delay = min(60.0, self.maintenance_s * (2**self._maintenance_failures))
            self._maintenance_retry_at = time.monotonic() + delay
            self._notice = f"Maintenance failed: {type(exc).__name__}: {exc}"[:240]
            self._mark_dirty("notice_changed")
            return []
        finally:
            self._record_metric("maintenance_finished", time.monotonic() - now)
            self._maintenance_running = False
            self._last_maintenance_pass = time.monotonic()
            if current_task is not None and self._maintenance_task is current_task:
                self._maintenance_task = None
            self._wake()
        self._apply_maintenance_outcome(outcome)
        if outcome.changed:
            self._request_load("maintenance", force=True)
        return list(outcome.changed)

    def _request_maintenance(
        self,
        *,
        full: bool = False,
        registry_changed: bool = False,
        inbox_names: frozenset[str] | None = frozenset(),
        exited: tuple[str, ...] = (),
    ) -> None:
        """Ask for a maintenance pass; the engine starts it when it falls due."""
        self._maintenance_full = self._maintenance_full or full
        self._maintenance_registry_changed = self._maintenance_registry_changed or registry_changed
        if inbox_names is None:
            self._maintenance_inbox_changed = None
        elif self._maintenance_inbox_changed is not None:
            self._maintenance_inbox_changed |= inbox_names
        self._maintenance_exited.update(exited)
        self._maintenance_urgent = self._maintenance_urgent or full or bool(exited)
        if self._maintenance_requested_at is None:
            self._maintenance_requested_at = time.monotonic()
        self._wake()

    def _take_maintenance_trigger(self, *, full: bool) -> MaintenanceTrigger:
        inbox = self._maintenance_inbox_changed
        trigger = MaintenanceTrigger(
            full=full or self._maintenance_full,
            registry_changed=self._maintenance_registry_changed,
            inbox_changed=None if inbox is None else frozenset(inbox),
            exited=frozenset(self._maintenance_exited),
            exit_watched=frozenset(self._exit_watched),
        )
        self._maintenance_requested_at = None
        self._maintenance_urgent = False
        self._maintenance_full = False
        self._maintenance_registry_changed = False
        self._maintenance_inbox_changed = set()
        self._maintenance_exited = set()
        self._maintenance_next_due = None
        return trigger

    def _return_maintenance_trigger(self, trigger: MaintenanceTrigger) -> None:
        """Keep a failed pass's work requested so the retry still does it."""
        self._request_maintenance(
            full=trigger.full,
            registry_changed=trigger.registry_changed,
            inbox_names=trigger.inbox_changed,
            exited=tuple(trigger.exited),
        )

    def _apply_maintenance_outcome(self, outcome: MaintenanceOutcome) -> None:
        watcher = self._watcher
        if watcher is not None:
            for session_id, pid in tuple(self._exit_watched.items()):
                if outcome.workers.get(session_id) != pid:
                    watcher.unwatch(f"pid:{session_id}")
                    del self._exit_watched[session_id]
            for session_id, pid in outcome.workers.items():
                if session_id not in self._exit_watched and watcher.watch_process(
                    f"pid:{session_id}", pid
                ):
                    self._exit_watched[session_id] = pid
        deadlines = [] if outcome.next_due_at is None else [outcome.next_due_at]
        if any(session_id not in self._exit_watched for session_id in outcome.workers):
            # No exit notification for this worker: probe it again later.
            deadlines.append(time.time() + self.maintenance_s)
        self._maintenance_next_due = (
            time.monotonic() + max(0.0, min(deadlines) - time.time()) if deadlines else None
        )

    def _maintenance_due_at(self) -> float | None:
        """When (monotonic) the next maintenance pass should start."""
        floor = self._last_maintenance_pass + MAINTENANCE_COALESCE_S
        candidates: list[float] = []
        requested = self._maintenance_requested_at
        if requested is not None:
            candidates.append(requested if self._maintenance_urgent else max(requested, floor))
        if self._maintenance_next_due is not None:
            candidates.append(max(self._maintenance_next_due, floor))
        if not candidates:
            return None
        return max(min(candidates), self._maintenance_retry_at)

    def _run_due_maintenance(self) -> None:
        # Stale-worker recovery and the cold first load consult the same index;
        # let the screen get its first rows first.
        if not self._loaded:
            return
        if self._maintenance_task is not None and not self._maintenance_task.done():
            return
        due = self._maintenance_due_at()
        if due is not None and time.monotonic() >= due:
            self._schedule_maintenance(force=False)

    def _schedule_maintenance(self, *, force: bool) -> asyncio.Task[list[BackgroundSession]]:
        current = self._maintenance_task
        if current is not None and not current.done():
            return current
        task = self._owned(
            asyncio.create_task(self.maintain_async(force=force), name="agenthicc-hub-maintenance")
        )
        self._maintenance_task = task
        return task

    # ── operations ───────────────────────────────────────────────────────────

    def _pending(self, target_id: str, label: str) -> bool:
        """The same operation on the same target is already running."""
        return any(
            target == target_id and pending == label
            for target, pending, _phase in self._operation_state.values()
        )

    def _start_operation(
        self,
        target_id: str,
        label: str,
        operation: Callable[[], Awaitable[object]],
    ) -> None:
        """Run one operation off the key path; its result becomes the notice."""
        if self._pending(target_id, label):
            return
        self._record_metric("action_dispatched")
        operation_id = uuid.uuid4().hex
        self._operation_state[operation_id] = (target_id, label, "starting")
        self._mark_dirty("operation_changed")
        task = self._owned(
            asyncio.create_task(
                self._run_operation(operation_id, target_id, label, operation),
                name=f"agenthicc-hub-{label}-{operation_id}",
            )
        )
        self._operation_tasks[operation_id] = cast("asyncio.Task[object]", task)

    async def _run_operation(
        self,
        operation_id: str,
        target_id: str,
        label: str,
        operation: Callable[[], Awaitable[object]],
    ) -> None:
        self._operation_state[operation_id] = (target_id, label, "running")
        self._mark_dirty("operation_changed")
        try:
            semaphore = self._operation_semaphore
            if semaphore is None:
                result = await operation()
            else:
                async with semaphore:
                    result = await operation()
            if isinstance(result, ManagerOperationResult) and not result.ok:
                self._notice = f"{label.title()} failed: {result.message}"[:240]
            elif isinstance(result, EnvironmentRunReceipt) and result.result != "succeeded":
                detail = ""
                if result.commands:
                    first = result.commands[0]
                    detail = first.error or first.stderr.strip() or first.stdout.strip()
                    detail = " · " + " ".join(detail.split())[:100] if detail else ""
                    if first.truncated:
                        detail += " · output truncated"
                self._notice = f"{label.title()} failed: {result.result}{detail}"[:240]
            else:
                summary = ""
                if isinstance(result, EnvironmentRunReceipt) and result.commands:
                    first = result.commands[0]
                    output = first.stdout or first.stderr
                    if output.strip():
                        summary = " · " + " ".join(output.split())[:120]
                    if first.result != "succeeded":
                        summary = f" · {first.result}" + summary
                    if first.truncated:
                        summary += " · output truncated"
                self._notice = f"{label.title()} completed{summary}"[:240]
                if label == "attach":
                    self._attach_result = HubResult("attach", target_id)
                elif label == "dispatch" and isinstance(result, ManagerOperationResult):
                    created = result.value
                    if isinstance(created, BackgroundSession):
                        self._selected_key = f"session:{created.session_id}"
                        self._notice = f"Started {created.title or created.session_id[:12]}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._notice = f"{label.title()} failed: {type(exc).__name__}: {exc}"[:240]
        finally:
            self._operation_state.pop(operation_id, None)
            self._operation_tasks.pop(operation_id, None)
            self._mark_dirty("operation_changed")
            self._request_load("operation_complete", force=True)

    # ── detail and peek data ─────────────────────────────────────────────────

    def _schedule_detail_read(self) -> None:
        """(Re)read the opened or peeked agent's transcript, receipt and changes."""
        session_id = self._detail_id or (self.selected_session_id if self._peek else None)
        if session_id is None or self._loop is None:
            return
        session = self._sessions.get(session_id)
        if session is None:
            return
        cached = self._details.get(session_id)
        path = journal_path(session)
        if (
            cached is not None
            and not self._detail_stale
            and cached.attempt == session.attempt
            and cached.fingerprint == journal_fingerprint(path)
        ):
            return
        self._detail_stale = False
        if self._watcher is not None and self._detail_watched != session_id:
            self._watcher.watch("detail", path)
            self._detail_watched = session_id
        self._detail_generation += 1
        generation = self._detail_generation
        if self._detail_task is not None and not self._detail_task.done():
            self._detail_task.cancel()
        now = time.monotonic()
        changes = cached is None or now - self._changes_read_at.get(session_id, 0.0) >= (
            CHANGES_INTERVAL_S
        )
        if changes:
            self._changes_read_at[session_id] = now
        self._detail_task = self._owned(
            asyncio.create_task(
                self._load_detail(session, generation, changes, cached),
                name=f"agenthicc-hub-detail-{session_id}",
            )
        )

    async def _load_detail(
        self,
        session: BackgroundSession,
        generation: int,
        changes: bool,
        previous: DetailData | None,
    ) -> None:
        try:
            value = await self._service.run_blocking(
                self._read_detail, session, self._full_transcript, changes, previous
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the detail view shows what it has
            return
        if generation != self._detail_generation or not isinstance(value, DetailData):
            return
        self._details[session.session_id] = value
        self._mark_dirty("detail_loaded")

    def _read_detail(
        self,
        session: BackgroundSession,
        full: bool,
        changes: bool = True,
        previous: DetailData | None = None,
    ) -> DetailData:
        """Blocking: transcript tail, latest input receipt, input history, changes.

        ``changes`` re-runs ``git diff``; otherwise the previous summary is kept.
        """
        from agenthicc.tui.runtime.session_log import load_user_message_history

        path = journal_path(session)
        fingerprint = journal_fingerprint(path)
        tail = self.settings.activity_tail_bytes * (64 if full else 4)
        transcript = read_transcript(
            path, tail_bytes=tail, limit=2_000 if full else TRANSCRIPT_EVENTS
        )
        history = tuple(load_user_message_history(session.session_id, last_turns=20))
        return DetailData(
            session_id=session.session_id,
            attempt=session.attempt,
            transcript=transcript,
            fingerprint=fingerprint,
            receipt=self._input_receipt(session),
            history=history,
            changes=changes_summary(session) if changes or previous is None else previous.changes,
        )

    def _input_receipt(self, session: BackgroundSession) -> str | None:
        """The agent's latest accepted input and how far it got (PRD-216)."""
        from agenthicc.background.input_inbox import BackgroundInputInbox
        from agenthicc.tui.runtime.session_export import _Redactor

        try:
            receipts = BackgroundInputInbox(self.store).receipts(session.session_id)
        except (OSError, ValueError):
            return None
        if not receipts:
            return None
        receipt = receipts[-1]
        labels = {
            "accepted": "waiting for recovery" if receipt.deferred else "queued",
            "claimed": "processing",
            "delivered": "delivered",
            "completed": "processed",
            "failed": "failed",
            "rejected": "not delivered",
        }
        detail = f"{labels.get(receipt.state, 'pending')} · {receipt.message_id[:12]}"
        if receipt.error:
            detail += f" · {str(_Redactor().value(receipt.error, 'message'))[:120]}"
        if receipt.recovery_error:
            detail += f" · {str(_Redactor().value(receipt.recovery_error, 'message'))[:120]}"
        return detail

    # ── the input composer (PRD-215–217) ─────────────────────────────────────

    def _build_target_input_editor(self, target: BackgroundSession) -> None:
        """Create the canonical editor with registries scoped to *target*."""
        from agenthicc.commands.builtins import build_builtin_registry
        from agenthicc.commands.plugin_loader import discover_command_plugins
        from agenthicc.config import load_config
        from agenthicc.runners.tui_session import _build_skill_command
        from agenthicc.skills.loader import discover_skills
        from agenthicc.tui.conversation_store import AppState
        from agenthicc.tui.input.capabilities import IDLE_CAPABILITIES, ModeCycleCapability
        from agenthicc.tui.input.unified_session import UnifiedInputSession
        from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
        from agenthicc.tui.runtime.mode_manager import ModeManager
        from agenthicc.tui.trigger import TriggerManager
        from agenthicc.tui.triggers.at_mention import AtMentionTrigger
        from agenthicc.tui.triggers.slash_command import SkillTrigger, SlashCommandTrigger
        from agenthicc.tui.workspace.overlay import OverlayHost
        from agenthicc.workflows.registry import build_workflow_registry

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
            project_dir=project_agent_dir, user_dir=user_agent_dir
        ).all_commands:
            commands.register(command)
        skills = discover_skills(project_dir=project_agent_dir, user_dir=user_agent_dir)
        for slug, skill in skills.items():
            command = _build_skill_command(slug, skill)
            if commands.get(command.name) is None:
                commands.register(command)
        workflows = build_workflow_registry(
            project_dir=project_agent_dir, user_dir=user_agent_dir, load_external=True
        )
        triggers = TriggerManager()
        triggers.register(AtMentionTrigger())
        triggers.register(SlashCommandTrigger(commands, workflows))
        triggers.register(SkillTrigger(commands))

        state = AppState()
        state.conversation.session_id.set(target.session_id)
        state.keymap.set(self.keymap)
        modes = ModeManager(app_state=state, default_name=target.mode_name or "Safe")
        overlay = OverlayHost(state)
        overlay.set_redraw_callback(lambda: self._mark_dirty("composer_overlay"))
        bus = CommandBus()
        target_key = (target.session_id, target.attempt)
        history_key = f"{target.session_id}:{target.attempt}"
        history = self._composer_history.get(history_key)
        if history is None:
            data = self._details.get(target.session_id)
            history = list(data.history) if data is not None else []
            self._composer_history[history_key] = history

        async def submit(command: SendMessageCommand) -> ManagerOperationResult:
            result = await self._submit_target_input(target, target_key, command)
            # A trigger-picker submission arrives here from its own task rather
            # than through Enter in _dispatch_composer_key; close on acceptance
            # so a hidden composer cannot swallow the next key.
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
            preserve_submission_text=True,
        )
        # The target's owner applies mode changes; a local copy of its mode must
        # not change here, so Shift+Tab is left out of this editor.
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
        """Submit only to the exact target the editor was opened for.

        ``/stop`` as the whole reply stops the agent instead (AGX-07 §5.1).
        """
        from agenthicc.tui.runtime.commands import SendMessageCommand

        if not isinstance(command, SendMessageCommand):
            return ManagerOperationResult(
                "", target.session_id, "failed", False, message="Invalid input"
            )
        current = await self._service.run_blocking(
            self.store.get, target_key[0], include_deleted=True
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
            if command.text.strip() == "/stop":
                accepted = await self._service.cancel_async(target.session_id)
                if accepted.ok:
                    self._composer_receipt = f"Stopping {target.title or target.session_id[:12]}"
            elif current.status == SessionStatus.WAITING_INPUT:
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
                    from agenthicc.background.input_inbox import BackgroundInput

                    receipt = accepted.value
                    if isinstance(receipt, BackgroundInput) and receipt.deferred:
                        self._composer_receipt = (
                            "Input saved; background recovery needs attention · "
                            f"{command.command_id[:12]}"
                            if receipt.recovery_error
                            else f"Accepted for background recovery · {command.command_id[:12]}"
                        )
                    else:
                        self._composer_receipt = (
                            f"Queued for {target.session_id[:12]} · {command.command_id[:12]}"
                        )
            self._composer_submitted = accepted.ok
            self._notice = (
                self._composer_receipt
                if accepted.ok
                else accepted.message or "Input was rejected; draft retained"
            )
            return accepted
        finally:
            self._composer_submitting = False
            self._mark_dirty("composer_submission_finished")

    def _open_input_composer(self, target: BackgroundSession) -> None:
        if not self._accepts_input(target, None):
            self._notice = f"Input unavailable while the agent is {target.status.value}"
            self._mark_dirty("composer_rejected")
            return
        # A previous accepted submission closes the editor asynchronously but
        # leaves its flag set; reset it before the next key is dispatched.
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
            self._composer_input = None
            self._composer_state = None
            self._composer_building = True
            self._composer_receipt = "Preparing target-scoped commands and mentions…"
            task = self._owned(
                asyncio.create_task(
                    self._build_input_composer_async(target),
                    name=f"agenthicc-hub-input-context-{target.session_id}",
                )
            )
            self._composer_build_task = task
            task.add_done_callback(_consume_task_result)
        self._composer_open = True
        self._mark_dirty("composer_opened")

    async def _build_input_composer_async(self, target: BackgroundSession) -> None:
        """Prepare the target's plugin and configuration context off the event loop."""
        try:
            await self._service.run_blocking(self._build_target_input_editor, target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._notice = f"Target input context unavailable: {type(exc).__name__}: {exc}"[:240]
            self._composer_receipt = "Target context unavailable; draft keys retained"
            self._composer_open = False
            self._mark_dirty("composer_context_failed")
            return
        finally:
            self._composer_building = False
        editor = self._composer_input
        if editor is not None:
            from agenthicc.tui.input.capabilities import _EXIT

            pending, self._composer_pending_keys = self._composer_pending_keys, []
            for key, char in pending:
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

    def _dispatch_composer_key(self, key: Key, ch: str) -> None:
        if self._composer_input is None:
            if self._composer_building:
                if len(self._composer_pending_keys) < 1_024:
                    self._composer_pending_keys.append((key, ch))
                else:
                    self._composer_receipt = "Input context is still loading; key queue is full"
                    self._mark_dirty("composer_key_queue_full")
            return

        async def dispatch() -> object:
            from agenthicc.tui.input.capabilities import _EXIT

            editor = self._composer_input
            if editor is None:
                return None
            result = await editor._dispatch(key, ch)
            if result is _EXIT or self._composer_submitted:
                self._close_input_composer()
            self._mark_dirty("composer_key")
            return result

        existing = self._composer_task
        if existing is not None and not existing.done():
            return
        task = self._owned(asyncio.create_task(dispatch(), name="agenthicc-hub-input-key"))
        self._composer_task = task
        task.add_done_callback(_consume_task_result)

    def _composer_lines(self, width: int) -> list[RenderableType]:
        from rich.text import Text

        from agenthicc.tui.workspace.components import ComposerComponent

        target = self._composer_target
        lines: list[RenderableType] = [
            Text(
                f" Send input · {target.title or target.session_id if target else ''}",
                style="bold cyan",
                no_wrap=True,
                overflow="ellipsis",
            )
        ]
        overlay = self._composer_overlay
        if overlay is not None and overlay.active:
            rendered = overlay.render()
            if rendered is not None:
                lines.append(rendered)
        if self._composer_state is not None:
            lines.append(ComposerComponent(self._composer_state).render())
        elif self._composer_building:
            lines.append(Text(" Loading target-scoped input context…", style="yellow"))
        if self._composer_submitting:
            lines.append(Text(" Submitting to the agent…", style="yellow"))
        elif self._composer_receipt:
            lines.append(Text(" " + self._composer_receipt, style="green"))
        lines.append(
            Text(
                " Enter send · Ctrl+J newline · /stop stops the agent · Esc back",
                style="dim",
                no_wrap=True,
                overflow="ellipsis",
            )
        )
        return lines

    # ── deletion (PRD-208) ───────────────────────────────────────────────────

    async def _on_delete_progress(self, _session_id: str, phase: str) -> None:
        self._deletion_phase = phase
        self._mark_dirty("operation_changed")

    def _start_delete(self, ids: tuple[str, ...]) -> None:
        if self._deletion_task is not None or not ids:
            return
        self._deleting_ids = ids
        self._deletion_operation_id = uuid.uuid4().hex
        self._deletion_phase = "starting"
        self._deletion_error = ""
        self._mark_dirty("operation_changed")
        self._deletion_task = self._owned(
            asyncio.create_task(
                self._service.delete_async(
                    ids,
                    operation_id=self._deletion_operation_id,
                    requested_by="agents",
                    progress=self._on_delete_progress,
                ),
                name=f"agenthicc-delete-{self._deletion_operation_id}",
            )
        )

    def _poll_delete(self) -> None:
        task = self._deletion_task
        if task is None or not task.done():
            return
        try:
            result = task.result()
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
        self._deleting_ids = ()
        self._deletion_phase = result.phase
        self.marked_ids.difference_update(result.deleted)
        if result.failures:
            self._deletion_error = "; ".join(
                f"{failure.session_id}: {failure.message}" for failure in result.failures
            )[:2_000]
            self._notice = "Delete failed: " + self._deletion_error[:200]
        else:
            self._deletion_error = ""
            self._notice = (
                f"Deleted {len(result.deleted)} agent(s) to recoverable trash "
                f"(operation {result.operation_id[:12]})."
            )
        self._deletion_operation_id = ""
        if self._detail_id in result.deleted:
            self._close_detail()
        self._mark_dirty("projection_changed")
        self._request_load("delete_complete", force=True)

    async def _shutdown_delete(self) -> None:
        """Briefly drain a deletion before closing; it is durable and recoverable."""
        task = self._deletion_task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
        except TimeoutError:
            # The supervisor claims the exact target before moving artifacts; a
            # cancelled deletion is finished by the next hub's recovery.
            task.cancel()
            self._deletion_error = "Deletion continues in recovery state after the hub closed"
        except asyncio.CancelledError:
            task.cancel()
        finally:
            self._poll_delete()

    # ── keys ─────────────────────────────────────────────────────────────────

    def handle_key(self, key: Key, ch: str = "") -> HubResult | None:
        """Handle one key; a result means the hub is done or hands off."""
        self._last_key_at = time.monotonic()
        self._record_metric("key_received")
        self._mark_dirty("input")
        if key is Key.CTRL_C:
            # Raw mode turns Ctrl+C into a key; it always leaves the hub.
            return HubResult("exit")
        if key is Key.MOUSE:
            return self._handle_mouse(ch)
        if key in (Key.FOCUS_IN, Key.FOCUS_OUT, Key.UNKNOWN):
            return None
        if self._choice is not None:
            return self._handle_choice(key, ch)
        if self._prompt is not None:
            self._handle_prompt(key, ch)
            return None
        if self._library_view_lines is not None:
            self._handle_library_view_key(key, ch)
            return None
        if self._composer_open:
            overlay_active = self._composer_overlay is not None and self._composer_overlay.active
            if key is Key.ESC and not overlay_active:
                self._close_input_composer()
            elif not self._composer_submitting:
                self._dispatch_composer_key(key, ch)
            return None
        if self._environment_action_mode:
            self._handle_environment_key(key, ch)
            return None
        if self._diff_lines is not None:
            self._handle_pager_key(key, ch)
            return None
        if self.help_visible:
            self.help_visible = False
            return None
        dispatcher = self._dispatcher
        if dispatcher is None or dispatcher.keymap is not self.keymap:
            dispatcher = self._dispatcher = Dispatcher(self.keymap)
        action = dispatcher.dispatch(self.context, key_name(key, ch), self.conditions())
        if action is None:
            return None
        return self._run_action(action.name, key)

    def _run_action(self, name: str, key: Key) -> HubResult | None:
        handler = self.handlers(key).get(name)
        return handler() if handler is not None else None

    def handlers(self, key: Key = Key.CHAR) -> dict[str, Callable[[], HubResult | None]]:
        """What each ``agents.*`` action does; *key* is the key that triggered it."""
        return {
            "agents.up": functools.partial(self._move, -1),
            "agents.down": functools.partial(self._move, 1),
            "agents.page_up": functools.partial(self._move, -max(1, self._list_height - 1)),
            "agents.page_down": functools.partial(self._move, max(1, self._list_height - 1)),
            "agents.first": functools.partial(self._move_to, 0),
            "agents.last": functools.partial(self._move_to, len(self._rows) - 1),
            "agents.group_previous": functools.partial(self._jump_group, -1),
            "agents.group_next": functools.partial(self._jump_group, 1),
            "agents.open": self._act_open,
            "agents.peek": self._act_peek,
            "agents.close_peek": self._act_close_peek,
            "agents.approve": functools.partial(self._act_approve, True),
            "agents.reject": functools.partial(self._act_approve, False),
            "agents.input": self._act_input,
            "agents.resume": functools.partial(self._act_lifecycle, "resume"),
            "agents.retry": functools.partial(self._act_lifecycle, "retry"),
            "agents.stop": functools.partial(self._act_bulk, "stop"),
            "agents.archive": functools.partial(self._act_bulk, "archive"),
            "agents.pin": self._act_pin,
            "agents.rename": functools.partial(self._act_prompt, "rename"),
            "agents.labels": functools.partial(self._act_prompt, "labels"),
            "agents.priority": self._act_priority,
            "agents.mark": self._act_mark,
            "agents.delete": self._act_delete,
            "agents.expire": self._act_expire,
            "agents.expand": functools.partial(self._act_tree, False),
            "agents.collapse": functools.partial(self._act_tree, True),
            "agents.move_up": functools.partial(self._act_reorder, -1),
            "agents.move_down": functools.partial(self._act_reorder, 1),
            "agents.search": self._act_search,
            "agents.sort": self._act_sort,
            "agents.group": self._act_group,
            "agents.next_tab": functools.partial(self._cycle_tab, 1),
            "agents.previous_tab": functools.partial(self._cycle_tab, -1),
            "agents.tab_running": functools.partial(self.switch_tab, Tab.RUNNING),
            "agents.tab_needs_you": functools.partial(self.switch_tab, Tab.NEEDS_YOU),
            "agents.tab_history": functools.partial(self.switch_tab, Tab.HISTORY),
            "agents.tab_library": functools.partial(self.switch_tab, Tab.LIBRARY),
            "agents.library_create": self._act_library_create,
            "agents.library_generate": self._act_library_generate,
            "agents.library_edit": self._act_library_edit,
            "agents.library_duplicate": self._act_library_duplicate,
            "agents.library_delete": self._act_library_delete,
            "agents.library_default": self._act_library_default,
            "agents.library_validate": self._act_library_validate,
            "agents.dispatch": self._act_dispatch,
            "agents.refresh": self._act_refresh,
            "agents.pause": self._act_pause,
            "agents.help": self._act_help,
            "agents.quit": functools.partial(self._act_quit, key),
            "agents.attach": self._act_attach,
            "agents.back": self._close_detail,
            "agents.scroll_up": functools.partial(self._scroll_detail, -1),
            "agents.scroll_down": functools.partial(self._scroll_detail, 1),
            "agents.scroll_page_up": functools.partial(
                self._scroll_detail, -max(1, self._detail_height - 2)
            ),
            "agents.scroll_page_down": functools.partial(
                self._scroll_detail, max(1, self._detail_height - 2)
            ),
            "agents.scroll_top": functools.partial(self._scroll_detail, -1_000_000),
            "agents.scroll_bottom": functools.partial(self._scroll_detail, 1_000_000),
            "agents.follow": self._act_follow,
            "agents.transcript": self._act_full_transcript,
            "agents.diff": self._act_diff,
            "agents.open_request": self._act_open_request,
            "agents.environment": self._act_environment,
        }

    # ── navigation ───────────────────────────────────────────────────────────

    def _selectable(self) -> list[int]:
        return [index for index, row in enumerate(self._rows) if row.selectable]

    def _select_index(self, index: int) -> None:
        if not 0 <= index < len(self._rows) or not self._rows[index].selectable:
            return
        key = self._rows[index].key
        if key != self._selected_key:
            self._selected_key = key
            self._selected_by_tab[self.tab] = key
            if self._detail_id is None:
                self._detail_item_id = None
            if self._peek:
                self._schedule_detail_read()
        self._mark_dirty("selection_changed")
        if (
            self.tab is Tab.HISTORY
            and len(self._history) < self._history_total
            and index >= len(self._rows) - HISTORY_CHUNK // 4
            and self._loop is not None
            and (self._history_task is None or self._history_task.done())
        ):
            self._history_task = self._owned(
                asyncio.create_task(self._extend_history(), name="agenthicc-hub-history")
            )

    def _move(self, delta: int) -> None:
        indices = self._selectable()
        if not indices:
            return
        current = self._row_index.get(self._selected_key or "")
        if current is None:
            self._select_index(indices[0] if delta >= 0 else indices[-1])
            return
        position = min(range(len(indices)), key=lambda i: abs(indices[i] - current))
        self._select_index(indices[min(max(position + delta, 0), len(indices) - 1)])

    def _move_to(self, index: int) -> None:
        indices = self._selectable()
        if not indices:
            return
        target = indices[0] if index <= 0 else indices[-1]
        self._select_index(target)

    def _jump_group(self, direction: int) -> None:
        current = self._row_index.get(self._selected_key or "", 0)
        headers = [index for index, row in enumerate(self._rows) if row.kind is RowKind.HEADER]
        if direction > 0:
            following = [index for index in headers if index > current]
            if following:
                self._move_to_first_after(following[0])
            return
        own = max((index for index in headers if index < current), default=None)
        if own is None:
            return
        previous = [index for index in headers if index < own]
        self._move_to_first_after(previous[-1] if previous else own)

    def _move_to_first_after(self, header_index: int) -> None:
        for index in range(header_index + 1, len(self._rows)):
            if self._rows[index].selectable:
                self._select_index(index)
                return

    def switch_tab(self, tab: Tab) -> None:
        """Show *tab*, keeping each tab's own selection."""
        if tab is self.tab and self._detail_id is None:
            return
        if (
            self._pending_attention_target is not None
            and self._pending_attention_target[0] is not tab
        ):
            self._pending_attention_target = None
        if self._selected_key is not None:
            self._selected_by_tab[self.tab] = self._selected_key
        self._close_detail()
        self._peek = False
        self.tab = tab
        self._top = 0
        self._selected_key = self._selected_by_tab.get(tab)
        self._rebuild_rows()
        if tab is Tab.HISTORY:
            self._request_load("tab_changed", force=True)
        elif tab is Tab.LIBRARY and not self._library_loaded and self._loop is not None:
            self._side_task = self._owned(asyncio.create_task(self._show_library()))
        self._mark_dirty("tab_changed")

    def _cycle_tab(self, direction: int) -> None:
        index = _TABS.index(self.tab)
        self.switch_tab(_TABS[(index + direction) % len(_TABS)])

    # ── opening, peeking, detail ─────────────────────────────────────────────

    def _act_open(self) -> HubResult | None:
        row = self.selected_row()
        if row is None:
            return None
        if row.kind is RowKind.SAVED and row.saved is not None:
            return self._hand_off(HubResult("open_saved", row.saved.session_id), row.saved.title)
        if row.kind is RowKind.SUBAGENT and row.worker is not None:
            return HubResult("subagent", f"{row.pool_id}:{row.worker.worker_id}")
        session_id = row.session_id
        if session_id is not None:
            self._open_detail(session_id, item_id=row.item.item_id if row.item else None)
        elif row.entry is not None:
            self._show_library_entry(row.entry)
        elif row.scheduled is not None:
            self._notice = (
                f"Schedule {row.scheduled.schedule_id}: {row.scheduled.name}. "
                "Manage it with /schedules or agenthicc schedules."
            )[:240]
        return None

    def _selected_library_entry(self) -> LibraryEntry | None:
        row = self.selected_row()
        return row.entry if row is not None and row.kind is RowKind.DEFINITION else None

    def _show_library_entry(self, entry: LibraryEntry) -> None:
        spec = entry.spec
        if self._library_effective_task is not None and not self._library_effective_task.done():
            self._library_effective_task.cancel()
        self._library_effective_generation += 1
        generation = self._library_effective_generation
        if self._effective_tools is None:
            effective_tools_line = (
                "Effective tools: session-specific; "
                "open this definition in an active session to calculate."
            )
        elif self._loop is None:
            effective_tools_line = (
                "Effective tools: unavailable until the active session is running."
            )
        else:
            effective_tools_line = (
                "Effective tools (model-visible; mode gates apply at invocation): calculating…"
            )
        lines = [
            f"Name: {entry.name}",
            f"Source: {entry.source} · state: {entry.state}",
            f"Description: {entry.description or '(none)'}",
            f"Mode: {entry.mode or '(unknown)'} · model: {entry.model or '(session default)'}",
            f"Declared tools: {entry.tools or '(none)'}",
        ]
        if spec is not None:
            lines.extend(
                (
                    f"Capabilities: {', '.join(spec.capabilities) if spec.capabilities is not None else 'all session-allowed capabilities'}",
                    f"Ceiling: {spec.ceiling or '(session mode)'} · max steps: {spec.max_steps or '(session default)'}",
                    f"Color: {spec.color or '(default)'} · hidden: {spec.hidden}",
                    effective_tools_line,
                    "",
                    "Rendered prompt:",
                    *(spec.prompt.splitlines() or ["(empty)"]),
                )
            )
        if entry.path:
            lines.extend(("", f"Path: {entry.path}"))
        if entry.note:
            lines.extend(("", f"Status: {entry.note}"))
        self._library_view_title = f"Agent definition · {entry.name}"
        self._library_view_lines = lines
        self._library_view_scroll = 0
        if spec is not None and self._effective_tools is not None and self._loop is not None:
            self._library_effective_task = self._owned(
                asyncio.create_task(
                    self._load_library_effective_tools(spec, generation),
                    name="agenthicc-library-effective-tools",
                )
            )
        self._mark_dirty("library_view")

    def _effective_tool_policy_snapshot(self) -> AppState:
        """Copy the UI state read by agent restrictions before using a worker."""
        snapshot = type(self._state).create()
        snapshot.active_mode.set(self._state.active_mode())
        snapshot.cli_flags = self._state.cli_flags
        return snapshot

    async def _load_library_effective_tools(self, spec: AgentSpec, generation: int) -> None:
        callback = self._effective_tools
        if callback is None:
            return
        try:
            policy_state = self._effective_tool_policy_snapshot()
            names = cast(
                "tuple[str, ...]", await self._service.run_blocking(callback, spec, policy_state)
            )
            detail = ", ".join(names) if names else "(none)"
        except Exception as exc:  # noqa: BLE001 - details must not hide the definition
            detail = f"unavailable ({type(exc).__name__})"
        if generation != self._library_effective_generation or self._library_view_lines is None:
            return
        prefix = "Effective tools (model-visible; mode gates apply at invocation):"
        for index, line in enumerate(self._library_view_lines):
            if line.startswith(prefix):
                self._library_view_lines[index] = f"{prefix} {detail}"
                break
        self._mark_dirty("library_effective_tools")

    def _handle_library_view_key(self, key: Key, ch: str) -> None:
        lines = self._library_view_lines or []
        page = max(1, self._list_height - 2)
        if key in (Key.ESC, Key.ENTER) or (key is Key.CHAR and ch == "q"):
            self._library_effective_generation += 1
            if self._library_effective_task is not None and not self._library_effective_task.done():
                self._library_effective_task.cancel()
            self._library_view_lines = None
            self._library_view_title = ""
        elif key is Key.UP or (key is Key.CHAR and ch == "k"):
            self._library_view_scroll = max(0, self._library_view_scroll - 1)
        elif key is Key.DOWN or (key is Key.CHAR and ch == "j"):
            self._library_view_scroll = min(max(0, len(lines) - 1), self._library_view_scroll + 1)
        elif key is Key.PAGE_UP:
            self._library_view_scroll = max(0, self._library_view_scroll - page)
        elif key is Key.PAGE_DOWN:
            self._library_view_scroll = min(
                max(0, len(lines) - 1), self._library_view_scroll + page
            )
        elif key is Key.HOME:
            self._library_view_scroll = 0
        elif key is Key.END:
            self._library_view_scroll = max(0, len(lines) - page)
        self._mark_dirty("library_view_scroll")

    def _act_library_create(self) -> None:
        self._begin_library_flow("create")

    def _act_library_generate(self) -> None:
        if self._generate_definition is None:
            self._notice = (
                "Draft generation is available only in a session with a configured model."
            )
            return
        self._begin_library_flow("generate")

    def _act_library_duplicate(self) -> None:
        entry = self._selected_library_entry()
        if entry is None or entry.spec is None:
            self._notice = (
                "This definition cannot be duplicated because it has no valid Markdown spec."
            )
            return
        self._begin_library_flow("duplicate", source=entry.spec)

    def _act_library_edit(self) -> None:
        entry = self._selected_library_entry()
        if (
            entry is None
            or entry.source not in ("project", "user")
            or not entry.path.endswith(".md")
        ):
            self._notice = "Only project and user Markdown definitions can be edited."
            return
        if self._loop is None:
            self._notice = "The Library editor is not ready."
            return
        self._library_edit_entry = entry
        self._library_editor_scroll = 0
        if self._external_editor is not None:
            self._library_task = self._owned(
                asyncio.create_task(
                    self._edit_library_with_external_editor(entry),
                    name="agenthicc-library-external-edit",
                )
            )
            return
        self._library_task = self._owned(
            asyncio.create_task(self._load_library_editor(entry), name="agenthicc-library-edit")
        )

    async def _edit_library_with_external_editor(self, entry: LibraryEntry) -> None:
        path = Path(entry.path)
        scope: DefinitionScope = "project" if entry.source == "project" else "user"
        try:
            value = await self._service.run_blocking(
                _read_library_definition,
                path,
                scope,
                self._cwd,
            )
            if not isinstance(value, str):
                raise OSError("the definition file could not be read")
            editor = self._external_editor
            if editor is None:
                return
            await editor(path, scope)
            self._library_loaded = False
            await self._show_library()
            self._notice = f"Edited {entry.name}; the Library was reloaded."
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface editor and path errors in the Library
            self._notice = f"Could not edit definition: {type(exc).__name__}: {exc}"[:240]
        finally:
            self._library_edit_entry = None
            self._mark_dirty("library_external_edit_finished")

    async def _load_library_editor(self, entry: LibraryEntry) -> None:
        path = Path(entry.path)
        scope: DefinitionScope = "project" if entry.source == "project" else "user"
        try:
            value = await self._service.run_blocking(
                _read_library_definition,
                path,
                scope,
                self._cwd,
            )
            if not isinstance(value, str):
                raise OSError("the definition file could not be read")
            self._prompt = _Prompt(
                "library",
                f"Edit {entry.name}",
                LineEditor(value, multiline=True, limit=32_000),
                target="edit",
            )
            self._notice = "Edit the Markdown; Enter saves, Ctrl+J inserts a newline."
        except (OSError, UnicodeError, ValueError) as exc:
            self._library_edit_entry = None
            self._notice = f"Could not open definition: {exc}"[:240]
        self._mark_dirty("library_editor_opened")

    def _act_library_delete(self) -> None:
        entry = self._selected_library_entry()
        if (
            entry is None
            or entry.source not in ("project", "user")
            or not entry.path.endswith(".md")
        ):
            self._notice = "Only project and user Markdown definitions can be deleted."
            return
        self._choice = _Choice(
            "library_delete",
            f"Delete {entry.name} at {entry.path}? This cannot be undone.",
            ("Delete", "Cancel"),
            target=entry.path,
        )
        self._mark_dirty("library_delete_confirm")

    def _act_library_default(self) -> None:
        entry = self._selected_library_entry()
        if entry is None or entry.spec is None or not entry.spec.primary or entry.state != "active":
            self._notice = "Only an active primary definition can be the project default."
            return
        self._start_library_task(
            lambda: self._change_library(
                "set default", lambda: set_project_default(self._cwd, entry.name)
            )
        )

    def _act_library_validate(self) -> None:
        self._start_library_task(self._validate_library)

    def _begin_library_flow(
        self,
        operation: Literal["create", "duplicate", "generate"],
        *,
        source: AgentSpec | None = None,
    ) -> None:
        self._library_draft = _LibraryDraft(operation=operation, source=source)
        label = {
            "create": "Create a definition in:",
            "duplicate": "Duplicate to:",
            "generate": "Generate a draft in:",
        }[operation]
        self._choice = _Choice("library_scope", label, ("Project", "User"), target=operation)
        self._mark_dirty("library_scope_choice")

    def _library_scope_chosen(self, choice: _Choice) -> None:
        draft = self._library_draft
        if draft is None:
            return
        draft.scope = "project" if choice.index == 0 else "user"
        if draft.operation == "create":
            self._library_prompt("Name", "name")
        elif draft.operation == "duplicate":
            source = draft.source
            if source is None:
                self._notice = "The selected definition is no longer available."
                return
            self._library_prompt("New name", "name", f"{source.name}-copy")
        else:
            self._library_prompt("Name", "name")

    def _library_prompt(self, label: str, target: str, initial: str = "") -> None:
        multiline = target in ("create_prompt", "edit")
        self._prompt = _Prompt(
            "library", label, LineEditor(initial, multiline=multiline, limit=32_000), target=target
        )
        self._library_editor_scroll = 0
        self._mark_dirty("library_prompt")

    def _submit_library_prompt(self, prompt: _Prompt) -> None:
        draft = self._library_draft
        text = prompt.editor.text
        target = prompt.target
        if target == "edit":
            self._submit_library_edit(prompt)
            return
        if draft is None:
            self._notice = "The definition form was cancelled."
            return
        if target == "name":
            draft.name = text.strip()
            if draft.operation == "duplicate":
                source = draft.source
                if source is None:
                    self._notice = "The selected definition is no longer available."
                    self._library_draft = None
                    return
                spec = replace(source, name=draft.name, source=draft.scope, path=None)
                self._review_library_spec(spec, draft.scope)
            elif draft.operation == "generate":
                self._library_prompt("One-line description", "description")
            else:
                self._library_prompt("Description", "description")
        elif target == "description":
            draft.description = text.strip()
            if draft.operation == "generate":
                self._start_library_task(lambda: self._generate_library_draft(draft))
            else:
                self._library_prompt("Mode (primary, subagent, all)", "mode")
        elif target == "mode":
            draft.mode = text.strip() or "all"
            self._library_prompt("Model (blank uses the session model)", "model")
        elif target == "model":
            draft.model = text.strip()
            self._library_prompt("Tools, comma separated (blank allows session tools)", "tools")
        elif target == "tools":
            values = tuple(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))
            draft.tools = values or None
            self._library_prompt("System prompt", "create_prompt")
        elif target == "create_prompt":
            draft.prompt = text.strip()
            spec = AgentSpec(
                name=draft.name,
                description=draft.description,
                prompt=draft.prompt,
                mode=draft.mode,
                model=draft.model,
                tools=draft.tools,
                source=draft.scope,
            )
            self._review_library_spec(spec, draft.scope)

    def _submit_library_edit(self, prompt: _Prompt) -> None:
        entry = self._library_edit_entry
        if entry is None:
            self._notice = "The definition being edited is no longer selected."
            return
        try:
            path = Path(entry.path)
            spec = parse_definition(prompt.editor.text, path=path, source=entry.source)
            if spec.name != path.stem:
                raise ValueError(
                    "renaming in the editor is not supported; duplicate, then delete the old definition"
                )
        except (DefinitionError, ValueError) as exc:
            self._notice = str(exc)[:240]
            self._prompt = prompt
            return
        scope: DefinitionScope = "project" if entry.source == "project" else "user"
        self._library_edit_entry = None
        self._start_library_task(
            lambda: self._change_library(
                f"Updated {spec.name}",
                lambda: write_definition(spec, scope=scope, project_root=self._cwd, overwrite=True),
            )
        )

    def _review_library_spec(self, spec: AgentSpec, scope: DefinitionScope) -> None:
        try:
            rendered = render_definition(spec)
            parse_definition(rendered, source=scope)
        except (DefinitionError, ValueError) as exc:
            self._notice = str(exc)[:240]
            self._library_draft = None
            return
        self._library_pending = (replace(spec, source=scope, path=None), scope)
        self._library_draft = None
        self._library_view_title = f"Review definition · {spec.name}"
        self._library_view_lines = rendered.splitlines()
        self._library_view_scroll = 0
        self._choice = _Choice(
            "library_save", "Save this reviewed definition?", ("Save", "Discard")
        )
        self._mark_dirty("library_draft_review")

    async def _generate_library_draft(self, draft: _LibraryDraft) -> None:
        generator = self._generate_definition
        if generator is None:
            self._notice = "Draft generation requires a configured session model."
            return
        self._notice = "Generating definition draft…"
        self._mark_dirty("library_generation_started")
        try:
            content = await generator(draft.name, draft.description)
            spec = parse_definition(content, source=draft.scope)
            if spec.name != draft.name:
                raise ValueError(
                    f"the generated definition used name {spec.name!r}, not {draft.name!r}"
                )
        except (DefinitionError, ValueError, RuntimeError) as exc:
            self._notice = f"Could not generate a valid definition: {exc}"[:240]
            self._library_draft = None
            self._mark_dirty("library_generation_failed")
            return
        self._review_library_spec(spec, draft.scope)

    def _start_library_task(self, operation: Callable[[], Awaitable[None]]) -> None:
        if self._loop is None:
            self._notice = "The Library is not ready for this operation."
            return
        if self._library_task is not None and not self._library_task.done():
            self._notice = "A Library operation is already running."
            return

        async def run() -> None:
            await operation()

        self._library_task = self._owned(
            asyncio.create_task(run(), name="agenthicc-library-operation")
        )

    async def _change_library(self, label: str, operation: Callable[[], object]) -> None:
        try:
            result = await self._service.run_blocking(operation)
        except Exception as exc:  # noqa: BLE001 - surface file and trust errors in the Library
            self._notice = f"{label} failed: {type(exc).__name__}: {exc}"[:240]
            self._mark_dirty("library_operation_failed")
            return
        if isinstance(result, Path):
            self._notice = f"{label}: {result}"
        else:
            self._notice = f"{label} completed."
        self._library_loaded = False
        await self._show_library()
        self._mark_dirty("library_operation_complete")

    async def _validate_library(self) -> None:
        try:
            loaded = await self._service.run_blocking(self._sources.library)
        except Exception as exc:  # noqa: BLE001 - validation results belong in the Library
            self._notice = f"Could not validate definitions: {type(exc).__name__}: {exc}"[:240]
            self._mark_dirty("library_validation_failed")
            return
        entries = (
            [item for item in loaded if isinstance(item, LibraryEntry)]
            if isinstance(loaded, list)
            else []
        )
        self._library = entries
        self._library_loaded = True
        self._rebuild_rows()
        problems = [entry for entry in entries if entry.state != "active"]
        lines = [f"{len(entries)} definition(s) checked."]
        if problems:
            lines.extend(("", "Definitions needing attention:"))
            for entry in problems:
                lines.append(f"{entry.name} · {entry.state}: {entry.note or 'not active'}")
                if entry.path:
                    lines.append(f"  {entry.path}")
        else:
            lines.append("All agent definitions are valid and active.")
        self._library_view_title = "Agent definition validation"
        self._library_view_lines = lines
        self._library_view_scroll = 0
        self._mark_dirty("library_validation_complete")

    def _save_library_pending(self) -> None:
        pending, self._library_pending = self._library_pending, None
        self._library_view_lines = None
        self._library_view_title = ""
        if pending is None:
            self._notice = "No definition draft is waiting to be saved."
            return
        spec, scope = pending
        self._start_library_task(
            lambda: self._change_library(
                f"Saved {spec.name}",
                lambda: write_definition(spec, scope=scope, project_root=self._cwd),
            )
        )

    def _delete_library_path(self, value: str) -> None:
        path = Path(value)

        def delete() -> None:
            delete_definition(path, project_root=self._cwd)
            if project_default(self._cwd) == path.stem:
                set_project_default(self._cwd, None)

        self._start_library_task(lambda: self._change_library(f"Deleted {path.stem}", delete))

    def _open_detail(self, session_id: str, *, item_id: str | None = None) -> None:
        # Opening reads the change summary afresh.
        self._changes_read_at.pop(session_id, None)
        self._detail_id = session_id
        self._detail_item_id = item_id
        self._detail_scroll = 0
        self._follow = True
        self._full_transcript = False
        self._diff_lines = None
        self._peek = False
        session = self._sessions.get(session_id)
        if session is not None:
            self._prefs.mark_seen(session)
        self._record_metric("detail_opened")
        self._schedule_detail_read()
        self._mark_dirty("detail_opened")

    def _close_detail(self) -> None:
        if self._detail_id is None:
            return
        self._detail_id = None
        self._detail_item_id = None
        self._diff_lines = None
        self._detail_scroll = 0
        if self._composer_open:
            self._close_input_composer()
        self._mark_dirty("detail_closed")

    def _act_peek(self) -> None:
        self._peek = not self._peek
        if self._peek:
            session = self._row_session(self.selected_row())
            if session is not None:
                self._prefs.mark_seen(session)
            self._schedule_detail_read()
        self._mark_dirty("peek_changed")

    def _act_close_peek(self) -> None:
        self._peek = False
        if self._composer_open:
            self._close_input_composer()
        self._mark_dirty("peek_changed")

    def _hand_off(self, result: HubResult, title: str = "") -> HubResult | None:
        """Leave for a session; inside a session, ask before switching (AGX-05 §5.2)."""
        if not self.in_session:
            return result
        name = title or (result.session_id or "")[:12]
        self._choice = _Choice(
            "switch",
            f"Switch to {name}? This session reopens when you leave it.",
            ("Switch", "Stay"),
            result=result,
        )
        return None

    def _act_attach(self) -> HubResult | None:
        session_id = self._detail_id
        if session_id is None:
            return None
        session = self._sessions.get(session_id)
        if self.in_session:
            title = session.title if session is not None else ""
            return self._hand_off(HubResult("attach", session_id), title)
        self._start_attach(session_id)
        return None

    def _start_attach(self, session_id: str) -> None:
        """Validate the exact target off the key path; the engine hands it back."""
        if session_id in self._sessions:
            self._prefs.mark_seen(self._sessions[session_id])
        self._start_operation(
            session_id,
            "attach",
            functools.partial(self._service.attach_prepare_async, session_id),
        )

    @property
    def attach_result(self) -> HubResult | None:
        """An attach validated by an operation, taken once."""
        result, self._attach_result = self._attach_result, None
        return result

    # ── operations on the selected agent ─────────────────────────────────────

    def _act_approve(self, allowed: bool) -> None:
        _row, session, item = self._target()
        if item is not None and item.kind == "approval":
            options = item.target.get("mode_options")
            if (
                allowed
                and item.target.get("request_kind") == "plan_review"
                and isinstance(options, list)
                and options
            ):
                self._choice = _Choice(
                    "approve_mode",
                    "Approve the plan and continue in",
                    tuple(str(option) for option in options),
                    target=item.item_id,
                    item=item,
                )
                return
            response: dict[str, object] = {"allowed": allowed}
            if allowed and item.target.get("workspace_access"):
                response["scope_grant"] = "target_once"
            self._respond(item, response, "approve" if allowed else "reject")
        elif session is not None and session.status is SessionStatus.WAITING_APPROVAL:
            # An approval recorded before the inbox existed has no item.
            self._start_operation(
                session.session_id,
                "approval",
                functools.partial(self._service.approve_async, session.session_id, allowed),
            )

    def _respond(self, item: BackgroundInboxItem, response: dict[str, object], label: str) -> None:
        """Resolve the exact inbox item, at the version the user saw (PRD-227)."""
        self._start_operation(
            item.session_id,
            label,
            functools.partial(
                self._service.inbox_respond_async,
                item.item_id,
                response,
                expected_version=item.version,
                idempotency_key=uuid.uuid4().hex,
            ),
        )

    def _act_input(self) -> None:
        _row, session, item = self._target()
        if item is not None and item.kind != "approval":
            # Answers are delivered verbatim: line breaks and spacing kept.
            self._prompt = _Prompt(
                "answer", "Answer", LineEditor(multiline=True), target=item.item_id, item=item
            )
        elif item is not None:
            self._prompt = _Prompt(
                "feedback",
                "Plan feedback (Enter approves)",
                LineEditor(multiline=True),
                target=item.item_id,
                item=item,
            )
        elif session is not None:
            if self._detail_id is None:
                self._peek = True
                self._schedule_detail_read()
            self._open_input_composer(session)
        self._mark_dirty("prompt_opened")

    def _act_lifecycle(self, action: Literal["resume", "retry"]) -> None:
        _row, session, _item = self._target()
        if session is None:
            return
        operation = self._service.resume_async if action == "resume" else self._service.retry_async
        self._start_operation(
            session.session_id, action, functools.partial(operation, session.session_id)
        )

    def _targets(self) -> list[str]:
        """Marked agents, or else the selected one."""
        if self.marked_ids:
            return sorted(self.marked_ids)
        _row, session, _item = self._target()
        return [session.session_id] if session is not None else []

    def _act_bulk(self, action: Literal["stop", "archive"]) -> None:
        for session_id in self._targets():
            operation = (
                self._service.cancel_async if action == "stop" else self._service.archive_async
            )
            self._start_operation(session_id, action, functools.partial(operation, session_id))
        self.marked_ids.clear()

    def _act_pin(self) -> None:
        _row, session, _item = self._target()
        if session is None:
            return
        pinned = not session.pinned
        order = [item for item in self._prefs.pin_order if item != session.session_id]
        if pinned:
            order.append(session.session_id)
        self._prefs.pin_order = order
        self._start_operation(
            session.session_id,
            "pin",
            functools.partial(self._service.pin_async, session.session_id, pinned),
        )

    def _act_prompt(self, kind: Literal["rename", "labels"]) -> None:
        _row, session, _item = self._target()
        if session is None:
            return
        if kind == "rename":
            self._prompt = _Prompt(
                "rename", "Rename to", LineEditor(session.title), target=session.session_id
            )
        else:
            self._prompt = _Prompt(
                "labels",
                "Labels (comma-separated)",
                LineEditor(", ".join(session.labels)),
                target=session.session_id,
            )

    def _act_priority(self) -> None:
        _row, session, _item = self._target()
        if session is None:
            return
        options = ("urgent", "normal", "low")
        current = session.priority.value
        self._choice = _Choice(
            "priority",
            "Queue priority",
            options,
            index=options.index(current) if current in options else 1,
            target=session.session_id,
        )

    def _act_mark(self) -> None:
        _row, session, _item = self._target()
        if session is None:
            return
        if session.session_id in self.marked_ids:
            self.marked_ids.discard(session.session_id)
        else:
            self.marked_ids.add(session.session_id)
        self._move(1)

    def _act_delete(self) -> None:
        if self._deleting_ids:
            return
        ids = tuple(self._targets())
        if ids:
            self._deletion_error = ""
            self._start_delete(ids)

    def _act_expire(self) -> None:
        row = self.selected_row()
        item = row.item if row is not None else None
        if item is None:
            return
        self._start_operation(
            item.session_id,
            "expire",
            functools.partial(
                self._service.inbox_expire_async,
                item.item_id,
                expected_version=item.version,
                idempotency_key=uuid.uuid4().hex,
            ),
        )

    def _act_tree(self, collapse: bool) -> None:
        row = self.selected_row()
        if row is None or row.session is None:
            return
        if collapse:
            self._collapsed.add(row.session.session_id)
        else:
            self._collapsed.discard(row.session.session_id)
        self._rebuild_rows()
        self._mark_dirty("tree_changed")

    def _act_reorder(self, direction: int) -> None:
        row = self.selected_row()
        session = self._row_session(row)
        if session is None or not session.pinned:
            return
        pinned = [
            r.session.session_id
            for r in self._rows
            if r.session is not None and r.group == "Pinned" and r.depth == 0
        ]
        if session.session_id not in pinned:
            return
        index = pinned.index(session.session_id)
        other = index + direction
        if not 0 <= other < len(pinned):
            return
        pinned[index], pinned[other] = pinned[other], pinned[index]
        rest = [item for item in self._prefs.pin_order if item not in pinned]
        self._prefs.pin_order = [*pinned, *rest]
        self._rebuild_rows()
        self._mark_dirty("pins_reordered")

    # ── search, sort, group ──────────────────────────────────────────────────

    def _act_search(self) -> None:
        self._search_before = self._selected_key
        self._prompt = _Prompt("search", "/", LineEditor(self._search.text))

    def set_search(self, text: str) -> None:
        """Filter the current tab as the user types (AGX-06 §5.4)."""
        started = time.monotonic()
        search = parse_search(text)
        if search == self._search:
            return
        self._search = search
        self._history_want = HISTORY_CHUNK
        self._top = 0
        self._rebuild_rows()
        if self.tab is Tab.HISTORY:
            self._request_load("search_changed", force=True)
        self._record_metric("search_updated", time.monotonic() - started)
        self._mark_dirty("search_changed")

    def _clear_search(self) -> None:
        self.set_search("")
        if self._search_before in self._row_index:
            self._selected_key = self._search_before
        self._search_before = None

    def _act_sort(self) -> None:
        self._prefs.sort = self._prefs.sort.next()
        self._notice = f"Sorted by {self._prefs.sort.label}"
        self._rebuild_rows()

    def _act_group(self) -> None:
        self._prefs.group_by = self._prefs.group_by.next()
        self._notice = f"Grouped by {self._prefs.group_by.value}"
        self._rebuild_rows()

    # ── refresh, pause, help, quit ───────────────────────────────────────────

    def _act_refresh(self) -> None:
        self._saved_loaded = False
        self._library_loaded = False
        self._request_load("manual", force=True)
        self._request_maintenance(full=True)
        if self._loop is not None:
            self._side_task = self._owned(asyncio.create_task(self._load_side()))
        self._notice = "Refresh requested"

    def _act_pause(self) -> None:
        self.paused = not self.paused
        if not self.paused:
            # A paused hub skipped registry changes; catch up now.
            self._request_load("unpaused", force=True)
        self._notice = "Live updates paused" if self.paused else "Live updates resumed"

    def _act_help(self) -> None:
        self.help_visible = not self.help_visible

    def _act_quit(self, key: Key) -> HubResult | None:
        if key is Key.ESC and self._search.active:
            # Esc clears the search first and restores the selection (AGX-06 §5.4).
            self._clear_search()
            return None
        if key is Key.ESC and self.marked_ids:
            self.marked_ids.clear()
            return None
        return HubResult("exit")

    # ── the detail view ──────────────────────────────────────────────────────

    def _scroll_detail(self, delta: int) -> None:
        """Scroll the detail view; scrolling stops following the transcript (``f``)."""
        self._follow = False
        self._detail_scroll = max(0, min(1_000_000, self._detail_scroll + delta))

    def _act_follow(self) -> None:
        self._follow = not self._follow

    def _act_full_transcript(self) -> None:
        self._full_transcript = not self._full_transcript
        self._detail_stale = True
        self._schedule_detail_read()

    def _act_open_request(self) -> None:
        self._follow = False
        self._detail_scroll = max(0, self._request_line)

    def _act_diff(self) -> None:
        session = self._sessions.get(self._detail_id or "")
        if session is None or not session.base_commit or self._loop is None:
            return
        self._owned(asyncio.create_task(self._load_diff(session)))

    async def _load_diff(self, session: BackgroundSession) -> None:
        def read() -> tuple[str, ...]:
            import subprocess

            try:
                output = subprocess.run(
                    ["git", "-C", session.cwd, "diff", session.base_commit],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                    check=True,
                ).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                return (f"git diff failed: {type(exc).__name__}",)
            lines = output.splitlines()
            if len(lines) > 20_000:
                lines = [*lines[:20_000], f"… {len(lines) - 20_000} more lines"]
            return tuple(lines) or ("No changes.",)

        value = await self._service.run_blocking(read)
        if isinstance(value, tuple):
            self._diff_lines = tuple(str(line) for line in value)
            self._diff_scroll = 0
            self._mark_dirty("diff_loaded")

    def _handle_pager_key(self, key: Key, ch: str) -> None:
        lines = self._diff_lines or ()
        page = max(1, self._detail_height - 2)
        if key in (Key.ESC, Key.ENTER) or (key is Key.CHAR and ch in ("q", "d")):
            self._diff_lines = None
        elif key is Key.UP:
            self._diff_scroll = max(0, self._diff_scroll - 1)
        elif key is Key.DOWN:
            self._diff_scroll = min(max(0, len(lines) - 1), self._diff_scroll + 1)
        elif key is Key.PAGE_UP:
            self._diff_scroll = max(0, self._diff_scroll - page)
        elif key is Key.PAGE_DOWN:
            self._diff_scroll = min(max(0, len(lines) - 1), self._diff_scroll + page)
        elif key is Key.HOME:
            self._diff_scroll = 0
        elif key is Key.END:
            self._diff_scroll = max(0, len(lines) - page)
        self._mark_dirty("pager")

    # ── prompts and choices ──────────────────────────────────────────────────

    def _handle_prompt(self, key: Key, ch: str) -> None:
        prompt = self._prompt
        if prompt is None:
            return
        result = prompt.editor.handle(key, ch)
        if prompt.kind == "search":
            if result is EditResult.CANCEL:
                self._prompt = None
                self._clear_search()
            elif result is EditResult.SUBMIT:
                self._prompt = None
            elif result is EditResult.CHANGED:
                self.set_search(prompt.editor.text)
            elif key in (Key.UP, Key.DOWN):
                # Results can be picked without leaving the search line.
                self._move(-1 if key is Key.UP else 1)
            elif key in (Key.PAGE_UP, Key.PAGE_DOWN):
                step = max(1, self._list_height - 1)
                self._move(-step if key is Key.PAGE_UP else step)
            return
        if result is EditResult.CANCEL:
            self._prompt = None
            if prompt.kind == "library":
                if prompt.target == "edit":
                    self._library_edit_entry = None
                else:
                    self._library_draft = None
        elif result is EditResult.SUBMIT:
            self._prompt = None
            self._submit_prompt(prompt)

    def _submit_prompt(self, prompt: _Prompt) -> None:
        text = prompt.editor.text
        target = prompt.target
        if prompt.kind == "rename":
            if not text.strip():
                self._notice = "Title unchanged: a title cannot be empty"
                return
            self._start_operation(
                target,
                "rename",
                functools.partial(self._service.rename_async, target, text.strip()),
            )
        elif prompt.kind == "labels":
            labels = tuple(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))
            self._start_operation(
                target, "labels", functools.partial(self._service.labels_async, target, labels)
            )
        elif prompt.kind == "answer" and prompt.item is not None:
            try:
                response = response_from_text(prompt.item, text)
            except ValueError as exc:
                self._notice = str(exc)[:240]
                prompt.label = "Answer"
                self._prompt = prompt
                return
            self._respond(prompt.item, response, "answer")
        elif prompt.kind == "feedback" and prompt.item is not None:
            options = prompt.item.target.get("mode_options")
            approval: dict[str, object] = {"allowed": True, "message": text}
            if isinstance(options, list) and options:
                approval["mode"] = str(options[0])
            self._respond(prompt.item, approval, "approve")
        elif prompt.kind == "dispatch":
            self._submit_dispatch(text)
        elif prompt.kind == "library":
            self._submit_library_prompt(prompt)

    def _handle_choice(self, key: Key, ch: str) -> HubResult | None:
        choice = self._choice
        if choice is None:
            return None
        count = len(choice.options)
        confirm = False
        if key is Key.ESC or (key is Key.CHAR and ch in ("n", "N") and choice.kind != "priority"):
            self._choice = None
            if choice.kind == "trust":
                self._pending_dispatch = None
                self._notice = "Not started: the project's extensions are not trusted"
            elif choice.kind == "library_scope":
                self._library_draft = None
            elif choice.kind == "library_save":
                self._library_pending = None
                self._library_view_lines = None
                self._library_view_title = ""
                self._notice = "Definition draft discarded."
            return None
        if key in (Key.LEFT, Key.UP, Key.SHIFT_TAB):
            choice.index = (choice.index - 1) % count
        elif key in (Key.RIGHT, Key.DOWN, Key.TAB):
            choice.index = (choice.index + 1) % count
        elif key is Key.CHAR and ch.isdigit() and 1 <= int(ch) <= count:
            choice.index = int(ch) - 1
            confirm = True
        elif key is Key.CHAR and ch in ("y", "Y") and choice.kind != "priority":
            choice.index = 0
            confirm = True
        elif key is Key.ENTER:
            confirm = True
        if not confirm:
            return None
        self._choice = None
        option = choice.options[choice.index]
        if choice.kind == "approve_mode" and choice.item is not None:
            response: dict[str, object] = {"allowed": True, "mode": option}
            if choice.item.target.get("workspace_access"):
                response["scope_grant"] = "target_once"
            self._respond(choice.item, response, "approve")
        elif choice.kind == "priority":
            self._start_operation(
                choice.target,
                "priority",
                functools.partial(self._service.priority_async, choice.target, option),
            )
        elif choice.kind == "trust":
            request, self._pending_dispatch = self._pending_dispatch, None
            if request is not None and choice.index == 0:
                self._start_operation(
                    str(request.cwd), "dispatch", functools.partial(self._dispatch, request, True)
                )
        elif choice.kind == "switch" and choice.result is not None and choice.index == 0:
            result = choice.result
            if result.action == "attach" and result.session_id is not None:
                self._start_attach(result.session_id)
                return None
            return result
        elif choice.kind == "library_scope":
            self._library_scope_chosen(choice)
        elif choice.kind == "library_save":
            if choice.index == 0:
                self._save_library_pending()
            else:
                self._library_pending = None
                self._library_view_lines = None
                self._library_view_title = ""
                self._notice = "Definition draft discarded."
        elif choice.kind == "library_delete":
            if choice.index == 0:
                self._delete_library_path(choice.target)
        return None

    # ── dispatch (AGX-05 §5.3) ───────────────────────────────────────────────

    def _act_dispatch(self) -> None:
        self._prompt = _Prompt("dispatch", "❯", LineEditor(multiline=True))

    def _dispatch_cwd(self) -> Path:
        """The selected agent's project, or the hub's directory."""
        session = self._row_session(self.selected_row())
        if session is not None and session.cwd and Path(session.cwd).is_dir():
            return Path(session.cwd)
        return self._cwd

    def _submit_dispatch(self, text: str) -> None:
        try:
            request, defaults = parse_dispatch(text, self._dispatch_cwd())
        except ValueError as exc:
            self._notice = str(exc)[:240]
            self._prompt = _Prompt("dispatch", "❯", LineEditor(text, multiline=True))
            return
        if request is None:
            # "/mode Plan" or "/model x" alone: the default for later dispatches.
            if defaults.mode_name is not None:
                self._dispatch_mode = defaults.mode_name
            if defaults.model:
                self._dispatch_model = defaults.model
            self._notice = "Next agents start with " + ", ".join(
                part
                for part in (
                    f"mode {self._dispatch_mode}" if self._dispatch_mode else "",
                    f"model {self._dispatch_model}" if self._dispatch_model else "",
                )
                if part
            )
            return
        self._start_operation(
            str(request.cwd), "dispatch", functools.partial(self._dispatch, request, False)
        )

    async def _dispatch(self, request: DispatchRequest, trusted: bool) -> ManagerOperationResult:
        """Start the agent unless the project has untrusted extensions to confirm."""
        if not trusted:
            untrusted = await self._service.run_blocking(_untrusted_extensions, request.cwd)
            if isinstance(untrusted, tuple) and untrusted:
                self._pending_dispatch = request
                self._choice = _Choice(
                    "trust",
                    f"{len(untrusted)} extension file(s) in {request.cwd} are not trusted "
                    "(see agenthicc trust list). Trust them and start the agent?",
                    ("Trust and start", "Cancel"),
                )
                self._mark_dirty("trust_choice")
                return ManagerOperationResult(
                    "", None, "pending", True, message="Waiting for trust"
                )
        else:
            await self._service.run_blocking(_trust_extensions, request.cwd)
        defaults = self._dispatch_defaults
        overrides = list(defaults.set_overrides)
        model = request.model or self._dispatch_model
        if model:
            overrides.append(f"execution.model={model}")
        return await self._service.submit_async(
            intent=request.intent,
            cwd=str(request.cwd),
            workflow_name=request.workflow_name,
            mode_name=request.mode_name or self._dispatch_mode or defaults.mode_name,
            config_path=defaults.config_path,
            set_overrides=tuple(overrides),
            set_secret_overrides=defaults.set_secret_overrides,
            dangerously_skip_permissions=defaults.dangerously_skip_permissions,
        )

    # ── worktree environment actions (PRD-229) ───────────────────────────────

    def _act_environment(self) -> None:
        session = self._sessions.get(self._detail_id or "")
        if session is None or session.role != "worker":
            self._notice = "Named environment actions are available only for worker agents"
            return
        try:
            from agenthicc.worktrees.environment_actions import (
                available_session_actions_with_context,
            )

            (
                self._environment_actions,
                self._environment_manifest_digest,
                self._environment_workspace_root,
            ) = available_session_actions_with_context(session)
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            self._notice = f"Unable to load worker actions: {type(exc).__name__}: {exc}"[:240]
            return
        if not self._environment_actions:
            self._notice = "No named actions are declared for this worker"
            return
        self._environment_action_mode = "select"
        self._environment_action_index = 0
        self._detail_scroll = 0

    def _handle_environment_key(self, key: Key, ch: str) -> None:
        if key is Key.ESC:
            if self._environment_action_mode == "preview":
                self._environment_action_mode = "select"
            else:
                self._environment_action_mode = ""
                self._environment_actions = ()
                self._environment_manifest_digest = ""
                self._environment_workspace_root = None
            self._detail_scroll = 0
            return
        up = key is Key.UP or (key is Key.CHAR and ch == "k")
        down = key is Key.DOWN or (key is Key.CHAR and ch == "j")
        if self._environment_action_mode == "preview":
            page = max(1, self._detail_height - 3)
            last = max(0, self._environment_rows - 1)
            if up:
                self._detail_scroll = max(0, self._detail_scroll - 1)
            elif down:
                self._detail_scroll = min(last, self._detail_scroll + 1)
            elif key is Key.PAGE_UP:
                self._detail_scroll = max(0, self._detail_scroll - page)
            elif key is Key.PAGE_DOWN:
                self._detail_scroll = min(last, self._detail_scroll + page)
            elif key is Key.HOME:
                self._detail_scroll = 0
            elif key is Key.END:
                self._detail_scroll = last
            elif key is Key.CHAR and ch in ("y", "Y"):
                self._run_environment_action()
            return
        if up and self._environment_actions:
            self._environment_action_index = max(0, self._environment_action_index - 1)
        elif down and self._environment_actions:
            self._environment_action_index = min(
                len(self._environment_actions) - 1, self._environment_action_index + 1
            )
        elif key is Key.ENTER and self._environment_actions:
            self._environment_action_mode = "preview"
            self._detail_scroll = 0

    def _run_environment_action(self) -> None:
        action = self._environment_actions[self._environment_action_index]
        session = self._sessions.get(self._detail_id or "")
        self._environment_action_mode = ""
        if session is None:
            self._notice = "Worker session is no longer available"
            return
        reviewed_digest = self._environment_manifest_digest
        session_id = session.session_id

        async def run_action() -> object:
            from agenthicc.worktrees.environment_actions import run_session_action

            cancel_event = threading.Event()
            self._environment_action_cancel_event = cancel_event
            command_task = asyncio.create_task(
                asyncio.to_thread(
                    run_session_action,
                    session_id,
                    action.command_id,
                    approved=True,
                    cancel_event=cancel_event,
                    expected_manifest_digest=reviewed_digest,
                    sessions=self.store,
                )
            )
            try:
                return await asyncio.shield(command_task)
            except asyncio.CancelledError:
                # Cancelling the asyncio wrapper cannot stop the thread running
                # the subprocess; signal its process group explicitly first.
                cancel_event.set()
                try:
                    await asyncio.shield(command_task)
                except Exception:  # noqa: BLE001 - preserve task cancellation
                    pass
                raise
            finally:
                if self._environment_action_cancel_event is cancel_event:
                    self._environment_action_cancel_event = None
                if self._environment_manifest_digest == reviewed_digest:
                    self._environment_manifest_digest = ""
                    self._environment_workspace_root = None

        self._start_operation(session_id, "environment-action", run_action)

    # ── the mouse (AGX-13 §5.4) ──────────────────────────────────────────────

    def _handle_mouse(self, report: str) -> HubResult | None:
        event = MouseEvent.decode(report)
        if event is None:
            return None
        if event.wheel:
            if self._detail_id is not None:
                self._scroll_detail(3 * event.wheel)
            else:
                self._move(3 * event.wheel)
            return None
        if not event.pressed or event.button != 0:
            return None
        row = event.row - 1
        column = event.column - 1
        geometry = self._geometry
        if row == 0:
            for start, end, tab in geometry.tabs:
                if start <= column < end:
                    self.switch_tab(tab)
                    return None
            return None
        if row == geometry.footer_line:
            for start, end, action in geometry.footer:
                if start <= column < end:
                    return self._run_action(action, Key.CHAR)
            return None
        index = geometry.rows.get(row)
        if index is None or self._detail_id is not None:
            return None
        now = time.monotonic()
        last_at, last_index = self._last_click
        self._last_click = (now, index)
        self._select_index(index)
        if last_index == index and now - last_at <= _DOUBLE_CLICK_S:
            return self._act_open()
        return None

    # ── drawing ──────────────────────────────────────────────────────────────

    def render(self, width: int, height: int) -> RenderableType:
        """The hub at *width* × *height* cells: never more lines than *height*."""
        from rich.console import Group

        started = time.monotonic()
        self._record_metric("frame_requested")
        lines = self._frame(max(20, width), max(4, height))
        self._dirty.clear()
        self._record_metric("frame_rendered", time.monotonic() - started)
        if self._last_key_at is not None:
            self._record_metric("key_to_frame", time.monotonic() - self._last_key_at)
            self._last_key_at = None
        return Group(*lines)

    def _frame(self, width: int, height: int) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.workspace.agents_hub.render import (
            footer_line,
            help_lines,
            tab_bar,
        )

        ui = self.ui
        plain = ui is not None and ui.screen_reader
        keymap = self.keymap
        geometry = _Geometry()
        lines: list[Text] = [
            tab_bar(
                self.tab,
                self._counts,
                width,
                paused=self.paused,
                plain=plain,
                help_hint=keymap.hint("agents.help"),
            )
        ]
        geometry.tabs = self._tab_spans(plain)
        if self.help_visible:
            title = {
                "agents.list": "Agents",
                "agents.peek": "Peek",
                "agents.detail": "Agent",
            }[self.context]
            lines += help_lines(title, keymap.help(self.context), width)
            self._geometry = geometry
            return lines[:height]
        bottom = self._bottom_lines(width, plain)
        hints = [
            hint
            for hint in keymap.hints(self.context, self.conditions())
            if hint.action not in ("agents.help", "agents.quit")
        ]
        tail = [hint for name in ("agents.help", "agents.quit") if (hint := keymap.hint(name))]
        footer = footer_line(hints, width, tail=tail, plain=plain)
        body_height = max(1, height - len(lines) - len(bottom) - 1)
        if self._diff_lines is not None:
            body = self._pager_lines(width, body_height)
        elif self._library_view_lines is not None:
            body = self._library_view_body(width, body_height)
        elif (
            self._prompt is not None
            and self._prompt.kind == "library"
            and self._prompt.target in ("edit", "create_prompt")
        ):
            body = self._library_editor_body(width, body_height)
        elif self._detail_id is not None and width >= 140 and not plain:
            list_width = max(40, width * 9 // 20)
            left = self._list_lines(list_width, body_height, plain, geometry, offset=len(lines))
            right = self._detail_body(width - list_width - 1, body_height, plain)
            body = []
            for index in range(body_height):
                line = Text(no_wrap=True, overflow="crop")
                left_line = left[index] if index < len(left) else Text("")
                left_line.truncate(list_width, overflow="crop", pad=True)
                line.append_text(left_line)
                line.append("│", style="dim")
                if index < len(right):
                    line.append_text(right[index])
                body.append(line)
        elif self._detail_id is not None:
            body = self._detail_body(width, body_height, plain)
        else:
            body = self._list_lines(width, body_height, plain, geometry, offset=len(lines))
        lines += body
        lines += [Text("")] * max(0, body_height - len(body))
        lines += bottom
        geometry.footer_line = len(lines)
        geometry.footer = self._footer_spans([*_fit_hints(hints, tail, width, plain), *tail], plain)
        lines.append(footer)
        self._geometry = geometry
        return lines[:height]

    def _library_view_body(self, width: int, height: int) -> list[Text]:
        from rich.text import Text

        output = [Text(self._library_view_title, style="bold cyan")]
        for raw in self._library_view_lines or []:
            wrapped = Text(raw).wrap(self.console, max(1, width - 2), overflow="fold")
            output.extend(wrapped or [Text("")])
        start = min(self._library_view_scroll, max(0, len(output) - height))
        return output[start : start + height]

    def _library_editor_body(self, width: int, height: int) -> list[Text]:
        from rich.text import Text

        prompt = self._prompt
        if prompt is None:
            return []
        value = prompt.editor.text
        content = value.split("\n")
        cursor_line = value.count("\n", 0, prompt.editor.cursor)
        cursor_start = value.rfind("\n", 0, prompt.editor.cursor) + 1
        cursor_column = prompt.editor.cursor - cursor_start
        start = self._library_editor_scroll
        visible = max(1, height - 1)
        if cursor_line < start:
            start = cursor_line
        elif cursor_line >= start + visible:
            start = cursor_line - visible + 1
        self._library_editor_scroll = max(0, start)
        output: list[Text] = [Text(prompt.label, style="bold cyan")]
        for row, raw in enumerate(content[start : start + visible], start=start):
            line = Text(no_wrap=True, overflow="crop")
            if row == cursor_line:
                horizontal = max(0, cursor_column - width + 1)
                shown = raw[horizontal : horizontal + width]
                column = min(len(shown), cursor_column - horizontal)
                line.append(shown[:column])
                line.append(shown[column : column + 1] or " ", style="reverse")
                line.append(shown[column + 1 :])
            else:
                line.append(raw)
            line.truncate(width, overflow="crop")
            output.append(line)
        return output

    def _tab_spans(self, plain: bool) -> list[tuple[int, int, Tab]]:
        from agenthicc.tui.workspace.agents_hub.render import tab_label

        spans: list[tuple[int, int, Tab]] = []
        column = len(" Agents   ")
        separator = 2 if plain else 3
        for index, tab in enumerate(_TABS):
            if index:
                column += separator
            label = tab_label(tab, self._counts)
            if plain and tab is self.tab:
                label = f"[{label}]"
            spans.append((column, column + len(label), tab))
            column += len(label)
        return spans

    @staticmethod
    def _footer_spans(hints: list[Hint], plain: bool) -> list[tuple[int, int, str]]:
        spans: list[tuple[int, int, str]] = []
        column = 1
        separator = 2
        for index, hint in enumerate(hints):
            if index:
                column += separator
            size = len(hint.keys) + 1 + len(hint.label)
            spans.append((column, column + size, hint.action))
            column += size
        return spans

    def _bottom_lines(self, width: int, plain: bool) -> list[Text]:
        """Choice or prompt, then the notice: the lines above the footer."""
        from rich.text import Text

        from agenthicc.tui.workspace.agents_hub.render import fit

        lines: list[Text] = []
        choice = self._choice
        prompt = self._prompt
        if choice is not None:
            line = Text(no_wrap=True, overflow="crop")
            if choice.label:
                line.append(f" {choice.label} ", style="bold")
            for index, option in enumerate(choice.options):
                selected = index == choice.index
                shown = f"[{option}]" if selected else f" {option} "
                line.append(shown, style="reverse bold" if selected and not plain else "")
                line.append(" ")
            line.append("  ←→ choose · Enter confirm · Esc cancel", style="dim")
            line.truncate(width, overflow="crop")
            lines.append(line)
        elif prompt is not None and prompt.kind != "search":
            if prompt.kind == "library" and prompt.target in ("edit", "create_prompt"):
                lines.append(
                    Text(" Ctrl+J newline · ↑↓ move · Enter save · Esc cancel", style="dim")
                )
            else:
                hint = {
                    "dispatch": "Enter start · Ctrl+J newline · @dir /workflow /mode /model · Esc cancel",
                    "answer": "Enter send · Ctrl+J newline · Esc cancel",
                    "feedback": "Enter approve · Ctrl+J newline · Esc cancel",
                }.get(prompt.kind, "Enter save · Esc cancel")
                label = f" {prompt.label} "
                room = max(8, width - len(label) - len(hint) - 4)
                shown, cursor = prompt.editor.view(room)
                line = Text(no_wrap=True, overflow="crop")
                line.append(label, style="bold cyan")
                line.append(shown[:cursor])
                line.append(shown[cursor : cursor + 1] or " ", style="reverse")
                line.append(shown[cursor + 1 :])
                line.append("  " + hint, style="dim")
                line.truncate(width, overflow="crop")
                lines.append(line)
        notice = self._status_notice()
        if notice:
            lines.append(Text(fit(" " + notice, width), style="yellow"))
        return lines

    def _status_notice(self) -> str:
        if self._deleting_ids:
            return f"Deleting {len(self._deleting_ids)} agent(s): {self._deletion_phase or 'starting'}…"
        if self._deletion_error:
            return f"Delete failed: {self._deletion_error[:200]}"
        if self._operation_state:
            labels = sorted({label for _target, label, _phase in self._operation_state.values()})
            return self._notice or f"Working ({', '.join(labels)})…"
        return self._notice

    # ── the list ─────────────────────────────────────────────────────────────

    def _list_lines(
        self, width: int, height: int, plain: bool, geometry: _Geometry, *, offset: int
    ) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.workspace.agents_hub.layout import layout
        from agenthicc.tui.workspace.agents_hub.render import (
            RowContext,
            fit,
            row_line,
            row_sentence,
        )

        lines: list[Text] = []
        prompt = self._prompt
        if (prompt is not None and prompt.kind == "search") or self._search.active:
            editing = prompt is not None and prompt.kind == "search"
            text = prompt.editor.text if editing and prompt is not None else self._search.text
            line = Text(no_wrap=True, overflow="crop")
            line.append(" / ", style="bold cyan")
            line.append(text)
            if editing:
                line.append(" ", style="reverse")
            matches = sum(1 for row in self._rows if row.selectable)
            line.append(f"   {matches} match{'es' if matches != 1 else ''}", style="dim")
            if self._search.errors:
                line.append("   " + "; ".join(self._search.errors), style="red")
            line.truncate(width, overflow="crop")
            lines.append(line)
        status = self._loading_line()
        if status:
            lines.append(Text(fit(" " + status, width), style="yellow"))
        if not self._rows:
            lines.append(Text(fit(" " + self._empty_message(), width), style="dim"))
            return lines[:height]
        if (
            self.tab is Tab.RUNNING
            and self._loaded
            and not self._running
            and not self._search.active
        ):
            lines.append(Text(fit(" " + self._empty_message(), width), style="dim"))
        context = RowContext(
            now=self._clock(),
            queue_positions=self._queue_positions,
            sessions=self._sessions,
            words=self._search.words,
            plain=plain,
        )
        present = {"status", "title", "age"}
        if any(row.session is not None for row in self._rows):
            present.update(("activity", "project", "agent"))
            if any(row.session is not None and row.session.cost_usd for row in self._rows):
                present.add("cost")
        elif self._rows:
            present.update(("activity", "project", "agent"))
        columns = layout(width, present=frozenset(present))
        available = max(1, height - len(lines))
        selected_index = self._row_index.get(self._selected_key or "", 0)
        peek: list[Text] = []
        if self._peek and self._detail_id is None:
            peek = self._peek_lines(width)
        # Keep the selected row, and its peek panel, inside the viewport.
        room = max(1, available - len(peek))
        self._list_height = room
        if selected_index < self._top:
            self._top = selected_index
        elif selected_index >= self._top + room:
            self._top = selected_index - room + 1
        self._top = max(0, min(self._top, max(0, len(self._rows) - room)))
        index = self._top
        while index < len(self._rows) and len(lines) < height:
            row = self._rows[index]
            selected = row.key == self._selected_key
            session = row.session
            if plain:
                rendered = row_sentence(row, context, selected=selected)
            else:
                rendered = row_line(
                    row,
                    columns,
                    context,
                    selected=selected,
                    marked=session is not None and session.session_id in self.marked_ids,
                    unseen=session is not None and self._prefs.unseen(session),
                )
            geometry.rows[offset + len(lines)] = index
            lines.append(rendered)
            if selected and peek:
                lines.extend(peek[: max(0, height - len(lines))])
            index += 1
        return lines

    def _peek_lines(self, width: int) -> list[Text]:
        from agenthicc.tui.workspace.agents_hub.render import peek_lines

        _row, session, item = self._target()
        if session is None:
            return []
        hints = [
            hint
            for hint in self.keymap.hints("agents.peek", self.conditions())
            if hint.action
            in ("agents.approve", "agents.reject", "agents.input", "agents.open", "agents.resume")
        ]
        lines = peek_lines(
            session,
            self._details.get(session.session_id),
            item,
            width,
            now=self._clock(),
            hints=hints,
        )
        if self._composer_open:
            for renderable in self._composer_lines(width):
                lines.extend(_text_lines(self.console, renderable, width))
        return lines

    def _loading_line(self) -> str:
        if self._loaded:
            return ""
        if self._load_error:
            return f"Agents failed to load: {self._load_error} — press Ctrl+R to retry"
        progress = self.store.rebuild_progress()
        if progress is not None:
            done, size = progress
            percent = int(100 * done / size) if size else 0
            label = f"Indexing the agent registry {percent}% ({size / 1_048_576:.0f} MB)"
            return label + (" · recent agents so far" if self._provisional else "")
        elapsed = max(0.0, time.monotonic() - (self._load_started_at or time.monotonic()))
        return f"Loading agents… {elapsed:.0f}s"

    def _empty_message(self) -> str:
        if self._search.active:
            return f"No agents match `{self._search.text}`. Esc clears the search."
        if not self._loaded:
            return "Loading agents…"
        return {
            Tab.RUNNING: (
                'No agents yet. Start one with `agenthicc --goal "…" --detach`, '
                "or /background in a session."
                if not self._running
                else "Nothing is running."
            ),
            Tab.NEEDS_YOU: "Nothing needs you. Agents waiting for your answer appear here.",
            Tab.HISTORY: "No finished agents or saved sessions yet.",
            Tab.LIBRARY: "No agent definitions found.",
        }[self.tab]

    # ── the detail view ──────────────────────────────────────────────────────

    def _detail_body(self, width: int, height: int, plain: bool) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.workspace.agents_hub.render import detail_lines, fit

        self._detail_height = height
        if self._environment_action_mode:
            return self._environment_lines(width, height)
        session = self._sessions.get(self._detail_id or "")
        if session is None:
            return [
                Text(fit(f" Agent {self._detail_id}", width), style="bold"),
                Text(fit(" This agent is no longer in the registry.", width), style="dim"),
            ]
        _row, _session, item = self._target()
        composer: list[Text] = []
        if self._composer_open:
            for renderable in self._composer_lines(width):
                composer.extend(_text_lines(self.console, renderable, width))
        view = detail_lines(
            session,
            self._details.get(session.session_id),
            item,
            width,
            now=self._clock(),
            queue_position=self._queue_positions.get(session.session_id),
            follow=self._follow,
            full_transcript=self._full_transcript,
            plain=plain,
        )
        # The title and the Needs you card stay in view; the rest scrolls.
        pinned = view.lines[: min(view.pinned, max(1, height // 2))]
        body = view.lines[len(pinned) :]
        self._request_line = max(0, view.request_line - len(pinned))
        room = max(1, height - len(pinned) - len(composer))
        maximum = max(0, len(body) - room)
        if self._follow:
            # Following keeps the newest transcript event in view.
            self._detail_scroll = max(0, view.transcript_end - len(pinned) - room + 1)
        self._detail_scroll = min(self._detail_scroll, maximum)
        visible = body[self._detail_scroll : self._detail_scroll + room]
        return [*pinned, *visible, *composer]

    def _environment_lines(self, width: int, height: int) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.workspace.agents_hub.render import fit
        from agenthicc.worktrees.environment import resolve_worktree_path

        lines = [Text(fit(" Worker environment action", width), style="bold cyan")]
        if self._environment_action_mode == "preview" and self._environment_actions:
            action = self._environment_actions[self._environment_action_index]
            root = self._environment_workspace_root
            cwd = (
                resolve_worktree_path(root, action.cwd, label=f"{action.command_id}.cwd")
                if root is not None
                else Path(action.cwd)
            )
            rows = [
                ("Action", action.command_id),
                ("Command", action.command),
                ("Workspace", str(cwd)),
                ("Network", action.network),
                ("Timeout", f"{action.timeout_s:g}s"),
                ("Approval", "Press y to approve this exact run; Esc cancels"),
            ]
            # Every character of the command is shown before it is approved:
            # long values wrap, and the preview scrolls.
            wrapped: list[Text] = []
            room = max(8, width - 15)
            for label, value in rows:
                text = " ".join(value.split())
                parts = [text[start : start + room] for start in range(0, len(text), room)] or [""]
                for index, part in enumerate(parts):
                    wrapped.append(Text(f"   {label if index == 0 else '':<10} {part}"))
            self._environment_rows = len(wrapped)
            lines.extend(wrapped[self._detail_scroll :])
        else:
            lines.append(
                Text(fit(" Select an action and press Enter to review it", width), style="dim")
            )
            for index, action in enumerate(self._environment_actions):
                marker = "▸" if index == self._environment_action_index else " "
                lines.append(Text(fit(f" {marker} {action.command_id}   {action.command}", width)))
        return lines[:height]

    def _pager_lines(self, width: int, height: int) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.workspace.agents_hub.render import fit

        source = self._diff_lines or ()
        self._detail_height = height
        lines = [Text(fit(" Diff (Esc closes)", width), style="bold cyan")]
        for raw in source[self._diff_scroll : self._diff_scroll + height - 1]:
            style = "green" if raw.startswith("+") else "red" if raw.startswith("-") else ""
            if raw.startswith(("+++", "---", "@@", "diff ")):
                style = "cyan"
            lines.append(Text(fit(raw, width), style=style))
        return lines

    def listing(self) -> list[str]:
        """The current tab as plain lines (ID, status, title, project, age)."""
        from agenthicc.tui.workspace.agents_hub.render import RowContext, listing_line, tab_label

        context = RowContext(
            now=self._clock(), queue_positions=self._queue_positions, sessions=self._sessions
        )
        counts = ", ".join(tab_label(tab, self._counts) for tab in _TABS)
        lines = [f"Agents — {counts}"]
        if not any(row.selectable for row in self._rows):
            lines.append(self._empty_message())
        lines.extend(listing_line(row, context) for row in self._rows)
        return lines

    # ── full screen ──────────────────────────────────────────────────────────

    def _draw(self, live: LiveRegion) -> None:
        width, height = self.console.size
        renderable = self.render(width, height)
        options = self.console.options.update(width=width, height=height)
        live.update(
            [tuple(line) for line in self.console.render_lines(renderable, options, pad=False)]
        )

    async def snapshot(self) -> None:
        """Load once without watching, for a non-interactive listing."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        try:
            self._load_generation += 1
            self._side_started = True
            await self._load(self._load_generation)
            await self._load_side()
        finally:
            self._loop = None

    async def run_terminal_handoff(self, operation: Callable[[], Awaitable[None]]) -> None:
        """Run *operation* with cooked terminal input, then resume the hub."""
        backend = self._backend
        if backend is None:
            raise RuntimeError("the interactive agents hub is not running")
        if self._terminal_handoff is not None:
            raise RuntimeError("another terminal handoff is already pending")
        completed = asyncio.get_running_loop().create_future()
        handoff = (operation, completed)
        self._terminal_handoff = handoff
        backend.restore()
        try:
            await completed
        except asyncio.CancelledError:
            if self._terminal_handoff is handoff:
                self._terminal_handoff = None
                backend.restore()
            raise

    async def run(self) -> HubResult:
        """Run full screen until the user leaves or hands off to a session."""
        from agenthicc.tui.terminal.backend import get_backend
        from agenthicc.tui.terminal.capabilities import capabilities
        from agenthicc.tui.workspace.live_region import LiveRegion

        backend = get_backend()
        if not backend.is_interactive():
            # Without a terminal the hub prints its rows once, as a listing.
            try:
                await self.snapshot()
                for line in self.listing():
                    self.console.print(line, markup=False, highlight=False, soft_wrap=True)
            finally:
                self._prefs.save()
                if self._owns_service:
                    await self._service.close()
            return HubResult("exit")
        await self.start()
        self._backend = backend
        ui = self.ui
        mouse = ui.mouse_enabled(fullscreen=True) if ui is not None else True
        try:
            while True:
                live = LiveRegion(
                    self.console,
                    transient=True,
                    synchronized_output=lambda: capabilities.synchronized_output,
                )
                read_finished = threading.Event()
                read_future: asyncio.Future[tuple[Key, str]] | None = None
                handoff: tuple[Callable[[], Awaitable[None]], asyncio.Future[None]] | None = None
                self.console.set_alt_screen(True)
                live.start()
                try:
                    with backend.enter_raw_mode():
                        if mouse:
                            backend.set_reporting(mouse=True)
                        loop = asyncio.get_running_loop()

                        def blocking_read_key(
                            finished: threading.Event = read_finished,
                        ) -> tuple[Key, str]:
                            try:
                                return backend.read_key()
                            finally:
                                finished.set()

                        read_finished.clear()
                        read_future = loop.run_in_executor(None, blocking_read_key)
                        self._draw(live)
                        self._record_metric("first_frame")
                        while True:
                            done = await self.step(frozenset({read_future}))
                            if read_future in done:
                                try:
                                    key, ch = read_future.result()
                                except OSError:
                                    if self._terminal_handoff is None:
                                        raise
                                    handoff, self._terminal_handoff = self._terminal_handoff, None
                                    break
                                result = self.handle_key(key, ch)
                                if result is not None:
                                    return result
                                read_finished.clear()
                                read_future = loop.run_in_executor(None, blocking_read_key)
                            attached = self.attach_result
                            if attached is not None:
                                return attached
                            if self._dirty:
                                if self.settings.frame_debounce_ms and read_future.done() is False:
                                    await asyncio.sleep(self.settings.frame_debounce_ms / 1_000.0)
                                self._draw(live)
                finally:
                    live.stop()
                    self.console.set_alt_screen(False)
                    # A POSIX read() is not cancelled with its Future: restoring the
                    # terminal first lets the reader thread return.
                    try:
                        backend.restore()
                    except Exception:  # noqa: BLE001
                        pass
                    pending = read_future is not None and not read_future.done()
                    if read_future is not None and pending:
                        read_future.cancel()
                        await asyncio.to_thread(read_finished.wait, 0.25)
                    if read_future is None or read_finished.is_set() or not pending:
                        backend.close()
                if handoff is None:
                    return HubResult("exit")
                operation, completed = handoff
                try:
                    await operation()
                except asyncio.CancelledError:
                    if not completed.done():
                        completed.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001 - report failures to the caller
                    if not completed.done():
                        completed.set_exception(exc)
                else:
                    if not completed.done():
                        completed.set_result(None)
        finally:
            self._backend = None
            pending_handoff, self._terminal_handoff = self._terminal_handoff, None
            if pending_handoff is not None and not pending_handoff[1].done():
                pending_handoff[1].set_exception(
                    RuntimeError("the agents hub stopped before terminal handoff")
                )
            backend.close()
            await self.stop()


_DISPATCH_TOKEN = re.compile(r"(?<!\S)(?:@(\S+)|/(workflow|model|mode)(?=\s|$)(?:[ \t]+(\S+))?)")


def parse_dispatch(text: str, cwd: Path) -> tuple[DispatchRequest | None, DispatchRequest]:
    """Parse a dispatch line: ``@dir``, ``/workflow NAME``, ``/mode NAME``, ``/model ID``.

    Returns the request (``None`` when the line only sets ``/mode`` or
    ``/model`` for later dispatches) and the defaults the line names. The
    rest of the line, line breaks included, is the agent's intent. Raises
    :class:`ValueError` for a missing directory or value, or an unknown
    ``/command`` at the start of the line.
    """
    directory = cwd
    workflow = ""
    mode: str | None = None
    model = ""
    for match in _DISPATCH_TOKEN.finditer(text):
        path, name, value = match.group(1), match.group(2), match.group(3)
        if path is not None:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = cwd / candidate
            if not candidate.is_dir():
                raise ValueError(f"No such directory: {path}")
            directory = candidate.resolve()
            continue
        if not value:
            raise ValueError(f"/{name} needs a name")
        if name == "workflow":
            workflow = value
        elif name == "mode":
            mode = value
        else:
            model = value
    remainder = _DISPATCH_TOKEN.sub("", text)
    intent = "\n".join(" ".join(line.split()) for line in remainder.splitlines()).strip()
    first = intent.split(maxsplit=1)[0] if intent else ""
    if first.startswith("/"):
        raise ValueError(f"Unknown {first}: use @directory, /workflow, /mode or /model")
    defaults = DispatchRequest(intent="", cwd=directory, mode_name=mode, model=model)
    if not intent:
        if mode is None and not model:
            raise ValueError("Describe what the agent should do")
        return None, defaults
    return (
        DispatchRequest(
            intent=intent, cwd=directory, workflow_name=workflow, mode_name=mode, model=model
        ),
        defaults,
    )


def _untrusted_extensions(project: Path) -> tuple[str, ...]:
    """Project extension files that are not trusted (``agenthicc trust list``)."""
    from agenthicc.cli.commands.trust import project_extension_files
    from agenthicc.plugins.trust import current_extension_trust

    trust = current_extension_trust()
    return tuple(
        str(path)
        for path, kind in project_extension_files(project)
        if trust.check(path, kind=kind) is not None
    )


def _trust_extensions(project: Path) -> None:
    """Trust the project's extension files, as ``agenthicc trust add`` does."""
    from agenthicc.cli.commands.trust import project_extension_files
    from agenthicc.plugins.trust import current_extension_trust

    trust = current_extension_trust()
    for path, kind in project_extension_files(project):
        if path.is_file() and trust.check(path, kind=kind) is not None:
            trust.trust(path, kind=kind)


def _text_lines(console: Console, renderable: RenderableType, width: int) -> list[Text]:
    """A renderable as plain ``Text`` lines at *width* (styles kept)."""
    from rich.text import Text

    options = console.options.update(width=width)
    lines: list[Text] = []
    for segments in console.render_lines(renderable, options, pad=False):
        line = Text(no_wrap=True, overflow="crop")
        for segment in segments:
            if not segment.control:
                line.append(segment.text, style=segment.style)
        lines.append(line)
    return lines


def _fit_hints(hints: list[Hint], tail: list[Hint], width: int, plain: bool) -> list[Hint]:
    """The hints :func:`footer_line` keeps at *width* (for click positions)."""
    from rich.cells import cell_len

    separator = ", " if plain else "  "
    chosen = list(hints)

    def size(entries: list[Hint]) -> int:
        return cell_len(" " + separator.join(f"{hint.keys} {hint.label}" for hint in entries))

    while chosen and size([*chosen, *tail]) > width:
        chosen.pop()
    return chosen


async def run_agents_hub(
    console: Console,
    *,
    store: BackgroundStore,
    supervisor: BackgroundSupervisor,
    settings: AgentsHubSettings | None = None,
    sources: HubSources | None = None,
    tab: Tab = Tab.RUNNING,
    open_session: str | None = None,
    select: str | None = None,
    run_id: str = "",
    dispatch: DispatchDefaults | None = None,
    ui: UISettings | None = None,
    generate_definition: Callable[[str, str], Awaitable[str]] | None = None,
) -> HubResult:
    """Open the hub full screen (``agenthicc agents``) with the user's key bindings.

    The bindings file is watched while the hub is open, as in a session.
    """
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.keymap_service import KeymapService

    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    keymap_service = KeymapService(state)
    external_editor: Callable[[Path, DefinitionScope], Awaitable[None]] | None = None
    from agenthicc.agents.external_editor import editor_is_configured, open_definition_in_editor

    if editor_is_configured():

        async def edit_definition(path: Path, scope: DefinitionScope) -> None:
            async def run_editor() -> None:
                await asyncio.to_thread(
                    open_definition_in_editor,
                    path,
                    scope=scope,
                    project_root=Path.cwd(),
                )

            await hub.run_terminal_handoff(run_editor)

        external_editor = edit_definition

    hub = AgentsHub(
        console,
        store=store,
        supervisor=supervisor,
        settings=settings,
        sources=sources,
        app_state=state,
        tab=tab,
        open_session=open_session,
        select=select,
        run_id=run_id,
        dispatch=dispatch,
        generate_definition=generate_definition,
        external_editor=external_editor,
    )
    watch = asyncio.create_task(keymap_service.watch(), name="agenthicc-hub-keybindings")
    try:
        return await hub.run()
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
