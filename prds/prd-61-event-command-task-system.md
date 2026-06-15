# PRD-61 — Event, Command & Task System

## 1. Purpose

Define the three orthogonal systems that carry intent from user interaction
through the runtime to state changes and side effects:

- **Events** — describe what *happened* (past, immutable)
- **Commands** — describe what *should happen* (intent, dispatchable)
- **Tasks** — long-running async operations with observable lifecycle

Together they replace all imperative wiring (`_run_agent`, `_sigint_cancel`,
`on_intent_submitted`, ad-hoc callback chains) with a typed, replayable,
testable message-passing system.

---

## 2. Event System

### 2.1 Typed Events

All events are frozen dataclasses. They are **immutable facts** about
what occurred. No handler mutates an event.

```python
from dataclasses import dataclass, field
from typing import Any
import time


@dataclass(frozen=True)
class DomainEvent:
    event_id: str = field(default_factory=lambda: _new_id())
    timestamp: float = field(default_factory=time.time)


# ── User input ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MessageSubmitted(DomainEvent):
    text: str

@dataclass(frozen=True)
class InputChanged(DomainEvent):
    buf: tuple[str, ...]
    cursor: int
    paste_condensed: bool = False
    paste_label: str = ""


# ── Agent lifecycle ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AgentStarted(DomainEvent):
    turn_id: str
    model: str

@dataclass(frozen=True)
class AgentCompleted(DomainEvent):
    turn_id: str

@dataclass(frozen=True)
class AgentFailed(DomainEvent):
    turn_id: str
    error: str

@dataclass(frozen=True)
class AgentInterrupted(DomainEvent):
    turn_id: str


# ── Tool lifecycle ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolStarted(DomainEvent):
    tool_use_id: str
    name: str
    args: dict[str, Any]

@dataclass(frozen=True)
class ToolCompleted(DomainEvent):
    tool_use_id: str
    name: str
    success: bool
    duration_ms: float | None
    output_lines: tuple[str, ...]
    args_str: str     # pre-formatted for display


# ── LLM streaming ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TextChunk(DomainEvent):
    turn_id: str
    text: str

@dataclass(frozen=True)
class TextFinalized(DomainEvent):
    turn_id: str
    full_text: str

@dataclass(frozen=True)
class TokensAccounted(DomainEvent):
    input_tokens: int
    output_tokens: int
    cost_usd: float


# ── System ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ResizeDetected(DomainEvent):
    cols: int
    rows: int

@dataclass(frozen=True)
class OverlayRequested(DomainEvent):
    overlay_name: str

@dataclass(frozen=True)
class OverlayClosed(DomainEvent):
    overlay_name: str
```

### 2.2 EventBus

```python
from collections import defaultdict
from typing import Callable, Type, TypeVar

E = TypeVar("E", bound=DomainEvent)

class EventBus:
    """Synchronous pub/sub bus. All handlers run in the event loop thread."""

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: Type[E], handler: Callable[[E], None]) -> Callable:
        self._handlers[event_type].append(handler)
        return lambda: self._handlers[event_type].remove(handler)

    def publish(self, event: DomainEvent) -> None:
        for handler in list(self._handlers.get(type(event), [])):
            try:
                handler(event)
            except Exception as exc:
                # Never let a handler crash the bus
                import traceback
                traceback.print_exc()

    def publish_async(self, event: DomainEvent) -> None:
        """Schedule publication on the event loop (thread-safe)."""
        import asyncio
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(self.publish, event)
```

### 2.3 Event Log (for replay / debugging)

```python
class EventLog:
    """Append-only log of all domain events. Used for session replay and debug."""

    def __init__(self, bus: EventBus, max_size: int = 10_000) -> None:
        self._log: list[DomainEvent] = []
        self._max = max_size
        bus.subscribe(DomainEvent, self._record)  # catch-all via base class

    def _record(self, event: DomainEvent) -> None:
        if len(self._log) >= self._max:
            self._log = self._log[-self._max // 2:]
        self._log.append(event)

    def replay(self, bus: EventBus) -> None:
        for event in list(self._log):
            bus.publish(event)

    def snapshot(self) -> list[DomainEvent]:
        return list(self._log)
```

---

## 3. Command System

### 3.1 Command vs Event

| | Event | Command |
|---|---|---|
| Tense | Past ("happened") | Future ("should happen") |
| Immutable | Yes | Yes |
| Multiple handlers | Yes | One handler |
| Replayable | Yes | Yes (with care) |

### 3.2 Command Definitions

