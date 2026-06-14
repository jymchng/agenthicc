# Master Execution PRD — AgentHICC TUI Redesign

**Document type**: Implementation master plan  
**Status**: Authoritative  
**Date**: 2026-06-13  
**Supersedes**: All prior TUI implementation notes

---

## 1. Executive Summary

### What Is Being Built

AgentHICC's TUI is being redesigned from a full-screen `prompt_toolkit` alternate-screen application to a **committed-transcript + live-bottom-block** architecture. Every completed agent turn is printed once to stdout and scrolls permanently into the terminal's native scrollback buffer. A small bottom block (4–6 rows, dynamically bounded) is erased and redrawn each frame to show the status bar, divider, input bar, mode footer, and optional dropdown.

The bottom block is implemented as a Textual `App` running in `inline=True` mode. The committed transcript is written directly to stdout via a `Terminal` class that is the sole owner of `sys.stdout`. These two layers coordinate through a `RenderLoop` that owns the render cadence and never allows concurrent writes.

### Why This Approach

The alternate-screen approach destroys the terminal's native scrollback buffer. For an autonomous software engineering agent that may run for 6+ hours, executes hundreds of tool calls, and produces thousands of output lines, the scrollback buffer *is the audit trail*. Alternate screen mode makes it impossible to: copy-paste from a previous turn, grep the session log, use tmux split-pane workflows, resume a session after SSH disconnect, or pipe output to a log file.

The committed-transcript + inline-mode architecture matches how experienced CLI developers already think about terminal output. It is the same pattern used by Claude Code, Aider, Rich's `Live` display, npm's `log-update`, and Ink.js.

### Key Decisions with Rationale

| Decision | Rationale |
|---|---|
| **No alternate screen, ever** | Scrollback is the primary work product of a long agent session. This is a hard architectural constraint, not a preference. The smcup/rmcup sequences (`\x1b[?1049h` / `\x1b[?1049l`) must never appear in any write to stdout from any code path. |
| **Textual inline mode for bottom block only** | Textual provides the best input handling (TextArea, readline emulation, @mention parsing, dropdown positioning) and is the only Python TUI framework with a production-tested inline mode (used by Toad in production). However, Textual's inline mode cannot handle an unbounded transcript — it is strictly bounded to the bottom block height. |
| **Raw stdout for committed transcript** | Committed lines must scroll into the terminal's native scrollback permanently. Textual's rendering pipeline cannot write to scrollback — only to its own inline viewport. Raw stdout via `Terminal.commit_lines()` is the correct mechanism. |
| **Single-write-per-frame atomicity** | Every bottom block update is assembled into a single `bytearray` and written with one `os.write()` call. This minimizes SSH round trips and eliminates the visual tear window between erase and redraw. |
| **50ms render debounce** | Empirically validated by Rich Live, Ink.js, and log-update as the sweet spot between perceived continuity and terminal write rate. Prevents the event loop from being dominated by rendering at high streaming token rates. |
| **FakeTerminal + pyte for all rendering tests** | Terminal rendering bugs are invisible to human code reviewers. FakeTerminal enables deterministic unit testing of all rendering paths. pyte integration tests catch ANSI sequence errors that FakeTerminal abstracts away. |
| **Pure FrameComposer** | A pure function with no I/O or side effects enables trivial snapshot testing, memoization, and O(new_turns) incremental rendering. |
| **Python + type hints + mypy strict** | This codebase targets autonomous coding agents as primary contributors. Strict types are not optional — they are the machine-readable contract that enables agents to work confidently in the codebase. |

---

## 2. Dependency Graph

The dependency graph below shows which components depend on which, and in what order they must be built. An arrow from A to B means "B depends on A; A must exist before B is built."

```
Layer 0: Foundation (no dependencies)
┌─────────────────────────────────────────────────────────┐
│  Size, Key, TerminalCapabilities                        │
│  Terminal, FakeTerminal                                 │
│  truncate_to_cols (wcwidth-based)                       │
│  Frame (frozen dataclass)                               │
│  RenderLoop (skeleton — tick/force_commit/request_redraw)│
│  InputState (pure dataclass + pure operations)          │
│  InputResult                                            │
└─────────────────────────────────────────────────────────┘
           │
           ▼
Layer 1: Core Models (depends on Layer 0)
┌─────────────────────────────────────────────────────────┐
│  TranscriptModel extensions                             │
│    - AgentTurnEntry with finalized: bool                │
│    - ToolCallEntry with ToolCallState enum              │
│    - _evict_old_turns()                                 │
│    - render_committed() → list[str]                     │
│    - render_current_partial() → list[str]               │
│  StatusState (tokens, cost, elapsed, spinner_frame)     │
│  DiffResult, DiffHunk (parsed unified diff)             │
│  ToolCallState enum (PENDING/RUNNING/SUCCESS/ERROR/     │
│    APPROVAL_NEEDED)                                     │
└─────────────────────────────────────────────────────────┘
           │
           ▼
Layer 2: Rendering (depends on Layer 1)
┌─────────────────────────────────────────────────────────┐
│  Rich Markdown rendering pipeline                       │
│    - render_markdown_to_ansi(text, width) → list[str]   │
│    - always force_terminal=True, explicit width         │
│  Tool call rendering                                    │
│    - _render_tool_call_committed(tc) → list[str]        │
│  Diff rendering                                         │
│    - _render_diff_block(diff_text, file_path, max_lines)│
│  FrameComposer                                          │
│    - compose(transcript, input_state, size, now) → Frame│
│    - _bottom_rows() — 6 zones, pure                     │
│    - _committed_cache + incremental rendering           │
└─────────────────────────────────────────────────────────┘
           │
           ▼
Layer 3: Textual Widgets (depends on Layer 2)
┌─────────────────────────────────────────────────────────┐
│  InputBar (TextArea extended, readline bindings)        │
│  ThinkingIndicator (LoadingIndicator / Static timer)    │
│  TriggerDropdown (OptionList in Float, @mention+/cmd)   │
│  ApprovalRequest (can_focus=True, focus trapping)       │
│  ModeIndicator (Button styled, reactive mode)           │
│  AgentStatusBar (Horizontal docked, reactive)           │
│  TokenMeter (Static reactive, throttled)                │
│  StreamingCursor (Static, 500ms blink interval)         │
│  ConversationDivider (Rule with label)                  │
│  ToolCallBlock (Collapsible extended)                   │
│  DiffViewer (Static + Rich Syntax)                      │
│  ExpandableOutput (Collapsible extended)                │
│  ErrorBlock (Static + Button children)                  │
│  NotificationToast (App.notify() / Toast subclass)      │
│  ContextSummary (Collapsible, lazy token count)         │
│  MentionChip (Button styled)                            │
│  SessionHeader (Static docked top)                      │
│  CommandPalette (Textual built-in extended)             │
│  AgentMessage (Widget + Markdown child)                 │
│  UserMessage (Static)                                   │
│  ChatTranscript (VerticalScroll, dynamic mounting)      │
│  ProgressIndicator (ProgressBar + Static)               │
└─────────────────────────────────────────────────────────┘
           │
           ▼
Layer 4: App Integration (depends on Layer 3)
┌─────────────────────────────────────────────────────────┐
│  BottomApp (Textual App, inline=True, fixed height)     │
│  InlineRenderer.run() — wires BottomApp + RenderLoop    │
│  SIGWINCH / SIGINT / SIGTERM / SIGHUP handler updates   │
│  _run_agent_turn() — streaming, force_commit, tool calls│
│  TUIEventAdapter — kernel AppState → Textual messages   │
│  DoomLoopDetector                                       │
│  SessionRecapGenerator                                  │
│  Session persistence integration                        │
│    - events.jsonl incremental write                     │
│    - --resume flag handler                              │
│    - SIGHUP → save + exit 0                             │
└─────────────────────────────────────────────────────────┘
           │
           ▼
Layer 5: Polish (depends on Layer 4)
┌─────────────────────────────────────────────────────────┐
│  textual-speedups installation + verification           │
│  Long session management (>200 turns, RSS < 100MB)      │
│  tmux / screen / SSH compatibility                      │
│    - synchronized output passthrough detection          │
│    - high-latency SSH debounce scaling                  │
│    - nested multiplexer detection                       │
│  WSL / Windows compatibility check                      │
│  NO_COLOR / FORCE_COLOR / COLORTERM full compliance     │
│  --accessibility mode (non-erasing stdout-only mode)    │
│  Event log rotation (100MB threshold)                   │
│  SQLite WAL checkpoint (every 1000 events)              │
└─────────────────────────────────────────────────────────┘
```

