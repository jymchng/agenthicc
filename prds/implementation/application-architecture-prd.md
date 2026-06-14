# Application Architecture PRD — AgentHICC TUI Redesign
## Implementation-Ready Engineering Specification

**Version:** 1.0  
**Date:** 2026-06-13  
**Consuming agents:** autonomous coding agents; no clarification available  
**Hard constraint:** `App.run(inline=True)` ONLY — no alternate screen ever  

---

## 0. Scope and Relationship to Other Documents

This document is the authoritative implementation specification for the TUI layer of
AgentHICC. It synthesises the master TUI redesign PRD, the non-alternate-screen
architecture reference, and the Textual inline-mode research into a single artefact
that a coding agent can implement without consulting any other document.

References (read-only; do not modify):
- `prds/tui-redesign-prd.md` — user flows, visual design, component specs
- `prds/non-alternate-screen-architecture.md` — erase-sequence patterns, FakeTerminal
- `prds/textual-architecture-research.md` — Textual inline mode, widget catalogue

Existing source files that this PRD extends or replaces:
- `src/agenthicc/__main__.py` — startup and entry point (extend)
- `src/agenthicc/config.py` — AgenthiccConfig (extend, add TUI settings)
- `src/agenthicc/kernel/` — event types (extend with new UI event types)
- `src/agenthicc/runtime/comm_tools.py` — CommunicationTools (read-only)

New package created by this PRD: `src/agenthicc/tui/`

---

## 1. Package Layout

Exact directory structure. Every file is listed with its single-sentence purpose.
No file may be omitted; no extra file may be created without updating this PRD.

```
src/agenthicc/tui/
  __init__.py              Re-exports: AgenthiccApp, run_tui, run_headless,
                           Terminal, FakeTerminal, TranscriptModel,
                           RenderLoop, FrameComposer, InputState, Frame
  app.py                   AgenthiccApp (Textual App subclass), run_tui(),
                           run_headless(), AppModel dataclass, startup sequence
  terminal.py              Terminal (single stdout owner), FakeTerminal (test double),
                           Size, TerminalCapabilities, Frame dataclasses
  frame_composer.py        FrameComposer class: pure function (AppModel, Size) → Frame;
                           _render_status_bar, _render_divider, _render_input,
                           _render_footer, _render_streaming, _render_dropdown
  render_loop.py           RenderLoop: asyncio.Task-driven 50ms debounce loop;
                           force_commit(), request_redraw(), shutdown()
  input_state.py           InputState pure state machine; DropdownState; TriggerType enum;
                           kill ring; history navigation; @mention and /command detection
  transcript.py            TranscriptModel (mutable), AgentTurnEntry, ToolCallEntry,
                           TurnState, ToolCallState, render_turn_to_lines(),
                           render_tool_call_line(); memory eviction
  event_adapter.py         TUIEventAdapter: subscribes to EventProcessor queue,
                           translates AppState diffs into TranscriptModel mutations
  approval_gate.py         ApprovalGate: manages approval state machine,
                           renders bottom-block approval UI, emits decision events
  doom_loop.py             DoomLoopDetector: tracks (tool_name, args_hash) repetitions,
                           fires after 3 identical calls; DoomLoopState dataclass
  session_recap.py         SessionRecapGenerator: produces compact recap lines
                           from AgentTurnEntry metadata; no LLM call
  symbols.py               All Unicode/ASCII symbol constants; _unicode_safe();
                           SPINNER_FRAMES; AGENT_COLORS; MODE_SYMBOLS; MODE_COLORS
  color.py                 ANSIColor dataclass; ColorPalette; color_for_depth();
                           strip_ansi(); clip_ansi_line(); wcswidth wrapper
  markdown_renderer.py     render_markdown_to_lines(): Rich Console → list[str];
                           always force_terminal=True; never alternate screen
  styles/
    app.tcss               Root Textual CSS: Screen height, CSS variables, themes
    layout.tcss            Dock, grid, size rules for the bottom block widgets
    widgets.tcss           Widget-specific component styles
    inline.tcss            :inline pseudo-selector overrides

tests/unit/
  test_terminal.py         Unit tests for Terminal and FakeTerminal (50+ cases)
  test_frame_composer.py   Unit tests for FrameComposer (40+ cases)
  test_render_loop.py      Unit tests for RenderLoop (15+ cases)
  test_input_state.py      Unit tests for InputState (20+ cases)
  test_tui_transcript.py   Unit tests for TranscriptModel (20+ cases; extends existing)
  test_approval_gate.py    Unit tests for ApprovalGate (15+ cases)
  test_doom_loop.py        Unit tests for DoomLoopDetector (10+ cases)
  test_symbols.py          Unit tests for symbol/color helpers (10+ cases)

tests/integration/
  test_tui_rendering.py    Pyte-based integration tests (20+ cases)

tests/e2e/
  test_tui_e2e.py          Full-session E2E tests using FakeTerminal (10+ cases)
```

---

## 2. Module Boundaries and Dependency Rules

### 2.1 Layering Diagram

```
Layer 0: Pure data / no dependencies
  symbols.py
  color.py
  input_state.py (no TUI imports)

Layer 1: Domain models (import only Layer 0)
  transcript.py    → symbols, color
  terminal.py      → symbols, color

Layer 2: Composition (import Layer 0+1)
  frame_composer.py → transcript, input_state, terminal, symbols, color
  markdown_renderer.py → color (and Rich; never Textual)

Layer 3: Runtime orchestration (import Layer 0+1+2)
  render_loop.py   → terminal, frame_composer, transcript, input_state
  approval_gate.py → transcript, symbols, color
  doom_loop.py     → transcript (ToolCallEntry only)
  session_recap.py → transcript (AgentTurnEntry only)

Layer 4: Kernel bridge (import Layer 0+1+2+3 + kernel package)
  event_adapter.py → transcript, render_loop, approval_gate, doom_loop
                      kernel.events, kernel.state

Layer 5: Application entry point (import everything)
  app.py           → all tui.* modules; kernel.*; config; runtime.*
  __init__.py      → re-exports from app.py, terminal.py, transcript.py
```

### 2.2 Forbidden Import Paths

The following imports are explicitly forbidden (will fail code review):

| From | May NOT import |
|------|---------------|
| `transcript.py` | `terminal.py`, `render_loop.py`, `app.py`, `event_adapter.py` |
| `frame_composer.py` | `render_loop.py`, `app.py`, `event_adapter.py`, `kernel.*` |
| `input_state.py` | Any `tui.*` module except `symbols.py` and `color.py` |
| `terminal.py` | `render_loop.py`, `frame_composer.py`, `app.py`, `kernel.*` |
| `doom_loop.py` | `terminal.py`, `render_loop.py`, `frame_composer.py`, `app.py` |
| `session_recap.py` | `terminal.py`, `render_loop.py`, `frame_composer.py`, `app.py` |
| Any `tui.*` | `tui.app` (except `app.py` itself) — prevents circular imports |

### 2.3 Kernel Package Rules

- `event_adapter.py` is the ONLY file in `tui/` that imports from `kernel/`, `runtime/`, or `memory/`
- `app.py` imports `event_adapter.py` and kernel packages directly (it is the root)
- No `tui/` module below Layer 4 touches the kernel event system

### 2.4 stdout Ownership Rule

`terminal.py:Terminal` is the SOLE owner of `sys.stdout` (fd 1). The rule is enforced
at construction time by passing `fd=1` (default). Every other module that needs to
write to the terminal calls methods on the `Terminal` instance. This includes:
- `render_loop.py` calls `terminal.commit_lines()` and `terminal.set_bottom()`
- `app.py` calls `terminal.clear_bottom()` and `terminal.commit_lines()` only during
  the `suspend()`/`resume()` cycle
- No `print()` or `sys.stdout.write()` calls anywhere in `tui/` except `terminal.py`

---

## 3. State Architecture

### 3.1 Global AppModel

`AppModel` is the TUI-specific view of application state. It is NOT the kernel's
`AppState`. It is a mutable dataclass populated by `TUIEventAdapter` and read by
`FrameComposer`. It lives in `app.py`.

```python
# src/agenthicc/tui/app.py
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcript import TranscriptModel
    from .input_state import InputState


@dataclass
class StatusState:
    """Live status bar data. Mutated by TUIEventAdapter."""
    active: bool = False                  # True while an agent turn is in progress
    spinner_frame: int = 0                # Advances every 100ms during active turn
    intent_started_at: float = 0.0       # monotonic time when current intent started
    model_name: str = ""                  # e.g. "claude-sonnet-4-6"
    provider: str = ""                    # e.g. "anthropic"
    agent_count: int = 0                  # number of active agents
    input_tokens: int = 0                 # cumulative session input tokens
    output_tokens: int = 0                # cumulative session output tokens
    session_cost_usd: float = 0.0        # cumulative session cost in USD
    session_id: str = ""                  # truncated session UUID (first 8 chars)
    permission_mode: str = "AUTO"         # one of AUTO/PLAN/ASK/REVIEW/SAFE/DEBUG
    background_count: int = 0            # number of backgrounded tasks
    error_banner: str | None = None      # critical error text; None = no banner
    doom_loop_active: bool = False       # True when doom loop detector fired


@dataclass
class AppModel:
    """Single mutable TUI state container. Never frozen."""
    status: StatusState = field(default_factory=StatusState)
    # transcript and input_state are stored separately (see Section 3.5)
    # AppModel holds references to them
    transcript: "TranscriptModel | None" = None
    input_state: "InputState | None" = None
    shutdown_requested: bool = False
    session_name: str = ""
    approval_pending: bool = False
    resume_session_id: str | None = None
```

**Mutation rules:**
- Only `TUIEventAdapter` mutates `AppModel.status` fields
- Only `InputState` methods mutate input buffer / cursor
- Only `TranscriptModel` methods mutate turns / tool calls
- `app.py` sets `shutdown_requested = True` on SIGTERM/SIGINT

### 3.2 Session State

Session lifecycle states (not a formal enum; tracked via `AppModel.status`):

| State | Condition | Stored Where |
|-------|-----------|--------------|
| `created` | `session_id` assigned, `events.jsonl` opened | `AppModel.status.session_id` |
| `active` | At least one intent submitted | `AppModel.status.active` implicitly |
| `persisted` | `events.jsonl` flushed on SIGTERM/SIGHUP | `EventProcessor` WAL |
| `resumed` | `--resume SESSION_ID` flag used | `AppModel.resume_session_id` |

Per-session stored data (path: `.agenthicc/sessions/{session_id}/`):
```python
@dataclass
class SessionMetadata:
    session_id: str          # UUID4 first 8 chars
    session_name: str        # user-supplied via --session flag
    cwd: str                 # os.getcwd() at start
    created_at: float        # time.time()
    last_used_at: float      # updated on each interaction
    turn_count: int          # number of committed turns
    total_cost_usd: float    # cumulative cost
    model_name: str          # last model used
    events_path: str         # absolute path to events.jsonl
```

The `SessionMetadata` is written to `sessions/{session_id}/metadata.json` on first
write and updated on each turn completion. `events.jsonl` is written incrementally
by `EventProcessor.run()`.

### 3.3 Agent State

`AgentState` is represented by `TurnState` in `transcript.py` (the per-turn view) and
`StatusState.active` / `StatusState.agent_count` (the global view).

```python
# src/agenthicc/tui/transcript.py
from enum import Enum, auto

class TurnState(Enum):
    STREAMING = auto()    # Agent is producing output; spinner shows
    COMPLETE = auto()     # Turn finished successfully; committed to scrollback
    CANCELLED = auto()    # User cancelled with Ctrl+C; partial text committed
    ERROR = auto()        # Agent returned an error; error committed to scrollback

    # Transitions (validated in TranscriptModel.transition_turn()):
    # STREAMING → COMPLETE  (on stop_reason received)
    # STREAMING → CANCELLED (on Ctrl+C SIGINT)
    # STREAMING → ERROR     (on unhandled exception in agent runner)
    # COMPLETE  → (no transitions; terminal state)
    # CANCELLED → (no transitions; terminal state)
    # ERROR     → (no transitions; terminal state)
```

State transition conditions:

| From | To | Condition | Side Effect |
|------|----|-----------|-------------|
| STREAMING | COMPLETE | `EventType.AGENT_TURN_COMPLETE` received | `render_loop.force_commit(lines)` |
| STREAMING | CANCELLED | `EventType.TURN_CANCELLED` received | commit partial text with `[cancelled]` suffix |
| STREAMING | ERROR | `EventType.AGENT_ERROR` received | commit error line, set `status.error_banner` |

### 3.4 Tool State