```python
@dataclass(frozen=True)
class Command:
    command_id: str = field(default_factory=_new_id)


@dataclass(frozen=True)
class SendMessageCommand(Command):
    text: str

@dataclass(frozen=True)
class InterruptAgentCommand(Command):
    pass

@dataclass(frozen=True)
class OpenOverlayCommand(Command):
    name: str           # "command_palette" | "file_picker" | "config"
    initial_query: str = ""

@dataclass(frozen=True)
class CloseOverlayCommand(Command):
    pass

@dataclass(frozen=True)
class ChangeModelCommand(Command):
    provider: str
    model: str

@dataclass(frozen=True)
class ClearConversationCommand(Command):
    pass

@dataclass(frozen=True)
class RunBuiltinCommand(Command):
    name: str           # "/config", "/model", "/status", etc.
    args: str = ""
```

### 3.3 CommandBus

```python
class CommandBus:
    """One handler per command type. Commands represent executable intent."""

    def __init__(self) -> None:
        self._handlers: dict[type, Callable] = {}

    def register(self, command_type: type, handler: Callable) -> None:
        self._handlers[command_type] = handler

    def dispatch(self, command: Command) -> Any:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        return handler(command)

    async def dispatch_async(self, command: Command) -> Any:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        import asyncio, inspect
        if inspect.iscoroutinefunction(handler):
            return await handler(command)
        return handler(command)
```

### 3.4 Command Registration (at startup)

```python
def wire_commands(
    bus: CommandBus,
    agent_runtime: AgentRuntime,
    conversation: ConversationStore,
    overlay_manager: OverlayManager,
    event_bus: EventBus,
) -> None:
    bus.register(SendMessageCommand,    _handle_send_message(agent_runtime, event_bus))
    bus.register(InterruptAgentCommand, _handle_interrupt(agent_runtime, event_bus))
    bus.register(OpenOverlayCommand,    overlay_manager.open)
    bus.register(CloseOverlayCommand,   overlay_manager.close)
    bus.register(ClearConversationCommand, lambda _: conversation.turns.set([]))
    bus.register(RunBuiltinCommand,     _handle_builtin_command(bus, event_bus))
```

---

## 4. Task System

### 4.1 TaskHandle

Every long-running operation produces a `TaskHandle` — an observable,
cancellable reference to the running async task.

```python
from enum import Enum, auto
import asyncio

class TaskState(Enum):
    PENDING   = auto()
    RUNNING   = auto()
    DONE      = auto()
    CANCELLED = auto()
    FAILED    = auto()


class TaskHandle:
    """Observable wrapper around an asyncio.Task."""

    def __init__(self, name: str, coro) -> None:
        self.name   = name
        self.state  = Signal(TaskState.PENDING)
        self.error: Exception | None = None
        self._task: asyncio.Task | None = None
        self._coro  = coro

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> "TaskHandle":
        async def _run():
            self.state.set(TaskState.RUNNING)
            try:
                await self._coro
                self.state.set(TaskState.DONE)
            except asyncio.CancelledError:
                self.state.set(TaskState.CANCELLED)
            except Exception as exc:
                self.error = exc
                self.state.set(TaskState.FAILED)

        self._task = asyncio.create_task(_run(), name=self.name)
        return self

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self.state() == TaskState.RUNNING

    async def wait(self) -> None:
        if self._task:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
```

### 4.2 TaskManager

```python
class TaskManager:
    """Registry of active tasks. Enables cancellation and monitoring."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskHandle] = {}

    def spawn(self, name: str, coro) -> TaskHandle:
        handle = TaskHandle(name, coro).start()
        self._tasks[name] = handle
        return handle

    def cancel(self, name: str) -> None:
        if handle := self._tasks.get(name):
            handle.cancel()

    def cancel_all(self) -> None:
        for handle in list(self._tasks.values()):
            handle.cancel()

    @property
    def active(self) -> list[TaskHandle]:
        return [h for h in self._tasks.values() if h.is_running]
```

---

## 5. AgentRuntime (Command Handler)

