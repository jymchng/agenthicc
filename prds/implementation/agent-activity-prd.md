# Agent Activity System — Implementation PRD

**Document version:** 1.0  
**Date:** 2026-06-13  
**Target framework:** Textual (Python), inline mode, no alternate screen  
**Consuming PRD:** tui-redesign-prd.md (master)  
**Component inventory reference:** component-inventory.md §3.7, §3.8, §3.9, §3.16  
**Author:** autonomous coding agent  

---

## 0. Purpose and Scope

This PRD specifies all code that must be written to implement the **agent activity
system** — the set of components that display what an agent is doing right now:
its thinking state, current tool, streaming token rate, elapsed time, and the
animated `ThinkingIndicator` widget.

The implementation touches four layers:

| Layer | Files to create or modify |
|---|---|
| State model | `src/agenthicc/tui/agent_status.py` (new) |
| Status bar rendering | `src/agenthicc/tui/status_bar.py` (new) |
| Textual widget | `src/agenthicc/tui/thinking_indicator.py` (new) |
| Signal/event wiring | `src/agenthicc/tui/event_adapter.py` (new) |
| Tests | `tests/unit/test_agent_status.py`, `tests/unit/test_status_bar.py`, `tests/unit/test_thinking_indicator.py`, `tests/integration/test_agent_activity_integration.py`, `tests/e2e/test_agent_activity_e2e.py` |

**Hard constraints (non-negotiable):**

- No alternate screen. Textual `App.run(inline=True)` only.
- All Python. No JavaScript, Rust, or Node.
- All type hints. `mypy --strict` must pass on every new file.
- `ruff check` and `ruff format --check` must pass.
- Must integrate with the existing `EventProcessor` in
  `src/agenthicc/kernel/processor.py` via its `subscribe()` queue.
- Must not import from `src/agenthicc/tui/app.py` (that file does not yet
  exist at the time of writing). Imports flow one way: app.py → everything else.
- `from __future__ import annotations` on every source file.
- `asyncio_mode = "auto"` in pytest — no `@pytest.mark.asyncio` decorator.

---

## 1. Agent Status Model

### 1.1 `AgentStatus` Enum

**File:** `src/agenthicc/tui/agent_status.py`

The existing `AgentStatus` in `src/agenthicc/kernel/state.py` has only three
values (`idle`, `busy`, `terminated`) — these are coarse kernel lifecycle states
for agent pool management. The TUI needs a finer-grained status that captures
the UX-relevant phase of an agent's work.

The new `AgentStatus` enum lives **only in the TUI layer**. It is distinct from
`kernel.state.AgentStatus` and must never be imported into the kernel. Import
the kernel type as `KernelAgentStatus` when both are needed in the same file.

```python
from __future__ import annotations

from enum import Enum

__all__ = ["AgentStatus"]


class AgentStatus(Enum):
    """Fine-grained TUI-layer status for a running agent.

    Transitions:
        IDLE → THINKING           (intent_submitted event received)
        THINKING → STREAMING      (first token arrives via streaming_token event)
        STREAMING → RUNNING_TOOL  (tool_call_started event)
        RUNNING_TOOL → THINKING   (tool_call_complete event, agent continues)
        RUNNING_TOOL → STREAMING  (tool_call_complete, agent resumes streaming)
        STREAMING → WAITING_APPROVAL  (approval_required effect)
        THINKING → WAITING_APPROVAL   (approval_required without prior streaming)
        WAITING_APPROVAL → THINKING   (approval_granted or approval_denied)
        THINKING → COMPLETE       (agent_run_complete event, no error)
        STREAMING → COMPLETE      (agent_run_complete after final token)
        RUNNING_TOOL → COMPLETE   (agent_run_complete after final tool)
        * → ERROR                 (any error event from the agent)
        ERROR → IDLE              (user acknowledges or new intent submitted)
        COMPLETE → IDLE           (user submits new intent)
    """

    IDLE = "idle"
    THINKING = "thinking"              # LLM is generating; no tokens yet
    STREAMING = "streaming"            # tokens are arriving
    RUNNING_TOOL = "running_tool"      # a tool call is executing
    WAITING_APPROVAL = "waiting_approval"  # blocked on user approval gate
    ERROR = "error"                    # agent or tool error
    COMPLETE = "complete"              # turn finished successfully
```

**Why these values, not others:** The research (Section 3.7 of component-inventory.md)
lists `IDLE`, `THINKING`, `RUNNING_TOOLS`, `AWAITING_APPROVAL`, `ERROR`,
`STREAMING`. The additional `COMPLETE` state is needed so the status bar can
display a transient success indicator before transitioning to `IDLE` on the next
user message.

### 1.2 `AgentStatusState` Dataclass

**File:** `src/agenthicc/tui/agent_status.py` (continued)

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = ["AgentStatus", "AgentStatusState"]


@dataclass
class AgentStatusState:
    """Mutable TUI-layer record of a single agent's current activity.

    One instance exists per active agent. The orchestrator creates an instance
    when an agent starts and destroys it when the agent terminates.

    All fields are mutable; the TUIEventAdapter updates them in-place on the
    asyncio event loop (no locking needed — single-writer).

    Fields
    ------
    agent_id : str
        Stable identifier matching ``AgentInstance.agent_id`` in the kernel.
    status : AgentStatus
        Current phase. See ``AgentStatus`` docstring for transition rules.
    model_id : str
        The model label to display (e.g. ``"claude-sonnet-4-6"``). Set from
        the ``agent_spawn`` event payload field ``"model_id"``.
    turn_start_time : float | None
        ``time.monotonic()`` when the current turn began. ``None`` when
        ``status == IDLE``.  Used to compute elapsed time for the status bar.
    input_tokens : int
        Cumulative input tokens consumed in the current session.
    output_tokens : int
        Cumulative output tokens produced in the current session.
    cost_usd : float
        Estimated cumulative cost in USD for the current session.
    streaming_rate_tps : float
        Tokens per second over the last 2 seconds of streaming. Computed by
        a rolling window of (timestamp, token_count) pairs stored in
        ``_token_window``. Zero when not streaming.
    partial_text : str
        The incomplete LLM text that has arrived so far in the current
        streaming turn. Cleared when the turn completes. Used to drive the
        live streaming display in the bottom block.
    current_tool_name : str | None
        The name of the currently-executing tool. ``None`` unless
        ``status == RUNNING_TOOL``.
    current_tool_call_id : str | None
        The ``tool_call_id`` of the currently-executing tool. ``None`` unless
        ``status == RUNNING_TOOL``.
    tool_start_time : float | None
        ``time.monotonic()`` when the current tool call began. ``None`` unless
        ``status == RUNNING_TOOL``.
    error_message : str | None
        The error text when ``status == ERROR``. ``None`` otherwise.
    turn_count : int
        Number of complete turns in this session (incremented when a turn
        reaches COMPLETE or ERROR).
    color_index : int
        0-based index into the palette of 6 agent colors for multi-agent
        display. Assigned by the TUIEventAdapter when the agent is spawned.
        The palette is: magenta, cyan, yellow, blue, green, red.
    """

    agent_id: str
    status: AgentStatus = AgentStatus.IDLE
    model_id: str = ""
    turn_start_time: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    streaming_rate_tps: float = 0.0
    partial_text: str = ""
    current_tool_name: str | None = None
    current_tool_call_id: str | None = None
    tool_start_time: float | None = None
    error_message: str | None = None
    turn_count: int = 0
    color_index: int = 0
    _token_window: list[tuple[float, int]] = field(
        default_factory=list, repr=False
    )

    # ── computed properties ───────────────────────────────────────────────

    def elapsed_seconds(self) -> float:
        """Seconds since the current turn started, or 0.0 if idle."""
        if self.turn_start_time is None:
            return 0.0
        return time.monotonic() - self.turn_start_time

    def tool_elapsed_seconds(self) -> float:
        """Seconds since the current tool call started, or 0.0."""
        if self.tool_start_time is None:
            return 0.0
        return time.monotonic() - self.tool_start_time

    def update_streaming_rate(self, new_tokens: int) -> None:
        """Add ``new_tokens`` to the rolling 2-second window and recompute rate.

        Call this every time a ``streaming_token`` event arrives.
        The window stores ``(monotonic_timestamp, cumulative_tokens)`` pairs.
        Pairs older than 2 seconds are evicted before each update.
        """
        now = time.monotonic()
        cutoff = now - 2.0
        self._token_window = [
            (t, n) for (t, n) in self._token_window if t >= cutoff
        ]
        self._token_window.append((now, new_tokens))
        if len(self._token_window) >= 2:
            total = sum(n for (_, n) in self._token_window)
            window_seconds = now - self._token_window[0][0]
            self.streaming_rate_tps = (
                total / window_seconds if window_seconds > 0 else 0.0
            )
        else:
            self.streaming_rate_tps = 0.0

    def reset_for_new_turn(self) -> None:
        """Reset per-turn fields; called when a new intent is submitted."""
        self.partial_text = ""
        self.current_tool_name = None
        self.current_tool_call_id = None
        self.tool_start_time = None
        self.error_message = None
        self.turn_start_time = time.monotonic()
        self._token_window = []
        self.streaming_rate_tps = 0.0