**Critical dependency constraint**: The `Terminal` class must be complete and fully tested before any other component that touches the screen is written. All other components either call `Terminal` methods or are unit-tested via `FakeTerminal`. Nothing writes to stdout except through `Terminal`.

---

## 3. Build Order (Strict Sequence)

### Layer 0: Foundation (no dependencies)

These components have zero dependencies on other new code. They can be built in any order within the layer but must all be complete before Layer 1 begins.

**`src/agenthicc/tui/terminal.py`**

```python
class Size(NamedTuple):
    rows: int
    cols: int

@dataclass(frozen=True)
class TerminalCapabilities:
    color_depth: int           # 0=none, 8=basic, 256=256color, 16777216=truecolor
    unicode_level: int         # 0=ascii, 1=bmp, 2=full
    synchronized_output: bool
    hyperlinks: bool
    no_color: bool

class Terminal:
    """Single owner of fd 1 (stdout). All terminal I/O flows through this class."""
    def __init__(self, fd: int = 1) -> None: ...
    @property def size(self) -> Size: ...
    def commit_lines(self, lines: list[str]) -> None: ...
    def set_bottom(self, rows: list[str]) -> None: ...
    def clear_bottom(self) -> None: ...
    def update_size(self) -> None: ...
    def teardown(self) -> None: ...
    def _write_atomic(self, data: bytes) -> None: ...

class FakeTerminal(Terminal):
    """Test double. No real I/O. Captures all writes for assertions."""
    committed_lines: list[str]
    bottom_history: list[list[str]]
    _current_bottom: list[str]
    write_call_count: int
```

Implementation notes:
- `_query_size()` must use `shutil.get_terminal_size()` with `.lines` and `.columns` attributes explicitly — tuple unpacking gives `(columns, lines)` which swaps rows and cols silently.
- `set_bottom()` assembles erase + new content into a single `bytearray`, then calls `os.write(self._fd, buffer)` once. Never two calls.
- `_on_sigwinch()` sets `self._resize_pending = True` only — no other work in signal handler.
- `NO_COLOR` detection happens in `__init__`. If set, `capabilities.color_depth = 0` and `capabilities.no_color = True`.
- `FakeTerminal.__init__()` must NOT call `signal.signal(SIGWINCH, ...)` — tests run without a real terminal.

**`src/agenthicc/tui/utils.py`**

```python
def truncate_to_cols(text: str, cols: int) -> str:
    """Clip ANSI-aware string to cols display columns using wcwidth."""

def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from a string."""

def display_width(text: str) -> int:
    """Return display width of text, stripping ANSI and using wcwidth."""
```

**`src/agenthicc/tui/frame.py`**

```python
@dataclass(frozen=True)
class Frame:
    rows: list[str]          # rendered rows, each pre-formatted with ANSI
    height: int              # len(rows)
    streaming_text: str | None
    cursor_row: int
    cursor_col: int
```

**`src/agenthicc/tui/input_state.py`**

```python
class Key(enum.Enum):
    ENTER = "enter"; CTRL_C = "ctrl_c"; CTRL_D = "ctrl_d"
    BACKSPACE = "backspace"; TAB = "tab"; SHIFT_TAB = "shift_tab"
    ESC = "esc"; CHAR = "char"; UP = "up"; DOWN = "down"
    LEFT = "left"; RIGHT = "right"; UNKNOWN = "unknown"
    # ... all required keys

@dataclass
class InputResult:
    submitted: bool
    text: str | None
    cancelled: bool

@dataclass
class InputState:
    buffer: str = ""
    cursor: int = 0
    history: list[str] = field(default_factory=list)
    history_idx: int = -1
    mode_name: str = "Auto"
    menu: MenuState | None = None

    def insert(self, char: str) -> InputState: ...   # pure, returns new state
    def delete_before_cursor(self) -> InputState: ...
    def move_cursor(self, delta: int) -> InputState: ...
    def submit(self) -> tuple[InputState, InputResult]: ...
    def render_lines(self, width: int) -> list[str]: ...
```

`InputState` operations are pure functions returning new `InputState` instances. No I/O.

### Layer 1: Core Models (depends on Layer 0)

**`src/agenthicc/tui/transcript.py` extensions**

Extend the existing `TranscriptModel` and related dataclasses:

```python
class ToolCallState(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    APPROVAL_NEEDED = "approval_needed"

@dataclass
class ToolCallEntry:
    tool_use_id: str
    tool_name: str
    args: dict[str, Any]
    state: ToolCallState = ToolCallState.PENDING
    duration_ms: int | None = None
    output_lines: list[str] = field(default_factory=list)
    error: str | None = None
    is_diff: bool = False
    expanded: bool = False

@dataclass
class AgentTurnEntry:
    agent_id: str
    agent_name: str
    timestamp: float
    lines: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    finalized: bool = False
    _evicted: bool = False

class TranscriptModel:
    MAX_TURNS_IN_MEMORY: ClassVar[int] = 200
    MAX_LINES_PER_TURN: ClassVar[int] = 500
    MAX_DIFF_LINES: ClassVar[int] = 50

    def _evict_old_turns(self) -> None: ...
    def render_committed(self) -> list[str]: ...
    def render_current_partial(self, partial_text: str) -> list[str]: ...
```

**`src/agenthicc/tui/status_state.py`**

```python
@dataclass
class StatusState:
    active: bool = False
    model_id: str = "claude-sonnet-4-6"
    session_id: str = ""
    mode_name: str = "Auto"
    input_tokens: int = 0
    output_tokens: int = 0
    session_cost_usd: float = 0.0
    completed_agents: int = 0
    active_agents: int = 0
    intent_started_at: float = 0.0
    spinner_frame: int = 0
    partial_text: str = ""
    background_tasks: int = 0
    doom_loop_detected: bool = False
    api_error: str | None = None
    api_error_retry_in: float | None = None
```

**`src/agenthicc/tui/diff.py`**

```python
@dataclass(frozen=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]   # raw diff lines for this hunk

@dataclass(frozen=True)
class DiffResult:
    file_path: str | None
    hunks: list[DiffHunk]
    added_count: int
    removed_count: int
    raw_text: str

def parse_unified_diff(text: str) -> DiffResult: ...
```

### Layer 2: Rendering (depends on Layer 1)

**`src/agenthicc/tui/renderers/markdown.py`**

```python
def render_markdown_to_ansi(text: str, width: int) -> list[str]:
    """Convert Markdown text to ANSI-formatted lines.

    Always uses force_terminal=True and explicit width.
    Never uses Console() with default detection.
    Strips trailing blank lines that Rich appends.
    """
```

Critical: always render at the *actual* terminal width passed in. Never hardcode 80.

**`src/agenthicc/tui/renderers/tool_call.py`**

```python
def render_tool_call_committed(tc: ToolCallEntry, width: int) -> list[str]:
    """Render a finalized tool call to committed-transcript lines."""

def render_tool_call_running(tc: ToolCallEntry, frame: int, width: int) -> str:
    """Render a running tool call for the bottom block (single line, with spinner)."""

def render_diff_block(diff: DiffResult, max_lines: int, width: int) -> list[str]:
    """Render a unified diff to ANSI lines with color-coded add/remove/hunk."""
```

**`src/agenthicc/tui/frame_composer.py`**

```python
class FrameComposer:
    """Pure function: (transcript, input_state, size, now) → Frame.

    Caches committed output to avoid re-rendering old turns on every tick.
    _committed_cache grows monotonically; never shrinks during a session.
    """

    def __init__(self) -> None:
        self._committed_cache: list[str] = []
        self._committed_turns_count: int = 0

    def compose(
        self,
        transcript: TranscriptModel,
        status: StatusState,
        input_state: InputState,
        size: Size,
        now: float | None = None,
    ) -> Frame: ...

    def _compose_bottom_rows(
        self,
        status: StatusState,
        input_state: InputState,
        size: Size,
        frame_tick: int,
    ) -> list[str]: ...
```

