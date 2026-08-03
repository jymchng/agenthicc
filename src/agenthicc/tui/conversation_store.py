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
    from agenthicc.tui.runtime.mode_manager import RuntimeMode
    from agenthicc.workflows.plugin import WorkflowRun
    from agenthicc.subagents.pool import SubagentPoolState


# ── Agent state ───────────────────────────────────────────────────────────────


class AgentState(Enum):
    IDLE = auto()
    THINKING = auto()
    RUNNING = auto()  # tool executing
    RECOVERING = auto()  # tool failed; LLM deciding how to respond
    COMPLETE = auto()
    ERROR = auto()


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
    event_id: str
    kind: str  # EventKind
    payload: dict[str, object]
    timestamp: float = field(default_factory=time.time)
    rendered: bool = False  # True once ScrollBufferAppender has printed it


@dataclass
class ConversationTurn:
    turn_id: str
    agent_name: str
    timestamp: float = field(default_factory=time.time)
    events: list[ConversationEvent] = field(default_factory=list)
    state: AgentState = AgentState.THINKING


# ── Notification entry ────────────────────────────────────────────────────────


@dataclass
class _NotificationEntry:
    """One stacked transient notification line with its auto-dismiss timer."""

    text: str
    handle: _asyncio.TimerHandle | None = field(default=None)


# ── Store ─────────────────────────────────────────────────────────────────────


