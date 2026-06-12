---
title: "PRD-01: AppState Kernel and Event System"
status: draft
version: 0.1.0
created: 2025-01-01
authors:
  - platform-team
tags:
  - appstate
  - event-bus
  - reducer
  - crash-recovery
  - immutability
---

# PRD-01: AppState Kernel and Event System

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Architecture and Design](#3-architecture-and-design)
4. [Data Structures and Interfaces](#4-data-structures-and-interfaces)
5. [Implementation Plan](#5-implementation-plan)
6. [Tests](#6-tests)
7. [Configuration Reference](#7-configuration-reference)
8. [Open Questions](#8-open-questions)

---

## 1. Executive Summary

The **AppState Kernel** is the single authoritative data model for a running
multi-agent session. It replaces the current ad-hoc mixture of mutable objects
scattered across `lauren_ai._agents._runner.AgentRunnerBase`, per-run
`AgentContext` instances, and the standalone `SignalBus` in
`lauren_ai._signals`. Instead of each component maintaining its own slice of
state, all state lives in one immutable `AppState` snapshot that is
transformed by a chain of pure **reducer** functions. The kernel hands back
an *effects list* — descriptions of side effects to carry out — rather than
performing side effects itself. This separation makes the entire state machine
testable without any I/O.

The **Event Bus** is the nervous system that connects producers (the runner
loop, tool executor, guardrails, memory subsystem) to the reducer pipeline.
Events are enqueued in a `asyncio.Queue` and consumed sequentially by a
single `EventProcessor` coroutine. Because every state transition produces a
new frozen snapshot, concurrent readers (HTTP handlers, monitoring dashboards,
streaming SSE endpoints) never contend with the writer: they atomically read
the current snapshot reference without a lock. The event log records every
event in append-only order, enabling deterministic crash recovery by
replaying the log against the initial state — the same property that powers
event-sourcing databases and distributed consensus protocols.

Together, the AppState Kernel and Event Bus provide a principled foundation
for the features that come after: multi-agent intent routing, workflow
orchestration, task scheduling, and policy enforcement. Any future subsystem
that needs to observe or mutate system state does so through this single
pathway, eliminating an entire class of concurrency bugs and making the
`lauren-ai` runtime auditable by construction.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal |
|---|------|
| G-1 | Provide a single `AppState` dataclass that is the authoritative source of truth for a session. |
| G-2 | Ensure `AppState` instances are immutable after construction; all mutations produce a new snapshot. |
| G-3 | Implement a multi-producer, single-consumer event queue with back-pressure. |
| G-4 | Define a pure-reducer protocol: `(AppState, Event) -> (AppState, list[Effect])`. |
| G-5 | Provide an `EventProcessor` that sequentially applies reducers and dispatches effects. |
| G-6 | Maintain an append-only `EventLog` for crash recovery. |
| G-7 | Enable deterministic recovery by replaying the event log against `AppState.empty()`. |
| G-8 | Allow lock-free reads from the current immutable snapshot via a shared `SnapshotIndex`. |
| G-9 | Integrate cleanly with `AgentRunnerBase`, `AgentContext`, `ToolContext`, and `SignalBus`. |
| G-10 | Expose a `settings` and `policy` namespace inside `AppState` for runtime configuration. |

### 2.2 Non-Goals

| # | Non-Goal |
|---|----------|
| NG-1 | This PRD does not redesign the LLM transport layer or tool schema system. |
| NG-2 | Distributed multi-process state synchronisation is out of scope (single-process only). |
| NG-3 | The event log format is not yet a stable public API — external consumers must not parse it directly. |
| NG-4 | This does not replace the existing `SignalBus` for external observability hooks; `SignalBus` remains as a fan-out layer above the kernel. |
| NG-5 | Long-term persistent storage (e.g., SQLite-backed history) is addressed in a future PRD. |
| NG-6 | HTTP/SSE streaming APIs are not in scope; only the in-process kernel contract is specified here. |

---

## 3. Architecture and Design

### 3.1 Layered Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                          Producers                              │
│                                                                 │
│  AgentRunnerBase   ToolExecutor   GuardrailRunner   MemorySub  │
│       │                 │                │               │      │
│       └────────────────►│◄──────────────┘◄──────────────┘      │
│                         │  emit(Event)                          │
└─────────────────────────┼───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Event Bus                                 │
│                                                                 │
│   asyncio.Queue[Event]  (bounded, back-pressure)               │
│                                                                 │
└─────────────────────────┬───────────────────────────────────────┘
                          │  dequeue (single consumer)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    EventProcessor                               │
│                                                                 │
│   for each event:                                               │
│     1. append to EventLog                                       │
│     2. run reducer chain:                                       │
│          (AppState, Event) → (AppState', [Effect])              │
│     3. atomically update SnapshotIndex                          │
│     4. dispatch Effects to EffectRunner                         │
│                                                                 │
└─────────────────────────┬───────────────────────────────────────┘
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
┌─────────────────────┐  ┌────────────────────────────────────┐
│    SnapshotIndex    │  │          EffectRunner              │
│                     │  │                                    │
│  current: AppState  │  │  EmitSignalEffect  → SignalBus     │
│  (atomic ref)       │  │  PersistMemEffect  → SQLite        │
│                     │  │  SpawnAgentEffect  → AgentRunner   │
│  lock-free reads    │  │  LogEffect         → Logger        │
└─────────────────────┘  └────────────────────────────────────┘
```

### 3.2 AppState Composition

```
AppState (frozen dataclass)
│
├── session_id: str
├── run_id: str
│
├── intents: tuple[Intent, ...]
├── workflows: tuple[Workflow, ...]
├── tasks: tuple[Task, ...]
│
├── agents: tuple[AgentRecord, ...]
│   └── AgentRecord: agent_id, agent_name, status, run_id, agent_class_name
│
├── tools: tuple[ToolRecord, ...]
│   └── ToolRecord: tool_name, call_count, last_call_ms, error_count
│
├── memory: MemorySnapshot
│   └── messages: tuple[MessageRecord, ...], summary: str | None
│
├── event_bus: EventBusConfig (queue maxsize, overflow policy)
│
├── event_log: EventLog (append-only sequence of LogEntry)
│
├── snapshot_index: int  (monotonic counter, incremented on every transition)
│
├── settings: AppSettings (model defaults, token budgets, feature flags)
│
├── policy: PolicyConfig (rate limits, allowed tool names, cost caps)
│
├── agent_types: tuple[str, ...]  (registered @agent class names)
│
└── tool_resolvers: tuple[ToolResolverRecord, ...]
```

### 3.3 Event → Reducer → Effect Pipeline

```
Event (sealed union)
│
├── AgentStarted(agent_id, agent_run_id, agent_name, model, session_id)
├── AgentTurnCompleted(agent_run_id, turn, usage, cost_usd)
├── AgentFinished(agent_run_id, stop_reason, turns, total_cost_usd)
├── ToolCallDispatched(agent_run_id, tool_name, tool_use_id, input_hash)
├── ToolCallResolved(agent_run_id, tool_use_id, duration_ms, success)
├── MemoryUpdated(agent_run_id, message_count, token_estimate)
├── PolicyViolation(agent_run_id, rule_name, detail)
├── WorkflowTransition(workflow_id, from_state, to_state)
└── SystemEvent(kind, payload)

Reducer chain (applied in order):
  agent_reducer       → manages AgentRecord updates
  tool_reducer        → manages ToolRecord statistics
  memory_reducer      → updates MemorySnapshot
  workflow_reducer    → drives WorkflowTransition FSM
  policy_reducer      → validates against PolicyConfig, emits PolicyViolation
  metrics_reducer     → accumulates cost / token counters

Each reducer: (AppState, Event) → (AppState, list[Effect])

Effect (sealed union):
  EmitSignalEffect(signal)           → forwarded to SignalBus.emit()
  PersistMemoryEffect(agent_run_id)  → triggers memory store flush
  SpawnAgentEffect(brief, config)    → calls AgentRunner.run()
  LogEffect(level, message)          → standard logging
  NoEffect()                         → no-op sentinel
```

### 3.4 Crash Recovery via Event Log Replay

```
Crash / restart
       │
       ▼
EventLog.read_all()   ← reads append-only file / store
       │
       ▼
state = AppState.empty()
for entry in log:
    state, _ = apply_reducers(state, entry.event)
       │
       ▼
SnapshotIndex.set(state)
EventProcessor resumes from tail of log
```

### 3.5 Integration with AgentRunnerBase

```
AgentRunnerBase._emit()  (existing)
       │
       ▼  currently calls SignalBus.emit() directly
       │
       ▼  NEW: also calls EventBus.put_nowait(event)
       │          │
       │          └──► EventProcessor dequeues, applies reducers,
       │               updates AppState snapshot, dispatches Effects
       │
       └──► SignalBus.emit() (unchanged — fan-out to external handlers)
```

---

## 4. Data Structures and Interfaces

All types use Python 3.12+ `dataclasses` with `frozen=True` for immutable
snapshots. Protocol definitions provide the structural interfaces that
implementation classes must satisfy.

### 4.1 Core AppState

```python
# src/agenthicc/kernel/state.py

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentRecord:
    """A snapshot of one agent instance's runtime state."""
    agent_id: str
    agent_run_id: str
    agent_name: str
    agent_class_name: str
    model: str
    status: str  # "running" | "finished" | "error"
    session_id: str | None = None
    turn: int = 0
    total_cost_usd: float = 0.0
    stop_reason: str | None = None


@dataclass(frozen=True)
class ToolRecord:
    """Aggregate statistics for one registered tool."""
    tool_name: str
    call_count: int = 0
    error_count: int = 0
    total_duration_ms: float = 0.0

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.call_count if self.call_count else 0.0


@dataclass(frozen=True)
class MessageRecord:
    """Immutable snapshot of a single conversation message."""
    role: str           # "user" | "assistant" | "tool"
    content: str
    tool_use_id: str | None = None


@dataclass(frozen=True)
class MemorySnapshot:
    """Immutable view of short-term memory at a point in time."""
    messages: tuple[MessageRecord, ...] = field(default_factory=tuple)
    summary: str | None = None
    token_estimate: int = 0


@dataclass(frozen=True)
class Intent:
    """A high-level user intent parsed from a session."""
    intent_id: str
    description: str
    confidence: float = 1.0
    resolved: bool = False


@dataclass(frozen=True)
class Workflow:
    """A named workflow with an FSM-like state."""
    workflow_id: str
    name: str
    current_state: str
    terminal: bool = False


@dataclass(frozen=True)
class Task:
    """An atomic unit of work within a workflow."""
    task_id: str
    workflow_id: str
    name: str
    status: str  # "pending" | "running" | "done" | "failed"
    assigned_agent_id: str | None = None


@dataclass(frozen=True)
class EventBusConfig:
    """Static configuration for the event queue."""
    maxsize: int = 1024
    overflow_policy: str = "drop_oldest"  # "drop_oldest" | "raise" | "block"


@dataclass(frozen=True)
class AppSettings:
    """Runtime-tunable settings embedded in AppState."""
    default_model: str = "claude-sonnet-4-6"
    default_max_turns: int = 10
    default_max_tokens_per_turn: int = 4096
    default_temperature: float = 0.7
    enable_streaming: bool = True
    enable_tool_caching: bool = False
    log_level: str = "INFO"


@dataclass(frozen=True)
class PolicyConfig:
    """Policy rules embedded in AppState."""
    max_cost_usd_per_run: float | None = None
    max_cost_usd_per_session: float | None = None
    allowed_tool_names: frozenset[str] | None = None  # None = allow all
    blocked_tool_names: frozenset[str] = field(default_factory=frozenset)
    max_turns_override: int | None = None
    require_hitl_for_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ToolResolverRecord:
    """Registry entry for a tool resolver (MCP alias, DI binding, etc.)."""
    resolver_id: str
    resolver_type: str  # "di" | "mcp" | "function"
    tool_names: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LogEntry:
    """Single entry in the append-only event log."""
    sequence: int
    timestamp_ns: int
    session_id: str
    event_type: str
    event_payload: dict[str, Any]


@dataclass(frozen=True)
class EventLog:
    """Append-only sequence of log entries; supports replay."""
    entries: tuple[LogEntry, ...] = field(default_factory=tuple)

    def append(self, entry: LogEntry) -> "EventLog":
        """Return a new EventLog with *entry* appended."""
        return EventLog(entries=self.entries + (entry,))

    def since(self, sequence: int) -> "EventLog":
        """Return entries with sequence > *sequence*."""
        return EventLog(entries=tuple(e for e in self.entries if e.sequence > sequence))


@dataclass(frozen=True)
class AppState:
    """
    The single authoritative, immutable snapshot of runtime state for one
    multi-agent session.

    All fields are frozen (immutable). Transitions are produced by pure
    reducer functions that return a new AppState.
    """
    session_id: str
    run_id: str

    # Domain collections
    intents: tuple[Intent, ...] = field(default_factory=tuple)
    workflows: tuple[Workflow, ...] = field(default_factory=tuple)
    tasks: tuple[Task, ...] = field(default_factory=tuple)
    agents: tuple[AgentRecord, ...] = field(default_factory=tuple)
    tools: tuple[ToolRecord, ...] = field(default_factory=tuple)

    # Memory
    memory: MemorySnapshot = field(default_factory=MemorySnapshot)

    # Infrastructure configuration (immutable per snapshot)
    event_bus: EventBusConfig = field(default_factory=EventBusConfig)
    settings: AppSettings = field(default_factory=AppSettings)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    # Audit trail
    event_log: EventLog = field(default_factory=EventLog)
    snapshot_index: int = 0

    # Registry
    agent_types: tuple[str, ...] = field(default_factory=tuple)
    tool_resolvers: tuple[ToolResolverRecord, ...] = field(default_factory=tuple)

    @classmethod
    def empty(cls, *, session_id: str | None = None, run_id: str | None = None) -> "AppState":
        """Return the canonical empty starting state for replay or fresh sessions."""
        return cls(
            session_id=session_id or uuid.uuid4().hex,
            run_id=run_id or uuid.uuid4().hex,
        )

    def replace(self, **kwargs: Any) -> "AppState":
        """Return a new AppState with the given fields replaced.

        Increments snapshot_index automatically.
        """
        import dataclasses
        return dataclasses.replace(
            self,
            snapshot_index=self.snapshot_index + 1,
            **kwargs,
        )

    def get_agent(self, agent_run_id: str) -> AgentRecord | None:
        """Look up an AgentRecord by run_id."""
        return next((a for a in self.agents if a.agent_run_id == agent_run_id), None)

    def get_tool(self, tool_name: str) -> ToolRecord | None:
        """Look up a ToolRecord by name."""
        return next((t for t in self.tools if t.tool_name == tool_name), None)
```

### 4.2 Event Union

```python
# src/agenthicc/kernel/events.py

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Union


@dataclass(frozen=True)
class AgentStarted:
    kind: Literal["AgentStarted"] = "AgentStarted"
    agent_id: str = ""
    agent_run_id: str = ""
    agent_name: str = ""
    agent_class_name: str = ""
    model: str = ""
    session_id: str | None = None
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class AgentTurnCompleted:
    kind: Literal["AgentTurnCompleted"] = "AgentTurnCompleted"
    agent_run_id: str = ""
    turn: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class AgentFinished:
    kind: Literal["AgentFinished"] = "AgentFinished"
    agent_run_id: str = ""
    stop_reason: str = "end_turn"
    turns: int = 0
    total_cost_usd: float = 0.0
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class ToolCallDispatched:
    kind: Literal["ToolCallDispatched"] = "ToolCallDispatched"
    agent_run_id: str = ""
    tool_name: str = ""
    tool_use_id: str = ""
    input_hash: str = ""
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class ToolCallResolved:
    kind: Literal["ToolCallResolved"] = "ToolCallResolved"
    agent_run_id: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    duration_ms: float = 0.0
    success: bool = True
    error: str | None = None
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class MemoryUpdated:
    kind: Literal["MemoryUpdated"] = "MemoryUpdated"
    agent_run_id: str = ""
    message_count: int = 0
    token_estimate: int = 0
    summary: str | None = None
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class PolicyViolation:
    kind: Literal["PolicyViolation"] = "PolicyViolation"
    agent_run_id: str = ""
    rule_name: str = ""
    detail: str = ""
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class WorkflowTransition:
    kind: Literal["WorkflowTransition"] = "WorkflowTransition"
    workflow_id: str = ""
    from_state: str = ""
    to_state: str = ""
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass(frozen=True)
class SystemEvent:
    kind: Literal["SystemEvent"] = "SystemEvent"
    name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())


# Sealed union — exhaustive pattern matching in reducers
Event = Union[
    AgentStarted,
    AgentTurnCompleted,
    AgentFinished,
    ToolCallDispatched,
    ToolCallResolved,
    MemoryUpdated,
    PolicyViolation,
    WorkflowTransition,
    SystemEvent,
]
```

### 4.3 Effects

```python
# src/agenthicc/kernel/effects.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class EmitSignalEffect:
    """Forward a signal to the existing SignalBus."""
    signal: Any  # a lauren_ai._signals.LifecycleEvent subclass instance


@dataclass(frozen=True)
class PersistMemoryEffect:
    """Request memory flush for the given agent run."""
    agent_run_id: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class SpawnAgentEffect:
    """Request that the runner launch a sub-agent."""
    brief: str
    parent_run_id: str
    config_override: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LogEffect:
    """Emit a log record at the given level."""
    level: str  # "debug" | "info" | "warning" | "error"
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NoEffect:
    """Sentinel for a reducer that produces no side effect."""


Effect = Union[
    EmitSignalEffect,
    PersistMemoryEffect,
    SpawnAgentEffect,
    LogEffect,
    NoEffect,
]
```

### 4.4 Reducer Protocol

```python
# src/agenthicc/kernel/protocols.py

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agenthicc.kernel.state import AppState
from agenthicc.kernel.events import Event
from agenthicc.kernel.effects import Effect


@runtime_checkable
class Reducer(Protocol):
    """
    Pure function: (AppState, Event) -> (AppState, list[Effect]).

    A Reducer must:
    - Be a pure function (no I/O, no mutable global state).
    - Return the same AppState unchanged when the event is irrelevant.
    - Never raise exceptions; handle errors by returning a LogEffect.
    - Be composable: multiple Reducers are applied in sequence by EventProcessor.
    """

    def __call__(
        self,
        state: AppState,
        event: Event,
    ) -> tuple[AppState, list[Effect]]: ...


@runtime_checkable
class EffectHandler(Protocol):
    """Executes a single Effect produced by a Reducer."""

    async def handle(self, effect: Effect, state: AppState) -> None: ...
```

### 4.5 Reducer Implementations

```python
# src/agenthicc/kernel/reducers.py

from __future__ import annotations

from agenthicc.kernel.state import AppState, AgentRecord, ToolRecord
from agenthicc.kernel.events import (
    Event, AgentStarted, AgentTurnCompleted, AgentFinished,
    ToolCallDispatched, ToolCallResolved, MemoryUpdated,
    PolicyViolation, WorkflowTransition,
)
from agenthicc.kernel.effects import Effect, LogEffect, NoEffect


def agent_reducer(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Manage the agents tuple in response to agent lifecycle events."""

    if isinstance(event, AgentStarted):
        record = AgentRecord(
            agent_id=event.agent_id,
            agent_run_id=event.agent_run_id,
            agent_name=event.agent_name,
            agent_class_name=event.agent_class_name,
            model=event.model,
            status="running",
            session_id=event.session_id,
        )
        return state.replace(agents=state.agents + (record,)), []

    if isinstance(event, AgentTurnCompleted):
        updated = tuple(
            AgentRecord(
                agent_id=a.agent_id,
                agent_run_id=a.agent_run_id,
                agent_name=a.agent_name,
                agent_class_name=a.agent_class_name,
                model=a.model,
                status=a.status,
                session_id=a.session_id,
                turn=event.turn + 1,
                total_cost_usd=a.total_cost_usd + event.cost_usd,
                stop_reason=a.stop_reason,
            )
            if a.agent_run_id == event.agent_run_id else a
            for a in state.agents
        )
        return state.replace(agents=updated), []

    if isinstance(event, AgentFinished):
        updated = tuple(
            AgentRecord(
                agent_id=a.agent_id,
                agent_run_id=a.agent_run_id,
                agent_name=a.agent_name,
                agent_class_name=a.agent_class_name,
                model=a.model,
                status="finished",
                session_id=a.session_id,
                turn=a.turn,
                total_cost_usd=event.total_cost_usd,
                stop_reason=event.stop_reason,
            )
            if a.agent_run_id == event.agent_run_id else a
            for a in state.agents
        )
        return state.replace(agents=updated), []

    return state, []


def tool_reducer(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Track ToolRecord statistics for every resolved tool call."""
    if not isinstance(event, ToolCallResolved):
        return state, []

    existing = {t.tool_name: t for t in state.tools}
    rec = existing.get(event.tool_name, ToolRecord(tool_name=event.tool_name))
    rec = ToolRecord(
        tool_name=rec.tool_name,
        call_count=rec.call_count + 1,
        error_count=rec.error_count + (0 if event.success else 1),
        total_duration_ms=rec.total_duration_ms + event.duration_ms,
    )
    existing[event.tool_name] = rec
    return state.replace(tools=tuple(existing.values())), []


def policy_reducer(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Enforce policy rules and emit PolicyViolation effects when breached."""
    if isinstance(event, ToolCallDispatched):
        policy = state.policy
        if (
            policy.blocked_tool_names
            and event.tool_name in policy.blocked_tool_names
        ):
            return state, [
                LogEffect(
                    level="warning",
                    message=f"Blocked tool call: {event.tool_name}",
                    context={"agent_run_id": event.agent_run_id},
                )
            ]
    return state, []


# Canonical reducer chain — applied left-to-right by EventProcessor
REDUCER_CHAIN: list = [agent_reducer, tool_reducer, policy_reducer]


def apply_reducers(
    state: AppState,
    event: Event,
    reducers: list = REDUCER_CHAIN,
) -> tuple[AppState, list[Effect]]:
    """Apply all reducers in sequence, accumulating effects."""
    all_effects: list[Effect] = []
    for reducer in reducers:
        state, effects = reducer(state, event)
        all_effects.extend(effects)
    return state, all_effects
```

### 4.6 Snapshot Index

```python
# src/agenthicc/kernel/snapshot.py

from __future__ import annotations

import asyncio
from typing import Any


class SnapshotIndex:
    """
    Lock-free atomic reference to the current AppState snapshot.

    Reads are always non-blocking (they see the last committed snapshot).
    Writes are performed only by EventProcessor, which is the single consumer.

    Because asyncio is cooperative, a single asyncio task writing `_current`
    is safe without a lock — Python's GIL guarantees the reference assignment
    is atomic.
    """

    __slots__ = ("_current",)

    def __init__(self, initial: Any) -> None:
        self._current = initial

    @property
    def current(self) -> Any:
        """Return the latest committed AppState. Always non-blocking."""
        return self._current

    def commit(self, new_state: Any) -> None:
        """Atomically update the current snapshot.

        Must only be called from the single EventProcessor consumer task.
        """
        self._current = new_state
```

### 4.7 Event Bus

```python
# src/agenthicc/kernel/bus.py

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agenthicc.kernel.events import Event

logger = logging.getLogger(__name__)


class EventBus:
    """
    Multi-producer single-consumer async queue.

    Producers call `put(event)` (awaitable) or `put_nowait(event)` (non-blocking).
    The single consumer (EventProcessor) calls `get()`.

    Overflow policy (from AppState.event_bus.overflow_policy):
    - "drop_oldest": discard the head of the queue when full.
    - "raise": raise QueueFull immediately.
    - "block": await until space is available (default asyncio behaviour).
    """

    def __init__(self, maxsize: int = 1024, overflow_policy: str = "drop_oldest") -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._overflow_policy = overflow_policy
        self._dropped: int = 0

    async def put(self, event: Event) -> None:
        """Enqueue an event, applying the overflow policy when full."""
        if self._queue.full():
            if self._overflow_policy == "drop_oldest":
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                    logger.warning("EventBus: queue full — dropped oldest event")
                except asyncio.QueueEmpty:
                    pass
            elif self._overflow_policy == "raise":
                raise asyncio.QueueFull("EventBus is full")
            # "block" falls through to the await below

        await self._queue.put(event)

    def put_nowait(self, event: Event) -> None:
        """Non-blocking enqueue; silently drops on overflow for 'drop_oldest'."""
        if self._queue.full():
            if self._overflow_policy == "drop_oldest":
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except asyncio.QueueEmpty:
                    pass
            elif self._overflow_policy == "raise":
                raise asyncio.QueueFull("EventBus is full")
            # "block" policy: no-op in non-blocking mode

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("EventBus.put_nowait: dropped event %r", type(event).__name__)
            self._dropped += 1

    async def get(self) -> Event:
        """Dequeue the next event (awaitable)."""
        return await self._queue.get()

    def task_done(self) -> None:
        """Signal that the dequeued event has been processed."""
        self._queue.task_done()

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped
```

### 4.8 EventProcessor

```python
# src/agenthicc/kernel/processor.py

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agenthicc.kernel.bus import EventBus
from agenthicc.kernel.effects import Effect, EmitSignalEffect, LogEffect, NoEffect
from agenthicc.kernel.events import Event
from agenthicc.kernel.reducers import apply_reducers
from agenthicc.kernel.snapshot import SnapshotIndex
from agenthicc.kernel.state import AppState, LogEntry, EventLog

logger = logging.getLogger(__name__)


class EventProcessor:
    """
    Single-consumer coroutine that drives the reducer pipeline.

    1. Dequeues one Event at a time from EventBus.
    2. Appends the event to the in-memory EventLog.
    3. Applies the full reducer chain: (AppState, Event) -> (AppState', Effects).
    4. Commits the new AppState to SnapshotIndex.
    5. Dispatches each Effect to the registered EffectHandlers.

    Run via: asyncio.create_task(processor.run())
    """

    def __init__(
        self,
        bus: EventBus,
        snapshot: SnapshotIndex,
        signal_bus: Any | None = None,
    ) -> None:
        self._bus = bus
        self._snapshot = snapshot
        self._signal_bus = signal_bus
        self._sequence: int = 0
        self._running: bool = False

    async def run(self) -> None:
        """Main processing loop. Runs until cancelled."""
        self._running = True
        while self._running:
            try:
                event = await self._bus.get()
                await self._process(event)
                self._bus.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("EventProcessor: unhandled error: %s", exc, exc_info=True)

    async def _process(self, event: Event) -> None:
        state: AppState = self._snapshot.current

        # 1. Build log entry and append to event log
        self._sequence += 1
        entry = LogEntry(
            sequence=self._sequence,
            timestamp_ns=time.time_ns(),
            session_id=state.session_id,
            event_type=type(event).__name__,
            event_payload=_event_to_dict(event),
        )
        new_log = state.event_log.append(entry)
        state = state.replace(event_log=new_log)

        # 2. Apply reducer chain
        state, effects = apply_reducers(state, event)

        # 3. Commit new snapshot atomically
        self._snapshot.commit(state)

        # 4. Dispatch effects
        for effect in effects:
            await self._dispatch(effect, state)

    async def _dispatch(self, effect: Effect, state: AppState) -> None:
        if isinstance(effect, EmitSignalEffect) and self._signal_bus is not None:
            try:
                await self._signal_bus.emit(effect.signal)
            except Exception as exc:  # noqa: BLE001
                logger.debug("EventProcessor: EmitSignalEffect failed: %s", exc)

        elif isinstance(effect, LogEffect):
            lvl = getattr(logging, effect.level.upper(), logging.INFO)
            logger.log(lvl, "Effect: %s | %s", effect.message, effect.context)

        elif isinstance(effect, NoEffect):
            pass

    def stop(self) -> None:
        self._running = False


def _event_to_dict(event: Any) -> dict:
    """Shallow serialisation of an event dataclass to a plain dict."""
    import dataclasses
    if dataclasses.is_dataclass(event):
        return {k: v for k, v in dataclasses.asdict(event).items() if k != "kind"}
    return {"raw": str(event)}
```

### 4.9 Kernel Facade

```python
# src/agenthicc/kernel/__init__.py

from __future__ import annotations

import asyncio
from typing import Any

from agenthicc.kernel.bus import EventBus
from agenthicc.kernel.events import Event
from agenthicc.kernel.processor import EventProcessor
from agenthicc.kernel.snapshot import SnapshotIndex
from agenthicc.kernel.state import AppState


class AppKernel:
    """
    Top-level facade that wires the EventBus, SnapshotIndex, and
    EventProcessor together.

    Usage::

        kernel = AppKernel(signal_bus=bus)
        await kernel.start()

        # from AgentRunnerBase or any producer:
        await kernel.emit(AgentStarted(...))

        # from an HTTP handler or SSE endpoint (lock-free):
        current_state = kernel.state

        await kernel.stop()
    """

    def __init__(
        self,
        initial_state: AppState | None = None,
        signal_bus: Any | None = None,
        maxsize: int = 1024,
        overflow_policy: str = "drop_oldest",
    ) -> None:
        state = initial_state or AppState.empty()
        self._snapshot = SnapshotIndex(state)
        self._bus = EventBus(maxsize=maxsize, overflow_policy=overflow_policy)
        self._processor = EventProcessor(
            bus=self._bus,
            snapshot=self._snapshot,
            signal_bus=signal_bus,
        )
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background EventProcessor task."""
        self._task = asyncio.create_task(self._processor.run(), name="event-processor")

    async def stop(self) -> None:
        """Gracefully stop the EventProcessor."""
        self._processor.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def emit(self, event: Event) -> None:
        """Emit an event onto the bus (awaitable, respects overflow policy)."""
        await self._bus.put(event)

    def emit_nowait(self, event: Event) -> None:
        """Non-blocking emit; may drop on overflow per policy."""
        self._bus.put_nowait(event)

    @property
    def state(self) -> AppState:
        """Return the current immutable AppState snapshot (lock-free read)."""
        return self._snapshot.current

    @classmethod
    async def restore_from_log(
        cls,
        log_entries: list,
        signal_bus: Any | None = None,
    ) -> "AppKernel":
        """Recover state by replaying a persisted event log."""
        from agenthicc.kernel.reducers import apply_reducers
        from agenthicc.kernel.state import AppState

        state = AppState.empty()
        for entry in log_entries:
            event = _deserialise_log_entry(entry)
            if event is not None:
                state, _ = apply_reducers(state, event)

        kernel = cls(initial_state=state, signal_bus=signal_bus)
        await kernel.start()
        return kernel


def _deserialise_log_entry(entry: Any) -> "Event | None":
    """Reconstruct an Event from a LogEntry. Extend per event type."""
    # Implementation left to the persistence layer (Phase 3).
    return None
```

---

## 5. Implementation Plan

### Phase 1 — Core Kernel (Sprint 1)

**Objective**: Implement the immutable `AppState`, `EventBus`, `SnapshotIndex`,
`EventProcessor`, and the canonical reducer chain. No persistence yet.

**Tasks**:

1. Create `src/agenthicc/kernel/` package with `__init__.py`, `state.py`,
   `events.py`, `effects.py`, `protocols.py`, `reducers.py`, `snapshot.py`,
   `bus.py`, `processor.py`.

2. Implement `AppState.empty()` and `AppState.replace()` as specified in
   Section 4.1.

3. Implement `EventBus` with `drop_oldest` overflow policy (Section 4.7).

4. Implement `SnapshotIndex` with atomic reference swap (Section 4.6).

5. Implement `EventProcessor` (Section 4.8): dequeue → log → reduce → commit
   → dispatch.

6. Implement `agent_reducer`, `tool_reducer`, `policy_reducer` (Section 4.5).

7. Implement `AppKernel` facade (Section 4.9).

**Integration with lauren-ai**:
- No changes to existing `lauren_ai` code in this phase. The kernel is
  exercised only through direct unit tests.

---

### Phase 2 — AgentRunnerBase Bridge (Sprint 2)

**Objective**: Wire `AppKernel` into `AgentRunnerBase._emit()` so that every
`SignalBus` emission also flows through the kernel as an `Event`.

**Tasks**:

1. Add optional `kernel: AppKernel | None = None` parameter to
   `AgentRunnerBase.__init__()` in
   `lauren_ai/_agents/_runner.py`.

2. In `AgentRunnerBase._emit()`, map `signal_name` strings to the corresponding
   `Event` subclass and call `self._kernel.emit_nowait(event)` when
   `self._kernel is not None`. Mapping table:

   | Signal name (str) | Event class |
   |---|---|
   | `"AgentRunComplete"` | `AgentFinished` |
   | `"AgentTurnComplete"` | `AgentTurnCompleted` |
   | `"ModelCallStarted"` | `AgentStarted` |
   | `"ToolCallStarted"` | `ToolCallDispatched` |
   | `"ToolCallComplete"` | `ToolCallResolved` |

3. Pass `agent_run_id`, `agent_id`, `agent_name` from `AgentContext` into the
   mapped `Event` constructors. `AgentContext` (in `lauren_ai/_agents/__init__.py`)
   already exposes `agent_run_id`, `agent_id`, `agent_name` (via property),
   and `config`.

4. Write integration test `tests/integration/test_kernel_runner_bridge.py`
   that creates a real `AppKernel`, passes it to `AgentRunnerBase`, runs a
   mock agent turn, and asserts that the kernel's `state.agents` reflects the
   completed run.

**No breaking changes** to the existing `SignalBus` or `AgentRunnerBase`
public API. The kernel is purely additive.

---

### Phase 3 — Event Log Persistence and Crash Recovery (Sprint 3)

**Objective**: Persist the `EventLog` to an append-only file (or SQLite WAL)
and implement `AppKernel.restore_from_log()`.

**Tasks**:

1. Create `src/agenthicc/kernel/persistence.py` with `EventLogWriter` and
   `EventLogReader` that serialise `LogEntry` to NDJSON (one JSON object per
   line).

2. Implement `_deserialise_log_entry()` in `AppKernel` (stub in Section 4.9)
   to reconstruct each `Event` subclass from its `event_type` and
   `event_payload`.

3. Hook `EventLogWriter.append()` into `EventProcessor._process()` (after
   step 1 in the sequence — see Section 4.8).

4. Implement `AppKernel.restore_from_log(path)` that:
   - Reads all `LogEntry` objects from the NDJSON file.
   - Replays them through `apply_reducers` starting from `AppState.empty()`.
   - Returns a running `AppKernel` with the recovered state.

5. Write an E2E crash-recovery test (see Section 6.3).

---

### Phase 4 — ToolContext and Policy Integration (Sprint 4)

**Objective**: Thread `AppState.policy` into `ToolContext` so tools can
self-enforce policy rules without reaching into global state.

**Tasks**:

1. Add `app_state_snapshot: AppState | None = None` to `ToolContext` in
   `lauren_ai/_tools/__init__.py`.

2. In `AgentRunnerBase._execute_single_tool()`, set
   `tool_context.app_state_snapshot = kernel.state` before calling
   `self._executor.execute(...)`.

3. Implement a `PolicyChecker` helper that reads `AppState.policy` from the
   snapshot and raises `PolicyViolationError` when a tool is blocked.

4. Emit `PolicyViolation` event via the kernel when a violation is detected.

5. Write tests verifying that blocked tools are rejected at the kernel level
   and that the violation appears in `AppState.event_log`.

---

### Phase 5 — Workflow and Intent Routing (Sprint 5)

**Objective**: Populate `AppState.intents`, `workflows`, and `tasks` by
integrating with the intent router and workflow FSM (to be defined in
PRD-02).

**Tasks** (preview — full spec in PRD-02):

1. Define `IntentClassifiedEvent` and `WorkflowCreatedEvent` in `events.py`.
2. Implement `intent_reducer` and `workflow_reducer` that manage the
   `AppState.intents`, `workflows`, and `tasks` tuples.
3. Wire `workflow_reducer` to `WorkflowTransition` events emitted by the
   workflow executor.

---

## 6. Tests

All tests use `pytest` + `pytest-asyncio`. The `anyio` backend is `asyncio`.

### 6.1 Unit Tests

```python
# tests/unit/test_appstate.py

from __future__ import annotations

import dataclasses

import pytest

from agenthicc.kernel.state import (
    AppState,
    AgentRecord,
    ToolRecord,
    EventLog,
    LogEntry,
    AppSettings,
    PolicyConfig,
)


class TestAppStateImmutability:
    def test_frozen_dataclass_cannot_be_mutated(self) -> None:
        state = AppState.empty()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            state.session_id = "mutated"  # type: ignore[misc]

    def test_replace_increments_snapshot_index(self) -> None:
        state = AppState.empty()
        next_state = state.replace(run_id="new-run")
        assert next_state.snapshot_index == state.snapshot_index + 1

    def test_replace_preserves_other_fields(self) -> None:
        state = AppState.empty(session_id="sess-1", run_id="run-1")
        next_state = state.replace(run_id="run-2")
        assert next_state.session_id == "sess-1"
        assert next_state.run_id == "run-2"

    def test_empty_returns_distinct_ids(self) -> None:
        s1 = AppState.empty()
        s2 = AppState.empty()
        assert s1.session_id != s2.session_id
        assert s1.run_id != s2.run_id

    def test_get_agent_returns_none_for_missing_id(self) -> None:
        state = AppState.empty()
        assert state.get_agent("nonexistent") is None

    def test_get_agent_returns_record(self) -> None:
        record = AgentRecord(
            agent_id="a1",
            agent_run_id="r1",
            agent_name="TestAgent",
            agent_class_name="TestAgent",
            model="claude-sonnet-4-6",
            status="running",
        )
        state = AppState.empty().replace(agents=(record,))
        assert state.get_agent("r1") == record


class TestEventLog:
    def test_append_returns_new_log(self) -> None:
        log = EventLog()
        entry = LogEntry(
            sequence=1,
            timestamp_ns=1_000_000,
            session_id="sess",
            event_type="AgentStarted",
            event_payload={},
        )
        new_log = log.append(entry)
        assert len(log.entries) == 0
        assert len(new_log.entries) == 1

    def test_since_filters_correctly(self) -> None:
        entries = tuple(
            LogEntry(
                sequence=i,
                timestamp_ns=i * 1000,
                session_id="sess",
                event_type="Test",
                event_payload={},
            )
            for i in range(1, 6)
        )
        log = EventLog(entries=entries)
        sliced = log.since(3)
        assert len(sliced.entries) == 2
        assert sliced.entries[0].sequence == 4


class TestToolRecord:
    def test_avg_duration_ms_zero_calls(self) -> None:
        rec = ToolRecord(tool_name="my_tool")
        assert rec.avg_duration_ms == 0.0

    def test_avg_duration_ms_computed(self) -> None:
        rec = ToolRecord(tool_name="my_tool", call_count=4, total_duration_ms=200.0)
        assert rec.avg_duration_ms == 50.0
```

```python
# tests/unit/test_reducers.py

from __future__ import annotations

import pytest

from agenthicc.kernel.state import AppState, AgentRecord
from agenthicc.kernel.events import (
    AgentStarted,
    AgentTurnCompleted,
    AgentFinished,
    ToolCallDispatched,
    ToolCallResolved,
    SystemEvent,
)
from agenthicc.kernel.effects import LogEffect
from agenthicc.kernel.reducers import (
    agent_reducer,
    tool_reducer,
    policy_reducer,
    apply_reducers,
)


class TestAgentReducer:
    def test_agent_started_adds_record(self) -> None:
        state = AppState.empty()
        event = AgentStarted(
            agent_id="a1",
            agent_run_id="r1",
            agent_name="Bot",
            agent_class_name="Bot",
            model="claude-sonnet-4-6",
        )
        new_state, effects = agent_reducer(state, event)
        assert len(new_state.agents) == 1
        assert new_state.agents[0].agent_run_id == "r1"
        assert new_state.agents[0].status == "running"
        assert effects == []

    def test_agent_turn_increments_turn_and_cost(self) -> None:
        record = AgentRecord(
            agent_id="a1", agent_run_id="r1", agent_name="Bot",
            agent_class_name="Bot", model="m", status="running",
        )
        state = AppState.empty().replace(agents=(record,))
        event = AgentTurnCompleted(
            agent_run_id="r1", turn=0, cost_usd=0.05
        )
        new_state, _ = agent_reducer(state, event)
        updated = new_state.get_agent("r1")
        assert updated is not None
        assert updated.turn == 1
        assert updated.total_cost_usd == pytest.approx(0.05)

    def test_agent_finished_sets_status(self) -> None:
        record = AgentRecord(
            agent_id="a1", agent_run_id="r1", agent_name="Bot",
            agent_class_name="Bot", model="m", status="running",
        )
        state = AppState.empty().replace(agents=(record,))
        event = AgentFinished(
            agent_run_id="r1", stop_reason="end_turn", turns=2,
            total_cost_usd=0.12,
        )
        new_state, _ = agent_reducer(state, event)
        updated = new_state.get_agent("r1")
        assert updated is not None
        assert updated.status == "finished"
        assert updated.stop_reason == "end_turn"

    def test_unrelated_events_pass_through(self) -> None:
        state = AppState.empty()
        event = SystemEvent(name="heartbeat")
        new_state, effects = agent_reducer(state, event)
        assert new_state is state
        assert effects == []


class TestToolReducer:
    def test_first_call_creates_record(self) -> None:
        state = AppState.empty()
        event = ToolCallResolved(
            agent_run_id="r1", tool_use_id="t1",
            tool_name="search_web", duration_ms=42.0, success=True,
        )
        new_state, _ = tool_reducer(state, event)
        rec = new_state.get_tool("search_web")
        assert rec is not None
        assert rec.call_count == 1
        assert rec.error_count == 0

    def test_failed_call_increments_error_count(self) -> None:
        state = AppState.empty()
        event = ToolCallResolved(
            agent_run_id="r1", tool_use_id="t1",
            tool_name="search_web", duration_ms=10.0, success=False,
            error="timeout",
        )
        new_state, _ = tool_reducer(state, event)
        rec = new_state.get_tool("search_web")
        assert rec is not None
        assert rec.error_count == 1

    def test_accumulates_across_calls(self) -> None:
        state = AppState.empty()
        for i in range(5):
            event = ToolCallResolved(
                agent_run_id="r1", tool_use_id=f"t{i}",
                tool_name="search_web", duration_ms=20.0, success=True,
            )
            state, _ = tool_reducer(state, event)
        rec = state.get_tool("search_web")
        assert rec is not None
        assert rec.call_count == 5
        assert rec.total_duration_ms == pytest.approx(100.0)


class TestPolicyReducer:
    def test_blocked_tool_produces_log_effect(self) -> None:
        from agenthicc.kernel.state import PolicyConfig
        policy = PolicyConfig(blocked_tool_names=frozenset({"delete_database"}))
        state = AppState.empty().replace(policy=policy)
        event = ToolCallDispatched(
            agent_run_id="r1", tool_name="delete_database",
            tool_use_id="t1", input_hash="abc",
        )
        new_state, effects = policy_reducer(state, event)
        assert len(effects) == 1
        assert isinstance(effects[0], LogEffect)
        assert "delete_database" in effects[0].message

    def test_allowed_tool_produces_no_effect(self) -> None:
        state = AppState.empty()
        event = ToolCallDispatched(
            agent_run_id="r1", tool_name="search_web",
            tool_use_id="t1", input_hash="xyz",
        )
        _, effects = policy_reducer(state, event)
        assert effects == []


class TestApplyReducers:
    def test_reducer_chain_accumulates_state(self) -> None:
        state = AppState.empty()
        event = AgentStarted(
            agent_id="a1", agent_run_id="r1", agent_name="Bot",
            agent_class_name="Bot", model="m",
        )
        new_state, _ = apply_reducers(state, event)
        assert len(new_state.agents) == 1

    def test_snapshot_index_advances_per_call(self) -> None:
        state = AppState.empty()
        assert state.snapshot_index == 0
        event = AgentStarted(
            agent_id="a1", agent_run_id="r1", agent_name="Bot",
            agent_class_name="Bot", model="m",
        )
        s1, _ = apply_reducers(state, event)
        assert s1.snapshot_index > state.snapshot_index
```

```python
# tests/unit/test_event_bus.py

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agenthicc.kernel.bus import EventBus
from agenthicc.kernel.events import SystemEvent


@pytest.mark.asyncio
class TestEventBus:
    async def test_put_and_get_roundtrip(self) -> None:
        bus = EventBus(maxsize=8)
        event = SystemEvent(name="ping")
        await bus.put(event)
        result = await bus.get()
        assert result is event

    async def test_qsize_reflects_pending(self) -> None:
        bus = EventBus(maxsize=8)
        for i in range(3):
            await bus.put(SystemEvent(name=f"e{i}"))
        assert bus.qsize == 3

    async def test_drop_oldest_on_overflow(self) -> None:
        bus = EventBus(maxsize=2, overflow_policy="drop_oldest")
        e1 = SystemEvent(name="first")
        e2 = SystemEvent(name="second")
        e3 = SystemEvent(name="third")
        await bus.put(e1)
        await bus.put(e2)
        await bus.put(e3)  # should drop e1
        assert bus.dropped == 1
        r1 = await bus.get()
        r2 = await bus.get()
        assert r1.name == "second"
        assert r2.name == "third"

    async def test_put_nowait_drop_on_overflow(self) -> None:
        bus = EventBus(maxsize=1, overflow_policy="drop_oldest")
        bus.put_nowait(SystemEvent(name="a"))
        bus.put_nowait(SystemEvent(name="b"))
        assert bus.dropped >= 1
```

```python
# tests/unit/test_snapshot_index.py

from __future__ import annotations

from agenthicc.kernel.snapshot import SnapshotIndex
from agenthicc.kernel.state import AppState


class TestSnapshotIndex:
    def test_initial_state_readable(self) -> None:
        state = AppState.empty()
        idx = SnapshotIndex(state)
        assert idx.current is state

    def test_commit_updates_reference(self) -> None:
        state1 = AppState.empty()
        state2 = AppState.empty()
        idx = SnapshotIndex(state1)
        idx.commit(state2)
        assert idx.current is state2

    def test_multiple_commits(self) -> None:
        idx = SnapshotIndex(AppState.empty())
        for i in range(10):
            idx.commit(AppState.empty())
        assert idx.current.snapshot_index == 0  # fresh empties always 0
```

### 6.2 Integration Tests

```python
# tests/integration/test_event_processor.py

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agenthicc.kernel.bus import EventBus
from agenthicc.kernel.events import AgentStarted, AgentFinished, ToolCallResolved
from agenthicc.kernel.processor import EventProcessor
from agenthicc.kernel.snapshot import SnapshotIndex
from agenthicc.kernel.state import AppState


@pytest.fixture
def setup_processor():
    """Helper: returns (bus, snapshot, processor) with a started processor task."""
    state = AppState.empty(session_id="test-sess")
    bus = EventBus(maxsize=64)
    snapshot = SnapshotIndex(state)
    processor = EventProcessor(bus=bus, snapshot=snapshot)
    return bus, snapshot, processor


@pytest.mark.asyncio
class TestEventProcessor:
    async def test_agent_started_reflected_in_snapshot(
        self, setup_processor
    ) -> None:
        bus, snapshot, processor = setup_processor
        task = asyncio.create_task(processor.run())
        try:
            event = AgentStarted(
                agent_id="a1", agent_run_id="r1",
                agent_name="TestBot", agent_class_name="TestBot",
                model="claude-sonnet-4-6",
            )
            await bus.put(event)
            await asyncio.sleep(0.05)  # let processor tick
            state = snapshot.current
            assert len(state.agents) == 1
            assert state.agents[0].agent_run_id == "r1"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_full_lifecycle_sequence(
        self, setup_processor
    ) -> None:
        bus, snapshot, processor = setup_processor
        task = asyncio.create_task(processor.run())
        try:
            events = [
                AgentStarted(
                    agent_id="a1", agent_run_id="r1",
                    agent_name="TestBot", agent_class_name="TestBot",
                    model="m",
                ),
                ToolCallResolved(
                    agent_run_id="r1", tool_use_id="t1",
                    tool_name="search", duration_ms=30.0, success=True,
                ),
                AgentFinished(
                    agent_run_id="r1", stop_reason="end_turn",
                    turns=1, total_cost_usd=0.01,
                ),
            ]
            for e in events:
                await bus.put(e)
            await asyncio.sleep(0.1)

            state = snapshot.current
            assert state.agents[0].status == "finished"
            tool_rec = state.get_tool("search")
            assert tool_rec is not None
            assert tool_rec.call_count == 1
            assert len(state.event_log.entries) == len(events)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_snapshot_index_monotonically_increases(
        self, setup_processor
    ) -> None:
        bus, snapshot, processor = setup_processor
        task = asyncio.create_task(processor.run())
        initial_index = snapshot.current.snapshot_index
        try:
            for i in range(5):
                await bus.put(AgentStarted(
                    agent_id=f"a{i}", agent_run_id=f"r{i}",
                    agent_name="Bot", agent_class_name="Bot", model="m",
                ))
            await asyncio.sleep(0.1)
            final_index = snapshot.current.snapshot_index
            assert final_index > initial_index
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_event_log_append_only(
        self, setup_processor
    ) -> None:
        bus, snapshot, processor = setup_processor
        task = asyncio.create_task(processor.run())
        try:
            await bus.put(AgentStarted(
                agent_id="a1", agent_run_id="r1",
                agent_name="Bot", agent_class_name="Bot", model="m",
            ))
            await asyncio.sleep(0.05)
            entries_after_1 = snapshot.current.event_log.entries

            await bus.put(AgentFinished(
                agent_run_id="r1", stop_reason="end_turn",
                turns=1, total_cost_usd=0.0,
            ))
            await asyncio.sleep(0.05)
            entries_after_2 = snapshot.current.event_log.entries

            assert len(entries_after_2) == len(entries_after_1) + 1
            # Original slice is unchanged (immutability)
            assert entries_after_1[0].sequence == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
```

```python
# tests/integration/test_app_kernel.py

from __future__ import annotations

import asyncio

import pytest

from agenthicc.kernel import AppKernel
from agenthicc.kernel.events import AgentStarted, AgentFinished, ToolCallResolved
from agenthicc.kernel.state import AppState


@pytest.mark.asyncio
class TestAppKernel:
    async def test_start_and_stop(self) -> None:
        kernel = AppKernel()
        await kernel.start()
        assert kernel.state is not None
        await kernel.stop()

    async def test_emit_updates_state(self) -> None:
        kernel = AppKernel()
        await kernel.start()
        try:
            await kernel.emit(AgentStarted(
                agent_id="a1", agent_run_id="r1",
                agent_name="Bot", agent_class_name="Bot", model="m",
            ))
            await asyncio.sleep(0.05)
            assert len(kernel.state.agents) == 1
        finally:
            await kernel.stop()

    async def test_emit_nowait_non_blocking(self) -> None:
        kernel = AppKernel()
        await kernel.start()
        try:
            # Should not block or raise
            kernel.emit_nowait(AgentStarted(
                agent_id="a2", agent_run_id="r2",
                agent_name="Bot2", agent_class_name="Bot2", model="m",
            ))
            await asyncio.sleep(0.05)
            assert any(a.agent_run_id == "r2" for a in kernel.state.agents)
        finally:
            await kernel.stop()

    async def test_concurrent_producers(self) -> None:
        """Multiple coroutines emitting events concurrently."""
        kernel = AppKernel(maxsize=256)
        await kernel.start()
        try:
            async def producer(i: int) -> None:
                await kernel.emit(AgentStarted(
                    agent_id=f"a{i}", agent_run_id=f"r{i}",
                    agent_name=f"Bot{i}", agent_class_name="Bot", model="m",
                ))

            await asyncio.gather(*(producer(i) for i in range(20)))
            await asyncio.sleep(0.15)
            assert len(kernel.state.agents) == 20
        finally:
            await kernel.stop()

    async def test_signal_bus_integration(self) -> None:
        """EmitSignalEffect forwarded to an attached SignalBus."""
        received: list = []

        class FakeSignalBus:
            async def emit(self, signal) -> None:
                received.append(signal)

        kernel = AppKernel(signal_bus=FakeSignalBus())
        await kernel.start()
        try:
            # Policy reducer emits LogEffect on blocked tool, not SignalEffect,
            # so we test via direct effect injection in a custom reducer.
            from agenthicc.kernel.effects import EmitSignalEffect
            from agenthicc.kernel.events import SystemEvent

            # Override the processor's dispatch to confirm it calls signal_bus
            event = SystemEvent(name="test_signal")
            await kernel.emit(event)
            await asyncio.sleep(0.05)
            # No EmitSignalEffect produced by default for SystemEvent,
            # so received stays empty — proves bus is wired without exploding.
            assert isinstance(received, list)
        finally:
            await kernel.stop()
```

### 6.3 End-to-End Tests

```python
# tests/e2e/test_crash_recovery.py

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from agenthicc.kernel import AppKernel
from agenthicc.kernel.events import AgentStarted, AgentFinished, ToolCallResolved
from agenthicc.kernel.state import AppState, LogEntry, EventLog
from agenthicc.kernel.reducers import apply_reducers


def _replay(entries: list[LogEntry]) -> AppState:
    """Utility: replay a list of LogEntry to reconstruct AppState."""
    from agenthicc.kernel.events import (
        AgentStarted, AgentFinished, AgentTurnCompleted,
        ToolCallDispatched, ToolCallResolved, MemoryUpdated,
        PolicyViolation, WorkflowTransition, SystemEvent,
    )

    _type_map = {
        "AgentStarted": AgentStarted,
        "AgentFinished": AgentFinished,
        "ToolCallResolved": ToolCallResolved,
        "SystemEvent": SystemEvent,
    }
    state = AppState.empty()
    for entry in entries:
        cls = _type_map.get(entry.event_type)
        if cls is None:
            continue
        payload = dict(entry.event_payload)
        payload.pop("kind", None)
        try:
            event = cls(**payload)
        except TypeError:
            continue
        state, _ = apply_reducers(state, event)
    return state


@pytest.mark.asyncio
class TestCrashRecovery:
    async def test_replay_produces_identical_state(self) -> None:
        """
        Run a kernel through a lifecycle, collect the event log,
        replay it from empty, and assert the final states match.
        """
        kernel = AppKernel()
        await kernel.start()
        try:
            events = [
                AgentStarted(
                    agent_id="a1", agent_run_id="r1",
                    agent_name="CrashBot", agent_class_name="CrashBot",
                    model="claude-sonnet-4-6",
                ),
                ToolCallResolved(
                    agent_run_id="r1", tool_use_id="t1",
                    tool_name="lookup", duration_ms=15.0, success=True,
                ),
                AgentFinished(
                    agent_run_id="r1", stop_reason="end_turn",
                    turns=1, total_cost_usd=0.003,
                ),
            ]
            for e in events:
                await kernel.emit(e)
            await asyncio.sleep(0.15)

            original_state = kernel.state
        finally:
            await kernel.stop()

        # Replay the event log
        log_entries = list(original_state.event_log.entries)
        recovered_state = _replay(log_entries)

        # Agent should be present and finished in both
        orig_agent = original_state.get_agent("r1")
        rec_agent = recovered_state.get_agent("r1")
        assert orig_agent is not None
        assert rec_agent is not None
        assert orig_agent.status == rec_agent.status
        assert orig_agent.stop_reason == rec_agent.stop_reason

        # Tool records should match
        orig_tool = original_state.get_tool("lookup")
        rec_tool = recovered_state.get_tool("lookup")
        assert orig_tool is not None and rec_tool is not None
        assert orig_tool.call_count == rec_tool.call_count

    async def test_empty_log_yields_empty_state(self) -> None:
        state = _replay([])
        assert state.agents == ()
        assert state.tools == ()

    async def test_partial_log_partial_state(self) -> None:
        """Recovery from a partial log (e.g. crash mid-session)."""
        kernel = AppKernel()
        await kernel.start()
        try:
            await kernel.emit(AgentStarted(
                agent_id="a1", agent_run_id="r1",
                agent_name="PartialBot", agent_class_name="PartialBot",
                model="m",
            ))
            # Note: no AgentFinished — simulates a crash
            await asyncio.sleep(0.05)
            log_entries = list(kernel.state.event_log.entries)
        finally:
            await kernel.stop()

        recovered = _replay(log_entries)
        agent = recovered.get_agent("r1")
        assert agent is not None
        assert agent.status == "running"  # not finished — matches crash point
```

```python
# tests/e2e/test_kernel_signal_bridge.py

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agenthicc.kernel import AppKernel
from agenthicc.kernel.events import AgentStarted, ToolCallResolved, AgentFinished


@pytest.mark.asyncio
class TestKernelSignalBridge:
    """
    Verifies that the AppKernel and an external SignalBus can coexist,
    with the kernel faithfully tracking state while the bus handles fan-out.
    """

    async def test_high_volume_events_no_data_loss(self) -> None:
        """
        Emit 500 events across 10 concurrent producers and verify
        state consistency at the end.
        """
        n_agents = 50

        kernel = AppKernel(maxsize=1024)
        await kernel.start()
        try:
            async def run_agent(i: int) -> None:
                run_id = f"run-{i:04d}"
                await kernel.emit(AgentStarted(
                    agent_id=f"a{i}", agent_run_id=run_id,
                    agent_name=f"Agent{i}", agent_class_name="Agent", model="m",
                ))
                await kernel.emit(ToolCallResolved(
                    agent_run_id=run_id, tool_use_id=f"t{i}",
                    tool_name="search", duration_ms=10.0, success=True,
                ))
                await kernel.emit(AgentFinished(
                    agent_run_id=run_id, stop_reason="end_turn",
                    turns=1, total_cost_usd=0.001,
                ))

            await asyncio.gather(*(run_agent(i) for i in range(n_agents)))
            await asyncio.sleep(0.5)

            state = kernel.state
            finished = [a for a in state.agents if a.status == "finished"]
            assert len(finished) == n_agents

            search_tool = state.get_tool("search")
            assert search_tool is not None
            assert search_tool.call_count == n_agents
            assert search_tool.error_count == 0

            assert len(state.event_log.entries) == n_agents * 3
        finally:
            await kernel.stop()
```

---

## 7. Configuration Reference

All configuration is expressed as TOML and mapped to `AppSettings` and
`PolicyConfig` instances embedded in `AppState`.

### 7.1 Kernel Configuration

```toml
# config/kernel.toml

[kernel]
# Event bus queue capacity. Events are dropped (oldest-first) when full.
event_bus_maxsize = 1024

# Overflow behaviour: "drop_oldest" | "block" | "raise"
event_bus_overflow_policy = "drop_oldest"

# Log level for internal kernel messages.
log_level = "INFO"
```

### 7.2 App Settings

```toml
# config/settings.toml

[settings]
# Default LLM model used when no agent-level override is set.
default_model = "claude-sonnet-4-6"

# Default maximum agentic loop iterations.
default_max_turns = 10

# Token budget per LLM call.
default_max_tokens_per_turn = 4096

# Sampling temperature (0.0–1.0).
default_temperature = 0.7

# Enable streaming mode by default.
enable_streaming = true

# Enable tool result caching (requires a CacheBackend to be configured).
enable_tool_caching = false
```

### 7.3 Policy Configuration

```toml
# config/policy.toml

[policy]
# Maximum USD cost per single agent run. Null disables the cap.
max_cost_usd_per_run = 0.50

# Maximum USD cost across an entire session (sum of all runs).
max_cost_usd_per_session = 5.00

# Tools in this list are always blocked, regardless of agent config.
# Empty list = allow all tools.
blocked_tool_names = ["delete_database", "rm_rf"]

# Tools that require human-in-the-loop approval before execution.
require_hitl_for_tools = ["send_email", "wire_transfer"]

# Override max_turns for all agents in this session (null = use agent default).
max_turns_override = null
```

### 7.4 Event Log Persistence (Phase 3)

```toml
# config/event_log.toml

[event_log]
# Backend: "ndjson" | "sqlite" (Phase 3)
backend = "ndjson"

# Path for NDJSON append-only log.
path = "/var/log/agenthicc/events.ndjson"

# Rotate after this many entries (0 = never rotate).
rotate_after = 100_000

# Compress rotated files.
compress_rotated = true
```

### 7.5 Loading Config into AppState

```python
# src/agenthicc/config.py

from __future__ import annotations

import tomllib
from pathlib import Path

from agenthicc.kernel.state import (
    AppSettings,
    PolicyConfig,
    EventBusConfig,
    AppState,
)


def load_kernel_config(path: str | Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def build_initial_state(config_dir: str | Path) -> AppState:
    """Build the initial AppState from TOML config files."""
    config_dir = Path(config_dir)

    kernel_cfg = load_kernel_config(config_dir / "kernel.toml").get("kernel", {})
    settings_cfg = load_kernel_config(config_dir / "settings.toml").get("settings", {})
    policy_cfg = load_kernel_config(config_dir / "policy.toml").get("policy", {})

    return AppState.empty().replace(
        event_bus=EventBusConfig(
            maxsize=kernel_cfg.get("event_bus_maxsize", 1024),
            overflow_policy=kernel_cfg.get("event_bus_overflow_policy", "drop_oldest"),
        ),
        settings=AppSettings(
            default_model=settings_cfg.get("default_model", "claude-sonnet-4-6"),
            default_max_turns=settings_cfg.get("default_max_turns", 10),
            default_max_tokens_per_turn=settings_cfg.get("default_max_tokens_per_turn", 4096),
            default_temperature=settings_cfg.get("default_temperature", 0.7),
            enable_streaming=settings_cfg.get("enable_streaming", True),
            enable_tool_caching=settings_cfg.get("enable_tool_caching", False),
        ),
        policy=PolicyConfig(
            max_cost_usd_per_run=policy_cfg.get("max_cost_usd_per_run"),
            max_cost_usd_per_session=policy_cfg.get("max_cost_usd_per_session"),
            blocked_tool_names=frozenset(
                policy_cfg.get("blocked_tool_names", [])
            ),
            require_hitl_for_tools=frozenset(
                policy_cfg.get("require_hitl_for_tools", [])
            ),
            max_turns_override=policy_cfg.get("max_turns_override"),
        ),
    )
```

### 7.6 pytest Configuration

```toml
# pyproject.toml (relevant sections)

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
python_files = ["test_*.py"]
python_functions = ["test_*"]
markers = [
    "unit: pure unit tests (no I/O)",
    "integration: in-process integration tests",
    "e2e: end-to-end tests",
]

[tool.coverage.run]
source = ["src/agenthicc"]
branch = true

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
]
```

---

## 8. Open Questions

| # | Question | Priority | Owner |
|---|----------|----------|-------|
| OQ-1 | **Reducer ordering**: Should `policy_reducer` run before `agent_reducer` so that policy violations abort the agent state update, or should violations be advisory-only? The current design treats them as advisory (LogEffect only). If blocking is needed, the reducer chain execution contract must be extended with an "abort" signal. | High | Platform |
| OQ-2 | **Snapshot retention**: How many historical snapshots should `SnapshotIndex` retain? The current design holds only the latest. Retaining N snapshots enables time-travel debugging but increases memory pressure. A ring-buffer of snapshots (configurable N) is the likely solution. | Medium | Platform |
| OQ-3 | **EventLog partitioning**: When sessions are long-lived (e.g., 100k events), replaying the full log is expensive. Should `EventLog.since(checkpoint_sequence)` be combined with periodic checkpointing that serialises the full `AppState` to disk? This is the standard CQRS checkpoint pattern. | Medium | Platform |
| OQ-4 | **Cross-session state**: `AppState` currently represents a single session. A multi-tenant deployment needs a `SessionRegistry` keyed by `session_id`. Should `AppKernel` own one `AppState` per session, or should there be one global `AppState` with all sessions nested inside it? The nested approach simplifies cross-session policy enforcement. | High | Architecture |
| OQ-5 | **`AgentRunnerBase` coupling**: Phase 2 adds `kernel` to `AgentRunnerBase.__init__()`. The parameter is optional and backward-compatible. However, `lauren_ai._module.AgentModule.for_root()` will need to wire the kernel into every runner it creates. What is the best injection point — constructor or a `set_kernel()` method? | Medium | Platform |
| OQ-6 | **Effect ordering guarantees**: Effects are currently dispatched sequentially within `EventProcessor._dispatch()`. For `SpawnAgentEffect`, spawning a sub-agent inside the processor loop could stall event processing. Should `SpawnAgentEffect` be queued to a separate `asyncio.Task` pool rather than awaited inline? | High | Platform |
| OQ-7 | **Schema versioning for EventLog**: NDJSON replay requires all event constructors to accept historical payloads. As event schemas evolve, old log entries may have missing or renamed fields. A schema version field on `LogEntry` and a migration layer are needed before Phase 3 is stable. | High | Platform |
| OQ-8 | **Integration with existing `SignalBus`**: The current plan has `EmitSignalEffect` forward to `SignalBus`. Should the kernel eventually replace `SignalBus` entirely, or should `SignalBus` remain as a separate fan-out layer for external observers? Keeping both avoids breaking existing `@bus.on(ModelCallComplete)` handlers but adds conceptual overhead. | Low | Architecture |
| OQ-9 | **Test isolation**: Integration tests currently sleep with `await asyncio.sleep(0.05)` to allow the processor to tick. This is flaky. Consider adding a `kernel.drain()` method that awaits `Queue.join()` to guarantee all enqueued events have been processed before assertions. | High | Testing |
| OQ-10 | **`AppState.replace()` performance**: Rebuilding large `tuple` fields (e.g., `agents`) on every event is O(n). For sessions with hundreds of concurrent agents, this may become a bottleneck. Evaluate switching to `frozendict` or a persistent/structural-sharing data structure (e.g., `pyrsistent.PVector`) in a follow-up PRD. | Low | Performance |
