---
title: "PRD-56: TUI Architecture — Description, Gaps, and Enhancement Plan"
status: draft
version: 0.1.0
created: 2026-06-14
---

# PRD-56: TUI Architecture

---

## 1. What the TUI Is

The `agenthicc` terminal UI is a **scroll-buffer + live-panel hybrid** built on Rich
without an alternate screen.  It operates in two distinct phases per agent turn:

### Phase A — Idle input
`read_line_with_mention` runs in a thread using CBREAK mode and raw ANSI escape
sequences.  The user sees:

```
 session-id  |  N turns  |  $cost   ↑ in  ↓ out
──────────────────────────────────────────────────
❯ ▌
──────────────────────────────────────────────────
  ⏵⏵ Auto  │  ctrl+j = ↵
```

Everything in this phase is printed directly to stdout (scroll buffer).

### Phase B — Agent streaming
A Rich `Live(transient=True)` block is active.  The block redraws every 50 ms and
shows:

```
   ⎿ tool_name(args)  ✓  42ms        ← SpinnerState rows
   Agent: Running │ grep │ 1.2s      ← StatusBarState
─────────────────────────────────────  ← InputPanel top border
❯ typed text▌                         ← InputBarState
─────────────────────────────────────  ← InputPanel bottom border
  ⏵⏵ Auto │ ctrl+j = ↵               ← FooterState
```

After the Live block stops, the full turn transcript is flushed to the scroll buffer
once via `flush_from_model()`.

### Component Map

```
src/agenthicc/tui/
│
├── tui.py               AgenthiccTUI — root orchestrator; wires bus, components, run loop
├── tui_events.py        EventBus + all typed Event dataclasses
├── reactive.py          _Observable mixin + ReactiveProperty descriptor
├── protocols.py         typing.Protocol contracts for every component
│
├── states.py            Four reactive state objects
│   ├── StatusBarState   agent state, tool name, tokens, runtime  (1 row)
│   ├── FooterState      context-sensitive key hints               (1 row)
│   ├── InputBarState    ❯ prompt + typed text + ▌ cursor         (1–N rows)
│   └── SpinnerState     per-turn tool-call list                   (0–N rows)
│
├── live_panel.py        LivePanel — Rich Live block; subscribes to all four states
├── streaming_input.py   StreamingInput — CBREAK keystroke capture during agent runs
├── console_transcript.py TranscriptView — scroll-buffer event-block printer
│
├── transcript.py        TranscriptModel — pure Python model; turn/tool-call state
├── events.py            TUIEventAdapter — kernel-event → TranscriptModel bridge
│
├── mention_input.py     read_line_with_mention — CBREAK input loop + dropdown
├── input_area.py        Layout constants; prompt_markup/ansi, footer_markup/ansi
├── input_bar.py         Slash-command + @-mention completion logic
├── trigger.py           TriggerRegistry + TriggerHandler protocol
├── triggers/            AtMentionTrigger, SlashCommandTrigger
│
├── app.py               Legacy: SlashCommandHandler (still used), dead InlineRenderer
└── menu.py              MenuDriver / MenuWidget (used by mention_input)
```

---

## 2. Architecture Gaps

### 2.1 Width Overflow — the Line-Overwrite Bug

**Severity: Critical**

Rich's `Live` block redraws by moving the terminal cursor up by exactly `N` lines and
overwriting them.  If any rendered line is wider than the terminal, the terminal wraps
it onto the next row, increasing the visual height.  Rich doesn't know about the extra
row, so on the next redraw it moves up too few lines and leaves stale content — the
"overwrite havoc" the user reports.

Root causes (all in `states.py` and `live_panel.py`):

| Location | Problem |
|---|---|
| `StatusBarState.render(cols)` | Receives `cols` but ignores it (`# noqa: ARG002`) |
| `FooterState.render(cols)` | Same — ignores `cols` |
| `InputBarState.render_prompt()` | Has **no `cols` parameter** |
| `SpinnerState.render_calls()` | Has **no `cols` parameter**; hardcodes `[:72]` for preview |
| `TranscriptModel.render()` | Hardcodes `ln[:120]` (lines 80, 393) |
| `console_transcript.py` `_SEP` | Hardcodes `"─" * 72` |
| `live_panel.py` `_heights` | Computed but **never used** for overflow detection |

**Core missing primitive**: a `visible_len(markup: str) -> int` function that returns
the number of terminal columns a Rich markup string occupies (stripping `[tag]` chars
before measuring).  Without it, every component guesses widths.

### 2.2 Dead Code

