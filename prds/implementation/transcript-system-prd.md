# AgentHICC Transcript System — Implementation PRD

**Document:** transcript-system-prd.md  
**Status:** Implementation-ready  
**Consumes:** tui-redesign-prd.md (master), component-inventory.md, non-alternate-screen-architecture.md  
**Output module:** `src/agenthicc/tui/transcript.py`  
**Extends:** Existing `TranscriptModel` referenced throughout tests (test_transcript.py, test_transcript_extended.py)  
**Framework:** Python 3.11+, no Textual dependency in this module, Rich for Markdown rendering only  
**Hard constraint:** No alternate screen. All committed content flows through `Terminal.commit_lines()` permanently to scrollback.

---

## 1. Conversation Data Model

### 1.1 Message Types

Every piece of content that can appear in the transcript is represented by a typed Python dataclass. These live in `src/agenthicc/tui/transcript.py`.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal

# ─── Enumerations ─────────────────────────────────────────────────────────────

class MessageState(Enum):
    """Lifecycle state for any message type."""
    PENDING    = auto()   # Created, not yet rendering
    STREAMING  = auto()   # Tokens arriving; partial content displayed in bottom block
    COMPLETE   = auto()   # Fully rendered; committed to scrollback
    FINALIZED  = auto()   # Committed AND all post-processing done (tool calls resolved)
    CANCELLED  = auto()   # Turn aborted by user (Ctrl+C)
    ERROR      = auto()   # Turn ended with unhandled error

class ToolCallState(Enum):
    """Lifecycle state for a single tool invocation."""
    PENDING          = auto()  # Emitted, not yet started
    RUNNING          = auto()  # Executor has started; spinner shown in bottom block
    SUCCESS          = auto()  # Tool returned result; committed line shows ✓
    FAILURE          = auto()  # Tool returned error; committed line shows ✗
    APPROVAL_NEEDED  = auto()  # Blocked; approval gate replaces input bar
    DENIED           = auto()  # User denied at approval gate

# ─── Core message dataclasses ─────────────────────────────────────────────────

@dataclass
class UserMessage:
    """A single user turn submitted from the input bar."""
    message_id: str           # UUID4, assigned at creation
    text: str                 # Raw submitted text, @mentions not yet resolved
    timestamp: float          # time.monotonic() at submission
    mention_paths: list[str]  # Resolved file/dir paths from @mentions (empty if none)
    state: MessageState = MessageState.COMPLETE  # User messages are always complete immediately
    # Rendered committed lines (produced once, never updated):
    committed_lines: list[str] = field(default_factory=list)

@dataclass
class AssistantMessage:
    """A single agent turn including all tool calls made during the turn."""
    message_id: str           # UUID4, assigned at turn start
    agent_id: str             # Kernel agent ID (e.g. "agent:planner")
    agent_name: str           # Display name (e.g. "agent:planner")
    model_id: str             # LLM model string (e.g. "claude-sonnet-4-6")
    timestamp: float          # time.monotonic() at turn start
    state: MessageState = MessageState.PENDING
    streaming_text: str = ""  # Accumulates during STREAMING; cleared on COMPLETE
    output_lines: list[str] = field(default_factory=list)  # Final rendered lines, committed
    tool_calls: list[ToolCallMessage] = field(default_factory=list)
    error_text: str = ""      # Populated on ERROR state
    tokens: int = 0           # Total tokens for this turn
    cost_usd: float = 0.0     # Cost for this turn
    color_index: int = 0      # 0-5, for parallel agent coloring
    # Header already committed to scrollback (turn header line is committed at PENDING→STREAMING):
    header_committed: bool = False

@dataclass
class SystemMessage:
    """An informational line committed directly to scrollback (not from agent LLM)."""
    message_id: str
    text: str                 # Pre-formatted ANSI or plain text
    timestamp: float
    level: Literal["info", "warning", "error", "debug"] = "info"
    state: MessageState = MessageState.COMPLETE
    committed_lines: list[str] = field(default_factory=list)

@dataclass
class ToolCallMessage:
    """A single tool invocation within an AssistantMessage."""
    tool_id: str              # UUID4 or tool_use_id from LLM
    tool_name: str            # e.g. "read_file"
    args: dict                # Tool arguments as parsed dict
    parent_message_id: str    # AssistantMessage.message_id this belongs to
    state: ToolCallState = ToolCallState.PENDING
    result_summary: str = ""  # e.g. "142 lines", "47 files"
    output_lines: list[str] = field(default_factory=list)  # Full output for /expand
    duration_ms: int = 0      # Set on SUCCESS/FAILURE
    error_message: str = ""   # Set on FAILURE
    diff_text: str = ""       # Set when tool output is a unified diff
    # Committed line for this tool call (set once on SUCCESS/FAILURE):
    committed_line: str = ""

@dataclass
class ErrorMessage:
    """An error that occurred outside normal tool/agent flow (API errors, crashes)."""
    message_id: str
    title: str                # Short label, e.g. "API Error"
    body: str                 # Full error text
    timestamp: float
    source: Literal["tool", "llm", "kernel", "network"] = "kernel"
    recoverable: bool = True
    state: MessageState = MessageState.COMPLETE
    committed_lines: list[str] = field(default_factory=list)
```

### 1.2 Message Lifecycle

#### State Transition Diagram

```
UserMessage:
    (created) ──────────────────────────────────► COMPLETE

AssistantMessage:
    PENDING ──first_token──► STREAMING ──stop_reason──► COMPLETE ──tool_calls_done──► FINALIZED
    PENDING ──cancel──► CANCELLED
    STREAMING ──cancel──► CANCELLED
    STREAMING ──llm_error──► ERROR
    COMPLETE ──(terminal, no tool calls)──► FINALIZED  (immediately)

ToolCallMessage:
    PENDING ──start──► RUNNING ──result──► SUCCESS
                       RUNNING ──error──► FAILURE
                       RUNNING ──approval_required──► APPROVAL_NEEDED
                       APPROVAL_NEEDED ──approved──► RUNNING
                       APPROVAL_NEEDED ──denied──► DENIED

SystemMessage / ErrorMessage:
    (created) ──────────────────────────────────► COMPLETE
```

#### What Triggers Each Transition

| From → To | Trigger | Who triggers |
|-----------|---------|-------------|
| `AssistantMessage` PENDING → STREAMING | First streaming token received | `TUIEventAdapter.on_streaming_token()` |
| `AssistantMessage` STREAMING → COMPLETE | `stop_reason` received from LLM | `TUIEventAdapter.on_turn_complete()` |
| `AssistantMessage` COMPLETE → FINALIZED | All tool calls in the turn are SUCCESS/FAILURE/DENIED | `TranscriptModel._check_finalization()` |
| `AssistantMessage` STREAMING → CANCELLED | SIGINT / Ctrl+C | `TUIEventAdapter.on_turn_cancelled()` |
| `AssistantMessage` STREAMING → ERROR | LLM API error | `TUIEventAdapter.on_turn_error()` |
| `ToolCallMessage` PENDING → RUNNING | `ToolCallStarted` kernel event | `TUIEventAdapter.on_tool_started()` |
| `ToolCallMessage` RUNNING → SUCCESS | `ToolCallComplete` kernel event with `ok=True` | `TUIEventAdapter.on_tool_complete()` |
| `ToolCallMessage` RUNNING → FAILURE | `ToolCallComplete` kernel event with `ok=False` | `TUIEventAdapter.on_tool_complete()` |
| `ToolCallMessage` RUNNING → APPROVAL_NEEDED | `ApprovalRequired` kernel effect | `TUIEventAdapter.on_approval_required()` |
| `ToolCallMessage` APPROVAL_NEEDED → RUNNING | `ApprovalGranted` kernel event | `TUIEventAdapter.on_approval_granted()` |
| `ToolCallMessage` APPROVAL_NEEDED → DENIED | `ApprovalDenied` kernel event | `TUIEventAdapter.on_approval_denied()` |

### 1.3 Streaming Model

#### Token Accumulation

During `STREAMING` state, tokens from the LLM accumulate in `AssistantMessage.streaming_text`. The `TranscriptModel` exposes this via `get_streaming_partial()`. The `FrameComposer` reads this value on each render tick and includes it in the bottom block's streaming zone.

The streaming text is NEVER committed to scrollback line-by-line as it arrives. It lives exclusively in the bottom block until `stop_reason` is received.

```
Token arrival:
  LLM chunk → TUIEventAdapter.on_streaming_token(token: str)
             → TranscriptModel.set_streaming_partial(existing + token)
             → RenderLoop._needs_redraw = True
             [50ms debounce: FrameComposer reads streaming_buffer → bottom block]