```python
# src/agenthicc/tui/transcript.py

class ToolCallState(Enum):
    PENDING = auto()           # Tool call emitted by agent, not yet dispatched
    RUNNING = auto()           # ToolExecutor has started; spinner in bottom block
    SUCCESS = auto()           # Tool returned a result; committed line shows ✓
    ERROR = auto()             # Tool returned an error; committed line shows ✗
    APPROVAL_NEEDED = auto()   # Approval gate is blocking this tool call

    # Transitions:
    # PENDING          → RUNNING          (on ToolCallStarted event)
    # PENDING          → APPROVAL_NEEDED  (on ApprovalRequired event)
    # APPROVAL_NEEDED  → RUNNING          (on ApprovalGranted event)
    # APPROVAL_NEEDED  → ERROR            (on ApprovalDenied event)
    # RUNNING          → SUCCESS          (on ToolCallComplete, success=True)
    # RUNNING          → ERROR            (on ToolCallComplete, success=False)
    # SUCCESS / ERROR  → (terminal states)


@dataclass
class ToolCallEntry:
    tool_id: str                          # UUID from kernel ToolCallStarted event
    tool_name: str                        # e.g. "read_file"
    args: dict[str, object]              # raw args dict from kernel event
    state: ToolCallState = ToolCallState.PENDING
    result_summary: str = ""             # e.g. "142 lines" or "exit code 1"
    duration_ms: int = 0                 # wall-clock time from start to finish
    error_message: str = ""             # only set when state == ERROR
    output_lines: list[str] = field(default_factory=list)  # expanded output
    expanded: bool = False               # True if user has expanded with Ctrl+E
    args_hash: str = ""                  # sha1 of sorted args for doom loop detection
```

### 3.5 UI State

```python
# src/agenthicc/tui/input_state.py

from enum import Enum, auto
from dataclasses import dataclass, field


class TriggerType(Enum):
    NONE = auto()
    AT_MENTION = auto()
    SLASH_COMMAND = auto()


@dataclass
class DropdownState:
    open: bool = False
    trigger: TriggerType = TriggerType.NONE
    items: list[str] = field(default_factory=list)
    item_descriptions: list[str] = field(default_factory=list)  # parallel list
    selected_index: int = 0
    filter_text: str = ""

    def next_item(self) -> None:
        if self.items:
            self.selected_index = (self.selected_index + 1) % len(self.items)

    def prev_item(self) -> None:
        if self.items:
            self.selected_index = (self.selected_index - 1) % len(self.items)

    def selected_item(self) -> str | None:
        if self.open and self.items:
            return self.items[self.selected_index]
        return None


class InputMode(Enum):
    NORMAL = auto()         # Standard typing
    APPROVAL = auto()       # Approval gate active: y/n/a keys only
    DOOM_LOOP = auto()      # Doom loop response: c/r/i keys only
    LOCKED = auto()         # Agent turn in progress; input disabled except Ctrl+C/B


@dataclass
class ViewState:
    """Current visual state of the full TUI bottom block."""
    mode: InputMode = InputMode.NORMAL
    scroll_offset: int = 0        # Reserved for future diff scrolling
    terminal_rows: int = 24       # Updated on SIGWINCH
    terminal_cols: int = 80       # Updated on SIGWINCH
    min_cols: int = 60            # Minimum supported width
    min_rows: int = 12            # Minimum supported height
    too_small: bool = False       # True if terminal is below minimum size
```

`InputState` is the full mutable state of the input bar:

```python
# src/agenthicc/tui/input_state.py

class InputState:
    def __init__(self, on_submit: Callable[[str], Awaitable[None]]) -> None:
        self._text: str = ""
        self._cursor: int = 0                     # byte offset into _text
        self._history: list[str] = []             # submitted messages, oldest first
        self._history_index: int = -1             # -1 = not navigating history
        self._dropdown: DropdownState = DropdownState()
        self._on_submit: Callable[[str], Awaitable[None]] = on_submit
        self._kill_ring: list[str] = []           # Ctrl+K / Ctrl+U killed text
        self._disabled: bool = False              # True during agent turn
        self._mode: InputMode = InputMode.NORMAL

    # Properties
    @property
    def text(self) -> str: ...
    @property
    def cursor(self) -> int: ...
    @property
    def dropdown(self) -> DropdownState: ...
    @property
    def dropdown_open(self) -> bool: ...
    @property
    def mode(self) -> InputMode: ...
    @property
    def disabled(self) -> bool: ...

    # Mutations — all return None; each calls _check_triggers() after modifying text
    def insert(self, char: str) -> None: ...
    def backspace(self) -> None: ...
    def delete_forward(self) -> None: ...
    def move_left(self) -> None: ...
    def move_right(self) -> None: ...
    def move_word_left(self) -> None: ...
    def move_word_right(self) -> None: ...
    def move_to_start(self) -> None: ...
    def move_to_end(self) -> None: ...
    def kill_to_end(self) -> None: ...        # stores in kill ring
    def kill_to_start(self) -> None: ...      # stores in kill ring
    def kill_word_back(self) -> None: ...     # stores in kill ring
    def yank(self) -> None: ...              # inserts from kill ring
    def history_up(self) -> None: ...
    def history_down(self) -> None: ...
    def insert_newline(self) -> None: ...    # Shift+Enter / Alt+Enter
    def set_mode(self, mode: InputMode) -> None: ...
    def set_disabled(self, disabled: bool) -> None: ...
    def close_dropdown(self) -> None: ...
    def dropdown_next(self) -> None: ...
    def dropdown_prev(self) -> None: ...
    def select_dropdown_item(self) -> None: ...
    async def submit(self) -> None: ...       # validates, clears text, calls on_submit

    # Trigger detection (called after every text/cursor change)
    def _check_triggers(self) -> None: ...
    def _apply_completion(self, item: str) -> None: ...

    # Rendering
    def render_lines(self, prompt_glyph: str, width: int, color: bool = True) -> list[str]: ...
```

---

## 4. Event System

### 4.1 New TUI-Specific Event Types

These are additions to the existing `kernel/events.py` `EventType` enumeration.
Existing event types are preserved unchanged.

```python
# src/agenthicc/kernel/events.py  (additions only)

# Add to EventType enum:
class EventType(str, Enum):
    # ... existing types preserved ...

    # TUI-specific events (prefixed TUI_)
    TUI_TOKEN_STREAM       = "tui_token_stream"        # new LLM token
    TUI_TURN_START         = "tui_turn_start"          # agent turn began
    TUI_TURN_COMPLETE      = "tui_turn_complete"       # agent turn ended successfully
    TUI_TURN_CANCELLED     = "tui_turn_cancelled"      # Ctrl+C cancelled turn
    TUI_TOOL_CALL_START    = "tui_tool_call_start"     # tool executor started
    TUI_TOOL_CALL_COMPLETE = "tui_tool_call_complete"  # tool executor finished
    TUI_APPROVAL_REQUIRED  = "tui_approval_required"   # approval gate needed
    TUI_APPROVAL_RESOLVED  = "tui_approval_resolved"   # user approved/denied
    TUI_DOOM_LOOP_DETECTED = "tui_doom_loop_detected"  # doom loop fired
    TUI_COST_UPDATE        = "tui_cost_update"         # token/cost counters changed
    TUI_MODE_CHANGED       = "tui_mode_changed"        # permission mode changed
    TUI_AGENT_SPAWNED      = "tui_agent_spawned"       # parallel agent started
    TUI_AGENT_COMPLETED    = "tui_agent_completed"     # parallel agent finished
    TUI_SESSION_RECAP      = "tui_session_recap"       # idle recap lines committed
    TUI_ERROR_BANNER       = "tui_error_banner"        # critical error needs display
    TUI_ERROR_CLEARED      = "tui_error_cleared"       # error banner dismissed
```

### 4.2 Event Payload Dataclasses

All TUI event payloads are typed dataclasses serialisable to dict for `Event.payload`.

```python
# src/agenthicc/tui/event_adapter.py

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class TUITokenStreamPayload:
    turn_id: str          # matches AgentTurnEntry.turn_id
    agent_id: str
    token: str            # the new text chunk

@dataclass
class TUITurnStartPayload:
    turn_id: str
    agent_id: str
    agent_name: str
    timestamp: float      # time.time()

@dataclass
class TUITurnCompletePayload:
    turn_id: str
    agent_id: str
    final_text: str       # complete turn text (Markdown)
    duration_ms: int

@dataclass
class TUITurnCancelledPayload:
    turn_id: str
    agent_id: str
    partial_text: str     # text accumulated before cancellation

@dataclass
class TUIToolCallStartPayload:
    tool_id: str          # UUID
    turn_id: str
    tool_name: str
    args: dict[str, object]
    args_hash: str        # sha1 of sorted repr(args)

@dataclass
class TUIToolCallCompletePayload:
    tool_id: str
    turn_id: str
    success: bool
    result_summary: str   # e.g. "142 lines" or "exit code 1"
    duration_ms: int
    error_message: str    # empty string on success

@dataclass
class TUIApprovalRequiredPayload:
    tool_id: str
    turn_id: str
    tool_name: str
    args: dict[str, object]
    proposed_diff: str    # unified diff text, empty if not a file write

@dataclass
class TUIApprovalResolvedPayload:
    tool_id: str
    decision: str         # "approved", "denied", "allow_all"

@dataclass
class TUIDoomLoopPayload:
    turn_id: str
    tool_name: str
    args: dict[str, object]
    repetition_count: int  # will be 3 on first fire

@dataclass
class TUICostUpdatePayload:
    input_tokens_delta: int
    output_tokens_delta: int
    cost_usd_delta: float

@dataclass
class TUIModeChangedPayload:
    new_mode: str          # one of AUTO/PLAN/ASK/REVIEW/SAFE/DEBUG
    old_mode: str

@dataclass
class TUIAgentSpawnedPayload:
    agent_id: str
    agent_name: str
    parent_agent_id: str | None

@dataclass
class TUIAgentCompletedPayload:
    agent_id: str
    agent_name: str

@dataclass
class TUIErrorBannerPayload:
    severity: str          # "critical" or "warning"
    message: str
    retry_after_seconds: int  # 0 = no auto-retry

@dataclass
class TUIErrorClearedPayload:
    pass  # no fields needed
```

### 4.3 Event Ownership

| Event Type | Emitted By | Consumed By |
|------------|-----------|-------------|
| `TUI_TOKEN_STREAM` | `AgentRunner` (via `CommunicationTools`) | `TUIEventAdapter` → `TranscriptModel` |
| `TUI_TURN_START` | `AgentRunner` | `TUIEventAdapter` → `TranscriptModel`, `StatusState` |
| `TUI_TURN_COMPLETE` | `AgentRunner` | `TUIEventAdapter` → `TranscriptModel`, `RenderLoop.force_commit()` |
| `TUI_TURN_CANCELLED` | SIGINT handler in `app.py` | `TUIEventAdapter` → `TranscriptModel` |
| `TUI_TOOL_CALL_START` | `ToolExecutor` | `TUIEventAdapter` → `TranscriptModel`, `DoomLoopDetector` |
| `TUI_TOOL_CALL_COMPLETE` | `ToolExecutor` | `TUIEventAdapter` → `TranscriptModel` |
| `TUI_APPROVAL_REQUIRED` | `SecurityPolicy` check in `ToolExecutor` | `TUIEventAdapter` → `ApprovalGate`, `TranscriptModel` |
| `TUI_APPROVAL_RESOLVED` | `ApprovalGate` (keyboard input) | `TUIEventAdapter` → `EventProcessor` (re-emits as kernel event) |
| `TUI_DOOM_LOOP_DETECTED` | `DoomLoopDetector` | `TUIEventAdapter` → `TranscriptModel`, `InputState` mode change |
| `TUI_COST_UPDATE` | `AgentRunner` | `TUIEventAdapter` → `StatusState` |
| `TUI_MODE_CHANGED` | `InputState` (Shift+Tab) | `TUIEventAdapter` → `StatusState`, `SecurityPolicy` |
| `TUI_AGENT_SPAWNED` | `Scheduler` | `TUIEventAdapter` → `StatusState`, `TranscriptModel` |
| `TUI_AGENT_COMPLETED` | `Scheduler` | `TUIEventAdapter` → `StatusState` |
| `TUI_ERROR_BANNER` | `AgentRunner` (on LLM API error) | `TUIEventAdapter` → `StatusState.error_banner` |
| `TUI_ERROR_CLEARED` | `ApprovalGate` or keyboard (user dismissed) | `TUIEventAdapter` → `StatusState.error_banner = None` |

### 4.4 Event Propagation

```
Kernel EventProcessor (MPSC queue)
    │
    │  processor.subscribe() returns asyncio.Queue[AppState]
    │
    ▼
TUIEventAdapter._adapter_loop()   [asyncio.Task, started in app.py on_mount()]
    │
    │  Translates AppState diffs into TranscriptModel + StatusState mutations
    │  All mutations happen on the asyncio event loop (single-threaded)
    │
    ├─▶ TranscriptModel.add_turn()          (on TUI_TURN_START)
    ├─▶ TranscriptModel.append_streaming_token()  (on TUI_TOKEN_STREAM)
    ├─▶ TranscriptModel.complete_turn()     (on TUI_TURN_COMPLETE)
    ├─▶ TranscriptModel.add_tool_call()     (on TUI_TOOL_CALL_START)
    ├─▶ TranscriptModel.update_tool_call()  (on TUI_TOOL_CALL_COMPLETE)
    ├─▶ RenderLoop.request_redraw()         (on any mutation)
    ├─▶ RenderLoop.force_commit(lines)      (on TUI_TURN_COMPLETE)
    └─▶ StatusState.* mutations             (on cost, mode, agent count changes)
```

