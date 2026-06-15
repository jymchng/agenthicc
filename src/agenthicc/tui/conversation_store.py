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
from typing import Callable, Literal

from agenthicc.reactive import Signal, Computed


# ── Agent state ───────────────────────────────────────────────────────────────

class AgentState(Enum):
    IDLE      = auto()
    THINKING  = auto()
    RUNNING   = auto()   # tool executing
    COMPLETE  = auto()
    ERROR     = auto()


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
]


@dataclass
class ConversationEvent:
    event_id:  str
    kind:      str          # EventKind
    payload:   dict
    timestamp: float = field(default_factory=time.time)
    rendered:  bool = False  # True once ScrollBufferAppender has printed it


@dataclass
class ConversationTurn:
    turn_id:    str
    agent_name: str
    timestamp:  float = field(default_factory=time.time)
    events:     list[ConversationEvent] = field(default_factory=list)
    state:      AgentState = AgentState.THINKING


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
        self.elapsed_s:        Signal[float]                  = Signal(0.0)
        self.tokens_in:        Signal[int]                    = Signal(0)
        self.tokens_out:       Signal[int]                    = Signal(0)
        self.cost_usd:         Signal[float]                  = Signal(0.0)
        self.session_id:       Signal[str]                    = Signal("")
        self.model_name:       Signal[str]                    = Signal("")
        self.active_mode_name: Signal[str]                    = Signal("Auto")
        self.active_mode_badge:Signal[str]                    = Signal("⏵⏵")
        self.mode_str:         Signal[str]                    = Signal(
            "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵"
        )
        self.notification:     Signal[str | None]             = Signal(None)

        # ── computed values ───────────────────────────────────────────────────
        self.is_running: Computed[bool] = Computed(
            lambda: self.agent_state() not in (AgentState.IDLE, AgentState.COMPLETE),
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

        # ── animation frames (driven by tick) ────────────────────────────────
        self._thinking_frame: int = 0
        self._flower_frame:   int = 0
        self._start_time:     float = 0.0

        # ── internal ──────────────────────────────────────────────────────────
        self._current_turn: ConversationTurn | None = None
        self._event_subscribers: list[Callable[[ConversationEvent], None]] = []

    # ── tick ──────────────────────────────────────────────────────────────────

    def tick(self) -> None:
        """Advance animation frames and elapsed timer. Called every ~50 ms."""
        if self.agent_state() not in (AgentState.IDLE, AgentState.COMPLETE):
            elapsed = time.monotonic() - self._start_time if self._start_time else 0.0
            if abs(elapsed - self.elapsed_s()) >= 0.1:
                self.elapsed_s.set(elapsed)
                self._thinking_frame += 1
                self._flower_frame = (self._flower_frame + 1) % 8

    # ── turn lifecycle ────────────────────────────────────────────────────────

    def begin_turn(self, agent_name: str, turn_id: str | None = None) -> ConversationTurn:
        tid = turn_id or str(uuid.uuid4())
        turn = ConversationTurn(turn_id=tid, agent_name=agent_name)
        self._current_turn = turn
        self.turns.set(self.turns.get() + [turn])
        self._start_time = time.monotonic()
        self.elapsed_s.set(0.0)
        self._thinking_frame = 0
        self.agent_state.set(AgentState.THINKING)
        return turn

    def end_turn(self) -> None:
        if self._current_turn:
            self._current_turn.state = AgentState.COMPLETE
        self._current_turn = None
        self.agent_state.set(AgentState.IDLE)
        self.active_tool.set("")
        self._start_time = 0.0

    def fail_turn(self, error: str) -> None:
        if self._current_turn:
            self._current_turn.state = AgentState.ERROR
            self.append_event("error", {"message": error})
        self._current_turn = None
        self.agent_state.set(AgentState.ERROR)
        self.active_tool.set("")

    # ── tool state ────────────────────────────────────────────────────────────

    def set_tool(self, name: str) -> None:
        self.active_tool.set(name)
        self.agent_state.set(AgentState.RUNNING)

    def clear_tool(self) -> None:
        self.active_tool.set("")
        if self.agent_state() == AgentState.RUNNING:
            self.agent_state.set(AgentState.THINKING)

    # ── metrics ───────────────────────────────────────────────────────────────

    def add_tokens(self, inp: int, out: int, cost: float) -> None:
        self.tokens_in.set(self.tokens_in() + inp)
        self.tokens_out.set(self.tokens_out() + out)
        self.cost_usd.set(self.cost_usd() + cost)

    # ── event appending ───────────────────────────────────────────────────────

    def append_event(
        self,
        kind: str,
        payload: dict,
        event_id: str | None = None,
    ) -> ConversationEvent:
        ev = ConversationEvent(
            event_id=event_id or str(uuid.uuid4()),
            kind=kind,
            payload=payload,
        )
        if self._current_turn is not None:
            self._current_turn.events.append(ev)
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
        self.conversation = ConversationStore()
        self.input        = InputState()
        self.overlay:     Signal[str]  = Signal("")     # active overlay name
        self.modal_open:  Signal[bool] = Signal(False)

    @classmethod
    def create(cls) -> "AppState":
        return cls()
