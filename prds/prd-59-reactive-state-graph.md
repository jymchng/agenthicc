# PRD-59 — Reactive State Graph

## 1. Purpose

The Reactive State Graph is the **single source of truth** for the entire
application. All UI components, renderers, and event handlers derive their
output from this graph. No component holds its own authoritative state.

The graph is built on **signals** and **computed values**. A signal is a
cell that holds a value and notifies subscribers when it changes. A computed
value is derived from one or more signals and automatically recomputes.

---

## 2. Signal Primitive

```python
from __future__ import annotations
from typing import TypeVar, Generic, Callable, Any
import threading

T = TypeVar("T")

class Signal(Generic[T]):
    """A reactive cell. Writes trigger subscriber notifications."""

    def __init__(self, initial: T) -> None:
        self._value = initial
        self._subscribers: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def get(self) -> T:
        return self._value

    def set(self, value: T) -> None:
        with self._lock:
            if value == self._value:
                return
            self._value = value
        for sub in list(self._subscribers):
            try:
                sub()
            except Exception:
                pass

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Register *fn* to be called on change. Returns an unsubscribe fn."""
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)

    # Convenience
    def __call__(self) -> T:
        return self.get()


class Computed(Generic[T]):
    """A read-only value derived from one or more signals."""

    def __init__(self, fn: Callable[[], T], *deps: Signal) -> None:
        self._fn = fn
        self._value = fn()
        self._subscribers: list[Callable[[], None]] = []

        def _recompute() -> None:
            new = fn()
            if new != self._value:
                self._value = new
                for sub in list(self._subscribers):
                    try:
                        sub()
                    except Exception:
                        pass

        for dep in deps:
            dep.subscribe(_recompute)

    def get(self) -> T:
        return self._value

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)

    def __call__(self) -> T:
        return self.get()
```

---

## 3. ConversationStore

The `ConversationStore` is the central reactive store. Everything the user
sees in the conversation surface is derived from this store.

```python
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal
import time

class AgentState(Enum):
    IDLE      = auto()
    THINKING  = auto()
    RUNNING   = auto()   # tool executing
    COMPLETE  = auto()
    ERROR     = auto()


@dataclass
class ConversationTurn:
    turn_id: str
    agent_name: str
    timestamp: float = field(default_factory=time.time)
    events: list["ConversationEvent"] = field(default_factory=list)
    state: AgentState = AgentState.THINKING


@dataclass
class ConversationEvent:
    event_id: str
    kind: Literal["text", "tool_start", "tool_complete", "thinking", "error"]
    payload: dict
    timestamp: float = field(default_factory=time.time)
    rendered: bool = False   # True once ScrollBufferAppender has printed it


class ConversationStore:
    """Reactive store for the full conversation history and agent state."""

    def __init__(self) -> None:
        # ── signals ───────────────────────────────────────────────────────────
        self.turns:       Signal[list[ConversationTurn]] = Signal([])
        self.agent_state: Signal[AgentState]             = Signal(AgentState.IDLE)
        self.active_tool: Signal[str]                    = Signal("")
        self.elapsed_s:   Signal[float]                  = Signal(0.0)
        self.tokens_in:   Signal[int]                    = Signal(0)
        self.tokens_out:  Signal[int]                    = Signal(0)
        self.cost_usd:    Signal[float]                  = Signal(0.0)
        self.session_id:  Signal[str]                    = Signal("")
        self.model_name:  Signal[str]                    = Signal("")
        self.mode_str:    Signal[str]                    = Signal(
            "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵"
        )

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

        # ── internal ──────────────────────────────────────────────────────────
        self._current_turn: ConversationTurn | None = None
        self._event_subscribers: list[Callable[[ConversationEvent], None]] = []

    # ── Turn lifecycle ────────────────────────────────────────────────────────

    def begin_turn(self, turn_id: str, agent_name: str) -> ConversationTurn:
        turn = ConversationTurn(turn_id=turn_id, agent_name=agent_name)
        self._current_turn = turn
        self.turns.set(self.turns.get() + [turn])
        self.agent_state.set(AgentState.THINKING)
        return turn

    def end_turn(self) -> None:
        if self._current_turn:
            self._current_turn.state = AgentState.COMPLETE
        self._current_turn = None
        self.agent_state.set(AgentState.IDLE)
        self.active_tool.set("")

    def fail_turn(self, error: str) -> None:
        if self._current_turn:
            self._current_turn.state = AgentState.ERROR
            self.append_event("error", {"message": error})
        self._current_turn = None
        self.agent_state.set(AgentState.ERROR)
        self.active_tool.set("")

    # ── Event appending ───────────────────────────────────────────────────────

    def append_event(
        self,
        kind: str,
        payload: dict,
        event_id: str | None = None,
    ) -> ConversationEvent:
        import uuid
        ev = ConversationEvent(
            event_id=event_id or str(uuid.uuid4()),
            kind=kind,  # type: ignore[arg-type]
            payload=payload,
        )
        if self._current_turn:
            self._current_turn.events.append(ev)
        # Notify event subscribers (ScrollBufferAppender, etc.)
        for sub in list(self._event_subscribers):
            try:
                sub(ev)
            except Exception:
                pass
        return ev

    def on_event(self, fn: Callable[[ConversationEvent], None]) -> Callable[[], None]:
        """Subscribe to new conversation events. Returns unsubscribe fn."""
        self._event_subscribers.append(fn)
        return lambda: self._event_subscribers.remove(fn)

    # ── Metrics ───────────────────────────────────────────────────────────────

    def add_tokens(self, inp: int, out: int, cost: float) -> None:
        self.tokens_in.set(self.tokens_in() + inp)
        self.tokens_out.set(self.tokens_out() + out)
        self.cost_usd.set(self.cost_usd() + cost)

    def set_tool(self, name: str) -> None:
        self.active_tool.set(name)
        self.agent_state.set(AgentState.RUNNING)

    def clear_tool(self) -> None:
        self.active_tool.set("")
        self.agent_state.set(AgentState.THINKING)
```