**Async event handling pattern:**

```python
# src/agenthicc/tui/event_adapter.py

class TUIEventAdapter:
    def __init__(
        self,
        processor: EventProcessor,
        transcript: TranscriptModel,
        status: StatusState,
        render_loop: RenderLoop,
        approval_gate: ApprovalGate,
        doom_detector: DoomLoopDetector,
    ) -> None: ...

    async def _adapter_loop(self) -> None:
        """Runs as asyncio.Task. Translates kernel events to TUI state mutations."""
        sub_queue: asyncio.Queue[AppState] = await self._processor.subscribe()
        prev_state: AppState | None = None
        try:
            while True:
                new_state = await sub_queue.get()
                await self._handle_state_diff(prev_state, new_state)
                prev_state = new_state
        finally:
            await self._processor.unsubscribe(sub_queue)

    async def _handle_state_diff(
        self, prev: AppState | None, curr: AppState
    ) -> None:
        """Dispatch to appropriate handler based on what changed."""
        # Diff logic: compare prev.intents vs curr.intents, etc.
        # Each handler is a separate async method.
        ...
```

**Event ordering guarantee:** Because `EventProcessor` is MPSC with a single-consumer
`run()` task, and because `TUIEventAdapter._adapter_loop()` consumes the subscriber
queue serially, all TUI state mutations happen in event-emission order. No locking
is needed. No concurrent mutation is possible.

---

## 5. Concurrency Model

### 5.1 Task Map

At steady state, the following `asyncio.Task` objects are running:

| Task | Created By | Purpose | Cancellation |
|------|-----------|---------|-------------|
| `EventProcessor.run()` | `app.py:on_mount()` | Kernel event loop | `processor.shutdown()` |
| `TUIEventAdapter._adapter_loop()` | `app.py:on_mount()` | Kernel→TUI bridge | `adapter.shutdown()` |
| `RenderLoop.run()` | `app.py:on_mount()` | 50ms render tick | `render_loop.shutdown()` |
| `RenderLoop._spinner_loop()` | `render_loop.run()` | Spinner frame advance | Internal cancel |
| `AgentRunner.run()` | `InputState.on_submit()` | LLM API call + streaming | SIGINT → `turn_cancelled` event |
| `BottomApp.run(inline=True)` | `app.py` main() | Textual bottom block | `app.exit()` |

### 5.2 Thread Safety

All `asyncio.Task`s above run on the **same event loop thread**. There is no
`ThreadPoolExecutor` involved in the TUI layer. Thread safety requirements:

- `TranscriptModel`: NOT thread-safe. Only accessed from the event loop thread.
- `StatusState`: NOT thread-safe. Only accessed from the event loop thread.
- `InputState`: NOT thread-safe. Only accessed from the Textual event handler (same thread).
- `Terminal._write_atomic()`: Thread-safe for the write operation itself (single `os.write()`
  syscall is atomic on POSIX up to PIPE_BUF bytes). BUT `_bottom_height` mutation is
  NOT protected. Only call from the event loop thread.
- `EventProcessor.emit()`: Thread-safe — uses `asyncio.Queue.put_nowait()` which is
  thread-safe for single-item puts.

### 5.3 Blocking Operation Handling

All blocking operations are wrapped:

| Blocking Operation | Wrapping Strategy |
|-------------------|--------------------|
| File I/O (read/write tools) | `asyncio.to_thread()` in `ToolExecutor` |
| subprocess execution | `asyncio.to_thread()` with process group kill |
| SQLite reads/writes | `asyncio.to_thread()` in `ProjectMemoryLayer` |
| LLM API streaming | Async HTTP via `httpx.AsyncClient` in `lauren-ai` |
| Terminal size query | Sync `os.get_terminal_size()` — fast, no wrapping needed |
| Session file writes | `asyncio.to_thread()` for large writes; sync for metadata |

**Rule:** No blocking call with a worst-case latency above 1ms runs on the event loop
thread without `asyncio.to_thread()` wrapping.

### 5.4 RenderLoop Concurrency Detail

`RenderLoop.run()` is an `asyncio.Task`. On each tick:
1. Reads `_needs_redraw` flag (set by other tasks via `request_redraw()`)
2. Reads `_pending_committed` list (populated by `force_commit()`)
3. Calls pure `FrameComposer.compose()` — synchronous, no await
4. Calls `Terminal.set_bottom()` — synchronous `os.write()`, no await
5. Awaits `asyncio.sleep(remaining_tick_time)` — yields to event loop

Steps 1–4 complete without yielding. This means no other coroutine can observe a
partial render state within one tick. The render is atomic at the asyncio level.

---

## 6. Entry Points and App Lifecycle

### 6.1 Module: `src/agenthicc/tui/app.py`

```python
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.widgets import Static, TextArea, Label
from textual.containers import Vertical, Horizontal

from .terminal import Terminal, FakeTerminal, Size
from .frame_composer import FrameComposer
from .render_loop import RenderLoop
from .input_state import InputState, InputMode
from .transcript import TranscriptModel
from .event_adapter import TUIEventAdapter
from .approval_gate import ApprovalGate
from .doom_loop import DoomLoopDetector
from .session_recap import SessionRecapGenerator
from .symbols import SPINNER_FRAMES


class AgenthiccApp(App):
    """
    Textual App for the bottom block ONLY.
    Runs with inline=True — never alternate screen.
    The committed transcript lives above this app in the terminal scrollback.
    """
    INLINE_PADDING = 0
    CSS_PATH = [
        "styles/app.tcss",
        "styles/layout.tcss",
        "styles/widgets.tcss",
        "styles/inline.tcss",
    ]

    def __init__(
        self,
        terminal: Terminal,
        render_loop: RenderLoop,
        input_state: InputState,
        transcript: TranscriptModel,
        status: "StatusState",
    ) -> None:
        super().__init__()
        self._terminal = terminal
        self._render_loop = render_loop
        self._input_state = input_state
        self._transcript = transcript
        self._status = status

    def compose(self) -> ComposeResult:
        with Vertical(id="bottom-block"):
            yield Static("", id="streaming-zone")
            yield Static("", id="status-bar")
            yield Static("", id="divider")
            yield TextArea(id="input-field")
            yield Static("", id="footer")
            yield Static("", id="dropdown", classes="hidden")


def run_tui(
    processor: "EventProcessor",
    config: "AgenthiccConfig",
    session_id: str,
    resume_session_id: str | None = None,
) -> int:
    """
    Main TUI entry point. Returns exit code.
    Called by src/agenthicc/__main__.py after argument parsing.
    """
    return asyncio.run(_run_async(processor, config, session_id, resume_session_id))


def run_headless(
    processor: "EventProcessor",
    config: "AgenthiccConfig",
    session_id: str,
) -> int:
    """
    Headless mode: JSON-lines to stdout. No TUI.
    Called by src/agenthicc/__main__.py when --headless flag is set.
    """
    return asyncio.run(_run_headless_async(processor, config, session_id))


async def _run_async(
    processor: "EventProcessor",
    config: "AgenthiccConfig",
    session_id: str,
    resume_session_id: str | None,
) -> int:
    """Internal async implementation of run_tui()."""
    # Step 1: Initialise Terminal (detects capabilities, registers SIGWINCH)
    terminal = Terminal(fd=1)

    # Step 2: Initialise state
    transcript = TranscriptModel()
    status = StatusState(
        session_id=session_id[:8],
        model_name=config.execution.model,
        permission_mode=config.security.default_mode.upper(),
    )

    async def _on_submit(text: str) -> None:
        from agenthicc.kernel.events import Event, EventType
        event = Event.create(
            event_type=EventType.INTENT_SUBMITTED,
            payload={"text": text, "session_id": session_id},
            source_agent_id="user",
        )
        await processor.emit(event)

    input_state = InputState(on_submit=_on_submit)

    # Step 3: Initialise render pipeline
    composer = FrameComposer(color=terminal.capabilities.color_depth > 0)
    render_loop = RenderLoop(terminal, composer, transcript, input_state)

    # Step 4: Initialise auxiliary components
    doom_detector = DoomLoopDetector()
    approval_gate = ApprovalGate(processor, terminal, render_loop)

    # Step 5: Initialise event adapter
    adapter = TUIEventAdapter(
        processor, transcript, status, render_loop, approval_gate, doom_detector
    )

    # Step 6: Handle session resume
    if resume_session_id:
        await _replay_session(resume_session_id, transcript, terminal, config)

    # Step 7: Register signal handlers
    _install_signal_handlers(terminal, render_loop, processor)

    # Step 8: Start all asyncio tasks
    tasks = [
        asyncio.create_task(processor.run(), name="kernel"),
        asyncio.create_task(adapter._adapter_loop(), name="tui-adapter"),
        asyncio.create_task(render_loop.run(), name="render-loop"),
    ]

    # Step 9: Run Textual bottom block inline
    app = AgenthiccApp(terminal, render_loop, input_state, transcript, status)
    try:
        await app.run_async(inline=True)
    finally:
        # Step 10: Graceful shutdown
        render_loop.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        terminal.clear_bottom()
        terminal.teardown()

    return 0
```

### 6.2 Startup Sequence (Exact Order)

1. `__main__.py` parses arguments via `argparse`
2. `load_config()` reads `agenthicc.toml` and `~/.agenthicc.toml` and merges
3. `EventProcessor` is constructed (queue created, no tasks started)
4. `AgentPool` is constructed and agents are registered
5. `run_tui()` is called (or `run_headless()`)
6. Inside `_run_async()`:
   a. `Terminal(fd=1)` constructed → detects capabilities → registers SIGWINCH
   b. `TranscriptModel()` constructed (empty)
   c. `StatusState` constructed with initial values from config
   d. `InputState(on_submit=_on_submit)` constructed
   e. `FrameComposer` constructed (reads `terminal.capabilities.color_depth`)
   f. `RenderLoop` constructed (not started yet)
   g. `DoomLoopDetector`, `ApprovalGate` constructed
   h. `TUIEventAdapter` constructed (not started yet)
   i. If `--resume`, replay last N turns to committed transcript
   j. SIGINT/SIGTERM/SIGHUP handlers installed
   k. `processor.run()` task created
   l. `adapter._adapter_loop()` task created
   m. `render_loop.run()` task created
   n. `AgenthiccApp.run_async(inline=True)` called (blocks until app exits)
7. On exit: `render_loop.shutdown()`, tasks cancelled, terminal cleaned up
8. Return exit code 0

**Time budget for startup:**

| Step | Budget |
|------|--------|
| Python import chain | ~200ms (not our code) |
| Config load | ~50ms |
| Terminal capability detection | ~30ms |
| EventProcessor / pool init | ~20ms |
| First bottom block draw | ~5ms |
| **Total to first render** | **< 305ms** (well within 800ms budget) |

### 6.3 Shutdown Sequence

**Normal exit (user types /exit or presses Ctrl+D):**
1. `InputState.submit()` → dispatches `/exit` intent
2. `TUIEventAdapter` receives `INTENT_SUBMITTED` for `/exit`
3. Sets `app_model.shutdown_requested = True`
4. Calls `render_loop.force_commit(session_summary_lines)`
5. `AgenthiccApp.exit()` called → `run_async()` returns
6. `render_loop.shutdown()` called → `run()` loop exits
7. All tasks cancelled
8. `terminal.clear_bottom()` → `terminal.teardown()`
9. Exit code 0

**SIGTERM:**
1. Signal handler sets `_sigterm_received = True`
2. Handler calls `asyncio.get_event_loop().call_soon_threadsafe(processor.shutdown)`
3. `processor.shutdown()` drains queue and calls `app.exit()`
4. Same shutdown sequence as normal exit follows
5. Total budget: 5 seconds (enforced by `asyncio.wait_for(shutdown_coro, timeout=5)`)

**SIGINT (first press during agent turn):**
1. Signal handler emits `TUI_TURN_CANCELLED` event
2. `TUIEventAdapter` handles it: transitions current turn to CANCELLED state
3. `render_loop.force_commit([cancelled_line])` called
4. `InputState.set_mode(InputMode.NORMAL)` called
5. Bottom block redraws in idle state

**SIGINT (second press within 2 seconds):** triggers SIGTERM sequence

**SIGINT (third press any time):** `sys.exit(1)` immediately

**SIGHUP (SSH disconnect):**
1. Same as SIGTERM but exits with code 0 after flush