No disk write, no committed lines, no ANSI escape sequences emitted to scrollback.
```

#### Partial Message Display (In Live Bottom Block, NOT Committed)

The streaming zone in the bottom block shows the last `MAX_STREAMING_ROWS = 8` wrapped lines of `streaming_text`. It is rendered with dim styling (`\033[2m...\033[0m`) to visually distinguish in-progress text from committed output above.

**Critical invariant:** The streaming zone is ONLY in the bottom block. It does not appear in committed scrollback. When `Terminal.set_bottom(frame)` is called, it erases the previous bottom block (including any previously shown streaming text) and redraws with the new partial text. The scrollback above is never touched.

#### Finalization: Streaming → Committed

When `stop_reason` is received:

1. `TranscriptModel.complete_turn(message_id, final_text)` is called.
2. The `final_text` is rendered to ANSI lines via `_render_markdown_to_lines(final_text, cols)`.
3. These lines are stored in `AssistantMessage.output_lines`.
4. These lines are appended to `_all_committed_lines`.
5. `streaming_text` is cleared (`set_streaming_partial("")`).
6. `RenderLoop.force_commit(new_lines)` is called.
7. `RenderLoop` calls `Terminal.clear_bottom()`, `Terminal.commit_lines(new_lines)`, then immediately calls `Terminal.set_bottom(idle_frame)`.
8. The bottom block redraws with no streaming zone.

#### Buffer Management

- `streaming_text` is a plain Python `str`. It is replaced atomically via `set_streaming_partial(text: str)`. It is never concatenated with `+=` from multiple threads (asyncio is single-threaded, so this is safe).
- Once a turn reaches COMPLETE, `streaming_text` is set to `""` and remains `""` permanently. The final text lives in `output_lines`.
- `_all_committed_lines` is an append-only `list[str]`. It is never modified after a line is appended. The `_committed_cursor` integer tracks how many of these lines have been sent to `Terminal.commit_lines()`.

---

## 2. TranscriptModel Specification

### 2.1 Extended TranscriptModel API

`TranscriptModel` is the single mutable presentation model of the session transcript. It lives in `src/agenthicc/tui/transcript.py`. It is mutated by `TUIEventAdapter` and read by `FrameComposer` and `RenderLoop`.

The existing test suite (`test_transcript.py`, `test_transcript_extended.py`) exercises `append_turn`, `append_line`, `add_tool_call`, `update_tool_call`, `render()`, `diff_lines()`, `advance_spinner()`, `has_running_tools()`. All of these methods MUST be preserved with their current signatures. The new methods are additive.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Generator

import wcwidth

MAX_TURNS_IN_MEMORY: int = 200          # configurable via AgenthiccConfig
MAX_LINES_PER_TURN: int = 500           # tool outputs truncated beyond this
MAX_DIFF_LINES: int = 50                # unified diffs truncated beyond this
MAX_STREAMING_ROWS: int = 8             # bottom block streaming zone height cap
_MD_SENTINEL: str = "\x00MD\x00"       # prefix on lines that contain Markdown

SPINNER_FRAMES: list[str] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
AGENT_COLORS: list[str] = ["35", "36", "33", "34", "32", "31"]  # ANSI color codes


# ─── Core dataclasses (preserved from existing API) ───────────────────────────

class ToolCallState(Enum):
    PENDING         = auto()
    RUNNING         = auto()
    SUCCESS         = auto()
    FAILURE         = auto()
    APPROVAL_NEEDED = auto()
    DENIED          = auto()


class TurnState(Enum):
    PENDING    = auto()
    STREAMING  = auto()
    COMPLETE   = auto()
    FINALIZED  = auto()
    CANCELLED  = auto()
    ERROR      = auto()


@dataclass
class ToolCallEntry:
    tool_use_id: str
    name: str
    args: dict = field(default_factory=dict)
    state: ToolCallState = ToolCallState.RUNNING
    duration_ms: float = 0.0
    error: str = ""
    result_summary: str = ""
    output_lines: list[str] = field(default_factory=list)
    diff_text: str = ""
    committed_line: str = ""  # Set once when tool call reaches terminal state


@dataclass
class AgentTurnEntry:
    agent_id: str
    agent_name: str
    timestamp: float
    state: TurnState = TurnState.STREAMING
    output_lines: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    streaming_text: str = ""    # Partial text during STREAMING; "" after COMPLETE
    color_index: int = 0        # Index into AGENT_COLORS
    cost_usd: float = 0.0       # Populated from kernel AppState on completion
    tokens: int = 0             # Populated from kernel AppState on completion
    header_committed: bool = False  # True once the "● agent:name  HH:MM:SS" line is committed
    _evicted: bool = field(default=False, init=False, repr=False)


# ─── TranscriptModel ──────────────────────────────────────────────────────────

class TranscriptModel:
    """
    Mutable presentation model for the session transcript.

    Single source of truth for:
    - All agent turns (AgentTurnEntry), in submission order
    - The streaming partial text (shown in bottom block, never committed until final)
    - The committed-line list and cursor (append-only; read by RenderLoop)
    - Spinner frame counter (advanced by RenderLoop on each tick)

    Thread safety: all mutations happen on the asyncio event loop (single-threaded).
    No locking required.
    """

    def __init__(self) -> None:
        self._turns: list[AgentTurnEntry] = []
        self._streaming_partial: str = ""       # Current streaming text (NOT committed)
        self._committed_lines: list[str] = []   # Append-only; all lines sent/to-send to scrollback
        self._committed_cursor: int = 0         # How many _committed_lines have been sent
        self._agent_color_map: dict[str, int] = {}
        self._next_color_index: int = 0
        self._spinner_frame: int = 0
        self._cols: int = 80                    # Updated by RenderLoop on each tick; used for Markdown

    # ── Existing API (preserved for backward compatibility) ─────────────────

    def append_turn(
        self,
        agent_id: str,
        agent_name: str,
        timestamp: float | None = None,
    ) -> AgentTurnEntry:
        """
        Start a new agent turn.

        Commits the turn header line ("● agent:name  HH:MM:SS") immediately to
        _committed_lines. The actual content (streaming_text, output_lines) comes later.

        Returns the new AgentTurnEntry.
        """
        if timestamp is None:
            timestamp = time.monotonic()
        if agent_id not in self._agent_color_map:
            self._agent_color_map[agent_id] = self._next_color_index % len(AGENT_COLORS)
            self._next_color_index += 1
        color_index = self._agent_color_map[agent_id]
        turn = AgentTurnEntry(
            agent_id=agent_id,
            agent_name=agent_name,
            timestamp=timestamp,
            color_index=color_index,
        )
        self._turns.append(turn)
        # Commit the header line immediately (it is final — agent name and timestamp never change)
        header = _render_turn_header(turn)
        self._committed_lines.append(header)
        turn.header_committed = True
        if len(self._turns) > MAX_TURNS_IN_MEMORY:
            self._evict_old_turns()
        return turn

    def append_line(self, agent_id: str, text: str) -> None:
        """
        Append a text line to the most-recent turn for agent_id.

        If the text starts with _MD_SENTINEL, it is flagged as Markdown and will
        be rendered via Rich on finalization. Otherwise it is appended as-is.

        This method does NOT commit the line to _committed_lines — lines are only
        committed when the turn is finalized via finalize_turn().
        """
        turn = self._get_turn_for_agent(agent_id)
        if turn is None:
            return
        turn.output_lines.append(text[:MAX_LINES_PER_TURN])

    def add_tool_call(
        self,
        agent_id: str,
        tool_use_id: str,
        name: str,
        args: dict | None = None,
    ) -> ToolCallEntry:
        """
        Register a new tool call for agent_id's current turn.

        Returns the new ToolCallEntry.
        """
        turn = self._get_turn_for_agent(agent_id)
        tc = ToolCallEntry(
            tool_use_id=tool_use_id,
            name=name,
            args=args or {},
            state=ToolCallState.RUNNING,
        )
        if turn is not None:
            turn.tool_calls.append(tc)
        return tc

    def finish_tool_call(
        self,
        tool_use_id: str,
        success: bool,
        duration_ms: float = 0.0,
        result_summary: str = "",
        error: str = "",
        output_lines: list[str] | None = None,
        diff_text: str = "",
    ) -> ToolCallEntry | None:
        """
        Mark a tool call as SUCCESS or FAILURE.

        Generates and stores the committed_line for the tool call.
        Appends the committed_line to _committed_lines.

        Returns the updated ToolCallEntry, or None if not found.
        """
        tc = self._get_tool_call(tool_use_id)
        if tc is None:
            return None
        tc.state = ToolCallState.SUCCESS if success else ToolCallState.FAILURE
        tc.duration_ms = duration_ms
        tc.result_summary = result_summary
        tc.error = error
        if output_lines:
            tc.output_lines = output_lines[:MAX_LINES_PER_TURN]
        if diff_text:
            tc.diff_text = diff_text
        tc.committed_line = _render_tool_call_committed(tc)
        self._committed_lines.append(tc.committed_line)
        return tc

    def update_tool_call(
        self,
        tool_use_id: str,
        state: ToolCallState,
        **kwargs: object,
    ) -> ToolCallEntry | None:
        """
        Update the state and optional fields of a tool call.

        If state is SUCCESS or FAILURE, generates and appends the committed_line.
        Keyword arguments are applied as setattr to the ToolCallEntry.
        """
        tc = self._get_tool_call(tool_use_id)
        if tc is None:
            return None
        tc.state = state
        for k, v in kwargs.items():
            setattr(tc, k, v)
        if state in (ToolCallState.SUCCESS, ToolCallState.FAILURE) and not tc.committed_line:
            tc.committed_line = _render_tool_call_committed(tc)
            self._committed_lines.append(tc.committed_line)
        return tc

    def render(self, finalized_only: bool = False) -> list[str]:
        """
        Render all turns to a list of strings.

        If finalized_only=True, only include turns in COMPLETE/FINALIZED/CANCELLED/ERROR state.
        Used by RenderLoop to check for newly finalized turns.

        Returns raw ANSI-formatted lines suitable for Terminal.commit_lines().
        """
        lines: list[str] = []
        for turn in self._turns:
            if finalized_only and turn.state == TurnState.STREAMING:
                continue
            lines.extend(_render_turn(turn))
        return lines

    def finalized_line_count(self) -> int:
        """Return the number of lines in _committed_lines. Used by RenderLoop."""
        return len(self._committed_lines)

    def advance_spinner(self) -> None:
        """Advance the spinner frame counter. Called by RenderLoop on each tick."""
        self._spinner_frame = (self._spinner_frame + 1) % len(SPINNER_FRAMES)

    def has_running_tools(self) -> bool:
        """Return True if any tool call in any turn is in PENDING or RUNNING state."""
        for turn in self._turns:
            for tc in turn.tool_calls:
                if tc.state in (ToolCallState.PENDING, ToolCallState.RUNNING):
                    return True
        return False

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self._turns)

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens for t in self._turns)

    def diff_lines(self, other: "TranscriptModel") -> list[str]:
        """
        Return lines in self._committed_lines not present in other._committed_lines.

        Used by RenderLoop for differential rendering: lines that have been committed
        to self but not yet sent to Terminal.commit_lines().
        """
        my_count = len(self._committed_lines)
        other_count = len(other._committed_lines)
        if my_count <= other_count:
            return []
        return self._committed_lines[other_count:]

    # ── New methods (additive extensions) ────────────────────────────────────

    def get_streaming_partial(self) -> str | None:
        """
        Return the current streaming partial text, or None if no active streaming.

        None means: no streaming zone should appear in the bottom block.
        "" (empty string) after a turn completes; do not show streaming zone.
        A non-empty string: render in the streaming zone of the bottom block.
        """
        if not self._streaming_partial:
            return None
        return self._streaming_partial

    def set_streaming_partial(self, text: str) -> None:
        """
        Set the streaming partial text.

        Called by TUIEventAdapter on each streaming token. This updates what the
        bottom block's streaming zone shows. Does NOT commit anything to scrollback.
        """
        self._streaming_partial = text

    def clear_streaming_partial(self) -> None:
        """
        Clear the streaming partial text.

        Called by TUIEventAdapter when a turn completes. After this call,
        get_streaming_partial() returns None and the streaming zone disappears.
        """
        self._streaming_partial = ""

    def finalize_turn(
        self,
        agent_id: str,
        final_text: str,
        tokens: int = 0,
        cost_usd: float = 0.0,
        cols: int | None = None,
    ) -> list[str]:
        """
        Finalize the most-recent turn for agent_id.

        1. Renders final_text (Markdown) to ANSI lines at the given column width.
        2. Stores lines in turn.output_lines.
        3. Appends those lines plus a turn separator to _committed_lines.
        4. Sets turn.state = TurnState.COMPLETE.
        5. Clears turn.streaming_text.
        6. Returns the new committed lines (so RenderLoop can call Terminal.commit_lines()).

        The turn header was already committed by append_turn(). Only the body and
        the separator are committed here.
        """
        if cols is None:
            cols = self._cols
        turn = self._get_turn_for_agent(agent_id)
        if turn is None:
            return []
        rendered = _render_markdown_to_lines(final_text, cols)
        rendered = rendered[:MAX_LINES_PER_TURN]
        turn.output_lines = rendered
        turn.tokens = tokens
        turn.cost_usd = cost_usd
        turn.state = TurnState.COMPLETE
        turn.streaming_text = ""
        separator = _render_separator(cols)
        new_lines = rendered + [separator]
        self._committed_lines.extend(new_lines)
        self._check_finalization(turn)
        return new_lines

    def set_turn_error(self, agent_id: str, error_text: str) -> list[str]:
        """
        Mark the most-recent turn for agent_id as ERROR.

        Commits an error indicator line. Returns the new committed lines.
        """
        turn = self._get_turn_for_agent(agent_id)
        if turn is None:
            return []
        turn.state = TurnState.ERROR
        turn.streaming_text = ""
        error_line = f"  \033[31m✗\033[0m {error_text}"
        separator = _render_separator(self._cols)
        new_lines = [error_line, separator]
        self._committed_lines.extend(new_lines)
        return new_lines

    def cancel_turn(self, agent_id: str) -> list[str]:
        """
        Mark the most-recent turn for agent_id as CANCELLED.

        Commits a cancellation indicator. Returns the new committed lines.
        """
        turn = self._get_turn_for_agent(agent_id)
        if turn is None:
            return []
        turn.state = TurnState.CANCELLED
        turn.streaming_text = ""
        cancel_line = "  \033[2m[cancelled]\033[0m"
        separator = _render_separator(self._cols)
        new_lines = [cancel_line, separator]
        self._committed_lines.extend(new_lines)
        return new_lines

    def commit_system_message(self, text: str, level: str = "info") -> list[str]:
        """
        Commit a system-level message directly to scrollback.

        Used for session banners, doom-loop alerts, session resume notices.
        Returns the committed lines.
        """
        if level == "warning":
            line = f"\033[33m⚠\033[0m {text}"
        elif level == "error":
            line = f"\033[31m✗\033[0m {text}"
        else:
            line = f"\033[2m{text}\033[0m"
        self._committed_lines.append(line)
        return [line]

    def commit_diff_block(self, file_path: str, diff_text: str) -> list[str]:
        """
        Commit a unified diff block to scrollback.

        Renders with color coding (added=green, removed=red, hunk=cyan).
        Truncates at MAX_DIFF_LINES. Returns the committed lines.
        """
        lines = _render_diff(file_path, diff_text, self._cols)
        self._committed_lines.extend(lines)
        return lines

    def get_new_committed_lines(self) -> list[str]:
        """
        Return committed lines that have not yet been sent to Terminal.commit_lines().

        Advances _committed_cursor. Called by RenderLoop before each set_bottom() call.
        """
        new = self._committed_lines[self._committed_cursor:]
        self._committed_cursor = len(self._committed_lines)
        return new

    def peek_new_committed_lines(self) -> list[str]:
        """
        Return committed lines not yet sent, WITHOUT advancing the cursor.

        Used by FrameComposer for rendering decisions without consuming state.
        """
        return self._committed_lines[self._committed_cursor:]

    def evict_old_turns(self, keep_last: int = 200) -> int:
        """
        Public entry point for memory eviction.

        Clears output_lines and tool_calls[*].output_lines for turns older than
        keep_last. Turn metadata (agent_id, agent_name, timestamp, state, header)
        is NEVER evicted. Returns number of turns evicted.
        """
        if len(self._turns) <= keep_last:
            return 0
        evict_before = len(self._turns) - keep_last
        count = 0
        for turn in self._turns[:evict_before]:
            if not turn._evicted:
                turn.output_lines = []
                for tc in turn.tool_calls:
                    tc.output_lines = []
                turn._evicted = True
                count += 1
        return count

    def update_cols(self, cols: int) -> None:
        """
        Update the column width used for rendering.

        Called by RenderLoop after SIGWINCH. Affects future Markdown renders only;
        already-committed lines are not re-rendered.
        """
        self._cols = cols

    @property
    def spinner_frame(self) -> int:
        return self._spinner_frame

    @property
    def committed_cursor(self) -> int:
        return self._committed_cursor

    @property
    def all_committed_lines(self) -> list[str]:
        """Read-only view of all committed lines. Do not mutate."""
        return self._committed_lines

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_turn_for_agent(self, agent_id: str) -> AgentTurnEntry | None:
        """Return the most-recent turn for agent_id, searching from the end."""
        for turn in reversed(self._turns):
            if turn.agent_id == agent_id:
                return turn
        return None

    def _get_tool_call(self, tool_use_id: str) -> ToolCallEntry | None:
        """Find a ToolCallEntry by tool_use_id, searching newest turns first."""
        for turn in reversed(self._turns):
            for tc in turn.tool_calls:
                if tc.tool_use_id == tool_use_id:
                    return tc
        return None

    def _evict_old_turns(self) -> None:
        """Internal eviction called automatically when MAX_TURNS_IN_MEMORY is exceeded."""
        self.evict_old_turns(keep_last=MAX_TURNS_IN_MEMORY)

    def _check_finalization(self, turn: AgentTurnEntry) -> None:
        """
        Promote a COMPLETE turn to FINALIZED if all its tool calls are terminal.

        A tool call is terminal when its state is SUCCESS, FAILURE, or DENIED.
        """
        if turn.state != TurnState.COMPLETE:
            return
        if all(
            tc.state in (ToolCallState.SUCCESS, ToolCallState.FAILURE, ToolCallState.DENIED)
            for tc in turn.tool_calls
        ):
            turn.state = TurnState.FINALIZED
```

### 2.2 Committed vs. Partial Content Rules

The following rules are absolute and must never be violated:

**Content that is COMMITTED (goes to scrollback via Terminal.commit_lines(), never erased):**

| Content | When committed | Method |
|---------|---------------|---------|
| Turn header line (`● agent:name  HH:MM:SS`) | Immediately on `append_turn()` | `append_turn()` → `_committed_lines.append(header)` |
| Tool call committed line (`  ⎿ tool_name(...)  ✓ ...`) | When tool call reaches SUCCESS or FAILURE | `finish_tool_call()` or `update_tool_call()` |
| Agent response body (Markdown rendered to ANSI) | When `finalize_turn()` is called | `finalize_turn()` → `_committed_lines.extend(lines)` |
| Turn separator (`─` × cols) | At end of `finalize_turn()`, `set_turn_error()`, `cancel_turn()` | Appended after body lines |
| System messages (session banners, doom-loop alerts) | Immediately | `commit_system_message()` |
| Diff blocks (proposed changes pre-approval) | Before approval gate is shown | `commit_diff_block()` |
| Cancellation indicator (`[cancelled]`) | On `cancel_turn()` | `cancel_turn()` |
| Error indicator line | On `set_turn_error()` | `set_turn_error()` |

**Content that is PARTIAL (in live bottom block, updated in-place, never written to scrollback):**

| Content | Where displayed | How cleared |
|---------|----------------|-------------|
| Streaming LLM text (`streaming_partial`) | Bottom block streaming zone (rows above status bar) | `clear_streaming_partial()` on turn complete |
| Tool call spinner (`⎿ tool_name(...)  ⠙`) | Bottom block streaming zone | Disappears when `finish_tool_call()` commits the final line |
| Approval gate rows (`⚠ tool(...)  — approve?`) | Bottom block replaces input bar | Clears when user responds |
| Thinking indicator (no text yet) | Bottom block streaming zone | Disappears on first token |

**The single key rule:** `_committed_lines` is strictly append-only. Once a line is appended, it is permanent. `_committed_cursor` only moves forward. `Terminal.commit_lines()` is called exactly once per batch of new lines.

---

## 3. Incremental Rendering

### 3.1 FrameComposer Integration

`FrameComposer.compose()` is a pure function that takes `TranscriptModel`, `InputState`, and `Size` and returns a `Frame`. It reads (but never mutates) these inputs.

The `finalized_only` distinction maps directly to the two content classes:

- `transcript.get_streaming_partial()` → bottom block streaming zone (partial)
- `transcript.get_new_committed_lines()` → queued for `Terminal.commit_lines()` (committed, permanent)

```python
# In RenderLoop._do_render():
def _do_render(self) -> None:
    size = self._terminal.size

    # Step 1: Flush any newly committed lines to scrollback
    new_lines = self._transcript.get_new_committed_lines()
    if new_lines:
        self._terminal.clear_bottom()
        self._terminal.commit_lines(new_lines)
        self._last_frame = None  # force bottom block redraw

    # Step 2: Compose and redraw the bottom block
    frame = self._composer.compose(
        self._transcript,
        self._input_state,
        size,
    )
    if frame != self._last_frame:
        self._terminal.set_bottom(frame)
        self._last_frame = frame
```

The `FrameComposer` accesses transcript data via:

```python
# In FrameComposer.compose():
partial = transcript.get_streaming_partial()  # None or str
spinner = transcript.spinner_frame            # int, for live tool call spinners
# FrameComposer does NOT call get_new_committed_lines() — that is RenderLoop's job.
```

### 3.2 Rendering Pipeline

#### User Turn → Render Sequence

```
1. User submits message → InputState.submit() called
2. "USER  HH:MM:SS" header line + message text committed to scrollback via:
       transcript.commit_system_message(f"> {text}")
   (or structured as a UserMessage committed_lines block)
3. ConversationDivider line committed:
       transcript.commit_system_message(f"─── Turn {n} ───  HH:MM:SS")
4. RenderLoop.request_redraw() → bottom block redraws (input cleared)
5. Agent runner starts; transcript.append_turn(...) commits the turn header
6. Bottom block now shows: status bar (THINKING), divider, disabled input bar
```

#### Assistant Turn Streaming → Partial → Committed Sequence

```
1. append_turn(agent_id, agent_name) → header line committed immediately
2. First token arrives → set_streaming_partial(token) + RenderLoop._needs_redraw = True
3. [50ms tick]: FrameComposer reads get_streaming_partial() → streaming zone rendered
   in bottom block. No scrollback change.
4. [tokens keep arriving]: set_streaming_partial(accumulated_text) on each chunk
5. [stop_reason received]:
   a. final_text = current streaming_partial
   b. clear_streaming_partial()
   c. finalize_turn(agent_id, final_text, tokens, cost_usd)
      → renders Markdown → appends body lines + separator to _committed_lines
   d. RenderLoop.force_commit():
      - get_new_committed_lines() returns all the body lines + separator
      - Terminal.clear_bottom() erases streaming zone
      - Terminal.commit_lines(new_lines) → permanent in scrollback
      - Terminal.set_bottom(idle_frame) → bottom block redraws without streaming zone
6. Session is back in idle state.
```

#### Tool Call → Running → Complete → Committed Sequence

```
1. ToolCallStarted kernel event:
   a. add_tool_call(agent_id, tool_use_id, name, args)
   b. The tool call is in RUNNING state; its spinner is visible in bottom block via:
      FrameComposer reads turns[-1].tool_calls and renders running ones in streaming zone
2. [Bottom block on each tick]: streaming zone shows:
      "  ⎿ tool_name(args)  ⠙"  (spinner animates via spinner_frame)
3. ToolCallComplete kernel event (success):
   a. finish_tool_call(tool_use_id, success=True, duration_ms=..., result_summary=...)
      → committed_line = "  ⎿ tool_name(args)  ✓ 142 lines (0.3s)"
      → _committed_lines.append(committed_line)
   b. RenderLoop flushes the new committed line to Terminal.commit_lines()
   c. Bottom block streaming zone no longer shows this tool call
4. If tool call had output (diff, file content):
   a. commit_diff_block() or commit_system_message() appends those lines too
   b. Flushed in the same batch as the committed_line
```

### 3.3 Markdown Rendering

Assistant text is rendered to ANSI via Rich's `Markdown` class. This happens once, at `finalize_turn()` time (not during streaming).

```python
import io
from rich.console import Console
from rich.markdown import Markdown as RichMarkdown

_MD_SENTINEL = "\x00MD\x00"


def _render_markdown_to_lines(text: str, cols: int) -> list[str]:
    """
    Render Markdown text to a list of ANSI-formatted strings.

    Always passes force_terminal=True and explicit width to Rich so that
    it never queries stdout TTY status.

    Returns lines WITHOUT trailing newlines.
    Trailing blank lines are stripped.
    """
    buf = io.StringIO()
    console = Console(
        file=buf,
        width=cols,
        highlight=False,
        markup=False,          # text is Markdown, not Rich markup
        force_terminal=True,   # prevents Rich from stripping ANSI in non-TTY envs
        no_color=False,
    )
    console.print(RichMarkdown(text))
    raw = buf.getvalue()
    lines = raw.splitlines()
    # Strip trailing blank lines that Rich appends
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _strip_md_sentinel(line: str) -> tuple[bool, str]:
    """Return (is_markdown, text_without_sentinel)."""
    if line.startswith(_MD_SENTINEL):
        return True, line[len(_MD_SENTINEL):]
    return False, line
```

**Width constraints:**
- `cols` is always `TranscriptModel._cols`, which is updated via `update_cols(cols)` by RenderLoop after each SIGWINCH.
- Markdown is rendered to `cols` wide. Lines exceeding `cols` display chars are clipped via `wcwidth.wcswidth()` before committing (handled by `Terminal._write_atomic`).
- If `cols < 40`, Markdown is rendered as plain text (pass `markup=False` and skip `RichMarkdown`).

**_MD_SENTINEL handling:**
- Lines prefixed with `_MD_SENTINEL` in `output_lines` are treated as Markdown source. They are rendered via `_render_markdown_to_lines()` at commit time.
- Lines without the sentinel are committed as-is.
- In practice, `finalize_turn()` receives the raw accumulated text (not pre-split lines), renders it in one pass, and stores the rendered lines. The sentinel is used when `append_line()` is called directly with Markdown content that needs deferred rendering.

---

## 4. Long Session Support

### 4.1 Memory Management

```python
# Constants (top of transcript.py):
MAX_TURNS_IN_MEMORY: int = 200   # eviction threshold; configurable via AgenthiccConfig.tui.max_turns
MAX_LINES_PER_TURN: int = 500    # max lines stored in AgentTurnEntry.output_lines
MAX_DIFF_LINES: int = 50         # diff blocks truncated to this many lines
MAX_TOOL_OUTPUT_LINES: int = 100 # tool output_lines capped at this
```

**Eviction Strategy:**

`_evict_old_turns()` is called automatically by `add_turn()` when `len(self._turns) > MAX_TURNS_IN_MEMORY`. It is also callable externally via `evict_old_turns(keep_last=200)`.

What the eviction does:
1. Identifies turns with index `< len(turns) - keep_last` (oldest turns).
2. For each such turn, sets `turn.output_lines = []` and `tc.output_lines = []` for all tool calls.
3. Sets `turn._evicted = True` to prevent double-eviction.
4. Does NOT clear: `turn.agent_id`, `turn.agent_name`, `turn.timestamp`, `turn.state`, `turn.color_index`, `turn.tool_calls[*].tool_use_id`, `turn.tool_calls[*].name`, `turn.tool_calls[*].committed_line`.

**What is NEVER evicted:**
- Turn headers (already in scrollback as `_committed_lines` entries; Python holds only the string reference)
- Tool call committed_line (already in scrollback)
- Turn metadata: agent_id, agent_name, timestamp, state
- `_committed_lines` list (the strings are already committed to scrollback; the Python list entries are small string objects, not copies of the full output)

**Memory budget validation:**

At 200 turns × 500 lines × ~80 chars/line = ~8MB for output_lines. After eviction of turns older than the retention window, only the most recent 200 turns' output is live in Python memory. The `_committed_lines` list holds references to the same strings but does not duplicate memory.

Total expected RSS for transcript data at 200 turns: < 10MB.

### 4.2 Session Recovery

**What to persist:** The kernel's `events.jsonl` contains all state. `ConversationStore` (existing module at `src/agenthicc/conversation_store.py`) handles SQLite persistence. The `TranscriptModel` is rebuilt from the event log on resume.

**Persisted per turn (via ConversationStore):**
```python
@dataclass
class PersistedTurn:
    turn_id: str
    agent_id: str
    agent_name: str
    timestamp: float
    state: str                  # TurnState.name
    final_text: str             # full Markdown text (not rendered lines)
    tool_calls: list[dict]      # serialized ToolCallEntry dicts
    tokens: int
    cost_usd: float
```

**Resume protocol:**

1. `EventProcessor.restore_from_log()` replays events.jsonl → AppState is restored.
2. `TranscriptModel.replay_from_store(conv_store, session_id, last_n=20)` is called.
3. For each of the last 20 stored turns:
   a. `append_turn(agent_id, agent_name, timestamp)` — commits header to `_committed_lines`
   b. `finalize_turn(agent_id, final_text, tokens, cost_usd)` — commits body + separator
   c. For each tool call: `finish_tool_call(...)` — commits the tool call line
4. `RenderLoop.force_commit()` sends all `_committed_lines` to scrollback via `Terminal.commit_lines()`.
5. The user sees the last 20 turns reprinted to their new terminal window.

```python
def replay_from_store(
    self,
    conv_store: "ConversationStore",
    session_id: str,
    last_n: int = 20,
    cols: int = 80,
) -> None:
    """
    Populate TranscriptModel from persisted session data.

    Called on --resume. Reprints the last last_n turns to scrollback.
    """
    self._cols = cols
    turns = conv_store.load_turns(session_id)[-last_n:]
    # Commit a resume banner first
    self.commit_system_message(
        f"── resumed session {session_id[:8]} · {len(turns)} turns ──",
        level="info",
    )
    for t in turns:
        entry = self.append_turn(t["agent_id"], t["agent_name"], t["timestamp"])
        self.finalize_turn(
            t["agent_id"],
            t.get("final_text", ""),
            tokens=t.get("tokens", 0),
            cost_usd=t.get("cost_usd", 0.0),
            cols=cols,
        )
        for tc_dict in t.get("tool_calls", []):
            self.add_tool_call(
                t["agent_id"],
                tc_dict["tool_use_id"],
                tc_dict["name"],
                tc_dict.get("args", {}),
            )
            self.finish_tool_call(
                tc_dict["tool_use_id"],
                success=(tc_dict.get("state") == "SUCCESS"),
                duration_ms=tc_dict.get("duration_ms", 0),
                result_summary=tc_dict.get("result_summary", ""),
                error=tc_dict.get("error", ""),
            )
```

---

## 5. Scroll Behavior

The scroll behavior is implemented entirely by the terminal emulator and is not controlled by the application. The application commits lines to scrollback permanently; the user scrolls using the terminal's native scroll mechanism (mouse wheel, Shift+PageUp, tmux copy mode, etc.).

**Anchor-to-bottom during streaming:**

During an active agent turn (streaming or tool calls running), the application's output continuously appends new committed lines to scrollback. The terminal's natural behavior (auto-scroll on new output) keeps the view at the bottom, showing the latest content.

However, if the user scrolls up while streaming is in progress, the terminal stops auto-scrolling (this is native terminal behavior — the application cannot detect it and does not need to). The streaming continues normally. The committed lines continue accumulating in scrollback. When the user scrolls back down (to the bottom), they see the latest content and the bottom block.

**Manual scroll up releases anchor:**

Because the bottom block is a fixed set of terminal rows at the current cursor position (below all committed content), scrolling up in the terminal's scrollback moves the visible viewport away from the bottom block. The bottom block remains at the bottom of the terminal but is below the visible viewport when scrolled up. This is correct behavior. The application does not interfere.

**Re-engagement on new user input:**

When the user types a new message (presses Enter), `Terminal.commit_lines()` appends the user's message to scrollback and then redraws the bottom block at the new bottom. On most terminal emulators, printing new content to stdout automatically scrolls the viewport back to the bottom (this is the terminal's default scroll-on-output behavior). On terminals where this does not happen, the user can press End or Ctrl+End to jump to the bottom of scrollback.

The application does NOT implement explicit scroll-to-bottom commands because there is no standard ANSI sequence for "scroll terminal scrollback to the bottom" that works across all emulators.

---

## 6. Full Test Specification

### Unit Tests (40+ enumerated)

All unit tests are in `tests/unit/test_transcript.py` and `tests/unit/test_transcript_extended.py`. Mark all tests with `@pytest.mark.unit`. No asyncio required for these tests (all methods are synchronous).

#### Group 1: append_turn and Turn Headers

```
test_01_append_turn_returns_agent_turn_entry
  Component: TranscriptModel.append_turn
  Input: agent_id="a1", agent_name="agent:test", timestamp=100.0
  Expected: returns AgentTurnEntry with correct fields
  Edge case: none

test_02_append_turn_commits_header_immediately
  Component: TranscriptModel.append_turn
  Input: agent_id="a1", agent_name="agent:worker"
  Expected: len(model._committed_lines) == 1 after call; line contains "agent:worker"
  Edge case: called before any other method

test_03_append_turn_assigns_distinct_colors_to_different_agents
  Component: TranscriptModel.append_turn
  Input: append_turn for agent "a1", then "a2", then "a3"
  Expected: each AgentTurnEntry has a different color_index
  Edge case: 7 agents → color_index wraps mod 6

test_04_append_turn_assigns_same_color_to_same_agent
  Component: TranscriptModel.append_turn
  Input: append_turn("a1", ...) twice
  Expected: both turns have the same color_index
  Edge case: none

test_05_append_turn_sets_header_committed_true
  Component: TranscriptModel.append_turn
  Input: append_turn("a1", "agent:test")
  Expected: turn.header_committed is True
  Edge case: none

test_06_append_turn_triggers_eviction_at_limit
  Component: TranscriptModel.append_turn, _evict_old_turns
  Input: append MAX_TURNS_IN_MEMORY + 1 turns
  Expected: oldest turn has output_lines == [] and _evicted == True
  Edge case: exactly MAX_TURNS_IN_MEMORY turns → no eviction
```

#### Group 2: append_line and output_lines

```
test_07_append_line_adds_to_current_turn
  Component: TranscriptModel.append_line
  Input: append_turn("a1", "test"), append_line("a1", "hello")
  Expected: turns[-1].output_lines == ["hello"]
  Edge case: none

test_08_append_line_does_not_commit
  Component: TranscriptModel.append_line
  Input: append_turn, then append_line
  Expected: len(_committed_lines) == 1 (only header); append_line does not add
  Edge case: none

test_09_append_line_targets_most_recent_turn_for_agent
  Component: TranscriptModel.append_line
  Input: append_turn("a1"), append_turn("a2"), append_line("a1", "x")
  Expected: only a1's turn has "x" in output_lines
  Edge case: agent has multiple turns

test_10_append_line_truncates_at_max_lines_per_turn
  Component: TranscriptModel.append_line
  Input: append MAX_LINES_PER_TURN + 50 lines to same agent
  Expected: output_lines has exactly MAX_LINES_PER_TURN entries
  Edge case: exactly at limit → not truncated; one over → truncated
```

#### Group 3: add_tool_call and finish_tool_call

```
test_11_add_tool_call_returns_tool_call_entry
  Component: TranscriptModel.add_tool_call
  Input: add_tool_call("a1", "tc1", "read_file", {"path": "x"})
  Expected: returns ToolCallEntry with name="read_file", state=RUNNING
  Edge case: none

test_12_add_tool_call_attaches_to_current_turn
  Component: TranscriptModel.add_tool_call
  Input: append_turn("a1"), add_tool_call("a1", "tc1", "read_file")
  Expected: turns[-1].tool_calls has one entry with tool_use_id="tc1"
  Edge case: none

test_13_finish_tool_call_success_commits_checkmark_line
  Component: TranscriptModel.finish_tool_call
  Input: add_tool_call then finish_tool_call(..., success=True, result_summary="42 lines")
  Expected: new entry in _committed_lines contains "✓" and "42 lines"
  Edge case: none

test_14_finish_tool_call_failure_commits_cross_line
  Component: TranscriptModel.finish_tool_call
  Input: finish_tool_call(..., success=False, error="file not found")
  Expected: new entry in _committed_lines contains "✗" and "file not found"
  Edge case: none

test_15_finish_tool_call_sets_committed_line_on_entry
  Component: TranscriptModel.finish_tool_call
  Input: finish_tool_call(success=True)
  Expected: tc.committed_line is a non-empty string
  Edge case: none

test_16_finish_tool_call_unknown_id_returns_none
  Component: TranscriptModel.finish_tool_call
  Input: finish_tool_call("nonexistent-id", ...)
  Expected: returns None; _committed_lines unchanged
  Edge case: empty model
```

#### Group 4: update_tool_call

```
test_17_update_tool_call_changes_state
  Component: TranscriptModel.update_tool_call
  Input: update_tool_call("tc1", ToolCallState.SUCCESS, duration_ms=99)
  Expected: tc.state == SUCCESS, tc.duration_ms == 99
  Edge case: none

test_18_update_tool_call_success_commits_line
  Component: TranscriptModel.update_tool_call
  Input: update_tool_call("tc1", ToolCallState.SUCCESS)
  Expected: _committed_lines grows by 1
  Edge case: calling SUCCESS twice → second call does NOT commit a duplicate

test_19_update_tool_call_approval_needed_no_commit
  Component: TranscriptModel.update_tool_call
  Input: update_tool_call("tc1", ToolCallState.APPROVAL_NEEDED)
  Expected: tc.state == APPROVAL_NEEDED; no new _committed_lines entry
  Edge case: none
```

#### Group 5: streaming partial

```
test_20_set_streaming_partial_stores_text
  Component: TranscriptModel.set_streaming_partial
  Input: set_streaming_partial("hello")
  Expected: get_streaming_partial() == "hello"
  Edge case: empty string → get_streaming_partial() returns None

test_21_clear_streaming_partial_returns_none
  Component: TranscriptModel.clear_streaming_partial
  Input: set_streaming_partial("text"); clear_streaming_partial()
  Expected: get_streaming_partial() is None
  Edge case: calling clear when already empty is safe

test_22_streaming_partial_not_in_committed_lines
  Component: TranscriptModel.set_streaming_partial
  Input: set_streaming_partial("this should not be committed")
  Expected: "this should not be committed" not in _committed_lines
  Edge case: none
```

#### Group 6: finalize_turn

```
test_23_finalize_turn_commits_body_and_separator
  Component: TranscriptModel.finalize_turn
  Input: append_turn("a1", "test"); finalize_turn("a1", "hello world", cols=80)
  Expected: _committed_lines contains rendered "hello world" text and separator "─" line
  Edge case: none

test_24_finalize_turn_sets_state_complete
  Component: TranscriptModel.finalize_turn
  Input: append_turn; finalize_turn
  Expected: turns[-1].state == TurnState.COMPLETE
  Edge case: none

test_25_finalize_turn_clears_streaming_text
  Component: TranscriptModel.finalize_turn
  Input: set_streaming_partial("partial"); finalize_turn(...)
  Expected: get_streaming_partial() is None after finalize_turn
  Edge case: none

test_26_finalize_turn_returns_new_committed_lines
  Component: TranscriptModel.finalize_turn
  Input: append_turn; finalize_turn("a1", "hello", cols=80)
  Expected: return value is a list[str] with at least 1 element
  Edge case: empty final_text → returns just the separator line

test_27_finalize_turn_truncates_at_max_lines_per_turn
  Component: TranscriptModel.finalize_turn
  Input: finalize_turn with text that renders to > MAX_LINES_PER_TURN lines
  Expected: turn.output_lines has at most MAX_LINES_PER_TURN entries
  Edge case: none

test_28_finalize_turn_promotes_to_finalized_when_no_tool_calls
  Component: TranscriptModel.finalize_turn, _check_finalization
  Input: append_turn (no tool calls); finalize_turn
  Expected: turns[-1].state == TurnState.FINALIZED immediately
  Edge case: none

test_29_finalize_turn_stays_complete_when_tool_calls_pending
  Component: TranscriptModel.finalize_turn, _check_finalization
  Input: append_turn; add_tool_call; finalize_turn (tool still RUNNING)
  Expected: turns[-1].state == TurnState.COMPLETE (not FINALIZED yet)
  Edge case: none
```

#### Group 7: get_new_committed_lines and cursor

```
test_30_get_new_committed_lines_returns_since_cursor
  Component: TranscriptModel.get_new_committed_lines
  Input: append_turn → 1 header committed; call get_new_committed_lines
  Expected: returns ["● agent:test  ..."] (the 1 header line); cursor advances to 1
  Edge case: none

test_31_get_new_committed_lines_advances_cursor
  Component: TranscriptModel.get_new_committed_lines
  Input: append_turn; get_new_committed_lines(); append_turn (second); get_new_committed_lines()
  Expected: first call returns 1 line; second call returns exactly 1 new line
  Edge case: none

test_32_peek_new_committed_lines_does_not_advance_cursor
  Component: TranscriptModel.peek_new_committed_lines
  Input: append_turn; peek_new_committed_lines(); peek_new_committed_lines()
  Expected: both calls return the same 1-element list; cursor remains 0
  Edge case: none

test_33_finalized_line_count_equals_len_committed_lines
  Component: TranscriptModel.finalized_line_count
  Input: append_turn; finalize_turn
  Expected: finalized_line_count() == len(_committed_lines)
  Edge case: initial state → 0
```

#### Group 8: eviction

```
test_34_evict_old_turns_clears_output_lines
  Component: TranscriptModel.evict_old_turns
  Input: create 5 turns with output_lines; evict_old_turns(keep_last=3)
  Expected: first 2 turns have output_lines == []; last 3 are unchanged
  Edge case: none

test_35_evict_old_turns_preserves_metadata
  Component: TranscriptModel.evict_old_turns
  Input: evict a turn
  Expected: agent_id, agent_name, timestamp, state, color_index all preserved
  Edge case: none

test_36_evict_old_turns_clears_tool_call_output_lines
  Component: TranscriptModel.evict_old_turns
  Input: turn with tool calls having output_lines; evict
  Expected: tc.output_lines == [] for evicted turns; tc.committed_line preserved
  Edge case: none

test_37_evict_old_turns_below_threshold_returns_zero
  Component: TranscriptModel.evict_old_turns
  Input: 3 turns; evict_old_turns(keep_last=200)
  Expected: returns 0; no turns modified
  Edge case: none

test_38_evict_old_turns_not_double_evicted
  Component: TranscriptModel.evict_old_turns
  Input: evict once; call evict again
  Expected: returns 0 on second call (already evicted turns skipped)
  Edge case: none
```

#### Group 9: render() and diff_lines()

```
test_39_render_contains_agent_header
  Component: TranscriptModel.render
  Input: append_turn("a1", "agent:test")
  Expected: render() contains a line with "●" and "agent:test"
  Edge case: none

test_40_render_finalized_only_skips_streaming_turns
  Component: TranscriptModel.render(finalized_only=True)
  Input: append_turn (STREAMING state); finalize_turn → COMPLETE
  Expected: with finalized_only=False: both appear; True: only the finalized one
  Edge case: none

test_41_diff_lines_returns_new_since_snapshot
  Component: TranscriptModel.diff_lines
  Input: model1 = TranscriptModel(); append_turn; snapshot_count = len(model1._committed_lines);
         another append → diff_lines(model with old count)
  Expected: returns exactly the new lines added after the snapshot
  Edge case: none

test_42_advance_spinner_cycles_modulo_frames
  Component: TranscriptModel.advance_spinner
  Input: call advance_spinner len(SPINNER_FRAMES) times
  Expected: spinner_frame returns to 0
  Edge case: none

test_43_has_running_tools_true_when_any_running
  Component: TranscriptModel.has_running_tools
  Input: add_tool_call (state=RUNNING); has_running_tools()
  Expected: True
  Edge case: none

test_44_has_running_tools_false_after_all_complete
  Component: TranscriptModel.has_running_tools
  Input: add_tool_call; finish_tool_call(success=True); has_running_tools()
  Expected: False
  Edge case: none

test_45_total_cost_usd_sums_all_turns
  Component: TranscriptModel.total_cost_usd
  Input: two turns each with cost_usd=0.001
  Expected: total_cost_usd == 0.002
  Edge case: none

test_46_total_tokens_sums_all_turns
  Component: TranscriptModel.total_tokens
  Input: two turns with tokens=100 and 200
  Expected: total_tokens == 300
  Edge case: none
```

#### Group 10: system messages and diff blocks

```
test_47_commit_system_message_info_appends_line
  Component: TranscriptModel.commit_system_message
  Input: commit_system_message("session started", level="info")
  Expected: _committed_lines has one new entry; returns that line
  Edge case: none

test_48_commit_system_message_warning_contains_warning_symbol
  Component: TranscriptModel.commit_system_message
  Input: commit_system_message("watch out", level="warning")
  Expected: committed line contains "⚠"
  Edge case: none

test_49_commit_diff_block_respects_max_diff_lines
  Component: TranscriptModel.commit_diff_block
  Input: diff_text with 100 lines (50 + / 50 -)
  Expected: committed lines count does not exceed MAX_DIFF_LINES + overhead (header/footer)
  Edge case: none

test_50_set_turn_error_marks_state_error
  Component: TranscriptModel.set_turn_error
  Input: append_turn; set_turn_error("a1", "API failed")
  Expected: turns[-1].state == TurnState.ERROR; "API failed" in _committed_lines
  Edge case: none

test_51_cancel_turn_marks_state_cancelled
  Component: TranscriptModel.cancel_turn
  Input: append_turn; cancel_turn("a1")
  Expected: turns[-1].state == TurnState.CANCELLED; "[cancelled]" in some committed line
  Edge case: none
```

### Integration Tests (15+ enumerated)

All integration tests are in `tests/integration/test_tui_rendering.py`. Mark with `@pytest.mark.integration`.

```
itest_01_full_turn_lifecycle_header_then_body_committed
  Components: TranscriptModel, FakeTerminal, RenderLoop
  Sequence:
    1. append_turn("a1", "agent:main") → header committed
    2. set_streaming_partial("thinking...") → bottom block shows partial
    3. finalize_turn("a1", "# Result\nFixed it.") → body committed
    4. get_new_committed_lines() returns body lines
  Expected:
    - FakeTerminal.committed_lines contains header, rendered markdown, separator
    - FakeTerminal.committed_lines does NOT contain "thinking..."
  Edge cases: none

itest_02_tool_call_line_committed_after_finish
  Components: TranscriptModel, RenderLoop, FakeTerminal
  Sequence:
    1. append_turn("a1", "agent")
    2. add_tool_call("a1", "tc1", "read_file", {"path": "x.py"})
    3. finish_tool_call("tc1", success=True, result_summary="42 lines", duration_ms=15)
    4. get_new_committed_lines()
  Expected:
    - Committed lines include a line with "read_file" and "✓" and "42 lines"
    - That line is NOT duplicated
  Edge cases: none

itest_03_streaming_not_in_committed_lines
  Components: TranscriptModel
  Sequence:
    1. append_turn("a1", "agent")
    2. set_streaming_partial("partial text abc")
    3. Check _committed_lines
  Expected: "partial text abc" not in any _committed_lines entry
  Edge cases: none

itest_04_render_loop_batches_new_lines_in_single_call
  Components: RenderLoop, FakeTerminal, TranscriptModel
  Sequence:
    1. append_turn + finalize_turn (produces N lines)
    2. RenderLoop._do_render()
  Expected:
    - FakeTerminal.committed_lines has all N lines
    - FakeTerminal.write_call_count incremented by exactly 1 (commit) + 1 (set_bottom) = 2
  Edge cases: 0 new lines → write_call_count unchanged

itest_05_eviction_does_not_affect_committed_scrollback
  Components: TranscriptModel
  Sequence:
    1. Append 210 turns, each finalized with output_lines
    2. evict_old_turns triggers at turn 201+
    3. Check _committed_lines length and committed_cursor
  Expected:
    - _committed_lines still has all lines from all 210 turns (they are committed strings)
    - Turns 0-9 have output_lines == []
    - Turns 10+ still have output_lines
  Edge cases: none

itest_06_resume_replay_prints_last_n_turns
  Components: TranscriptModel.replay_from_store, FakeTerminal, RenderLoop
  Sequence:
    1. Create ConversationStore mock with 30 stored turns
    2. replay_from_store(conv_store, session_id, last_n=20)
    3. Flush to FakeTerminal
  Expected:
    - FakeTerminal.committed_lines contains content from last 20 turns
    - First committed line contains "resumed session"
    - Exactly 20 turn headers in committed lines (not 30)
  Edge cases: fewer than last_n turns stored → replay all available

itest_07_diff_block_truncated_at_max_diff_lines
  Components: TranscriptModel.commit_diff_block
  Sequence:
    1. commit_diff_block("x.py", diff_text_with_200_lines)
    2. Check _committed_lines for diff content
  Expected:
    - Lines from diff in _committed_lines count <= MAX_DIFF_LINES + 3 (header + truncation msg + footer)
    - Truncation indicator present ("... N more lines")
  Edge cases: diff shorter than MAX_DIFF_LINES → not truncated

itest_08_multiple_agents_color_coded_in_headers
  Components: TranscriptModel
  Sequence:
    1. append_turn("a1", "agent:one")
    2. append_turn("a2", "agent:two")
    3. append_turn("a3", "agent:three")
    4. Check header lines in _committed_lines
  Expected:
    - Each header line contains a different ANSI color code
    - Colors cycle from AGENT_COLORS list
  Edge cases: none

itest_09_approval_needed_tool_call_not_committed_until_resolved
  Components: TranscriptModel
  Sequence:
    1. add_tool_call("a1", "tc1", "write_file")
    2. update_tool_call("tc1", ToolCallState.APPROVAL_NEEDED)
    3. Check _committed_lines
  Expected: No committed line for "write_file" yet
  Sequence continues:
    4. update_tool_call("tc1", ToolCallState.SUCCESS)
  Expected: Now a committed line for "write_file" with "✓" appears
  Edge cases: none

itest_10_sigwinch_updates_cols_for_future_renders
  Components: TranscriptModel.update_cols
  Sequence:
    1. Set cols=80; append_turn; finalize_turn with long text
    2. update_cols(40)
    3. append_turn (second); finalize_turn with same text
  Expected:
    - First turn's body lines are wrapped for 80 cols
    - Second turn's body lines are wrapped for 40 cols
  Edge cases: none

itest_11_no_alternate_screen_in_terminal_output
  Components: Terminal, RenderLoop, TranscriptModel
  Sequence: Full render session with multiple turns
  Expected:
    - Raw ANSI bytes written to output contain no "\x1b[?1049h" (smcup)
    - Raw ANSI bytes contain no "\x1b[?1049l" (rmcup)
    - Raw ANSI bytes contain no "\x1b[\d+;\d+r" (DECSTBM scroll region)
  Edge cases: none

itest_12_pyte_confirms_committed_lines_in_scrollback
  Components: Terminal, RenderLoop, TranscriptModel, pyte
  Sequence:
    1. append_turn; finalize_turn with text "UNIQUE_MARKER_XYZ"
    2. Run through pyte Screen/Stream
  Expected:
    - pyte Screen buffer contains "UNIQUE_MARKER_XYZ" somewhere in its rows
  Edge cases: none

itest_13_pyte_confirms_input_bar_at_bottom
  Components: Terminal, FrameComposer, RenderLoop
  Sequence: Idle state render → pyte Screen
  Expected:
    - "❯" or ">" appears in rows ROWS-5 through ROWS-1
  Edge cases: narrow terminal (ROWS=12) → still in last 5 rows

itest_14_committed_lines_append_only_across_ticks
  Components: TranscriptModel, RenderLoop
  Sequence:
    1. Initial render tick → record len(_committed_lines) = N
    2. Append more content; second render tick → len(_committed_lines) = M
    3. Third tick (no new content) → len(_committed_lines) = M (unchanged)
  Expected: len never decreases; new content only appended
  Edge cases: none

itest_15_cancel_turn_clears_streaming_partial
  Components: TranscriptModel
  Sequence:
    1. append_turn; set_streaming_partial("partial progress")
    2. cancel_turn("a1")
  Expected:
    - get_streaming_partial() is None
    - "[cancelled]" line in _committed_lines
    - "partial progress" NOT in _committed_lines
  Edge cases: none
```

### E2E Tests (10+ enumerated)

All E2E tests are in `tests/e2e/test_tui_transcript_e2e.py`. Mark with `@pytest.mark.e2e`. Use `FakeTerminal` (no real terminal required).

```
e2e_01_standard_chat_workflow
  User scenario: User sends a message; agent responds with Markdown
  Components: Full stack from InputState.submit() through FakeTerminal
  Sequence:
    1. InputState: simulate Enter press with text "fix the bug"
    2. Emit intent_submitted kernel event
    3. TranscriptModel: append_turn for agent
    4. Stream 20 tokens via set_streaming_partial
    5. finalize_turn with Markdown response
    6. RenderLoop: force_commit
  Expected:
    - FakeTerminal.committed_lines contains: user message line, agent header, response body, separator
    - FakeTerminal.committed_lines does NOT contain streaming partials
    - FakeTerminal current bottom block shows idle input bar
  Success criteria: committed_lines have exactly these categories in order; no streaming text leaked

e2e_02_tool_execution_workflow
  User scenario: Agent reads a file, writes a file
  Sequence:
    1. append_turn for agent
    2. add_tool_call("a1", "tc1", "read_file", {"path": "x.py"})
    3. [50ms streaming tick] → bottom block shows spinner for read_file
    4. finish_tool_call("tc1", success=True, result_summary="142 lines")
    5. add_tool_call("a1", "tc2", "write_file", {"path": "x.py"})
    6. finish_tool_call("tc2", success=True, result_summary="written")
    7. finalize_turn
  Expected:
    - Committed lines: agent header, read_file committed line, write_file committed line, response body, separator
    - Both tool call committed lines contain "✓"
  Success criteria: tool call lines appear in order; both contain success indicator

e2e_03_approval_workflow
  User scenario: Agent proposes a write; user approves
  Sequence:
    1. append_turn; commit_diff_block("auth.py", unified_diff)
    2. add_tool_call("a1", "tc1", "write_file")
    3. update_tool_call("tc1", APPROVAL_NEEDED)
    4. [bottom block shows approval gate]
    5. update_tool_call("tc1", RUNNING)  [user pressed Y]
    6. finish_tool_call("tc1", success=True)
    7. finalize_turn
  Expected:
    - Diff block committed BEFORE any approval gate interaction
    - write_file committed line shows "✓ approved"
    - FakeTerminal bottom_history contains a frame with "⚠" approval text
  Success criteria: diff committed first; tool committed only after approval resolved

e2e_04_long_session_eviction
  User scenario: Session exceeds MAX_TURNS_IN_MEMORY
  Sequence:
    1. Append and finalize MAX_TURNS_IN_MEMORY + 10 turns
    2. Verify eviction ran
  Expected:
    - len(model._turns) == MAX_TURNS_IN_MEMORY + 10 (turns list never shrinks — only output evicted)
    - model._turns[:10] all have output_lines == [] and _evicted == True
    - model._turns[-MAX_TURNS_IN_MEMORY:] have output_lines intact
    - total_cost_usd and total_tokens still accumulate correctly
  Success criteria: no Python exception; memory usage stays bounded

e2e_05_session_resume_prints_last_20_turns
  User scenario: User runs agenthicc --resume SESSION_ID
  Sequence:
    1. Mock ConversationStore with 50 stored turns
    2. model.replay_from_store(store, session_id, last_n=20)
    3. Flush to FakeTerminal
  Expected:
    - FakeTerminal.committed_lines contains content from exactly 20 turns
    - First line contains "resumed session" banner
    - committed_cursor == len(committed_lines) after flush
  Success criteria: exactly 20 turn headers visible in committed_lines; no more

e2e_06_streaming_isolation_from_scrollback
  User scenario: Agent streams 100 tokens; turn finalizes
  Sequence:
    1. append_turn
    2. for i in range(100): set_streaming_partial("t" * i)
    3. finalize_turn("a1", "Final content XYZ")
    4. Flush to FakeTerminal
  Expected:
    - FakeTerminal.committed_lines: none contain the intermediate streaming partials
    - FakeTerminal.committed_lines: contains "Final content XYZ" (rendered)
    - FakeTerminal.bottom_history: intermediate frames showed partial text
  Success criteria: streaming_partial never leaks into committed_lines

e2e_07_parallel_agents_color_coded
  User scenario: Three agents run in parallel
  Sequence:
    1. append_turn("a1", "agent:planner")
    2. append_turn("a2", "agent:coder")
    3. append_turn("a3", "agent:tester")
    4. finalize all three
    5. Flush to FakeTerminal
  Expected:
    - Each agent header line has a different ANSI color escape code
    - Headers appear in submission order
  Success criteria: three distinct color codes in three distinct header lines

e2e_08_tool_error_committed_with_cross
  User scenario: Tool fails; error committed inline
  Sequence:
    1. append_turn; add_tool_call
    2. finish_tool_call(success=False, error="permission denied", duration_ms=5)
    3. Flush to FakeTerminal
  Expected:
    - Committed lines contain a line with "✗" and "permission denied"
    - No exception raised
  Success criteria: error information visible in committed_lines

e2e_09_sigwinch_cols_change_renders_correctly
  User scenario: User resizes terminal mid-session
  Sequence:
    1. update_cols(80); append_turn; finalize_turn with long text
    2. update_cols(60); append_turn; finalize_turn with same text
    3. Flush both to FakeTerminal
  Expected:
    - First turn's body lines: each display width <= 80
    - Second turn's body lines: each display width <= 60
  Success criteria: no lines exceed their respective column limit

e2e_10_no_alternate_screen_full_session
  User scenario: Full session from start to finish
  Components: Terminal (real, with FakeTerminal substituted for stdout), full stack
  Sequence:
    1. Full 5-turn session with tool calls
    2. Capture all bytes written to output
  Expected:
    - Output bytes contain NO "\x1b[?1049h" (smcup)
    - Output bytes contain NO "\x1b[?1049l" (rmcup)
    - Output bytes contain "● agent:" (committed turn headers)
  Success criteria: zero alternate screen sequences in any output
```

---

## 7. Acceptance Criteria

All criteria are binary pass/fail. A criterion passes if and only if the stated condition is measurable and true.

### AC-01: No Alternate Screen
**Criterion:** Running `grep -c '\\x1b\[?1049h' captured_output.bin` on the output of any full session returns 0.  
**Measured by:** E2E test `e2e_10_no_alternate_screen_full_session`.

### AC-02: Streaming Text Never in Scrollback
**Criterion:** After `set_streaming_partial(text)`, calling `get_new_committed_lines()` returns a list that does not contain any string equal to `text`.  
**Measured by:** Unit test `test_22_streaming_partial_not_in_committed_lines`; integration test `itest_03_streaming_not_in_committed_lines`.

### AC-03: Turn Headers Committed Immediately on append_turn
**Criterion:** After `append_turn(agent_id, agent_name)`, `len(model._committed_lines) >= 1` and `model._committed_lines[-1]` contains `agent_name`.  
**Measured by:** Unit test `test_02_append_turn_commits_header_immediately`.

### AC-04: Tool Call Lines Committed Only on Terminal State
**Criterion:** `add_tool_call()` does not change `len(_committed_lines)`. `finish_tool_call(success=True)` increases `len(_committed_lines)` by exactly 1. Calling `finish_tool_call` a second time with the same `tool_use_id` does NOT increase `len(_committed_lines)`.  
**Measured by:** Unit tests `test_12_add_tool_call_attaches_to_current_turn`, `test_13_finish_tool_call_success_commits_checkmark_line`, `test_18_update_tool_call_success_commits_line`.

### AC-05: finalize_turn Renders Markdown via Rich
**Criterion:** `finalize_turn("a1", "**bold** text", cols=80)` produces `turn.output_lines` containing at least one line with the ANSI bold escape sequence `\033[1m` OR the text "bold" (Rich Markdown rendering).  
**Measured by:** Unit test `test_23_finalize_turn_commits_body_and_separator`.

### AC-06: Memory Eviction Under Threshold
**Criterion:** After appending MAX_TURNS_IN_MEMORY + 1 turns (each with 100 output_lines), the Python process RSS increase attributable to `TranscriptModel` is less than 15MB. Verified via `tracemalloc` in a benchmark test.  
**Measured by:** E2E test `e2e_04_long_session_eviction`.

### AC-07: Evicted Turns Retain Metadata
**Criterion:** After `evict_old_turns(keep_last=200)` is called on a model with 210 turns, `turns[0].agent_id` is non-empty, `turns[0].timestamp > 0`, and `turns[0].state` is not None.  
**Measured by:** Unit test `test_35_evict_old_turns_preserves_metadata`.

### AC-08: _committed_lines is Append-Only
**Criterion:** Between any two calls to `get_new_committed_lines()`, `len(model._committed_lines)` is non-decreasing.  
**Measured by:** Integration test `itest_14_committed_lines_append_only_across_ticks`.

### AC-09: Single write() Call Per Committed Batch
**Criterion:** `FakeTerminal.write_call_count` increases by exactly 1 when `RenderLoop._do_render()` calls `Terminal.commit_lines(new_lines)` for any non-empty `new_lines`.  
**Measured by:** Integration test `itest_04_render_loop_batches_new_lines_in_single_call`.

### AC-10: Streaming Zone in Bottom Block
**Criterion:** While `get_streaming_partial()` returns a non-None value, `FakeTerminal._current_bottom` (the most recent frame from `set_bottom()`) contains a row that includes text from `streaming_partial`.  
**Measured by:** E2E test `e2e_06_streaming_isolation_from_scrollback`.

### AC-11: Resume Replays Last 20 Turns
**Criterion:** `replay_from_store(mock_store_with_50_turns, session_id, last_n=20)` results in exactly 20 turn header lines in `_committed_lines` (plus the resume banner and body lines).  
**Measured by:** E2E test `e2e_05_session_resume_prints_last_20_turns`.

### AC-12: Diff Blocks Truncated at MAX_DIFF_LINES
**Criterion:** `commit_diff_block("x.py", large_diff)` where `large_diff` has 200 changed lines results in at most `MAX_DIFF_LINES + 3` new entries in `_committed_lines` (the +3 accounts for header, truncation notice, footer).  
**Measured by:** Integration test `itest_07_diff_block_truncated_at_max_diff_lines`.

### AC-13: Parallel Agent Color Differentiation
**Criterion:** Three agents with distinct `agent_id` values have distinct `color_index` values assigned by `append_turn()`, and their header lines in `_committed_lines` contain distinct ANSI color escape codes.  
**Measured by:** Integration test `itest_08_multiple_agents_color_coded_in_headers`.

### AC-14: Backward Compatibility with Existing API
**Criterion:** All tests in `tests/unit/test_transcript.py` and `tests/unit/test_transcript_extended.py` pass against the new implementation without modification.  
**Measured by:** Running the existing test suite: `uv run pytest tests/unit/test_transcript.py tests/unit/test_transcript_extended.py -v`.

### AC-15: Mypy and Ruff Clean
**Criterion:** `uv run mypy src/agenthicc/tui/transcript.py` exits with code 0. `uv run ruff check src/agenthicc/tui/transcript.py` exits with code 0. `uv run ruff format --check src/agenthicc/tui/transcript.py` exits with code 0.  
**Measured by:** CI gate.

---

## Appendix A: Module File Location and Imports

The `TranscriptModel` and all supporting types must live at:

```
src/agenthicc/tui/__init__.py        (re-exports public symbols)
src/agenthicc/tui/transcript.py      (primary module — this PRD)
```

Public symbols re-exported from `src/agenthicc/tui/__init__.py`:

```python
from agenthicc.tui.transcript import (
    AgentTurnEntry,
    ToolCallEntry,
    ToolCallState,
    TurnState,
    TranscriptModel,
    SPINNER_FRAMES,
    MAX_TURNS_IN_MEMORY,
    MAX_LINES_PER_TURN,
    MAX_DIFF_LINES,
    _MD_SENTINEL,
)
```

Required pip dependencies for `transcript.py`:

```
wcwidth>=0.2.13     # display-width calculation
rich>=13.0          # Markdown → ANSI rendering
```

These must be in `pyproject.toml` dependencies.

---

## Appendix B: Private Render Helpers

These functions live in `transcript.py` and are used internally. They are not part of the public API but must match these signatures for tests to work.

```python
def _render_turn_header(turn: AgentTurnEntry) -> str:
    """
    Render the single-line turn header.
    Format: "● agent:name  HH:MM:SS"
    With color: ANSI escape for agent color, bold ●, muted timestamp.
    Without color (NO_COLOR env): plain text with no escapes.
    """

def _render_tool_call_committed(tc: ToolCallEntry) -> str:
    """
    Render a completed tool call as a single committed line.
    SUCCESS: "  ⎿ tool_name(arg=val)  ✓ result_summary (Xms)"
    FAILURE: "  ⎿ tool_name(arg=val)  ✗ Xms  error_message"
    """

def _render_separator(cols: int) -> str:
    """
    Render a horizontal separator at full terminal width.
    "─" × cols, in dim styling with ANSI reset.
    """

def _render_turn(turn: AgentTurnEntry) -> list[str]:
    """
    Render a complete turn (header + body + tool calls + separator) to lines.
    Used by render(). Handles evicted turns gracefully (shows compact form).
    """

def _render_diff(file_path: str, diff_text: str, cols: int) -> list[str]:
    """
    Render a unified diff to ANSI lines.
    Added lines: green. Removed lines: red. Hunk markers: cyan. Context: dim.
    Header: "─── Proposed change: {file_path} ─── " padded to cols.
    Footer: "─" × cols.
    Truncates at MAX_DIFF_LINES; appends truncation notice if needed.
    """

def _render_markdown_to_lines(text: str, cols: int) -> list[str]:
    """
    Render Markdown text to ANSI lines via Rich.
    Always uses force_terminal=True and explicit width.
    Returns lines without trailing newlines.
    """

def _format_args(args: dict) -> str:
    """
    Format tool call arguments as a short string.
    Shows up to 2 key=value pairs; truncates remainder.
    Examples: "path='x.py'" or "cmd='pytest tests/'" or "path='x.py', ..."
    """

def _truncate_to_display_cols(text: str, cols: int) -> str:
    """
    Truncate text to at most cols display columns, using wcwidth.wcswidth().
    Strips ANSI escape sequences before measuring. Preserves ANSI in output.
    """
```

---

## Appendix C: AgenthiccConfig Integration

`TranscriptModel` constants must be overridable via `AgenthiccConfig`. The config sub-dataclass:

```python
# In src/agenthicc/config.py, add to AgenthiccConfig:
@dataclass
class TUISettings:
    max_turns_in_memory: int = 200
    max_lines_per_turn: int = 500
    max_diff_lines: int = 50
    max_streaming_rows: int = 8
    spinner_frames: list[str] = field(default_factory=lambda: SPINNER_FRAMES)
```

`TranscriptModel.__init__()` accepts an optional `config: TUISettings | None = None` parameter and uses it to override the module-level constants:

```python
def __init__(self, config: "TUISettings | None" = None) -> None:
    self._max_turns = (config.max_turns_in_memory if config else MAX_TURNS_IN_MEMORY)
    self._max_lines = (config.max_lines_per_turn if config else MAX_LINES_PER_TURN)
    # ... etc
```