```python
class AgentRuntime:
    """Handles SendMessageCommand. Manages the agent task lifecycle."""

    def __init__(
        self,
        conversation: ConversationStore,
        event_bus: EventBus,
        task_manager: TaskManager,
    ) -> None:
        self._conv    = conversation
        self._bus     = event_bus
        self._tasks   = task_manager
        self._current: TaskHandle | None = None

    async def handle_send_message(self, cmd: SendMessageCommand) -> None:
        """Entry point for SendMessageCommand."""
        import uuid
        turn_id = str(uuid.uuid4())

        self._current = self._tasks.spawn(
            f"agent-{turn_id[:8]}",
            self._run_turn(turn_id, cmd.text),
        )

    async def _run_turn(self, turn_id: str, text: str) -> None:
        self._conv.begin_turn(turn_id, self._conv.model_name())
        self._bus.publish(AgentStarted(turn_id=turn_id, model=self._conv.model_name()))

        # Append turn-start event to conversation (renders header via ScrollBufferAppender)
        self._conv.append_event("turn_start", {
            "agent_name": self._conv.model_name(),
        })

        try:
            async for event in self._stream_agent(text):
                await self._handle_stream_event(turn_id, event)

            self._bus.publish(AgentCompleted(turn_id=turn_id))
            self._conv.end_turn()

        except asyncio.CancelledError:
            self._bus.publish(AgentInterrupted(turn_id=turn_id))
            self._conv.end_turn()
        except Exception as exc:
            self._bus.publish(AgentFailed(turn_id=turn_id, error=str(exc)))
            self._conv.fail_turn(str(exc))

    async def _handle_stream_event(self, turn_id: str, event: dict) -> None:
        kind = event.get("type")
        if kind == "tool_start":
            self._conv.set_tool(event["name"])
            self._bus.publish(ToolStarted(
                tool_use_id=event["tool_use_id"],
                name=event["name"],
                args=event.get("args", {}),
            ))
        elif kind == "tool_complete":
            self._conv.clear_tool()
            # Build display args string
            args     = event.get("args", {})
            args_str = _format_args(args)
            # Append to conversation store → ScrollBufferAppender renders it
            self._conv.append_event("tool_complete", {
                "tool_use_id": event["tool_use_id"],
                "name":        event["name"],
                "success":     event.get("success", True),
                "args_str":    args_str,
                "dur_str":     f"  [dim]{event['duration_ms']:.0f}ms[/dim]" if event.get("duration_ms") else "",
                "output_lines": event.get("output_lines", []),
            })
            self._bus.publish(ToolCompleted(
                tool_use_id=event["tool_use_id"],
                name=event["name"],
                success=event.get("success", True),
                duration_ms=event.get("duration_ms"),
                output_lines=tuple(event.get("output_lines", [])),
                args_str=args_str,
            ))
        elif kind == "text_finalized":
            text = event.get("text", "")
            if text.strip():
                self._conv.append_event("text", {"text": text})
                self._bus.publish(TextFinalized(turn_id=turn_id, full_text=text))
        elif kind == "tokens":
            self._conv.add_tokens(
                event.get("input_tokens", 0),
                event.get("output_tokens", 0),
                event.get("cost_usd", 0.0),
            )
            self._bus.publish(TokensAccounted(
                input_tokens=event.get("input_tokens", 0),
                output_tokens=event.get("output_tokens", 0),
                cost_usd=event.get("cost_usd", 0.0),
            ))

    def interrupt(self) -> None:
        if self._current:
            self._current.cancel()
```

---

## 6. Integration: From Keystroke to Command

```
User presses Enter with text "hello"
    │
    ▼
InputSession.dispatch_normal(Key.ENTER, "")
    │
    ▼
_submit() → returns "hello"
    │
    ▼
CommandBus.dispatch_async(SendMessageCommand(text="hello"))
    │
    ▼
AgentRuntime.handle_send_message(cmd)
    │
    ├── ConversationStore.begin_turn(...)
    ├── EventBus.publish(AgentStarted(...))
    ├── for event in agent.stream():
    │       match event:
    │           tool_complete → ConversationStore.append_event(...)
    │                           → ScrollBufferAppender._on_event(ev)
    │                           → console.print("  ⎿ tool() ✓ 6ms")
    │           text_finalized → ConversationStore.append_event(...)
    │                            → ScrollBufferAppender._on_event(ev)
    │                            → console.print(Markdown(text))
    │
    └── ConversationStore.end_turn()
        EventBus.publish(AgentCompleted(...))
```

---

## 7. Eliminating Current Anti-Patterns

| Current | Replacement |
|---|---|
| `_on_tool_complete` in `tui.py` | `AgentRuntime._handle_stream_event` publishes `ToolCompleted` → `ScrollBufferAppender` renders |
| `_on_assistant_complete` + `flush_from_model()` | `AgentRuntime._handle_stream_event` appends text event → `ScrollBufferAppender` renders |
| `_run_agent` async closure in `tui.py` | `AgentRuntime.handle_send_message` command handler |
| `_sigint_cancel` lambda | `InterruptAgentCommand` → `AgentRuntime.interrupt()` |
| `_pending_queue` list + drain loop | `SendMessageCommand` dispatched immediately; queued messages are additional commands |
| `EventBus.publish(ToolCompleteEvent)` in `agent_turn.py` | `AgentRuntime._handle_stream_event` emits `ToolCompleted` via new EventBus |