---

## 4. Input State

```python
@dataclass
class InputState:
    """Reactive state for the composer (input bar)."""
    buf:             Signal[list[str]]  = field(default_factory=lambda: Signal([]))
    cursor:          Signal[int]        = field(default_factory=lambda: Signal(0))
    paste_condensed: Signal[bool]       = field(default_factory=lambda: Signal(False))
    paste_label:     Signal[str]        = field(default_factory=lambda: Signal(""))
    mode:            Signal[str]        = field(default_factory=lambda: Signal("idle"))

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
```

---

## 5. Application State (Root)

```python
class AppState:
    """Root state container. Single instance for the application lifetime."""

    def __init__(self) -> None:
        self.conversation = ConversationStore()
        self.input        = InputState()
        # Active overlay name (empty = none)
        self.overlay:     Signal[str]  = Signal("")
        # Whether config menu or other modal is open
        self.modal_open:  Signal[bool] = Signal(False)

    @classmethod
    def create(cls) -> "AppState":
        """Factory — use this to create the singleton."""
        return cls()
```

---

## 6. Reactive Binding Rules

**Rule 1: No component mutates state directly.**

All mutations go through well-defined methods on the store:
```python
# CORRECT
app_state.conversation.add_tokens(inp, out, cost)

# WRONG — never do this
app_state.conversation.tokens_in._value += inp
```

**Rule 2: Components subscribe, never poll.**

```python
# CORRECT
unsub = app_state.conversation.agent_state.subscribe(self._on_state_change)

# WRONG
while True:
    state = app_state.conversation.agent_state.get()
    await asyncio.sleep(0.1)
```

**Rule 3: Computed values are never mutated.**

Computed values recalculate automatically when their dependencies change.

**Rule 4: Thread safety is the Signal's responsibility.**

Signals use a lock internally. Callers do not need external locking.

---

## 7. State Persistence / Snapshot

For session restoration:

```python
class StateSnapshot:
    """Serialisable snapshot of ConversationStore for session save/restore."""

    def to_dict(self, store: ConversationStore) -> dict:
        return {
            "session_id": store.session_id(),
            "model_name": store.model_name(),
            "turns": [self._turn_to_dict(t) for t in store.turns()],
            "tokens_in": store.tokens_in(),
            "tokens_out": store.tokens_out(),
            "cost_usd": store.cost_usd(),
        }

    def restore(self, store: ConversationStore, data: dict) -> None:
        store.session_id.set(data.get("session_id", ""))
        store.model_name.set(data.get("model_name", ""))
        store.tokens_in.set(data.get("tokens_in", 0))
        store.tokens_out.set(data.get("tokens_out", 0))
        store.cost_usd.set(data.get("cost_usd", 0.0))
        # Turns are restored as non-streaming (rendered = True)
        ...
```

---

## 8. Derived State Examples

```python
# Status bar first line signal
status_line_1 = Computed(
    lambda: _build_status_line_1(
        store.agent_state(),
        store.active_tool(),
        store.elapsed_s(),
    ),
    store.agent_state, store.active_tool, store.elapsed_s,
)

# Status bar second line signal (model + tokens)
status_line_2 = Computed(
    lambda: _build_status_line_2(
        store.model_name(),
        store.total_tokens(),
        store.cost_usd(),
    ),
    store.model_name, store.total_tokens, store.cost_usd,
)

# Footer hints (change with agent state)
footer_hints = Computed(
    lambda: HINTS[store.agent_state()],
    store.agent_state,
)
```

---

## 9. Migration from Current State

| Current | Replacement |
|---|---|
| `StatusBarState._state` | `ConversationStore.agent_state` (Signal) |
| `StatusBarState._tool` | `ConversationStore.active_tool` (Signal) |
| `StatusBarState._session_id` | `ConversationStore.session_id` (Signal) |
| `FooterState._mode` | `ConversationStore.agent_state` → computed hints |
| `InputBarState.buf` | `InputState.buf` (Signal) |
| `SpinnerState` | **Deleted** — tool calls go to scroll buffer |
| `_printed_count` | **Deleted** — `ConversationEvent.rendered` flag |
| `_text_events_printed` | **Deleted** — event subscriber pattern |
| `_pending_menu` | `AppState.overlay` (Signal) |

---

## 10. Performance Considerations

- Signals use a write-through cache: if value unchanged, no notifications fire
- Computed values cache their last result; recompute only on dependency change
- Subscriber lists are copied before iteration (safe concurrent append/remove)
- All mutations from the asyncio event loop (no cross-thread writes except via `Signal.set` which is lock-protected)
- Subscription count stays small (<20 per signal); O(N) notification is acceptable
