---
id: PRD-06
title: "TUI and Observability"
status: draft
created: 2025-01-01
author: engineering
version: "0.1.0"
tags: [tui, observability, prompt_toolkit, lauren-ai, terminal]
---

# PRD-06: TUI and Observability

## 1. Executive Summary

This document specifies a terminal user interface (TUI) for the Lauren-AI multi-agent
orchestration system. The TUI renders in real time using `prompt_toolkit`, presenting a
transcript-style scrollable history pane that mirrors the look and feel of Claude Code.
The interface is driven entirely by events published to the `SignalBus` in
`lauren_ai._signals`, with no polling.

The core invariant is that the **Input Bar is permanently pinned at the very bottom of
the terminal window**. All overlays — slash-command menus, HITL approval dialogs, the
settings editor, and the history search panel — float *above* the Input Bar as transient
layers and never displace it downward. Scrolling the transcript, spawning agents, or
receiving streaming output does not move the Input Bar.

A **headless mode** is also defined: when the process is not connected to a TTY, or when
`--headless` is passed, the system emits newline-delimited JSON to `stdout` at the same
cadence it would repaint the TUI, enabling log aggregation, CI pipelines, and remote
dashboards without modification.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal |
|---|------|
| G1 | Render a scrollable, live-updating transcript that shows every agent turn, tool call, and model invocation. |
| G2 | Keep the Input Bar stationary at the bottom of the terminal at all times, regardless of scroll position or overlay state. |
| G3 | Surface spinner states (running / success / failure) per tool call with sub-100 ms latency from signal receipt to repaint. |
| G4 | Provide slash-command menus that pop up as overlays *above* the Input Bar and are dismissed without affecting layout. |
| G5 | Expose a headless mode that emits structured JSON-lines so the system is observable without a TTY. |
| G6 | Be fully driven by `SignalBus` subscriptions — no polling loops over agent state. |
| G7 | Achieve a render-loop latency of < 16 ms (60 fps equivalent) for 95th-percentile updates. |
| G8 | Support live-editable TOML configuration for theme, refresh rate, and headless mode. |

### 2.2 Non-Goals

| # | Non-Goal |
|---|----------|
| NG1 | A graphical (GUI) interface or browser-based dashboard. |
| NG2 | Mouse support beyond basic scrolling. |
| NG3 | Persistent storage of transcripts (that is PRD-05). |
| NG4 | Running multiple simultaneous TUI sessions against the same process. |
| NG5 | Custom rendering backends other than `prompt_toolkit` and JSON-lines stdout. |

---

## 3. Architecture and Design

### 3.1 High-Level Architecture

```
  +-------------------------------------------------------------------+
  |                        Lauren-AI Core                            |
  |                                                                   |
  |  Agent Pool ──► SignalBus ──► TUI EventAdapter                   |
  |                                  |                               |
  |                                  v                               |
  |                            asyncio.Queue                         |
  +----------------------------------+--------------------------------+
                                     |  UIEvent (typed dataclass)
                                     v
  +-------------------------------------------------------------------+
  |                          TUI Process                             |
  |                                                                   |
  |   render_loop()                                                   |
  |     +-- drain UIEvent queue                                       |
  |     +-- mutate AppState (thread-safe via asyncio lock)            |
  |     +-- diff against previous FrameSnapshot                       |
  |     +-- call app.invalidate() ──► prompt_toolkit repaints         |
  |                                                                   |
  |   Layout (HSplit)                                                 |
  |     +-- TranscriptWindow (scrollable, grows to fill height)       |
  |     +-- StatusLine (height=1, session info + cost)                |
  |     +-- InputBar (height=1, always at bottom)                     |
  |          +-- FloatContainer: menu overlays (conditional)          |
  +-------------------------------------------------------------------+
```

### 3.2 Detailed Terminal Layout

```
+------------------------------------------------------+  <- terminal top
|                                                      |
|  * agent:planner  12:34:01                           |
|  > Analyzing codebase structure...                   |
|    [tool] read_file src/auth.py          ✓  45ms     |
|    [tool] write_file src/auth.py         ⣿ running  |
|  ─────────────────────────────────────────────────── |
|  * agent:tester  12:34:05                            |
|  > Running test suite...                             |
|    [tool] run_tests                      ✗  FAILED   |
|           AssertionError: expected 200, got 500      |
|                                                      |
|  (transcript continues — scrollable with arrow keys) |
|                                                      |
+------------------------------------------------------+
|  session-abc | 3 agents | $0.042 | 1,234 tok         |  <- StatusLine (row -3)
+------------------------------------------------------+
|  > _                            [Input Bar - fixed]  |  <- row -2  ALWAYS HERE
+------------------------------------------------------+
|  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  |  <- row -1  menu anchor row
+------------------------------------------------------+  <- terminal bottom

  When /status is typed, menu overlay FLOATS ABOVE Input Bar:

+------------------------------------------------------+
|  +--------------------------------------------------+  |  <- overlay top
|  | /status                                          |  |
|  |  +-- agent:planner   RUNNING  turn 3             |  |
|  |  +-- agent:tester    COMPLETE turns=5            |  |
|  |  +-- agent:reviewer  WAITING                     |  |
|  +--------------------------------------------------+  |  <- overlay bottom
+------------------------------------------------------+
|  session-abc | 3 agents | $0.042 | 1,234 tok         |
+------------------------------------------------------+
|  > /status_                     [Input Bar - fixed]  |  <- UNCHANGED
+------------------------------------------------------+
```

### 3.3 Component Diagram

```
prompt_toolkit Application
|
+-- layout: Layout(FloatContainer(
|     content=HSplit([
|       TranscriptWindow,          # grows to fill remaining height
|       StatusLine,                # height=1
|       InputBar,                  # height=1
|     ]),
|     floats=[
|       Float(
|         content=ConditionalContainer(MenuOverlay, filter=menu_visible),
|         bottom=2,                # anchored 2 rows above terminal bottom
|         right=0,
|       ),
|     ]
|   ))
|
+-- key_bindings: KeyBindings
|     /        -> begin slash command
|     Ctrl-C   -> confirm exit
|     PgUp/Dn  -> scroll transcript
|     Arrow UD -> scroll transcript
|     Escape   -> dismiss menu overlay
|     Enter    -> submit input / confirm overlay action
|     Tab      -> autocomplete slash command
|
+-- style: Style.from_dict(theme_dict)
```

---

## 4. Layout Specification

### 4.1 prompt_toolkit Component Tree

