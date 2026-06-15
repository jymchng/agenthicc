# PRD-66 — Kernel Event Bridge

## 1. The Two-Bus Problem

The current agenthicc architecture has **two independent event systems** that
PRDs 58–63 do not reconcile:

```
┌─────────────────────────────────┐    ┌─────────────────────────────────┐
│     Kernel Event Processor      │    │       TUI Event Bus              │
│  (agenthicc.kernel.processor)  │    │  (agenthicc.tui.tui_events)     │
│                                 │    │                                  │
│  EventProcessor (MPSC queue)   │    │  EventBus (synchronous pub/sub)  │
│  root_reducer (pure fn)         │    │  AgenthiccTUI._wire_bus()        │
│  AppState (immutable)           │    │  ToolCompleteEvent, etc.         │
│  Effect → EffectExecutor        │    │                                  │
└────────────┬────────────────────┘    └──────────────────────────────────┘
             │                                        ▲
             │  TUIEventAdapter subscribes            │
             │  to kernel state queue                 │
             └────────────────────────────────────────┘
```

`TUIEventAdapter` (in `agenthicc/tui/events.py`) subscribes to the kernel
processor's subscriber queue and translates `AppState` diffs into events on the
TUI bus. This bridge must survive the migration to the new architecture.

### 1.1 Current TUIEventAdapter Responsibilities

From `agenthicc/tui/events.py`, the adapter maps kernel state changes to:
- `AgentStateChangeEvent` (kernel → TUI bus)
- `SessionSummaryEvent` (kernel → TUI bus)

From `agent_turn.py`, the runner publishes directly to the TUI bus:
- `AssistantStartEvent`, `AssistantCompleteEvent`, `ThinkingStepEvent`
- `ToolStartEvent`, `ToolCompleteEvent`, `FileModifiedEvent`
- `ErrorEvent`, `TokenUpdateEvent`

### 1.2 Architecture Decision

In the new architecture, ALL state changes flow into `ConversationStore` signals.
The kernel processor's `AppState` is separate from the TUI's `ConversationStore`.
The bridge translates kernel state changes into `ConversationStore` mutations.

```
Kernel EventProcessor
        │
        ▼
   KernelBridge (new)
        │
        ├── on AgentState change → ConversationStore.agent_state.set()
        ├── on SessionSummary → ConversationStore.session_id/model_name.set()
        ├── on Notification → ConversationStore.notification.set()
        └── on ModeChange → ConversationStore.mode_str.set()

Agent runner (agent_turn.py / AgentRuntime)
        │
        ├── tool_start → ConversationStore.set_tool()
        ├── tool_complete → ConversationStore.append_event("tool_complete", ...)
        ├── text_finalized → ConversationStore.append_event("text", ...)
        ├── thinking_step → ConversationStore.append_event("thinking_step", ...)
        ├── file_modified → ConversationStore.append_event("file_modified", ...)
        ├── error → ConversationStore.append_event("error", ...)
        └── tokens → ConversationStore.add_tokens()
```

---

## 2. KernelBridge

### 2.1 Implementation

```python
from __future__ import annotations
import asyncio
from typing import Any

class KernelBridge:
    """Subscribes to the kernel EventProcessor and translates state changes
    into ConversationStore mutations.

    The kernel uses an append-only event log with an immutable AppState.
    This bridge subscribes to the kernel's subscriber queue and reacts
    to diff-relevant changes.
    """

    def __init__(
        self,
        processor: Any,           # agenthicc.kernel.EventProcessor
        conversation: Any,        # ConversationStore
        mode_manager: Any,        # ModeManager (for mode_str rebuilding)
    ) -> None:
        self._proc      = processor
        self._conv      = conversation
        self._modes     = mode_manager
        self._task: asyncio.Task | None = None
        self._prev_state: Any = None

    def start(self) -> None:
        """Begin subscribing to kernel state changes."""
        self._task = asyncio.create_task(
            self._run(), name="kernel-bridge"
        )

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        """Poll the kernel subscriber queue for AppState updates."""
        queue: asyncio.Queue = asyncio.Queue()
        self._proc.subscribe(queue)
        try:
            while True:
                new_state = await queue.get()
                self._on_state(new_state)
        except asyncio.CancelledError:
            pass
        finally:
            self._proc.unsubscribe(queue)

    def _on_state(self, state: Any) -> None:
        """Translate AppState diff into ConversationStore mutations."""
        prev = self._prev_state
        self._prev_state = state

        # ── Session / model ───────────────────────────────────────────────
        session_id = getattr(state, "session_id", "")
        if session_id and (prev is None or getattr(prev, "session_id", "") != session_id):
            self._conv.session_id.set(session_id)

        # Model name from settings
        settings = getattr(state, "settings", None)
        if settings:
            model = getattr(settings, "model", "")
            if model and (prev is None or getattr(getattr(prev, "settings", None), "model", "") != model):
                self._conv.model_name.set(model)

        # ── Agent state ───────────────────────────────────────────────────
        # The kernel tracks running agents in AppState.agents
        agents = getattr(state, "agents", {})
        prev_agents = getattr(prev, "agents", {}) if prev else {}

        # Detect agent completion / failure from kernel state
        for agent_id, agent in agents.items():
            prev_agent = prev_agents.get(agent_id)
            if prev_agent is None:
                continue
            prev_status = getattr(prev_agent, "status", None)
            curr_status = getattr(agent, "status", None)
            if prev_status != curr_status:
                self._on_agent_status_change(agent_id, curr_status)

        # ── Notification ──────────────────────────────────────────────────
        # Kernel may publish notification messages via AppState
        notification = getattr(state, "notification", None)
        if notification and (prev is None or getattr(prev, "notification", None) != notification):
            self._conv.notification.set(notification)
            # Auto-clear after 3 seconds
            asyncio.get_event_loop().call_later(
                3.0,
                lambda: self._conv.notification.set(None)
            )

    def _on_agent_status_change(self, agent_id: str, status: str) -> None:
        from agenthicc.tui.conversation_store import AgentState
        mapping = {
            "running": AgentState.RUNNING,
            "thinking": AgentState.THINKING,
            "idle": AgentState.IDLE,
            "complete": AgentState.COMPLETE,
            "error": AgentState.ERROR,
        }
        if status in mapping:
            self._conv.agent_state.set(mapping[status])
```