Bottom block zones (in order, top to bottom):
1. Streaming text zone (0–8 rows, only during active turn with partial_text)
2. Status bar (1 row, always)
3. Divider (1 row, always: `─` × width in dim)
4. Input bar (1–4 rows, always)
5. Mode footer (1 row, always)
6. Dropdown (0–8 rows, only when dropdown open)

Maximum total bottom height: `min(12, size.rows // 3)`.

### Layer 3: Textual Widgets (depends on Layer 2)

All Textual widgets must be built with `inline=True` constraints:
- No widget may grow taller than its allotted zone
- All widgets use `DEFAULT_CSS` for scoped styles
- No widget writes directly to stdout; only `Terminal.commit_lines()` and `Terminal.set_bottom()` may write

Key widgets and their Textual base classes:

| Widget | Textual Base | Critical Notes |
|---|---|---|
| `InputBar` | `TextArea` extended | Custom `on_key()`, readline bindings, @/cmd trigger detection |
| `TriggerDropdown` | `OptionList` in `Float` | Max 8 visible items, above InputBar, Escape dismisses |
| `ThinkingIndicator` | `LoadingIndicator` or `Static` + interval | Unmounted on first token (not hidden) |
| `StreamingCursor` | `Static` + `set_interval(0.5, ...)` | One in DOM at a time; removed at turn end |
| `ModeIndicator` | `Button` styled | Reactive `mode` attribute, max 6 chars label |
| `AgentStatusBar` | `Horizontal`, dock relative | Never wraps; truncate model_id on narrow terminal |
| `TokenMeter` | `Static` reactive | Throttled to 200ms max update rate |
| `ApprovalRequest` | `Widget`, `can_focus=True` | Focus trapping; diff committed before gate appears |
| `ToolCallBlock` | `Collapsible` extended | Status-aware header with success/error/running states |
| `DiffViewer` | `Static` + Rich `Syntax` | Max 200 lines before virtualisation; j/k for hunk navigation |
| `ChatTranscript` | `VerticalScroll` | Dynamic mounting; anchor() on new content; virtualise > 500 items |
| `CommandPalette` | Textual built-in extended | Group-header rows; fuzzy scoring (prefix > word > substring) |
| `ExpandableOutput` | `Collapsible` extended | Collapse threshold: > 5 lines OR > 2000 chars |
| `NotificationToast` | `Toast` subclass | Max 3 stacked; oldest displaced on 4th |
| `ErrorBlock` | `Static` + `Button` children | `role="alert"` semantics |
| `ContextSummary` | `Collapsible` | Hidden when empty; lazy token count on expand |
| `ConversationDivider` | `Rule` | Turn numbers 1-indexed |
| `MentionChip` | `Button` styled | Chips shown below textarea in InputBar, not inline |
| `AgentMessage` | `Widget` + `Markdown` child | `Markdown.get_stream()` for streaming; not `Markdown.update()` in loop |
| `UserMessage` | `Static` | Static post-submission |
| `SessionHeader` | `Static`, `dock="top"` | 1 row always; truncate left-to-right: intent → cwd → session ID |
| `ProgressIndicator` | `ProgressBar` + `Static` | 8fps (125ms), indeterminate and determinate modes |

**Anti-patterns to enforce in code review:**
- Never call `Markdown.update()` in a streaming loop — use `Markdown.get_stream()` / `MarkdownStream`
- Never query DOM in `__init__` or `compose()` — use `on_mount()`
- Never update UI from a thread without `call_from_thread()` or `post_message()`
- Never use `reactive(layout=True)` unless size actually changes
- Never leave `RichLog` without `max_lines` (unbounded memory)

### Layer 4: App Integration (depends on Layer 3)

**`src/agenthicc/tui/bottom_app.py`**

```python
class BottomApp(App[None]):
    """Textual App running inline=True. Manages the bottom block only.

    Height is dynamically computed as max(3, min(6, terminal.rows // 4)).
    Never draws above its anchor position.
    """
    INLINE_PADDING = 0

    CSS_PATH = ["styles/inline.tcss"]

    def compose(self) -> ComposeResult: ...
    def run_inline(self) -> None:
        self.run(inline=True)
```

**`src/agenthicc/tui/inline_renderer.py`**

```python
class InlineRenderer:
    """Wires Terminal + RenderLoop + BottomApp + AgentRunner together.

    Owns the coordination between:
    1. Committed transcript writes (Terminal.commit_lines)
    2. Bottom block redraws (Terminal.set_bottom via BottomApp)
    3. Agent turn lifecycle (start → streaming → force_commit → reset)
    4. Signal handlers (SIGWINCH, SIGINT, SIGTERM, SIGHUP)
    """

    async def run(self) -> None: ...
    async def _run_agent_turn(self, intent: str) -> None: ...
    def _install_signal_handlers(self) -> None: ...
```

Signal handler contracts:
- `SIGWINCH`: set `terminal._resize_pending = True`, call `render_loop.request_redraw()` — nothing else
- `SIGINT` (first): cancel current agent turn; emit `turn_cancelled`; redraw bottom in idle state
- `SIGINT` (second within 2s): graceful shutdown (same as SIGTERM)
- `SIGINT` (third): immediate exit
- `SIGTERM`: cancel turn → flush committed → clear bottom → save session → exit 0
- `SIGHUP`: cancel turn → flush committed → save session → exit 0

**`src/agenthicc/tui/tui_event_adapter.py`**

```python
class TUIEventAdapter:
    """Subscribes to kernel EventProcessor and translates AppState diffs into
    Textual messages and TranscriptModel mutations.

    Runs as an asyncio Worker inside BottomApp.
    All UI updates go through post_message() or call_from_thread().
    """

    def __init__(self, processor: EventProcessor, app: BottomApp) -> None: ...

    @work
    async def _kernel_event_loop(self) -> None:
        """Translates kernel events → TUI mutations."""
```

**`src/agenthicc/tui/doom_loop.py`**

```python
class DoomLoopDetector:
    """Detects when the same tool+args are called N times in one turn."""

    THRESHOLD: ClassVar[int] = 3

    def record_call(self, tool_name: str, args: dict[str, Any]) -> bool:
        """Returns True if doom loop detected."""

    def reset(self) -> None:
        """Call at turn start."""
```

**Session persistence integration:**
- `events.jsonl` is written incrementally on every `EventProcessor.emit()` — no batching
- `--resume SESSION_ID` calls `EventProcessor.restore_from_log()`, then replays last 20 turns to stdout via `Terminal.commit_lines()`
- SIGHUP handler must flush and save within 5 seconds
- If `events.jsonl` exceeds 100MB, rotate to `events.jsonl.1` atomically (file rename)

### Layer 5: Polish (depends on Layer 4)

These items are post-correctness optimizations and compatibility hardening:

**textual-speedups**:
- Install `textual-speedups` as an optional dependency
- Verify Rust geometry module loads: `import textual._speedups` in startup check
- Document fallback: `TEXTUAL_SPEEDUPS=0` disables if needed

**Long session management** (>200 turns, RSS < 100MB):
- `TranscriptModel._evict_old_turns()` runs automatically when `len(turns) > 200`
- Evicted turns lose `output_lines` but keep metadata and signatures
- `FrameComposer._committed_cache` is not bounded — committed lines accumulate as integers in `Terminal._committed_line_lengths` (one int per line, ~28 bytes each)
- Add `tracemalloc` benchmark test: 200 turns × 500 lines must stay under 10MB transcript memory

**tmux/screen/SSH compatibility**:
- Detect `$TMUX` → `_in_tmux = True`
- Detect `$STY` → `_in_screen = True`
- Detect `$SSH_CONNECTION` → `_in_ssh = True`
- In tmux: synchronized output passthrough requires tmux >= 3.2. Detect via `tmux -V` parse.
- On high-latency SSH (>200ms estimated): increase `MIN_TICK_INTERVAL` from 50ms to 150ms
- On `$TERM=dumb` or unset: disable all ANSI, use ASCII symbol fallback table

**NO_COLOR/FORCE_COLOR/COLORTERM compliance**:
- `NO_COLOR=1`: zero ANSI codes in any write path. Symbols (`✓ ✗ ⚠ ●`) remain.
- `FORCE_COLOR=1`: enable color even when stdout is not a TTY
- `COLORTERM=truecolor` or `24bit`: 24-bit RGB mode
- Add CI test: `NO_COLOR=1 uv run agenthicc --headless` with `grep -c $'\x1b' output` = 0

