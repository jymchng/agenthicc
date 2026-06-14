# Non-Alternate-Screen TUI Architecture
## A Comprehensive Design Reference for AI Coding Agent Terminals

**Hard requirement**: This application MUST NEVER call smcup/rmcup (alternate screen mode).
All output flows into the terminal's normal scrollback buffer.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Core Architecture: Committed + Live Pattern](#2-core-architecture-committed--live-pattern)
3. [Textual Inline Mode Deep Dive](#3-textual-inline-mode-deep-dive)
4. [Transcript Rendering Architecture](#4-transcript-rendering-architecture)
5. [Live Status Architecture](#5-live-status-architecture)
6. [Input Bar Architecture](#6-input-bar-architecture)
7. [Streaming Content Architecture](#7-streaming-content-architecture)
8. [Long Session Management](#8-long-session-management)
9. [Multiplexer Compatibility Guide](#9-multiplexer-compatibility-guide)
10. [Implementation Patterns with Code Examples](#10-implementation-patterns-with-code-examples)
11. [Known Limitations & Mitigations](#11-known-limitations--mitigations)
12. [Testing Strategy for Inline Mode](#12-testing-strategy-for-inline-mode)

---

## 1. Executive Summary

### Why No Alternate Screen Matters

The alternate screen (invoked by smcup, exited by rmcup — the ANSI sequences that
traditional full-screen TUIs like vim, less, and htop use) creates a separate display
buffer. When you exit the application, the terminal switches back to the main buffer and
the entire TUI disappears. This is the wrong model for an AI coding agent because:

**Scrollback is work product.** When an AI agent runs for 3 hours, reads 200 files, and
produces 50 code changes, the scrollback buffer IS the audit trail. The user must be
able to scroll up and read what the agent did, copy outputs, share them in a PR, and
reference past tool calls. Alternate screen destroys this on exit.

**Remote environments lack state.** SSH sessions drop. tmux windows get swapped. Screen
sessions reconnect. When a user reconnects to a dropped SSH session, they expect to
scroll up and see where the agent left off. An alternate-screen app shows nothing.

**Long sessions accumulate context.** An AI coding session can run for hours with
hundreds of tool calls. The terminal's native scrollback (typically 10,000–50,000 lines
in most emulators, unlimited in tmux with `history-limit 0`) handles this gracefully.
An alternate-screen buffer is bounded by the terminal window size.

**CI and logging compatibility.** When `agenthicc` is run in CI, its output should be
capturable via normal stdout redirect. Alternate-screen apps write to the alternate
buffer, which is not captured by `> output.log`.

**The right model: Claude Code's approach.** Tools like Claude Code, Aider, and gh's
copilot extension use what this document calls the "committed + live" pattern: permanent
output scrolls into the main buffer like a normal program's stdout, while a small
dynamic region at the bottom of the screen (the status bar and input prompt) updates in
place using cursor-movement sequences. The application never switches screen buffers.

### The Fundamental Constraint

This architecture assumes that we use the "managed bottom block" pattern throughout.
Specifically:

- Content lines are **committed** (printed once to scrollback, never erased)
- A small **bottom block** (status + input) is **erased and redrawn** each frame
- The cursor is always positioned relative to the bottom block, never via absolute
  screen coordinates
- No DECSTBM scroll regions (these conflict with Rich's width detection and break in
  multiplexers)
- No smcup/rmcup at any level, including in dependencies

---

## 2. Core Architecture: Committed + Live Pattern

### The Pattern

Every no-alternate-screen TUI that handles dynamic content uses a variant of the same
core pattern. Ink.js calls it "static vs. dynamic output". Rich calls it `Live`.
log-update (npm) is a thin implementation of exactly this. The pattern:

```
┌─────────────────────────────────────────────────────────────────────┐
│  SCROLLBACK BUFFER (grows upward, never modified)                   │
│                                                                     │
│  ● assistant (claude-sonnet-4-6)  10:44:05                          │
│    Reading src/auth.py...                                           │
│    ⎿ read_file(path='src/auth.py')  ✓  12ms                         │
│                                                                     │
│  ● assistant (claude-sonnet-4-6)  10:44:09                          │
│    I found the issue. The JWT validation is missing the exp check.  │
│    Here's the fix:                                                  │
│    ⎿ patch_file(path='src/auth.py')  ✓  8ms                         │
│      --- a/src/auth.py                                              │
│      +++ b/src/auth.py                                              │
│      @@ -42,6 +42,9 @@                                              │
│  ...                                                                │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤ ← bottom block starts
│  ⠙ Thinking…  4.2s  │  ↑ 12,847  ↓ 2,341            [status line]  │ ← erased+redrawn
│  ──────────────────────────────────────────────────  [divider]      │ ← erased+redrawn
│  ❯ _                                                [input bar]     │ ← erased+redrawn
└─────────────────────────────────────────────────────────────────────┘
```

The separator between scrollback and the bottom block is invisible — it is simply the
current terminal row where the bottom block begins, tracked by `_bottom_height`.

### The Single Invariant

After every public Terminal method returns, the cursor is at **column 0 of the row
immediately below the last committed content**. All bottom-block rows are below that.
This invariant makes the math simple: to erase the bottom block, move up
`_bottom_height` rows and issue ESC[0J (erase to end of screen).

### Data Flow

```
Agent event / user keystroke
         │
         ▼
   TranscriptModel (single source of truth)
   + StatusState (tokens, cost, elapsed)
   + InputState (buffer, cursor, mode)
         │
         ▼ pure function, no I/O
   FrameComposer
   → Frame(committed: list[str], bottom: list[str])
         │
         ▼ append-only diff
   RenderLoop
   → new_lines = committed[_committed_count:]
   → terminal.commit_lines(new_lines)
   → terminal.set_bottom(bottom)
         │
         ▼
   Terminal (single I/O owner)
   → erase bottom block
   → print committed lines → scrollback
   → redraw bottom block
```

### The Three Content Classes

Every piece of output belongs to exactly one class:

| Class | Written to | Erased? | Examples |
|-------|-----------|---------|----------|
| **Committed** | Scrollback (permanent) | Never | Turn headers, completed tool calls, AI response text |
| **Transient-bottom** | Bottom block | Each frame | Status line, thinking animation, input bar |
| **Transient-committed** | Bottom block, then promoted | On commit | Streaming text during a turn |

The critical insight: **streaming text in progress lives in the bottom block as
transient-committed content.** When a streaming turn ends (stop_reason received), the
partial text is promoted to committed content and the bottom block no longer shows it.
This is what prevents content from "disappearing" when the bottom block is redrawn.

---

## 3. Textual Inline Mode Deep Dive

### What Textual Inline Mode Is

Textual is a Python TUI framework built on Rich. Its default mode uses alternate screen
(smcup/rmcup). As of Textual 0.27+, it offers `App.run(inline=True)` which renders the
app into the current terminal position without switching to alternate screen.

### How Textual Inline Mode Works Internally

`run(inline=True)` passes `inline=True` to the `Driver`. Textual's driver then:

1. Does NOT send smcup (alternate screen enter)
2. Renders the app into a fixed-height "viewport" at the current cursor position
3. Uses cursor-up + ESC[0J to erase and redraw the viewport on every render tick
4. Tracks the viewport height; if the app's height changes, it adjusts accordingly
5. On exit, does NOT send rmcup — the rendered content remains in scrollback

The ANSI sequences Textual uses in inline mode:

```
ESC[{n}A     — cursor up n rows (to start of viewport)
ESC[0J       — erase from cursor to end of screen
{rendered content}   — the app's widgets drawn as ANSI text
```

This is identical to what Rich's `Live` display does and what the log-update npm package
does. The pattern is universal.

### Textual Inline Mode Constraints

**Height must be bounded.** The inline viewport has a fixed maximum height. If your app
tries to render taller than the terminal height, Textual clips it. The transcript content
cannot scroll independently within the viewport — Textual doesn't give you a sub-widget
scrollbar that works in inline mode the way it does in full-screen mode.

**This is a fundamental problem for a long-session AI agent.** If the transcript has
100 turns and the terminal is 40 rows tall, you cannot show all 100 turns in an inline
Textual app — you get at most 40 rows total for everything (including the status bar and
input). The user cannot scroll up to see earlier turns within the Textual viewport; they
would need to scroll the terminal's own scrollback, but the earlier turns were never
committed to scrollback — they're inside the viewport that gets erased each frame.

**Conclusion: Textual's inline mode is NOT suitable for the transcript region.** It is
designed for bounded-height apps (spinners, progress bars, short status panels). For our
use case — an hours-long session with thousands of transcript lines — we need the
committed-to-scrollback model where transcript lines accumulate permanently.

### What Textual Inline Mode IS Good For

The bottom block only. If you want to use Textual for the status bar and input widget,
you can build a small Textual app (3–5 rows tall) that runs inline and manages only the
bottom portion of the screen. The transcript above it is written via normal stdout prints
that commit to scrollback. This is a hybrid approach:

- Transcript: raw `sys.stdout.write` / Rich Console prints (permanent, in scrollback)
- Bottom block: Textual `App.run(inline=True)` with fixed height

The challenge with this hybrid: Textual's inline app and your stdout writes fight over
the terminal cursor position. You need to coordinate so that when Textual redraws its
inline viewport, it does not overwrite your transcript output. This requires knowing
exactly how many rows the transcript has consumed.

**Recommendation**: For simplicity and correctness, do NOT use Textual at all. Use the
pure managed-bottom-block implementation described in Section 10. Textual inline mode
is a valid alternative only if you already have a Textual codebase and want to migrate
incrementally.

### Textual Inline Mode: Widget Support

In inline mode, most Textual widgets work but with constraints:

- `Static` widgets render correctly
- `Input` widget works (it captures keypresses via Textual's key handling)
- `ScrollableContainer` does NOT give the user the ability to scroll with the terminal's
  own scrollback — scrolling within the Textual viewport erases and redraws content
- `DataTable`, `ListView` work within the height constraint
- Animations and transitions work

### If You Must Use Textual

```python
from textual.app import App, ComposeResult
from textual.widgets import Static, Input

class BottomBlockApp(App):
    # Fixed height - only the bottom block
    CSS = """
    Screen {
        height: 5;  /* status + divider + input + footer + padding */
    }
    """
    
    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Static("─" * 80, id="divider")
        yield Input(placeholder="❯ ", id="input")
        yield Static(id="footer")
    
    async def run_with_transcript(self, transcript_printer):
        # Run the app inline; transcript_printer commits lines to scrollback
        # by writing to sys.stdout BEFORE run() is called
        await self.run_async(inline=True, inline_no_clear=False)

# Usage: run the Textual bottom block inline
# but commit transcript lines via sys.stdout.write() before each run() tick
```

`inline_no_clear=True` (Textual 0.40+) keeps the previous render visible instead of
erasing on every frame — useful if you want to "freeze" the display between updates.

---

## 4. Transcript Rendering Architecture

### Design Goals

The transcript renderer must:

1. Commit lines permanently to scrollback (never erase committed content)
2. Render Markdown (bold, italic, code blocks) via ANSI sequences
3. Handle tool call state: pending → running (spinner) → success/failure
4. Show unified diffs for file edits, truncated with a "show more" hint
5. Show @mention chips inline
6. Never corrupt the bottom block by writing past it

### The TranscriptModel as Single Source of Truth

```python
# src/agenthicc/tui/transcript.py

@dataclass
class AgentTurnEntry:
    agent_id: str
    agent_name: str
    timestamp: float
    lines: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    mention_chips: list[MentionChip] = field(default_factory=list)
    finalized: bool = False   # True once the turn's stop_reason has been received

class TranscriptModel:
    turns: list[AgentTurnEntry]
    
    def render_committed(self) -> list[str]:
        """Return only fully finalized lines. Called by FrameComposer._compose_committed().
        
        A turn is committed (finalized=True) once the LLM stop_reason fires.
        Tool calls within a finalized turn are fully committed.
        The CURRENT (in-progress) turn is NOT in committed output — it lives
        in the bottom block as partial_text until finalized.
        """
        lines = []
        for turn in self.turns:
            if not turn.finalized:
                continue  # in-progress turn belongs in bottom block
            lines.extend(self._render_turn(turn))
        return lines
    
    def render_current_partial(self, partial_text: str) -> list[str]:
        """Render the in-progress turn for the bottom block streaming zone."""
        ...
```

### Committed Lines: What Gets Printed

Each finalized turn renders as:

```
● assistant (claude-sonnet-4-6)  10:44:05
  Here is the analysis of your codebase:

  The authentication module uses MD5 for password hashing, which is
  cryptographically broken. I'll fix this now.

  ⎿ read_file(path='src/auth.py')  ✓  12ms
  ⎿ patch_file(path='src/auth.py')  ✓  8ms
      --- a/src/auth.py
      +++ b/src/auth.py
      @@ -42,6 +42,9 @@
       def validate_password(plain, hashed):
      -    return hashlib.md5(plain.encode()).hexdigest() == hashed
      +    return bcrypt.checkpw(plain.encode(), hashed.encode())
  ⎿ @src/auth.py  ✓  3.2 KB

```

The rendering rules:

```python
def _render_turn(self, turn: AgentTurnEntry) -> list[str]:
    lines = []
    
    # Turn header
    ts = time.strftime("%H:%M:%S", time.localtime(turn.timestamp))
    lines.append(f"\x1b[1;36m●\x1b[0m \x1b[1m{turn.agent_name}\x1b[0m  \x1b[2m{ts}\x1b[0m")
    
    # Text lines (Markdown rendered)
    for line in turn.lines:
        if line.startswith(MARKDOWN_SENTINEL):
            lines.extend(_render_markdown(line[len(MARKDOWN_SENTINEL):]))
        else:
            lines.append(f"  {line}")
    
    # Tool calls (only finalized ones in committed output)
    for tc in turn.tool_calls:
        lines.extend(_render_tool_call_committed(tc))
    
    # @mention chips
    for chip in turn.mention_chips:
        lines.extend(_render_chip(chip))
    
    # Trailing blank line between turns
    lines.append("")
    
    return lines
```

### Tool Call Rendering

Tool calls render differently depending on state:

**In the committed transcript** (turn finalized):
```
  ⎿ read_file(path='src/auth.py')  ✓  12ms
  ⎿ patch_file(path='src/auth.py')  ✓  8ms
      --- a/src/auth.py
      +++ b/src/auth.py
      @@ -42,6 +42,9 @@
```

**In the bottom block** (turn in progress, running):
```
  ⎿ patch_file(path='src/auth.py')  ⠙
```

**Diff truncation** — diffs over `MAX_DIFF_LINES` (default 50) are truncated:
```
  ⎿ patch_file(path='src/bigfile.py')  ✓  34ms
      --- a/src/bigfile.py
      +++ b/src/bigfile.py
      @@ -1,3 +1,4 @@
       line 1
      +new line
       line 2
      ... (+47 more lines — /expand abc12345)
```

### Markdown Rendering

Use Rich's Markdown renderer to convert markdown to ANSI, but wrap it to a known width
before committing so that line counts are predictable:

```python
from rich.console import Console
from rich.markdown import Markdown
import io

def _render_markdown(text: str, width: int = 80) -> list[str]:
    """Render markdown to ANSI lines, clipped to width."""
    buf = io.StringIO()
    console = Console(file=buf, width=width, highlight=False,
                      markup=False, force_terminal=True)
    console.print(Markdown(text))
    raw = buf.getvalue()
    # Split on newlines, preserve ANSI, strip trailing blank
    return [line for line in raw.splitlines()]
```

**Critical**: always render Markdown to the ACTUAL terminal width (queried from the
Terminal object), not a hardcoded value. If you hardcode 80 and the terminal is 120
columns wide, Rich will produce 80-column output which looks wrong. If you hardcode 80
and the terminal is 60 columns wide, the lines extend past the right edge.

### Pagination and Truncation for Large Outputs

Long tool outputs (e.g., a `run_bash` that produces 10,000 lines of test output) must
not fill the scrollback with useless noise:

```python
MAX_TOOL_OUTPUT_LINES = 100
MAX_DIFF_LINES = 50
MAX_MENTION_CHARS = 16_000

def _render_tool_output(tc: ToolCallEntry, width: int) -> list[str]:
    if not tc.output_lines:
        return []
    
    if tc.expanded or len(tc.output_lines) <= MAX_TOOL_OUTPUT_LINES:
        visible = tc.output_lines
        truncated = False
    else:
        visible = tc.output_lines[:MAX_TOOL_OUTPUT_LINES]
        truncated = True
    
    result = []
    for line in visible:
        # Clip to width, preserving ANSI
        result.append(f"      \x1b[2m{_clip_ansi(line, width - 6)}\x1b[0m")
    
    if truncated:
        extra = len(tc.output_lines) - MAX_TOOL_OUTPUT_LINES
        short_id = tc.tool_use_id[:8]
        result.append(f"      \x1b[2m(+{extra} more lines — /expand {short_id})\x1b[0m")
    
    return result
```

---

## 5. Live Status Architecture

### The Bottom Block Structure

The bottom block is the only part of the display that updates dynamically. Its structure
is always:

```
[streaming text zone — optional]        ← only during agent turn with partial_text
[status line]                           ← always present
[divider]                               ← always present  
[input rows]                            ← always present
[mode footer]                           ← always present
```

The block height is variable: it grows when the input buffer has multiple lines, when
the streaming text zone is active, or when a dropdown menu is shown.

### Status Line States

**Active (agent thinking)**:
```
 ⠙ Thinking…  12.4s  │  ↑ 48,320  ↓ 2,341
```

Components:
- Braille spinner frame (8 frames, advances each render tick)
- "Thinking…" with a bold character sweeping L→R→L ("thinking wave")
- Elapsed seconds since turn start
- `│` separator
- Input tokens (↑, cyan)
- Output tokens (↓, green)

**Idle (between turns)**:
```
 claude-sonnet-4-6  │  3 turns  │  $0.138  ↑ 1,037,670  ↓ 10,911
```

Components:
- Model name / session ID
- Turn count
- Cumulative cost
- Cumulative tokens

**Rendering the thinking animation**:

```python
_THINKING_TEXT = "Thinking…"
_THINKING_LEN = len(_THINKING_TEXT)

def _thinking_wave(frame: int) -> str:
    """Bold char sweeps L→R then R→L through 'Thinking…'."""
    cycle = 2 * (_THINKING_LEN - 1)
    pos = frame % cycle
    if pos >= _THINKING_LEN:
        pos = cycle - pos
    result = ""
    for i, ch in enumerate(_THINKING_TEXT):
        if i == pos:
            result += f"\x1b[1m{ch}\x1b[22m"  # bold on → bold off
        else:
            result += ch
    return result

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

def render_status_active(status: StatusState, frame: int, width: int) -> str:
    spinner = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
    thinking = _thinking_wave(frame)
    elapsed = time.monotonic() - status.intent_started_at
    tokens_in = f"\x1b[36m↑ {status.input_tokens:,}\x1b[0m"
    tokens_out = f"\x1b[32m↓ {status.output_tokens:,}\x1b[0m"
    line = f" {spinner} {thinking}  \x1b[2m{elapsed:.1f}s\x1b[0m  \x1b[2m│\x1b[0m  {tokens_in}  {tokens_out}"
    return _clip_ansi(line, width)

def render_status_idle(status: StatusState, width: int) -> str:
    parts = [
        f"\x1b[2m{status.session_id}\x1b[0m",
        f"\x1b[2m{status.completed_agents} turns\x1b[0m",
        f"\x1b[2m${status.session_cost_usd:.3f}\x1b[0m",
        f"\x1b[36m↑ {status.input_tokens:,}\x1b[0m",
        f"\x1b[32m↓ {status.output_tokens:,}\x1b[0m",
    ]
    line = "  " + "  \x1b[2m│\x1b[0m  ".join(parts)
    return _clip_ansi(line, width)
```

### Streaming Text Zone

When the agent is streaming text (between chunks), the partial text appears in the
bottom block above the status line, wrapped to terminal width, dimmed:

```python
def _compose_streaming_zone(partial_text: str, width: int) -> list[str]:
    if not partial_text:
        return []
    
    # Wrap to width - 2 (for "  " indent), dim styling
    wrapped = _wrap_text(partial_text, width - 2)
    return [f"  \x1b[2m{line}\x1b[0m" for line in wrapped[-MAX_STREAMING_ROWS:]]
```

`MAX_STREAMING_ROWS` (default 8) caps how many rows the streaming zone can occupy.
This prevents the streaming zone from pushing the input bar off-screen during rapid,
multi-paragraph streaming.

### The Divider

A simple horizontal rule at full terminal width:

```python
def _render_divider(width: int) -> str:
    return f"\x1b[2m{'─' * width}\x1b[0m"
```

Always the full width (re-queried each frame since terminal size can change).

---

## 6. Input Bar Architecture

### CBREAK Mode vs Prompt_toolkit

The current architecture uses raw CBREAK mode with custom key parsing. This avoids
prompt_toolkit's alternate-screen tendencies (prompt_toolkit patches stdout in ways
that can cause issues) but requires implementing readline-style editing from scratch.

The input bar occupies the bottom N rows of the bottom block, where N is:
- 1 for a single-line input
- 1 + (number of `\n` in buffer) for multi-line input
- Plus 1 for the mode footer

### Key Handling

```python
def _read_key(fd: int) -> tuple[Key, str]:
    """Read one keystroke from a CBREAK fd."""
    b = os.read(fd, 1)
    
    if b == b'\x1b':  # escape sequence
        # Try to read the rest with 50ms timeout
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return Key.ESC, ""
        seq = b + os.read(fd, 6)
        return _decode_escape(seq)
    
    if b == b'\r' or b == b'\n':
        return Key.ENTER, ""
    if b == b'\x03':
        return Key.CTRL_C, ""
    if b == b'\x04':
        return Key.CTRL_D, ""
    if b == b'\x7f' or b == b'\x08':
        return Key.BACKSPACE, ""
    if b == b'\t':
        return Key.TAB, ""
    if b == b'\x1b[Z':  # shift+tab (some terminals)
        return Key.SHIFT_TAB, ""
    
    # Decode printable UTF-8 character (may be multi-byte)
    try:
        char = b.decode('utf-8')
        if char.isprintable():
            return Key.CHAR, char
    except UnicodeDecodeError:
        # Read remaining bytes of multi-byte sequence
        ...
    
    return Key.UNKNOWN, ""
```

### Multi-line Input

The buffer is a plain string with embedded `\n` characters. Alt+Enter or
backslash+Enter appends `\n` to the buffer. Enter on a non-`\n`-containing buffer
submits.

```python
@dataclass
class InputState:
    buffer: str = ""
    cursor: int = 0       # byte offset into buffer
    history: list[str] = field(default_factory=list)
    history_idx: int = -1
    
    def render_lines(self, prompt: str, width: int) -> list[str]:
        """Render the input buffer as display lines."""
        prompt_str = f"\x1b[1;32m❯\x1b[0m "
        prompt_width = 2  # "❯ " is 2 display columns
        
        raw_lines = self.buffer.split('\n') if self.buffer else [""]
        result = []
        
        for i, line in enumerate(raw_lines):
            if i == 0:
                prefix = prompt_str
                available = width - prompt_width
            else:
                prefix = "  "  # continuation indent
                available = width - 2
            
            # Clip to available width
            display = _clip_display(line, available)
            result.append(prefix + display)
        
        return result
```

### The Mode Footer

```python
def _render_mode_footer(mode_name: str, width: int) -> str:
    if mode_name == "Auto":
        return f"\x1b[2m  ⏵⏵ Auto  (shift+tab to cycle)\x1b[0m"
    else:
        return f"\x1b[2m  ⏵⏵ \x1b[0m\x1b[1m{mode_name}\x1b[0m\x1b[2m  (shift+tab to cycle)\x1b[0m"
```

### Dropdown Menus in the Bottom Block

When `@` is typed, the @mention file picker appears. When `/` is typed, the slash
command dropdown appears. These are rendered as additional rows WITHIN the bottom
block, above the input bar:

```
┌─────────────────────────────────────────────────────────┐
│                                          [scrollback]    │
├─────────────────────────────────────────────────────────┤
│  ⠙ Thinking…  4.2s  │  ↑ 12,847  ↓ 341   [status]      │
│  ────────────────────────────────────── [divider]        │
│  📄 src/auth.py                         [dropdown row 1] │
│  📄 src/auth_utils.py                   [dropdown row 2] │
│  📄 src/models/user.py                  [dropdown row 3] │
│  ──                                     [dropdown end]   │
│  ❯ @src/auth  _                         [input bar]      │
│    ⏵⏵ Auto  (shift+tab to cycle)        [mode footer]    │
└─────────────────────────────────────────────────────────┘
```

The dropdown is capped at `MAX_VISIBLE = 8` items and is included in the bottom block's
`list[str]` returned by `FrameComposer._compose_bottom()`. Since `Terminal.set_bottom()`
erases and redraws the entire bottom block each frame, the dropdown appears and
disappears atomically — no cursor corruption.

---

## 7. Streaming Content Architecture

### The Problem

AI models stream output as a series of text chunks. Each chunk arrives asynchronously.
Between chunks, we must update the display without:
- Flickering (rapid erase/redraw visible to the user)
- Corrupting scrollback (writing to rows above the bottom block)
- Missing chunks (falling behind the stream)
- Garbling output (interleaved writes from concurrent async tasks)

### The Solution: Debounced Render Ticks

The RenderLoop runs on a timer, not on every chunk arrival. Chunks accumulate in
`StatusState.partial_text`; the render loop fires at most once every 50ms (matching
Ink.js's debounce interval):

```python
class RenderLoop:
    MIN_TICK_INTERVAL = 0.05  # 50ms — matches Ink's default debounce
    
    def __init__(self, terminal: Terminal, composer: FrameComposer):
        self.terminal = terminal
        self.composer = composer
        self._committed_count = 0
        self._last_tick = 0.0
        self._frame = 0
    
    def tick(self, model: TranscriptModel, status: StatusState,
             input_state: InputState | None) -> None:
        """Called after every model mutation. Debounces rapid updates."""
        now = time.monotonic()
        if now - self._last_tick < self.MIN_TICK_INTERVAL:
            return
        self._render(model, status, input_state)
        self._last_tick = now
        self._frame += 1
    
    def force_commit(self, model: TranscriptModel, status: StatusState,
                     input_state: InputState | None) -> None:
        """Force immediate render regardless of debounce. Call at turn end."""
        self._render(model, status, input_state)
        self._frame += 1
    
    def _render(self, model, status, input_state):
        frame = self.composer.compose(model, status, input_state, self._frame)
        
        new_lines = frame.committed[self._committed_count:]
        if new_lines or frame.bottom != self._last_bottom:
            self.terminal.commit_lines(new_lines)
            self._committed_count = len(frame.committed)
            self.terminal.set_bottom(frame.bottom)
            self._last_bottom = frame.bottom
```

### Streaming Text Display Strategy

During a turn, partial_text accumulates in the bottom block's streaming zone:

```python
# In the stream loop:
async for chunk in stream:
    if chunk.delta:
        status.partial_text += chunk.delta
        render_loop.tick(transcript, status, None)
    
    if chunk.stop_reason is not None:
        # Promote partial text to transcript
        transcript.append_line(agent_id, MARKDOWN_SENTINEL + "".join(current_turn))
        transcript.turns[-1].finalized = True
        status.partial_text = ""
        render_loop.force_commit(transcript, status, None)
```

This ensures:
1. During streaming: text appears in the bottom block, updated every 50ms
2. At stop_reason: text is committed to scrollback permanently
3. The bottom block's streaming zone is cleared when the turn ends

### Atomic Bottom Block Updates

The critical property of `Terminal.set_bottom()` is that it is atomic at the ANSI
sequence level. The erase and redraw happen in a single write() call:

```python
def set_bottom(self, rows: list[str]) -> None:
    """Atomically erase the old bottom block and draw the new one."""
    new_height = len(rows)
    
    # Build the entire update as one string to minimize flickering
    buf = []
    
    # Move to start of bottom block
    if self._bottom_height > 0:
        buf.append(f"\x1b[{self._bottom_height}A")  # cursor up
    buf.append("\x1b[0J")  # erase to end of screen
    
    # Write new bottom block
    for i, row in enumerate(rows):
        buf.append(row)
        if i < len(rows) - 1:
            buf.append("\n")
    
    # Single write() call for atomicity
    self._out.write("".join(buf))
    self._out.flush()
    self._bottom_height = new_height
```

The single `write()` call is important. Two separate `write()` calls (one for erase,
one for content) create a window where the terminal is blank, which causes visible
flicker on slow connections (SSH) or when the system is under load.

### Handling Rapid Tool Call Updates

During a turn, tool calls fire in parallel (`parallel_tool_calls=True`). Multiple
ToolCallStarted and ToolCallComplete signals can arrive within milliseconds. The
debounce (50ms min interval) ensures we don't trigger hundreds of redraws per second,
but each signal still mutates the TranscriptModel immediately:

```python
@signals.on(ToolCallStarted)
async def _on_tool_started(sig):
    transcript.add_tool_call(
        agent_id=agent_id,
        tool_use_id=sig.tool_use_id,
        name=sig.tool_name,
        args=dict(sig.input or {}),
    )
    # tick() respects the debounce — no problem firing it on every signal
    render_loop.tick(transcript, status, None)

@signals.on(ToolCallComplete)
async def _on_tool_complete(sig):
    transcript.finish_tool_call(
        tool_use_id=sig.tool_use_id,
        success=sig.success,
        duration_ms=sig.duration_ms,
        error=sig.error,
    )
    render_loop.tick(transcript, status, None)
```

---

## 8. Long Session Management

### The Core Challenge

A long AI coding session can produce:
- 500+ turns over several hours
- 10,000+ transcript lines
- 50,000+ characters of streaming text
- Hundreds of tool calls with diffs

The terminal emulator's scrollback buffer handles display (iTerm2 defaults to 10,000
rows; kitty, Alacritty, and WezTerm default to much more or unlimited). The challenge
is memory management in the Python process.

### TranscriptModel Memory Budgeting

```python
MAX_TURNS_IN_MEMORY = 200        # beyond this, old turns are archived
MAX_LINES_PER_TURN = 500         # tool outputs are truncated after this
MAX_DIFF_LINES = 50              # unified diffs truncated
MAX_TOOL_OUTPUT_LINES = 100      # non-diff tool output truncated

class TranscriptModel:
    def _evict_old_turns(self) -> None:
        """Remove tool output details from old turns to save memory.
        
        The turn header, text lines, and tool call signatures are preserved
        (they have already been committed to scrollback and re-printing them
        is not needed). Only the output_lines of tool calls are cleared.
        """
        if len(self.turns) <= MAX_TURNS_IN_MEMORY:
            return
        
        # Evict output lines from turns older than the retention window
        evict_before = len(self.turns) - MAX_TURNS_IN_MEMORY
        for turn in self.turns[:evict_before]:
            for tc in turn.tool_calls:
                tc.output_lines = []  # freed; scrollback already has them
            turn._evicted = True
```

### FrameComposer: Committed Line Count vs Memory

The FrameComposer must produce `frame.committed` efficiently. Since old turns have
already been committed to scrollback and `_committed_count` tracks how many lines have
been flushed, the composer does NOT need to re-render old turns on every tick:

```python
class FrameComposer:
    def __init__(self):
        self._committed_cache: list[str] = []
        self._committed_turns_rendered: int = 0
    
    def compose(self, model, status, input_state, frame: int) -> Frame:
        # Only render NEW turns since last compose
        new_turns = model.turns[self._committed_turns_rendered:]
        finalized_new = [t for t in new_turns if t.finalized]
        
        if finalized_new:
            new_lines = []
            for turn in finalized_new:
                new_lines.extend(model._render_turn(turn))
            self._committed_cache.extend(new_lines)
            self._committed_turns_rendered += len(finalized_new)
        
        bottom = self._compose_bottom(model, status, input_state, frame)
        
        return Frame(committed=self._committed_cache, bottom=bottom)
```

This makes `compose()` O(new_turns) rather than O(all_turns), critical for long
sessions.

### Very Long Outputs: Truncation Strategy

For tool outputs that are inherently long (e.g., test suite output with 5,000 lines),
truncation happens at multiple levels:

1. **TranscriptModel level**: `MAX_TOOL_OUTPUT_LINES` lines stored; excess discarded
2. **FrameComposer level**: Output is rendered with a "show more" hint
3. **`/expand` command**: Typing `/expand abc12345` sets `tc.expanded = True`, causing
   the next render to show up to `MAX_TOOL_OUTPUT_LINES` lines
4. **Scrollback level**: Committed lines are never re-printed; the user scrolls up

For truly huge outputs (test suites producing megabytes), consider streaming them to a
temp file and showing a path reference instead:

```python
# When tool output > HUGE_OUTPUT_THRESHOLD:
if len(output) > HUGE_OUTPUT_THRESHOLD:
    path = write_to_temp(output)
    tc.output_lines = [
        f"[output truncated — {len(output):,} bytes]",
        f"Full output: {path}",
        f"  cat {path} | less",
    ]
```

### Session Persistence and Resume

Long sessions should checkpoint their state so a dropped SSH connection or crash
doesn't lose hours of work:

```python
# ConversationStore handles this — see src/agenthicc/conversation_store.py
# Key points for non-alternate-screen:
#
# On resume, the transcript is NOT re-drawn to the terminal.
# Instead, only the most recent N turns are printed to scrollback
# as plain text (not the full ANSI-colored live format).
# This gives the user context without flooding the terminal.

MAX_RESUME_REPLAY_TURNS = 20

def replay_for_resume(session_id: str, conv_store: ConversationStore) -> None:
    """Print the last N turns to scrollback on resume."""
    past = conv_store.load_turns(session_id)[-MAX_RESUME_REPLAY_TURNS:]
    
    console = Console(highlight=False, markup=True)
    console.rule(f"[dim]resumed session {session_id[:12]}[/dim]")
    
    for turn in past:
        if "user" in turn:
            console.print(f"[dim]❯ {turn['user']}[/dim]")
        if "assistant" in turn:
            console.print(f"[bold cyan]●[/bold cyan] assistant")
            console.print(Markdown(turn["assistant"]))
```

---

## 9. Multiplexer Compatibility Guide

### tmux

tmux is the most common multiplexer for remote AI agent sessions. Key considerations:

**TERM variable**: tmux sets `TERM=screen-256color` or `TERM=tmux-256color` by default.
Some applications check for `xterm` and behave differently. For our ANSI sequences
(cursor up, erase to end), this doesn't matter — they're standard VT100 and work in
all these TERM types.

**tmux scrollback**: tmux maintains its own scrollback buffer (`history-limit`, default
2000, recommended to set to 0 for unlimited). The committed lines from our app go into
tmux's scrollback naturally. Users scroll with `Ctrl-b [` (copy mode).

**Terminal width**: tmux reports the width of the smallest attached client. If a user
attaches two clients of different widths, the terminal width returned by
`shutil.get_terminal_size()` may change. Our SIGWINCH handler must handle this. In
tmux, SIGWINCH fires when the pane is resized.

**Double-width characters**: tmux can have issues with double-width Unicode characters
(CJK, some emoji) in its status line. Our divider character `─` (U+2500 BOX DRAWINGS
LIGHT HORIZONTAL) is single-width and safe. The bullet `●` (U+25CF) is safe. Braille
spinner frames `⠋⠙⠹` are single-width.

**Mouse mode**: If the user has enabled tmux mouse mode, mouse scroll events in the
terminal go to tmux, not to our application. This is actually desirable — the user
scrolls the tmux scrollback with the mouse, which shows our committed transcript
correctly.

**Detection**:
```python
def _in_tmux() -> bool:
    return "TMUX" in os.environ

def _in_screen() -> bool:
    return os.environ.get("TERM", "").startswith("screen")

def _in_multiplexer() -> bool:
    return _in_tmux() or _in_screen()
```

**tmux-specific fix for cursor styling**: Some versions of tmux don't pass through
`OSC 12` (cursor color) sequences. Don't set cursor color; it won't work. The braille
spinner in the status line is sufficient visual feedback.

### GNU screen

GNU screen is older and has more limitations:

- screen uses `TERM=screen` which lacks some capabilities
- 256-color support requires `TERM=screen-256color` and appropriate terminfo
- ANSI cursor sequences still work (ESC[nA, ESC[0J)
- screen has its own scrollback (`Ctrl-a [` to enter copy mode)

**Reduced color support**: In screen with limited color depth, our ANSI 256-color codes
may not render. Use `TERM` to detect and fall back to 16-color ANSI:

```python
def _supports_256_colors() -> bool:
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    return "256color" in term or colorterm in ("truecolor", "24bit")

def _color_depth() -> int:
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return 24
    if _supports_256_colors():
        return 256
    return 16
```

### SSH Sessions

In SSH sessions, the key concern is latency. High-latency SSH (>100ms round-trip) makes
the 50ms render debounce insufficient — the user sees stutter on every bottom block
update because each write() travels over the network.

**Mitigation 1**: Batch all ANSI sequences into a single `write()` call (already done
in our `Terminal.set_bottom()` design). A single large write is faster over SSH than
many small writes due to per-packet overhead.

**Mitigation 2**: Increase the debounce interval in high-latency environments:
```python
def _estimate_latency() -> float:
    """Rough RTT estimate via SSH_TTY and SSH_CONNECTION."""
    if not os.environ.get("SSH_CONNECTION"):
        return 0.0
    # Can't know actual RTT without a ping, so use a conservative default
    return 0.1  # assume 100ms

DEBOUNCE_INTERVAL = max(0.05, _estimate_latency())
```

**Mitigation 3**: mosh (mobile shell) handles SSH latency at the protocol level, even
doing predictive local echo. Our app doesn't need to do anything special for mosh.

### TERM Detection and Capability Negotiation

```python
import os
import shutil

def _terminal_capabilities() -> dict:
    """Detect terminal capabilities without alternate screen."""
    term = os.environ.get("TERM", "dumb")
    colorterm = os.environ.get("COLORTERM", "")
    
    caps = {
        "color_depth": 16,
        "unicode": True,
        "cursor_movement": True,
        "sixel": False,
    }
    
    if "256color" in term:
        caps["color_depth"] = 256
    if colorterm in ("truecolor", "24bit"):
        caps["color_depth"] = 16_777_216
    if term == "dumb":
        caps["unicode"] = False
        caps["cursor_movement"] = False
    
    # Check locale for Unicode support
    import locale
    if "utf" not in locale.getpreferredencoding(False).lower():
        caps["unicode"] = False
    
    return caps

def _unicode_safe(text: str, fallback: str) -> str:
    """Use text if Unicode supported, else fallback."""
    caps = _terminal_capabilities()
    return text if caps["unicode"] else fallback

# Usage:
BULLET = _unicode_safe("●", "*")
TOOL_LEADER = _unicode_safe("⎿", "->")
CHECK = _unicode_safe("✓", "OK")
CROSS = _unicode_safe("✗", "FAIL")
DIVIDER_CHAR = _unicode_safe("─", "-")
```

### VS Code Integrated Terminal

VS Code's integrated terminal has excellent ANSI support and behaves like xterm. No
special handling needed. `TERM=xterm-256color` or `TERM_PROGRAM=vscode`.

**Note on VS Code Remote**: When using VS Code Remote (SSH or Dev Containers), the
terminal is still the VS Code integrated terminal — behavior is identical.

### Detecting Alternate Screen Requests from Dependencies

Some dependencies (like Rich's `Console.input()`, prompt_toolkit's `PromptSession`) may
try to use alternate screen. To be safe, audit at startup:

```python
def _check_no_alternate_screen() -> None:
    """Debug helper: verify no dependency has requested alternate screen."""
    # Can be run in debug mode to trace ANSI writes
    original_write = sys.stdout.write
    
    def traced_write(s: str) -> int:
        if "\x1b[?1049h" in s:  # smcup
            import traceback
            raise RuntimeError(
                "Alternate screen mode requested!\n"
                + "".join(traceback.format_stack())
            )
        if "\x1b[?1049l" in s:  # rmcup
            raise RuntimeError("Alternate screen exit requested!")
        return original_write(s)
    
    if os.environ.get("AGENTHICC_DEBUG_NO_ALTSCREEN"):
        sys.stdout.write = traced_write
```

---

## 10. Implementation Patterns with Code Examples

### Pattern 1: Terminal — The Single I/O Owner

```python
# src/agenthicc/tui/terminal.py
from __future__ import annotations

import os
import shutil
import signal
import sys
from dataclasses import dataclass
from typing import IO


@dataclass
class Size:
    rows: int
    cols: int


class Terminal:
    """Single owner of all terminal I/O.
    
    Implements the managed-bottom-block pattern:
    - committed lines scroll into the terminal's main scrollback (permanent)
    - the bottom block (status + input) is erased and redrawn each frame
    
    After every public method returns, the cursor is at column 0 of the
    row immediately below the last committed content (the first row of
    the bottom block).
    """
    
    def __init__(self, out: IO[str] | None = None) -> None:
        self._out = out or sys.stdout
        self._bottom_height = 0
        self._size_dirty = False
        self._size = self._query_size()
        
        # Handle terminal resize
        signal.signal(signal.SIGWINCH, self._on_resize)
    
    def _on_resize(self, signum, frame) -> None:
        self._size_dirty = True
    
    def _query_size(self) -> Size:
        s = shutil.get_terminal_size((80, 24))
        # CRITICAL: use .lines and .columns explicitly, never tuple-unpack.
        # Tuple unpacking gives (columns, lines) which swaps rows and cols.
        return Size(rows=s.lines, cols=s.columns)
    
    @property
    def size(self) -> Size:
        if self._size_dirty:
            self._size = self._query_size()
            self._size_dirty = False
        return self._size
    
    def commit_lines(self, lines: list[str]) -> None:
        """Print lines permanently to scrollback.
        
        Erases the bottom block first, prints the lines (they become
        part of scrollback), then redraws the bottom block below.
        """
        if not lines:
            return
        
        buf = []
        
        # Move to start of bottom block, erase it
        if self._bottom_height > 0:
            buf.append(f"\x1b[{self._bottom_height}A")
        buf.append("\x1b[0J")
        
        # Print lines with newlines (these become scrollback)
        for line in lines:
            buf.append(_clip_to_cols(line, self.size.cols))
            buf.append("\n")
        
        # Redraw the bottom block (if we have one)
        # NOTE: set_bottom() will be called immediately after by RenderLoop
        # No need to redraw here — just leave cursor at the right position
        
        self._out.write("".join(buf))
        self._out.flush()
        # Bottom height is now 0 — set_bottom() will restore it
        self._bottom_height = 0
    
    def set_bottom(self, rows: list[str]) -> None:
        """Atomically erase old bottom block and draw new one.
        
        Single write() call for minimal flicker. The cursor ends up
        at the last character of the last row (bottom of screen).
        """
        if not rows:
            # Clear any existing bottom block
            if self._bottom_height > 0:
                self._out.write(f"\x1b[{self._bottom_height}A\x1b[0J")
                self._out.flush()
                self._bottom_height = 0
            return
        
        width = self.size.cols
        buf = []
        
        # Move to start of bottom block, erase to end of screen
        if self._bottom_height > 0:
            buf.append(f"\x1b[{self._bottom_height}A")
        buf.append("\x1b[0J")
        
        # Write new bottom block rows
        for i, row in enumerate(rows):
            buf.append(_clip_to_cols(row, width))
            if i < len(rows) - 1:
                buf.append("\n")
        # Do NOT append \n after the last row — cursor stays on that row
        
        self._out.write("".join(buf))
        self._out.flush()
        self._bottom_height = len(rows)
    
    def clear_bottom(self) -> None:
        """Erase the bottom block, cursor at col 0 of where block started."""
        if self._bottom_height > 0:
            self._out.write(f"\x1b[{self._bottom_height}A\x1b[0J")
            self._out.flush()
            self._bottom_height = 0
    
    def write_plain(self, text: str) -> None:
        """Write text with no cursor management (for startup messages)."""
        self._out.write(text)
        self._out.flush()
    
    def teardown(self) -> None:
        """Reset terminal state on exit."""
        self.clear_bottom()
        self._out.write("\x1b[?25h")  # show cursor (in case we hid it)
        self._out.write("\x1b[0m")    # reset all ANSI attributes
        self._out.flush()


def _clip_to_cols(text: str, cols: int) -> str:
    """Clip a string (which may contain ANSI escape sequences) to cols display columns."""
    # Strip ANSI for measurement, then clip the original
    import re
    _ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]')
    plain = _ANSI_RE.sub('', text)
    if len(plain) <= cols:
        return text
    # Naive clip — does not handle double-width Unicode
    # For production, use wcwidth
    return text[:cols]


class FakeTerminal(Terminal):
    """Test double: captures all output as a list of committed frames."""
    
    def __init__(self) -> None:
        self._out = None  # type: ignore[assignment]
        self._bottom_height = 0
        self._size = Size(rows=40, cols=120)
        self._size_dirty = False
        self.committed_lines: list[str] = []
        self.bottom_history: list[list[str]] = []
        self._current_bottom: list[str] = []
    
    def commit_lines(self, lines: list[str]) -> None:
        self.committed_lines.extend(lines)
        self._bottom_height = 0
    
    def set_bottom(self, rows: list[str]) -> None:
        self._current_bottom = list(rows)
        self.bottom_history.append(list(rows))
        self._bottom_height = len(rows)
    
    def clear_bottom(self) -> None:
        self._current_bottom = []
        self._bottom_height = 0
    
    def write_plain(self, text: str) -> None:
        pass
    
    def teardown(self) -> None:
        pass
```

### Pattern 2: FrameComposer — Pure Render Function

```python
# src/agenthicc/tui/frame_composer.py
from __future__ import annotations

from dataclasses import dataclass

from .transcript import TranscriptModel
from .app import StatusState


@dataclass
class Frame:
    committed: list[str]   # all lines committed to scrollback so far (append-only)
    bottom: list[str]      # current bottom block rows


class FrameComposer:
    """Pure function: (model, status, input_state, frame) → Frame.
    
    Caches committed output to avoid re-rendering old turns on every tick.
    """
    
    def __init__(self) -> None:
        self._committed_cache: list[str] = []
        self._committed_turns_count: int = 0
    
    def compose(self, model: TranscriptModel, status: StatusState,
                input_state, frame: int) -> Frame:
        # Append any newly finalized turns to the committed cache
        finalized = [t for t in model.turns if t.finalized]
        new_turns = finalized[self._committed_turns_count:]
        
        if new_turns:
            for turn in new_turns:
                self._committed_cache.extend(_render_turn(turn))
            self._committed_turns_count = len(finalized)
        
        bottom = self._compose_bottom(model, status, input_state, frame)
        
        return Frame(committed=list(self._committed_cache), bottom=bottom)
    
    def _compose_bottom(self, model, status, input_state, frame: int) -> list[str]:
        rows = []
        width = 80  # updated from Terminal.size.cols by the caller
        
        # Zone 1: streaming text (only during active agent turn)
        if status.active and status.partial_text:
            streaming_rows = _wrap_text_ansi(
                f"\x1b[2m{status.partial_text}\x1b[0m",
                width - 2, prefix="  "
            )[-8:]  # cap at 8 rows
            rows.extend(streaming_rows)
        
        # Zone 2: status line
        if status.active:
            rows.append(_render_status_active(status, frame, width))
        else:
            rows.append(_render_status_idle(status, width))
        
        # Zone 3: divider
        rows.append(f"\x1b[2m{'─' * width}\x1b[0m")
        
        # Zone 4: input rows
        if input_state is not None:
            rows.extend(input_state.render_lines("❯ ", width))
        else:
            rows.append("\x1b[1;32m❯\x1b[0m ")
        
        # Zone 5: mode footer
        mode = getattr(input_state, 'mode_name', 'Auto') if input_state else 'Auto'
        rows.append(f"\x1b[2m  ⏵⏵ {mode}  (shift+tab to cycle)\x1b[0m")
        
        # Zone 6: dropdown menu (if active)
        if input_state is not None and hasattr(input_state, 'menu') and input_state.menu:
            menu_rows = input_state.menu.render_rows(width)
            # Insert before input rows (after divider)
            divider_idx = rows.index(f"\x1b[2m{'─' * width}\x1b[0m") + 1
            for i, row in enumerate(menu_rows):
                rows.insert(divider_idx + i, row)
        
        return rows
```

### Pattern 3: RenderLoop — Differential Rendering

```python
# src/agenthicc/tui/render_loop.py
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .terminal import Terminal
    from .frame_composer import FrameComposer, Frame
    from .transcript import TranscriptModel
    from .app import StatusState


class RenderLoop:
    """Connects TranscriptModel + StatusState → Terminal via FrameComposer.
    
    Key property: committed lines are only sent to Terminal.commit_lines()
    ONCE, then cached. The bottom block is redrawn every frame.
    """
    
    MIN_TICK_INTERVAL = 0.05  # 50ms debounce
    
    def __init__(self, terminal: "Terminal", composer: "FrameComposer") -> None:
        self.terminal = terminal
        self.composer = composer
        self._committed_count = 0
        self._last_tick = 0.0
        self._frame = 0
        self._last_bottom: list[str] = []
    
    def tick(self, model: "TranscriptModel", status: "StatusState",
             input_state) -> None:
        """Debounced render. Call on every model mutation."""
        now = time.monotonic()
        if now - self._last_tick < self.MIN_TICK_INTERVAL:
            return
        self._do_render(model, status, input_state)
        self._last_tick = now
    
    def force_commit(self, model: "TranscriptModel", status: "StatusState",
                     input_state) -> None:
        """Force render without debounce. Call at turn end."""
        self._do_render(model, status, input_state)
    
    def _do_render(self, model, status, input_state) -> None:
        # Update composer with current terminal width
        width = self.terminal.size.cols
        
        frame = self.composer.compose(model, status, input_state, self._frame)
        self._frame += 1
        
        # Commit any new lines to scrollback
        new_lines = frame.committed[self._committed_count:]
        if new_lines:
            self.terminal.commit_lines(new_lines)
            self._committed_count = len(frame.committed)
        
        # Always refresh the bottom block
        bottom = [_clip_to_cols(r, width) for r in frame.bottom]
        self.terminal.set_bottom(bottom)
        self._last_bottom = bottom
```

### Pattern 4: CBREAK Input Without alternate screen

```python
# Usage pattern — the main input loop

import contextlib
import sys
import termios
import tty

@contextlib.contextmanager
def cbreak_mode():
    """Enable CBREAK on stdin, restore on exit."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        # Read POST-setcbreak settings and layer ECHOCTL + ISIG removal on top
        cur = list(termios.tcgetattr(fd))
        cur[3] &= ~(termios.ECHOCTL | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, cur)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# The main input loop — called during the idle phase (between agent turns)
async def read_user_input(render_loop: RenderLoop, transcript: TranscriptModel,
                           status: StatusState, input_state: InputState) -> str:
    """Read one line of user input, updating the bottom block in real time."""
    fd = sys.stdin.fileno()
    
    with cbreak_mode():
        while True:
            # Redraw bottom block with current input state
            render_loop.force_commit(transcript, status, input_state)
            
            # Read one key (blocking, but the loop is async-friendly via asyncio.to_thread)
            key, char = await asyncio.to_thread(_read_key, fd)
            
            if key == Key.ENTER:
                if '\n' not in input_state.buffer:
                    # Single-line: submit
                    text = input_state.buffer
                    input_state.buffer = ""
                    input_state.cursor = 0
                    return text
                # Multi-line with \n: submit only on empty line?
                # Design choice: require Ctrl+D or a specific key
                
            elif key == Key.CTRL_D:
                # Submit multi-line input
                text = input_state.buffer
                input_state.buffer = ""
                return text
                
            elif key == Key.BACKSPACE:
                if input_state.cursor > 0:
                    # Delete char before cursor
                    b = input_state.buffer
                    c = input_state.cursor
                    input_state.buffer = b[:c-1] + b[c:]
                    input_state.cursor -= 1
                    
            elif key == Key.CHAR:
                input_state.buffer = (input_state.buffer[:input_state.cursor]
                                       + char
                                       + input_state.buffer[input_state.cursor:])
                input_state.cursor += len(char)
            
            # ... other keys
```

### Pattern 5: Integrating Rich Markdown Without Alternate Screen

Rich's `Console` will try to use alternate screen if you use `Console.input()` or if
you pass it `force_interactive=True`. The safe way:

```python
from rich.console import Console
from rich.markdown import Markdown
import io

def render_markdown_to_ansi(text: str, width: int) -> list[str]:
    """Convert Markdown to ANSI lines without any alternate screen calls."""
    # force_terminal=True: prevents Rich from stripping ANSI codes when it
    # thinks stdout is not a TTY (e.g. when piped or in certain SSH sessions)
    # NO_COLOR=False, markup=False: we pass raw markdown, not Rich markup
    buf = io.StringIO()
    console = Console(
        file=buf,
        width=width,
        highlight=False,
        markup=False,
        force_terminal=True,
    )
    console.print(Markdown(text))
    result = buf.getvalue()
    # Trim trailing blank line that Rich always adds
    lines = result.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines

# Safe: use Rich's Console only for printing committed lines
# (this writes to stdout naturally without alternate screen)
committed_console = Console(
    highlight=False,
    markup=True,
    force_terminal=True,
)
committed_console.print(f"[bold cyan]●[/bold cyan] [bold]{turn.agent_name}[/bold]")
```

**DO NOT use `rich.live.Live`** for the bottom block. Rich's Live internally checks
if the terminal supports alternate screen and may use it. Instead, implement
`Terminal.set_bottom()` directly as shown above. If you use Rich Live:

```python
# AVOID: Rich's Live may use alternate screen on some terminals
from rich.live import Live
with Live(renderable, screen=False):  # screen=False is important!
    ...

# But even with screen=False, Live does some cursor tracking that can
# conflict with our own bottom-block management. Prefer the raw Terminal approach.
```

### Pattern 6: Spinner Without Alternate Screen

```python
import asyncio
import time

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

async def animate_spinner(status: StatusState, render_loop: RenderLoop,
                           transcript: TranscriptModel) -> None:
    """Background task: advance spinner frame every 100ms during active turns."""
    while status.active:
        status.spinner_frame += 1
        render_loop.tick(transcript, status, None)
        await asyncio.sleep(0.1)
```

The spinner is just a frame counter that the FrameComposer reads when composing the
status line. No separate terminal manipulation needed.

---

## 11. Known Limitations & Mitigations

### Limitation 1: Terminal Resize During Agent Turn

**Problem**: When the user resizes the terminal during an active agent turn, the
bottom block height may change (especially if streaming text wraps differently).
The `_bottom_height` counter can desync.

**Mitigation**: SIGWINCH handler sets `_size_dirty = True`. The next `tick()` or
`force_commit()` will query the new size and call `set_bottom()` with the correct
new layout. The brief flash between resize and next tick is unavoidable (50ms max).

For immediate response to resize, trigger a render directly in the SIGWINCH handler:
```python
def _on_resize(self, signum, frame):
    self._size_dirty = True
    # Force immediate redraw (safe from signal handler — just a write())
    if self._render_loop:
        self._render_loop.force_commit_sync()
```

### Limitation 2: Long Lines Wrapping

**Problem**: If a committed line is longer than the terminal width, the terminal wraps
it, consuming multiple rows. Our `_bottom_height` counter doesn't know about this
wrapping, so after several wrapped lines, the bottom block appears at the wrong
position.

**Mitigation**: Always clip committed lines to `terminal.size.cols` before committing.
The `_clip_to_cols()` function handles this. For Markdown output, render at the exact
terminal width.

**Edge case**: A line rendered at width=120 is committed. User then resizes terminal
to width=80. The old committed line now wraps to 2 rows. But since committed lines
are in scrollback (never re-rendered), we can't fix this. The bottom block position
will be off by 1 row per wrapped committed line.

**Mitigation**: Accept this as a known limitation. On resize, we can force a full
redraw by clearing all bottom state and re-printing from scratch. This is jarring
but correct. Alternatively, track the number of wrapped rows per committed line
and adjust `_bottom_height` accordingly on resize.

### Limitation 3: No Independent Transcript Scrolling

**Problem**: In alternate-screen TUIs, you can implement a scrollable transcript
region with its own scrollbar. In our model, the transcript is in the terminal's
native scrollback, which the user scrolls with the terminal's own scroll mechanism.

**This is actually the correct behavior** for our use case. The user uses their
terminal emulator's scroll (mouse wheel, Shift+PageUp, tmux copy mode) to review
history. No special implementation needed.

**The only limitation**: The user cannot scroll "within" the app and then resume
typing without switching back. In practice, terminal users understand this — scrolling
the scrollback is a native terminal operation, not an app operation.

### Limitation 4: Rich Console Width Detection

**Problem**: When stdout is being manipulated (by CBREAK mode, when patched by
prompt_toolkit, etc.), Rich may detect the wrong width or conclude it's not a TTY.

**Mitigation**: Always pass `force_terminal=True` and an explicit `width` to Rich's
`Console`. Never use `Console()` with default detection in the TUI code path.

### Limitation 5: Concurrent Writes from Asyncio Tasks

**Problem**: If two asyncio tasks both call `terminal.set_bottom()` concurrently, the
writes can interleave and corrupt the display.

**Mitigation**: The `RenderLoop` is the only caller of `Terminal` methods. All model
mutations go through `TranscriptModel` and `StatusState` (which are plain Python
objects, mutated in the single-threaded asyncio event loop). The `RenderLoop.tick()`
is debounced, so at most one render is in flight at any time. No lock needed because
asyncio is single-threaded.

**The rule**: `Terminal.commit_lines()` and `Terminal.set_bottom()` are ONLY called
from `RenderLoop._do_render()`. Nothing else calls them.

### Limitation 6: Multiplexer Scrollback Size

**Problem**: tmux's default `history-limit` is 2000 lines. A long session with many
tool calls may push old turns out of the tmux scrollback.

**Mitigation**: Document that users should set `set -g history-limit 0` (unlimited)
in their `.tmux.conf`. The ConversationStore provides a fallback: `/history` or
`agenthicc sessions` shows past sessions that can be resumed.

### Limitation 7: No Mouse Support for In-App Scrolling

**Problem**: With alternate screen, you can implement mouse-scrollable content within
the app. Without it, mouse events in the terminal go to the terminal emulator's native
scroll, not to the app.

**This is the desired behavior** for our architecture. Users scroll the terminal
scrollback with the mouse. The app does not need to handle mouse events.

---

## 12. Testing Strategy for Inline Mode

### The Core Challenge

Testing a no-alternate-screen TUI requires capturing what ANSI sequences were sent to
stdout and verifying the logical content without running a real terminal. Two approaches:

1. **FakeTerminal**: Replace `Terminal` with `FakeTerminal` (already designed above)
2. **pyte**: Use the pyte terminal emulator to interpret ANSI sequences and inspect
   the resulting screen buffer

### Unit Tests: FakeTerminal Pattern

```python
# tests/unit/test_render_loop.py
import pytest
from agenthicc.tui.terminal import FakeTerminal
from agenthicc.tui.frame_composer import FrameComposer
from agenthicc.tui.render_loop import RenderLoop
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.app import StatusState


@pytest.mark.unit
def test_committed_lines_accumulate():
    term = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    # Add a completed turn
    model.append_turn("a1", "assistant")
    model.append_line("a1", "Hello world")
    model.turns[-1].finalized = True
    
    loop.force_commit(model, status, None)
    
    # Committed lines should contain the turn header and text
    assert any("assistant" in line for line in term.committed_lines)
    assert any("Hello world" in line for line in term.committed_lines)


@pytest.mark.unit
def test_committed_lines_never_repeated():
    term = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    # Finalize turn 1
    model.append_turn("a1", "assistant")
    model.append_line("a1", "Turn 1 content")
    model.turns[-1].finalized = True
    loop.force_commit(model, status, None)
    
    count_after_first = len(term.committed_lines)
    
    # Tick again without new content
    loop.force_commit(model, status, None)
    
    # No new lines should have been committed
    assert len(term.committed_lines) == count_after_first


@pytest.mark.unit
def test_in_progress_turn_not_committed():
    term = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState(active=True, partial_text="thinking...")
    
    # Turn not yet finalized
    model.append_turn("a1", "assistant")
    model.append_line("a1", "In progress content")
    # finalized = False (default)
    
    loop.force_commit(model, status, None)
    
    # No content committed to scrollback
    assert term.committed_lines == []
    # But bottom block should have streaming text
    bottom_text = " ".join(term._current_bottom)
    assert "thinking" in bottom_text.lower()


@pytest.mark.unit
def test_bottom_block_structure():
    term = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    loop.force_commit(model, status, None)
    
    # Bottom block must have at least: status, divider, input, footer
    assert len(term._current_bottom) >= 4
    # Divider row contains ─
    assert any("─" in row for row in term._current_bottom)


@pytest.mark.unit
def test_terminal_resize_handled():
    term = FakeTerminal()
    term._size = Size(rows=40, cols=120)
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    loop.force_commit(model, status, None)
    
    # Simulate resize
    term._size = Size(rows=40, cols=60)
    term._size_dirty = True
    
    loop.force_commit(model, status, None)
    
    # Bottom block rows should be clipped to new width (60 cols)
    for row in term._current_bottom:
        import re
        plain = re.sub(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]', '', row)
        assert len(plain) <= 60, f"Row too wide: {plain!r}"
```

### Integration Tests: pyte Terminal Emulator

```python
# tests/integration/test_tui_inline.py
import pyte
import pytest
from agenthicc.tui.terminal import Terminal
from agenthicc.tui.frame_composer import FrameComposer
from agenthicc.tui.render_loop import RenderLoop
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.app import StatusState
import io
import re


COLS, ROWS = 80, 24
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]')


def _make_pyte_session():
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    return screen, stream


def _capture_output(fn) -> tuple[str, pyte.Screen]:
    """Run fn(terminal), capture all writes, process through pyte."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    fn(term)
    
    screen, stream = _make_pyte_session()
    stream.feed(buf.getvalue())
    return buf.getvalue(), screen


@pytest.mark.integration
def test_no_alternate_screen_sequences():
    """Verify that smcup/rmcup are never emitted."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    # Run a full simulated session
    model.append_turn("a1", "assistant")
    model.append_line("a1", "Test content")
    model.turns[-1].finalized = True
    loop.force_commit(model, status, None)
    term.teardown()
    
    output = buf.getvalue()
    
    # smcup: ESC[?1049h — MUST NOT appear
    assert "\x1b[?1049h" not in output, "Alternate screen enter (smcup) detected!"
    # rmcup: ESC[?1049l — MUST NOT appear
    assert "\x1b[?1049l" not in output, "Alternate screen exit (rmcup) detected!"
    # DECSTBM scroll region: ESC[{top};{bottom}r — MUST NOT appear
    assert not re.search(r'\x1b\[\d+;\d+r', output), "Scroll region (DECSTBM) detected!"


@pytest.mark.integration
def test_committed_lines_in_scrollback():
    """Verify committed content is visible in the pyte scrollback buffer."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    model.append_turn("a1", "assistant (test-model)")
    model.append_line("a1", "UNIQUE_CONTENT_MARKER")
    model.turns[-1].finalized = True
    loop.force_commit(model, status, None)
    
    screen, stream = _make_pyte_session()
    stream.feed(buf.getvalue())
    
    # The committed content should appear somewhere in the pyte screen buffer
    all_text = " ".join(
        "".join(screen.buffer[row][col].data for col in range(COLS)).rstrip()
        for row in range(ROWS)
    )
    assert "UNIQUE_CONTENT_MARKER" in all_text, (
        f"Committed content not found in pyte buffer.\nAll text: {all_text!r}"
    )


@pytest.mark.integration
def test_input_bar_always_at_bottom():
    """Verify the input bar (❯) is in the last rows of the pyte screen."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    loop.force_commit(model, status, None)
    
    screen, stream = _make_pyte_session()
    stream.feed(buf.getvalue())
    
    # The ❯ prompt should appear in the last few rows
    bottom_rows = range(ROWS - 5, ROWS)
    bottom_text = " ".join(
        "".join(screen.buffer[row][col].data for col in range(COLS)).rstrip()
        for row in bottom_rows
    )
    # ❯ may not render in pyte's buffer (Unicode), check for > as fallback
    assert "❯" in bottom_text or ">" in bottom_text, (
        f"Input prompt not found in bottom rows.\nBottom text: {bottom_text!r}"
    )


@pytest.mark.integration
def test_bottom_block_height_tracking():
    """Verify Terminal._bottom_height matches actual rows drawn."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    loop.force_commit(model, status, None)
    
    # Height should be at least 4 (status + divider + input + footer)
    assert term._bottom_height >= 4
    
    # After clear_bottom(), height should be 0
    term.clear_bottom()
    assert term._bottom_height == 0
```

### E2E Tests: Full Session Simulation

```python
# tests/e2e/test_full_session.py
import asyncio
import pytest
from agenthicc.tui.terminal import FakeTerminal
from agenthicc.tui.frame_composer import FrameComposer
from agenthicc.tui.render_loop import RenderLoop
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.app import StatusState


@pytest.mark.e2e
async def test_multi_turn_session():
    """Simulate 3 complete agent turns and verify scrollback accumulation."""
    term = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState()
    
    for i in range(3):
        agent_id = f"agent-{i}"
        
        # Simulate streaming
        model.append_turn(agent_id, "assistant")
        for chunk in ["Hello ", "world ", "from ", f"turn {i}"]:
            status.partial_text += chunk
            loop.tick(model, status, None)
        
        # Simulate turn completion
        full_text = status.partial_text
        model.append_line(agent_id, full_text)
        model.turns[-1].finalized = True
        status.partial_text = ""
        status.completed_agents += 1
        loop.force_commit(model, status, None)
    
    # All 3 turns should be committed to scrollback
    committed_text = " ".join(term.committed_lines)
    assert "turn 0" in committed_text
    assert "turn 1" in committed_text
    assert "turn 2" in committed_text
    
    # None of the committed content should be in the current bottom block
    bottom_text = " ".join(term._current_bottom)
    # Partial text was cleared so shouldn't be there
    assert "turn 0" not in bottom_text  # already committed


@pytest.mark.e2e
async def test_streaming_stays_in_bottom():
    """Verify that streaming text appears in bottom, not committed, until finalized."""
    term = FakeTerminal()
    composer = FrameComposer()
    loop = RenderLoop(term, composer)
    model = TranscriptModel()
    status = StatusState(active=True)
    
    model.append_turn("a1", "assistant")
    
    # Streaming: partial text should be in bottom block only
    status.partial_text = "This is streaming content in progress"
    loop.force_commit(model, status, None)
    
    assert term.committed_lines == [], "Streaming content should not be committed"
    bottom_text = " ".join(term._current_bottom)
    assert "streaming content" in bottom_text.lower() or "This is" in bottom_text
```

### What to Assert Against

When testing no-alternate-screen TUIs:

1. **No smcup/rmcup**: Search the raw output for `\x1b[?1049h` and `\x1b[?1049l`
2. **No DECSTBM**: Search for `\x1b[\d+;\d+r` (scroll region)
3. **Committed content in pyte buffer**: Use pyte to interpret ANSI and check screen
4. **Input bar position**: Always in the bottom N rows of the pyte screen
5. **Bottom block height**: `terminal._bottom_height` equals actual rows drawn
6. **Append-only invariant**: `len(committed_lines)` only increases between ticks
7. **Streaming isolation**: `partial_text` appears only in bottom, not in committed

### CI Considerations

The pyte-based tests require no real terminal. Run them in CI without a TTY:

```bash
# No TTY needed — pyte emulates the terminal in Python
uv run pytest tests/integration/test_tui_inline.py tests/e2e/test_full_session.py -v
```

For tests that use `FakeTerminal`, no terminal at all is needed.

For tests that use the real `Terminal` class, set `TERM=dumb` to ensure no color
escapes confuse assertions, or use `force_terminal=False` in Rich Console calls.

---

## Appendix: ANSI Sequences Reference

The complete set of ANSI sequences used by this architecture:

| Sequence | Meaning | Used in |
|----------|---------|---------|
| `\x1b[{n}A` | Cursor up n rows | `Terminal.set_bottom()`, `commit_lines()` |
| `\x1b[0J` | Erase from cursor to end of screen | `Terminal.set_bottom()`, `commit_lines()` |
| `\x1b[2J` | Clear entire screen | Startup only (optional) |
| `\x1b[H` | Cursor to home (1,1) | Startup only (optional) |
| `\x1b[0m` | Reset all attributes | End of every styled span |
| `\x1b[1m` | Bold | Turn headers, prompt |
| `\x1b[2m` | Dim | Timestamps, secondary info |
| `\x1b[22m` | Bold off (not reset all) | End of thinking-wave bold char |
| `\x1b[36m` | Cyan foreground | Input token counter |
| `\x1b[32m` | Green foreground | Output token counter, ✓ check |
| `\x1b[31m` | Red foreground | ✗ failure |
| `\x1b[1;36m` | Bold cyan | Bullet ● |
| `\x1b[1;32m` | Bold green | Input prompt ❯ |
| `\x1b[?25h` | Show cursor | `Terminal.teardown()` |
| `\x1b[?25l` | Hide cursor | Optional (during heavy rendering) |

Sequences that MUST NEVER appear:

| Sequence | Meaning | Why forbidden |
|----------|---------|---------------|
| `\x1b[?1049h` | smcup (enter alternate screen) | Destroys scrollback |
| `\x1b[?1049l` | rmcup (exit alternate screen) | Destroys scrollback |
| `\x1b[{t};{b}r` | DECSTBM (set scroll region) | Conflicts with Rich, breaks in multiplexers |
| `\x1b[?1047h` | Save/restore cursor + screen (older variant) | Same as smcup |
| `\x1b[?47h` | Alternate screen (ANSI older variant) | Same as smcup |