```

### 1.3 Status Transition Rules

The following table is the **canonical** transition table. The `TUIEventAdapter`
(Section 5) consults this table when processing each kernel event. Any transition
not listed here is **illegal** and must raise `ValueError` in tests.

```
ASCII State Machine:

                       intent_submitted
    ┌──────────────────────────────────────────────────────┐
    │                                                      ▼
  [IDLE] ←──────────────────────────────────────────── [COMPLETE]
    │       (next intent submitted or auto-reset 3s)       ▲
    │                                                      │ agent_run_complete
    │ intent_submitted                                     │ (no error)
    ▼                                                      │
  [THINKING] ──────────────────────────────────────────────┤
    │   │    │                                             │
    │   │    └──────────────────────────────────────── [ERROR]
    │   │            any error event                      │
    │   │                                                  │ user_ack or
    │   │ streaming_token (first)                          │ intent_submitted
    │   ▼                                                  │
    │ [STREAMING] ─────────────────────────────────────────┤
    │   │    │   │                                         │
    │   │    │   └─────────────────────────────────────────┘ agent_run_complete
    │   │    │                                               (after last token)
    │   │    └────────────────────────────┐
    │   │     tool_call_started            │
    │   │                                  ▼
    │   │                         [RUNNING_TOOL] ──────────► [ERROR]
    │   │                              │   │           error event
    │   │                              │   │
    │   │         tool_call_complete ──┘   │ approval_required
    │   │         (agent continues)        ▼
    │   │                         [WAITING_APPROVAL]
    │   │                              │
    │   │          approval_granted ───┘
    │   │          or approval_denied
    │   │
    │   └─────────────────────────────── [WAITING_APPROVAL]
    │         approval_required               │
    │         (from THINKING)                 │ approval_granted/denied
    └─────────────────────────────────────────┘
              (agent resumes THINKING)
```

**Transition side effects (what must happen at each transition):**

| From → To | Side effects |
|---|---|
| IDLE → THINKING | `reset_for_new_turn()`, set `status = THINKING` |
| THINKING → STREAMING | set `status = STREAMING`, emit first token to `partial_text` |
| STREAMING → RUNNING_TOOL | set `status = RUNNING_TOOL`, set `current_tool_name`, set `tool_start_time = time.monotonic()` |
| RUNNING_TOOL → THINKING | set `status = THINKING`, clear tool fields |
| RUNNING_TOOL → STREAMING | set `status = STREAMING`, clear tool fields, append token to `partial_text` |
| THINKING → WAITING_APPROVAL | set `status = WAITING_APPROVAL` |
| STREAMING → WAITING_APPROVAL | set `status = WAITING_APPROVAL` |
| WAITING_APPROVAL → THINKING | set `status = THINKING` |
| any → COMPLETE | set `status = COMPLETE`, increment `turn_count`, clear `partial_text` |
| any → ERROR | set `status = ERROR`, set `error_message` |
| ERROR → IDLE | set `status = IDLE`, clear `error_message` |
| COMPLETE → IDLE | set `status = IDLE` |

---

## 2. Status Bar Rendering

### 2.1 Active Status Line

**File:** `src/agenthicc/tui/status_bar.py`

The status bar is a **pure function** — `render_status_line` takes an
`AgentStatusState` plus a wall clock reference and returns a list of `str`,
each being one formatted terminal line. No I/O, no side effects.

#### 2.1.1 Thinking animation: `_thinking_wave`

```python
def _thinking_wave(frame: int) -> str:
    """Return a single braille spinner character for frame ``frame``.

    Cycles through 10 braille frames at 100ms per frame (10 fps).
    ``frame`` is the monotonically-increasing frame counter; the function
    takes ``frame % 10`` to select the character.

    Braille spinner sequence (U+2800 block):
        ⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏

    In 8-color degraded mode (TERM=xterm or --ascii flag), falls back to
        | / - \\ | / - \\ ...  (4-frame ASCII spinner, frame % 4)
    """
```

Full implementation spec:

```python
_BRAILLE_FRAMES: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ASCII_FRAMES: tuple[str, ...] = ("|", "/", "-", "\\")


def _thinking_wave(frame: int, ascii_mode: bool = False) -> str:
    if ascii_mode:
        return _ASCII_FRAMES[frame % 4]
    return _BRAILLE_FRAMES[frame % 10]
```

#### 2.1.2 Token counter display

Format for the token section of the status bar:

```
↑1.2k ↓456  ($0.0031)
```

Where `↑` = input tokens, `↓` = output tokens. Values above 10,000 are
abbreviated with `k` suffix (`10.1k`). Values above 1,000,000 use `M` suffix.

```python
def _format_tokens(input_tokens: int, output_tokens: int) -> str:
    """Format token counts as ``↑Xk ↓Y  ($cost)``."""