```python
from prompt_toolkit.layout import (
    HSplit, VSplit, Window, FloatContainer, Float,
    ConditionalContainer, ScrollablePane,
)
from prompt_toolkit.layout.controls import (
    BufferControl, FormattedTextControl,
)
from prompt_toolkit.filters import Condition

# -- Transcript window -------------------------------------------------
transcript_control = FormattedTextControl(
    text=render_transcript,   # callable -> list[tuple[str, str]]
    focusable=False,
    show_cursor=False,
)
transcript_window = Window(
    content=transcript_control,
    dont_extend_height=False,   # expands to fill available rows
    wrap_lines=True,
    get_vertical_scroll=lambda w: app_state.scroll_offset,
)

# -- Status line -------------------------------------------------------
status_control = FormattedTextControl(
    text=render_status_line,
    focusable=False,
)
status_window = Window(
    content=status_control,
    height=1,
    style="class:statusline",
)

# -- Input bar ---------------------------------------------------------
input_buffer = Buffer(name="input", multiline=False, completer=slash_completer)
input_control = BufferControl(buffer=input_buffer, focusable=True)
input_window = Window(
    content=input_control,
    height=1,
    style="class:input-bar",
    get_line_prefix=lambda _lineno, _wrap: [("class:prompt", "> ")],
)

# -- Menu overlay ------------------------------------------------------
menu_overlay = ConditionalContainer(
    content=Window(
        content=FormattedTextControl(render_active_menu),
        style="class:menu-overlay",
    ),
    filter=Condition(lambda: app_state.menu_visible),
)

# -- Root layout -------------------------------------------------------
root_container = FloatContainer(
    content=HSplit([
        transcript_window,
        status_window,
        input_window,
    ]),
    floats=[
        Float(
            content=menu_overlay,
            bottom=2,    # 2 rows above terminal bottom = just above input bar
            right=0,
            xcursor=False,
            ycursor=False,
        ),
    ],
)
layout = Layout(root_container, focused_element=input_window)
```

### 4.2 Key Bindings

| Key | Context | Action |
|-----|---------|--------|
| `/` | input bar focused, buffer empty | enter slash-command mode, show autocomplete |
| `Tab` | slash-command mode | cycle completions |
| `Enter` | input bar | submit command or send user message |
| `Enter` | menu overlay (approve dialog) | confirm YES |
| `n` | menu overlay (approve dialog) | confirm NO |
| `Escape` | menu overlay active | dismiss overlay, return focus to input bar |
| `PageUp` / `Ctrl-B` | any | scroll transcript up one page |
| `PageDown` / `Ctrl-F` | any | scroll transcript down one page |
| `Up` / `Down` | any | scroll transcript one line |
| `Ctrl-C` | any | show exit confirmation, second `Ctrl-C` exits |
| `Ctrl-L` | any | hard-refresh (clear diff cache, full repaint) |
| `Ctrl-/` | any | toggle help overlay |

### 4.3 Slash Commands

| Command | Menu Class | Description |
|---------|-----------|-------------|
| `/status` | `StatusMenu` | Tree of all running/completed agents and tasks |
| `/approve` | `ApproveMenu` | HITL tool-call approval dialog |
| `/settings` | `SettingsMenu` | Live TOML editor with save-on-close |
| `/history` | `HistoryMenu` | Searchable event log with fuzzy filter |
| `/help` | `HelpMenu` | Key-binding reference |
| `/quit` | — | Graceful shutdown with confirmation |

---

## 5. Transcript Rendering Specification

### 5.1 Block Format

Each agent "turn" is rendered as a named block separated by a horizontal rule.

```
  * agent:planner  12:34:01
  > [streaming text from the model, word-wrapped to terminal width]
    [tool] read_file src/auth.py              ✓  45ms
    [tool] write_file src/auth.py             ⣿ running...
    [tool] run_tests                          ✗  3 errors
           AssertionError: tests/test_api.py::test_post_200
  ─────────────────────────────────────────────────────────────────
```

- The `*` bullet uses `class:agent-bullet` and is colored per-agent by hashing `agent_id`.
- The `>` prefix on model text uses `class:model-text`.
- Tool calls are indented 4 spaces and prefixed with `[tool]` in `class:tool-label`.
- Horizontal rules are drawn with `─` (U+2500) and use `class:separator`.

### 5.2 Tool Call Rendering States

```
State machine per tool call (keyed by tool_use_id):

  PENDING --> RUNNING --> SUCCESS
                     \--> FAILURE
```

| State | Symbol | Color class | Suffix |
|-------|--------|-------------|--------|
| PENDING | `.` | `class:pending` | *(none)* |
| RUNNING | `⣿` (Braille spinner) | `class:spinner` | ` running...` |
| SUCCESS | `✓` | `class:success` | ` {duration_ms}ms` |
| FAILURE | `✗` | `class:error` | ` {error_summary}` |

Spinner animation cycles through `⣾ ⣽ ⣻ ⢿ ⡿ ⣟ ⣯ ⣷` at 100 ms intervals,
driven by a separate `asyncio` timer that only runs while any tool is in RUNNING state.

### 5.3 Streaming Model Text

Model text arrives token-by-token via `ModelCallStarted` / `ContentBlockDelta` signals.
Each delta appends to a mutable string buffer associated with the current agent turn.
The transcript control re-renders on every invalidation; there is no explicit diff of
individual characters — `prompt_toolkit` handles terminal diffing internally.

### 5.4 Cost and Token Footer

The final line of each completed agent turn shows a cost/token summary:

```
  -> tokens: 1,204 in / 387 out  cost: $0.003  stop: end_turn
```

This is populated when `AgentRunComplete` fires.

### 5.5 Diff Algorithm

To avoid redundant full repaints, `render_transcript` is memoized by a hash of:

```python
transcript_hash = hash((
    len(app_state.turns),
    app_state.turns[-1].last_delta_ts if app_state.turns else 0,
    frozenset(app_state.active_tool_states.items()),
    app_state.spinner_tick,
))
```

If the hash is unchanged since the last `render_transcript` call, the cached
`FormattedText` list is returned immediately. The diff against the *previous frame*
is handled by `prompt_toolkit`'s built-in `_DiffRenderer`.

---

## 6. Data Structures and Interfaces

### 6.1 Core Types

```python
# tui/types.py

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import time


class ToolState(Enum):
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILURE = auto()


@dataclass
class ToolCallEntry:
    tool_use_id: str
    tool_name: str
    agent_id: str
    input: dict
    state: ToolState = ToolState.PENDING
    started_at: float = field(default_factory=time.monotonic)
    duration_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class AgentTurnEntry:
    agent_id: str
    agent_name: str
    started_at: float
    model_text: str = ""
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    is_complete: bool = False
    total_cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    stop_reason: Optional[str] = None
    last_delta_ts: float = field(default_factory=time.monotonic)


@dataclass
class AppState:
    session_id: str = "unknown"
    turns: list[AgentTurnEntry] = field(default_factory=list)
    active_agents: dict[str, str] = field(default_factory=dict)   # agent_id -> agent_name
    active_tool_states: dict[str, ToolState] = field(default_factory=dict)  # tool_use_id -> state
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    scroll_offset: int = 0
    menu_visible: bool = False
    active_menu: Optional[str] = None   # "status" | "approve" | "settings" | "history"
    pending_approval: Optional[ToolCallEntry] = None
    spinner_tick: int = 0
    headless: bool = False
```