**Crash recovery (unhandled exception in asyncio main loop):**
1. Top-level exception handler in `__main__.py` catches
2. `terminal.clear_bottom()` called unconditionally (cannot raise)
3. Traceback written to `~/.agenthicc/crash-{timestamp}.log`
4. Short message printed to stderr
5. `sys.exit(1)`

### 6.4 SIGWINCH Handling

```python
def _install_signal_handlers(
    terminal: Terminal,
    render_loop: RenderLoop,
    processor: EventProcessor,
) -> None:
    def _sigwinch(signum: int, frame: object) -> None:
        # Signal handler: set flag only, no I/O
        terminal._resize_pending = True
        render_loop._needs_redraw = True
        # Note: terminal.update_size() is called by render_loop.run() on next tick

    def _sigterm(signum: int, frame: object) -> None:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(processor.shutdown)

    def _sighup(signum: int, frame: object) -> None:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(processor.shutdown)

    signal.signal(signal.SIGWINCH, _sigwinch)
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGHUP, _sighup)
    # SIGINT is handled separately (counting presses)
```

---

## 7. Error Handling Strategy

### 7.1 Error Categories

| Category | Definition | Examples |
|----------|-----------|---------|
| **Recoverable** | Tool call failed; agent can continue | `read_file` path not found, `run_bash` non-zero exit |
| **Critical** | LLM API error; agent turn paused | 429 rate limit, 500 server error, network timeout |
| **Fatal** | Unhandled Python exception | `AttributeError` in reducer, `ImportError`, OOM |

### 7.2 Per-Category Handling

**Recoverable errors:**
- `ToolExecutor` catches the exception and returns `ToolResult(ok=False, error=str(e))`
- `TUIEventAdapter` receives `TUI_TOOL_CALL_COMPLETE(success=False)` event
- `TranscriptModel.update_tool_call()` sets `state=ERROR, error_message=...`
- On next `force_commit()`, `render_tool_call_line(tc, color)` renders `✗` line
- No user intervention required; agent receives error as tool output and continues

**Critical errors:**
- `AgentRunner` catches `anthropic.APIError` (or equivalent) and emits `TUI_ERROR_BANNER`
- `TUIEventAdapter` sets `status.error_banner = message` and calls `render_loop.request_redraw()`
- `FrameComposer._render_status_bar()` detects `status.error_banner is not None` and
  renders the banner as a prominent line in the bottom block (replacing normal status)
- Banner persists until user presses `r` (retry), `w` (wait and retry), or `c` (cancel)
- After user response, `TUI_ERROR_CLEARED` event sets `status.error_banner = None`

**Fatal errors:**
```python
# src/agenthicc/__main__.py — top-level exception handler

async def _main_with_recovery():
    try:
        return await _run_async(...)
    except Exception as exc:
        import traceback
        import datetime
        # Step 1: clear terminal (cannot raise)
        try:
            terminal.clear_bottom()
            terminal.teardown()
        except Exception:
            pass
        # Step 2: write crash log
        crash_ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        crash_path = Path.home() / f".agenthicc/crash-{crash_ts}.log"
        crash_path.parent.mkdir(parents=True, exist_ok=True)
        crash_path.write_text(traceback.format_exc())
        # Step 3: print to stderr
        print(
            f"agenthicc crashed: {type(exc).__name__}: {exc}\n"
            f"Session saved. Resume with: agenthicc --resume {session_id[:8]}\n"
            f"Full log: {crash_path}",
            file=sys.stderr,
        )
        return 1
```

### 7.3 Propagation Rules

- Exceptions from `ToolExecutor` are NEVER propagated to `AgentRunner` unhandled;
  always returned as `ToolResult(ok=False, error=...)`
- Exceptions from `AgentRunner` (LLM API errors) are caught and emitted as
  `TUI_ERROR_BANNER` events; the runner sets `status.active = False`
- Exceptions from `TUIEventAdapter._adapter_loop()` are caught and re-raised as
  `TUI_ERROR_BANNER("internal_error", str(exc))` before re-raising to the task
- Exceptions from `RenderLoop.run()` are considered fatal; the run() coroutine
  should never raise (all Terminal calls are wrapped in try/except within the loop)
- Exceptions from `Terminal._write_atomic()` (e.g., broken pipe on SSH disconnect)
  set `terminal._io_error = True` which causes `RenderLoop.run()` to exit cleanly

### 7.4 User-Visible Error Presentation

**Inline tool error (recoverable):**
```
  ⎿ read_file(path='/nonexistent.py')  ✗ 2ms  No such file or directory
```
Rendered in committed transcript. No bottom-block change.

**Critical error banner (bottom block replacement):**
```
[AUTO] claude-sonnet-4-6  ...
──────────────────────────────────────────────────────────────────
✗ API ERROR: 429 rate_limit_exceeded — retry in 23s
[R] Retry now  [W] Wait 23s  [C] Cancel turn
```
The status bar row is replaced by the error banner. Persists until user responds.

**Fatal crash (stderr, then exit):**
```
agenthicc crashed: ValueError: unexpected state transition
Session saved. Resume with: agenthicc --resume a3f8
Full log: ~/.agenthicc/crash-20260613-094512.log
```

---

## 8. Performance Budget

### 8.1 Startup

| Checkpoint | Target | Measurement |
|-----------|--------|------------|
| Cold start to first bottom block draw | < 800ms | `time` from process launch |
| First token to first committed line | < 200ms | `time` from first `TUI_TOKEN_STREAM` |
| Session resume to first render | < 500ms | `time` from `--resume` flag |

**Profiling checkpoints** (measured in integration benchmarks):

```python
# src/agenthicc/tui/app.py — add timing probes in debug mode

import os
_t0 = time.monotonic()

# After Terminal.__init__():
_DEBUG and print(f"[perf] Terminal init: {(time.monotonic()-_t0)*1000:.1f}ms", file=sys.stderr)

# After first render_loop tick:
_DEBUG and print(f"[perf] First render: {(time.monotonic()-_t0)*1000:.1f}ms", file=sys.stderr)
```

Enable with `AGENTHICC_DEBUG_PERF=1` environment variable.

### 8.2 Render Performance

| Metric | Target | Verification |
|--------|--------|-------------|
| `FrameComposer.compose()` | < 8ms per call | `time.perf_counter()` in benchmark |
| `Terminal.set_bottom()` | < 2ms per call | `time.perf_counter()` in benchmark |
| Full tick (compose + write) | < 16ms | Loop timing in `RenderLoop.run()` |
| Write syscalls per frame | exactly 1 | `FakeTerminal.write_call_count` assertion |
| Frames per second | 20 (50ms tick) | Constant `MIN_TICK_INTERVAL = 0.050` |

**FrameComposer optimisations to achieve < 8ms:**
- Render cache: `_render_cache: dict[str, list[str]]` keyed by `turn_id`; only
  invalidated when turn transitions from STREAMING to COMPLETE
- Incremental committed lines: `_committed_turns_count` tracks how many turns have
  been rendered; only new turns are rendered on each `compose()` call
- String formatting: pre-build ANSI escape sequences as constants in `symbols.py`;
  avoid format string allocation in the hot path where possible
- Width: `FrameComposer` receives `Size` on every call; no global state for width

### 8.3 Memory

| Metric | Target | Enforcement |
|--------|--------|------------|
| Base RSS (idle, no turns) | < 10MB | Process measurement |
| Per turn (average) | < 50KB | `tracemalloc` benchmark |
| Total at 200 turns | < 10MB transcript | `TranscriptModel.MAX_TURNS_IN_MEMORY = 200` |
| Full RSS after 200 turns | < 100MB | `psutil` in long-session benchmark |

**Memory eviction rules (in `TranscriptModel._evict_old_turns()`):**
- Triggered when `len(turns) > MAX_TURNS_IN_MEMORY` (200)
- Evicts `output_lines` from the oldest 20 turns (keeps metadata)
- Evicts `tool_calls[*].output_lines` from the same 20 turns
- Never evicts `AgentTurnEntry.tool_calls` list (tool metadata kept)
- Logs a `TUI_SESSION_RECAP` event with a compact summary of evicted turns

### 8.4 CPU

| State | Target | Measurement |
|-------|--------|------------|
| Idle (between turns) | < 1% CPU | `psutil.Process().cpu_percent()` |
| Active streaming | < 15% CPU (single core) | `psutil` sampling |
| SIGWINCH handling | < 5ms | Signal-to-render latency |

**Dirty flag optimization:**
`RenderLoop.run()` only calls `FrameComposer.compose()` when `_needs_redraw is True`.
`_needs_redraw` is set to False after each render and re-set to True only by:
- `request_redraw()` (called by `TUIEventAdapter` on state mutation)
- `force_commit()` (called on turn completion)
- Resize detection (`terminal.resize_pending`)

Between events, `run()` sleeps for the full 50ms tick. CPU usage is negligible.

### 8.5 SSH / High-Latency Degradation

When `SSH_CONNECTION` environment variable is set AND estimated RTT > 200ms:
- `RenderLoop.MIN_TICK_INTERVAL` increases to `0.150` (6.7fps instead of 20fps)
- `StatusState.degraded_mode = True` triggers simplified status bar (fewer fields)

RTT is estimated by timing a DSR probe (`\x1b[5n`) during `Terminal.__init__()`.
If no response within 100ms, RTT is assumed to be 100ms+.

---

## 9. Full Test Specification

### 9.1 Unit Tests — Terminal (tests/unit/test_terminal.py)

**T-TERM-001** `test_init_detects_no_color_env`
- Component: `Terminal.__init__()`, `_detect_capabilities()`
- Input: `os.environ["NO_COLOR"] = "1"`
- Expected: `terminal.capabilities.color_depth == 0`, `terminal.capabilities.no_color == True`
- Edge case: value is "0" — spec says any non-empty value disables color

**T-TERM-002** `test_init_force_color_enables_color_without_tty`
- Component: `Terminal.__init__()`, `_detect_capabilities()`
- Input: `os.environ["FORCE_COLOR"] = "1"`, `fd` is not a TTY (use pipe)
- Expected: `terminal.capabilities.color_depth > 0`

**T-TERM-003** `test_init_truecolor_from_colorterm`
- Input: `os.environ["COLORTERM"] = "truecolor"`
- Expected: `terminal.capabilities.color_depth == 16777216`

**T-TERM-004** `test_init_256color_from_colorterm`
- Input: `os.environ["COLORTERM"] = "256color"`
- Expected: `terminal.capabilities.color_depth == 256`

**T-TERM-005** `test_init_256color_from_term_suffix`
- Input: `os.environ["TERM"] = "xterm-256color"`
- Expected: `terminal.capabilities.color_depth == 256`

**T-TERM-006** `test_init_dumb_terminal_ascii_only`
- Input: `os.environ["TERM"] = "dumb"`
- Expected: `terminal.capabilities.unicode_level == 0`, `terminal.capabilities.color_depth == 0`

**T-TERM-007** `test_commit_lines_empty_list_is_noop`
- Component: `Terminal.commit_lines([])`
- Using `FakeTerminal`
- Expected: `fake.write_call_count == 0`

**T-TERM-008** `test_commit_lines_increments_write_call_count_by_one`
- Component: `Terminal.commit_lines(["line1", "line2"])`
- Using `FakeTerminal`
- Expected: `fake.write_call_count == 1` (one write for both lines)

**T-TERM-009** `test_commit_lines_extends_committed_list`
- Component: `FakeTerminal.commit_lines(["a", "b"])`
- Expected: `fake.committed_lines == ["a", "b"]`

**T-TERM-010** `test_commit_lines_resets_bottom_height`
- Component: `FakeTerminal.commit_lines()`
- Pre-condition: `fake._bottom_height = 3`
- Expected: `fake._bottom_height == 0` after call

**T-TERM-011** `test_set_bottom_stores_frame_in_history`
- Component: `FakeTerminal.set_bottom(frame)`
- Input: `Frame(rows=["status", "input"], height=2, cursor_row=1, cursor_col=2)`
- Expected: `fake.bottom_history[-1] == frame`

**T-TERM-012** `test_set_bottom_updates_bottom_height`
- Component: `FakeTerminal.set_bottom(frame)`
- Input: frame with `height=4`
- Expected: `fake._bottom_height == 4`

**T-TERM-013** `test_set_bottom_single_write_call`
- Component: `FakeTerminal.set_bottom(frame)`
- Expected: `fake.write_call_count == 1`

**T-TERM-014** `test_clear_bottom_sets_height_to_zero`
- Component: `FakeTerminal.clear_bottom()`
- Pre-condition: `fake._bottom_height = 5`
- Expected: `fake._bottom_height == 0`

**T-TERM-015** `test_sigwinch_sets_resize_pending`
- Component: `Terminal._on_sigwinch()`
- Action: manually call `terminal._on_sigwinch(signal.SIGWINCH, None)`
- Expected: `terminal._resize_pending == True`