---

## 4. Milestones with Acceptance Gates

### Milestone 1: Terminal Renders Without Eating Content

**Goal**: The `Terminal` class works correctly. No smcup. No content eaten above bottom block.

**Gate criteria (all must be binary pass):**

- [ ] `uv run pytest tests/unit/test_terminal.py -v` passes — 50+ tests
- [ ] `grep -r '\x1b\[?1049h' src/` returns empty — no alternate screen code anywhere
- [ ] `grep -r '\x1b\[?1049l' src/` returns empty
- [ ] `test_set_bottom_does_not_eat_content_above()` passes — FakeTerminal verifies committed lines are untouched after set_bottom()
- [ ] `test_bottom_height_tracking()` passes — `_bottom_height` equals rows passed to `set_bottom()`
- [ ] `test_resize_query_rows_cols_not_swapped()` passes — explicitly verifies `size.rows == shutil.get_terminal_size().lines` and `size.cols == shutil.get_terminal_size().columns`
- [ ] `test_single_write_per_frame()` passes — `FakeTerminal.write_call_count == 1` after each `set_bottom()` call
- [ ] pyte integration: `test_no_alternate_screen_sequences()` passes
- [ ] pyte integration: `test_committed_lines_in_scrollback()` passes
- [ ] `test_commit_lines_visible_after_set_bottom()` passes — verifies that content committed before a `set_bottom()` appears in pyte scrollback above the bottom block

**Deliverables**: `src/agenthicc/tui/terminal.py`, `src/agenthicc/tui/utils.py`, `src/agenthicc/tui/frame.py`, `tests/unit/test_terminal.py`, `tests/integration/test_terminal_pyte.py`

---

### Milestone 2: Agent Turn Works End-to-End

**Goal**: A simulated agent turn — streaming tokens, tool call, diff — produces correct committed transcript and bottom block output.

**Gate criteria:**

- [ ] `uv run pytest tests/unit/test_transcript.py tests/unit/test_frame_composer.py -v` passes — 70+ tests
- [ ] `test_committed_lines_never_repeated()` passes — second `force_commit()` with no new turns produces no new committed lines
- [ ] `test_in_progress_turn_not_committed()` passes — streaming turn appears in bottom only, not committed
- [ ] `test_turn_commits_to_scrollback_on_finalize()` passes — after `turn.finalized = True`, committed lines include turn header + text
- [ ] `test_tool_call_spinner_in_bottom_not_committed()` passes — running tool appears in bottom block, not scrollback
- [ ] `test_tool_call_commits_on_complete()` passes — completed tool call line appears in committed lines
- [ ] `test_diff_renders_green_red_cyan()` passes — diff lines have correct ANSI codes; pyte verifies colors
- [ ] `test_doom_loop_detection()` passes — same tool+args called 3× triggers `DoomLoopDetector.record_call()` returning True
- [ ] `test_force_commit_latency()` passes — force_commit() completes in under 5ms (measured with `time.perf_counter()`)
- [ ] E2E: `test_multi_turn_session()` passes — 3 complete turns all appear in `FakeTerminal.committed_lines`, none in current bottom

**Deliverables**: Extended `transcript.py`, `status_state.py`, `diff.py`, `frame_composer.py`, rendering pipeline, `tests/unit/test_transcript.py`, `tests/unit/test_frame_composer.py`, `tests/e2e/test_agent_turn.py`

---

### Milestone 3: Full Input System

**Goal**: The `InputBar` Textual widget works with readline emulation, @mention trigger, /command trigger, and dropdown.

**Gate criteria:**

- [ ] `uv run pytest tests/unit/test_input_state.py -v` passes — 50+ tests
- [ ] `test_submit_single_line()` passes — Enter on single-line buffer returns text and clears buffer
- [ ] `test_multiline_shift_enter()` passes — Shift+Enter inserts newline; second Enter submits
- [ ] `test_at_mention_trigger()` passes — `@` after whitespace triggers MENTION_TRIGGER state
- [ ] `test_at_mention_no_trigger_in_email()` passes — `user@example` does not trigger
- [ ] `test_slash_trigger()` passes — `/` at column 0 triggers COMMAND_TRIGGER state
- [ ] `test_dropdown_arrow_navigation()` passes — Up/Down keys cycle dropdown selection
- [ ] `test_dropdown_tab_accepts()` passes — Tab inserts selected completion
- [ ] `test_dropdown_escape_dismisses()` passes — Escape closes dropdown
- [ ] `test_history_up_down()` passes — Up on empty input recalls last submitted message
- [ ] `test_readline_ctrl_a_ctrl_e()` passes — Ctrl+A moves to start; Ctrl+E to end
- [ ] `test_readline_ctrl_k()` passes — Ctrl+K kills from cursor to end
- [ ] `test_readline_ctrl_u()` passes — Ctrl+U kills from start to cursor
- [ ] `test_input_blocked_during_agent_turn()` passes — `agent_ready=False` prevents submission
- [ ] Textual Pilot: `test_inputbar_renders_in_bottom_block()` passes

**Deliverables**: `InputBar`, `TriggerDropdown`, `ModeIndicator`, `input_state.py`, `tests/unit/test_input_state.py`, `tests/unit/test_trigger_dropdown.py`

---

### Milestone 4: Complete TUI Experience

**Goal**: All 22 components work together. Full session from cold start through multi-turn conversation with tool calls, approval gate, and doom-loop detection.

**Gate criteria:**

- [ ] `uv run pytest tests/ -q` passes — no failures
- [ ] `test_approval_gate_shows_diff_before_prompt()` passes — diff committed to scrollback before approval gate appears in bottom block
- [ ] `test_approval_grant_proceeds_tool()` passes — `y` key at approval gate emits `ApprovalGranted` to kernel
- [ ] `test_approval_deny_sends_denial()` passes — `n` key emits `ApprovalDenied` to kernel
- [ ] `test_session_resume_replays_last_20_turns()` passes — `--resume SESSION_ID` prints last 20 turns then continues
- [ ] `test_sigterm_graceful_shutdown()` passes — SIGTERM → bottom cleared → session saved → exit 0
- [ ] `test_sighup_saves_session()` passes — SIGHUP → session saved → exit 0
- [ ] `test_sigwinch_redraws_within_one_tick()` passes — resize → bottom block redraws within 50ms
- [ ] `test_no_alternate_screen_full_session()` passes — full simulated session via pyte produces zero `\x1b[?1049h` occurrences
- [ ] `test_context_summary_hidden_when_empty()` passes
- [ ] `test_notification_toast_autodismiss()` passes — toast disappears after configured duration
- [ ] `test_error_block_renders_in_transcript()` passes — tool failure appends ErrorBlock to ChatTranscript
- [ ] Manual smoke test: `uv run agenthicc` in iTerm2, Alacritty, tmux, and VS Code integrated terminal — checklist signed off

**Deliverables**: All 22 components, `inline_renderer.py`, `tui_event_adapter.py`, `bottom_app.py`, session persistence integration, full test suite, manual smoke test sign-off

---

### Milestone 5: Production Ready

**Goal**: Performance budget met. All compatibility targets hit. mypy strict and ruff clean.

**Gate criteria:**

- [ ] `uv run pytest tests/ -q` — 490+ unit, 165+ integration, 105+ E2E tests, all pass
- [ ] `grep -r '\x1b\[?1049h' tests/` and `src/` — both return empty
- [ ] Cold start `time uv run agenthicc --headless --one-shot "hello"` < 800ms (measured 5 runs, median)
- [ ] Memory benchmark: 200 turns × average response — RSS < 100MB at completion
- [ ] `uv run mypy src/agenthicc --strict` — 0 errors
- [ ] `uv run ruff check src/ tests/` — 0 errors
- [ ] `uv run ruff format --check src/ tests/` — 0 errors
- [ ] `uv run python scripts/check_llms.py` — 0 coverage gaps
- [ ] Compatibility matrix tested and signed off (see Section 6.2)
- [ ] `NO_COLOR=1 uv run agenthicc --headless --one-shot "hello" > /tmp/out.txt && grep -c $'\x1b' /tmp/out.txt` outputs `0`
- [ ] `textual-speedups` installed and Rust geometry module detected in startup
- [ ] Event log rotation test: write > 100MB to `events.jsonl`, verify atomic rotation