```

#### 2.1.3 Elapsed time display

```python
def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as a compact string.

    0–59s   → "5s"
    60–3599s → "2m14s"
    3600+s  → "1h02m"
    """
```

#### 2.1.4 Exact format strings per status

```python
# THINKING:
# " ● Thinking  ⠋  5s  ↑1.2k ↓0  [claude-sonnet-4-6]"
THINKING_FORMAT = " {status_icon} Thinking  {spinner}  {elapsed}  {tokens}  [{model}]"

# STREAMING:
# " ~ Streaming  ⠙  3s  ↑1.2k ↓456  12 tok/s  [claude-sonnet-4-6]"
STREAMING_FORMAT = " {status_icon} Streaming  {spinner}  {elapsed}  {tokens}  {rate}  [{model}]"

# RUNNING_TOOL:
# " ▶ read_file  ⠹  1.2s  ↑1.2k ↓456  [claude-sonnet-4-6]"
RUNNING_TOOL_FORMAT = " {status_icon} {tool_name}  {spinner}  {tool_elapsed}  {tokens}  [{model}]"

# WAITING_APPROVAL:
# " ⚠ Awaiting approval — write_file  ↑1.2k ↓456  [claude-sonnet-4-6]"
WAITING_APPROVAL_FORMAT = " {status_icon} Awaiting approval — {tool_name}  {tokens}  [{model}]"

# ERROR:
# " ✗ Error: <message truncated to 40 chars>  [claude-sonnet-4-6]"
ERROR_FORMAT = " {status_icon} Error: {error_msg}  [{model}]"

# COMPLETE:
# " ✓ Done  Turn 3  ↑2.1k ↓892  ($0.0071)  [claude-sonnet-4-6]"
COMPLETE_FORMAT = " {status_icon} Done  Turn {turn_count}  {tokens}  [{model}]"
```

**Status icon mapping:**

| Status | Icon | ANSI color | NO_COLOR text |
|---|---|---|---|
| IDLE | `○` | dim white `\033[2m` | `○` |
| THINKING | `●` | yellow `\033[33m` | `●` |
| STREAMING | `~` | green `\033[32m` | `~` |
| RUNNING_TOOL | `▶` | cyan `\033[36m` | `>` |
| WAITING_APPROVAL | `⚠` | bold yellow `\033[1;33m` | `!` |
| ERROR | `✗` | red `\033[31m` | `X` |
| COMPLETE | `✓` | green `\033[32m` | `V` |

### 2.2 Idle Status Line

When `status == AgentStatus.IDLE`:

```
# Format:
# " ○ Idle  [AUTO]  claude-sonnet-4-6  Turn 3  ↑2.1k ↓892  ($0.0071)  [a3f8]"
IDLE_FORMAT = " {status_icon} Idle  [{mode}]  {model}  Turn {turn_count}  {tokens}  [{session_id}]"
```

Fields:

- `mode` — current permission mode label (AUTO, PLAN, ASK, REVIEW, SAFE, DEBUG).
  Colors per master PRD §8.2: AUTO=green bold, PLAN=yellow bold, ASK=cyan bold,
  REVIEW=blue bold, SAFE=red bold, DEBUG=magenta bold.
- `session_id` — first 4 hex characters of `AppState.session_id`.
- `tokens` — cumulative session totals formatted as `↑Xk ↓Y  ($0.00XX)`.
- `turn_count` — number of completed turns.

**Truncation rules** (enforced by `_truncate_for_width(line: str, cols: int) -> str`):

When the idle format string exceeds `cols - 1` characters (measured with
`wcwidth.wcswidth`), fields are dropped in this priority order (last dropped
first):
1. Drop session ID segment
2. Truncate model name to 16 characters
3. Drop turn count
4. Drop tokens/cost

### 2.3 Streaming Partial Text Zone

During `STREAMING`, the bottom block shows 4–8 lines of the most recent partial
text above the status bar. This is the "live streaming zone."

**Specification:**

- Maximum 8 lines, minimum 0 lines.
- Only the **tail** of `partial_text` is shown: split on `\n`, take last 8 lines.
- Each line is truncated to `cols - 2` columns with `wcwidth`.
- Rendered in dim style: `\033[2m{line}\033[0m`.
- The zone is cleared when the turn transitions to COMPLETE (partial text is
  flushed to the committed transcript).
- The zone is never rendered in `IDLE`, `ERROR`, `COMPLETE`, or
  `WAITING_APPROVAL` states.

```python
def render_streaming_zone(
    partial_text: str,
    cols: int,
    max_lines: int = 8,
    no_color: bool = False,
) -> list[str]:
    """Return up to ``max_lines`` dim-styled lines from the tail of ``partial_text``.

    Parameters
    ----------
    partial_text:
        The accumulated partial LLM response text.
    cols:
        Terminal column width for line truncation.
    max_lines:
        Maximum number of lines to return (default 8, minimum 0).
    no_color:
        If True, suppress ANSI dim styling.

    Returns
    -------
    list[str]
        Each element is a terminal-ready string, possibly with ANSI escape codes.
        Empty list when partial_text is empty or status is not STREAMING.
    """
```

Full `render_streaming_zone` implementation contract:
1. Split `partial_text` on `"\n"`.
2. Take the last `min(max_lines, len(lines))` lines.
3. For each line: `wcwidth`-truncate to `cols - 2` characters.
4. If `no_color` is False: wrap each line in `\033[2m` ... `\033[0m`.
5. Return the list. Caller prepends it to the bottom block rows.

---

## 3. Activity Feed

### 3.1 `ThinkingIndicator` Component

**File:** `src/agenthicc/tui/thinking_indicator.py`

This is the Textual widget that appears inside `AgentMessage` in the
`ChatTranscript` between the moment the agent turn starts and the first token
arrives. It is **unmounted** (not hidden) when the first token arrives.

#### Full class specification

```python
from __future__ import annotations

import time
from typing import ClassVar

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

__all__ = ["ThinkingIndicator"]

_SPINNER_FRAMES: tuple[str, ...] = ("◐", "◑", "◒", "◓")
_EXTENDED_SPINNER_FRAMES: tuple[str, ...] = ("◐", "◑", "◒", "◓", "◐", "◑", "◒", "◓")


class ThinkingIndicator(Widget):
    """Animated spinner shown while the agent has not yet produced any tokens.

    Lifecycle:
    - Mount when a new agent turn begins (before first token).
    - Unmount when the first token arrives (transition THINKING → STREAMING).
    - Never hidden — always either mounted or unmounted.

    CSS class ``--thinking-extended`` is added when ``extended`` is True,
    turning the spinner cyan to indicate extended thinking mode.

    Attributes
    ----------
    DEFAULT_CSS : ClassVar[str]
        Inline CSS for the widget. Relies only on Textual built-in CSS features.
    label : reactive[str]
        The text label beside the spinner. Default "Thinking…".
        Reactive so that callers can update it without remounting.
    extended : reactive[bool]
        When True, the spinner colour changes to cyan and the label is updated
        to "Thinking (extended)…".  Default False.
    interval_ms : int
        Animation tick interval in milliseconds. Default 200 (5 fps).
    """

    DEFAULT_CSS: ClassVar[str] = """
    ThinkingIndicator {
        height: 1;
        color: $warning;
        padding-left: 2;
    }
    ThinkingIndicator.extended {
        color: $accent;
    }
    """

    label: reactive[str] = reactive("Thinking…")
    extended: reactive[bool] = reactive(False)

    def __init__(
        self,
        label: str = "Thinking…",
        interval_ms: int = 200,
        extended: bool = False,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.label = label
        self.interval_ms = interval_ms
        self.extended = extended
        self._frame: int = 0
        self._timer: Timer | None = None
        self._start_time: float = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Static(self._render_text(), id="thinking-text")

    def on_mount(self) -> None:
        """Start the animation timer."""
        self._timer = self.set_interval(
            self.interval_ms / 1000.0,
            self._tick,
        )

    def on_unmount(self) -> None:
        """Stop the animation timer to avoid orphaned callbacks."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    async def _tick(self) -> None:
        """Advance the spinner frame and refresh the static text."""
        self._frame = (self._frame + 1) % len(_SPINNER_FRAMES)
        text_widget = self.query_one("#thinking-text", Static)
        text_widget.update(self._render_text())

    def _render_text(self) -> str:
        """Produce the current spinner + label string."""
        frames = _SPINNER_FRAMES
        spinner = frames[self._frame % len(frames)]
        elapsed = time.monotonic() - self._start_time
        elapsed_str = _format_elapsed_short(elapsed)
        return f"{spinner} {self.label} ({elapsed_str})"

    def watch_extended(self, value: bool) -> None:
        """React to extended mode changes."""
        if value:
            self.add_class("extended")
            self.label = "Thinking (extended)…"
        else:
            self.remove_class("extended")
            self.label = "Thinking…"

    def elapsed_seconds(self) -> float:
        """Return seconds since this indicator was mounted."""
        return time.monotonic() - self._start_time