**T-TERM-016** `test_update_size_clears_resize_pending`
- Component: `Terminal.update_size()`
- Pre-condition: `terminal._resize_pending = True`
- Expected: `terminal._resize_pending == False`

**T-TERM-017** `test_set_bottom_erase_sequence_correct`
- Component: `Terminal.set_bottom()` on a real `BytesIO`-backed terminal
- Pre-condition: `terminal._bottom_height = 3`
- Input: new frame with height 2
- Expected: bytes contain `b"\x1b[2K"` followed by two `b"\x1b[1A\x1b[2K"` sequences
- (Verifies canonical erase sequence from master PRD section 6.2)

**T-TERM-018** `test_set_bottom_no_erase_when_previous_height_zero`
- Component: `Terminal.set_bottom()` raw bytes
- Pre-condition: `terminal._bottom_height = 0`
- Expected: bytes do NOT contain `b"\x1b[1A"` (no cursor-up needed)

**T-TERM-019** `test_synchronized_output_wraps_bsu_esu`
- Component: `Terminal.set_bottom()` with `capabilities.synchronized_output = True`
- Expected: bytes start with `b"\x1b[?2026h"` and end with `b"\x1b[?2026l"`

**T-TERM-020** `test_synchronized_output_absent_when_unsupported`
- Component: `Terminal.set_bottom()` with `capabilities.synchronized_output = False`
- Expected: bytes do NOT contain `b"\x1b[?2026h"`

**T-TERM-021** `test_no_alternate_screen_sequences_in_any_operation`
- Component: all `Terminal` methods exercised in sequence
- Expected: output bytes do NOT contain `b"\x1b[?1049h"` or `b"\x1b[?1049l"`

**T-TERM-022** `test_commit_then_set_bottom_correct_sequence`
- Component: `commit_lines()` followed by `set_bottom()`
- Using real bytes capture
- Expected: committed lines appear BEFORE bottom block bytes in output

**T-TERM-023** `test_teardown_shows_cursor_and_resets`
- Component: `Terminal.teardown()`
- Expected: output contains `b"\x1b[?25h"` (show cursor) and `b"\x1b[0m"` (reset)

**T-TERM-024** `test_fake_terminal_size_default`
- Component: `FakeTerminal()`
- Expected: `fake.size.rows == 24`, `fake.size.cols == 80`

**T-TERM-025** `test_fake_terminal_committed_lines_append_only`
- Component: multiple `FakeTerminal.commit_lines()` calls
- Expected: each call extends `committed_lines`; list never shrinks

### 9.2 Unit Tests — FrameComposer (tests/unit/test_frame_composer.py)

**T-FC-001** `test_compose_returns_frame_with_minimum_4_rows`
- Component: `FrameComposer.compose(empty_transcript, idle_input_state, Size(24, 80))`
- Expected: `len(frame.rows) >= 4` (status + divider + input + footer)

**T-FC-002** `test_compose_is_deterministic`
- Component: `FrameComposer.compose()` called twice with identical args
- Expected: `frame1 == frame2` (Frame is frozen dataclass)

**T-FC-003** `test_compose_clamps_to_max_height`
- Component: `FrameComposer.compose()` with 20-row streaming buffer and open dropdown
- Input: `Size(30, 80)` — max height = `min(12, 30//3) = 10`
- Expected: `frame.height <= 10`

**T-FC-004** `test_compose_streaming_buffer_adds_rows`
- Component: `FrameComposer._render_streaming()`
- Input: transcript with `streaming_buffer = "some long text here"`
- Expected: `frame.height > base_height`

**T-FC-005** `test_compose_dropdown_open_inserts_rows_before_status`
- Component: `FrameComposer._render_dropdown()`
- Input: input_state with `dropdown.open=True, dropdown.items=["a","b","c"]`
- Expected: dropdown rows appear before status bar in `frame.rows`

**T-FC-006** `test_compose_no_color_strips_ansi_sequences`
- Component: `FrameComposer(color=False).compose()`
- Expected: no `\x1b[` sequences in any `frame.rows` element

**T-FC-007** `test_render_status_bar_shows_mode_badge`
- Component: `FrameComposer._render_status_bar()`
- Input: `status.permission_mode = "REVIEW"`
- Expected: status bar row contains "REVIEW" text

**T-FC-008** `test_render_status_bar_mode_badge_auto`
- Input: `status.permission_mode = "AUTO"`
- Expected: row contains "[AUTO]" or "AUTO" (depending on color mode)

**T-FC-009** `test_render_status_bar_shows_agent_count`
- Input: `status.agent_count = 3`
- Expected: row contains "3 agent"

**T-FC-010** `test_render_status_bar_shows_cost`
- Input: `status.session_cost_usd = 0.031`
- Expected: row contains "$0.031"

**T-FC-011** `test_render_status_bar_shows_session_id`
- Input: `status.session_id = "a3f8b2c1"`
- Expected: row contains "a3f8b2c1"

**T-FC-012** `test_render_divider_exact_width`
- Input: `Size(24, 80)`
- Component: `_render_divider(80)`
- Expected: stripped ANSI divider row has exactly 80 display columns (wcwidth)

**T-FC-013** `test_render_divider_uses_box_drawing_char`
- Expected: divider row contains "─" (U+2500)

**T-FC-014** `test_render_input_single_line`
- Input: `input_state.text = "hello world"`, `Size(24, 80)`
- Expected: exactly one input row, contains "hello world"

**T-FC-015** `test_render_input_wraps_long_text`
- Input: `input_state.text = "x" * 100`, `Size(24, 60)`
- Expected: multiple input rows (text wraps at col 60)

**T-FC-016** `test_render_input_shows_prompt_glyph`
- Expected: first input row starts with ">" or "❯" (glyph)

**T-FC-017** `test_render_footer_shows_cancel_keybinding_during_agent_turn`
- Input: `status.active = True` (simulated via transcript having streaming turn)
- Expected: footer row contains "Ctrl+C" or "cancel"

**T-FC-018** `test_render_footer_shows_submit_during_idle`
- Input: `status.active = False`
- Expected: footer row contains "Enter" and "send"

**T-FC-019** `test_render_footer_shows_mode_cycle_hint`
- Expected: footer row contains "Shift+Tab" and "mode"

**T-FC-020** `test_render_dropdown_max_8_items`
- Input: dropdown with 12 items
- Expected: at most 8 dropdown rows rendered

**T-FC-021** `test_render_dropdown_highlights_selected`
- Input: dropdown with 3 items, `selected_index = 1`
- Expected: second dropdown row has different styling (bold or highlighted)

**T-FC-022** `test_frame_equality_works`
- Component: `Frame.__eq__()`
- Input: two `Frame` objects with identical fields
- Expected: `frame1 == frame2` is True

**T-FC-023** `test_frame_inequality_on_row_change`
- Input: same Frame with one row text changed
- Expected: `frame1 != frame2` is True

**T-FC-024** `test_compose_error_banner_replaces_status`
- Input: `status.error_banner = "API ERROR: 429 rate_limit_exceeded"`
- Expected: frame row contains "API ERROR" (banner replaces normal status)

**T-FC-025** `test_compose_approval_gate_replaces_input`
- Input: `status.approval_pending = True`, approval gate text provided
- Expected: frame rows contain "approve?" and "[Y]" and "[N]"

**T-FC-026** `test_render_streaming_caps_at_8_rows`
- Input: `transcript.streaming_buffer = "line\n" * 20`
- Expected: streaming zone is at most 8 rows

**T-FC-027** `test_compose_minimum_terminal_size_warning`
- Input: `Size(10, 50)` (below 60x12 minimum)
- Expected: `frame.rows` contains "Terminal too small" warning

**T-FC-028** `test_render_mode_badge_all_six_modes`
- Input: each of AUTO/PLAN/ASK/REVIEW/SAFE/DEBUG
- Expected: each produces a distinct text badge in the status row

**T-FC-029** `test_compose_caches_rendered_turns`
- Component: `FrameComposer._committed_turns_count`
- Action: call `compose()` twice with same transcript (1 finalized turn)
- Expected: `_committed_turns_count == 1` after both calls; cache hit on second call

**T-FC-030** `test_compose_incremental_new_turns`
- Component: `FrameComposer._committed_cache`
- Action: add 2 turns, call `compose()`, add 1 more turn, call `compose()` again
- Expected: `_committed_turns_count == 3` after second call; cache was extended, not rebuilt

### 9.3 Unit Tests — RenderLoop (tests/unit/test_render_loop.py)

**T-RL-001** `test_run_calls_set_bottom_on_first_tick`
- Setup: `FakeTerminal`, `FrameComposer`, `TranscriptModel`, `InputState`
- Action: start `render_loop.run()` as task, wait one tick
- Expected: `fake.bottom_history` has at least 1 entry

**T-RL-002** `test_run_skips_redraw_when_frame_unchanged`
- Setup: initial render done, `_needs_redraw = False`
- Action: wait two more ticks without state change
- Expected: `fake.bottom_history` count unchanged after extra ticks

**T-RL-003** `test_force_commit_flushes_lines_before_bottom_block`
- Action: `render_loop.force_commit(["line1", "line2"])`, wait one tick
- Expected: `fake.committed_lines` contains "line1" and "line2"

**T-RL-004** `test_force_commit_clears_bottom_before_committing`
- Pre-condition: `fake._bottom_height = 3`
- Action: `render_loop.force_commit(["line"])`
- Expected: `fake._bottom_height == 0` at moment of commit, then non-zero after set_bottom

**T-RL-005** `test_force_commit_resets_last_frame`
- Action: `render_loop.force_commit(["line"])`
- Expected: `render_loop._last_frame is None` after commit (forces full redraw)

**T-RL-006** `test_resize_pending_triggers_redraw`
- Action: set `terminal._resize_pending = True`, wait one tick
- Expected: `render_loop._terminal.update_size()` was called (mock or verify `_resize_pending == False`)

**T-RL-007** `test_shutdown_stops_loop`
- Action: start `run()` as task, call `render_loop.shutdown()`, await task with timeout
- Expected: task completes within 200ms

**T-RL-008** `test_min_tick_interval_respected`
- Setup: mock `time.monotonic()` to control time
- Action: call `run()` loop and advance mock time by less than 50ms
- Expected: `FrameComposer.compose()` not called more than once per 50ms

**T-RL-009** `test_request_redraw_forces_next_tick`
- Setup: frame rendered, `_needs_redraw = False`
- Action: `render_loop.request_redraw()`, wait one tick
- Expected: `set_bottom()` called again

**T-RL-010** `test_spinner_loop_advances_frame_counter`
- Action: start `render_loop.run()`, wait 300ms
- Expected: `render_loop._spinner_frame > 2` (should have advanced 3 times at 100ms interval)

**T-RL-011** `test_pending_committed_cleared_after_flush`
- Action: `render_loop.force_commit(["a", "b"])`, wait one tick
- Expected: `render_loop._pending_committed == []` after flush

**T-RL-012** `test_render_loop_handles_terminal_io_error_gracefully`
- Setup: `FakeTerminal` with `_io_error = True` flag
- Action: start `render_loop.run()`, simulate write failure
- Expected: loop exits cleanly (no unhandled exception)

### 9.4 Unit Tests — InputState (tests/unit/test_input_state.py)

**T-IS-001** `test_insert_updates_text_and_advances_cursor`
- Action: `input_state.insert("a")`
- Expected: `text == "a"`, `cursor == 1`

**T-IS-002** `test_insert_at_middle_position`
- Setup: `text = "ac"`, `cursor = 1`
- Action: `input_state.insert("b")`
- Expected: `text == "abc"`, `cursor == 2`

**T-IS-003** `test_backspace_removes_character_before_cursor`
- Setup: `text = "abc"`, `cursor = 2`
- Action: `input_state.backspace()`
- Expected: `text == "ac"`, `cursor == 1`

**T-IS-004** `test_backspace_at_start_is_noop`
- Setup: `text = "abc"`, `cursor = 0`
- Action: `input_state.backspace()`
- Expected: `text == "abc"`, `cursor == 0`

**T-IS-005** `test_kill_to_end_stores_in_kill_ring`
- Setup: `text = "hello world"`, `cursor = 5`
- Action: `input_state.kill_to_end()`
- Expected: `text == "hello"`, `kill_ring == [" world"]`

**T-IS-006** `test_kill_to_start_stores_in_kill_ring`
- Setup: `text = "hello world"`, `cursor = 5`
- Action: `input_state.kill_to_start()`
- Expected: `text == " world"`, `cursor == 0`, `kill_ring == ["hello"]`

**T-IS-007** `test_yank_inserts_from_kill_ring`
- Setup: `kill_ring = ["killed text"]`, `text = "ab"`, `cursor = 1`
- Action: `input_state.yank()`
- Expected: `text == "akilled textb"`, `cursor == 12`

**T-IS-008** `test_yank_empty_kill_ring_is_noop`
- Setup: `kill_ring = []`
- Action: `input_state.yank()`
- Expected: `text` unchanged