### 6.2 UIEvent Union

```python
# tui/events.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Union


@dataclass
class ToolStartedEvent:
    tool_name: str
    tool_use_id: str
    agent_id: str
    input: dict


@dataclass
class ToolCompleteEvent:
    tool_name: str
    tool_use_id: str
    agent_id: str
    duration_ms: float
    success: bool
    error: str | None


@dataclass
class ModelTextDeltaEvent:
    agent_id: str
    agent_name: str
    delta: str


@dataclass
class AgentCompleteEvent:
    agent_id: str
    agent_name: str
    turns: int
    total_cost_usd: float
    input_tokens: int
    output_tokens: int
    stop_reason: str


@dataclass
class AgentTurnStartEvent:
    agent_id: str
    agent_name: str


@dataclass
class SpinnerTickEvent:
    pass


UIEvent = Union[
    ToolStartedEvent,
    ToolCompleteEvent,
    ModelTextDeltaEvent,
    AgentCompleteEvent,
    AgentTurnStartEvent,
    SpinnerTickEvent,
]
```

### 6.3 TUIApp Interface

```python
# tui/app.py

class TUIApp:
    def __init__(self, state: AppState, config: TUIConfig) -> None: ...
    async def run(self) -> None: ...
    async def stop(self) -> None: ...
    def push_event(self, event: UIEvent) -> None: ...
    def set_headless(self, headless: bool) -> None: ...
```

### 6.4 TUIConfig (TOML-backed)

```python
# tui/config.py
from dataclasses import dataclass

@dataclass
class TUIConfig:
    theme: str = "dark"
    refresh_rate_ms: int = 16          # target frame budget
    headless: bool = False
    spinner_interval_ms: int = 100
    max_transcript_lines: int = 10_000
    show_token_counts: bool = True
    show_cost: bool = True
    input_bar_prompt: str = "> "
    menu_max_height: int = 20
    history_max_events: int = 500
    color_agents: bool = True          # per-agent bullet color
    wrap_tool_args: bool = True
    log_file: str | None = None        # optional file sink for headless JSON
```

---

## 7. Implementation Plan

### 7.1 SignalBus Integration

Lauren-AI publishes all observability events through `lauren_ai._signals.SignalBus`.
The TUI subscribes at startup and converts each signal into a `UIEvent` which is
placed on the `asyncio.Queue`.

```python
# tui/adapter.py

from lauren_ai._signals import (
    SignalBus,
    ToolCallStarted,
    ToolCallComplete,
    AgentRunComplete,
    ModelCallStarted,
    ModelCallComplete,
    AgentTurnComplete,
)
from .events import (
    ToolStartedEvent,
    ToolCompleteEvent,
    ModelTextDeltaEvent,
    AgentCompleteEvent,
    AgentTurnStartEvent,
)
import asyncio


class TUIEventAdapter:
    """Bridges lauren_ai SignalBus to the TUI UIEvent queue."""

    def __init__(self, bus: SignalBus, queue: asyncio.Queue) -> None:
        self._bus = bus
        self._queue = queue

    def subscribe_all(self) -> None:
        self._bus.subscribe(ToolCallStarted, self._on_tool_started)
        self._bus.subscribe(ToolCallComplete, self._on_tool_complete)
        self._bus.subscribe(AgentRunComplete, self._on_agent_complete)
        self._bus.subscribe(ModelCallStarted, self._on_model_started)
        self._bus.subscribe(AgentTurnComplete, self._on_turn_complete)

    def _on_tool_started(self, sig: ToolCallStarted) -> None:
        self._queue.put_nowait(
            ToolStartedEvent(
                tool_name=sig.tool_name,
                tool_use_id=sig.tool_use_id,
                agent_id=sig.agent_id,
                input=sig.input,
            )
        )

    def _on_tool_complete(self, sig: ToolCallComplete) -> None:
        self._queue.put_nowait(
            ToolCompleteEvent(
                tool_name=sig.tool_name,
                tool_use_id=sig.tool_use_id,
                agent_id=sig.agent_id,
                duration_ms=sig.duration_ms,
                success=sig.success,
                error=sig.error,
            )
        )

    def _on_agent_complete(self, sig: AgentRunComplete) -> None:
        usage = sig.total_usage
        self._queue.put_nowait(
            AgentCompleteEvent(
                agent_id=sig.agent_id,
                agent_name=sig.agent_name,
                turns=sig.turns,
                total_cost_usd=sig.total_cost_usd,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                stop_reason=sig.stop_reason,
            )
        )

    def _on_model_started(self, sig: ModelCallStarted) -> None:
        self._queue.put_nowait(
            AgentTurnStartEvent(
                agent_id=sig.agent_id,
                agent_name=sig.agent_name,
            )
        )

    def _on_turn_complete(self, sig: AgentTurnComplete) -> None:
        # AgentTurnComplete carries partial usage; used for streaming cost display
        pass
```

### 7.2 Render Loop