| Symbol | File | Status |
|---|---|---|
| `ToolProgressEvent` | tui_events.py | Defined; never published or subscribed |
| `ApprovalRequestEvent` | tui_events.py | Defined; approval flow not implemented |
| `ApprovalResponseEvent` | tui_events.py | Same |
| `InputBarState.render_mode()` | states.py | Defined in protocol; never called |
| `_heights` dict | live_panel.py | Computed; comment says "future use"; never used |
| `InlineRenderer` references | app.py | Old renderer; `AgenthiccTUI` replaced it |

### 2.3 Inconsistent `cols` Propagation

The `cols` value is read once in `LivePanel._build()` (good) but is only partially
passed downstream:

- `SpinnerState.render_calls()` — no `cols` parameter, cannot truncate to width
- `InputBarState.render_prompt()` — no `cols` parameter, cannot truncate to width
- `StatusBarState.render(cols)` and `FooterState.render(cols)` — receive `cols` but
  ignore it (suppressed unused-arg lint warning)

### 2.4 No Overflow / Resize Handling

- `LivePanel._build()` reads the terminal size each redraw, but individual components
  don't guarantee output ≤ `cols` columns.
- No `SIGWINCH` handler — resizing the terminal mid-run causes desynchronised heights.
- No total-height guard — if the Live block content exceeds the terminal height, Rich
  wraps to the scrollback and subsequent redraws corrupt the display.

### 2.5 Exception Silencing

`LivePanel._redraw()` wraps the entire render in `except Exception: pass`.  Silent
failures make rendering bugs invisible during development.

### 2.6 Missing Approval Workflow

`ApprovalRequestEvent` and `ApprovalResponseEvent` are defined in `tui_events.py` but
have no handlers, no UI rendering, and no key-interception in `AgenthiccTUI`.  The
approval flow is entirely absent from the TUI layer.

### 2.7 Two Separate CBREAK Loops

`mention_input.py` (idle) and `streaming_input.py` (streaming) both implement raw
CBREAK keystroke reading with almost identical `termios`/`select` code.  They share no
common abstraction, making future input handling changes require edits in two places.

### 2.8 `app.py` Contains Mixed Concerns

`app.py` originally held `InlineRenderer` (now dead) and `SlashCommandHandler` (still
active) alongside `_thinking_wave()`, `StatusState`, `detect_slash_command()`, and
`MENU_COMMANDS`.  This file is now a miscellany that should be broken apart.

---

## 3. Enhancement Plan

### 3.1 Fix Width Overflow (Critical Path)

**Step 1 — Add `visible_len` to `reactive.py` or a new `tui/rendering.py`**

```python
from rich.text import Text

def visible_len(markup: str) -> int:
    """Terminal columns occupied by *markup* after stripping Rich tags."""
    return Text.from_markup(markup).cell_len

def fit(markup: str, cols: int, ellipsis: str = "…") -> str:
    """Truncate *markup* to at most *cols* visible columns, adding ellipsis if needed."""
    t = Text.from_markup(markup)
    if t.cell_len <= cols:
        return markup
    # Truncate the plain text, rebuild markup
    plain = t.plain
    e_len = Text.from_markup(ellipsis).cell_len
    budget = cols - e_len
    truncated = Text(plain[:budget])
    return truncated.markup + ellipsis
```

**Step 2 — Thread `cols` through every `render` call consistently**

Update the `RenderableState` Protocol in `protocols.py`:

```python
class RenderableState(Protocol):
    def height(self, cols: int) -> int: ...
    def render(self, cols: int) -> str: ...   # guaranteed ≤ cols visible chars
```

Update all state classes to accept `cols` in `render()` and every sub-render method:

- `StatusBarState.render(cols)` — truncate or drop trailing fields if total > cols
- `FooterState.render(cols)` — drop lowest-priority hints first until ≤ cols
- `InputBarState.render_prompt(cols)` — already has correct wrapping logic; add `cols`
  to the signature and enforce it
- `SpinnerState.render_calls(cols)` — use `fit(line, cols)` on every generated line

**Step 3 — Add overflow guard in `LivePanel._build(cols)`**

```python
MAX_PANEL_ROWS = max(3, terminal_rows - 4)  # reserve 4 rows for context above
total = sum(_heights.values())
if total > MAX_PANEL_ROWS:
    # Drop spinner rows from the top until it fits
    spinner_lines = self.spinner.render_calls(cols)
    visible_spinner = spinner_lines[-(MAX_PANEL_ROWS - 3):]  # keep newest
    ...
```

**Step 4 — Add `SIGWINCH` redraw**