**T-IS-009** `test_history_up_loads_previous`
- Setup: `history = ["first", "second"]`
- Action: `input_state.history_up()`
- Expected: `text == "second"` (most recent first)

**T-IS-010** `test_history_up_twice`
- Setup: `history = ["first", "second"]`
- Action: `history_up()`, `history_up()`
- Expected: `text == "first"` (older entry)

**T-IS-011** `test_history_down_returns_to_empty`
- Setup: after `history_up()` once
- Action: `input_state.history_down()`
- Expected: `text == ""`, `history_index == -1`

**T-IS-012** `test_submit_clears_text_and_adds_to_history`
- Setup: `text = "hello"`, async `on_submit` mock
- Action: `await input_state.submit()`
- Expected: `text == ""`, `history == ["hello"]`, `on_submit` called with "hello"

**T-IS-013** `test_submit_empty_text_is_noop`
- Setup: `text = "   "`
- Action: `await input_state.submit()`
- Expected: `on_submit` not called, history unchanged

**T-IS-014** `test_submit_disabled_is_noop`
- Setup: `input_state.set_disabled(True)`
- Action: `await input_state.submit()`
- Expected: `on_submit` not called

**T-IS-015** `test_slash_trigger_opens_dropdown`
- Action: `input_state.insert("/")`
- Expected: `dropdown.open == True`, `dropdown.trigger == TriggerType.SLASH_COMMAND`

**T-IS-016** `test_slash_trigger_only_at_start_of_line`
- Setup: `text = "hello "`, `cursor = 6`
- Action: `input_state.insert("/")`
- Expected: `dropdown.open == False` (slash not at start)

**T-IS-017** `test_at_trigger_opens_dropdown_at_word_boundary`
- Setup: `text = " "`, `cursor = 1`
- Action: `input_state.insert("@")`
- Expected: `dropdown.open == True`, `dropdown.trigger == TriggerType.AT_MENTION`

**T-IS-018** `test_at_trigger_does_not_fire_in_email_context`
- Setup: `text = "user"`, `cursor = 4`
- Action: `input_state.insert("@")`
- Expected: `dropdown.open == False` (preceded by alphanumeric)

**T-IS-019** `test_at_trigger_fires_at_start_of_input`
- Setup: empty `InputState`
- Action: `input_state.insert("@")`
- Expected: `dropdown.open == True`

**T-IS-020** `test_close_dropdown_resets_all_fields`
- Setup: dropdown open with items
- Action: `input_state.close_dropdown()`
- Expected: `dropdown.open == False`, `dropdown.items == []`

**T-IS-021** `test_mode_locked_disables_text_insert`
- Setup: `input_state.set_mode(InputMode.LOCKED)`
- Action: `input_state.insert("a")`
- Expected: `text` unchanged (locked mode blocks input)

**T-IS-022** `test_insert_newline`
- Action: `input_state.insert_newline()`
- Expected: `text` contains `"\n"` at cursor position

**T-IS-023** `test_render_lines_single_line`
- Setup: `text = "hello"`, `cursor = 5`
- Action: `input_state.render_lines("❯", 80)`
- Expected: exactly 1 row starting with prompt glyph

**T-IS-024** `test_render_lines_multiline`
- Setup: `text = "line1\nline2"`
- Action: `input_state.render_lines("❯", 80)`
- Expected: 2 rows; second starts with continuation indent

**T-IS-025** `test_move_word_left_stops_at_boundary`
- Setup: `text = "hello world"`, `cursor = 11`
- Action: `input_state.move_word_left()`
- Expected: `cursor == 6`

### 9.5 Unit Tests — TranscriptModel (tests/unit/test_tui_transcript.py)

**T-TM-001** `test_add_turn_assigns_unique_colors_to_different_agents`
- Action: add turns for agents "a1", "a2", "a3"
- Expected: `turns[0].color_index != turns[1].color_index`, all distinct

**T-TM-002** `test_add_turn_assigns_same_color_to_same_agent`
- Action: add two turns for agent "a1"
- Expected: both turns have the same `color_index`

**T-TM-003** `test_add_turn_cycles_colors_after_6`
- Action: add turns for 7 distinct agents
- Expected: agent 7's `color_index` equals agent 1's `color_index` (modulo 6)

**T-TM-004** `test_evict_old_turns_clears_output_lines`
- Setup: 201 turns in transcript, all finalized with output_lines
- Action: `_evict_old_turns()` called (triggered automatically)
- Expected: `turns[0].output_lines == []`

**T-TM-005** `test_evict_old_turns_retains_metadata`
- After eviction: `turns[0].agent_id`, `.agent_name`, `.timestamp` still set

**T-TM-006** `test_evict_old_turns_only_evicts_oldest_20`
- Setup: 220 turns
- Expected: turns 0–19 evicted, turns 20+ still have output_lines

**T-TM-007** `test_complete_turn_truncates_at_max_lines`
- Input: `final_lines = ["x"] * 600`
- Action: `transcript.complete_turn(turn_id, final_lines)`
- Expected: `turn.output_lines` has at most `MAX_LINES_PER_TURN` (500) entries

**T-TM-008** `test_append_streaming_token_accumulates`
- Action: three calls to `append_streaming_token("a")`
- Expected: `streaming_buffer == "aaa"`

**T-TM-009** `test_clear_streaming_buffer`
- Setup: `streaming_buffer = "some text"`
- Action: `clear_streaming_buffer()`
- Expected: `streaming_buffer == ""`

**T-TM-010** `test_update_tool_call_finds_by_id`
- Setup: turn with two tool calls, different `tool_id`
- Action: `update_tool_call(tool_id=tc2.tool_id, state=ToolCallState.SUCCESS)`
- Expected: only tc2 updated; tc1 unchanged

**T-TM-011** `test_add_tool_call_to_correct_turn`
- Setup: two turns "t1" and "t2"
- Action: `add_tool_call("t2", ToolCallEntry(...))`
- Expected: tool call in `turns[1].tool_calls`, not `turns[0]`

**T-TM-012** `test_commit_lines_advances_cursor`
- Action: `commit_lines(["a", "b", "c"])`
- Expected: `len(all_committed_lines) == 3`

**T-TM-013** `test_render_turn_to_lines_header_format`
- Input: `AgentTurnEntry(agent_id="a1", agent_name="main", timestamp=1718289600.0)`
- Expected: first line contains "main" and a timestamp in HH:MM:SS format

**T-TM-014** `test_render_turn_to_lines_separator_width`
- Input: `cols=80`
- Expected: separator line (last line before blank) has 80 chars stripped of ANSI

**T-TM-015** `test_render_tool_call_line_success`
- Input: `ToolCallEntry(tool_name="read_file", state=SUCCESS, result_summary="142 lines", duration_ms=300)`
- Expected: rendered line contains "read_file", "✓", "142 lines", "300ms"

**T-TM-016** `test_render_tool_call_line_error`
- Input: `ToolCallEntry(tool_name="read_file", state=ERROR, error_message="No such file")`
- Expected: rendered line contains "✗" and "No such file"

**T-TM-017** `test_render_turn_no_color_strips_ansi`
- Input: `color=False`
- Expected: no `\x1b[` in any rendered line

**T-TM-018** `test_tool_call_state_transitions`
- Action: create ToolCallEntry, update PENDING→RUNNING→SUCCESS
- Expected: each `update_tool_call()` call correctly updates state

**T-TM-019** `test_turn_state_streaming_to_complete`
- Setup: turn in STREAMING state
- Action: `complete_turn(turn_id, final_lines)`
- Expected: `turn.state == TurnState.COMPLETE`

**T-TM-020** `test_turn_state_streaming_to_cancelled`
- Action: emit and handle `TUI_TURN_CANCELLED` (via `TranscriptModel.cancel_turn()`)
- Expected: `turn.state == TurnState.CANCELLED`

### 9.6 Unit Tests — ApprovalGate (tests/unit/test_approval_gate.py)

**T-AG-001** `test_approval_gate_starts_inactive`
- Expected: `gate.active == False`

**T-AG-002** `test_activate_sets_tool_and_diff`
- Action: `gate.activate(tool_id, tool_name, args, proposed_diff)`
- Expected: `gate.active == True`, `gate.current_tool_name == tool_name`

**T-AG-003** `test_approve_emits_resolution_event`
- Setup: gate activated
- Action: `await gate.handle_key("y")`
- Expected: `TUI_APPROVAL_RESOLVED(decision="approved")` event emitted

**T-AG-004** `test_deny_emits_denial_event`
- Action: `await gate.handle_key("n")`
- Expected: `TUI_APPROVAL_RESOLVED(decision="denied")` event emitted

**T-AG-005** `test_allow_all_emits_allow_all_event`
- Action: `await gate.handle_key("a")`
- Expected: `TUI_APPROVAL_RESOLVED(decision="allow_all")` event emitted

**T-AG-006** `test_approval_gate_inactive_after_resolution`
- Action: approve, then check state
- Expected: `gate.active == False`

**T-AG-007** `test_diff_committed_before_gate_shows`
- Setup: approval required for `write_file` with a 5-line diff
- Action: activate gate
- Expected: `render_loop.force_commit()` was called with diff lines BEFORE gate shows

**T-AG-008** `test_approval_renders_tool_name_in_gate`
- Setup: gate active for `write_file`
- Action: render approval bottom block rows
- Expected: rows contain "write_file" and "approve?"

**T-AG-009** `test_batched_approvals_show_queue_count`
- Setup: gate with 3 pending approvals (activate called 3 times)
- Expected: bottom block rows contain "1 of 3"

**T-AG-010** `test_enter_key_acts_as_approve`
- Action: `await gate.handle_key("enter")`  
- Expected: same as "y" (approved)

**T-AG-011** `test_uppercase_y_works`
- Action: `await gate.handle_key("Y")`
- Expected: approved

**T-AG-012** `test_irrelevant_key_does_nothing`
- Action: `await gate.handle_key("x")`
- Expected: `gate.active` unchanged, no events emitted

**T-AG-013** `test_s_key_skips_queue`
- Setup: 3-item approval queue
- Action: `await gate.handle_key("s")`
- Expected: current approval skipped, next shown

**T-AG-014** `test_gate_deactivates_when_queue_empty`
- Setup: 1-item queue
- Action: approve once
- Expected: `gate.active == False`

**T-AG-015** `test_allow_all_auto_approves_remaining_queue`
- Setup: 3-item queue, all same tool_name
- Action: `gate.handle_key("a")`
- Expected: all 3 items resolved as "approved", gate empty

### 9.7 Unit Tests — DoomLoopDetector (tests/unit/test_doom_loop.py)

**T-DL-001** `test_no_detection_below_threshold`
- Action: record same `(tool_name, args_hash)` 2 times
- Expected: `detector.doom_loop_active == False`

**T-DL-002** `test_detection_fires_at_third_repetition`
- Action: record same `(tool_name, args_hash)` 3 times
- Expected: `detector.doom_loop_active == True` after 3rd call

**T-DL-003** `test_different_args_no_detection`
- Action: record `("run_bash", hash1)`, `("run_bash", hash2)`, `("run_bash", hash3)` (distinct)
- Expected: `detector.doom_loop_active == False`

**T-DL-004** `test_different_tool_names_no_detection`
- Action: record `("read_file", hash)`, `("write_file", hash)`, `("read_file", hash)` etc.
- Expected: `detector.doom_loop_active == False`

**T-DL-005** `test_reset_clears_state`
- Setup: doom loop active
- Action: `detector.reset()`
- Expected: `detector.doom_loop_active == False`

**T-DL-006** `test_args_hash_uses_sorted_repr`
- Action: record two tool calls with same args in different key order
- Expected: both have same `args_hash` (order-independent)

**T-DL-007** `test_detection_emits_event`
- Setup: detector connected to mock event emitter
- Action: record 3 identical calls
- Expected: mock received `TUI_DOOM_LOOP_DETECTED` event

**T-DL-008** `test_threshold_is_configurable`
- Action: construct `DoomLoopDetector(threshold=5)`, record 4 identical calls
- Expected: no detection; fires on 5th

**T-DL-009** `test_turn_boundary_resets_counter`
- Setup: 2 identical calls in turn 1
- Action: new turn starts, 1 identical call in turn 2
- Expected: no detection (counter reset per turn)

**T-DL-010** `test_detection_returns_payload`
- Action: detect doom loop
- Expected: `detector.last_detection` has `tool_name`, `args`, `repetition_count=3`

### 9.8 Unit Tests — Symbols and Color (tests/unit/test_symbols.py)

**T-SYM-001** `test_unicode_safe_returns_unicode_when_supported`
- Input: `unicode_level=1`
- Expected: `_unicode_safe("●", "*") == "●"`

**T-SYM-002** `test_unicode_safe_returns_fallback_for_dumb_terminal`
- Input: `unicode_level=0`
- Expected: `_unicode_safe("●", "*") == "*"`