def _format_elapsed_short(seconds: float) -> str:
    """Format elapsed seconds as e.g. '5s', '2m14s', '1h02m'."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds) // 60
    remaining_s = int(seconds) % 60
    if minutes < 60:
        return f"{minutes}m{remaining_s:02d}s"
    hours = minutes // 60
    remaining_m = minutes % 60
    return f"{hours}h{remaining_m:02d}m"
```

**Full method contract list:**

| Method | Signature | Contract |
|---|---|---|
| `__init__` | `(label, interval_ms, extended, *, id, classes)` | Stores params; does NOT start timer |
| `compose` | `() -> ComposeResult` | Yields one `Static("#thinking-text")` |
| `on_mount` | `() -> None` | Starts `set_interval` timer; records `_start_time` |
| `on_unmount` | `() -> None` | Calls `_timer.stop()`; sets `_timer = None` |
| `_tick` | `async () -> None` | Increments `_frame`; calls `text_widget.update()` |
| `_render_text` | `() -> str` | Returns `"{spinner} {label} ({elapsed})"` |
| `watch_extended` | `(value: bool) -> None` | Adds/removes CSS class; updates `label` |
| `elapsed_seconds` | `() -> float` | `time.monotonic() - _start_time` |

**Animation mechanism (background timer, not blocking):**

The animation uses `Widget.set_interval()` which is a Textual-managed asyncio
coroutine that fires the callback on each tick without blocking the event loop.
The timer is created in `on_mount()` and explicitly stopped in `on_unmount()`.
This ensures no orphaned timers when the widget is removed from the DOM.

**Critical:** The `_tick` method is `async def`. Textual's `set_interval`
accepts both sync and async callbacks; async is preferred here to allow
`await` if needed in future extensions.

### 3.2 Progress Indicators

#### 3.2.1 Long-running tool progress

When a tool supports progress reporting (e.g. `run_bash` with streaming output),
the `ToolCallBlock` renders a `ProgressIndicator` widget. The specification for
this widget is in component-inventory.md §3.13. This PRD extends that
specification with the following signal integration:

- A `streaming_tool_progress` event payload field `{"progress": 0.0–1.0,
  "label": str}` drives the `ProgressBar` value and label.
- If no progress payload arrives within 5 seconds of `tool_call_started`, the
  indicator automatically switches to `INDETERMINATE` mode.

#### 3.2.2 Indeterminate progress (spinner)

Use the same `_SPINNER_FRAMES` as `ThinkingIndicator` but at 125ms interval
(8 fps, per component-inventory.md §6.5 `SPINNER_INTERVAL_MS = 125`).

Rendered as `{spinner}  running…` beside the tool name in the `ToolCallBlock`
header.

#### 3.2.3 Determinate progress (bar)

Use `textual.widgets.ProgressBar` with `total=1.0`. Update via
`progress_bar.advance(delta)` or direct assignment `progress_bar.progress =
value`.

Bar character encoding (for terminals without `ProgressBar` widget rendering):

```python
BAR_FULL = "█"
BAR_EMPTY = "░"
BAR_WIDTH = 20  # characters

def render_progress_bar(value: float, width: int = BAR_WIDTH) -> str:
    """Render a filled/empty bar string for value in [0.0, 1.0]."""
    filled = int(value * width)
    empty = width - filled
    return f"[{BAR_FULL * filled}{BAR_EMPTY * empty}]  {int(value * 100)} %"
```

#### 3.2.4 Time estimates

When `status == RUNNING_TOOL` and `tool_start_time` is set, compute an ETA
only if there is a `progress` value available:

```python
def estimate_eta_seconds(
    elapsed: float,
    progress: float,
) -> float | None:
    """Return estimated remaining seconds, or None if not computable.

    Returns None if progress <= 0.0 (avoid division by zero).
    Returns None if elapsed < 1.0 (too early for reliable estimate).
    """
    if progress <= 0.0 or elapsed < 1.0:
        return None
    total_estimated = elapsed / progress
    return max(0.0, total_estimated - elapsed)
```

Display format: `ETA ~Xs` or `ETA ~Xm`. Omitted when `estimate_eta_seconds`
returns `None`.

---

## 4. Multi-Agent Visibility

### 4.1 Agent Color Assignment

**File:** `src/agenthicc/tui/event_adapter.py`

When the `TUIEventAdapter` processes an `agent_spawn` event, it assigns a color
index to the new agent's `AgentStatusState` using a round-robin counter:

```python
_AGENT_COLORS: tuple[str, ...] = (
    "magenta",  # index 0
    "cyan",     # index 1
    "yellow",   # index 2
    "blue",     # index 3
    "green",    # index 4
    "red",      # index 5
)

_AGENT_ANSI_CODES: tuple[str, ...] = (
    "\033[35m",  # magenta
    "\033[36m",  # cyan
    "\033[33m",  # yellow
    "\033[34m",  # blue
    "\033[32m",  # green
    "\033[31m",  # red
)
```

The counter is stored in `TUIEventAdapter._next_color_index: int = 0` and
increments modulo 6 on each spawn.

The main agent (first spawn, `parent_agent_id == None`) always gets index 0
(magenta). Sub-agents get subsequent indices.

### 4.2 Parallel Agent Status Display

When `len(active_agents) > 1` (multiple `AgentStatusState` instances in
`TUIEventAdapter._agents` with status not `IDLE` or `COMPLETE`), the status bar
displays a summary:

```
# 3 agents active — use /agents to see details
 ● 3 agents  ↑6.2k ↓1.2k  ($0.0089)  [AUTO]  [a3f8]
```

When `len(active_agents) == 1`, the single-agent format from Section 2.1/2.2
is used.

**`render_multi_agent_line` function:**

```python
def render_multi_agent_line(
    agents: list[AgentStatusState],
    session_id: str,
    mode_label: str,
    cols: int,
    no_color: bool = False,
) -> str:
    """Render a summary status bar for multiple active agents.

    Shows: agent count, combined token totals, combined cost, mode badge,
    session ID. Color is applied to the count indicator using the first
    active agent's color_index.

    Parameters
    ----------
    agents:
        All ``AgentStatusState`` instances that are not IDLE or COMPLETE.
    session_id:
        First 4 hex characters of the kernel session ID.
    mode_label:
        Current permission mode label (e.g. "AUTO").
    cols:
        Terminal width for truncation.
    no_color:
        If True, suppress all ANSI codes.
    """
```

### 4.3 Color Differentiation

The agent color (from `AgentStatusState.color_index`) is used in:

1. The agent turn header line committed to the transcript:
   `"\033[{code}m● agent:{name}\033[0m  {timestamp}"` where `code` is the ANSI
   color for the agent's index.
2. The `ThinkingIndicator` CSS class: `add_class(f"agent-color-{color_index}")`
   allowing per-agent CSS styling.
3. The multi-agent status bar summary.

NO_COLOR mode: colors are suppressed; agents are differentiated by name only.

---

## 5. Signal/Event Integration

### 5.1 Kernel Signal Bus

The `TUIEventAdapter` subscribes to the `EventProcessor` via
`processor.subscribe()` which returns an `asyncio.Queue[AppState]`. On each
new `AppState`, the adapter diffs it against the previous state and fires
the appropriate `AgentStatusState` mutations.

**File:** `src/agenthicc/tui/event_adapter.py`

```python
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agenthicc.kernel.processor import EventProcessor
from agenthicc.kernel.state import AppState, AgentStatus as KernelAgentStatus
from agenthicc.tui.agent_status import AgentStatus, AgentStatusState

if TYPE_CHECKING:
    from agenthicc.kernel.events import Event

__all__ = ["TUIEventAdapter"]

logger = logging.getLogger(__name__)

_NEXT_COLOR_INDEX = 0
_AGENT_COLORS = ("magenta", "cyan", "yellow", "blue", "green", "red")


class TUIEventAdapter:
    """Subscribes to EventProcessor; translates AppState diffs to TUI mutations.

    Usage
    -----
    adapter = TUIEventAdapter(processor)
    asyncio.create_task(adapter.run())
    # Now adapter.agents is live; render loop reads it.

    Thread safety
    -------------
    All mutations happen on the asyncio event loop (single-threaded). No locks.
    """

    def __init__(self, processor: EventProcessor) -> None:
        self._processor = processor
        self._queue: asyncio.Queue[AppState] = processor.subscribe()
        self._previous_state: AppState | None = None
        self._agents: dict[str, AgentStatusState] = {}
        self._next_color_index: int = 0
        self._running: bool = False

    @property
    def agents(self) -> dict[str, AgentStatusState]:
        """Live map of agent_id → AgentStatusState. Read-only from outside."""
        return self._agents

    def primary_agent(self) -> AgentStatusState | None:
        """Return the main (first-spawned) agent, or None if none exist."""
        for state in self._agents.values():
            if state.color_index == 0:
                return state
        return next(iter(self._agents.values()), None)

    async def run(self) -> None:
        """Main loop. Runs until stop() is called."""
        self._running = True
        while self._running:
            try:
                new_state = await asyncio.wait_for(
                    self._queue.get(), timeout=0.1
                )
                self._process_state_change(new_state)
                self._previous_state = new_state
            except TimeoutError:
                continue
            except Exception:
                logger.exception("TUIEventAdapter error")

    async def stop(self) -> None:
        self._running = False
        self._processor.unsubscribe(self._queue)

    def _process_state_change(self, new_state: AppState) -> None:
        """Diff new_state against previous_state; fire status mutations."""
        prev = self._previous_state

        # New agents
        for agent_id, agent in new_state.agents.items():
            if prev is None or agent_id not in prev.agents:
                self._on_agent_spawn(agent_id, agent, new_state)

        # Agent status changes
        for agent_id, agent in new_state.agents.items():
            if prev is not None and agent_id in prev.agents:
                prev_agent = prev.agents[agent_id]
                if prev_agent.status != agent.status:
                    self._on_agent_status_change(agent_id, agent, new_state)

        # Terminated agents
        if prev is not None:
            for agent_id in prev.agents:
                if agent_id not in new_state.agents:
                    self._on_agent_terminate(agent_id)
```

### 5.2 Full Event-to-Transition Mapping Table

The following table maps every kernel event type (from `kernel/events.py` and
the reducer) to the specific `AgentStatusState` mutation it triggers. The
`TUIEventAdapter._process_state_change` method implements these by diffing
`AppState` before and after each event.

| Kernel Event / AppState Diff | AgentStatusState Mutation | Notes |
|---|---|---|
| `intent_submitted` (new Intent in `intents` dict) | `reset_for_new_turn()` → `status = THINKING` | Applied to primary agent |
| Agent `status` changes from `idle` → `busy` | `status = THINKING` (if not already set by intent) | Kernel `AgentStatus.busy` maps to TUI `THINKING` |
| `streaming_token` payload (`tokens: int, text: str`) | Append `text` to `partial_text`; `update_streaming_rate(tokens)`; if status == THINKING: → STREAMING | First token triggers THINKING→STREAMING |
| `tool_call_started` (new tool in payload) | `status = RUNNING_TOOL`; set `current_tool_name`; set `tool_start_time` | |
| `tool_call_complete` (tool result in payload, agent continues) | If next event is streaming: → STREAMING; else → THINKING | Detected by subsequent `streaming_token` within 200ms |
| `tool_call_complete` (last tool of turn) | → COMPLETE via `agent_run_complete` below | |
| `approval_required` effect | `status = WAITING_APPROVAL` | |
| `approval_granted` or `approval_denied` | → THINKING (agent resumes) | |
| `agent_run_complete` (no error) | `status = COMPLETE`; `turn_count += 1`; `partial_text = ""` | |
| `agent_run_error` (error payload) | `status = ERROR`; `error_message = payload["error"]` | |
| `model_call_complete` (token usage in payload) | `input_tokens += payload["input_tokens"]`; `output_tokens += payload["output_tokens"]`; `cost_usd += payload["cost_usd"]` | |
| Agent `status` changes from `busy` → `idle` | If current TUI status is COMPLETE: schedule COMPLETE→IDLE after 3s | |
| Agent `status` changes to `terminated` | Remove from `_agents` dict | |
| `agent_spawn` (new AgentInstance) | Create new `AgentStatusState`; assign color | |

**`model_call_complete` event** — this event type must be emitted by the agent
runner in the same turn as `agent_run_complete`. Its payload schema:

```python
{
    "agent_id": str,
    "input_tokens": int,
    "output_tokens": int,
    "cost_usd": float,
    "model_id": str,
}
```

**`streaming_token` event** — emitted by the agent runner for every token or
chunk. Its payload schema:

```python
{
    "agent_id": str,
    "text": str,      # the text chunk (may be >1 token)
    "tokens": int,    # token count for rate calculation
}
```

Both `model_call_complete` and `streaming_token` are **new event types** that
must be added to the reducer in `kernel/reducer.py` as pass-through handlers
(they update no AppState fields directly; the TUIEventAdapter observes them via
the subscriber queue). Add them to `kernel/events.py` as recognized event_type
strings.

---

## 6. `StatusState` Integration

### 6.1 Existing `StatusState`

The existing code in `src/agenthicc/__main__.py` and `src/agenthicc/config.py`
does not contain a `StatusState` class at this time (the TUI was previously
`prompt_toolkit`-based and has not yet been ported). This section specifies
the new `StatusState` that the Textual bottom block will use.

### 6.2 New `StatusState` Dataclass

**File:** `src/agenthicc/tui/agent_status.py` (add to existing file)

```python
@dataclass
class StatusState:
    """Presentation state for the bottom block status bar.

    This is the single mutable object that the ``FrameComposer`` (or the
    Textual ``AgentStatusBar`` widget) reads to render the bottom block.
    It aggregates data from ``AgentStatusState`` and from the kernel
    ``AppState``.

    Updated exclusively by ``TUIEventAdapter`` on the asyncio event loop.

    Fields
    ------
    agent_status : AgentStatus
        Current status of the primary agent (or IDLE if no agents).
    primary_agent : AgentStatusState | None
        Reference to the primary agent's state object.
    active_agent_count : int
        Number of agents with status not IDLE or COMPLETE.
    mode_label : str
        Current permission mode label ("AUTO", "PLAN", etc.).
    session_id : str
        First 4 hex characters of ``AppState.session_id``.
    model_id : str
        Active model label.
    partial_text : str
        Current streaming text (empty when not streaming).
    streaming_rate_tps : float
        Tokens per second of the primary agent (0.0 when not streaming).
    input_tokens : int
        Cumulative session input tokens.
    output_tokens : int
        Cumulative session output tokens.
    cost_usd : float
        Cumulative session cost.
    turn_count : int
        Completed turns.
    error_message : str | None
        Current error, or None.
    current_tool_name : str | None
        Tool currently executing, or None.
    """

    agent_status: AgentStatus = AgentStatus.IDLE
    primary_agent: AgentStatusState | None = None
    active_agent_count: int = 0
    mode_label: str = "AUTO"
    session_id: str = ""
    model_id: str = ""
    partial_text: str = ""
    streaming_rate_tps: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turn_count: int = 0
    error_message: str | None = None
    current_tool_name: str | None = None
```

### 6.3 Backward Compatibility

The existing `AgentStatus` in `kernel/state.py` (3-value kernel enum: `idle`,
`busy`, `terminated`) is **not modified**. Any file importing
`agenthicc.kernel.state.AgentStatus` continues to work. When both are needed:

```python
from agenthicc.kernel.state import AgentStatus as KernelAgentStatus
from agenthicc.tui.agent_status import AgentStatus as TUIAgentStatus
```

The `__all__` in `kernel/state.py` already exports `AgentStatus`; the new
`src/agenthicc/tui/agent_status.py` also exports `AgentStatus`. These are
two distinct classes in two distinct modules.

---

## 7. Full Test Specification

### 7.1 Unit Tests

**File:** `tests/unit/test_agent_status.py`

Test markers: `@pytest.mark.unit`

#### Group A: `AgentStatus` enum (5 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U01 | `test_agent_status_values_are_strings` | `list(AgentStatus)` | All `.value` are `str` | None |
| U02 | `test_agent_status_idle_value` | `AgentStatus.IDLE` | `.value == "idle"` | None |
| U03 | `test_agent_status_thinking_value` | `AgentStatus.THINKING` | `.value == "thinking"` | None |
| U04 | `test_agent_status_complete_value` | `AgentStatus.COMPLETE` | `.value == "complete"` | None |
| U05 | `test_agent_status_all_seven_values` | `AgentStatus` | `len(list(AgentStatus)) == 7` | Adding new value must fail this test |

#### Group B: `AgentStatusState` construction (5 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U06 | `test_state_default_status_is_idle` | `AgentStatusState("agent-1")` | `state.status == AgentStatus.IDLE` | None |
| U07 | `test_state_default_tokens_are_zero` | `AgentStatusState("agent-1")` | `input_tokens == 0, output_tokens == 0` | None |
| U08 | `test_state_default_partial_text_empty` | `AgentStatusState("agent-1")` | `partial_text == ""` | None |
| U09 | `test_state_turn_start_time_none_when_idle` | `AgentStatusState("agent-1")` | `turn_start_time is None` | None |
| U10 | `test_state_color_index_default_zero` | `AgentStatusState("agent-1")` | `color_index == 0` | None |

#### Group C: `elapsed_seconds` and `tool_elapsed_seconds` (4 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U11 | `test_elapsed_zero_when_no_start` | `AgentStatusState("a")`, no `reset_for_new_turn()` | `elapsed_seconds() == 0.0` | None |
| U12 | `test_elapsed_positive_after_reset` | `reset_for_new_turn()` then immediate check | `0.0 <= elapsed_seconds() < 0.1` | Timing sensitive — use `pytest-mock` `time.monotonic` |
| U13 | `test_tool_elapsed_zero_when_no_tool` | No tool fields set | `tool_elapsed_seconds() == 0.0` | None |
| U14 | `test_tool_elapsed_positive_when_running` | Set `tool_start_time = time.monotonic()` | `0.0 <= tool_elapsed_seconds() < 0.1` | None |

#### Group D: `update_streaming_rate` rolling window (6 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U15 | `test_rate_zero_with_single_sample` | Call once with `new_tokens=5` | `streaming_rate_tps == 0.0` (single sample: no interval) | None |
| U16 | `test_rate_positive_with_two_samples` | Two calls with `new_tokens=10` each, 1s apart | `~10.0 tps` | Mock `time.monotonic` |
| U17 | `test_rate_evicts_old_samples` | Five calls spaced 0.5s apart; check after 5th | Only last 4 samples (≤2s window) contribute | Mock time |
| U18 | `test_rate_zero_after_clear` | Set up rate, call `reset_for_new_turn()` | `streaming_rate_tps == 0.0` | None |
| U19 | `test_rate_window_empty_at_start` | Fresh state | `_token_window == []` | None |
| U20 | `test_rate_handles_zero_tokens` | Call with `new_tokens=0` | Does not raise; rate remains 0.0 | None |

#### Group E: `reset_for_new_turn` (4 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U21 | `test_reset_clears_partial_text` | Set `partial_text = "hello"`, then reset | `partial_text == ""` | None |
| U22 | `test_reset_clears_tool_fields` | Set `current_tool_name = "read_file"`, then reset | `current_tool_name is None` | None |
| U23 | `test_reset_sets_turn_start_time` | Fresh state, call reset | `turn_start_time is not None` | None |
| U24 | `test_reset_clears_token_window` | Add items to `_token_window`, then reset | `_token_window == []` | None |

---

**File:** `tests/unit/test_status_bar.py`

Test markers: `@pytest.mark.unit`

#### Group F: `_thinking_wave` (4 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U25 | `test_wave_braille_frame_0` | `_thinking_wave(0)` | `"⠋"` | None |
| U26 | `test_wave_braille_cycles` | `_thinking_wave(10)` | `"⠋"` (same as frame 0) | `10 % 10 == 0` |
| U27 | `test_wave_ascii_mode` | `_thinking_wave(0, ascii_mode=True)` | `"|"` | None |
| U28 | `test_wave_ascii_cycles` | `_thinking_wave(4, ascii_mode=True)` | `"|"` | `4 % 4 == 0` |

#### Group G: `_format_tokens` (5 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U29 | `test_format_tokens_small` | `(100, 50)` | Contains `"100"` and `"50"` | None |
| U30 | `test_format_tokens_k_suffix` | `(11000, 5000)` | Contains `"11.0k"` and `"5.0k"` | Boundary at 10000 |
| U31 | `test_format_tokens_m_suffix` | `(1_500_000, 200_000)` | Contains `"1.5M"` and `"200.0k"` | None |
| U32 | `test_format_tokens_zero` | `(0, 0)` | `"↑0 ↓0"` substring present | None |
| U33 | `test_format_tokens_cost_in_output` | `(1000, 500)` with cost=0.0031 | `"$0.0031"` substring | None |

#### Group H: `_format_elapsed` (5 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U34 | `test_elapsed_under_60` | `5.7` | `"5s"` | Floor not round |
| U35 | `test_elapsed_exactly_60` | `60.0` | `"1m00s"` | Boundary |
| U36 | `test_elapsed_over_60` | `134.0` | `"2m14s"` | None |
| U37 | `test_elapsed_one_hour` | `3600.0` | `"1h00m"` | None |
| U38 | `test_elapsed_zero` | `0.0` | `"0s"` | None |

#### Group I: `render_streaming_zone` (6 tests)

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U39 | `test_zone_empty_for_empty_text` | `partial_text=""` | `[]` | None |
| U40 | `test_zone_takes_last_8_lines` | 10 lines of text | 8 items returned | Exactly 8 |
| U41 | `test_zone_truncates_long_lines` | Line of 200 chars, cols=80 | Each item ≤ 78 chars (80-2) | `wcwidth` |
| U42 | `test_zone_dim_style_applied` | `no_color=False` | Each item starts with `"\033[2m"` | None |
| U43 | `test_zone_no_color_mode` | `no_color=True` | No `"\033["` in any item | None |
| U44 | `test_zone_max_lines_param` | `max_lines=4` | At most 4 items | None |

#### Group J: `ThinkingIndicator` widget (11 tests)

**File:** `tests/unit/test_thinking_indicator.py`

| # | Test name | Inputs | Expected | Edge cases |
|---|---|---|---|---|
| U45 | `test_default_label` | `ThinkingIndicator()` | `indicator.label == "Thinking…"` | None |
| U46 | `test_custom_label` | `ThinkingIndicator(label="Working…")` | `indicator.label == "Working…"` | None |
| U47 | `test_default_extended_false` | `ThinkingIndicator()` | `indicator.extended == False` | None |
| U48 | `test_frame_starts_at_zero` | Fresh instance | `indicator._frame == 0` | None |
| U49 | `test_timer_none_before_mount` | Fresh instance | `indicator._timer is None` | None |
| U50 | `test_render_text_contains_spinner` | `_render_text()` on frame 0 | Contains `"◐"` | None |
| U51 | `test_render_text_contains_label` | `_render_text()` | Contains `"Thinking…"` | None |
| U52 | `test_watch_extended_adds_class` | `indicator.extended = True` | `indicator.has_class("extended")` | Requires Textual App pilot |
| U53 | `test_watch_extended_updates_label` | `indicator.extended = True` | `indicator.label == "Thinking (extended)…"` | None |
| U54 | `test_elapsed_seconds_positive_after_mount` | After `on_mount()`, immediate check | `0.0 <= elapsed_seconds() < 0.5` | None |
| U55 | `test_format_elapsed_short_all_ranges` | `(0.0, 59.0, 134.0, 3600.0)` | `("0s", "59s", "2m14s", "1h00m")` | None |

---

### 7.2 Integration Tests

**File:** `tests/integration/test_agent_activity_integration.py`

Test markers: `@pytest.mark.integration`

These tests use a real `EventProcessor` with a running `run()` task (via the
`running_processor` fixture from `tests/conftest.py`).

| # | Scenario | Signal Flow | Expected UI Result |
|---|---|---|---|
| I01 | Agent spawns and status becomes THINKING | Emit `agent_spawn` + `intent_submitted` events | `TUIEventAdapter._agents` has one entry with `status == THINKING` |
| I02 | First streaming token transitions to STREAMING | Emit `streaming_token` with `text="hello"` | `status == STREAMING`, `partial_text == "hello"` |
| I03 | Multiple tokens accumulate in partial_text | Emit 5 `streaming_token` events | `partial_text` contains all concatenated text |
| I04 | Tool call transitions to RUNNING_TOOL | Emit `tool_call_started` | `status == RUNNING_TOOL`, `current_tool_name` set |
| I05 | Tool completes and agent resumes thinking | Emit `tool_call_complete`, then `streaming_token` | `status == STREAMING` after second event |
| I06 | Agent run completes | Emit `agent_run_complete` | `status == COMPLETE`, `turn_count == 1` |
| I07 | Token stats updated on model_call_complete | Emit `model_call_complete` with token payload | `input_tokens` and `output_tokens` updated |
| I08 | Second turn resets partial_text | Two full turn cycles | `partial_text == ""` at start of second turn |
| I09 | Error event sets ERROR status | Emit `agent_run_error` | `status == ERROR`, `error_message` non-empty |
| I10 | Multi-agent parallel spawn | Spawn 3 agents, each gets distinct `color_index` | `color_index` values are 0, 1, 2 (no duplicates) |
| I11 | Streaming rate computed over 2-second window | Emit 10 tokens spaced 100ms apart | `streaming_rate_tps` between 5.0 and 15.0 |
| I12 | Agent termination removes from dict | Emit agent terminated event (kernel status→terminated) | `agent_id` removed from `_agents` |
| I13 | StatusState aggregates primary agent data | 1 active agent in STREAMING | `StatusState.agent_status == STREAMING`, `partial_text` matches |
| I14 | Multi-agent count in StatusState | 3 active agents | `StatusState.active_agent_count == 3` |
| I15 | Waiting approval transition | Emit `approval_required` | `status == WAITING_APPROVAL` |

---

### 7.3 E2E Tests

**File:** `tests/e2e/test_agent_activity_e2e.py`

Test markers: `@pytest.mark.e2e`

These tests use `pyte` to run a real terminal emulator and assert on the rendered
screen buffer. They also use `FakeTerminal` for faster assertion.

**pyte terminal dimensions:** `ROWS = 24`, `COLS = 80` (standard test size).

| # | Scenario | Observable Terminal Output | Timing Criteria |
|---|---|---|---|
| E01 | IDLE status bar visible on startup | `screen.buffer[ROWS-4]` contains `"○ Idle"` | Under 200ms from process start |
| E02 | THINKING indicator appears on turn start | Screen contains spinner char (`"⠋"` or `"⠙"`) | Within 50ms of `intent_submitted` event |
| E03 | Streaming text appears in streaming zone | Lines above status bar contain first 40 chars of streamed text | Within 100ms of first `streaming_token` |
| E04 | Tool name appears during RUNNING_TOOL | Status bar line contains tool name string | Within 50ms of `tool_call_started` |
| E05 | COMPLETE status shows turn count | Status bar contains `"Turn 1"` after `agent_run_complete` | Immediately after event |
| E06 | ThinkingIndicator spinner advances | Two pyte frames 200ms apart show different braille chars | Spinner frame advances in ≤250ms |
| E07 | Multi-agent shows agent count | Status bar contains `"2 agents"` when 2 agents active | Within 100ms of second spawn |
| E08 | Streaming zone cleared on turn complete | Lines above status bar are empty (no partial text) | Within 50ms of `agent_run_complete` |
| E09 | ERROR state shows error message | Status bar contains `"Error:"` substring | Within 50ms of `agent_run_error` |
| E10 | Token counts update in status bar | `"↑"` or token count changes between two frames | Within 200ms of `model_call_complete` |

---

## 8. Acceptance Criteria

All criteria are binary (pass/fail). No partial credit.

### 8.1 Animation Smoothness

| Criterion | Measurement | Pass condition |
|---|---|---|
| AC01 | `ThinkingIndicator` frame rate | Time between consecutive `_tick()` calls | 190ms ≤ interval ≤ 210ms at 200ms setting |
| AC02 | Spinner does not block event loop | Measure asyncio event loop latency during animation | Loop latency < 5ms during animation tick |
| AC03 | Timer stops on unmount | After `on_unmount()`, no further `_tick()` calls | Zero calls within 500ms of unmount |
| AC04 | Animation frame 0 on fresh mount | First rendered character is `"◐"` | Exact match |

### 8.2 Status Bar Latency

| Criterion | Measurement | Pass condition |
|---|---|---|
| AC05 | THINKING status appears after `intent_submitted` | `time.monotonic()` before emit vs. first THINKING render | < 50ms |
| AC06 | STREAMING status after first token | `streaming_token` emit to STREAMING in status bar | < 50ms |
| AC07 | RUNNING_TOOL after `tool_call_started` | Emit to tool name visible in status bar | < 50ms |
| AC08 | COMPLETE appears after `agent_run_complete` | Emit to COMPLETE in status bar | < 50ms |

### 8.3 Rendering Correctness

| Criterion | Measurement | Pass condition |
|---|---|---|
| AC09 | Idle format string has all fields | Test with mock `StatusState` | All of: status icon, mode badge, model, turn count, tokens, session ID |
| AC10 | Streaming zone max 8 lines | `partial_text` with 20 lines | `len(render_streaming_zone(...)) <= 8` |
| AC11 | No ANSI in NO_COLOR mode | `no_color=True` renders | Zero `"\033["` sequences in any output |
| AC12 | `wcwidth` used for truncation | Lines with wide characters (e.g. `"日本語"`) | No truncation artifacts; visual width correct |
| AC13 | Token abbreviation correct | `input_tokens=11000` | Renders as `"11.0k"` not `"11000"` |
| AC14 | Elapsed time format correct | `elapsed_seconds=134` | Renders as `"2m14s"` |

### 8.4 Integration Correctness

| Criterion | Measurement | Pass condition |
|---|---|---|
| AC15 | All 15 integration tests pass | `pytest tests/integration/test_agent_activity_integration.py` | 15/15 pass |
| AC16 | All 10 E2E tests pass | `pytest tests/e2e/test_agent_activity_e2e.py` | 10/10 pass |
| AC17 | mypy clean on all new files | `mypy src/agenthicc/tui/agent_status.py src/agenthicc/tui/status_bar.py src/agenthicc/tui/thinking_indicator.py src/agenthicc/tui/event_adapter.py` | Zero errors |
| AC18 | ruff clean on all new files | `ruff check` on new files | Zero violations |
| AC19 | `AgentStatus` in kernel unchanged | Import `from agenthicc.kernel.state import AgentStatus` | Still has 3 values: idle, busy, terminated |
| AC20 | No circular imports | `python -c "import agenthicc.tui.event_adapter"` | No `ImportError` |

### 8.5 Memory and CPU

| Criterion | Measurement | Pass condition |
|---|---|---|
| AC21 | Token window bounded | After 1000 `update_streaming_rate()` calls at 200ms spacing | `len(_token_window) <= 15` (only last 2s of 200ms samples = 10 max) |
| AC22 | Streaming rate CPU | 1000 `update_streaming_rate()` calls | Completes in < 10ms total |
| AC23 | ThinkingIndicator mount/unmount cycle | 100 mount/unmount cycles | Zero leaked `Timer` objects (check via `gc.get_objects()`) |

---

## 9. File Creation Checklist

The agent implementing this PRD must create the following files. No other files
may be created. Existing files that require modification are listed separately.

### 9.1 New files to create

```
src/agenthicc/tui/
    __init__.py              (empty, or re-exports)
    agent_status.py          (AgentStatus, AgentStatusState, StatusState)
    status_bar.py            (render_status_line, _thinking_wave, _format_tokens,
                              _format_elapsed, render_streaming_zone,
                              render_multi_agent_line)
    thinking_indicator.py    (ThinkingIndicator, _format_elapsed_short)
    event_adapter.py         (TUIEventAdapter)

tests/unit/
    test_agent_status.py     (U01–U24, groups A–E)
    test_status_bar.py       (U25–U44, groups F–J, partial)
    test_thinking_indicator.py (U45–U55, group J continued)

tests/integration/
    test_agent_activity_integration.py  (I01–I15)

tests/e2e/
    test_agent_activity_e2e.py          (E01–E10)
```

### 9.2 Existing files to modify

```
src/agenthicc/kernel/events.py
    - Add "streaming_token" to recognized event_type strings (comment only;
      no code change needed since event_type is free-form str)
    - Add "model_call_complete", "agent_run_complete", "agent_run_error",
      "tool_call_started", "tool_call_complete", "approval_required" as
      recognized event_type constants (optional: add a MODULE-level dict or
      frozenset of recognized types for validation in tests)

src/agenthicc/kernel/reducer.py
    - Add pass-through handlers for "streaming_token" and "model_call_complete"
      that return (state, []) — no AppState mutation, but the event is still
      dispatched to subscribers.
```

### 9.3 What NOT to create

- Do NOT create `src/agenthicc/tui/app.py` — that is specified in a separate PRD.
- Do NOT create `src/agenthicc/tui/transcript.py` — that is specified in a
  separate PRD.
- Do NOT modify `src/agenthicc/kernel/state.py` — the existing kernel
  `AgentStatus` enum stays unchanged.
- Do NOT create any `.md` documentation files.
- Do NOT create `app.tcss` — CSS is specified in a separate PRD.

---

## 10. Dependencies

### 10.1 Python package dependencies

All must already be in `pyproject.toml` or added:

| Package | Version constraint | Purpose |
|---|---|---|
| `textual` | `>=0.56.0` | `MarkdownStream`, `set_interval`, `reactive` |
| `wcwidth` | `>=0.2.13` | Display-width calculation for terminal strings |
| `pyte` | `>=0.8.0` | E2E terminal emulator tests |
| `pytest-mock` | `>=3.12.0` | Mocking `time.monotonic` in unit tests |

### 10.2 Internal dependencies

| Module | Imported by | What is imported |
|---|---|---|
| `agenthicc.kernel.processor` | `event_adapter.py` | `EventProcessor` |
| `agenthicc.kernel.state` | `event_adapter.py` | `AppState`, `AgentStatus as KernelAgentStatus` |
| `agenthicc.tui.agent_status` | `event_adapter.py`, `status_bar.py` | `AgentStatus`, `AgentStatusState`, `StatusState` |
| `agenthicc.tui.status_bar` | (called by app layer) | `render_status_line`, `render_streaming_zone` |
| `agenthicc.tui.thinking_indicator` | (mounted by app layer) | `ThinkingIndicator` |

---

## 11. Worked Examples

### 11.1 Single agent thinking for 8 seconds

```python
# State after 8 seconds of thinking
state = AgentStatusState(agent_id="main-agent", color_index=0)
state.status = AgentStatus.THINKING
state.turn_start_time = time.monotonic() - 8.0
state.model_id = "claude-sonnet-4-6"
state.input_tokens = 1200
state.output_tokens = 0

# render_status_line output (no_color=False, cols=80, frame=3):
# " ● Thinking  ⠸  8s  ↑1.2k ↓0  [claude-sonnet-4-6]"
```

### 11.2 Streaming at 15 tok/s

```python
state.status = AgentStatus.STREAMING
state.streaming_rate_tps = 15.2
state.output_tokens = 120
state.partial_text = "The authentication bug is caused by...\nThe fix is to..."

# render_status_line output (frame=7):
# " ~ Streaming  ⠇  3s  ↑1.2k ↓120  15.2 tok/s  [claude-sonnet-4-6]"

# render_streaming_zone output (cols=80, max_lines=4):
# ["\033[2mThe authentication bug is caused by...\033[0m",
#  "\033[2mThe fix is to...\033[0m"]
```

### 11.3 Running tool for 1.5 seconds

```python
state.status = AgentStatus.RUNNING_TOOL
state.current_tool_name = "read_file"
state.tool_start_time = time.monotonic() - 1.5

# render_status_line output (frame=5):
# " ▶ read_file  ⠴  1.5s  ↑1.2k ↓120  [claude-sonnet-4-6]"
```

### 11.4 Two agents active

```python
agents = [
    AgentStatusState("main", status=AgentStatus.STREAMING, color_index=0,
                     input_tokens=3000, output_tokens=800, cost_usd=0.0041),
    AgentStatusState("sub-1", status=AgentStatus.RUNNING_TOOL, color_index=1,
                     input_tokens=2100, output_tokens=400, cost_usd=0.0031),
]

# render_multi_agent_line output (cols=80, mode_label="AUTO"):
# " ● 2 agents  ↑5.1k ↓1.2k  ($0.0072)  [AUTO]  [a3f8]"
```

---

## 12. Common Pitfalls for Implementors

| Symptom | Cause | Fix |
|---|---|---|
| `ImportError: cannot import name 'AgentStatus' from 'agenthicc.tui.agent_status'` | Forgot `from __future__ import annotations` or `__all__` omits it | Add to `__all__` in `agent_status.py` |
| `ThinkingIndicator._timer` is not None after unmount in tests | Textual App not properly stopped before asserting | Call `app.stop()` in test teardown |
| `streaming_rate_tps` is 0.0 after calling `update_streaming_rate` | Only one sample in window (single sample cannot compute a rate) | Expected: need 2+ calls; test accordingly |
| mypy error: `"AgentStatus" is not defined` | Circular import via `event_adapter.py` | Use `TYPE_CHECKING` guard for Textual widget imports |
| `wcwidth.wcswidth` returns -1 for string with combining characters | Some Unicode combining chars are width -1 | Guard: `max(0, wcswidth(s))` |
| Timer fires after test ends, causing `RuntimeError: no running event loop` | Textual widget timer not stopped in test cleanup | Use `async with app.run_test() as pilot:` context manager |
| Status transitions silently no-op for unknown event types | `_process_state_change` doesn't handle all event types | Add debug logging for unrecognized event payloads |
| `partial_text` grows unboundedly in long streaming sessions | No max-size guard | Cap at 100KB: `if len(self.partial_text) > 100_000: self.partial_text = self.partial_text[-50_000:]` |
