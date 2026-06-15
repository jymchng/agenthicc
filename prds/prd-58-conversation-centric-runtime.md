# PRD-58 — Conversation-Centric Runtime Architecture

## 1. Executive Summary

This document defines the architectural foundation for transforming agenthicc's
Rich-based TUI from a screen-navigated application into a **conversation-centric
reactive runtime** — a continuously running workspace where everything is state,
everything streams, and nothing navigates.

The mental model is not "an app with screens."
The mental model is "a live intelligence workspace that never stops thinking."

This PRD establishes the **top-level architecture** and **paradigm contract**.
Subsequent PRDs (59–63) specify each subsystem in detail.

---

## 2. Problem with the Current Architecture

The current system exhibits **screen-based thinking** at every layer:

| Symptom | Impact |
|---|---|
| `live_panel.start()` / `live_panel.stop()` bracketing every agent turn | UI flickers, cursor races, terminal corruption |
| `flush_from_model()` as the rendering sync primitive | Duplicate rendering, MAX_VISIBLE_TOOL_CALLS caps, swallowed LLM text |
| `InputSession` vs `StreamingSession` as separate modes | Two CBREAK loops, race conditions on `raw_mode` handoff |
| `_printed_count` to track what's been rendered | Stateful cursor that gets desynced on every agent turn |
| Tool calls printed via `console.print()` during Live block | Background `_RefreshThread` races, cursor desynchronisation |
| `_pending_menu`, `_pending_queue`, `_text_events_printed` | Leaky state scattered across event handlers |

The root cause: the system was built to **navigate between modes** rather than to
**project a continuously evolving state**.

---

## 3. Core Principle: Reject Navigation, Embrace Projection

```
REJECT                          ADOPT
──────────────────────────────────────────────────────
Screen navigation               Continuous workspace
live_panel.start/stop           Always-on reactive layer
flush_from_model()              State → render pipeline
Mode switching                  Context shifting
console.print() during Live     Scroll buffer append protocol
Imperative redraws              Reactive subscriptions
```

The workspace **never stops**. The conversation **always flows**. The UI is a
**read-only projection** of state — it never mutates state directly.

---

## 4. Runtime Layers

```
┌─────────────────────────────────────────────────────────┐
│                    Application Shell                    │
│  (asyncio.run, signal handling, startup/shutdown)       │
├─────────────────────────────────────────────────────────┤
│                    Agent Runtime                        │
│  (TaskManager, AgentRunner, ToolExecutor)               │
├─────────────────────────────────────────────────────────┤
│              Reactive State Graph (PRD-59)              │
│  (ConversationStore, signals, computed values)          │
├─────────────────────────────────────────────────────────┤
│              Event & Command Bus (PRD-61)               │
│  (typed events, command dispatcher, event log)          │
├─────────────────────────────────────────────────────────┤
│             Component System (PRD-60)                   │
│  (Workspace, ConversationSurface, Composer, Status)     │
├─────────────────────────────────────────────────────────┤
│             Overlay & Focus Manager (PRD-62)            │
│  (OverlayHost, FocusManager, modal traps)               │
├─────────────────────────────────────────────────────────┤
│           Rich Rendering Pipeline (PRD-60)              │
│  (dumb renderers, Rich console, scroll buffer protocol) │
└─────────────────────────────────────────────────────────┘
```

Each layer has a **single responsibility** and communicates only through
well-defined interfaces. No layer reaches across layers.

---

## 5. The Workspace

The workspace is the **only persistent UI surface**. It does not navigate. It
does not have pages. It has **regions** whose content evolves continuously.

```
┌──────────────────────────────────────────────────────────┐
│  Context Strip    [model] [session] [tokens] [cost]      │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  Conversation Surface                                    │
│  (scroll buffer — appended, never rewritten)            │
│                                                          │
│  ● assistant  13:24:01                                  │
│    ⎿ list_directory(path='src')  ✓  6ms                │
│    Based on the structure, I'll focus on…               │
│                                                          │
├──────────────────────────────────────────────────────────┤
│  Live Region  [status flower+animation │ runtime │ tool] │
│  ─────────────────────────────────────────────────────  │
│  ❯ input▌                                               │
│  ─────────────────────────────────────────────────────  │
│  [mode str]  [Esc Interrupt │ Ctrl+Z Background]        │
└──────────────────────────────────────────────────────────┘
```

- **Context Strip**: always visible, always current — never "reloaded"
- **Conversation Surface**: append-only scroll buffer — never rewritten
- **Live Region**: the only Rich Live block, always active, always stable

---

## 6. The Scroll Buffer Protocol

The **conversation surface is append-only**. This replaces all current
`flush_from_model()`, `console.print()`, and `_printed_count` machinery.

```
Protocol:
  1. All human messages, tool calls, LLM text, errors → appended to ConversationStore
  2. ConversationRenderer subscribes to the store
  3. On new item: renderer prints ONE canonical line to stdout (ABOVE the Live region)
  4. The Live region never moves — Rich manages its position relative to the terminal bottom

Rules:
  - NOTHING is printed while the Live block's background thread is running
  - ALL printing happens via the ScrollBufferAppender (single point of truth)
  - NO duplicate rendering possible (each item has a unique ID; renderer tracks last_rendered_id)
```

