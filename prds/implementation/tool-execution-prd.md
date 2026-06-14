# Tool Execution Display — Implementation PRD

**Document version:** 1.0  
**Date:** 2026-06-13  
**Status:** Implementation-ready  
**Target file:** `/root/python_projects/agenthicc/src/agenthicc/tui/tool_execution.py` (primary) plus integration points documented per section  
**Framework constraints:** Python 3.11+, type hints throughout, mypy strict mode, no alternate screen, inline committed-transcript architecture

---

## Purpose

This PRD specifies the complete implementation of tool execution display for the AgentHICC TUI. It covers the data model, rendering logic, streaming architecture, lifecycle integration, cancellation, the `/expand` command, parallel tool call display, and a complete test suite with 100+ named tests.

All rendering follows the committed-transcript + live-bottom-block architecture from the master PRD. Running tool calls appear exclusively in the live bottom block. Completed tool calls are committed to the scrollback transcript as immutable lines. No tool call line is ever erased once committed.

---

## 1. Tool Call Data Model

### 1.1 `ToolCallEntry` — Extended Dataclass

**File:** `src/agenthicc/tui/tool_execution.py`

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Final


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPINNER_BRAILLE: Final[list[str]] = [
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
]
SPINNER_ASCII: Final[list[str]] = ["|", "/", "-", "\\"]

# Tool categories for rendering logic
FILE_TOOLS: Final[frozenset[str]] = frozenset({
    "read_file", "write_file", "patch_file", "append_file",
    "delete_file", "move_file", "copy_file", "read_lines",
})
SHELL_TOOLS: Final[frozenset[str]] = frozenset({
    "run_bash", "run_command", "run_python", "run_python_expr", "run_tests",
})
GIT_TOOLS: Final[frozenset[str]] = frozenset({
    "git_status", "git_diff", "git_log", "git_show", "git_add",
    "git_commit", "git_checkout", "git_branch", "git_stash",
    "git_blame", "git_grep",
})
SEARCH_TOOLS: Final[frozenset[str]] = frozenset({
    "search_files", "grep_files", "list_directory", "file_exists",
    "get_file_info",
})

MAX_ARGS_DISPLAY_CHARS: Final[int] = 60
DEFAULT_OUTPUT_PREVIEW_LINES: Final[int] = 2
DEFAULT_DIFF_PREVIEW_LINES: Final[int] = 20
MAX_STREAM_BUFFER_LINES: Final[int] = 200


# ---------------------------------------------------------------------------
# ToolCallState enum
# ---------------------------------------------------------------------------

class ToolCallState(Enum):
    """
    State machine for a single tool call.

    Valid transitions:
        PENDING  -> RUNNING
        RUNNING  -> SUCCESS
        RUNNING  -> FAILURE
        RUNNING  -> APPROVAL_NEEDED
        APPROVAL_NEEDED -> RUNNING   (user approved)
        APPROVAL_NEEDED -> FAILURE   (user denied)

    PENDING, RUNNING, and APPROVAL_NEEDED are live states — rendered in the
    bottom block only, never committed to the transcript.

    SUCCESS and FAILURE are terminal states — the committed line is written
    to the transcript exactly once when these states are reached.
    """
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILURE = auto()
    APPROVAL_NEEDED = auto()


# ---------------------------------------------------------------------------
# ToolCallEntry — the extended data record
# ---------------------------------------------------------------------------

@dataclass
class ToolCallEntry:
    """
    Complete record for one tool invocation.

    Lifecycle:
        Created when ToolCallStarted signal arrives. State begins as PENDING.
        Transitions to RUNNING on executor start. Transitions to SUCCESS or
        FAILURE on ToolCallComplete. If approval is required,
        transitions through APPROVAL_NEEDED back to RUNNING.

    Rendering contract:
        While state in (PENDING, RUNNING, APPROVAL_NEEDED):
            Rendered in the live bottom block only.
        When state in (SUCCESS, FAILURE):
            Committed to scrollback transcript as immutable lines.
            Removed from the live bottom block.

    Fields:
        tool_use_id     Unique identifier scoped to one session. Used as the
                        expansion key for /expand {tool_use_id[:8]}.
        name            Tool function name, e.g. "read_file".
        args            Raw argument dict passed to the tool.
        state           Current ToolCallState.
        duration_ms     Wall-clock milliseconds from RUNNING start to
                        SUCCESS/FAILURE. 0 while still running.
        error           Error message string on FAILURE. Empty string on
                        SUCCESS or while still running.
        output_lines    All output lines from the tool result. May be thousands
                        of lines for bash commands. Never truncated here;
                        truncation happens only at render time.
        expanded        Whether the user has /expanded this tool call. When
                        True, all output_lines are rendered; when False, only
                        DEFAULT_OUTPUT_PREVIEW_LINES are shown.
        spinner_frame   Current spinner animation frame index. Incremented by
                        the render loop. Read-only to all callers except
                        RenderLoop.tick().
        diff            Unified diff string for file-editing tools. None for
                        non-file tools. Populated at SUCCESS/FAILURE time by
                        the DiffComputer.
        started_at      monotonic() timestamp when state transitioned to
                        RUNNING. 0.0 until then.
        completed_at    monotonic() timestamp when state transitioned to
                        SUCCESS or FAILURE. 0.0 until then.
        file_snapshot_before
                        Raw file bytes captured immediately before the tool
                        executed (only for write_file, patch_file, append_file,
                        delete_file, move_file, copy_file). None for all other
                        tools. Used by DiffComputer to generate the diff.
    """
    # Identity
    tool_use_id: str
    name: str
    args: dict[str, object]

    # State machine
    state: ToolCallState = ToolCallState.PENDING

    # Timing
    started_at: float = 0.0
    completed_at: float = 0.0
    duration_ms: int = 0

    # Result
    error: str = ""
    output_lines: list[str] = field(default_factory=list)

    # Rendering
    expanded: bool = False
    spinner_frame: int = 0
    diff: str | None = None

    # Pre-execution file snapshot (for diff generation)
    file_snapshot_before: bytes | None = field(default=None, repr=False)

    # ---------------------------------------------------------------------------
    # Derived properties
    # ---------------------------------------------------------------------------

    @property
    def short_id(self) -> str:
        """First 8 characters of tool_use_id. Used in /expand hints."""
        return self.tool_use_id[:8]

    @property
    def is_live(self) -> bool:
        """True while still in the bottom block (not yet committed)."""
        return self.state in (
            ToolCallState.PENDING,
            ToolCallState.RUNNING,
            ToolCallState.APPROVAL_NEEDED,
        )

    @property
    def is_terminal(self) -> bool:
        """True when committed to the transcript."""
        return self.state in (ToolCallState.SUCCESS, ToolCallState.FAILURE)

    @property
    def tool_category(self) -> str:
        """
        One of: "file", "shell", "git", "search", "other".
        Controls rendering logic (diff display, output streaming, truncation).
        """
        if self.name in FILE_TOOLS:
            return "file"
        if self.name in SHELL_TOOLS:
            return "shell"
        if self.name in GIT_TOOLS:
            return "git"
        if self.name in SEARCH_TOOLS:
            return "search"
        return "other"

    @property
    def should_show_diff(self) -> bool:
        """True for file-editing tools that have a non-None diff."""
        return self.tool_category in ("file", "git") and self.diff is not None

    def mark_running(self) -> None:
        """Transition from PENDING to RUNNING. Records started_at."""
        assert self.state == ToolCallState.PENDING, (
            f"mark_running called from state {self.state!r}"
        )
        self.state = ToolCallState.RUNNING
        self.started_at = time.monotonic()

    def mark_success(self, output_lines: list[str], diff: str | None = None) -> None:
        """
        Transition to SUCCESS. Records duration, stores output and diff.
        Must be called from RUNNING state.
        """
        assert self.state == ToolCallState.RUNNING, (
            f"mark_success called from state {self.state!r}"
        )
        self.completed_at = time.monotonic()
        self.duration_ms = int((self.completed_at - self.started_at) * 1000)
        self.state = ToolCallState.SUCCESS
        self.output_lines = output_lines
        self.diff = diff

    def mark_failure(self, error: str, output_lines: list[str] | None = None) -> None:
        """
        Transition to FAILURE. Records duration and error message.
        May be called from RUNNING or APPROVAL_NEEDED.
        """
        assert self.state in (
            ToolCallState.RUNNING, ToolCallState.APPROVAL_NEEDED
        ), f"mark_failure called from state {self.state!r}"
        self.completed_at = time.monotonic()
        self.duration_ms = int((self.completed_at - self.started_at) * 1000)
        self.state = ToolCallState.FAILURE
        self.error = error
        if output_lines is not None:
            self.output_lines = output_lines

    def mark_approval_needed(self) -> None:
        """Transition from RUNNING to APPROVAL_NEEDED."""
        assert self.state == ToolCallState.RUNNING, (
            f"mark_approval_needed called from state {self.state!r}"
        )
        self.state = ToolCallState.APPROVAL_NEEDED

    def resume_from_approval(self) -> None:
        """Transition from APPROVAL_NEEDED back to RUNNING (user approved)."""
        assert self.state == ToolCallState.APPROVAL_NEEDED, (
            f"resume_from_approval called from state {self.state!r}"
        )
        self.state = ToolCallState.RUNNING

    def tick_spinner(self) -> None:
        """Advance spinner_frame by one. Called by RenderLoop on each tick."""
        self.spinner_frame = (self.spinner_frame + 1) % len(SPINNER_BRAILLE)