class ConversationStore:
    """Reactive store for the full conversation history and agent state.

    All UI components derive their rendered output from this store's signals.
    No component holds authoritative state of its own.
    """

    def __init__(self) -> None:
        # ── core signals ──────────────────────────────────────────────────────
        self.turns: Signal[list[ConversationTurn]] = Signal([])
        self.agent_state: Signal[AgentState] = Signal(AgentState.IDLE)
        self.active_tool: Signal[str] = Signal("")
        self.frame: Signal[int] = Signal(0)
        """Shared animation counter for active status surfaces (PRD-120/164).

        ``frame`` advances on active thinking/tool/recovery or compaction ticks.
        It intentionally does not advance while the visible status is idle,
        complete, or in an error state because those renderings are static.
        All animated elements (flower, thinking, compaction spinner) derive
        their frame index from ``frame() % N``. The workspace subscribes once
        for animation redraws.
        """
        self.tokens_in: Signal[int] = Signal(0)
        self.tokens_out: Signal[int] = Signal(0)
        self.cost_usd: Signal[float] = Signal(0.0)
        self.usage_status: Signal[str] = Signal("unavailable")
        self.cost_status: Signal[str] = Signal("unavailable")
        self.usage_calls: Signal[int] = Signal(0)
        self.session_id: Signal[str] = Signal("")
        self.model_name: Signal[str] = Signal("")
        self.notification: Signal[str | None] = Signal(None)
        self.workflow_override: Signal[str | None] = Signal(None)
        """Name of the /workflow-selected override (PRD-114).  None = mode default."""
        self.transcript_loading: Signal[bool] = Signal(False)
        """True while a resumed session transcript is being replayed."""
        self.compaction_active: Signal[bool] = Signal(False)
        """True while a compaction LLM call is in flight (PRD-119)."""
        self.subagent_pool_state: Signal[SubagentPoolState | None] = Signal(None)
        """Live state of the active SubagentPool — None when no pool is running (PRD-124)."""
        # PRD-149: owned background-terminal wait projection.  These are
        # separate signals so a live wait can refresh its elapsed/count line
        # without changing the conversation event stream.
        self.terminal_waiting: Signal[bool] = Signal(False)
        self.terminal_wait_id: Signal[str] = Signal("")
        self.terminal_wait_label: Signal[str] = Signal("")
        self.terminal_wait_elapsed_s: Signal[float] = Signal(0.0)
        self.terminal_running_count: Signal[int] = Signal(0)
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
        self.tool_group_count: Signal[int] = Signal(0)
        # Number of tool calls currently hidden above the scroll-buffer threshold.
        # Set live by ScrollBufferAppender as each overflow call arrives;
        # reset to 0 when the group closes (text/error event).
        # FooterComponent renders this as a live "⎿ …and N more" footer row.
        self.live_tool_overflow: Signal[int] = Signal(0)

        # ── computed values ───────────────────────────────────────────────────
        self.is_running: Computed[bool] = Computed(
            lambda: (
                self.agent_state() not in (AgentState.IDLE, AgentState.COMPLETE, AgentState.ERROR)
            ),
            self.agent_state,
        )
        self.turn_count: Computed[int] = Computed(
            lambda: len(self.turns()),
            self.turns,
        )
        self.total_tokens: Computed[int] = Computed(
            lambda: self.tokens_in() + self.tokens_out(),
            self.tokens_in,
            self.tokens_out,
        )

        # ── animation (driven by tick) ────────────────────────────────────────
        self._start_time: float = 0.0
        # ``elapsed_s`` is intentionally wall-clock based for turn telemetry.
        # The status bar uses this separate cached clock so a render caused by
        # SIGWINCH cannot change a waiting modal's visible duration.
        self.display_elapsed_s: Signal[float] = Signal(0.0)
        self._display_paused: bool = False
        self._last_display_tick: float | None = None
        # The outer activity spans all LLM turns belonging to one user request
        # (for example, every phase of a workflow).  ``begin_turn``/
        # ``close_turn`` still delimit individual ConversationTurn records,
        # but the status bar must not reset while that outer activity remains
        # live.  This is deliberately wall-clock based: the value represents
        # the total time between the activity becoming non-idle and returning
        # to IDLE, including internal turn boundaries and user-owned waits.
        self.activity_elapsed_s: Signal[float] = Signal(0.0)
        self._activity_start_time: float = 0.0
        self._activity_active: bool = False
        self._activity_external_scope: bool = False

        # ── internal ──────────────────────────────────────────────────────────
        self._current_turn: ConversationTurn | None = None
        self._event_subscribers: list[Callable[[ConversationEvent], None]] = []
        # Subscribe to detect external notification.set(None) and cancel stacked timers.
        self.notification.subscribe(self._on_notification_externally_cleared)

    # ── tick ──────────────────────────────────────────────────────────────────

    @property
    def elapsed_s(self) -> float:
        """Wall-clock seconds since the current turn started, or 0.0 when idle.

        This remains the source for turn-completion telemetry.  The status bar
        uses :attr:`activity_elapsed_s` for the total outer activity duration;
        :attr:`display_elapsed_s` remains the cached per-turn active-work
        duration used by waiting-modal behavior.
        """
        return time.monotonic() - self._start_time if self._start_time else 0.0

    def _advance_display_clock(self, now: float) -> None:
        """Accumulate active-work time without reading the clock from render.

        ``_last_display_tick`` is moved while paused as well.  That prevents
        the entire user-wait interval from being charged when the next active
        tick arrives.
        """
        previous = self._last_display_tick
        self._last_display_tick = now
        if previous is None or self._display_paused or self._current_turn is None:
            return
        delta = max(0.0, now - previous)
        if delta:
            self.display_elapsed_s.set(self.display_elapsed_s() + delta)

    def _advance_activity_clock(self, now: float) -> None:
        """Refresh the total wall-clock duration of the outer agent activity."""

        if not self._activity_active:
            return
        elapsed = max(0.0, now - self._activity_start_time)
        if elapsed != self.activity_elapsed_s():
            self.activity_elapsed_s.set(elapsed)

    def begin_activity(self) -> None:
        """Start the total-duration clock for one user-level agent activity.

        The operation is idempotent so the TUI can establish the outer scope
        before dispatching a direct turn or a multi-phase workflow. Individual
        ``begin_turn`` calls nested inside that scope do not reset this clock.
        """

        if self._activity_active:
            # An outer caller may claim an activity after a direct turn has
            # already opened its implicit scope. Preserve the single clock but
            # transfer the final IDLE boundary to that outer caller.
            self._activity_external_scope = True
            return
        self._start_activity(external=True)

    def _start_activity(self, *, external: bool) -> None:
        """Start an activity clock, recording who owns its idle boundary."""

        if self._activity_active:
            return
        self._activity_active = True
        self._activity_external_scope = external
        self._activity_start_time = time.monotonic()
        self.activity_elapsed_s.set(0.0)

    @property
    def is_activity_active(self) -> bool:
        """Whether the outer user-level activity is between its idle edges."""

        return self._activity_active

    def end_activity(self) -> float:
        """Stop and reset the outer activity clock, returning its total seconds."""

        if not self._activity_active:
            return 0.0
        self._advance_activity_clock(time.monotonic())
        elapsed = self.activity_elapsed_s()
        # Publish the final wall-clock value before resetting the reactive
        # clock.  The scroll renderer uses this event to report the complete
        # user-intent duration after the last per-turn "Worked for" line.
        self.agent_state.set(AgentState.IDLE)
        self.append_event("activity_complete", {"elapsed_s": elapsed})
        self._activity_active = False
        self._activity_external_scope = False
        self._activity_start_time = 0.0
        self.activity_elapsed_s.set(0.0)
        return elapsed

    def set_display_paused(self, paused: bool) -> None:
        """Pause or resume the cached UI clock at an idempotent state edge.

        ``AppState.pending_approval`` calls this synchronously, so repeated
        signal writes and overlay replacements cannot double-count a wait.
        """
        if paused == self._display_paused:
            return
        now = time.monotonic()
        # Capture active work up to the exact pause edge.  When resuming, the
        # current paused timestamp becomes the new accumulation baseline.
        self._advance_display_clock(now)
        self._display_paused = paused
        self._last_display_tick = now

    def tick(self, *, paused: bool | None = None) -> None:
        """Advance active animation and cached active-work time.

        The session pauses this tick while an approval or question overlay is
        waiting for the user. That keeps the status bar and Live region stable
        while a modal prompt owns the terminal. ``paused=None`` uses the
        authoritative state set by ``AppState.pending_approval``; an explicit
        boolean is retained for session-loop callers and focused tests.
        """
        if paused is not None:
            self.set_display_paused(paused)
        now = time.monotonic()
        self._advance_activity_clock(now)
        self._advance_display_clock(now)
        if self._display_paused:
            return
        # Idle/complete/error status is static. Publishing a frame on every
        # idle tick would cause Workspace to refresh the Live block at 20 Hz;
        # terminals or captured clients that do not interpret Rich's erase
        # controls would then display duplicate idle panels (PRD-164).
        if self.is_running() or self.compaction_active():
            self.frame.set(self.frame() + 1)

    def set_terminal_wait(
        self,
        *,
        terminal_id: str,
        label: str,
        elapsed_s: float,
        running_count: int,
    ) -> None:
        """Project one active terminal wait into the reactive status bar."""

        self.terminal_waiting.set(True)
        self.terminal_wait_id.set(terminal_id)
        self.terminal_wait_label.set(label)
        self.terminal_wait_elapsed_s.set(max(0.0, elapsed_s))
        self.terminal_running_count.set(max(0, running_count))

    def clear_terminal_wait(self) -> None:
        """Clear terminal wait state after completion, cancellation, or close."""

        self.terminal_waiting.set(False)
        self.terminal_wait_id.set("")
        self.terminal_wait_label.set("")
        self.terminal_wait_elapsed_s.set(0.0)
        self.terminal_running_count.set(0)

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
            self._notification_lines = [e for e in self._notification_lines if e is not entry]
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
        # Direct callers that do not have an outer TUI activity still get a
        # correctly scoped total clock. TUISession starts the outer scope first
        # for workflows so consecutive internal turns share one duration.
        if not self._activity_active:
            self._start_activity(external=False)
        turn = ConversationTurn(turn_id=tid, agent_name=agent_name)
        self._current_turn = turn
        self.turns.set(self.turns.get() + [turn])
        self._start_time = time.monotonic()
        self.display_elapsed_s.set(0.0)
        self._display_paused = False
        self._last_display_tick = self._start_time
        self.agent_state.set(AgentState.THINKING)
        return turn

    @property
    def is_turn_active(self) -> bool:
        """True when a turn is currently open (between begin_turn and close_turn)."""
        return self._current_turn is not None

    def close_turn(self, *, error: str | None = None) -> None:
        """Idempotent cleanup for one ConversationTurn.

        Safe to call multiple times; subsequent calls are no-ops.

        When an outer activity is active, this closes only the individual
        ConversationTurn and leaves the store non-idle. The outer
        ``end_activity()`` call owns the final transition to IDLE.

        Parameters
        ----------
        error:
            Human-readable error string including the exception class name
            (``"ReadTimeout: ..."``) or ``None`` for a clean exit.
            When set, appends an ``error`` scroll-buffer event and marks the
            turn as ``AgentState.ERROR`` internally. An outer activity remains
            non-idle until its own ``end_activity()`` call.
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
        activity_active = self._activity_active
        external_activity = self._activity_external_scope
        self._current_turn = None
        # An outer TUI activity owns the real IDLE boundary. Between workflow
        # phases, remain non-idle so the status duration continues accumulating;
        # end_activity() performs the final transition to IDLE.
        if external_activity:
            self.agent_state.set(AgentState.THINKING)
        else:
            self.agent_state.set(AgentState.IDLE)
        self.active_tool.set("")
        self._start_time = 0.0
        self.display_elapsed_s.set(0.0)
        self._display_paused = False
        self._last_display_tick = None
        if activity_active and not external_activity:
            self.end_activity()

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
        self.usage_status.set("complete")
        self.cost_status.set("estimated")
        self.usage_calls.set(self.usage_calls() + 1)

    def set_tokens(self, inp: int, out: int, cost: float) -> None:
        """Overwrite token counts with authoritative absolute values.

        Used by the AgentRunComplete reconciliation path.  Signal equality
        short-circuits no-ops, so calling this with already-correct values
        causes zero extra redraws.
        """
        self.tokens_in.set(inp)
        self.tokens_out.set(out)
        self.cost_usd.set(cost)
        self.usage_status.set("complete")
        self.cost_status.set("estimated")

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
            except Exception:  # noqa: BLE001
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
        self.buf: Signal[list[str]] = Signal([])
        self.cursor: Signal[int] = Signal(0)
        self.paste_condensed: Signal[bool] = Signal(False)
        self.paste_label: Signal[str] = Signal("")

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
        from agenthicc.tui.runtime.mode_manager import build_safe_mode  # noqa: PLC0415
        from agenthicc.cli.context import CLIFlags  # noqa: PLC0415

        self.conversation = ConversationStore()
        self.input = InputState()
        self.active_mode: Signal[RuntimeMode] = Signal(build_safe_mode())
        self.overlay: Signal[str] = Signal("")  # active overlay name
        self.modal_open: Signal[bool] = Signal(False)
        # PRD-78: non-None when an agent tool is paused waiting for approval.
        self.pending_approval: Signal[ApprovalRequest | None] = Signal(None)
        # Keep the cached display clock in sync with the authoritative prompt
        # state.  This is synchronous and idempotent; rendering and resizing
        # never need to inspect a wall clock to decide whether a wait is active.
        self.pending_approval.subscribe(self._sync_display_wait_state)
        # PRD-81: holds WorkflowRun | None; set by WorkflowRunner during execution.
        self.workflow_run: Signal[WorkflowRun | None] = Signal(None)
        # PRD-79: ephemeral CLI flags — frozen after startup, read by ApprovalGate etc.
        self.cli_flags: CLIFlags = CLIFlags()

    def _sync_display_wait_state(self) -> None:
        """Mirror prompt ownership into the conversation display clock."""
        self.conversation.set_display_paused(self.pending_approval() is not None)

    @classmethod
    def create(cls) -> "AppState":
        return cls()

    def update_workflow_phase(
        self,
        *,
        workflow_name: str,
        phase_name: str,
        phase_index: int,
        total_phases: int,
        run_id: str,
        intent: str,
        model_id: str = "",
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
                workflow_name=workflow_name,
                current_phase=phase_name,
                current_phase_index=phase_index,
                total_phases=total_phases,
                status="running",
                current_phase_model=model_id,
            )
        else:
            updated = WorkflowRun(
                run_id=run_id,
                workflow_name=workflow_name,
                intent=intent,
                current_phase=phase_name,
                current_phase_index=phase_index,
                total_phases=total_phases,
                status="running",
                current_phase_model=model_id,
            )
        self.workflow_run.set(updated)