```python
# tui/render_loop.py

import asyncio
import time
from .events import UIEvent, ToolStartedEvent, ToolCompleteEvent
from .events import ModelTextDeltaEvent, AgentCompleteEvent, AgentTurnStartEvent
from .events import SpinnerTickEvent
from .types import AppState, ToolCallEntry, ToolState, AgentTurnEntry


async def render_loop(
    queue: asyncio.Queue,
    state: AppState,
    invalidate_fn,          # prompt_toolkit app.invalidate
    headless_emit_fn=None,  # callable(event) for JSON-lines mode
) -> None:
    while True:
        event: UIEvent = await queue.get()
        t0 = time.monotonic()

        _apply_event(state, event)

        if state.headless and headless_emit_fn:
            headless_emit_fn(event)
        else:
            invalidate_fn()

        elapsed_ms = (time.monotonic() - t0) * 1000
        if elapsed_ms > 16:
            # Log slow frames for performance debugging
            import logging
            logging.getLogger("tui.render_loop").warning(
                "Slow frame: %.1f ms for %s", elapsed_ms, type(event).__name__
            )

        queue.task_done()


def _apply_event(state: AppState, event: UIEvent) -> None:
    if isinstance(event, AgentTurnStartEvent):
        state.active_agents[event.agent_id] = event.agent_name
        state.turns.append(
            AgentTurnEntry(
                agent_id=event.agent_id,
                agent_name=event.agent_name,
                started_at=time.monotonic(),
            )
        )

    elif isinstance(event, ModelTextDeltaEvent):
        turn = _find_or_create_turn(state, event.agent_id, event.agent_name)
        turn.model_text += event.delta
        turn.last_delta_ts = time.monotonic()

    elif isinstance(event, ToolStartedEvent):
        turn = _find_or_create_turn(state, event.agent_id, "unknown")
        entry = ToolCallEntry(
            tool_use_id=event.tool_use_id,
            tool_name=event.tool_name,
            agent_id=event.agent_id,
            input=event.input,
            state=ToolState.RUNNING,
        )
        turn.tool_calls.append(entry)
        state.active_tool_states[event.tool_use_id] = ToolState.RUNNING

    elif isinstance(event, ToolCompleteEvent):
        state.active_tool_states.pop(event.tool_use_id, None)
        for turn in reversed(state.turns):
            for tc in turn.tool_calls:
                if tc.tool_use_id == event.tool_use_id:
                    tc.state = ToolState.SUCCESS if event.success else ToolState.FAILURE
                    tc.duration_ms = event.duration_ms
                    tc.error = event.error
                    return

    elif isinstance(event, AgentCompleteEvent):
        state.active_agents.pop(event.agent_id, None)
        state.total_cost_usd += event.total_cost_usd
        state.total_tokens += event.input_tokens + event.output_tokens
        for turn in reversed(state.turns):
            if turn.agent_id == event.agent_id and not turn.is_complete:
                turn.is_complete = True
                turn.total_cost_usd = event.total_cost_usd
                turn.input_tokens = event.input_tokens
                turn.output_tokens = event.output_tokens
                turn.stop_reason = event.stop_reason
                return

    elif isinstance(event, SpinnerTickEvent):
        state.spinner_tick = (state.spinner_tick + 1) % 8


def _find_or_create_turn(
    state: AppState, agent_id: str, agent_name: str
) -> AgentTurnEntry:
    for turn in reversed(state.turns):
        if turn.agent_id == agent_id and not turn.is_complete:
            return turn
    # No open turn — create one implicitly
    turn = AgentTurnEntry(
        agent_id=agent_id,
        agent_name=agent_name,
        started_at=time.monotonic(),
    )
    state.turns.append(turn)
    return turn
```

### 7.3 Spinner Timer

```python
# tui/spinner.py

import asyncio
from .events import SpinnerTickEvent

SPINNER_FRAMES = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]


async def spinner_task(queue: asyncio.Queue, state, interval_ms: int = 100) -> None:
    while True:
        await asyncio.sleep(interval_ms / 1000)
        if state.active_tool_states:   # only tick if tools are running
            queue.put_nowait(SpinnerTickEvent())


def get_spinner_char(tick: int) -> str:
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]
```

### 7.4 Headless Mode

```python
# tui/headless.py

import json
import sys
import time
from dataclasses import asdict


def headless_emit(event) -> None:
    line = json.dumps({
        "ts": time.time(),
        "event_type": type(event).__name__,
        **asdict(event),
    }, default=str)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
```

### 7.5 Menu Overlay System

Each menu is a separate renderer function returning `FormattedText`. The active
menu name is stored in `AppState.active_menu`. The `ConditionalContainer` for the
float tests `app_state.menu_visible`.

```python
# tui/menus.py

from typing import Callable
from .types import AppState

_MENU_RENDERERS: dict[str, Callable[[AppState], list]] = {}

def register_menu(name: str):
    def decorator(fn):
        _MENU_RENDERERS[name] = fn
        return fn
    return decorator

def render_active_menu(app_state: AppState) -> list:
    if not app_state.active_menu:
        return []
    renderer = _MENU_RENDERERS.get(app_state.active_menu)
    return renderer(app_state) if renderer else []

@register_menu("status")
def render_status_menu(state: AppState) -> list:
    lines = [("class:menu-title", " /status — Agent Status\n")]
    for agent_id, agent_name in state.active_agents.items():
        lines.append(("class:menu-item", f"  +-- {agent_name} ({agent_id})  RUNNING\n"))
    if not state.active_agents:
        lines.append(("class:menu-empty", "  (no active agents)\n"))
    return lines

@register_menu("approve")
def render_approve_menu(state: AppState) -> list:
    if not state.pending_approval:
        return [("class:menu-empty", "  No pending approvals.\n")]
    tc = state.pending_approval
    lines = [
        ("class:menu-title", " /approve — Tool Approval Required\n"),
        ("class:menu-item", f"  Tool:  {tc.tool_name}\n"),
        ("class:menu-item", f"  Agent: {tc.agent_id}\n"),
        ("class:menu-item", f"  Args:  {tc.input}\n"),
        ("class:menu-separator", "  ─────────────────────────────\n"),
        ("class:menu-item", "  [Enter] Approve    [n] Deny    [Esc] Dismiss\n"),
    ]
    return lines
```

### 7.6 Phased Implementation Plan

| Phase | Milestone | Est. Effort |
|-------|-----------|-------------|
| P1 | Scaffold `TUIApp`, `AppState`, `UIEvent` types; headless JSON-lines working | 2 days |
| P2 | `TUIEventAdapter` wired to `SignalBus`; `render_loop` mutating `AppState` | 2 days |
| P3 | `prompt_toolkit` layout: `TranscriptWindow`, `StatusLine`, `InputBar` | 3 days |
| P4 | Spinner timer, tool-call state machine, memoized transcript diff | 2 days |
| P5 | Slash-command menus: `/status`, `/approve`, `/history`, `/settings` | 3 days |
| P6 | TOML config loader with live reload; theme support | 1 day |
| P7 | Full test suite (unit + integration + e2e with pyte) | 3 days |
| P8 | Performance tuning to meet < 16 ms P95 render budget | 2 days |

---

## 8. Tests

All tests use `pytest` and `pytest-asyncio`. Run with:

```bash
pytest tests/tui/ -v --tb=short
```

### 8.1 Unit Tests