```

### 1.2 `ToolCallState` — Detailed Transition Table

| From state | Event | To state | Side effect |
|---|---|---|---|
| `PENDING` | executor starts | `RUNNING` | `started_at` set to `time.monotonic()` |
| `RUNNING` | tool returns ok | `SUCCESS` | `completed_at`, `duration_ms`, `output_lines`, `diff` set; committed-line queued |
| `RUNNING` | tool returns error | `FAILURE` | `completed_at`, `duration_ms`, `error` set; committed-line queued |
| `RUNNING` | approval required | `APPROVAL_NEEDED` | approval gate appears in bottom block; agent paused |
| `APPROVAL_NEEDED` | user presses Y | `RUNNING` | approval gate dismissed; agent resumes |
| `APPROVAL_NEEDED` | user presses N | `FAILURE` | `error = "denied by user"`; committed-line queued |
| Any live state | Ctrl+C | `FAILURE` | `error = "cancelled by user"`; partial output committed |

### 1.3 Tool Categories — Rendering Behaviour Matrix

| Category | Example tools | Running state display | Success state display | Failure state display |
|---|---|---|---|---|
| `file` | `read_file`, `write_file`, `patch_file` | Spinner + name + primary arg | ✓ + line count (reads) or diff (writes) | ✗ + error message |
| `shell` | `run_bash`, `run_command`, `run_tests` | Spinner + name + truncated cmd; stream last N output lines in bottom block | ✓ + exit code + timing; output collapsed by default | ✗ + exit code + first error line |
| `git` | `git_diff`, `git_commit`, `git_status` | Spinner + name + args | ✓ + summary; diff shown for destructive ops | ✗ + git error |
| `search` | `grep_files`, `search_files` | Spinner + name + pattern/path | ✓ + match count; truncated result list | ✗ + error |
| `other` | All unclassified | Spinner + name + args summary | ✓ + result summary | ✗ + error |

---

## 2. Tool Call Rendering

### 2.1 Running State — Live Bottom Block

Running tool calls are displayed in the live bottom block, which is erased and redrawn on each RenderLoop tick (50ms interval). The spinner frame advances each tick.

**Exact format:**

```
  ⎿ tool_name(arg1='val', arg2='val')  ⠋