---

## 5. Risk Analysis

### Risk 1: Textual Inline Mode Bugs

**Description**: Textual's inline mode (`App.run(inline=True)`) has a history of rendering bugs. Known issues include scrollback duplication on resize (fixed in v2.1.116+), garbled output with `inline_no_clear` (fixed in v6.0.0), and laggy interactive widgets (fixed in post-v0.56.2 releases). New bugs may exist in the version range we target.

**Probability**: Medium  
**Impact**: High — corrupted scrollback defeats the primary design goal  
**Mitigation**:
- Pin Textual to a specific tested version in `pyproject.toml` (`textual = ">=0.56.0,<1.0.0"` or more narrowly)
- Run pyte integration tests against the full rendering pipeline on every commit
- Test against all 16 terminal emulators in the compatibility matrix before each release
- Keep `textual-speedups` separate from the Textual version pin

**Contingency**: If Textual inline mode is unfixable for a critical bug, the bottom block can be rewritten using the raw `Terminal.set_bottom()` approach documented in `non-alternate-screen-architecture.md` Section 10 — no Textual dependency. The architecture deliberately keeps Textual confined to the bottom block to make this swap feasible.

---

### Risk 2: Terminal Width/Height Swap Bug

**Description**: `shutil.get_terminal_size()` returns a `os.terminal_size` namedtuple. Tuple unpacking gives `(columns, lines)` — assigning to `(rows, cols)` silently swaps width and height. This has historically caused layout bugs that only manifest on non-square terminals.

**Probability**: Low (fixed with explicit attribute access)  
**Impact**: Critical — every rendered line has wrong width; bottom block position is wrong  
**Mitigation**:
- `Terminal._query_size()` must use `.lines` and `.columns` attributes explicitly, never tuple unpacking
- Unit test `test_resize_query_rows_cols_not_swapped()` is a mandatory Milestone 1 gate
- Code review checklist includes this specific check

**Contingency**: If the bug slips through, the symptom is consistent wrong-width rendering — immediately visible in any terminal test. The fix is a one-line change.

---

### Risk 3: Rich Markup in Committed Lines

**Description**: Rich uses `[bold]text[/bold]` markup syntax which conflicts with regular bracket characters. If agent response text contains literal `[` characters that Rich interprets as markup, committed lines will have garbled formatting or raised exceptions.

**Probability**: High — LLM responses commonly contain brackets (array syntax, Markdown links, etc.)  
**Impact**: Medium — visual corruption in committed transcript  
**Mitigation**:
- Always pass `markup=False` to `Console` when rendering agent response text via `Markdown`
- Use `Console.print(Markdown(text))` — the Markdown renderer handles escaping internally
- For non-Markdown lines (turn headers, tool call lines), construct manually with ANSI codes — no Rich markup
- Unit tests assert on lines containing brackets

**Contingency**: If Rich markup leaks through, add a `rich.markup.escape()` call on the text before passing to Console.

---

### Risk 4: tmux Scroll Region Conflicts

**Description**: The `non-alternate-screen-architecture.md` explicitly prohibits DECSTBM scroll regions (`\x1b[\d+;\d+r`). If any dependency (Rich, Textual, or legacy code) emits DECSTBM, it will conflict with tmux's own scroll region management and corrupt the display.

**Probability**: Low (explicitly tested)  
**Impact**: High — visual corruption in tmux sessions, which is the primary remote use case  
**Mitigation**:
- Integration test `test_no_alternate_screen_sequences()` checks for DECSTBM in addition to smcup/rmcup
- `AGENTHICC_DEBUG_NO_ALTSCREEN=1` env var activates traced write that raises on forbidden sequences
- pyte tests run on every commit

**Contingency**: Identify the dependency emitting DECSTBM via the traced write hook; patch or replace it.

---

### Risk 5: Long Session Memory Growth

**Description**: A 6-8 hour session may accumulate 300-500 turns. Each turn has output lines, tool call records, and diff blocks. Without bounded eviction, RSS can exceed 100MB and trigger OOM.

**Probability**: Medium — DevOps users (Jordan persona) run agenthicc for hours  
**Impact**: High — OOM crash loses session state  
**Mitigation**:
- `TranscriptModel._evict_old_turns()` runs automatically at 200 turns
- Eviction clears `output_lines` on old tool calls (already committed to scrollback)
- `FrameComposer._committed_cache` stores only the committed lines as strings — no repeated re-render
- Memory benchmark test is a Milestone 5 gate
- Event log rotation at 100MB prevents `events.jsonl` from growing unboundedly

**Contingency**: If RSS exceeds 200MB in testing, implement turn virtualization: remove `AgentTurnEntry` from `TranscriptModel.turns` after eviction (keep only metadata), and store raw committed lines in a bounded ring buffer.

---

### Risk 6: SIGWINCH During Agent Turn

**Description**: Terminal resize during an active agent turn can desync `Terminal._bottom_height` if the resize changes the number of rows needed by the streaming text zone (which wraps differently at the new width).

**Probability**: Medium — developers frequently resize terminal windows  
**Impact**: Medium — temporary visual corruption; corrects on next tick  
**Mitigation**:
- SIGWINCH sets `_resize_pending = True`, handled on next render tick
- RenderLoop explicitly checks `terminal.resize_pending` before each tick
- `test_sigwinch_redraws_within_one_tick()` is a Milestone 4 gate
- `Terminal.set_bottom()` unconditionally erases with `ESC[{n}A ESC[0J` regardless of tracked height — this heals desyncs