```python
# tests/tui/test_transcript_render.py

import pytest
import time
from tui.types import AppState, AgentTurnEntry, ToolCallEntry, ToolState
from tui.render import render_transcript, get_spinner_char


def make_state_with_turn(**kwargs) -> AppState:
    state = AppState(session_id="test-session")
    turn = AgentTurnEntry(
        agent_id="agent:planner",
        agent_name="planner",
        started_at=time.monotonic(),
        **kwargs,
    )
    state.turns.append(turn)
    return state


class TestTranscriptSnapshot:
    def test_empty_state_renders_empty(self):
        state = AppState(session_id="test-session")
        result = render_transcript(state)
        assert result == [] or all(text.strip() == "" for _, text in result)

    def test_agent_name_in_output(self):
        state = make_state_with_turn(model_text="Hello world")
        result = render_transcript(state)
        flat = "".join(text for _, text in result)
        assert "planner" in flat

    def test_model_text_in_output(self):
        state = make_state_with_turn(model_text="Analyzing codebase")
        result = render_transcript(state)
        flat = "".join(text for _, text in result)
        assert "Analyzing codebase" in flat

    def test_tool_call_success_shows_checkmark(self):
        state = make_state_with_turn()
        tc = ToolCallEntry(
            tool_use_id="tc-001",
            tool_name="read_file",
            agent_id="agent:planner",
            input={"path": "src/auth.py"},
            state=ToolState.SUCCESS,
            duration_ms=45.2,
        )
        state.turns[0].tool_calls.append(tc)
        result = render_transcript(state)
        flat = "".join(text for _, text in result)
        assert "✓" in flat
        assert "read_file" in flat

    def test_tool_call_failure_shows_cross(self):
        state = make_state_with_turn()
        tc = ToolCallEntry(
            tool_use_id="tc-002",
            tool_name="run_tests",
            agent_id="agent:planner",
            input={},
            state=ToolState.FAILURE,
            error="AssertionError: expected 200 got 500",
        )
        state.turns[0].tool_calls.append(tc)
        result = render_transcript(state)
        flat = "".join(text for _, text in result)
        assert "✗" in flat
        assert "AssertionError" in flat

    def test_tool_call_running_shows_spinner(self):
        state = make_state_with_turn()
        state.spinner_tick = 3
        tc = ToolCallEntry(
            tool_use_id="tc-003",
            tool_name="write_file",
            agent_id="agent:planner",
            input={"path": "src/auth.py"},
            state=ToolState.RUNNING,
        )
        state.turns[0].tool_calls.append(tc)
        state.active_tool_states["tc-003"] = ToolState.RUNNING
        result = render_transcript(state)
        flat = "".join(text for _, text in result)
        # Any braille spinner frame should be present
        assert any(c in flat for c in "⣾⣽⣻⢿⡿⣟⣯⣷")

    def test_completed_turn_shows_cost_footer(self):
        state = make_state_with_turn(
            is_complete=True,
            total_cost_usd=0.003,
            input_tokens=1204,
            output_tokens=387,
            stop_reason="end_turn",
        )
        result = render_transcript(state)
        flat = "".join(text for _, text in result)
        assert "0.003" in flat
        assert "1204" in flat or "1,204" in flat


class TestSpinnerStateMachine:
    def test_spinner_cycles_through_all_frames(self):
        frames = set()
        for tick in range(8):
            frames.add(get_spinner_char(tick))
        assert len(frames) == 8

    def test_spinner_wraps_at_8(self):
        assert get_spinner_char(0) == get_spinner_char(8)
        assert get_spinner_char(1) == get_spinner_char(9)

    def test_spinner_frame_3_is_braille(self):
        char = get_spinner_char(3)
        # All spinner chars are in Braille Patterns block U+2800-U+28FF
        assert 0x2800 <= ord(char) <= 0x28FF

    def test_all_frames_are_braille(self):
        for tick in range(8):
            char = get_spinner_char(tick)
            assert 0x2800 <= ord(char) <= 0x28FF, (
                f"Frame {tick} ('{char}') is not a Braille character"
            )


class TestTranscriptDiffAlgorithm:
    def test_same_state_returns_cached_result(self):
        state = make_state_with_turn(model_text="Hello")
        r1 = render_transcript(state)
        r2 = render_transcript(state)
        # Should be same object (memoized)
        assert r1 is r2

    def test_changed_state_returns_new_result(self):
        state = make_state_with_turn(model_text="Hello")
        r1 = render_transcript(state)
        state.turns[0].model_text += " world"
        state.turns[0].last_delta_ts = time.monotonic() + 0.001
        r2 = render_transcript(state)
        assert r1 is not r2

    def test_spinner_tick_change_busts_cache(self):
        state = make_state_with_turn()
        tc = ToolCallEntry(
            tool_use_id="tc-spin",
            tool_name="write_file",
            agent_id="agent:planner",
            input={},
            state=ToolState.RUNNING,
        )
        state.turns[0].tool_calls.append(tc)
        state.active_tool_states["tc-spin"] = ToolState.RUNNING
        state.spinner_tick = 0
        r1 = render_transcript(state)
        state.spinner_tick = 1
        r2 = render_transcript(state)
        assert r1 is not r2

    def test_new_turn_busts_cache(self):
        state = make_state_with_turn(model_text="Turn 1")
        r1 = render_transcript(state)
        # Add a second turn
        state.turns.append(AgentTurnEntry(
            agent_id="agent:tester",
            agent_name="tester",
            started_at=time.monotonic(),
            model_text="Turn 2",
        ))
        r2 = render_transcript(state)
        assert r1 is not r2
```

### 8.2 Integration Tests