```

**Rendering rules:**

1. Two leading spaces as indentation (visual subordination to agent text above).
2. `⎿` (U+23BF) as tool call prefix, rendered in dim style (`\033[2m⎿\033[0m`).
3. One space, then `tool_name` in normal weight.
4. Opening paren, then formatted args (see §2.1.1), closing paren.
5. Two spaces, then the current spinner character in cyan (`\033[36m`).
6. No output lines yet — output is never shown during RUNNING state.

**Argument formatting — §2.1.1:**

```python
def format_args_display(args: dict[str, object], max_chars: int = MAX_ARGS_DISPLAY_CHARS) -> str:
    """
    Format a tool argument dict as a short positional-style string.
    Truncates to max_chars with '...' if needed.

    Rules:
    - String values: single-quoted, truncated to 40 chars individually
    - Non-string values: repr(), truncated to 20 chars
    - Multiple args: joined with ', '
    - Total truncated to max_chars with trailing '...'

    Examples:
        {"path": "src/auth.py"} -> "path='src/auth.py'"
        {"cmd": "pytest tests/ -x --tb=short"} -> "cmd='pytest tests/ -x -...'"
        {"path": "src/x.py", "content": "a\nb\nc"} -> "path='src/x.py', content='a\\nb...'"
    """
    parts: list[str] = []
    for key, val in args.items():
        if isinstance(val, str):
            truncated = val[:40] + ("..." if len(val) > 40 else "")
            parts.append(f"{key}={truncated!r}")
        else:
            s = repr(val)
            truncated = s[:20] + ("..." if len(s) > 20 else "")
            parts.append(f"{key}={truncated}")
    joined = ", ".join(parts)
    if len(joined) > max_chars:
        joined = joined[:max_chars - 3] + "..."
    return joined
```

**Full ANSI-colored running line:**

```python
def render_running_line(tc: ToolCallEntry, color: bool, unicode_mode: bool) -> str:
    args_str = format_args_display(tc.args)
    if unicode_mode:
        spinner = SPINNER_BRAILLE[tc.spinner_frame % len(SPINNER_BRAILLE)]
        prefix = "⎿"
    else:
        spinner = SPINNER_ASCII[tc.spinner_frame % len(SPINNER_ASCII)]
        prefix = ">"

    if color:
        prefix_s = f"\033[2m{prefix}\033[0m"
        spinner_s = f"\033[36m{spinner}\033[0m"
    else:
        prefix_s = prefix
        spinner_s = spinner

    return f"  {prefix_s} {tc.name}({args_str})  {spinner_s}"
```

**Shell tool streaming addition:**

For shell tools only, while in RUNNING state, up to 3 lines of the most recent stdout are shown beneath the spinner line in the bottom block. These lines are dim and truncated to the terminal width.

```
  ⎿ run_bash(cmd='pytest tests/ -x')  ⠋
      collected 47 items
      PASSED tests/test_auth.py::test_verify (dim)
```

These streaming output lines are **never committed** — they exist only in the live bottom block and vanish when the tool completes.

### 2.2 Success State — Committed to Scrollback

When a tool call reaches SUCCESS, the following lines are committed to the scrollback transcript exactly once via `RenderLoop.force_commit()`. They are **never erased or redrawn**.

**Format (collapsed, default):**

```
  ⎿ tool_name(arg1='val')  ✓  23ms
      output line 1 (dim)
      output line 2 (dim)
      (+5 more — /expand abc12345)
```

**Rendering rules:**

1. First line: indented tool call header.
   - `⎿` dim prefix
   - Tool name and truncated args (same rules as running state)
   - Two spaces
   - `✓` in green (`\033[32m✓\033[0m`) or plain `✓` in NO_COLOR mode
   - Two spaces
   - Duration in dim (`\033[2m23ms\033[0m`)
2. Output lines: shown at `DEFAULT_OUTPUT_PREVIEW_LINES` (2) by default, each indented 6 spaces, rendered dim (`\033[2m`).
3. Truncation hint: if `len(output_lines) > DEFAULT_OUTPUT_PREVIEW_LINES` and not `expanded`:
   - `      (+N more — /expand {tc.short_id})` in dim
   - N = `len(output_lines) - DEFAULT_OUTPUT_PREVIEW_LINES`
4. If `tc.expanded == True`: all `output_lines` are shown, no truncation hint.

**Expanded state:**

```
  ⎿ tool_name(arg1='val')  ✓  23ms  [expanded]
      output line 1 (dim)
      output line 2 (dim)
      output line 3 (dim)
      ... (all N lines)
```

The `[expanded]` marker is in dim cyan: `\033[2;36m[expanded]\033[0m`.

**Full ANSI commit renderer:**

```python
def render_success_committed(
    tc: ToolCallEntry,
    color: bool,
    unicode_mode: bool,
) -> list[str]:
    """
    Produce the committed lines for a SUCCESS tool call.
    Returns a list of strings, each a complete display line.
    No newline characters in the strings.
    """
    lines: list[str] = []
    args_str = format_args_display(tc.args)
    prefix = "⎿" if unicode_mode else ">"
    check = "✓"

    if color:
        prefix_s = f"\033[2m{prefix}\033[0m"
        check_s = f"\033[32m{check}\033[0m"
        dur_s = f"\033[2m{tc.duration_ms}ms\033[0m"
        expanded_s = f"  \033[2;36m[expanded]\033[0m" if tc.expanded else ""
    else:
        prefix_s = prefix
        check_s = check
        dur_s = f"{tc.duration_ms}ms"
        expanded_s = "  [expanded]" if tc.expanded else ""

    header = f"  {prefix_s} {tc.name}({args_str})  {check_s}  {dur_s}{expanded_s}"
    lines.append(header)

    # Output lines
    display_count = len(tc.output_lines) if tc.expanded else DEFAULT_OUTPUT_PREVIEW_LINES
    for out_line in tc.output_lines[:display_count]:
        if color:
            lines.append(f"      \033[2m{out_line}\033[0m")
        else:
            lines.append(f"      {out_line}")

    # Truncation hint
    remaining = len(tc.output_lines) - display_count
    if remaining > 0 and not tc.expanded:
        hint = f"      (+{remaining} more — /expand {tc.short_id})"
        if color:
            lines.append(f"\033[2m{hint}\033[0m")
        else:
            lines.append(hint)

    # Diff block (for file and git tools)
    if tc.should_show_diff and tc.diff:
        lines.extend(render_diff_block(tc.diff, color, unicode_mode, expanded=tc.expanded))

    return lines
```

### 2.3 Failure State — Committed to Scrollback

**Format:**

```
  ⎿ tool_name(arg1='val')  ✗  23ms
      Error: message here (dim red)
```

**Rendering rules:**

1. Header line: same structure as success but with `✗` in red (`\033[31m✗\033[0m`).
2. Error line: 6 spaces indent + `Error: ` prefix + error message, all in dim red (`\033[2;31m`).
3. If `output_lines` is non-empty (partial output on failure), show up to `DEFAULT_OUTPUT_PREVIEW_LINES` lines in dim, with expand hint.

```python
def render_failure_committed(
    tc: ToolCallEntry,
    color: bool,
    unicode_mode: bool,
) -> list[str]:
    lines: list[str] = []
    args_str = format_args_display(tc.args)
    prefix = "⎿" if unicode_mode else ">"
    cross = "✗"

    if color:
        prefix_s = f"\033[2m{prefix}\033[0m"
        cross_s = f"\033[31m{cross}\033[0m"
        dur_s = f"\033[2m{tc.duration_ms}ms\033[0m"
        err_s = f"\033[2;31mError: {tc.error}\033[0m"
    else:
        prefix_s = prefix
        cross_s = cross
        dur_s = f"{tc.duration_ms}ms"
        err_s = f"Error: {tc.error}"

    header = f"  {prefix_s} {tc.name}({args_str})  {cross_s}  {dur_s}"
    lines.append(header)
    lines.append(f"      {err_s}")

    # Partial output if present
    if tc.output_lines:
        display_count = len(tc.output_lines) if tc.expanded else DEFAULT_OUTPUT_PREVIEW_LINES
        for out_line in tc.output_lines[:display_count]:
            if color:
                lines.append(f"      \033[2m{out_line}\033[0m")
            else:
                lines.append(f"      {out_line}")
        remaining = len(tc.output_lines) - display_count
        if remaining > 0 and not tc.expanded:
            hint = f"      (+{remaining} more — /expand {tc.short_id})"
            lines.append(f"\033[2m{hint}\033[0m" if color else hint)

    return lines
```

### 2.4 Diff Display — File Tools

Diffs are rendered as committed lines below the tool call header line. They are part of the same `render_success_committed()` output list.

**Format:**

```
  ─── src/auth/session.py ─────────────────────────────────────────
  @@ -145,4 +145,4 @@
       def validate_token(token: SessionToken) -> bool:
  -    expiry = datetime.now() + timedelta(hours=24)
  +    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
  -    if token.expiry < datetime.now():
  +    if token.expiry < datetime.now(timezone.utc):
  ─────────────────────────────────────────────────────────────────
      (+12 more lines — /expand abc12345)
```

**Rendering rules:**

1. Header separator: `  ─── {filename} ` padded to terminal width with `─`.
2. `@@` hunk markers in dim cyan (`\033[2;36m@@ ... @@\033[0m`).
3. Added lines: `+` prefix in green (`\033[32m+    ...\033[0m`).
4. Removed lines: `-` prefix in red (`\033[31m-    ...\033[0m`).
5. Context lines: space prefix in dim (`\033[2m     ...\033[0m`).
6. Footer separator: `  ` + `─` × (terminal_width - 2).
7. Default maximum shown lines: `DEFAULT_DIFF_PREVIEW_LINES` = 20. When diff exceeds this, truncate and add:
   - `      (+N more lines — /expand {tc.short_id})`

```python
def render_diff_block(
    diff_text: str,
    color: bool,
    unicode_mode: bool,
    expanded: bool = False,
    file_label: str = "",
    cols: int = 80,
    short_id: str = "",
) -> list[str]:
    """
    Render a unified diff string as display lines.

    Args:
        diff_text:  Raw unified diff string (output of difflib.unified_diff
                    or the tool's diff output).
        color:      Whether to apply ANSI color codes.
        unicode_mode: Whether to use Unicode box characters.
        expanded:   If True, show all lines without truncation.
        file_label: Optional filename for the header separator.
        cols:       Terminal column count for separator width.
        short_id:   8-char tool_use_id prefix for expand hint.

    Returns:
        List of display lines (no trailing newlines).
    """
    diff_lines = diff_text.splitlines()
    lines: list[str] = []

    # Header separator
    sep_char = "─" if unicode_mode else "-"
    if file_label:
        prefix = f"  {sep_char}{sep_char}{sep_char} {file_label} "
        fill = max(0, cols - len(prefix) - 2)
        header = prefix + sep_char * fill
    else:
        header = "  " + sep_char * (cols - 2)
    lines.append(f"\033[2m{header}\033[0m" if color else header)

    # Diff lines
    max_lines = len(diff_lines) if expanded else DEFAULT_DIFF_PREVIEW_LINES
    for raw_line in diff_lines[:max_lines]:
        if raw_line.startswith("@@"):
            rendered = f"\033[2;36m{raw_line}\033[0m" if color else raw_line
        elif raw_line.startswith("+") and not raw_line.startswith("+++"):
            rendered = f"\033[32m{raw_line}\033[0m" if color else raw_line
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            rendered = f"\033[31m{raw_line}\033[0m" if color else raw_line
        else:
            rendered = f"\033[2m{raw_line}\033[0m" if color else raw_line
        lines.append(rendered)

    # Truncation hint
    remaining = len(diff_lines) - max_lines
    if remaining > 0 and not expanded:
        hint = f"      (+{remaining} more lines — /expand {short_id})"
        lines.append(f"\033[2m{hint}\033[0m" if color else hint)

    # Footer separator
    footer = "  " + sep_char * (cols - 2)
    lines.append(f"\033[2m{footer}\033[0m" if color else footer)

    return lines
```

---

## 3. Tool Output Streaming

### 3.1 Shell Tool Output Streaming

For `run_bash`, `run_command`, `run_python`, and `run_tests`, stdout is streamed in real time while the tool is executing. The streaming display appears in the live bottom block.

**Architecture:**

- The tool executor calls `tool_entry.append_stream_line(line: str)` for each stdout/stderr line received.
- `ToolCallEntry` maintains a ring buffer of the last `MAX_STREAM_BUFFER_LINES` lines.
- `RenderLoop` reads `tool_entry.stream_buffer_tail(n=3)` on each tick to compose the bottom block.
- Buffer lines are **never committed** to the transcript as streaming intermediates; only the final `output_lines` populated at SUCCESS time are committed.

**Stream buffer on `ToolCallEntry`:**

```python
# Additional fields on ToolCallEntry for shell streaming

# Stream buffer — ring buffer of last MAX_STREAM_BUFFER_LINES lines
_stream_buffer: list[str] = field(default_factory=list, repr=False)

def append_stream_line(self, line: str) -> None:
    """Append one stdout/stderr line to the stream ring buffer."""
    self._stream_buffer.append(line)
    if len(self._stream_buffer) > MAX_STREAM_BUFFER_LINES:
        self._stream_buffer = self._stream_buffer[-MAX_STREAM_BUFFER_LINES:]

def stream_buffer_tail(self, n: int = 3) -> list[str]:
    """Return the last n lines of the stream buffer."""
    return self._stream_buffer[-n:]
```

### 3.2 Buffer Management

- **Ring buffer size:** `MAX_STREAM_BUFFER_LINES = 200`. When exceeded, the oldest lines are discarded. The stream buffer is never persisted; it is purely for live display.
- **Line truncation:** Each stream line is truncated to `cols - 8` characters before display (6 chars indent + 2 chars margin) to prevent word-wrap in the bottom block.
- **Memory:** The stream buffer consumes at most `200 × (terminal_cols - 8)` bytes, approximately 14 KB for an 80-column terminal. This is well within budget.

### 3.3 Truncation Strategy

For non-shell tools (file, git, search, other), `output_lines` is populated only at tool completion, not streamed. The truncation strategy at commit time:

1. File tools: `output_lines = ["<N lines>"]` — only a line count summary, no raw content.
2. Git tools: `output_lines = result.stdout.splitlines()` — raw git output, up to 500 lines.
3. Search tools: `output_lines = result.matches[:50]` — first 50 matches.
4. Shell tools: `output_lines = all_stdout_lines` — complete output. Expansion shows all.
5. Other tools: `output_lines = [str(result)][:200]` — JSON/repr truncated to 200 lines.

---

## 4. Tool Call Lifecycle Integration

### 4.1 Signal Flow

```
ToolExecutor (tools/executor.py)
  │
  ├── ToolCallStarted effect
  │     payload: { tool_use_id, name, args }
  │     → TUIEventAdapter.on_tool_call_started()
  │         → creates ToolCallEntry(state=PENDING)
  │         → TranscriptModel.add_tool_call(turn_id, entry)
  │         → RenderLoop.request_redraw()
  │
  ├── ToolCallRunning effect  [new effect type]
  │     payload: { tool_use_id }
  │     → TUIEventAdapter.on_tool_call_running()
  │         → entry.mark_running()
  │         → if file tool: DiffComputer.snapshot_before(args["path"])
  │             → entry.file_snapshot_before = bytes
  │         → RenderLoop.request_redraw()
  │
  ├── ToolCallStreamLine effect  [new effect type, shell tools only]
  │     payload: { tool_use_id, line: str }
  │     → TUIEventAdapter.on_tool_call_stream_line()
  │         → entry.append_stream_line(line)
  │         → RenderLoop.request_redraw()  [50ms debounced]
  │
  └── ToolCallComplete effect
        payload: { tool_use_id, ok: bool, result: dict, error: str }
        → TUIEventAdapter.on_tool_call_complete()
            → if ok:
                output_lines = _extract_output_lines(result)
                diff = DiffComputer.compute(entry) if file tool
                entry.mark_success(output_lines, diff)
            → else:
                entry.mark_failure(result.get("error", error))
            → committed_lines = render_committed(entry)
            → RenderLoop.force_commit(committed_lines)
            → TranscriptModel.remove_live_tool_call(entry.tool_use_id)
```

### 4.2 `TUIEventAdapter` — New Methods

**File:** `src/agenthicc/tui/events.py`

```python
def on_tool_call_started(
    self,
    turn_id: str,
    tool_use_id: str,
    name: str,
    args: dict[str, object],
) -> None:
    """Create ToolCallEntry and register it as a live tool call."""
    entry = ToolCallEntry(
        tool_use_id=tool_use_id,
        name=name,
        args=args,
        state=ToolCallState.PENDING,
    )
    self._transcript.add_tool_call(turn_id, entry)
    self._live_tool_calls[tool_use_id] = entry
    self._render_loop.request_redraw()


def on_tool_call_running(self, tool_use_id: str) -> None:
    """Transition entry to RUNNING. Trigger file snapshot if needed."""
    entry = self._live_tool_calls.get(tool_use_id)
    if entry is None:
        return
    entry.mark_running()
    if entry.tool_category == "file" and "path" in entry.args:
        path = str(entry.args["path"])
        entry.file_snapshot_before = self._diff_computer.read_file_bytes(path)
    self._render_loop.request_redraw()


def on_tool_call_stream_line(self, tool_use_id: str, line: str) -> None:
    """Append a streaming output line (shell tools only)."""
    entry = self._live_tool_calls.get(tool_use_id)
    if entry is None:
        return
    entry.append_stream_line(line)
    # Debounce: only request redraw every 3rd stream line or after 50ms
    self._stream_line_count[tool_use_id] = (
        self._stream_line_count.get(tool_use_id, 0) + 1
    )
    if self._stream_line_count[tool_use_id] % 3 == 0:
        self._render_loop.request_redraw()


def on_tool_call_complete(
    self,
    tool_use_id: str,
    ok: bool,
    result: dict[str, object],
    error: str = "",
) -> None:
    """
    Transition entry to terminal state, compute diff, and commit to transcript.
    """
    entry = self._live_tool_calls.pop(tool_use_id, None)
    if entry is None:
        return

    if ok:
        output_lines = self._extract_output_lines(entry, result)
        diff: str | None = None
        if entry.tool_category in ("file", "git"):
            diff = self._diff_computer.compute(entry, result)
        entry.mark_success(output_lines, diff)
        committed = render_success_committed(
            entry,
            color=self._color,
            unicode_mode=self._unicode_mode,
        )
    else:
        partial_lines = self._extract_output_lines(entry, result) if result else None
        entry.mark_failure(error, partial_lines)
        committed = render_failure_committed(
            entry,
            color=self._color,
            unicode_mode=self._unicode_mode,
        )

    self._render_loop.force_commit(committed)
    self._transcript.remove_live_tool_call(tool_use_id)
    self._stream_line_count.pop(tool_use_id, None)
```

### 4.3 `DiffComputer` — File Snapshot and Diff Generation

**File:** `src/agenthicc/tui/diff_computer.py`

```python
from __future__ import annotations

import difflib
import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tool_execution import ToolCallEntry


class DiffComputer:
    """
    Computes unified diffs for file-editing tool calls.

    Workflow:
        1. Before tool executes: read_file_bytes(path) → save as file_snapshot_before
        2. After tool completes: compute(entry, result) → unified diff string

    Handles edge cases:
        - File did not exist before (new file): treat before as empty bytes
        - File deleted (delete_file): treat after as empty bytes
        - Binary file: return None (no diff shown)
        - Path outside project root: return None (security)
    """

    def read_file_bytes(self, path: str) -> bytes | None:
        """Read file bytes, returning None if file does not exist or is unreadable."""
        try:
            p = pathlib.Path(path)
            if not p.exists():
                return b""  # new file — before state is empty
            return p.read_bytes()
        except (OSError, PermissionError):
            return None

    def compute(
        self,
        entry: "ToolCallEntry",
        result: dict[str, object],
    ) -> str | None:
        """
        Compute a unified diff string from the before-snapshot and after state.

        Returns None if:
            - entry.file_snapshot_before is None (snapshot not taken)
            - The file is binary (null bytes in content)
            - The tool did not produce a path in result

        Returns "" (empty string) if before == after (no change).
        """
        path = str(entry.args.get("path", result.get("path", "")))
        if not path:
            return None

        before_bytes = entry.file_snapshot_before
        if before_bytes is None:
            return None

        # Check if binary
        if b"\x00" in before_bytes:
            return None

        # Read after state
        if entry.name == "delete_file":
            after_bytes = b""
        else:
            try:
                after_bytes = pathlib.Path(path).read_bytes()
            except (OSError, FileNotFoundError):
                return None

        if b"\x00" in after_bytes:
            return None

        before_lines = before_bytes.decode("utf-8", errors="replace").splitlines(
            keepends=True
        )
        after_lines = after_bytes.decode("utf-8", errors="replace").splitlines(
            keepends=True
        )

        if before_lines == after_lines:
            return ""

        diff_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"{path} (before)",
                tofile=f"{path} (after)",
                lineterm="",
                n=3,
            )
        )
        return "\n".join(diff_lines)
```

---

## 5. Cancellation and Retry

### 5.1 Ctrl+C Cancellation

When the user presses Ctrl+C while a tool call is running:

1. `AppState.current_turn.cancelled = True` is set via a `turn_cancelled` event.
2. `ToolExecutor` detects the cancellation flag and sends `SIGTERM` (then `SIGKILL` after 2s) to any subprocess spawned by shell tools.
3. `on_tool_call_complete` is called with `ok=False, error="cancelled by user"`.
4. Partial output collected up to that point is stored in `entry.output_lines`.
5. The committed line shows: `  ⎿ tool_name(...)  ✗  {elapsed_ms}ms   Error: cancelled by user`
6. Any partial output lines are committed beneath it with the standard expand hint.

**Signal sequence for shell tool cancellation:**

```python
# In run_bash / run_command executor
async def _run_with_cancellation(self, entry: ToolCallEntry, cmd: str) -> dict:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # required for os.killpg
    )
    collected_lines: list[str] = []
    try:
        async for line in proc.stdout:
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            collected_lines.append(decoded)
            self._adapter.on_tool_call_stream_line(entry.tool_use_id, decoded)
        await proc.wait()
    except asyncio.CancelledError:
        # Kill the entire process group
        import os, signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise
    return {
        "exit_code": proc.returncode,
        "output": collected_lines,
    }
```

### 5.2 Retry Mechanism

Retry is not automatic. After a FAILURE state, the agent receives the error as tool output via the standard `ToolResult` protocol and may choose to re-invoke the tool. From the TUI perspective:

- Each retry is a **new** `ToolCallEntry` with a new `tool_use_id`.
- The previous failed entry remains committed in the transcript.
- If the same `(name, args)` combination fails 3 times within the same turn, the `DoomLoopDetector` fires (see master PRD §7.4).

There is no "retry" button in the tool call committed line. Retry is agent-initiated only.

### 5.3 Partial Output on Cancellation

When a tool call is cancelled:

1. `entry.output_lines` is set to whatever was collected in `_stream_buffer` at cancellation time.
2. If `len(output_lines) > 0`, the committed failure line includes partial output with the expand hint:
   ```
     ⎿ run_bash(cmd='pytest tests/')  ✗  1240ms
         Error: cancelled by user
         collected 47 items (dim)
         RUNNING tests/test_auth.py (dim)
         (+43 more — /expand abc12345)
   ```
3. The full partial output is stored and accessible via `/expand {short_id}`.

---

## 6. `/expand` Command

### 6.1 Command Syntax and Resolution

The `/expand` command expands the collapsed output of a completed tool call in the transcript.

**Syntax:** `/expand {tool_use_id[:8]}`

The 8-character prefix uniquely identifies a tool call within a session (probability of collision across N tool calls ≈ N/16^8 ≈ negligible for N < 10,000).

**Command registration** (in `UnifiedCommandRegistry`):

```python
Command(
    name="expand",
    description="Expand a tool call's full output",
    argument_hint="<tool-id>",
    handler=expand_handler,
)
```

### 6.2 Handler Implementation

```python
async def expand_handler(args: str, transcript: TranscriptModel, render_loop: RenderLoop, ...) -> None:
    """
    /expand abc12345
    Finds the tool call entry by short_id prefix. Marks it expanded.
    Re-commits the expanded lines to the transcript.
    """
    short_id = args.strip()
    if not short_id:
        render_loop.force_commit(["Usage: /expand <tool-id>"])
        return

    entry = transcript.find_tool_call_by_short_id(short_id)
    if entry is None:
        render_loop.force_commit([f"No tool call found with id starting with '{short_id}'"])
        return
    if not entry.is_terminal:
        render_loop.force_commit([f"Tool call {short_id!r} is still running"])
        return
    if entry.expanded:
        render_loop.force_commit([f"Tool call {short_id!r} is already expanded"])
        return

    entry.expanded = True

    # Re-render and commit the expanded version
    if entry.state == ToolCallState.SUCCESS:
        new_lines = render_success_committed(entry, color=..., unicode_mode=...)
    else:
        new_lines = render_failure_committed(entry, color=..., unicode_mode=...)

    # Commit the expanded version as new committed lines
    # (the original collapsed lines remain above — this is an append, not a replacement)
    render_loop.force_commit(
        [f"  ── expanded: {entry.name} ({entry.short_id}) ──"] + new_lines
    )
```

### 6.3 Storage of Full Output

Full output is stored in `ToolCallEntry.output_lines` (a plain Python list). This list is unbounded — the OS truncation happens only at render time via the `DEFAULT_OUTPUT_PREVIEW_LINES` constant. The full list persists in `TranscriptModel._tool_call_store: dict[str, ToolCallEntry]` for the lifetime of the session.

Memory budget: at `MAX_LINES_PER_TURN = 500` and a typical line length of 100 bytes, one tool call's max output is 50 KB. With 100 tool calls per session, worst case ≈ 5 MB. Well within the 10 MB transcript budget.

### 6.4 Rendering Expanded State

When `tc.expanded = True`:

- `render_success_committed()` emits all `output_lines` (no truncation hint).
- The diff block (if present) emits all diff lines (no truncation hint).
- The header line shows `[expanded]` suffix in dim cyan.

---

## 7. Parallel Tool Calls

### 7.1 Multiple Tools Running Simultaneously

The AgentHICC parallel DAG executor may execute multiple tool calls concurrently (when two workflow nodes have no dependency between them). When N tool calls are simultaneously in the RUNNING state, the live bottom block shows all N:

```
  ⎿ read_file(path='src/auth.py')       ⠋
  ⎿ read_file(path='tests/test_auth.py') ⠹
  ⎿ grep_files(pattern='datetime.now')   ⠸
```

**Ordering:** Live tool calls are rendered in the order they were created (FIFO by `started_at`). Maximum visible simultaneously: `min(N_running, MAX_LIVE_TOOL_DISPLAY_LINES // 2)` where `MAX_LIVE_TOOL_DISPLAY_LINES = 6` — so at most 3 parallel tools visible at once. When more than 3 are running, a summary line is shown instead:

```
  ⎿ (3 tools running: read_file, run_bash, grep_files)  ⠋
```

### 7.2 Completion Ordering

Parallel tool calls complete in arbitrary order. Each completion is independent:

1. Tool A completes → its committed lines are appended to the transcript.
2. Tool B completes → its committed lines are appended beneath A's lines.
3. Tool C completes → its committed lines are appended beneath B's lines.

The ordering in the transcript reflects completion order, not launch order. This is by design — deterministic ordering is impossible when execution is parallel.

### 7.3 Bottom Block Layout for Parallel Calls

```python
def render_live_tool_calls(
    live_entries: list[ToolCallEntry],
    color: bool,
    unicode_mode: bool,
    cols: int,
    max_display: int = 3,
) -> list[str]:
    """
    Render the live tool calls section of the bottom block.

    When len(live_entries) <= max_display:
        Render each entry on its own line.
    When len(live_entries) > max_display:
        Render a single summary line showing count and names.
    """
    if not live_entries:
        return []

    if len(live_entries) > max_display:
        names = ", ".join(e.name for e in live_entries[:max_display])
        rest = len(live_entries) - max_display
        summary = f"  ({len(live_entries)} tools running: {names}"
        if rest > 0:
            summary += f" +{rest} more"
        summary += ")"
        spinner = SPINNER_BRAILLE[live_entries[0].spinner_frame % len(SPINNER_BRAILLE)]
        if color:
            return [f"\033[2m{summary}\033[0m  \033[36m{spinner}\033[0m"]
        return [f"{summary}  {spinner}"]

    lines: list[str] = []
    for entry in live_entries:
        lines.append(render_running_line(entry, color=color, unicode_mode=unicode_mode))
        # For shell tools: show last streaming line beneath
        if entry.tool_category == "shell":
            tail = entry.stream_buffer_tail(n=1)
            if tail:
                line = tail[0][:cols - 8]
                if color:
                    lines.append(f"      \033[2m{line}\033[0m")
                else:
                    lines.append(f"      {line}")
    return lines
```

---

## 8. Full Test Specification

### 8.1 Unit Tests (65 tests)

All unit tests live in `tests/unit/test_tool_execution.py`. They use `@pytest.mark.unit` and do NOT require asyncio (except where noted). All tests are synchronous unless marked `async`.

---

#### Group A: `ToolCallEntry` Data Model (15 tests)

**A-01** `test_tool_call_entry_initial_state`
- Component: `ToolCallEntry.__init__`
- Input: `ToolCallEntry(tool_use_id="abc123", name="read_file", args={"path": "x.py"})`
- Expected: `state == PENDING`, `duration_ms == 0`, `error == ""`, `expanded == False`, `diff is None`, `started_at == 0.0`

**A-02** `test_mark_running_sets_started_at`
- Component: `ToolCallEntry.mark_running`
- Input: Entry in PENDING state; call `mark_running()`
- Expected: `state == RUNNING`, `started_at > 0.0`, `started_at <= time.monotonic()`

**A-03** `test_mark_running_from_non_pending_raises`
- Component: `ToolCallEntry.mark_running`
- Input: Entry in RUNNING state; call `mark_running()`
- Expected: `AssertionError` raised

**A-04** `test_mark_success_sets_state_and_duration`
- Component: `ToolCallEntry.mark_success`
- Input: Entry in RUNNING state (started_at set); call `mark_success(["line1"], diff=None)`
- Expected: `state == SUCCESS`, `duration_ms > 0`, `output_lines == ["line1"]`, `diff is None`

**A-05** `test_mark_success_stores_diff`
- Component: `ToolCallEntry.mark_success`
- Input: Entry in RUNNING state; call `mark_success([], diff="@@...\n-old\n+new")`
- Expected: `diff == "@@...\n-old\n+new"`

**A-06** `test_mark_failure_sets_error_message`
- Component: `ToolCallEntry.mark_failure`
- Input: Entry in RUNNING state; call `mark_failure("permission denied")`
- Expected: `state == FAILURE`, `error == "permission denied"`, `duration_ms > 0`

**A-07** `test_mark_failure_from_approval_needed`
- Component: `ToolCallEntry.mark_failure`
- Input: Entry in APPROVAL_NEEDED state; call `mark_failure("denied by user")`
- Expected: `state == FAILURE`, `error == "denied by user"`

**A-08** `test_mark_approval_needed_transition`
- Component: `ToolCallEntry.mark_approval_needed`
- Input: Entry in RUNNING state
- Expected: `state == APPROVAL_NEEDED`

**A-09** `test_resume_from_approval_transition`
- Component: `ToolCallEntry.resume_from_approval`
- Input: Entry in APPROVAL_NEEDED state
- Expected: `state == RUNNING`

**A-10** `test_short_id_is_first_8_chars`
- Component: `ToolCallEntry.short_id`
- Input: `tool_use_id = "abcdef1234567890"`
- Expected: `short_id == "abcdef12"`

**A-11** `test_is_live_for_pending_running_approval`
- Component: `ToolCallEntry.is_live`
- Input: Entry in each of PENDING, RUNNING, APPROVAL_NEEDED
- Expected: `is_live == True` for all three

**A-12** `test_is_terminal_for_success_failure`
- Component: `ToolCallEntry.is_terminal`
- Input: Entry in SUCCESS, then FAILURE state
- Expected: `is_terminal == True` for both

**A-13** `test_tool_category_classification`
- Component: `ToolCallEntry.tool_category`
- Input: Names `"read_file"`, `"run_bash"`, `"git_commit"`, `"grep_files"`, `"tool_define"`
- Expected: `"file"`, `"shell"`, `"git"`, `"search"`, `"other"` respectively

**A-14** `test_tick_spinner_cycles`
- Component: `ToolCallEntry.tick_spinner`
- Input: Call `tick_spinner()` 10 times from `spinner_frame=0`
- Expected: `spinner_frame == 0` after 10 ticks (wraps at `len(SPINNER_BRAILLE) == 10`)

**A-15** `test_stream_buffer_ring_eviction`
- Component: `ToolCallEntry.append_stream_line`, `stream_buffer_tail`
- Input: Append `MAX_STREAM_BUFFER_LINES + 5` lines
- Expected: `len(_stream_buffer) == MAX_STREAM_BUFFER_LINES`; `stream_buffer_tail(3)` returns last 3 appended lines

---

#### Group B: `format_args_display` (8 tests)

**B-01** `test_format_single_string_arg`
- Input: `{"path": "src/auth.py"}`
- Expected: `"path='src/auth.py'"`

**B-02** `test_format_truncates_long_string_at_40_chars`
- Input: `{"path": "a" * 50}`
- Expected: contains `"..."`, total arg value portion ≤ 43 chars

**B-03** `test_format_multiple_args`
- Input: `{"path": "a.py", "content": "hello"}`
- Expected: `"path='a.py', content='hello'"`

**B-04** `test_format_non_string_values`
- Input: `{"count": 42, "flag": True}`
- Expected: `"count=42, flag=True"` (no quotes around non-strings)

**B-05** `test_format_total_truncated_to_60_chars`
- Input: `{"arg1": "x" * 30, "arg2": "y" * 30}`
- Expected: result length ≤ 63 chars (60 + `"..."`)

**B-06** `test_format_empty_args`
- Input: `{}`
- Expected: `""`

**B-07** `test_format_single_long_value_truncated`
- Input: `{"cmd": "pytest tests/ -x --tb=short --cov=src --cov-report=html"}`
- Expected: total ≤ 63 chars; ends with `"..."`

**B-08** `test_format_nested_value_uses_repr`
- Input: `{"data": {"key": "val"}}`
- Expected: arg formatted as `repr({"key": "val"})`, truncated to 20 chars

---

#### Group C: Running State Rendering (7 tests)

**C-01** `test_render_running_line_format`
- Input: RUNNING entry; `color=True`, `unicode_mode=True`
- Expected: line starts with `"  "`, contains `"⎿"`, contains tool name, contains braille spinner

**C-02** `test_render_running_line_no_color`
- Input: RUNNING entry; `color=False`, `unicode_mode=True`
- Expected: no ANSI escape sequences (no `\033[`)

**C-03** `test_render_running_line_ascii_mode`
- Input: RUNNING entry; `color=True`, `unicode_mode=False`
- Expected: spinner is one of `|`, `/`, `-`, `\`; prefix is `>`

**C-04** `test_render_running_line_pending_shows_circle`
- Input: PENDING entry; `color=True`
- Expected: contains `"○"` not a spinner character

**C-05** `test_render_running_line_approval_needed`
- Input: APPROVAL_NEEDED entry; `color=True`
- Expected: contains `"awaiting approval"`, `"⚠"` present

**C-06** `test_render_running_line_spinner_advances`
- Input: entry with `spinner_frame=0`; `tick_spinner()` → re-render
- Expected: spinner character changed

**C-07** `test_render_running_line_args_truncated`
- Input: RUNNING entry with args producing 80+ char string
- Expected: line stays within reasonable bounds (args portion ≤ 63 chars)

---

#### Group D: Success State Rendering (12 tests)

**D-01** `test_render_success_header_format`
- Input: SUCCESS entry, 2 output lines, `color=True`, `unicode_mode=True`
- Expected: first line starts with `"  "`, contains `"⎿"`, `"✓"`, `"ms"`

**D-02** `test_render_success_shows_2_output_lines_by_default`
- Input: SUCCESS entry with 5 output lines, `expanded=False`
- Expected: exactly 2 output lines in result (positions 1 and 2)

**D-03** `test_render_success_shows_expand_hint_when_truncated`
- Input: SUCCESS entry with 7 output lines, `expanded=False`
- Expected: result contains a line matching `r"\(\+5 more — /expand [0-9a-f]{8}\)"`

**D-04** `test_render_success_expanded_shows_all_output`
- Input: SUCCESS entry with 7 output lines, `expanded=True`
- Expected: 7 output lines in result; no expand hint

**D-05** `test_render_success_expanded_shows_expanded_marker`
- Input: SUCCESS entry with 3 output lines, `expanded=True`
- Expected: first line contains `"[expanded]"`

**D-06** `test_render_success_no_output_no_hint`
- Input: SUCCESS entry with 0 output lines
- Expected: only 1 line returned (the header); no expand hint

**D-07** `test_render_success_no_color_no_ansi`
- Input: SUCCESS entry, `color=False`
- Expected: no `\033[` sequences in any returned line

**D-08** `test_render_success_duration_in_header`
- Input: SUCCESS entry with `duration_ms=142`
- Expected: first line contains `"142ms"`

**D-09** `test_render_success_with_diff`
- Input: SUCCESS entry with `diff="@@...\n-old\n+new"`, `should_show_diff=True`
- Expected: result contains lines from `render_diff_block()` output

**D-10** `test_render_success_output_lines_are_dim`
- Input: SUCCESS entry with 1 output line, `color=True`
- Expected: output line starts with `"\033[2m"` (dim)

**D-11** `test_render_success_no_diff_for_non_file_tool`
- Input: SUCCESS entry with `name="run_bash"`, `diff` field non-None (should not happen but guarded)
- Expected: diff block not rendered (since `should_show_diff` is False for shell tools)

**D-12** `test_render_success_zero_duration`
- Input: SUCCESS entry with `duration_ms=0`
- Expected: first line contains `"0ms"` (not blank)

---

#### Group E: Failure State Rendering (8 tests)

**E-01** `test_render_failure_header_format`
- Input: FAILURE entry; `color=True`
- Expected: first line contains `"✗"`, not `"✓"`

**E-02** `test_render_failure_shows_error_line`
- Input: FAILURE entry with `error="permission denied"`
- Expected: second line contains `"Error: permission denied"`

**E-03** `test_render_failure_error_line_is_dim_red`
- Input: FAILURE entry; `color=True`
- Expected: second line starts with `"\033[2;31m"` or contains that sequence

**E-04** `test_render_failure_no_color`
- Input: FAILURE entry; `color=False`
- Expected: no `\033[` sequences in any returned line

**E-05** `test_render_failure_with_partial_output`
- Input: FAILURE entry with `output_lines=["partial1", "partial2", "partial3"]`
- Expected: 2 output lines shown + expand hint for 1 remaining

**E-06** `test_render_failure_partial_output_expand_hint_present`
- Input: FAILURE entry with 5 output lines; `expanded=False`
- Expected: result contains expand hint `r"\(\+3 more — /expand"`

**E-07** `test_render_failure_empty_output_no_hint`
- Input: FAILURE entry with 0 output lines
- Expected: only 2 lines (header + error line)

**E-08** `test_render_failure_cancellation_message`
- Input: FAILURE entry with `error="cancelled by user"`
- Expected: second line contains `"cancelled by user"`

---

#### Group F: Diff Rendering (10 tests)

**F-01** `test_render_diff_adds_are_green`
- Input: `diff_text="+new line"`, `color=True`
- Expected: result contains `"\033[32m+new line\033[0m"`

**F-02** `test_render_diff_removes_are_red`
- Input: `diff_text="-old line"`, `color=True`
- Expected: result contains `"\033[31m-old line\033[0m"`

**F-03** `test_render_diff_hunk_header_is_cyan`
- Input: `diff_text="@@ -1,3 +1,3 @@"`, `color=True`
- Expected: result contains `"\033[2;36m@@ -1,3 +1,3 @@\033[0m"`

**F-04** `test_render_diff_context_lines_are_dim`
- Input: `diff_text=" context line"`, `color=True`
- Expected: result contains `"\033[2m context line\033[0m"`

**F-05** `test_render_diff_no_color`
- Input: any `diff_text`, `color=False`
- Expected: no `\033[` sequences

**F-06** `test_render_diff_truncated_at_20_lines`
- Input: `diff_text` with 30 lines, `expanded=False`
- Expected: 20 diff lines shown + separator lines + expand hint; not 30

**F-07** `test_render_diff_expand_hint_shows_remaining`
- Input: `diff_text` with 30 lines, `expanded=False`, `short_id="abc12345"`
- Expected: result contains `"(+10 more lines — /expand abc12345)"`

**F-08** `test_render_diff_expanded_shows_all`
- Input: `diff_text` with 30 lines, `expanded=True`
- Expected: 30 diff lines shown; no expand hint

**F-09** `test_render_diff_file_label_in_header`
- Input: `file_label="src/auth.py"`, `diff_text="+line"`
- Expected: first line contains `"src/auth.py"`

**F-10** `test_render_diff_separator_uses_terminal_cols`
- Input: `cols=60`, any diff
- Expected: separator lines have length ≤ 60 characters (ANSI stripped)

---

#### Group G: `DiffComputer` (5 tests)

**G-01** `test_diff_computer_new_file`
- Input: `file_snapshot_before=b""` (new file); after content = `b"line1\n"`
- Expected: diff shows all lines as additions

**G-02** `test_diff_computer_deleted_file`
- Input: `name="delete_file"`, `file_snapshot_before=b"old\n"`
- Expected: diff shows all lines as removals

**G-03** `test_diff_computer_no_change`
- Input: before and after bytes identical
- Expected: diff is empty string `""`

**G-04** `test_diff_computer_binary_file_returns_none`
- Input: `file_snapshot_before=b"binary\x00data"`
- Expected: `compute()` returns `None`

**G-05** `test_diff_computer_snapshot_not_taken_returns_none`
- Input: `file_snapshot_before=None`
- Expected: `compute()` returns `None`

---

#### Unit Test Totals by Group

| Group | Tests | Component |
|---|---|---|
| A | 15 | `ToolCallEntry` data model |
| B | 8 | `format_args_display` |
| C | 7 | Running state rendering |
| D | 12 | Success state rendering |
| E | 8 | Failure state rendering |
| F | 10 | Diff block rendering |
| G | 5 | `DiffComputer` |
| **Total** | **65** | |

### 8.2 Integration Tests (25 tests)

All integration tests live in `tests/integration/test_tool_execution_integration.py`. They use `@pytest.mark.integration` and require asyncio (`asyncio_mode = "auto"`).

These tests use a `FakeTerminal` (from master PRD §9.1) and a real `TranscriptModel`, `RenderLoop`, and `TUIEventAdapter`. They verify signal sequences → rendered output.

---

**I-01** `test_tool_started_appears_in_live_block`
- Scenario: Emit `ToolCallStarted` for `read_file`. Tick the render loop once.
- Signal sequence: `on_tool_call_started("turn1", "id1", "read_file", {"path": "x.py"})`
- Expected: `FakeTerminal.bottom_history[-1]` contains a row with `"read_file"` and `"○"` (pending)

**I-02** `test_tool_running_shows_spinner`
- Scenario: Emit started then running for `read_file`. Tick twice.
- Signal sequence: `on_tool_call_started(...)`, then `on_tool_call_running("id1")`
- Expected: bottom block contains a row with a braille spinner char

**I-03** `test_tool_success_committed_not_in_live_block`
- Scenario: Complete lifecycle for `read_file` with SUCCESS.
- Signal sequence: started → running → complete(ok=True)
- Expected: `FakeTerminal.committed_lines` contains a line with `"✓"` and `"read_file"`; bottom block no longer contains that tool call

**I-04** `test_tool_failure_committed_with_error`
- Scenario: Complete lifecycle for `run_bash` with FAILURE.
- Signal sequence: started → running → complete(ok=False, error="exit code 1")
- Expected: committed line contains `"✗"` and `"exit code 1"`; bottom block is clean

**I-05** `test_two_parallel_tools_both_in_live_block`
- Scenario: Start two tools simultaneously without completing either.
- Signal sequence: `started(id1, "read_file", ...)`, `started(id2, "grep_files", ...)`; both `running`
- Expected: bottom block contains rows for both tool names

**I-06** `test_parallel_tools_complete_in_arbitrary_order`
- Scenario: Start A and B; complete B first, then A.
- Expected: committed lines contain B's committed line before A's

**I-07** `test_more_than_3_parallel_tools_shows_summary`
- Scenario: Start 4 tools simultaneously, all in RUNNING state.
- Expected: bottom block contains a summary line with `"(4 tools running:"` format

**I-08** `test_shell_streaming_lines_appear_in_live_block`
- Scenario: `run_bash` RUNNING; call `on_tool_call_stream_line` 3 times.
- Expected: bottom block contains at least one of the streamed lines in dim

**I-09** `test_shell_streaming_lines_not_committed`
- Scenario: `run_bash` RUNNING with 5 streamed lines, then SUCCESS.
- Expected: `committed_lines` does NOT contain the intermediate streaming lines

**I-10** `test_file_tool_diff_computed_on_success`
- Scenario: `write_file` lifecycle with DiffComputer mocked to return a diff string.
- Expected: committed lines include diff block lines (contains `"@@ "`)

**I-11** `test_diff_not_computed_for_shell_tool`
- Scenario: `run_bash` SUCCESS; DiffComputer should NOT be called.
- Expected: no diff lines in committed output; `DiffComputer.compute` call count == 0

**I-12** `test_approval_needed_pauses_in_live_block`
- Scenario: Tool starts, runs, then transitions to APPROVAL_NEEDED.
- Expected: bottom block contains `"⚠"` and `"awaiting approval"`

**I-13** `test_approval_granted_resumes_tool`
- Scenario: APPROVAL_NEEDED → user key `"y"` → RUNNING.
- Expected: bottom block transitions back to spinner state

**I-14** `test_approval_denied_commits_failure`
- Scenario: APPROVAL_NEEDED → user key `"n"`.
- Expected: committed line with `"✗"` and `"denied by user"` error

**I-15** `test_expand_command_reveals_all_output`
- Scenario: `read_file` SUCCESS with 10 output lines; `/expand {short_id}` invoked.
- Expected: subsequent committed lines contain all 10 lines; expansion marker present

**I-16** `test_expand_command_unknown_id_shows_error`
- Scenario: `/expand zzzzzzzz` with no matching tool call.
- Expected: committed line contains `"No tool call found"`

**I-17** `test_expand_command_live_tool_shows_error`
- Scenario: Tool is still RUNNING; `/expand {short_id}` invoked.
- Expected: committed line contains `"still running"`

**I-18** `test_spinner_advances_on_each_render_tick`
- Scenario: Tool in RUNNING state; tick render loop 12 times.
- Expected: spinner characters change across ticks; full cycle observed

**I-19** `test_sigwinch_redraws_tool_calls_at_new_width`
- Scenario: Tool RUNNING; terminal resize to 60 cols via SIGWINCH; tick.
- Expected: bottom block line lengths ≤ 60 chars

**I-20** `test_cancellation_commits_partial_output`
- Scenario: `run_bash` RUNNING with 5 streamed lines; `mark_failure("cancelled by user")` called.
- Expected: committed lines contain partial output + expand hint for remaining lines

**I-21** `test_multiple_turns_tool_calls_isolated`
- Scenario: Turn 1 has `read_file` SUCCESS; Turn 2 starts `write_file`.
- Expected: Turn 2's live block does not contain Turn 1's committed lines; each turn's tool calls are independent.

**I-22** `test_search_tool_truncates_at_50_matches`
- Scenario: `grep_files` SUCCESS with 60 match lines in result.
- Expected: `output_lines` contains at most 50 lines; expand hint shows "+10 more"

**I-23** `test_git_tool_no_diff_for_read_ops`
- Scenario: `git_status` SUCCESS (read-only); no file snapshot taken.
- Expected: `entry.diff is None`; no diff block in committed output

**I-24** `test_git_diff_tool_shows_diff_in_committed`
- Scenario: `git_diff` SUCCESS with diff content in result.
- Expected: committed output contains `"@@"` hunk markers

**I-25** `test_tool_call_entry_evicted_after_commit`
- Scenario: Tool completes; verify `TranscriptModel._live_tool_calls` no longer contains the entry.
- Expected: `transcript.find_live_tool_call(tool_use_id) is None` after completion

### 8.3 End-to-End Tests (15 tests)

All E2E tests live in `tests/e2e/test_tool_execution_e2e.py`. They use `@pytest.mark.e2e` and run the full TUI via `pyte` virtual terminal.

Each test scenario is described with: (a) user scenario, (b) terminal sequence, (c) pass criteria.

---

**E2E-01** `test_read_file_complete_cycle`
- User scenario: Agent reads a Python file; user observes the tool call appear and commit.
- Terminal sequence: Inject `ToolCallStarted` → `ToolCallRunning` → `ToolCallComplete(ok=True, lines=42)`
- Pass criteria: pyte screen contains `"read_file"`, `"✓"`, `"42"` in committed region; no tool call visible in bottom block rows.

**E2E-02** `test_write_file_shows_diff_before_commit`
- User scenario: Agent writes a file; diff appears in committed transcript.
- Terminal sequence: started → running (with before-snapshot) → complete with diff
- Pass criteria: pyte screen contains `"@@"`, `"+"` green lines, `"-"` red lines in committed region.

**E2E-03** `test_run_bash_streams_output_in_live_block`
- User scenario: Agent runs pytest; user sees streaming output in bottom block.
- Terminal sequence: started → running → 3 stream lines → complete
- Pass criteria: During running, pyte screen bottom rows contain at least one streamed line; after completion, bottom rows no longer contain it.

**E2E-04** `test_run_bash_failure_shows_exit_code`
- User scenario: pytest fails; user sees failure committed to transcript.
- Terminal sequence: started → running → complete(ok=False, error="exit code 1")
- Pass criteria: pyte screen committed region contains `"✗"` and `"exit code 1"`.

**E2E-05** `test_expand_reveals_all_lines`
- User scenario: `read_file` with 20 lines; user types `/expand abc12345`.
- Terminal sequence: full lifecycle → inject `/expand` command
- Pass criteria: pyte screen committed region contains `"[expanded]"` and more than 2 output lines.

**E2E-06** `test_parallel_tools_all_visible`
- User scenario: Agent runs 3 simultaneous reads.
- Terminal sequence: start 3 tools → tick without completing any
- Pass criteria: pyte screen bottom block contains 3 lines each with `"⎿"` and a spinner.

**E2E-07** `test_parallel_tools_summary_for_4_plus`
- User scenario: Agent runs 4 simultaneous tools.
- Terminal sequence: start 4 tools → tick
- Pass criteria: pyte screen bottom block contains summary line `"(4 tools running:"`.

**E2E-08** `test_approval_gate_replaces_input_bar`
- User scenario: Agent attempts `write_file` in REVIEW mode; approval gate appears.
- Terminal sequence: started → running → approval_needed
- Pass criteria: pyte screen bottom block contains `"[Y] Allow"` and `"[N] Deny"`.

**E2E-09** `test_approval_granted_tool_completes`
- User scenario: Approval gate shown; user presses `y`; tool completes.
- Terminal sequence: approval_needed → key `y` → complete
- Pass criteria: committed region contains `"✓"`; bottom block no longer shows approval gate.

**E2E-10** `test_approval_denied_failure_committed`
- User scenario: Approval gate shown; user presses `n`.
- Terminal sequence: approval_needed → key `n`
- Pass criteria: committed region contains `"✗"` and `"denied by user"`.

**E2E-11** `test_ctrl_c_cancels_running_tool`
- User scenario: `run_bash` running for 2s; user presses Ctrl+C.
- Terminal sequence: running → SIGINT → failure with partial output
- Pass criteria: committed region contains `"✗"` and `"cancelled by user"`.

**E2E-12** `test_no_color_mode_all_symbols_present`
- User scenario: Session started with `NO_COLOR=1`.
- Terminal sequence: Full lifecycle for `read_file`
- Pass criteria: committed region contains `"✓"` but NO `\033[` sequences anywhere on screen.

**E2E-13** `test_diff_truncated_with_expand_hint`
- User scenario: `patch_file` generates a 50-line diff; only 20 shown by default.
- Terminal sequence: write_file lifecycle with 50-line diff
- Pass criteria: pyte screen shows exactly 20 diff lines + hint `"(+30 more lines — /expand"`.

**E2E-14** `test_long_session_tool_calls_scroll_off`
- User scenario: 10 sequential tool calls all commit; earlier tool calls scroll off screen.
- Terminal sequence: 10 complete tool lifecycles
- Pass criteria: pyte screen bottom block stays at 4 rows; committed region shows most recent tools; total height stable.

**E2E-15** `test_doom_loop_detection_halts_repeated_tool`
- User scenario: Same tool (`run_bash` with same `cmd`) called 3 times in same turn; doom loop banner appears.
- Terminal sequence: 3× `{started → running → failure("exit code 1")}` with identical args
- Pass criteria: After 3rd failure, pyte screen committed region contains `"DOOM LOOP DETECTED"` banner.

---

## 9. Acceptance Criteria

All acceptance criteria are binary (pass/fail) and measurable without subjective judgement.

### 9.1 Data Model

- [ ] `ToolCallEntry` passes all 15 Group A unit tests.
- [ ] All 6 state transitions are tested and pass.
- [ ] `ToolCallState.PENDING → SUCCESS` is not a valid direct transition; test confirms `AssertionError` raised if attempted.
- [ ] `short_id` property returns exactly the first 8 characters of `tool_use_id`.
- [ ] `tool_category` returns one of `"file"`, `"shell"`, `"git"`, `"search"`, `"other"` for all tool names in `FILE_TOOLS`, `SHELL_TOOLS`, `GIT_TOOLS`, `SEARCH_TOOLS`.

### 9.2 Running State Display

- [ ] Running tool call appears in the live bottom block within one render tick (≤ 50ms) of `on_tool_call_running()` being called.
- [ ] Spinner character changes on every tick while tool is in RUNNING state.
- [ ] Shell tool streaming lines appear in the bottom block within 3 lines or 1 tick, whichever comes first.
- [ ] APPROVAL_NEEDED state shows `"⚠"` and `"awaiting approval"` in the bottom block.
- [ ] Maximum 3 parallel tool call lines shown; summary line appears for 4+.

### 9.3 Committed Display

- [ ] SUCCESS committed line appears in `FakeTerminal.committed_lines` exactly once per tool call completion.
- [ ] FAILURE committed line appears in `FakeTerminal.committed_lines` exactly once per tool call failure.
- [ ] No tool call ever appears both in the live bottom block and the committed transcript simultaneously.
- [ ] `render_success_committed()` returns exactly `DEFAULT_OUTPUT_PREVIEW_LINES` output lines when `expanded=False` and total lines > `DEFAULT_OUTPUT_PREVIEW_LINES`.
- [ ] Expand hint includes correct `short_id` and correct remaining count.

### 9.4 Diff Display

- [ ] Diff block is rendered for `write_file`, `patch_file`, `delete_file`, `move_file`, `copy_file`, `git_commit` on SUCCESS.
- [ ] Diff block is NOT rendered for `run_bash`, `run_command`, `read_file`, `search_files`.
- [ ] Added lines contain ANSI green `\033[32m` (when `color=True`).
- [ ] Removed lines contain ANSI red `\033[31m` (when `color=True`).
- [ ] `@@` lines contain ANSI dim cyan `\033[2;36m` (when `color=True`).
- [ ] No ANSI codes present when `color=False` (NO_COLOR mode).
- [ ] Diff truncated at `DEFAULT_DIFF_PREVIEW_LINES = 20` lines when `expanded=False`.
- [ ] Expand hint appears when diff has more than 20 lines.

### 9.5 `/expand` Command

- [ ] `/expand {8_char_id}` successfully marks the entry `expanded=True`.
- [ ] After `/expand`, subsequent committed output contains all `output_lines`.
- [ ] `/expand` on unknown ID commits an error message.
- [ ] `/expand` on a live (still-running) tool commits a "still running" message.
- [ ] `/expand` on already-expanded entry commits an "already expanded" message.

### 9.6 Parallel Tool Calls

- [ ] All integration tests I-05 through I-07 pass.
- [ ] Completion order of parallel tools matches arrival order of `ToolCallComplete` signals, not launch order.
- [ ] After all parallel tools complete, the live bottom block contains no tool call lines.

### 9.7 Cancellation

- [ ] `mark_failure("cancelled by user")` can be called from RUNNING state without assertion error.
- [ ] Partial output lines (from stream buffer) are preserved in `output_lines` on cancellation.
- [ ] Cancelled tool call committed line contains `"✗"` and `"cancelled by user"`.

### 9.8 Performance

- [ ] `render_success_committed()` executes in < 2ms for a 500-line output (benchmarked with `timeit`).
- [ ] `render_diff_block()` executes in < 2ms for a 200-line diff (benchmarked with `timeit`).
- [ ] `format_args_display()` executes in < 0.1ms for any input (benchmarked with `timeit`).
- [ ] `FakeTerminal.write_call_count` increments by exactly 1 per `set_bottom()` call (single write per frame invariant).

### 9.9 Color and Accessibility

- [ ] All 65 unit tests pass with `color=True`.
- [ ] All 65 unit tests pass with `color=False` (no ANSI sequences in output).
- [ ] All symbols (`✓`, `✗`, `⚠`, `⎿`, `○`) are present in NO_COLOR mode — only colors absent.
- [ ] ASCII mode (`unicode_mode=False`) replaces all Unicode symbols with ASCII equivalents and all braille spinners with `|`, `/`, `-`, `\`.

### 9.10 Test Coverage

- [ ] `tests/unit/test_tool_execution.py`: 65 tests, all passing.
- [ ] `tests/integration/test_tool_execution_integration.py`: 25 tests, all passing.
- [ ] `tests/e2e/test_tool_execution_e2e.py`: 15 tests, all passing.
- [ ] `mypy src/agenthicc/tui/tool_execution.py --strict` exits with code 0.
- [ ] `mypy src/agenthicc/tui/diff_computer.py --strict` exits with code 0.
- [ ] `ruff check src/agenthicc/tui/tool_execution.py` exits with code 0.
- [ ] `uv run pytest tests/unit/test_tool_execution.py tests/integration/test_tool_execution_integration.py -q` exits with code 0.

---

## Appendix A: File Layout

```
src/agenthicc/tui/
  tool_execution.py        Primary module — ToolCallEntry, ToolCallState, renderers,
                           format_args_display, render_running_line,
                           render_success_committed, render_failure_committed,
                           render_live_tool_calls, SPINNER_BRAILLE, SPINNER_ASCII,
                           FILE_TOOLS, SHELL_TOOLS, GIT_TOOLS, SEARCH_TOOLS,
                           MAX_STREAM_BUFFER_LINES, DEFAULT_OUTPUT_PREVIEW_LINES,
                           DEFAULT_DIFF_PREVIEW_LINES, MAX_ARGS_DISPLAY_CHARS

  diff_computer.py         DiffComputer class — read_file_bytes, compute

  events.py                TUIEventAdapter (existing) — add on_tool_call_started,
                           on_tool_call_running, on_tool_call_stream_line,
                           on_tool_call_complete

tests/unit/
  test_tool_execution.py   65 unit tests (Groups A–G)

tests/integration/
  test_tool_execution_integration.py    25 integration tests (I-01–I-25)

tests/e2e/
  test_tool_execution_e2e.py            15 E2E tests (E2E-01–E2E-15)
```

## Appendix B: New Effect Types Required

The following new `EffectType` values must be added to `src/agenthicc/kernel/events.py`:

```python
class EffectType(Enum):
    # ... existing types ...
    TOOL_CALL_RUNNING = "tool_call_running"       # NEW: tool executor started
    TOOL_CALL_STREAM_LINE = "tool_call_stream_line"  # NEW: one stdout line available
```

And the corresponding payloads:

```python
# EffectType.TOOL_CALL_RUNNING payload:
{
    "tool_use_id": str,   # matches the ToolCallStarted tool_use_id
}

# EffectType.TOOL_CALL_STREAM_LINE payload:
{
    "tool_use_id": str,
    "line": str,           # one decoded, stripped stdout/stderr line
}
```

The existing `TOOL_CALL_STARTED` and `TOOL_CALL_COMPLETE` effects are unchanged.

## Appendix C: Integration Points with Existing Modules

| Existing module | Change required |
|---|---|
| `src/agenthicc/tui/transcript.py` | Add `add_tool_call(turn_id, entry)`, `remove_live_tool_call(tool_use_id)`, `find_tool_call_by_short_id(short_id)`, `_live_tool_calls: dict[str, ToolCallEntry]`, `_tool_call_store: dict[str, ToolCallEntry]` |
| `src/agenthicc/tui/events.py` | Add 4 new methods documented in §4.2; add `_live_tool_calls: dict[str, ToolCallEntry]`; add `_stream_line_count: dict[str, int]`; inject `DiffComputer` dependency |
| `src/agenthicc/tui/app.py` | Register `/expand` command with `CommandRegistry`; wire `ApprovalGate` key handler to keyboard input |
| `src/agenthicc/kernel/events.py` | Add `TOOL_CALL_RUNNING` and `TOOL_CALL_STREAM_LINE` to `EffectType` |
| `src/agenthicc/tools/executor.py` | Emit `TOOL_CALL_RUNNING` effect before tool execution; emit `TOOL_CALL_STREAM_LINE` effects during shell tool execution |

## Appendix D: Constants Reference

```python
# src/agenthicc/tui/tool_execution.py

MAX_ARGS_DISPLAY_CHARS: Final[int] = 60
# Maximum characters in the args portion of a tool call line.
# Prevents very long argument values from pushing spinners off-screen.

DEFAULT_OUTPUT_PREVIEW_LINES: Final[int] = 2
# Number of output lines shown in the committed transcript before truncation.
# Users see 2 lines as a preview; /expand reveals all.

DEFAULT_DIFF_PREVIEW_LINES: Final[int] = 20
# Number of diff lines shown before the expand hint appears.
# 20 lines covers a typical 3-hunk diff without overwhelming the transcript.

MAX_STREAM_BUFFER_LINES: Final[int] = 200
# Ring buffer size for shell tool streaming output.
# Prevents unbounded memory growth for long-running commands.

MAX_LIVE_TOOL_DISPLAY_LINES: Final[int] = 6
# Maximum rows in the live bottom block dedicated to tool call display.
# When ceil(N_tools × 2) > this, summary line replaces individual entries.
# Keeps bottom block within the 12-row maximum.
```