```python
import signal

def _on_resize(signum, frame):
    self._redraw()          # force immediate redraw at new size

signal.signal(signal.SIGWINCH, _on_resize)
```

### 3.2 Unify CBREAK Input

Extract a shared `CbreakReader` class into `tui/input_reader.py`:

```python
class CbreakReader:
    """Async CBREAK keystroke reader; shared by mention_input and streaming_input."""

    def __init__(self, fd: int) -> None: ...

    async def read_byte(self) -> bytes:
        """Read one raw byte with 20 ms polling interval."""
        ...

    async def read_char(self) -> tuple[str, str]:
        """Decode one logical keypress (handles multi-byte UTF-8, CSI sequences).
        Returns (key_name, character_or_empty) matching mention_input.Key semantics."""
        ...
```

Both `mention_input.py` and `streaming_input.py` use `CbreakReader`; all the
`select.select` + `os.read` + `termios` plumbing lives in one place.

### 3.3 Implement Approval Workflow

Wire the existing event types into the TUI:

1. `AgenthiccTUI._on_approval_required(e: ApprovalRequestEvent)`:
   - Set `footer_state.mode = "approval"`
   - Append approval block to `console_transcript`
   - Disable `StreamingInput` normal typing
   - Intercept Y/N/Esc keystrokes in `StreamingInput`

2. `StreamingInput` Y/N handler posts `ApprovalResponseEvent` to bus.

3. `AgenthiccTUI._on_approval_decided(e: ApprovalResponseEvent)`:
   - Restore normal footer mode
   - Re-enable `StreamingInput`

### 3.4 Accommodate Future Features

**A. Pluggable Live Panel Rows**

Replace the hardcoded `_build()` row list with a `RowContributor` protocol:

```python
class RowContributor(Protocol):
    def rows(self, cols: int) -> list[str]: ...   # Rich markup lines
    def height(self, cols: int) -> int: ...
```

`LivePanel` maintains an ordered list of contributors.  Adding a new region (e.g., a
progress bar, a diff preview, or a file tree) requires only registering a new
contributor — no changes to `_build()`.

**B. Theming**

Move all colour references into a `Theme` dataclass in `tui/theme.py`.  Each component
receives the theme at construction.  Switching themes is a single attribute change that
triggers `_notify()` on all components.

**C. Mouse Support**

Rich `Live` supports mouse events when the terminal provides them.  Add an optional
`on_mouse(event)` callback slot to `LivePanel`; contributors can declare clickable
regions.  This unblocks future features like clicking a tool call to expand it.

**D. Multi-panel Layout**

Reserve the left gutter for a future file-tree or tool-output panel.  `LivePanel`
should accept a `width` fraction (0.0–1.0) so a second panel can occupy the right
portion of the terminal.

### 3.5 Clean Up `app.py`

Split into:
- `tui/slash_commands.py` — `SlashCommandHandler` (currently active)
- `tui/thinking.py` — `_thinking_wave()`, `StatusState` shim (keep for compat)
- Delete: `InlineRenderer`, `build_app`, `run_inline`, `run_headless`, `render_frame_ansi`

---

## 4. Prioritised Roadmap

| Priority | Item | Effort | Impact |
|---|---|---|---|
| P0 | Fix width overflow (`visible_len` + `fit` + thread `cols` everywhere) | M | Critical bug fix |
| P0 | Remove exception silencing in `LivePanel._redraw()` | XS | Debuggability |
| P1 | Add `SIGWINCH` handler | XS | Stability on resize |
| P1 | Remove dead code (`render_mode`, unused events, `_heights`) | S | Clarity |
| P1 | Unify CBREAK loops into `CbreakReader` | M | Maintainability |
| P2 | Implement approval workflow | M | Feature completeness |
| P2 | Pluggable `RowContributor` for LivePanel | M | Extensibility |
| P3 | Split `app.py` | S | Code hygiene |
| P3 | Theming system | L | UX polish |
| P4 | Mouse support | L | Future features |
| P4 | Multi-panel layout | XL | Future features |

---

## 5. Acceptance Criteria for P0 (Width Overflow Fix)

1. No visible line in the LivePanel ever exceeds `os.get_terminal_size().columns`.
2. `visible_len(markup)` correctly ignores Rich tag characters.
3. `fit(markup, cols)` never returns a string whose `visible_len` exceeds `cols`.
4. All four state `render(cols)` methods call `fit()` on their output.
5. Resizing the terminal to 40, 80, 120, and 200 columns produces correct rendering
   with no stale lines.
6. `LivePanel._redraw()` logs (does not silently swallow) rendering exceptions.