```python
# tests/tui/test_render_pipeline.py

import asyncio
import pytest
import time

from tui.types import AppState, ToolState
from tui.events import (
    ToolStartedEvent, ToolCompleteEvent,
    AgentTurnStartEvent, AgentCompleteEvent,
    ModelTextDeltaEvent,
)
from tui.render_loop import _apply_event


@pytest.fixture
def state():
    return AppState(session_id="integration-test")


class TestLiveUpdateLatency:
    def test_tool_started_apply_under_16ms(self, state):
        event = ToolStartedEvent(
            tool_name="read_file",
            tool_use_id="tc-lat-001",
            agent_id="agent:planner",
            input={"path": "src/auth.py"},
        )
        # Need a turn first
        _apply_event(state, AgentTurnStartEvent(
            agent_id="agent:planner", agent_name="planner"
        ))

        t0 = time.monotonic()
        _apply_event(state, event)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert elapsed_ms < 16, (
            f"_apply_event took {elapsed_ms:.2f}ms, budget is 16ms"
        )

    def test_model_text_delta_under_16ms(self, state):
        _apply_event(state, AgentTurnStartEvent(
            agent_id="agent:x", agent_name="x"
        ))
        event = ModelTextDeltaEvent(
            agent_id="agent:x",
            agent_name="x",
            delta="hello world " * 20,
        )
        t0 = time.monotonic()
        _apply_event(state, event)
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < 16

    def test_100_mixed_events_all_under_16ms(self, state):
        slow_events = []

        # Seed some agent turns
        for i in range(5):
            _apply_event(state, AgentTurnStartEvent(
                agent_id=f"agent-{i}", agent_name=f"agent-{i}"
            ))

        events = [
            ToolStartedEvent(
                tool_name="read_file",
                tool_use_id=f"tc-{i}",
                agent_id=f"agent-{i % 5}",
                input={"path": f"file-{i}.py"},
            )
            for i in range(50)
        ] + [
            ModelTextDeltaEvent(
                agent_id=f"agent-{i % 5}",
                agent_name=f"agent-{i % 5}",
                delta=f"Processing step {i}...",
            )
            for i in range(50)
        ]

        for evt in events:
            t0 = time.monotonic()
            _apply_event(state, evt)
            elapsed_ms = (time.monotonic() - t0) * 1000
            if elapsed_ms > 16:
                slow_events.append((type(evt).__name__, elapsed_ms))

        assert not slow_events, f"Slow events (>16ms): {slow_events}"

    def test_signal_to_state_pipeline_full_lifecycle(self, state):
        """Simulate a complete tool lifecycle: start -> running -> complete."""
        # 1. Start agent turn
        _apply_event(state, AgentTurnStartEvent(
            agent_id="agent:x", agent_name="x"
        ))
        assert "agent:x" in state.active_agents

        # 2. Tool starts
        _apply_event(state, ToolStartedEvent(
            tool_name="write_file",
            tool_use_id="tc-pipe-001",
            agent_id="agent:x",
            input={"path": "out.py"},
        ))
        assert "tc-pipe-001" in state.active_tool_states
        assert state.active_tool_states["tc-pipe-001"] == ToolState.RUNNING
        assert len(state.turns[0].tool_calls) == 1
        assert state.turns[0].tool_calls[0].state == ToolState.RUNNING

        # 3. Tool completes
        _apply_event(state, ToolCompleteEvent(
            tool_name="write_file",
            tool_use_id="tc-pipe-001",
            agent_id="agent:x",
            duration_ms=55.1,
            success=True,
            error=None,
        ))
        assert "tc-pipe-001" not in state.active_tool_states
        assert state.turns[0].tool_calls[0].state == ToolState.SUCCESS
        assert state.turns[0].tool_calls[0].duration_ms == pytest.approx(55.1)

    def test_tool_failure_sets_error_message(self, state):
        _apply_event(state, AgentTurnStartEvent(
            agent_id="agent:tester", agent_name="tester"
        ))
        _apply_event(state, ToolStartedEvent(
            tool_name="run_tests",
            tool_use_id="tc-fail-001",
            agent_id="agent:tester",
            input={},
        ))
        _apply_event(state, ToolCompleteEvent(
            tool_name="run_tests",
            tool_use_id="tc-fail-001",
            agent_id="agent:tester",
            duration_ms=234.5,
            success=False,
            error="AssertionError: expected 200, got 500",
        ))
        tc = state.turns[0].tool_calls[0]
        assert tc.state == ToolState.FAILURE
        assert tc.error == "AssertionError: expected 200, got 500"

    def test_agent_complete_updates_totals(self, state):
        _apply_event(state, AgentTurnStartEvent(
            agent_id="agent:planner", agent_name="planner"
        ))
        assert state.total_cost_usd == pytest.approx(0.0)

        _apply_event(state, AgentCompleteEvent(
            agent_id="agent:planner",
            agent_name="planner",
            turns=3,
            total_cost_usd=0.042,
            input_tokens=1000,
            output_tokens=500,
            stop_reason="end_turn",
        ))
        assert state.total_cost_usd == pytest.approx(0.042)
        assert state.total_tokens == 1500
        assert "agent:planner" not in state.active_agents
        assert state.turns[0].is_complete is True
        assert state.turns[0].stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_queue_drain_processes_all_events(self):
        """Ensure render_loop drains all events from queue."""
        from tui.render_loop import render_loop

        state = AppState(session_id="queue-drain-test")
        queue = asyncio.Queue()
        invalidate_count = [0]

        def mock_invalidate():
            invalidate_count[0] += 1

        # Enqueue 10 events
        for i in range(10):
            queue.put_nowait(AgentTurnStartEvent(
                agent_id=f"agent-{i}", agent_name=f"agent-{i}"
            ))

        # Run loop until queue is empty
        async def stop_when_empty():
            await queue.join()
            raise asyncio.CancelledError

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    render_loop(queue, state, mock_invalidate),
                    stop_when_empty(),
                ),
                timeout=2.0,
            )
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        assert invalidate_count[0] == 10
        assert len(state.turns) == 10
```

### 8.3 End-to-End Tests (pyte VT100 Emulator)