**T-SYM-003** `test_strip_ansi_removes_escape_sequences`
- Input: `"\x1b[32mhello\x1b[0m"`
- Expected: `strip_ansi(input) == "hello"`

**T-SYM-004** `test_strip_ansi_preserves_plain_text`
- Input: `"hello world"`
- Expected: `strip_ansi(input) == "hello world"`

**T-SYM-005** `test_clip_ansi_line_at_width`
- Input: `"\x1b[32m" + "a" * 100 + "\x1b[0m"`, `width=10`
- Expected: plain text of result has at most 10 display columns

**T-SYM-006** `test_spinner_frames_count`
- Expected: `len(SPINNER_FRAMES) == 10`

**T-SYM-007** `test_agent_colors_count`
- Expected: `len(AGENT_COLORS) == 6`

**T-SYM-008** `test_mode_symbols_has_all_six_modes`
- Expected: `MODE_SYMBOLS.keys() == {"AUTO", "PLAN", "ASK", "REVIEW", "SAFE", "DEBUG"}`

**T-SYM-009** `test_mode_colors_has_all_six_modes`
- Expected: `MODE_COLORS.keys() == {"AUTO", "PLAN", "ASK", "REVIEW", "SAFE", "DEBUG"}`

**T-SYM-010** `test_wcswidth_double_width_chars`
- Input: `"日本"` (2 CJK chars, each width 2)
- Expected: `wcswidth_wrapper("日本") == 4`

**T-SYM-011** `test_no_nerd_font_symbols_above_bmp`
- Scan all constants in `symbols.py`
- Expected: no codepoint > 0xFFFF

### 9.9 Unit Tests — MarkdownRenderer (tests/unit/test_markdown_renderer.py)

**T-MD-001** `test_render_markdown_produces_ansi_output`
- Input: `"# Hello\nworld"`
- Expected: result contains `\x1b[` ANSI sequences (bold for header)

**T-MD-002** `test_render_markdown_to_specified_width`
- Input: 200-char single paragraph, `width=80`
- Expected: all result lines have ≤ 80 display columns (strip ANSI, measure)

**T-MD-003** `test_render_markdown_no_trailing_blank_line`
- Input: `"hello world"`
- Expected: last element of result is non-empty

**T-MD-004** `test_render_markdown_never_uses_alternate_screen`
- Monitor bytes written to io.BytesIO during render
- Expected: no `\x1b[?1049h` in output

**T-MD-005** `test_render_markdown_with_code_fence`
- Input: `"```python\nx = 1\n```"`
- Expected: result contains syntax-highlighted "x = 1"

### 9.10 Unit Tests — SessionRecap (tests/unit/test_session_recap.py)

**T-SR-001** `test_generate_returns_list_of_strings`
- Input: 3 finalized turns with output_lines and tool_calls
- Expected: result is non-empty `list[str]`

**T-SR-002** `test_generate_includes_all_turns_since_cutoff`
- Input: 5 turns, `since_timestamp` excludes first 2
- Expected: 3 turns represented in recap

**T-SR-003** `test_generate_no_llm_call`
- Verify: no external I/O during `generate()` (pure computation)
- Method: mock all network calls; none should be invoked

**T-SR-004** `test_recap_truncates_at_max_turns`
- Input: 50 turns passed, `max_turns=10`
- Expected: at most 10 turn summaries in output

**T-SR-005** `test_recap_shows_tool_count`
- Input: turn with 3 tool calls
- Expected: recap for that turn mentions tool count

---

### 9.11 Integration Tests — Pyte Rendering (tests/integration/test_tui_rendering.py)

All integration tests use pyte to emulate a terminal and verify ANSI sequences.

**Setup helper:**
```python
import pyte
import io

COLS, ROWS = 80, 24

def make_pyte_session():
    screen = pyte.Screen(COLS, ROWS)
    stream_p = pyte.Stream(screen)
    return screen, stream_p

def capture_terminal_output(terminal_op):
    buf = io.StringIO()
    # Real Terminal backed by StringIO
    ...
```

**T-INT-001** `test_no_smcup_sequence_in_any_operation`
- Run: full lifecycle (init, turns, shutdown) on `StringIO`-backed terminal
- Assert: `"\x1b[?1049h"` not in output

**T-INT-002** `test_no_rmcup_sequence_in_any_operation`
- Assert: `"\x1b[?1049l"` not in output

**T-INT-003** `test_no_decstbm_scroll_region`
- Assert: no `re.search(r'\x1b\[\d+;\d+r', output)`

**T-INT-004** `test_committed_line_visible_in_pyte_buffer`
- Action: `terminal.commit_lines(["UNIQUE_MARKER_XYZ"])`
- Pyte: feed all bytes to pyte screen
- Assert: "UNIQUE_MARKER_XYZ" present in pyte screen text

**T-INT-005** `test_bottom_block_in_last_rows`
- Action: render a bottom block with input glyph
- Pyte: check screen rows ROWS-5 through ROWS-1
- Assert: "❯" or ">" present in last 5 rows

**T-INT-006** `test_committed_line_not_erased_by_subsequent_set_bottom`
- Action: `commit_lines(["keep me"])`, then `set_bottom(new_frame)` 10 times
- Pyte: check entire screen
- Assert: "keep me" still present somewhere above bottom block

**T-INT-007** `test_bottom_block_erase_correct_height_tracking`
- Action: `set_bottom(frame_3rows)`, then `set_bottom(frame_5rows)`
- Assert: `terminal._bottom_height == 5` after second call

**T-INT-008** `test_bottom_height_zero_after_clear`
- Action: `set_bottom(frame)`, then `clear_bottom()`
- Assert: `terminal._bottom_height == 0`

**T-INT-009** `test_resize_redraws_bottom_at_new_width`
- Action: set `terminal._size = Size(24, 120)`, call `set_bottom(frame_at_80_wide)`
- Verify: frame redrawn with new 120-col width (mock size update)

**T-INT-010** `test_mode_badge_visible_in_status_bar`
- Setup: `status.permission_mode = "REVIEW"`
- Action: full render pipeline tick
- Pyte: bottom 6 rows
- Assert: "REVIEW" visible in bottom block

**T-INT-011** `test_input_glyph_visible_after_commit_lines`
- Action: `commit_lines(long_list)`, then `set_bottom(idle_frame)`
- Pyte: last row
- Assert: "❯" or ">" present

**T-INT-012** `test_tool_call_success_line_committed`
- Setup: turn with completed read_file tool call
- Action: `force_commit(render_turn_to_lines(turn, ...))`
- Pyte: search all rows
- Assert: "read_file" and "✓" present above bottom block

**T-INT-013** `test_diff_block_committed_before_approval_gate`
- Setup: approval required for write_file with 3-line diff
- Action: activate approval gate
- Pyte: check rows above bottom block
- Assert: diff lines ("+++", "---") present in scrollback area before approval rows

**T-INT-014** `test_spinner_frame_changes_between_ticks`
- Action: record bottom_history[0] and bottom_history[2] (simulating two 100ms ticks)
- Assert: the status bar row differs between the two frames (spinner advanced)

**T-INT-015** `test_no_content_above_bottom_block_at_startup`
- Action: startup with empty session
- Pyte: rows above bottom block
- Assert: all cells are blank (no garbage escape sequences)

**T-INT-016** `test_agent_turn_header_color`
- Setup: color enabled terminal
- Action: commit turn with agent_name="main", agent color 0 (magenta)
- Pyte: find header row
- Assert: row contains color attributes for magenta (ANSI code 35)

**T-INT-017** `test_divider_full_width`
- Action: render with COLS=80
- Pyte: identify divider row in bottom block
- Assert: row has 80 consecutive "─" characters (after stripping ANSI)

**T-INT-018** `test_bottom_block_max_height_constraint`
- Setup: large streaming buffer + open dropdown (would exceed 12 rows)
- Action: render
- Assert: `terminal._bottom_height <= min(12, ROWS // 3)`

**T-INT-019** `test_teardown_leaves_cursor_visible`
- Action: `terminal.teardown()`
- Assert: output contains `\x1b[?25h` (show cursor)

**T-INT-020** `test_commit_then_immediate_set_bottom_no_overlap`
- Action: `commit_lines(["line1"])` + `set_bottom(frame)` in one render tick
- Pyte: verify "line1" is in scrollback and bottom block is below it
- Assert: "line1" appears in a row above any bottom-block row

---

### 9.12 E2E Tests — Full Session (tests/e2e/test_tui_e2e.py)

All E2E tests use `FakeTerminal`. No real LLM calls. Agent turns are simulated
by directly calling `TranscriptModel` and `RenderLoop` methods.

**T-E2E-001** `test_single_turn_full_lifecycle`
- Simulate: user submits "hello", agent streams 50 tokens, turn completes
- Assert: `fake.committed_lines` non-empty after turn
- Assert: `fake._current_bottom` has input glyph (idle state)
- Assert: no ANSI cursor sequences in committed lines except color codes

**T-E2E-002** `test_streaming_content_in_bottom_not_committed`
- Simulate: 20 tokens streamed without completing turn
- Assert: `fake.committed_lines == []` (streaming, not committed)
- Assert: streaming text visible in `fake._current_bottom`

**T-E2E-003** `test_multiple_turns_accumulate_in_scrollback`
- Simulate: 3 complete turns
- Assert: `len(fake.committed_lines) > 0` after each turn
- Assert: committed_lines count monotonically increases
- Assert: content from all 3 turns present in `fake.committed_lines`

**T-E2E-004** `test_tool_call_lifecycle_committed_only_on_complete`
- Simulate: tool call PENDING → RUNNING → SUCCESS
- Assert: no tool call line in `fake.committed_lines` until SUCCESS
- Assert: after SUCCESS, `fake.committed_lines` contains "✓"

**T-E2E-005** `test_tool_call_error_shows_cross`
- Simulate: tool call → ERROR state
- Assert: `fake.committed_lines` contains "✗" and error message

**T-E2E-006** `test_approval_gate_blocks_committed_output`
- Simulate: approval required for write_file
- Assert: `fake._current_bottom` contains "approve?" while gate active
- Assert: committed lines do NOT advance while gate active

**T-E2E-007** `test_approval_gate_approval_then_normal_flow`
- Simulate: approval granted → tool runs → turn completes
- Assert: after approval, tool result committed and input bar idle

**T-E2E-008** `test_doom_loop_detection_changes_input_mode`
- Simulate: same tool called 3 times in one turn
- Assert: `input_state.mode == InputMode.DOOM_LOOP`
- Assert: committed lines contain doom loop banner

**T-E2E-009** `test_parallel_agents_different_colors`
- Simulate: two agents with different agent_ids completing turns
- Assert: turn headers in `fake.committed_lines` have different ANSI color codes

**T-E2E-010** `test_session_recap_after_idle`
- Simulate: 5 turns completed, 3+ minute idle (mock `time.time`)
- Action: user types new message
- Assert: recap lines committed before new intent dispatched

**T-E2E-011** `test_sigterm_flushes_and_clears_bottom`
- Simulate: turn in progress when SIGTERM fires
- Action: call signal handler directly
- Assert: `fake._bottom_height == 0` after teardown

**T-E2E-012** `test_memory_eviction_at_200_turns`
- Simulate: add 201 turns
- Assert: `transcript._turns[0].output_lines == []` (evicted)
- Assert: `len(transcript._turns) == 201` (metadata kept)

**T-E2E-013** `test_headless_mode_json_lines_output`
- Setup: `run_headless()` with mock processor
- Simulate: one complete turn
- Assert: stdout contains valid JSON lines with `turn_id` and `text` fields

**T-E2E-014** `test_no_color_mode_no_ansi_in_committed`
- Setup: `NO_COLOR=1`, `FakeTerminal` with `capabilities.no_color=True`
- Simulate: one complete turn
- Assert: no `\x1b[` in any `fake.committed_lines` element

**T-E2E-015** `test_ctrl_c_cancels_turn`
- Simulate: turn STREAMING
- Action: inject SIGINT (call handler directly)
- Assert: `transcript.turns[-1].state == TurnState.CANCELLED`
- Assert: committed line contains "[cancelled]"

---

## 10. Acceptance Criteria

All criteria are binary pass/fail. All must pass before the TUI layer is considered
complete.

### 10.1 Functional Criteria

**AC-FUNC-001** No alternate screen: `grep -r '\\x1b\[?1049h' src/agenthicc/tui/` returns zero matches.

**AC-FUNC-002** No DECSTBM scroll region: `grep -rP '\\x1b\[\d+;\d+r' src/agenthicc/tui/` returns zero matches.

**AC-FUNC-003** Single stdout owner: `grep -rn 'sys.stdout.write\|print(' src/agenthicc/tui/ | grep -v terminal.py` returns zero matches (only `terminal.py` may write to stdout).

**AC-FUNC-004** All six permission modes render correctly: `test_render_mode_badge_all_six_modes` passes.

**AC-FUNC-005** Approval gate shows diff before gate: `test_diff_committed_before_approval_gate` passes.