**Contingency**: If desyncs persist, add explicit `Terminal.force_sync()` that clears from the top of the terminal down (ESC[H ESC[0J), then redraws committed + bottom.

---

### Risk 7: Signal Handler Accumulation

**Description**: If `InlineRenderer._install_signal_handlers()` is called multiple times (e.g., during test setup/teardown or session resume), signal handlers accumulate and the previous handler may not be restored. This causes zombie handlers that fire on signals after the TUI exits.

**Probability**: Medium — test isolation and session resume both call `_install_signal_handlers()`  
**Impact**: Medium — ghost handlers cause unexpected behavior after TUI exit  
**Mitigation**:
- `_install_signal_handlers()` saves the previous handler via `signal.signal()` return value
- `_uninstall_signal_handlers()` restores previous handlers — called in `teardown()`
- Tests use `pytest.fixture(autouse=True)` to restore signal handlers after each test
- `FakeTerminal` does not register SIGWINCH — tests that need it mock it explicitly

**Contingency**: Add assertion in `__main__.py` that signal handlers are clean on startup.

---

### Risk 8: Bottom Block Height Tracking Desync

**Description**: `Terminal._bottom_height` must always equal the actual number of rows the bottom block occupies. If it goes out of sync (e.g., a line wraps unexpectedly, or `set_bottom()` is called with rows that contain embedded newlines), the erase sequence will erase wrong rows.

**Probability**: Medium — any embedded newline in a bottom block row causes the mismatch  
**Impact**: High — content above the bottom block gets erased  
**Mitigation**:
- `set_bottom()` strips embedded newlines from each row before rendering: `row.replace('\n', ' ')`
- `truncate_to_cols()` is called on every row before passing to `set_bottom()` — prevents wrap
- `FakeTerminal.write_call_count` test ensures single-write atomicity
- Unit test `test_bottom_height_matches_rows_passed()` runs on every tick in the test suite

**Contingency**: Add a defensive check in `set_bottom()`: if any row contains a newline, split it and warn (debug log, not exception).

---

### Risk 9: Streaming Token Rate Exceeds Render Budget

**Description**: Some Claude models stream at 150+ tokens/second. At 5 chars/token average, the streaming text zone can accumulate 750 chars/second. If the render loop can't keep up, the bottom block falls behind reality.

**Probability**: Low — the 50ms debounce handles this in normal cases  
**Impact**: Low — visual lag during high-rate streaming; no data loss  
**Mitigation**:
- The 50ms debounce (20fps max) means at most 20 redraws/second regardless of token rate
- `StatusState.partial_text` accumulates between ticks — the render loop picks up the latest state, not a queue of intermediate states
- `MarkdownStream` in the Textual layer internally batches updates at display refresh rate
- CPU benchmark test: `test_streaming_cpu_usage()` measures < 15% single-core during simulated 200 tok/s stream

**Contingency**: If latency exceeds 100ms, implement adaptive debounce: increase `MIN_TICK_INTERVAL` when `len(partial_text) > 10_000` chars.

---

### Risk 10: WSL/Windows Compatibility

**Description**: Textual's inline mode is documented as not supported on Windows (native, not WSL). The POSIX signal API (`signal.SIGWINCH`, `signal.SIGHUP`) is unavailable on Windows. `tty.setcbreak()` is unavailable.

**Probability**: Low for direct impact (WSL is widely used); Medium for user confusion  
**Impact**: Medium — Windows users without WSL cannot use the TUI  
**Mitigation**:
- Document WSL2 requirement clearly in README
- At startup, check `sys.platform == 'win32'` — if True and not running under WSL, print error and offer `--headless` mode
- Headless mode (JSON-lines stdout) works on Windows without POSIX requirements
- All POSIX-specific code guarded with `if sys.platform != 'win32':` checks

**Contingency**: If Windows native support becomes a requirement, investigate Windows ConPTY API. This is out of scope for v1.0.

---

## 6. Test Coverage Matrix

### Layer Coverage Targets

| Component | Unit Tests | Integration Tests | E2E Tests | Coverage Target |
|---|---|---|---|---|
| `Terminal` | 50 | 20 | 5 | 95% |
| `FakeTerminal` | 10 | — | — | 100% |
| `truncate_to_cols` / `utils` | 30 | 5 | — | 100% |
| `Frame` | 5 | — | — | 100% |
| `InputState` | 50 | 10 | 5 | 95% |
| `FrameComposer` | 30 | 10 | 5 | 90% |
| `RenderLoop` | 20 | 10 | 5 | 90% |
| `TranscriptModel` | 40 | 15 | 10 | 90% |
| `StatusState` | 10 | 5 | — | 90% |
| `DiffResult` / `DiffHunk` | 20 | 5 | — | 95% |
| Markdown renderer | 15 | 5 | — | 90% |
| Tool call renderer | 20 | 5 | — | 90% |
| Diff renderer | 20 | 5 | — | 90% |
| `InputBar` widget | 25 | 10 | 5 | 85% |
| `TriggerDropdown` | 20 | 5 | 5 | 85% |
| `ThinkingIndicator` | 5 | 3 | — | 80% |
| `StreamingCursor` | 5 | 3 | — | 80% |
| `ModeIndicator` | 10 | 3 | — | 85% |
| `AgentStatusBar` | 10 | 5 | — | 85% |
| `TokenMeter` | 10 | 5 | — | 85% |
| `ApprovalRequest` | 15 | 5 | 5 | 85% |
| `ToolCallBlock` | 15 | 5 | 5 | 80% |
| `DiffViewer` | 10 | 5 | 5 | 80% |
| `ExpandableOutput` | 10 | 5 | — | 80% |
| `ErrorBlock` | 10 | 3 | — | 80% |
| `NotificationToast` | 10 | 3 | — | 80% |
| `ContextSummary` | 10 | 3 | — | 80% |
| `MentionChip` | 10 | 3 | — | 80% |
| `AgentMessage` | 15 | 5 | 5 | 80% |
| `UserMessage` | 5 | 3 | — | 80% |
| `ChatTranscript` | 15 | 5 | 5 | 80% |
| `ConversationDivider` | 5 | — | — | 80% |
| `ProgressIndicator` | 5 | 3 | — | 80% |
| `SessionHeader` | 5 | — | — | 80% |
| `CommandPalette` | 10 | 5 | 5 | 80% |
| `BottomApp` | 10 | 10 | 5 | 80% |
| `InlineRenderer` | 15 | 10 | 10 | 80% |
| `TUIEventAdapter` | 15 | 10 | 5 | 80% |
| `DoomLoopDetector` | 10 | — | 5 | 90% |
| `SessionRecapGenerator` | 10 | — | — | 85% |
| Session persistence | 20 | 10 | 5 | 85% |
| Signal handlers | 10 | 5 | 5 | 85% |
| pyte integration scenarios | — | 25 | — | — |
| Full E2E session flows | — | — | 20 | — |

**Totals: 490+ unit, 165+ integration, 105+ E2E tests**

### Key Test Files

| Test File | Layer | Focus |
|---|---|---|
| `tests/unit/test_terminal.py` | Unit | Terminal class, FakeTerminal, erase sequences |
| `tests/unit/test_input_state.py` | Unit | Pure InputState operations, all key combinations |
| `tests/unit/test_frame_composer.py` | Unit | Pure FrameComposer, committed cache, bottom zones |
| `tests/unit/test_render_loop.py` | Unit | Debounce, force_commit, committed-lines dedup |
| `tests/unit/test_transcript.py` | Unit | TranscriptModel, eviction, render_committed |
| `tests/unit/test_diff.py` | Unit | Diff parsing, DiffResult, hunk extraction |
| `tests/unit/test_doom_loop.py` | Unit | DoomLoopDetector threshold, reset |
| `tests/unit/test_approval_gate.py` | Unit | ApprovalRequest state machine, y/n/a responses |
| `tests/unit/test_tui_transcript.py` | Unit | Existing transcript tests (must remain passing) |
| `tests/integration/test_terminal_pyte.py` | Integration | pyte-based no-alternate-screen, scrollback, input bar position |
| `tests/integration/test_tui_rendering.py` | Integration | Full rendering pipeline pyte scenarios |
| `tests/integration/test_agent_turn_pyte.py` | Integration | Streaming → commit cycle via pyte |
| `tests/e2e/test_tui_e2e.py` | E2E | Full session: cold start → turns → approval → shutdown |
| `tests/e2e/test_session_resume.py` | E2E | --resume flag, event log replay |

---

## 7. Definition of Done

### Per Feature

Every new function, class, or method is done when:

- [ ] Implementation complete and integrated into the module's `__init__.py` exports
- [ ] Unit tests pass (`pytest tests/unit/ -q` — no failures)
- [ ] Integration tests pass for affected subsystems
- [ ] `uv run mypy src/agenthicc/tui/<file>.py --strict` — 0 errors
- [ ] `uv run ruff check src/agenthicc/tui/<file>.py` — 0 errors
- [ ] All public symbols documented (docstring on every public function and class)
- [ ] Performance budget met for the component (see Section 9.3)
- [ ] `grep -n '\x1b\[?1049h' src/agenthicc/tui/<file>.py` — empty

### Per Milestone

**Milestone 1 done when:**
- [ ] All Milestone 1 gate criteria pass
- [ ] `tests/unit/test_terminal.py` has 50+ tests
- [ ] `tests/integration/test_terminal_pyte.py` has 20+ tests
- [ ] The rows/cols swap bug has a dedicated regression test

**Milestone 2 done when:**
- [ ] All Milestone 2 gate criteria pass
- [ ] `TranscriptModel._evict_old_turns()` is tested with a 201-turn session
- [ ] `FrameComposer._committed_cache` incremental rendering is verified: second `compose()` with no new turns produces identical committed list pointer (no copy)

**Milestone 3 done when:**
- [ ] All Milestone 3 gate criteria pass
- [ ] `should_trigger_at_mention()` edge cases tested: start of line, after whitespace, mid-word, email address, URL
- [ ] `/` trigger only fires at column 0 (not mid-sentence)
- [ ] Textual Pilot tests cover InputBar key handling

**Milestone 4 done when:**
- [ ] All Milestone 4 gate criteria pass
- [ ] Manual smoke test completed in: iTerm2, Alacritty, tmux 3.2+ inside iTerm2, VS Code integrated terminal, over SSH with `xterm-256color`
- [ ] `agenthicc --resume` tested with a real events.jsonl from a previous session
- [ ] Doom loop detection tested manually: agent stuck in a loop triggers the banner

**Milestone 5 done when:**
- [ ] All Milestone 5 gate criteria pass
- [ ] All compatibility matrix terminals signed off
- [ ] Release notes written
- [ ] `CLAUDE.md` updated with new TUI module documentation

### For Release (v1.0)

**Binary, measurable criteria — all must pass:**

- [ ] `uv run pytest tests/ -q` — 0 failures, 0 errors
- [ ] Total test count: unit ≥ 490, integration ≥ 165, E2E ≥ 105
- [ ] `grep -r '\x1b\[?1049h' src/ tests/` — returns empty
- [ ] `grep -r '\x1b\[?1049l' src/ tests/` — returns empty
- [ ] `uv run mypy src/agenthicc --strict` — 0 errors
- [ ] `uv run ruff check src/ tests/` — 0 errors
- [ ] `uv run ruff format --check src/ tests/` — 0 errors
- [ ] `uv run python scripts/check_llms.py` — 0 coverage gaps
- [ ] Cold start (median of 5 runs): `time uv run agenthicc --headless --one-shot "ping"` < 800ms
- [ ] Warm start (process cached): < 300ms
- [ ] Memory at 200 turns: RSS < 100MB (measured via `tracemalloc`)
- [ ] Render cycle: `FrameComposer.compose()` median < 8ms for 80-column terminal with 10 completed turns
- [ ] `set_bottom()` median < 5ms (measured in benchmark test)
- [ ] `NO_COLOR=1` output: `grep -c $'\x1b'` = 0
- [ ] Works in: tmux 3.2+ (macOS + Linux), GNU screen 4.9+, SSH (xterm-256color), VS Code integrated terminal
- [ ] Windows WSL2: full support documented and tested
- [ ] Windows native: `--headless` fallback works; informative error on `--tui`

---

## 8. Required Dependencies

```toml
# pyproject.toml additions

[project.dependencies]
# Core TUI
textual = ">=0.56.0,<1.0.0"   # Inline mode is production-grade from 0.56
rich = ">=13.7.0"              # Markdown, Syntax, Console
wcwidth = ">=0.2.13"           # Correct display-width for Unicode

# Optional: Rust accelerator for Textual geometry
# Installed separately to avoid build requirement on every platform
# textual-speedups = ">=0.1.0"

[project.optional-dependencies]
dev = [
    "pyte>=0.8.0",             # Terminal emulator for integration tests
    "pytest-asyncio>=0.23.0",
    "pytest-textual-snapshot>=0.4.0",  # Snapshot tests for Textual widgets
    "mypy>=1.9.0",
    "ruff>=0.4.0",
    "tracemalloc",             # stdlib; no install needed
    "psutil>=5.9.0",           # CPU/memory benchmarks
]

[tool.textual]
# Inline mode: disables alternate screen
# Set in the App class: self.run(inline=True)
```

**Version pinning rationale:**
- `textual >= 0.56.0`: `MarkdownStream` (`Markdown.get_stream()`) introduced in v4.0; inline mode scrollback duplication fixed in v2.1.116+; garbled `inline_no_clear` fixed in v6.0.0. The `0.56.0` floor covers all of these.
- `rich >= 13.7.0`: `Markdown` widget stability; `Console` with `force_terminal=True` parameter.
- `wcwidth >= 0.2.13`: Correct Unicode 15 character widths.
- `pyte >= 0.8.0`: Required for `pyte.Stream` API used in integration tests.

**Textual version considerations:**
- Textual makes breaking changes at major versions. Pin to `<1.0.0` for this release cycle.
- When upgrading Textual major version, run the full pyte integration suite to detect rendering regressions before updating the pin.

---

## 9. Engineering Standards

### Code Quality

Every file in `src/agenthicc/tui/` must satisfy:

```
from __future__ import annotations  # required on every file
```

- **Type hints required on all function signatures, including `self` return types where applicable**
- `mypy --strict` zero errors — `--strict` includes `--disallow-untyped-defs`, `--disallow-incomplete-defs`, `--check-untyped-defs`, `--no-implicit-optional`
- `ruff check` zero errors — `line-length = 100`, all default rules enabled
- `ruff format --check` zero errors
- No `# type: ignore` comments without a documented reason in the same line comment
- No `Any` unless the typing cannot be expressed — document with `# Any: <reason>`
- Docstring on every public function, class, and method

### Architecture Rules

These rules are mandatory. Any PR that violates them must be rejected:

1. **`Terminal` is the ONLY module that writes to `sys.stdout` or `os.write(1, ...)`**. No other module may write to stdout, fd 1, or any file handle that points to the terminal. Violation: immediate reject.

2. **`FrameComposer.compose()` must be pure**. No I/O, no async, no state mutation, no side effects. It reads `transcript`, `status`, `input_state`, `size`, and `now` — nothing else. Violation: immediate reject.

3. **`RenderLoop` is the sole caller of `Terminal.commit_lines()` and `Terminal.set_bottom()`**. No other code path may call these methods. Violation: immediate reject.

4. **`InputState` operations are pure**. Methods return new `InputState` instances. No mutation. Tests verify this by asserting that the original instance is unchanged after every operation.

5. **Signal handlers must complete in under 1ms**. They may set flags and call `render_loop.request_redraw()` (which sets a flag). They must not call `await`, acquire locks, write to disk, or call any function with unknown latency.

6. **No blocking operations in any async event handler**. If a handler needs to do I/O or computation > 1ms, it must dispatch to a `@work` worker or `asyncio.create_task()`.

7. **No alternate screen, ever**. This is not a soft recommendation. The sequences `\x1b[?1049h`, `\x1b[?1049l`, `\x1b[?47h`, `\x1b[?1047h` must never appear in any write path. The CI integration test enforces this on every commit.

8. **Textual widgets must not grow taller than their allotted zone**. The bottom block is bounded to `min(12, size.rows // 3)` rows. Any widget that could grow unboundedly must have a `max-height` CSS rule.

9. **All inter-widget communication goes through Textual messages**. No direct method calls across widget boundaries (except queries within the same widget). This ensures testability and event traceability.

10. **No circular imports**. The dependency graph in Section 2 is strict. Layer N may only import from layers 0..N-1. CI enforces this via `uv run python -c "import agenthicc.tui"` — if a circular import exists, this fails.

### Performance Rules

| Operation | Budget | Measurement |
|---|---|---|
| Cold start to first bottom block draw | < 800ms | `time uv run agenthicc --headless --one-shot ""` |
| Warm start (cached imports) | < 300ms | Second invocation |
| `FrameComposer.compose()` per call | < 8ms | Benchmark test, 80-col terminal, 10 turns |
| `Terminal.set_bottom()` per call | < 5ms | Benchmark test |
| Force-commit at turn end | < 5ms | `time.perf_counter()` in test |
| RenderLoop tick interval | 50ms minimum | `MIN_TICK_INTERVAL = 0.050` constant |
| Memory per turn (avg) | < 50KB | `tracemalloc` benchmark |
| RSS at 200 turns | < 100MB | `tracemalloc` + `psutil.Process().memory_info().rss` |
| CPU idle (between turns) | < 1% | `psutil` benchmark |
| CPU during streaming | < 15% single core | `psutil` benchmark at 200 tok/s |
| Signal handler execution | < 1ms | Not measurable in tests; enforce by inspection |

---

## 10. Release Criteria

The following criteria are **binary and measurable**. v1.0 is not released until all are checked:

### Functionality

- [ ] All 22 components in the component inventory are implemented and tested
- [ ] All 5 milestone acceptance gates pass
- [ ] `agenthicc` starts, handles a conversation, and exits cleanly in each compatibility target terminal
- [ ] Session resume (`--resume SESSION_ID`) works after clean exit, crash, and SIGHUP
- [ ] All 6 permission modes (Auto/Plan/Ask/Review/Safe/Debug) are visually distinguishable and togglable
- [ ] @mention autocomplete resolves files, directories, and globs
- [ ] /command palette shows all registered skills and built-in commands with fuzzy search
- [ ] Approval gate shows diff before prompt (never after) for file write operations
- [ ] Doom-loop detection fires correctly after 3 identical tool+args invocations

### Testing

- [ ] `uv run pytest tests/ -q` — 0 failures, unit ≥ 490, integration ≥ 165, E2E ≥ 105
- [ ] pyte integration tests cover all no-alternate-screen invariants
- [ ] Snapshot tests (`pytest-textual-snapshot`) cover all Textual widget states

### Code Quality

- [ ] `uv run mypy src/agenthicc --strict` — 0 errors
- [ ] `uv run ruff check src/ tests/` — 0 errors
- [ ] `uv run ruff format --check src/ tests/` — 0 errors
- [ ] `uv run python scripts/check_llms.py` — 0 coverage gaps
- [ ] Zero `# type: ignore` without documented reason

### Performance

- [ ] Cold start < 800ms (median of 5 runs)
- [ ] RSS at 200 turns < 100MB
- [ ] `FrameComposer.compose()` < 8ms per call (80-column terminal)
- [ ] `Terminal.set_bottom()` < 5ms per call
- [ ] CPU idle < 1% (measured over 60 seconds with no agent activity)

### Security (No Alternate Screen)

- [ ] `grep -r '\x1b\[?1049h' src/` — empty
- [ ] `grep -r '\x1b\[?1049l' src/` — empty
- [ ] `grep -r '\x1b\[\d*;\d*r' src/` — empty (no DECSTBM scroll regions)
- [ ] pyte integration test `test_no_alternate_screen_sequences()` passes on full session

### Compatibility

- [ ] iTerm2 2.x+ (macOS) — visual sign-off
- [ ] Alacritty 0.12+ (Linux) — visual sign-off
- [ ] tmux 3.2+ wrapping iTerm2 — visual sign-off
- [ ] VS Code integrated terminal — visual sign-off
- [ ] SSH over OpenSSH with `xterm-256color` — visual sign-off
- [ ] `NO_COLOR=1` mode — automated test passes + visual sign-off
- [ ] WSL2 (Ubuntu 22.04) — visual sign-off
- [ ] Windows native: informative error + `--headless` works

### Documentation

- [ ] `CLAUDE.md` updated with new TUI module paths and architecture notes
- [ ] All 22 components have complete docstrings
- [ ] `prds/implementation/` contains this master PRD (committed to repo)
- [ ] `CHANGELOG.md` entry for v1.0 TUI redesign

---

## Appendix A: Canonical ANSI Sequences Reference

Sequences used by this implementation:

| Sequence | Meaning | Used in |
|---|---|---|
| `\x1b[{n}A` | Cursor up n rows | `Terminal.set_bottom()`, `commit_lines()` |
| `\x1b[0J` | Erase from cursor to end of screen | `Terminal.set_bottom()`, `commit_lines()` |
| `\x1b[0m` | Reset all attributes | End of every styled span |
| `\x1b[1m` | Bold | Turn headers, prompt |
| `\x1b[2m` | Dim | Timestamps, secondary info |
| `\x1b[22m` | Bold off | End of thinking-wave bold char |
| `\x1b[36m` | Cyan | Input token counter, @mention |
| `\x1b[32m` | Green | ✓ success, diff adds |
| `\x1b[31m` | Red | ✗ failure, diff removes |
| `\x1b[33m` | Yellow | ⚠ warning, PLAN mode |
| `\x1b[34m` | Blue | REVIEW mode |
| `\x1b[35m` | Magenta | Agent header ●, DEBUG mode |
| `\x1b[1;36m` | Bold cyan | Agent header bullet |
| `\x1b[1;32m` | Bold green | Input prompt |
| `\x1b[?2026h` | Begin Synchronized Update | When terminal supports it |
| `\x1b[?2026l` | End Synchronized Update | Paired with BSU |
| `\x1b[?25h` | Show cursor | `Terminal.teardown()` |
| `\x1b[?25l` | Hide cursor | Optional, during heavy rendering |

Sequences that **must never appear** in any write path:

| Sequence | Meaning | Why forbidden |
|---|---|---|
| `\x1b[?1049h` | smcup (enter alternate screen) | Destroys scrollback — terminal contract violation |
| `\x1b[?1049l` | rmcup (exit alternate screen) | Destroys scrollback |
| `\x1b[?47h` | Older alternate screen variant | Same as smcup |
| `\x1b[?1047h` | Save/restore cursor + screen | Same as smcup |
| `\x1b[{t};{b}r` | DECSTBM scroll region | Conflicts with Rich, breaks in tmux |

---

## Appendix B: ASCII Symbol Fallback Table

When `$TERM=xterm` (8-color) or `--ascii` flag is passed, use these fallbacks:

| Unicode Symbol | Codepoint | Fallback | Role |
|---|---|---|---|
| ● | U+25CF | `*` | Agent turn header |
| ○ | U+25CB | `o` | Pending indicator |
| ✓ | U+2713 | `[ok]` | Success |
| ✗ | U+2717 | `[!!]` | Failure |
| ⚠ | U+26A0 | `[!]` | Warning |
| ⎿ | U+23BF | `>` | Tool call prefix |
| ◆ | U+25C6 | `<>` | PLAN mode badge |
| ─ | U+2500 | `-` | Divider |
| │ | U+2502 | `\|` | Vertical line |
| ┌┐└┘ | U+250C etc | `+` | Dropdown corners |
| ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ | Braille | `\|/-\\` | Spinner frames |
| ⊕ | U+2295 | `[R]` | REVIEW mode badge |
| ⛔ | U+26D4 | `[S]` | SAFE mode badge |
| ⚙ | U+2699 | `[D]` | DEBUG mode badge |

---

## Appendix C: File Structure

```
src/agenthicc/tui/
  __init__.py                  # Re-exports: Terminal, FakeTerminal, Size, Frame,
                               #   InputState, TranscriptModel, FrameComposer,
                               #   RenderLoop, InlineRenderer, BottomApp
  terminal.py                  # Terminal, FakeTerminal, Size, TerminalCapabilities
  utils.py                     # truncate_to_cols, strip_ansi, display_width
  frame.py                     # Frame frozen dataclass
  input_state.py               # InputState, InputResult, Key enum
  transcript.py                # TranscriptModel, AgentTurnEntry, ToolCallEntry,
                               #   ToolCallState, DiffResult, DiffHunk
  status_state.py              # StatusState
  diff.py                      # parse_unified_diff, DiffResult, DiffHunk
  frame_composer.py            # FrameComposer
  render_loop.py               # RenderLoop
  doom_loop.py                 # DoomLoopDetector
  session_recap.py             # SessionRecapGenerator
  tui_event_adapter.py         # TUIEventAdapter
  inline_renderer.py           # InlineRenderer
  bottom_app.py                # BottomApp (Textual App, inline=True)

  renderers/
    __init__.py
    markdown.py                # render_markdown_to_ansi
    tool_call.py               # render_tool_call_committed, render_tool_call_running
    diff.py                    # render_diff_block

  widgets/
    __init__.py
    input_bar.py               # InputBar (TextArea extended)
    trigger_dropdown.py        # TriggerDropdown (OptionList in Float)
    thinking_indicator.py      # ThinkingIndicator
    streaming_cursor.py        # StreamingCursor
    mode_indicator.py          # ModeIndicator
    agent_status_bar.py        # AgentStatusBar
    token_meter.py             # TokenMeter
    approval_request.py        # ApprovalRequest
    tool_call_block.py         # ToolCallBlock
    diff_viewer.py             # DiffViewer
    expandable_output.py       # ExpandableOutput
    error_block.py             # ErrorBlock
    notification_toast.py      # NotificationToast
    context_summary.py         # ContextSummary
    mention_chip.py            # MentionChip
    agent_message.py           # AgentMessage
    user_message.py            # UserMessage
    chat_transcript.py         # ChatTranscript
    conversation_divider.py    # ConversationDivider
    progress_indicator.py      # ProgressIndicator
    session_header.py          # SessionHeader
    command_palette.py         # CommandPalette

  styles/
    app.tcss                   # CSS variables, themes
    layout.tcss                # Layout-only rules
    widgets.tcss               # Widget-specific styles
    inline.tcss                # :inline pseudo-selector rules

tests/unit/
  test_terminal.py             # Terminal, FakeTerminal (50+ tests)
  test_input_state.py          # InputState (50+ tests)
  test_frame_composer.py       # FrameComposer (30+ tests)
  test_render_loop.py          # RenderLoop (20+ tests)
  test_transcript.py           # TranscriptModel (40+ tests)
  test_diff.py                 # DiffResult parsing (20+ tests)
  test_doom_loop.py            # DoomLoopDetector (10+ tests)
  test_approval_gate.py        # ApprovalRequest (15+ tests)
  test_tui_transcript.py       # Existing tests — must remain passing

tests/integration/
  test_terminal_pyte.py        # pyte: no alternate screen, scrollback, input bar
  test_tui_rendering.py        # pyte: full rendering pipeline scenarios
  test_agent_turn_pyte.py      # pyte: streaming → commit cycle

tests/e2e/
  test_tui_e2e.py              # Full session: cold start → turns → approval → shutdown
  test_session_resume.py       # --resume flag, event log replay
```