The Live region is **always-on** with `auto_refresh=False`. It only updates when
state changes (reactive subscriptions), never on a timer thread.

---

## 7. The Live Region: Always-On, Never Stopped

The current architecture starts and stops the Live block on every agent turn.
This is the source of cursor races, flickering, and terminal corruption.

**New behavior**: the Live block starts **once** at application startup and stops
**once** at shutdown. It never starts/stops mid-session.

```python
# Application startup:
workspace.live.start()

# Agent turn:
agent_runtime.run(prompt)
# ↑ No live_panel.start() / live_panel.stop() here

# Application shutdown:
workspace.live.stop()
```

The Live region renders the **current state** of the reactive graph. Agent turns
do not change the Live region structure — they change the **state** that the Live
region renders.

---

## 8. Two Rendering Surfaces

The architecture has exactly two rendering surfaces:

```
Surface A: Scroll Buffer (stdout, above Live)
  - Append-only
  - Driven by ConversationStore events
  - Managed by ScrollBufferAppender
  - Never re-rendered (immutable once written)

Surface B: Live Region (Rich Live block, pinned to terminal bottom)
  - Always-on (start once, stop once)
  - auto_refresh=False (no background thread)
  - Updated by reactive state subscriptions
  - Contains: status, input bar, footer
  - Does NOT contain: conversation history, tool calls
```

Tool calls and LLM text go to **Surface A** (scroll buffer). They are **never**
in Surface B (Live region). This eliminates the cursor race completely.

---

## 9. Agent Turn Model

An agent turn is a **state transition**, not a UI mode switch.

```
ConversationStore state machine:
  IDLE → RUNNING → IDLE

During RUNNING:
  - StatusState reflects "Thinking" / "Running"
  - Tool calls are appended to ConversationStore.events
  - LLM text is streamed into ConversationStore.events
  - ScrollBufferAppender renders each event exactly once to Surface A
  - Live Region reflects RUNNING state (animation, tool name)

During IDLE:
  - StatusState reflects "Idle"
  - Live Region reflects IDLE state (no animation)
  - User types in Composer (always-on InputSession)
```

No `start()` / `stop()` of the Live region required.

---

## 10. Input Architecture: One Session, Always Running

The current system alternates between `IdleInputSession` and `StreamingSession`.
This creates the `raw_mode` nesting bugs.

**New behavior**: one `InputSession` runs for the entire application lifetime.

```
InputSession (lifetime = application lifetime)
│
├── idle mode: full trigger support, history, cursor movement
└── streaming mode: reduced key set (queue messages, interrupt)

Mode is a PROPERTY of InputSession, not a different class.

When agent starts: session.set_mode(InputMode.STREAMING)
When agent ends:   session.set_mode(InputMode.IDLE)
```

`raw_mode` is entered **once** at startup and exited **once** at shutdown.
No race conditions, no nested contexts, no terminal corruption.

---

## 11. Data Flow

```
User keystroke
    │
    ▼
InputSession (single CBREAK loop)
    │
    ├── trigger (@/@/) → TriggerSystem → CompletionOverlay
    ├── command (/)   → CommandBus     → CommandHandler
    ├── submit        → CommandBus     → RunAgentCommand
    └── interrupt     → AgentRuntime  → cancel()
    │
    ▼
CommandBus.dispatch(RunAgentCommand(text))
    │
    ▼
AgentRuntime.run(text)
    │
    ├── ConversationStore.add_turn(turn)
    ├── for event in agent.stream():
    │       ConversationStore.append_event(event)
    │           │
    │           ▼
    │       ScrollBufferAppender.on_event(event)
    │           │
    │           └── console.print(render(event))  ← ONE surface, ONE writer
    │
    └── ConversationStore.complete_turn()
```

---

## 12. Success Criteria

| Criterion | Measurement |
|---|---|
| Zero terminal corruption | No cursor desync across 1000 agent turns |
| No `start()`/`stop()` of Live region | Audit: `live.start` called exactly once |
| No `flush_from_model()` | Symbol removed from codebase |
| No `_printed_count` tracking | Symbol removed from codebase |
| Single `raw_mode` context | Audit: `with raw_mode` appears in exactly one place |
| Tool calls always show args + correct status | Verified via test suite |
| LLM text never swallowed | Regression test: 100 agent turns, all text visible |
| Zero background refresh thread | `auto_refresh=False` enforced |

---

## 13. Relationship to Other PRDs

| PRD | Subsystem | What it specifies |
|---|---|---|
| PRD-59 | Reactive State Graph | Signal system, ConversationStore, computed values |
| PRD-60 | Component System | Component contract, lifecycle, rendering pipeline |
| PRD-61 | Event & Command System | Typed events, CommandBus, TaskManager |
| PRD-62 | Streaming, Overlay, Focus | Streaming protocol, OverlayHost, FocusManager |
| PRD-63 | Migration & Plugins | Incremental migration plan, plugin architecture |