**AC-FUNC-006** Doom loop detection fires at 3 repetitions: `test_detection_fires_at_third_repetition` passes.

**AC-FUNC-007** Session resume replays last 20 turns: integration test `test_session_resume_replays_turns` passes.

**AC-FUNC-008** SIGWINCH redraws within 50ms: `test_resize_redraws_bottom_at_new_width` passes with timing assertion.

**AC-FUNC-009** Ctrl+C cancels current turn: `test_ctrl_c_cancels_turn` passes.

**AC-FUNC-010** `NO_COLOR=1` produces zero ANSI codes in committed output: `test_no_color_mode_no_ansi_in_committed` passes.

### 10.2 Performance Gates

**AC-PERF-001** Cold start < 800ms: measured by benchmark test `test_cold_start_latency`; must pass on a 2021+ laptop equivalent.

**AC-PERF-002** Frame render < 16ms: `FrameComposer.compose()` + `Terminal.set_bottom()` combined in `test_frame_render_time` benchmark.

**AC-PERF-003** Single write per frame: `FakeTerminal.write_call_count == 1` per `set_bottom()` call, verified in `test_set_bottom_single_write_call`.

**AC-PERF-004** Memory at 200 turns < 10MB: `tracemalloc` benchmark `test_memory_200_turns` must show `< 10 * 1024 * 1024` bytes for transcript.

**AC-PERF-005** CPU idle < 1%: `test_cpu_idle_between_turns` passes using `psutil` sampling.

### 10.3 Code Quality Gates

**AC-QUAL-001** mypy clean: `uv run mypy src/agenthicc/tui/` returns exit code 0 with no errors.

**AC-QUAL-002** ruff clean: `uv run ruff check src/agenthicc/tui/ tests/unit/test_terminal.py tests/unit/test_frame_composer.py` returns exit code 0.

**AC-QUAL-003** ruff format: `uv run ruff format --check src/agenthicc/tui/` returns exit code 0.

**AC-QUAL-004** Test coverage — Terminal: `uv run pytest tests/unit/test_terminal.py --cov=agenthicc.tui.terminal --cov-fail-under=95` passes.

**AC-QUAL-005** Test coverage — FrameComposer: `--cov-fail-under=100` for `test_frame_composer.py`.

**AC-QUAL-006** Test coverage — ApprovalGate: `--cov-fail-under=100` for `test_approval_gate.py`.

**AC-QUAL-007** Test coverage — InputState: `--cov-fail-under=95` for `test_input_state.py`.

**AC-QUAL-008** Test coverage — TranscriptModel: `--cov-fail-under=95` for `test_tui_transcript.py`.

**AC-QUAL-009** No circular imports: `python -c "from agenthicc.tui import AgenthiccApp"` succeeds without `ImportError`.

**AC-QUAL-010** `from __future__ import annotations` present in every `.py` file in `src/agenthicc/tui/`.

**AC-QUAL-011** All public symbols in `llms-full.txt`: `uv run python scripts/check_llms.py` passes.

**AC-QUAL-012** No Nerd Font symbols: `python -c "import agenthicc.tui.symbols; import sys; [sys.exit(1) for c in str(vars(agenthicc.tui.symbols)) if ord(c) > 0xFFFF]"` exits 0.

### 10.4 Compatibility Gates

**AC-COMPAT-001** pyte integration tests pass with 0 failures: `uv run pytest tests/integration/test_tui_rendering.py -v` all green.

**AC-COMPAT-002** Headless JSON-lines mode produces parseable output: `test_headless_mode_json_lines_output` passes.

**AC-COMPAT-003** FakeTerminal E2E tests pass with 0 failures: `uv run pytest tests/e2e/test_tui_e2e.py -v` all green.

---

## Appendix A: Python Interface Signatures Reference

Quick-reference for all public interfaces. Every signature must exactly match the implementation.

```python
# terminal.py
class Size(NamedTuple):
    rows: int
    cols: int

@dataclass(frozen=True)
class TerminalCapabilities:
    color_depth: int           # 0, 8, 256, or 16777216
    unicode_level: int         # 0=ASCII, 1=BMP
    synchronized_output: bool
    hyperlinks: bool
    no_color: bool

@dataclass(frozen=True)
class Frame:
    rows: list[str]
    height: int
    cursor_row: int
    cursor_col: int

class Terminal:
    def __init__(self, fd: int = 1) -> None: ...
    @property
    def size(self) -> Size: ...
    @property
    def capabilities(self) -> TerminalCapabilities: ...
    @property
    def resize_pending(self) -> bool: ...
    def commit_lines(self, lines: list[str]) -> None: ...
    def set_bottom(self, frame: Frame) -> None: ...
    def clear_bottom(self) -> None: ...
    def update_size(self) -> None: ...
    def teardown(self) -> None: ...
    def _write_atomic(self, data: bytes) -> None: ...

class FakeTerminal:
    committed_lines: list[str]
    bottom_history: list[Frame]
    write_call_count: int
    def __init__(self) -> None: ...
    def commit_lines(self, lines: list[str]) -> None: ...
    def set_bottom(self, frame: Frame) -> None: ...
    def clear_bottom(self) -> None: ...
    def update_size(self) -> None: ...
    def teardown(self) -> None: ...


# frame_composer.py
class FrameComposer:
    def __init__(self, color: bool = True) -> None: ...
    def compose(
        self,
        transcript: TranscriptModel,
        input_state: InputState,
        size: Size,
        now: float | None = None,
    ) -> Frame: ...


# render_loop.py
class RenderLoop:
    MIN_TICK_INTERVAL: float  # = 0.050
    def __init__(
        self,
        terminal: Terminal,
        composer: FrameComposer,
        transcript: TranscriptModel,
        input_state: InputState,
    ) -> None: ...
    async def run(self) -> None: ...
    def force_commit(self, lines: list[str]) -> None: ...
    def request_redraw(self) -> None: ...
    def shutdown(self) -> None: ...


# input_state.py
class InputState:
    def __init__(self, on_submit: Callable[[str], Awaitable[None]]) -> None: ...
    @property
    def text(self) -> str: ...
    @property
    def cursor(self) -> int: ...
    @property
    def dropdown(self) -> DropdownState: ...
    @property
    def dropdown_open(self) -> bool: ...
    @property
    def mode(self) -> InputMode: ...
    @property
    def disabled(self) -> bool: ...
    def insert(self, char: str) -> None: ...
    def backspace(self) -> None: ...
    def delete_forward(self) -> None: ...
    def move_left(self) -> None: ...
    def move_right(self) -> None: ...
    def move_word_left(self) -> None: ...
    def move_word_right(self) -> None: ...
    def move_to_start(self) -> None: ...
    def move_to_end(self) -> None: ...
    def kill_to_end(self) -> None: ...
    def kill_to_start(self) -> None: ...
    def kill_word_back(self) -> None: ...
    def yank(self) -> None: ...
    def history_up(self) -> None: ...
    def history_down(self) -> None: ...
    def insert_newline(self) -> None: ...
    def set_mode(self, mode: InputMode) -> None: ...
    def set_disabled(self, disabled: bool) -> None: ...
    def close_dropdown(self) -> None: ...
    def dropdown_next(self) -> None: ...
    def dropdown_prev(self) -> None: ...
    def select_dropdown_item(self) -> None: ...
    async def submit(self) -> None: ...
    def render_lines(self, prompt_glyph: str, width: int, color: bool = True) -> list[str]: ...


# transcript.py
class TranscriptModel:
    MAX_TURNS_IN_MEMORY: ClassVar[int]  # = 200
    MAX_LINES_PER_TURN: ClassVar[int]   # = 500
    MAX_DIFF_LINES: ClassVar[int]       # = 50
    def __init__(self) -> None: ...
    @property
    def turns(self) -> list[AgentTurnEntry]: ...
    @property
    def streaming_buffer(self) -> str: ...
    @property
    def committed_cursor(self) -> int: ...
    @property
    def all_committed_lines(self) -> list[str]: ...
    def add_turn(self, turn: AgentTurnEntry) -> None: ...
    def append_streaming_token(self, token: str) -> None: ...
    def clear_streaming_buffer(self) -> None: ...
    def complete_turn(self, turn_id: str, final_lines: list[str]) -> None: ...
    def cancel_turn(self, turn_id: str, partial_text: str) -> None: ...
    def error_turn(self, turn_id: str, error_message: str) -> None: ...
    def add_tool_call(self, turn_id: str, tool_call: ToolCallEntry) -> None: ...
    def update_tool_call(self, tool_id: str, state: ToolCallState, **kwargs: object) -> None: ...
    def commit_lines(self, lines: list[str]) -> None: ...
    def _evict_old_turns(self) -> None: ...

def render_turn_to_lines(turn: AgentTurnEntry, color: bool, cols: int) -> list[str]: ...
def render_tool_call_line(tc: ToolCallEntry, color: bool) -> str: ...


# event_adapter.py
class TUIEventAdapter:
    def __init__(
        self,
        processor: EventProcessor,
        transcript: TranscriptModel,
        status: StatusState,
        render_loop: RenderLoop,
        approval_gate: ApprovalGate,
        doom_detector: DoomLoopDetector,
    ) -> None: ...
    async def _adapter_loop(self) -> None: ...
    def shutdown(self) -> None: ...


# approval_gate.py
class ApprovalGate:
    def __init__(
        self,
        processor: EventProcessor,
        terminal: Terminal,
        render_loop: RenderLoop,
    ) -> None: ...
    @property
    def active(self) -> bool: ...
    def activate(
        self,
        tool_id: str,
        tool_name: str,
        args: dict[str, object],
        proposed_diff: str,
    ) -> None: ...
    async def handle_key(self, key: str) -> None: ...
    def render_bottom_rows(self, width: int, color: bool) -> list[str]: ...


# doom_loop.py
class DoomLoopDetector:
    def __init__(self, threshold: int = 3) -> None: ...
    @property
    def doom_loop_active(self) -> bool: ...
    def record_tool_call(
        self,
        turn_id: str,
        tool_name: str,
        args: dict[str, object],
    ) -> bool: ...  # returns True if doom loop just fired
    def reset(self) -> None: ...


# session_recap.py
class SessionRecapGenerator:
    def __init__(self, max_turns: int = 10) -> None: ...
    def generate(
        self,
        turns: list[AgentTurnEntry],
        since_timestamp: float,
    ) -> list[str]: ...


# markdown_renderer.py
def render_markdown_to_lines(text: str, width: int, color: bool = True) -> list[str]: ...


# symbols.py
SPINNER_FRAMES: list[str]     # 10 braille frames
SPINNER_ASCII: list[str]      # 4 ASCII frames
AGENT_COLORS: list[str]       # 6 ANSI color codes (as strings, e.g. "35")
MODE_SYMBOLS: dict[str, str]  # mode → symbol
MODE_COLORS: dict[str, str]   # mode → ANSI color code string

def _unicode_safe(unicode_str: str, ascii_fallback: str, unicode_level: int = 1) -> str: ...


# color.py
def strip_ansi(text: str) -> str: ...
def clip_ansi_line(text: str, max_cols: int) -> str: ...
def wcswidth_wrapper(text: str) -> int: ...


# app.py
def run_tui(
    processor: EventProcessor,
    config: AgenthiccConfig,
    session_id: str,
    resume_session_id: str | None = None,
) -> int: ...

def run_headless(
    processor: EventProcessor,
    config: AgenthiccConfig,
    session_id: str,
) -> int: ...
```

---

## Appendix B: Required Dependencies

Add to `pyproject.toml` under `[project.dependencies]`:

```toml
"textual>=0.56.0",          # inline mode; MarkdownStream; TextArea
"wcwidth>=0.2.13",          # Unicode display width
"pyte>=0.8.0",              # integration test terminal emulation (test dep)
"pytest-textual-snapshot",  # snapshot testing (test dep)
```

Optional acceleration (add to extras):
```toml
[project.optional-dependencies]
speedups = ["textual-speedups"]
```

`textual-speedups` is OPTIONAL; the app must work identically without it.
Install in production with `pip install agenthicc[speedups]`.

---

## Appendix C: File-by-File Type Annotation Checklist

Every file in `src/agenthicc/tui/` must pass all items:

- [ ] `from __future__ import annotations` on line 1
- [ ] All function parameters have type annotations
- [ ] All return types annotated (including `-> None`)
- [ ] No bare `dict`, `list`, `tuple` — always parameterized
- [ ] `TYPE_CHECKING` imports used for forward references that would cause cycles
- [ ] No `Any` without explicit `# type: ignore[assignment]` comment
- [ ] `ClassVar` used for class-level constants
- [ ] Frozen dataclasses use `@dataclass(frozen=True)`
- [ ] Mutable dataclasses use `@dataclass` (not frozen)
- [ ] Enums inherit from `(str, Enum)` when used as JSON keys; `Enum` otherwise

---

*End of Application Architecture PRD v1.0*