---

## 3. Agent Runner Event Mapping

### 3.1 Complete Event Table

All events published by `agent_turn.py` (or `AgentRuntime` in the new
architecture) must map to `ConversationStore` mutations. This table is
exhaustive — no event may be silently dropped.

| Source event | Current handler in tui.py | New ConversationStore action |
|---|---|---|
| `AssistantStartEvent` | `_on_assistant_start` → print header | `begin_turn()` + `append_event("turn_start", {agent_name, timestamp})` |
| `AssistantChunkEvent` | no-op (streaming shown via Thinking) | no-op — status animation handles it |
| `AssistantCompleteEvent` | `_on_assistant_complete` → flush text | `append_event("text", {text})` via `AgentRuntime._handle_stream_event` |
| `ThinkingStepEvent` | `print_thinking_step(step, done)` | `append_event("thinking_step", {step, done})` |
| `ToolStartEvent` | `set_tool(name)` + footer mode | `set_tool(name)` on ConversationStore |
| `ToolCompleteEvent` | `_on_tool_complete` → console.print | `append_event("tool_complete", {name, args_str, success, dur_str, output_lines})` |
| `FileModifiedEvent` | `print_file_modified(path)` | `append_event("file_modified", {path})` |
| `ErrorEvent` | `print_error(msg, detail)` | `append_event("error", {message, detail})` |
| `AgentStateChangeEvent` | `status_state.state/tool.set()` | `agent_state.set()` + `active_tool.set()` on ConversationStore |
| `TokenUpdateEvent` | `add_tokens(inp, out, cost)` | `conversation.add_tokens(inp, out, cost)` |
| `SessionSummaryEvent` | `session_id.set(e.session_id)` | `conversation.session_id.set()` + `model_name.set()` |
| `InputChangedEvent` | `input_bar_state.update(buf, cursor)` | `InputState.buf/cursor.set()` directly from `UnifiedInputSession._push()` — no event needed |
| `ModeChangedEvent` | `mode_str/footer_state.mode_str.set()` | `conversation.mode_str.set()` from `ModeManager.cycle()` — no event needed |
| `NotificationEvent` | `footer_state.notify_text(e.text)` | `conversation.notification.set(e.text)` via `KernelBridge` |

### 3.2 New ScrollBufferAppender Match Statement (complete)

```python
def _on_event(self, ev: ConversationEvent) -> None:
    if ev.rendered:
        return
    ev.rendered = True

    match ev.kind:
        case "turn_start":
            self._console.print(
                f"[bold cyan]●[/bold cyan] [bold]{ev.payload['agent_name']}[/bold]"
                f"  [dim]{_hhmmss(ev.timestamp)}[/dim]",
                markup=True, highlight=False,
            )

        case "tool_complete":
            self._render_tool_complete(ev.payload)

        case "text":
            from rich.markdown import Markdown
            text = ev.payload.get("text", "")
            if text.strip():
                self._console.print(Markdown(text), end="")

        case "thinking_step":
            step = ev.payload.get("step", "")
            done = ev.payload.get("done", False)
            icon = "[green]✓[/green]" if done else "[yellow]→[/yellow]"
            self._console.print(
                f"  {icon} [dim]{step}[/dim]",
                markup=True, highlight=False,
            )

        case "file_modified":
            path = ev.payload.get("path", "")
            from rich.markup import escape as _e
            self._console.print(
                f"  [dim]Modified:[/dim] [cyan]{_e(path)}[/cyan]",
                markup=True, highlight=False,
            )

        case "error":
            msg    = ev.payload.get("message", "")
            detail = ev.payload.get("detail", "")
            from rich.markup import escape as _e
            self._console.print(
                f"\n[red bold]ERROR[/red bold] {_e(msg)}",
                markup=True, highlight=False,
            )
            if detail:
                self._console.print(
                    f"[dim]{_e(detail)}[/dim]",
                    markup=True, highlight=False,
                )

        case "mention_chips":
            chips = ev.payload.get("chips", [])
            for chip in chips:
                raw     = chip.get("raw", "")
                preview = chip.get("content_preview", "")
                from rich.markup import escape as _e
                self._console.print(
                    f"  [dim]@[/dim][cyan]{_e(raw.lstrip('@'))}[/cyan]"
                    + (f"  [dim]{_e(preview[:60])}[/dim]" if preview else ""),
                    markup=True, highlight=False,
                )

        case "user_message":
            text = ev.payload.get("text", "")
            from rich.markup import escape as _e
            sep = "[dim]" + "─" * 72 + "[/dim]"
            self._console.print(
                f"[bold cyan]You[/bold cyan]\n{sep}\n{_e(text)}",
                markup=True, highlight=False,
            )

        case _:
            pass  # unknown event kinds are silently ignored
```