```python
# tests/tui/test_e2e_layout.py
#
# Verifies that the Input Bar stays at the bottom of the terminal
# regardless of how much content is in the transcript.
# Uses `pyte` to emulate a VT100 terminal and read back rendered output.
#
# Dependencies:
#   pip install pyte pytest
#
# The render_full_frame_ansi() function in tui/render.py produces a complete
# ANSI frame for a given AppState, cols, and rows, usable without running
# the full prompt_toolkit event loop.

import pytest
import time

try:
    import pyte
    PYTE_AVAILABLE = True
except ImportError:
    PYTE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not PYTE_AVAILABLE,
    reason="pyte not installed — pip install pyte"
)

COLS = 80
ROWS = 24


def make_virtual_screen():
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    return screen, stream


def render_to_screen(state) -> pyte.Screen:
    from tui.render import render_full_frame_ansi
    screen, stream = make_virtual_screen()
    ansi_output = render_full_frame_ansi(state, cols=COLS, rows=ROWS)
    stream.feed(ansi_output)
    return screen


def row_text(screen: pyte.Screen, row: int) -> str:
    return "".join(screen.buffer[row][col].data for col in range(COLS))


class TestInputBarPosition:
    """Verify Input Bar is always at row ROWS-1 (bottom) of the terminal."""

    def test_input_bar_present_on_bottom_row_empty_state(self):
        from tui.types import AppState
        state = AppState(session_id="e2e-empty")
        screen = render_to_screen(state)
        bottom = row_text(screen, ROWS - 1)
        assert ">" in bottom, (
            f"Expected '>' prompt in bottom row, got: {repr(bottom)}"
        )

    def test_input_bar_stays_at_bottom_with_many_turns(self):
        from tui.types import AppState, AgentTurnEntry

        state = AppState(session_id="e2e-scroll")
        for i in range(30):
            turn = AgentTurnEntry(
                agent_id=f"agent-{i}",
                agent_name=f"agent-{i}",
                started_at=time.monotonic(),
                model_text=f"Turn {i}: processing step {i} of the workflow " + "x" * 30,
                is_complete=True,
            )
            state.turns.append(turn)

        screen = render_to_screen(state)
        bottom = row_text(screen, ROWS - 1)
        assert ">" in bottom, (
            f"Input bar displaced by scroll content. Bottom row: {repr(bottom)}"
        )

    def test_input_bar_stays_at_bottom_with_overlay_active(self):
        from tui.types import AppState

        state = AppState(session_id="e2e-overlay")
        state.menu_visible = True
        state.active_menu = "status"
        for i in range(5):
            state.active_agents[f"agent-{i}"] = f"agent-{i}"

        screen = render_to_screen(state)
        bottom = row_text(screen, ROWS - 1)
        assert ">" in bottom, (
            f"Overlay pushed input bar down. Bottom row: {repr(bottom)}"
        )

    def test_status_line_is_second_to_last_row(self):
        from tui.types import AppState
        state = AppState(session_id="e2e-session-123", total_cost_usd=0.042)
        state.active_agents["agent:planner"] = "planner"

        screen = render_to_screen(state)
        status_row = row_text(screen, ROWS - 2)
        assert "e2e-session-123" in status_row or "$" in status_row, (
            f"Expected status info at row {ROWS-2}, got: {repr(status_row)}"
        )

    def test_overlay_does_not_appear_below_input_bar(self):
        from tui.types import AppState
        state = AppState(session_id="e2e-overlay-pos")
        state.menu_visible = True
        state.active_menu = "status"

        screen = render_to_screen(state)
        # The terminal has ROWS rows indexed 0..ROWS-1.
        # Input bar is at ROWS-1. There is nothing below it.
        # Verify the menu content appears in a row ABOVE the input bar.
        bottom = row_text(screen, ROWS - 1)
        assert ">" in bottom, (
            f"Input bar missing from bottom row when overlay active: {repr(bottom)}"
        )
        # Menu title should appear somewhere above the input bar
        full_screen_text = "\n".join(
            row_text(screen, r) for r in range(ROWS - 1)
        )
        assert "status" in full_screen_text.lower() or "agent" in full_screen_text.lower()


class TestScrollBehavior:
    def test_scrolling_does_not_change_input_bar_row(self):
        from tui.types import AppState, AgentTurnEntry
        from tui.render import render_full_frame_ansi

        state = AppState(session_id="e2e-scroll-3")
        for i in range(50):
            state.turns.append(AgentTurnEntry(
                agent_id="agent:x",
                agent_name="x",
                started_at=time.monotonic(),
                model_text=f"Line {i}: " + "x" * 40,
            ))

        for offset in [0, 10, 20, 30]:
            state.scroll_offset = offset
            ansi = render_full_frame_ansi(state, cols=COLS, rows=ROWS)
            screen, stream = make_virtual_screen()
            stream.feed(ansi)
            bottom = row_text(screen, ROWS - 1)
            assert ">" in bottom, (
                f"Input bar missing at scroll_offset={offset}: {repr(bottom)}"
            )

    def test_transcript_content_changes_on_scroll(self):
        from tui.types import AppState, AgentTurnEntry
        from tui.render import render_full_frame_ansi

        state = AppState(session_id="e2e-scroll-diff")
        for i in range(60):
            state.turns.append(AgentTurnEntry(
                agent_id="agent:x",
                agent_name="x",
                started_at=time.monotonic(),
                model_text=f"Unique marker TURN_{i:03d}",
            ))

        # Get frame at offset 0
        state.scroll_offset = 0
        ansi0 = render_full_frame_ansi(state, cols=COLS, rows=ROWS)
        screen0, stream0 = make_virtual_screen()
        stream0.feed(ansi0)
        text0 = "\n".join(row_text(screen0, r) for r in range(ROWS - 2))

        # Get frame at offset 30
        state.scroll_offset = 30
        ansi30 = render_full_frame_ansi(state, cols=COLS, rows=ROWS)
        screen30, stream30 = make_virtual_screen()
        stream30.feed(ansi30)
        text30 = "\n".join(row_text(screen30, r) for r in range(ROWS - 2))

        # Different scroll positions should show different transcript content
        assert text0 != text30, "Scrolling had no effect on transcript content"


class TestHeadlessMode:
    def test_headless_emit_produces_json_line(self, capsys):
        from tui.headless import headless_emit
        from tui.events import AgentTurnStartEvent

        event = AgentTurnStartEvent(agent_id="agent:x", agent_name="x")
        headless_emit(event)

        captured = capsys.readouterr()
        import json
        line = captured.out.strip()
        data = json.loads(line)
        assert data["event_type"] == "AgentTurnStartEvent"
        assert data["agent_id"] == "agent:x"
        assert "ts" in data

    def test_headless_emit_is_valid_json_for_all_event_types(self, capsys):
        from tui.headless import headless_emit
        from tui.events import (
            ToolStartedEvent, ToolCompleteEvent,
            ModelTextDeltaEvent, AgentCompleteEvent, SpinnerTickEvent,
        )
        import json

        events = [
            ToolStartedEvent("read_file", "tc-1", "agent:x", {"path": "f.py"}),
            ToolCompleteEvent("read_file", "tc-1", "agent:x", 12.3, True, None),
            ModelTextDeltaEvent("agent:x", "x", "hello"),
            AgentCompleteEvent("agent:x", "x", 3, 0.01, 100, 50, "end_turn"),
            SpinnerTickEvent(),
        ]
        for evt in events:
            headless_emit(evt)

        lines = capsys.readouterr().out.strip().split("\n")
        assert len(lines) == len(events)
        for line in lines:
            data = json.loads(line)  # must not raise
            assert "event_type" in data
            assert "ts" in data
```

---

## 9. Configuration Reference

### 9.1 TOML Schema

