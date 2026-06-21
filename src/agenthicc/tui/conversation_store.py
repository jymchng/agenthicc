"""ConversationStore — reactive single source of truth for the TUI runtime.

This is separate from `agenthicc.conversation_store` (the SQLite-backed
project memory).  This store lives for the application lifetime and drives
the Rich rendering pipeline through Signal subscriptions.

Architecture: PRD-58 §6, PRD-59 §3.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable, Literal

from agenthicc.reactive import Signal, Computed

if TYPE_CHECKING:
    import asyncio as _asyncio
    from agenthicc.tools.approval import ApprovalRequest
    from agenthicc.workflows.plugin import WorkflowRun
    from agenthicc.subagents.pool import SubagentPoolState


# ── Agent state ───────────────────────────────────────────────────────────────

class AgentState(Enum):
    IDLE       = auto()
    THINKING   = auto()
    RUNNING    = auto()    # tool executing
    RECOVERING = auto()    # tool failed; LLM deciding how to respond
    COMPLETE   = auto()
    ERROR      = auto()


# ── Conversation events ───────────────────────────────────────────────────────

EventKind = Literal[
    "turn_start",
    "tool_complete",
    "text",
    "thinking_step",
    "file_modified",
    "error",
    "mention_chips",
    "user_message",
    "tokens",
    # Subagent pool events (PRD-124 Phase 3)
    "subagent_pool_started",
    "subagent_worker_done",
    "subagent_pool_done",
    # Cached pool result for resume detection (PRD-124 Phase 4)
    "subagent_pool_result",
    # Generic text line from internal systems (compactor, subagents, etc.)
    "system",
]


@dataclass
class ConversationEvent:
    event_id:  str
    kind:      str               # EventKind
    payload:   dict[str, object]
    timestamp: float = field(default_factory=time.time)
    rendered:  bool  = False     # True once ScrollBufferAppender has printed it


@dataclass
class ConversationTurn:
    turn_id:    str
    agent_name: str
    timestamp:  float = field(default_factory=time.time)
    events:     list[ConversationEvent] = field(default_factory=list)
    state:      AgentState = AgentState.THINKING


# ── Notification entry ────────────────────────────────────────────────────────

@dataclass
class _NotificationEntry:
    """One stacked transient notification line with its auto-dismiss timer."""

    text:   str
    handle: _asyncio.TimerHandle | None = field(default=None)


# ── Store ─────────────────────────────────────────────────────────────────────

class ConversationStore:
    """Reactive store for the full conversation history and agent state.

    All UI components derive their rendered output from this store's signals.
    No component holds authoritative state of its own.
    """

    def __init__(self) -> None:
        # ── core signals ──────────────────────────────────────────────────────
        self.turns:            Signal[list[ConversationTurn]] = Signal([])
        self.agent_state:      Signal[AgentState]             = Signal(AgentState.IDLE)
        self.active_tool:      Signal[str]                    = Signal("")
        self.frame:            Signal[int]                    = Signal(0)
        """Universal animation counter — increments every 50 ms unconditionally (PRD-120).
        All animated elements (flower, thinking, compact spinner) derive their frame index
        from ``frame() % N``.  The workspace subscribes once for all animation redraws."""
        self.tokens_in:        Signal[int]                    = Signal(0)
        self.tokens_out:       Signal[int]                    = Signal(0)
        self.cost_usd:         Signal[float]                  = Signal(0.0)
        self.session_id:       Signal[str]                    = Signal("")
        self.model_name:       Signal[str]      = Signal("")
        self.notification:        Signal[str | None] = Signal(None)
        self.workflow_override:   Signal[str | None] = Signal(None)
        """Name of the /workflow-selected override (PRD-114).  None = mode default."""
        self.compaction_active:   Signal[bool]       = Signal(False)
        """True while a compaction LLM call is in flight (PRD-119)."""
        self.subagent_pool_state: Signal[SubagentPoolState | None] = Signal(None)
        """Live state of the active SubagentPool — None when no pool is running (PRD-124)."""
        # Internal: per-line notification stack.
        # notify_transient() appends; each dismiss closure removes only its own
        # entry by identity, leaving other lines untouched.
        self._notification_lines: list[_NotificationEntry] = []
        # True while _sync_notification_signal() is writing to self.notification.
        # Prevents the notification.subscribe callback from treating our own
        # write as an "external clear".
        self._notification_syncing: bool = False
        # Counts consecutive tool_complete events in the current group.
        # Resets to 0 when a text event or turn_start arrives.
        # Drives the collapsed "…and N more" summary in the scroll buffer.
        self.tool_group_count: Signal[int]   = Signal(0)
        # Number of tool calls currently hidden above the scroll-buffer threshold.
        # Set live by ScrollBufferAppender as each overflow call arrives;
        # reset to 0 when the group closes (text/error event).
        # FooterComponent renders this as a live "⎿ …and N more" footer row.
        self.live_tool_overflow: Signal[int] = Signal(0)

        # ── computed values ───────────────────────────────────────────────────
        self.is_running: Computed[bool] = Computed(
            lambda: self.agent_state() not in (
                AgentState.IDLE, AgentState.COMPLETE, AgentState.ERROR
            ),
            self.agent_state,
        )
        self.turn_count: Computed[int] = Computed(
            lambda: len(self.turns()),
            self.turns,
        )
        self.total_tokens: Computed[int] = Computed(
            lambda: self.tokens_in() + self.tokens_out(),
            self.tokens_in, self.tokens_out,
        )

        # ── animation (driven by tick) ────────────────────────────────────────
        self._start_time: float = 0.0

        # ── internal ──────────────────────────────────────────────────────────
        self._current_turn: ConversationTurn | None = None
        self._event_subscribers: list[Callable[[ConversationEvent], None]] = []
        # Subscribe to detect external notification.set(None) and cancel stacked timers.
        self.notification.subscribe(lambda _: self._on_notification_externally_cleared())

    # ── tick ──────────────────────────────────────────────────────────────────

    @property
    def elapsed_s(self) -> float:
        """Seconds since the current turn started, or 0.0 when idle."""
        return time.monotonic() - self._start_time if self._start_time else 0.0

    def tick(self) -> None:
        """Advance the universal frame counter. Called every ~50 ms unconditionally.

        All animated UI elements derive their frame index from ``frame() % N``
        (PRD-120).  No per-feature branches needed here.
        """
        self.frame.set(self.frame() + 1)

    # ── turn lifecycle ────────────────────────────────────────────────────────

    def notify_transient(self, message: str, duration: float = 2.0) -> None:
        """Append a transient notification line that auto-dismisses after *duration* seconds.

        Multiple calls while notifications are still visible stack vertically —
        each new message appears on its own line below any existing ones.  Each
        line carries its own independent timer and is removed when its timer
        fires, leaving the other lines untouched.

        A direct ``notification.set(None)`` call from anywhere cancels all
        stacked lines and their timers immediately (via the subscription set up
        in ``__init__``).

        Safe to call with no running event loop (headless / tests): the message
        is set persistently with no dismiss scheduled.
        """
        import asyncio  # noqa: PLC0415

        entry = _NotificationEntry(text=message)

        def _dismiss() -> None:
            # Remove only this entry by identity; leave other lines alone.
            self._notification_lines = [
                e for e in self._notification_lines if e is not entry
            ]
            self._sync_notification_signal()

        self._notification_lines.append(entry)
        self._sync_notification_signal()

        try:
            loop = asyncio.get_running_loop()
            entry.handle = loop.call_later(duration, _dismiss)
        except RuntimeError:
            pass  # no running event loop — stays persistent (tests/headless)

    def _sync_notification_signal(self) -> None:
        """Recompute the notification signal from the current line stack."""
        lines = [e.text for e in self._notification_lines]
        self._notification_syncing = True
        try:
            self.notification.set("\n".join(lines) if lines else None)
        finally:
            self._notification_syncing = False

    def _on_notification_externally_cleared(self) -> None:
        """Cancel all stacked transient lines when notification is set to None externally."""
        if self._notification_syncing:
            return  # our own write — ignore
        if self.notification() is not None:
            return  # only care about set(None)
        # Cancel all pending dismiss timers and drop the stack.
        for entry in self._notification_lines:
            if entry.handle is not None:
                try:
                    entry.handle.cancel()
                except Exception:  # noqa: BLE001
                    pass
        self._notification_lines.clear()

    def begin_turn(self, agent_name: str, turn_id: str | None = None) -> ConversationTurn:
        tid = turn_id or str(uuid.uuid4())
        turn = ConversationTurn(turn_id=tid, agent_name=agent_name)
        self._current_turn = turn
        self.turns.set(self.turns.get() + [turn])
        self._start_time = time.monotonic()
        self.agent_state.set(AgentState.THINKING)
        return turn

    @property
    def is_turn_active(self) -> bool:
        """True when a turn is currently open (between begin_turn and close_turn)."""
        return self._current_turn is not None

    def close_turn(self, *, error: str | None = None) -> None:
        """Idempotent single cleanup path — always returns to IDLE.

        Safe to call multiple times; subsequent calls are no-ops.

        Parameters
        ----------
        error:
            Human-readable error string including the exception class name
            (``"ReadTimeout: ..."``) or ``None`` for a clean exit.
            When set, appends an ``error`` scroll-buffer event and marks the
            turn as ``AgentState.ERROR`` internally before resetting to IDLE.
        """
        # Capture elapsed before clearing _start_time.
        elapsed = self.elapsed_s
        if self._current_turn is not None:
            if error:
                self._current_turn.state = AgentState.ERROR
                self.append_event("error", {"message": error})
            else:
                self._current_turn.state = AgentState.COMPLETE
            # Always emit turn_complete (with elapsed) so the scroll buffer can
            # print "✾ Worked for …" regardless of success or error path.
            self.append_event("turn_complete", {"elapsed_s": elapsed})
        self._current_turn = None
        self.agent_state.set(AgentState.IDLE)   # ALWAYS IDLE — invariant
        self.active_tool.set("")
        self._start_time = 0.0

    def end_turn(self) -> None:
        """Close the turn successfully. Prefer ``close_turn()`` for new code."""
        self.close_turn()

    def fail_turn(self, error: str) -> None:
        """Close the turn with an error. Prefer ``close_turn(error=...)`` for new code."""
        self.close_turn(error=error)

    # ── tool state ────────────────────────────────────────────────────────────

    def set_tool(self, name: str) -> None:
        self.active_tool.set(name)
        self.agent_state.set(AgentState.RUNNING)

    def clear_tool(self, success: bool = True) -> None:
        self.active_tool.set("")
        if self.agent_state() == AgentState.RUNNING:
            next_state = AgentState.THINKING if success else AgentState.RECOVERING
            self.agent_state.set(next_state)

    # ── metrics ───────────────────────────────────────────────────────────────

    def add_tokens(self, inp: int, out: int, cost: float) -> None:
        self.tokens_in.set(self.tokens_in() + inp)
        self.tokens_out.set(self.tokens_out() + out)
        self.cost_usd.set(self.cost_usd() + cost)

    def set_tokens(self, inp: int, out: int, cost: float) -> None:
        """Overwrite token counts with authoritative absolute values.

        Used by the AgentRunComplete reconciliation path.  Signal equality
        short-circuits no-ops, so calling this with already-correct values
        causes zero extra redraws.
        """
        self.tokens_in.set(inp)
        self.tokens_out.set(out)
        self.cost_usd.set(cost)

    # ── event appending ───────────────────────────────────────────────────────

    def append_event(
        self,
        kind: str,
        payload: dict[str, object],
        event_id: str | None = None,
    ) -> ConversationEvent:
        ev = ConversationEvent(
            event_id=event_id or str(uuid.uuid4()),
            kind=kind,
            payload=payload,
        )
        if self._current_turn is not None:
            self._current_turn.events.append(ev)
        # Keep tool_group_count in sync before notifying subscribers so any
        # Live-block component that reads it gets the updated value immediately.
        if kind == "tool_complete":
            self.tool_group_count.set(self.tool_group_count.get() + 1)
        elif kind in ("text", "turn_start"):
            self.tool_group_count.set(0)
        for sub in list(self._event_subscribers):
            try:
                sub(ev)
            except Exception:       # noqa: BLE001
                pass
        return ev

    def on_event(
        self,
        fn: Callable[[ConversationEvent], None],
    ) -> Callable[[], None]:
        """Subscribe to new conversation events. Returns unsubscribe callable."""
        self._event_subscribers.append(fn)
        return lambda: self._safely_remove_sub(fn)

    def _safely_remove_sub(self, fn: Callable[[ConversationEvent], None]) -> None:
        try:
            self._event_subscribers.remove(fn)
        except ValueError:
            pass


# ── Input state ───────────────────────────────────────────────────────────────

class InputState:
    """Reactive state for the composer (input bar)."""

    def __init__(self) -> None:
        self.buf:             Signal[list[str]]    = Signal([])
        self.cursor:          Signal[int]          = Signal(0)
        self.paste_condensed: Signal[bool]         = Signal(False)
        self.paste_label:     Signal[str]          = Signal("")

    def update(
        self,
        buf: list[str],
        cursor: int,
        paste_condensed: bool = False,
        paste_label: str = "",
    ) -> None:
        self.buf.set(list(buf))
        self.cursor.set(cursor)
        self.paste_condensed.set(paste_condensed)
        self.paste_label.set(paste_label)

    def clear(self) -> None:
        self.update([], 0)


# ── Root application state ────────────────────────────────────────────────────

class AppState:
    """Root state container — single instance for the application lifetime."""

    def __init__(self) -> None:
        from agenthicc.tui.runtime.mode_manager import RuntimeMode  # noqa: PLC0415
        from agenthicc.cli.context import CLIFlags                  # noqa: PLC0415
        self.conversation = ConversationStore()
        self.input        = InputState()
        self.active_mode: Signal[RuntimeMode] = Signal(
            RuntimeMode(name="Auto", badge="⏵⏵", description="Automatic")
        )
        self.overlay:           Signal[str]  = Signal("")     # active overlay name
        self.modal_open:        Signal[bool] = Signal(False)
        # PRD-78: non-None when an agent tool is paused waiting for approval.
        self.pending_approval: Signal[ApprovalRequest | None]  = Signal(None)
        # PRD-81: holds WorkflowRun | None; set by WorkflowRunner during execution.
        self.workflow_run:     Signal[WorkflowRun | None]      = Signal(None)
        # PRD-79: ephemeral CLI flags — frozen after startup, read by ApprovalGate etc.
        self.cli_flags: CLIFlags = CLIFlags()

    @classmethod
    def create(cls) -> "AppState":
        return cls()

    def update_workflow_phase(
        self,
        *,
        workflow_name:  str,
        phase_name:     str,
        phase_index:    int,
        total_phases:   int,
        run_id:         str,
        intent:         str,
        model_id:       str = "",
    ) -> None:
        """Atomically update all workflow TUI state from a phase's parameters.

        Replaces scattered ``dataclasses.replace(wf_run, ...) + workflow_run.set()``
        boilerplate in each phase method.  Creates a fresh ``WorkflowRun`` when
        no run is currently set.

        Parameters
        ----------
        workflow_name:  Registry name of the running workflow (e.g. ``"code_plan"``).
        phase_name:     Current phase identifier (e.g. ``"plan"``).
        phase_index:    0-based position of this phase in the workflow graph.
        total_phases:   Total phase count shown in the ``N/M`` status-bar counter.
        run_id:         UUID hex for the current run.
        intent:         Original user intent string.
        model_id:       Model string shown in phase display (optional).
        """
        import dataclasses as _dc  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowRun  # noqa: PLC0415

        current = self.workflow_run()
        if current is not None and _dc.is_dataclass(current):
            updated = _dc.replace(
                current,
                workflow_name        = workflow_name,
                current_phase        = phase_name,
                current_phase_index  = phase_index,
                total_phases         = total_phases,
                status               = "running",
                current_phase_model  = model_id,
            )
        else:
            updated = WorkflowRun(
                run_id               = run_id,
                workflow_name        = workflow_name,
                intent               = intent,
                current_phase        = phase_name,
                current_phase_index  = phase_index,
                total_phases         = total_phases,
                status               = "running",
                current_phase_model  = model_id,
            )
        self.workflow_run.set(updated)