---

## 4. ThinkingStep Events (Extended Thinking)

`ThinkingStepEvent` arrives when the LLM uses extended thinking (Claude's
`<thinking>` blocks). The agent runner publishes one event per thinking
step, with `done=False` while thinking and `done=True` when the step completes.

### 4.1 ConversationEvent representation

```python
ConversationEvent(
    kind="thinking_step",
    payload={
        "step": "Analyzing the repository structure...",
        "done": False,   # False = in progress, True = completed
    }
)
```

### 4.2 Rendering

- In-progress: `  → [dim]Analyzing the repository structure…[/dim]`
- Completed: `  ✓ [dim]Analyzing the repository structure[/dim]`

Because `rendered=True` is set on first render, the step line is printed once
when it starts (showing →) and never reprinted when done. To show the ✓ update,
the event must be re-appended as a new event with `done=True`:

```python
# In AgentRuntime._handle_stream_event:
case "thinking_step":
    step = event.get("step", "")
    done = event.get("done", False)
    if done:
        # Append a new "done" thinking step event
        self._conv.append_event("thinking_step", {"step": step, "done": True})
    else:
        # Append a "running" thinking step event
        self._conv.append_event("thinking_step", {"step": step, "done": False})
```

---

## 5. @mention Chips

When the agent resolves `@path` mentions (e.g. `@README.md`), the resolved
content is attached to the turn. This is currently stored in
`AgentTurnEntry.mention_chips` and rendered inline in the transcript.

### 5.1 ConversationEvent representation

```python
ConversationEvent(
    kind="mention_chips",
    payload={
        "chips": [
            {
                "raw": "@README.md",
                "resolved_path": "/project/README.md",
                "content_preview": "# agenthicc\nA state-driven agent operating…",
            }
        ]
    }
)
```

This event is appended to `ConversationStore` BEFORE the first tool call of the
turn, once mention resolution is complete.

### 5.2 Rendering

```
  @README.md  # agenthicc\nA state-driven agent operating…
```

---

## 6. Error Handling

### 6.1 Agent turn errors

When the agent turn raises an unhandled exception, `AgentRuntime._run_turn`
catches it and:

```python
except Exception as exc:
    import traceback
    detail = traceback.format_exc()
    self._conv.append_event("error", {
        "message": f"{type(exc).__name__}: {exc}",
        "detail": detail,
    })
    self._conv.fail_turn(str(exc))
```

The error is rendered in the scroll buffer as a red ERROR block. The agent
state returns to IDLE (via `fail_turn`).

### 6.2 Non-fatal errors (tool failures)

Tool failures (✗) are represented as `"tool_complete"` events with
`success=False`. They are NOT `"error"` events.

### 6.3 Kernel processor errors

Errors originating in the kernel processor (e.g. invalid intent schema) are
translated by `KernelBridge` into `"error"` ConversationEvents.

---

## 7. Migration Checklist

| Item | Action | Done |
|---|---|---|
| Remove `TUIEventAdapter` (old) | Replace with `KernelBridge` | Slice 3 |
| Remove `tui/tui_events.py` imports from tui.py | All events → ConversationStore | Slice 2 |
| `ThinkingStepEvent` → `append_event("thinking_step")` | Agent runner | Slice 2 |
| `FileModifiedEvent` → `append_event("file_modified")` | Agent runner | Slice 2 |
| `ErrorEvent` → `append_event("error")` | Agent runner | Slice 2 |
| `AgentStateChangeEvent` → `KernelBridge` | Kernel bridge | Slice 3 |
| `SessionSummaryEvent` → `KernelBridge` | Kernel bridge | Slice 3 |
| `NotificationEvent` → `KernelBridge` | Kernel bridge | Slice 3 |
| `ModeChangedEvent` → removed (ModeManager is TUI-local) | UnifiedInputSession | Slice 4 |
| `InputChangedEvent` → removed (push direct from session) | UnifiedInputSession | Slice 4 |
| `mention_chips` → `append_event("mention_chips")` | Agent runner | Slice 2 |
| `user_message` → `append_event("user_message")` | AgentRuntime / TUI | Slice 3 |