```toml
# ~/.config/lauren-ai/tui.toml

[tui]
# Visual theme. One of: "dark", "light", "solarized", "monokai"
theme = "dark"

# Target frame budget in milliseconds. Lower = more responsive, higher CPU.
# Default 16ms = 60fps. Use 33ms for ~30fps on slower terminals.
refresh_rate_ms = 16

# When true, suppress TUI and emit JSON-lines to stdout.
# Auto-detected if stdout is not a TTY.
headless = false

# Interval between spinner animation frames in milliseconds.
spinner_interval_ms = 100

# Maximum number of transcript lines to keep in memory.
# Oldest lines are dropped when this limit is reached.
max_transcript_lines = 10000

# Show per-turn token counts in the transcript footer.
show_token_counts = true

# Show per-turn and running-total cost in transcript footer.
show_cost = true

# The prompt string shown before the cursor in the Input Bar.
input_bar_prompt = "> "

# Maximum height of menu overlays in rows.
menu_max_height = 20

# Maximum number of events stored in the /history log.
history_max_events = 500

# Color each agent's bullet with a unique hue derived from agent_id.
color_agents = true

# Wrap long tool argument strings across multiple lines.
wrap_tool_args = true

# Optional file path for writing JSON-lines alongside the TUI.
# Set to "" to disable.
log_file = ""

[tui.theme.dark]
# prompt_toolkit style class overrides for the dark theme.
background         = "bg:#1e1e2e"
transcript_bg      = "bg:#1e1e2e"
statusline_bg      = "bg:#313244"
statusline_fg      = "#cdd6f4"
input_bar_bg       = "bg:#1e1e2e"
input_bar_fg       = "#cdd6f4"
prompt_fg          = "#89b4fa"
agent_bullet       = "#a6e3a1"
model_text         = "#cdd6f4"
tool_label         = "#89dceb"
tool_success       = "#a6e3a1"
tool_failure       = "#f38ba8"
tool_spinner       = "#fab387"
tool_pending       = "#6c7086"
separator          = "#45475a"
menu_bg            = "bg:#313244"
menu_title         = "#89b4fa bold"
menu_item          = "#cdd6f4"
menu_empty         = "#6c7086 italic"
cost_footer        = "#6c7086"

[tui.theme.light]
background         = "bg:#eff1f5"
transcript_bg      = "bg:#eff1f5"
statusline_bg      = "bg:#bcc0cc"
statusline_fg      = "#4c4f69"
input_bar_bg       = "bg:#eff1f5"
input_bar_fg       = "#4c4f69"
prompt_fg          = "#1e66f5"
agent_bullet       = "#40a02b"
model_text         = "#4c4f69"
tool_label         = "#04a5e5"
tool_success       = "#40a02b"
tool_failure       = "#d20f39"
tool_spinner       = "#fe640b"
tool_pending       = "#9ca0b0"
separator          = "#bcc0cc"
menu_bg            = "bg:#dce0e8"
menu_title         = "#1e66f5 bold"
menu_item          = "#4c4f69"
menu_empty         = "#9ca0b0 italic"
cost_footer        = "#9ca0b0"
```

### 9.2 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LAUREN_TUI_HEADLESS` | `0` | Set to `1` to force headless mode |
| `LAUREN_TUI_THEME` | `dark` | Override theme without editing TOML |
| `LAUREN_TUI_REFRESH_MS` | `16` | Override refresh rate |
| `LAUREN_TUI_LOG` | *(unset)* | Path to JSON-lines log file |
| `LAUREN_TUI_NO_COLOR` | `0` | Disable all ANSI color output |
| `NO_COLOR` | *(unset)* | Standard `NO_COLOR` convention respected |

### 9.3 Live Configuration Reload

The `/settings` menu renders the current `tui.toml` in an editable pane. On `Ctrl-S`
(save) or `Enter` (when cursor is on the save button), the file is written and
`TUIConfig` is hot-reloaded. Only visual settings (`theme`, `show_cost`,
`show_token_counts`, `wrap_tool_args`, `color_agents`) take effect immediately.
Changes to `refresh_rate_ms` and `headless` require a restart and are flagged with
a "(requires restart)" annotation in the menu.

---

## 10. Open Questions

| # | Question | Owner | Status |
|---|----------|-------|--------|
| OQ-1 | Should `/approve` block the approving agent's execution (suspend the coroutine) or queue the decision and resume async? Blocking is simpler but could deadlock if the operator is slow to respond. | Eng | Open |
| OQ-2 | How should the transcript behave when `max_transcript_lines` is reached? Options: (a) drop oldest turn, (b) truncate within a turn, (c) paginate to an external file. | Product | Open |
| OQ-3 | The `ModelCallStarted` signal carries `messages_count` and `input_tokens_estimate`. Is the estimate accurate enough to show a "thinking..." cost prediction, or would it mislead users? | ML | Open |
| OQ-4 | `color_agents = true` derives hue from `agent_id` hash. Should colors be stable across sessions (deterministic hash) or remapped each run? | Design | Open |
| OQ-5 | Does `prompt_toolkit`'s `Float` anchoring (`bottom=2`) behave correctly on terminals where the TUI does not fill the full height (e.g., a small tmux pane)? Needs testing on sub-24-row terminals. | Eng | Open |
| OQ-6 | The `/history` menu stores up to `history_max_events` events in memory. Should this be backed by the SQLite event store from PRD-05, or remain an in-process ring buffer? | Arch | Open |
| OQ-7 | For the pyte e2e tests, `render_full_frame_ansi()` must produce a complete ANSI frame from `AppState` without running the full `prompt_toolkit` event loop. Is a separate "offline renderer" warranted, or should we use `prompt_toolkit`'s headless output renderer? | Eng | Open |
| OQ-8 | Should the Input Bar support multi-line input (e.g., pasting a code block)? If so, how does the fixed-height constraint interact with a growing buffer? | Product | Open |
| OQ-9 | Accessibility: are Braille spinner characters rendered correctly in screen readers or terminals with non-standard font support? Should we provide a `--ascii-spinner` fallback (`-/|\`)? | Design | Open |
| OQ-10 | The headless JSON-lines format is currently ad-hoc (`dataclasses.asdict` + event type). Should it follow the OpenTelemetry log record schema to ease integration with existing log pipelines? | Platform | Open |

---

## Appendix A: Dependency Matrix

| Dependency | Version | Purpose |
|------------|---------|---------|
| `prompt_toolkit` | `>=3.0.43` | TUI rendering, layout, key bindings |
| `pyte` | `>=0.8.0` | VT100 emulation for e2e tests |
| `pytest` | `>=8.0` | Test runner |
| `pytest-asyncio` | `>=0.23` | Async test support |
| `tomllib` / `tomli` | stdlib (3.11+) / `tomli>=2.0` | TOML config parsing |
| `lauren_ai._signals` | internal | `SignalBus` and signal types |

---

## Appendix B: File Layout

```
tui/
+-- __init__.py
+-- app.py            # TUIApp class, prompt_toolkit Application wiring
+-- adapter.py        # TUIEventAdapter (SignalBus -> UIEvent queue)
+-- config.py         # TUIConfig dataclass, TOML loader
+-- events.py         # UIEvent union types
+-- headless.py       # headless_emit() JSON-lines writer
+-- menus.py          # Menu registry and renderers
+-- render.py         # render_transcript(), render_status_line(), render_full_frame_ansi()
+-- render_loop.py    # render_loop(), _apply_event()
+-- spinner.py        # spinner_task(), get_spinner_char()
+-- types.py          # AppState, AgentTurnEntry, ToolCallEntry, ToolState

tests/tui/
+-- __init__.py
+-- test_transcript_render.py   # Unit: snapshot, spinner state machine, diff cache
+-- test_render_pipeline.py     # Integration: latency, signal->state pipeline
+-- test_e2e_layout.py          # E2E: pyte VT100, Input Bar position invariant
```

---

*End of PRD-06*
