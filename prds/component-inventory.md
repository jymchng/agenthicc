# AgentHICC TUI — Complete Component Inventory

**Document version:** 1.0  
**Framework:** Textual (Python) — inline mode, NO alternate screen  
**Architecture:** Event-sourced kernel feeding an immutable `AppState`; all TUI
mutations are driven by kernel subscriber events translated by `TUIEventAdapter`.

---

## 1. Executive Summary

AgentHICC renders an AI coding-agent interface directly in the user's terminal
scroll buffer (Textual "inline" mode).  Because the TUI runs inline rather than
in an alternate screen, every widget must coexist gracefully with the user's
shell history above it.  The design is optimised for:

- **Readability at speed** — streaming text arrives token-by-token; layout must
  never reflow more than the final input line.
- **Low surprise** — keyboard shortcuts follow readline and VS Code conventions
  wherever possible; mode changes are always visible in a persistent status bar.
- **Accessibility** — all state is conveyed through both colour and symbol/text
  so that colour-blind and monochrome terminal users lose nothing.
- **Extensibility** — modes, commands, and tool outputs are open extension points;
  components emit typed Textual messages that other widgets can subscribe to
  without coupling.

The 22 components below fall into five functional layers:

| Layer | Components |
|---|---|
| **Transcript** | ChatTranscript, AgentMessage, UserMessage, ToolCallBlock, DiffViewer, ConversationDivider, ExpandableOutput, ErrorBlock |
| **Live feedback** | StreamingCursor, ThinkingIndicator, ProgressIndicator, NotificationToast |
| **Status / chrome** | SessionHeader, AgentStatusBar, TokenMeter, ModeIndicator, ContextSummary |
| **Input** | InputBar, TriggerDropdown, MentionChip, CommandPalette |
| **Approval / safety** | ApprovalRequest |

---

## 2. Component Hierarchy Diagram

```
AgentHICC (App, inline=True)
│
├── SessionHeader                        ← pinned top chrome
│
├── ChatTranscript  (VerticalScroll)     ← main scrollable body
│   ├── ConversationDivider              ← between turns
│   ├── UserMessage                      ← user turn
│   │   └── MentionChip (0..N)          ← @file tokens inline
│   ├── AgentMessage                     ← agent turn
│   │   ├── ThinkingIndicator            ← during generation
│   │   ├── StreamingCursor              ← end-of-stream text
│   │   └── ExpandableOutput             ← long outputs collapsed
│   ├── ToolCallBlock                    ← per tool invocation
│   │   ├── ProgressIndicator            ← while running
│   │   ├── DiffViewer                   ← when tool output is a diff
│   │   └── ExpandableOutput             ← long tool output
│   ├── ErrorBlock                       ← errors in-flow
│   └── ApprovalRequest                  ← inline approval gate
│
├── ContextSummary                       ← active files / mode context
│
├── AgentStatusBar                       ← agent FSM state
│   ├── ModeIndicator                    ← current mode badge
│   └── TokenMeter                       ← token / cost tracker
│
├── InputBar  (TextArea extended)        ← always at bottom
│   └── MentionChip (inline preview)    ← resolved @mentions
│
├── TriggerDropdown  (Float overlay)     ← @ and / completions
│
├── CommandPalette  (Float overlay)      ← full / command browser
│
└── NotificationToast  (Float overlay)  ← transient alerts
```

---

## 3. Full Component Specifications

---

### 3.1 ChatTranscript

**Purpose & Responsibilities**  
The primary scrollable viewport.  Owns the ordered list of all conversation
items — user messages, agent messages, tool call blocks, dividers, and approval
requests — and keeps the view pinned to the latest item during active streaming
unless the user scrolls up (which disables auto-scroll until they scroll back
to the bottom or press `End`).

**Visual Design**

```
╔══════════════════════════════════════════════════════════════╗
║  ── Turn 3 ───────────────────────────────────────────────── ║
║                                                              ║
║  YOU   11:42                                                 ║
║  Fix the auth bug in @src/auth.py                            ║
║                                                              ║
║  AGENT  11:42                                                ║
║  Let me read the file first…                                 ║
║  ┌─ read_file ──────────────────── ✓ 0.3s ──────────────┐   ║
║  │ src/auth.py (342 lines)                               │   ║
║  └────────────────────────────────────────────────────────┘  ║
║  I found the issue on line 87.  Here is the fix:            ║
║  ┌─ write_file ─────────────────── ● running ────────────┐   ║
║  │ …                                                     │   ║
║  └────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════╝
  [scroll indicator: ▼ 3 new messages]
```

**State Model**

| State | Description |
|---|---|
| `IDLE` | No streaming; scroll freely |
| `STREAMING` | Auto-scroll locked to bottom |
| `SCROLL_PAUSED` | User scrolled up during streaming; scroll-lock suspended |
| `AWAITING_APPROVAL` | An `ApprovalRequest` is the bottommost item; input blocked |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `items` | `list[TranscriptItem]` | Ordered conversation items from `TranscriptModel` |
| `auto_scroll` | `bool` | Whether to pin to bottom |
| `max_items` | `int` | Soft cap before oldest items are virtualised (default 500) |

**Outputs (Textual messages)**

| Message | Payload | When |
|---|---|---|
| `ScrollPaused` | `offset: int` | User scrolls up while streaming |
| `ScrollResumed` | — | User returns to bottom |
| `ItemFocused` | `item_id: str` | Keyboard navigation selects an item |

**Interactions**

| Key / Event | Action |
|---|---|
| `↑` / `PageUp` | Scroll up; pauses auto-scroll if streaming |
| `↓` / `PageDown` | Scroll down |
| `End` | Jump to bottom; resumes auto-scroll |
| `Home` | Jump to top |
| `Enter` on `ApprovalRequest` | Delegates to `ApprovalRequest.confirm()` |
| Mouse scroll | Standard Textual scroll behaviour |

**Accessibility**  
- Screen-reader-friendly: each item has an `aria-label` attribute synthesised
  from its type, speaker, and timestamp.
- Scroll-paused state announced via `notify()` in the status bar.
- High-contrast borders (no colour-only distinction).

**Textual Widget**  
Extend `textual.scroll_view.ScrollView` or use `VerticalScroll` as the container;
individual items are `Widget` subclasses mounted dynamically.

**Notes**  
- Items are appended, never replaced in-place (frozen kernel state model).
- Partial `StreamingCursor` updates mutate only the cursor widget's text, not the
  full list, to avoid reflow.
- Virtualisation (unmounting off-screen items) activates when `len(items) > max_items`.

---

### 3.2 AgentMessage

**Purpose & Responsibilities**  
Renders a single agent turn: the speaker label, timestamp, Markdown body text
(streamed token-by-token), and any child `ToolCallBlock` or `ExpandableOutput`
widgets that appear inline during or after the turn.

**Visual Design**

```
AGENT  11:43  [claude-opus-4-8]
─────────────────────────────────
I have fixed the authentication bug.  The root cause was a missing
`await` on the token validation call.  Here is a summary of changes:

  • src/auth.py line 87 — added `await`
  • tests/test_auth.py   — added regression test

The fix is committed as `fix: await token validation in verify_jwt`.
▌   ← StreamingCursor (disappears when turn ends)
```

**State Model**

| State | Description |
|---|---|
| `PENDING` | Placeholder shown; agent has not yet produced output |
| `STREAMING` | Text arriving token-by-token; cursor visible |
| `COMPLETE` | Final text committed; cursor removed |
| `ERROR` | Turn ended with an error; `ErrorBlock` injected |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `turn_id` | `str` | Unique turn identifier |
| `model_id` | `str` | Model label shown in header |
| `timestamp` | `datetime` | Displayed as `HH:MM` |
| `text` | `str` | Accumulated Markdown text |
| `streaming` | `bool` | Controls cursor visibility |
| `tool_blocks` | `list[ToolCallBlock]` | Child tool widgets |
| `error` | `str \| None` | If set, renders `ErrorBlock` at bottom |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `TurnComplete` | `turn_id: str` | Streaming ends |
| `TurnErrored` | `turn_id: str, error: str` | Error state entered |

**Interactions**  
- `c` on focused message: copy full text to clipboard.
- `e` on focused message: expand all `ExpandableOutput` children.

**Accessibility**  
- Speaker label "AGENT" always present as text (not icon-only).
- `aria-live="polite"` on the streaming text region.
- Timestamp exposed as a `title` attribute for hover.

**Textual Widget**  
`Widget` with a `Vertical` layout; inner `Markdown` widget for body text.  
Extend `Markdown` from `textual.widgets` for streaming updates via `update()`.

**Notes**  
- Markdown widget should use `textual.widgets.Markdown` which supports
  incremental `update(text)` calls; do not remount on each token.
- Model label rendered in `dim` style to reduce visual noise.

---

### 3.3 UserMessage

**Purpose & Responsibilities**  
Renders a single user turn in the transcript.  Displays the raw message text,
resolved `MentionChip` widgets in-flow, and a timestamp.  Non-editable once
committed.

**Visual Design**

```
YOU  11:42
──────────────────────────────────
Fix the auth bug in [src/auth.py]  ← MentionChip
```

**State Model**  
Single state: `DISPLAYED`.  No streaming; user messages are appended whole.

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `turn_id` | `str` | Unique identifier |
| `text` | `str` | Raw text with @mentions stripped |
| `mentions` | `list[Mention]` | Resolved mentions for chip rendering |
| `timestamp` | `datetime` | Display time |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `MentionActivated` | `mention: Mention` | User clicks / activates a chip |

**Interactions**  
- `c`: copy message text to clipboard.
- Tab to focus individual `MentionChip` children.

**Accessibility**  
- "YOU" label never replaced by an icon alone.
- `MentionChip` widgets expose `aria-label="file: <path>"`.

**Textual Widget**  
`Static` for the text body with inline `MentionChip` widgets.  Use a `Horizontal`
flow layout to interleave chips with text spans.

**Notes**  
- `@mention` tokens are replaced with `MentionChip` widgets at render time; the
  raw `@token` text is not displayed.

---

### 3.4 ToolCallBlock

**Purpose & Responsibilities**  
Displays one tool invocation lifecycle: the tool name, argument summary, live
status (pending / running / success / error), duration, and collapsible output.
Wraps a `DiffViewer` when the output is a unified diff, or an `ExpandableOutput`
for long plain text.

**Visual Design**

```
┌─ read_file  ─────────────────────────── ✓ 0.3s ─┐
│ path: "src/auth.py"                              │
│ → 342 lines returned                             │
└──────────────────────────────────────────────────┘

┌─ write_file ─────────────────────────── ● running ─┐
│ path: "src/auth.py"                                │
│ [████████░░░░░░░░░░░░]  writing…                   │
└────────────────────────────────────────────────────┘

┌─ patch_file ─────────────────────────── ✓ 1.1s ─┐
│ path: "src/auth.py"                              │
│ ┌─ diff ────────────────────────────────────┐   │
│ │ - return verify(token)                   │   │
│ │ + return await verify(token)             │   │
│ └───────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘

┌─ run_bash ───────────────────────────── ✗ error ─┐
│ cmd: "pytest tests/test_auth.py"                  │
│ [Show 47 lines of output]  ← ExpandableOutput     │
└────────────────────────────────────────────────────┘
```

**State Model**

| State | Description |
|---|---|
| `PENDING` | Tool queued, not yet started |
| `RUNNING` | Executing; `ProgressIndicator` shown |
| `SUCCESS` | Completed without error; green checkmark + duration |
| `ERROR` | Failed; red X + error message |
| `APPROVAL_NEEDED` | Blocked awaiting `ApprovalRequest` |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `tool_id` | `str` | Unique call identifier |
| `tool_name` | `str` | e.g. `"write_file"` |
| `args_summary` | `str` | Human-readable key args (1-2 lines max) |
| `status` | `ToolStatus` | Current FSM state |
| `duration_ms` | `int \| None` | Set on completion |
| `output` | `str \| None` | Tool result text |
| `is_diff` | `bool` | Whether output should render as `DiffViewer` |
| `error` | `str \| None` | Error message if status is ERROR |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ToolExpanded` | `tool_id: str` | User expands collapsed output |
| `ToolCollapsed` | `tool_id: str` | User collapses |
| `ApprovalRequested` | `tool_id: str` | Block enters APPROVAL_NEEDED |

**Interactions**

| Key / Event | Action |
|---|---|
| `Space` / `Enter` | Toggle output expand/collapse |
| `c` | Copy tool output to clipboard |
| `y` (when APPROVAL_NEEDED) | Forward to `ApprovalRequest.confirm()` |
| `n` (when APPROVAL_NEEDED) | Forward to `ApprovalRequest.deny()` |

**Accessibility**  
- Status conveyed by both colour (green/red/yellow) and symbol (✓/✗/●).
- Duration shown as `aria-label="completed in 0.3 seconds"`.
- Collapsed output has `aria-expanded="false"` attribute.

**Textual Widget**  
`Collapsible` widget from Textual extended to accept status-aware header rendering.
Inner body is either `DiffViewer` or `ExpandableOutput`.

**Notes**  
- The argument summary must be truncated to 2 lines maximum in the header;
  full args accessible via expand.
- Approval-needed state renders an inline `ApprovalRequest` below the args row,
  not as a separate widget mount.

---

### 3.5 DiffViewer

**Purpose & Responsibilities**  
Renders a unified diff (GNU format) with syntax highlighting: red for removed
lines, green for added lines, dim cyan for hunk headers, plain for context lines.
Used inside `ToolCallBlock` when `is_diff=True` and as a standalone component in
`AgentMessage` when the agent produces a diff in its response text.

**Visual Design**

```
┌─ diff: src/auth.py ───────────────────────────────┐
│ @@ -85,7 +85,7 @@ async def verify_jwt(token):   │
│   try:                                             │
│ -     return verify(token)                         │
│ +     return await verify(token)                   │
│   except JWTError:                                 │
│       raise AuthenticationError()                  │
│                                                    │
│ @@ -102,4 +102,4 @@ def refresh_token(user_id):  │
│   …                                                │
└────────────────────────────────────────────────────┘
  3 hunks · +4 / -4 lines  [Copy] [Expand]
```

**State Model**

| State | Description |
|---|---|
| `COLLAPSED` | Only hunk count summary shown |
| `EXPANDED` | Full diff rendered |
| `LOADING` | Diff being computed (streaming patch tool) |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `diff_text` | `str` | Raw unified diff string |
| `file_path` | `str \| None` | For header label |
| `max_lines` | `int` | Lines before collapse (default 40) |
| `collapsed` | `bool` | Initial state |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `DiffCopied` | `diff_text: str` | User copies diff |
| `DiffToggled` | `collapsed: bool` | Expand/collapse toggled |

**Interactions**

| Key | Action |
|---|---|
| `Space` / `Enter` | Toggle expand/collapse |
| `c` | Copy raw diff to clipboard |
| `o` | Open file in `$EDITOR` (if file_path is set) |

**Accessibility**  
- Removed lines prefixed with `[-]` text marker in addition to red colour.
- Added lines prefixed with `[+]` text marker.
- Hunk headers announced with "hunk" prefix for screen readers.

**Textual Widget**  
`Static` with `markup=False`; apply `Rich` `Syntax` object for colouring.  
Alternatively use `textual.widgets.RichLog` for streaming line-by-line adds.

**Notes**  
- Large diffs (>200 lines) are virtualised: only visible hunk range is rendered.
- Hunk navigation with `j`/`k` for next/previous hunk when expanded.

---

### 3.6 StreamingCursor

**Purpose & Responsibilities**  
A blinking block cursor (`▌`) appended at the insertion point of actively
streaming text.  Removed from the DOM when the turn completes.  Provides the
only in-flow visual cue that more text is coming.

**Visual Design**

```
…and the fix is applied to line 87.▌
```

When idle (between tokens but still streaming):

```
…and the fix is applied to line 87.▌   ← blinks at ~1 Hz
```

**State Model**

| State | Description |
|---|---|
| `VISIBLE_ON` | Character is rendered (blink phase on) |
| `VISIBLE_OFF` | Character is blank (blink phase off) |
| `HIDDEN` | Removed; turn complete |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `blink_interval_ms` | `int` | Default 500 |
| `char` | `str` | Cursor character, default `"▌"` |

**Outputs**

None.  Cursor has no external API; its parent `AgentMessage` removes it.

**Interactions**  
No direct interactions.

**Accessibility**  
- Rendered as text character, not a CSS animation, so it works in all terminals.
- The `aria-label="streaming"` attribute on the parent container informs screen
  readers that content is live.

**Textual Widget**  
`Static` with a 500 ms `set_interval` timer toggling between `"▌"` and `" "`.

**Notes**  
- Only one `StreamingCursor` exists in the DOM at a time; previous instance is
  removed when a new agent turn begins.
- The blink interval should pause when the window is not focused to reduce
  battery use on laptops.

---

### 3.7 AgentStatusBar

**Purpose & Responsibilities**  
A single-line persistent bar showing the current agent FSM state, the active
mode badge (`ModeIndicator`), and the running token/cost counter (`TokenMeter`).
Always visible at the bottom of the transcript, above the input bar.

**Visual Design**

```
 ● Thinking   [AUTO]  claude-opus-4-8   1,234 tok · $0.0031  ──  Session abc123
 ○ Idle        [PLAN]  claude-opus-4-8   4,890 tok · $0.0124  ──  Session abc123
 ▶ Running     [SAFE]  claude-sonnet-4-6 2,100 tok · $0.0041  ──  Session abc123
 ✗ Error       [ASK]   claude-opus-4-8   1,100 tok · $0.0028  ──  Session abc123
```

**State Model**

| State | Icon | Colour |
|---|---|---|
| `IDLE` | `○` | dim white |
| `THINKING` | `●` | yellow |
| `RUNNING_TOOLS` | `▶` | cyan |
| `AWAITING_APPROVAL` | `⚠` | yellow (bold) |
| `ERROR` | `✗` | red |
| `STREAMING` | `~` | green |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `agent_state` | `AgentFSMState` | Current FSM state |
| `model_id` | `str` | Active model label |
| `session_id` | `str` | Short form shown at right |
| `mode` | `Mode` | Forwarded to `ModeIndicator` |
| `token_stats` | `TokenStats` | Forwarded to `TokenMeter` |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ModeChangeRequested` | — | User clicks mode badge |

**Interactions**

| Key | Action |
|---|---|
| `Ctrl-M` | Open mode picker (cycles forward through modes) |
| Click mode badge | Open mode picker overlay |

**Accessibility**  
- Status icon is always accompanied by text label (not icon-only).
- Colour + symbol dual coding for all states.
- Bar has `role="status"` for screen-reader live region.

**Textual Widget**  
`Horizontal` layout with three child widgets: `AgentStateLabel` (Static),
`ModeIndicator`, `TokenMeter`.  Use `dock="bottom"` (relative to the app
layout, above `InputBar`).

**Notes**  
- The bar must never wrap to a second line; truncate `model_id` if the terminal
  is narrow (< 80 columns).
- In headless mode, state changes are emitted as JSON-line events instead.

---

### 3.8 TokenMeter

**Purpose & Responsibilities**  
Displays the running token count (input + output, separated) and estimated USD
cost for the current session.  Updates after every streaming chunk.  Shows a
warning colour when approaching a configured token budget threshold.

**Visual Design**

```
Normal:   1,234 in · 567 out · $0.0031
Warning:  9,800 in · 2,340 out · $0.24  ⚠
Critical: 15,000 in · 5,000 out · $0.62 ⛔ near limit
```

**State Model**

| State | Description |
|---|---|
| `NORMAL` | Under 80 % of budget |
| `WARNING` | 80–95 % of budget; amber colour |
| `CRITICAL` | > 95 % of budget; red + icon |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `input_tokens` | `int` | Cumulative input tokens this session |
| `output_tokens` | `int` | Cumulative output tokens this session |
| `cost_usd` | `float` | Estimated cost |
| `budget_tokens` | `int \| None` | Optional cap from config |
| `show_detail` | `bool` | Whether to show in/out split or just total |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `BudgetWarning` | `fraction: float` | Crossed 80 % threshold |
| `BudgetExceeded` | — | Crossed 100 % threshold |

**Interactions**  
- Click / `Enter` when focused: toggle `show_detail` (total vs. in/out split).

**Accessibility**  
- Numeric values readable as text; colour only reinforces, not encodes state.
- `aria-label` includes full text: "1234 input tokens, 567 output tokens, cost 3 cents".

**Textual Widget**  
`Static` with reactive `input_tokens`, `output_tokens`, `cost_usd` attributes.
Colour applied via CSS reactive class (`--tokens-ok`, `--tokens-warn`, `--tokens-crit`).

**Notes**  
- Cost formula: `(input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price`.
  Prices sourced from `AgenthiccConfig.execution.model` look-up table.
- Updates must be throttled to at most once per 200 ms to avoid DOM thrashing
  during fast streaming.

---

### 3.9 ModeIndicator

**Purpose & Responsibilities**  
A coloured badge showing the active operating mode.  Cycling through modes is the
primary interaction; it also acts as a hit-target for the mode picker overlay.

**Visual Design**

```
[AUTO]      ← white, default
[PLAN]      ← green
[ASK]       ← cyan
[REVIEW]    ← blue
[SAFE]      ← yellow
[DEBUG]     ← red
[CUSTOM]    ← magenta (plugin mode)
```

**State Model**  
Single reactive property: `mode: Mode`.  Visual state is derived from
`mode.colour` and `mode.label`.

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `mode` | `Mode` | Current active mode |
| `all_modes` | `list[Mode]` | Ordered cycle list for tooltip |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ModeActivated` | `mode: Mode` | Mode changed by any source |
| `ModeCycleRequested` | `direction: int` | `+1` forward, `-1` backward |
| `ModePickerRequested` | — | Click or `Ctrl-M` |

**Interactions**

| Key / Event | Action |
|---|---|
| `Ctrl-M` | Cycle to next mode |
| `Shift-Ctrl-M` | Cycle to previous mode |
| Click | Open mode picker overlay |
| `?` when focused | Show mode description tooltip |

**Accessibility**  
- Badge label is always text (`AUTO`, `PLAN`, etc.) not an abbreviation-only glyph.
- Tooltip exposes the full mode description and shortcut hint.
- Colour changes announced via `notify()`.

**Textual Widget**  
`Button` styled as a badge (no border radius, fixed width).  Reactive `mode`
attribute triggers `watch_mode()` to update label text and CSS colour class.

**Notes**  
- Mode label max 6 characters; truncated with ellipsis if a plugin mode name
  is longer.
- Shortcut hint (`mode.shortcut_hint`) shown in tooltip, not in the badge itself.

---

### 3.10 InputBar

**Purpose & Responsibilities**  
The primary text entry widget.  Supports multi-line input (Shift-Enter for new
line, Enter to submit), real-time `@mention` and `/command` detection that
triggers the `TriggerDropdown`, inline `MentionChip` display of resolved
mentions, and paste-to-inline for images and file paths.

**Visual Design**

```
╔══════════════════════════════════════════════════════════════╗
║ > Fix the lint errors in [src/utils.py] and [tests/]        ║
║   then run the test suite                                    ║
║                                          [Auto] ↵ to send   ║
╚══════════════════════════════════════════════════════════════╝
```

Typing `@src`:

```
╔══════════════════════════════════════════════════════════════╗
║ > Fix the lint errors in @src                 ← cursor here  ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
│ src/auth.py           file    ← TriggerDropdown appears above
│ src/utils.py          file
│ src/                  dir
```

Typing `/`:

```
╔══════════════════════════════════════════════════════════════╗
║ > /                                                          ║
╚══════════════════════════════════════════════════════════════╝
│ /help          Show available commands      Built-in
│ /model         Switch the active model      Built-in
│ /clear         Clear the transcript         Built-in
│ /status        Show session status          Built-in
│ …
```

**State Model**

| State | Description |
|---|---|
| `IDLE` | Empty input, no trigger active |
| `TYPING` | User entering text |
| `MENTION_TRIGGER` | `@` detected; dropdown shown |
| `COMMAND_TRIGGER` | `/` at position 0; dropdown shown |
| `SUBMITTING` | Enter pressed; sending to agent |
| `BLOCKED` | Agent is not idle; input disabled with visual cue |
| `MULTILINE` | Text contains newline(s); height expands |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `placeholder` | `str` | Default `"Message AgentHICC…"` |
| `agent_ready` | `bool` | When `False`, submitting is blocked |
| `current_mode` | `Mode` | Displayed as hint in corner |
| `max_height_lines` | `int` | Max lines before scroll (default 8) |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `MessageSubmitted` | `text: str, mentions: list[Mention]` | User presses Enter |
| `TriggerDetected` | `kind: str, fragment: str, cursor_pos: int` | `@` or `/` typed |
| `TriggerDismissed` | — | Escape or space after trigger |
| `InputChanged` | `text: str` | Any keystroke |

**Interactions**

| Key | Action |
|---|---|
| `Enter` | Submit (if `agent_ready`) |
| `Shift-Enter` | Insert newline |
| `Escape` | Dismiss dropdown if open; else clear input |
| `@` | Enter mention-trigger mode |
| `/` at col 0 | Enter command-trigger mode |
| `Ctrl-C` | Cancel current agent turn (if running) |
| `↑` (empty input) | Recall previous message |
| `↓` (in history) | Forward through history |
| `Tab` | Accept top dropdown suggestion |
| `Ctrl-K` | Clear input |

**Accessibility**  
- `role="textbox"` with `aria-multiline="true"`.
- `aria-disabled="true"` when `BLOCKED`.
- Placeholder text meets WCAG AA contrast (4.5:1 minimum).
- Submission shortcut shown as visible hint text, not just tooltip.

**Textual Widget**  
Extend `textual.widgets.TextArea` with custom key bindings.  Override
`on_key()` to intercept `Enter` and trigger detection.

**Notes**  
- The `@` trigger only activates when immediately following whitespace or at
  position 0 (to avoid false positives in URLs).
- Input history is stored in session memory (up to 200 entries) and persisted
  across sessions via `ProjectMemoryLayer`.
- Resolved `MentionChip` widgets are displayed inline in the transcript
  `UserMessage`, not inside the `InputBar` itself; the bar shows only the raw
  `@token` text during composition.

---

### 3.11 TriggerDropdown

**Purpose & Responsibilities**  
A floating overlay panel that provides type-ahead completions for both `@mention`
(files, directories, URLs, glob patterns) and `/command` (slash commands with
argument hints).  Appears above the `InputBar`, is keyboard-navigable, and
dismisses on `Escape` or when the trigger character is deleted.

**Visual Design — @mention mode**

```
╔═ @mention ═══════════════════════════════════════════╗
║ src/auth.py          file        12.3 KB             ║  ← highlighted
║ src/auth_utils.py    file         4.1 KB             ║
║ src/                 directory   (34 files)           ║
║ src/**/*.py          glob                             ║
╚══════════════════════════════════════════════════════╝
  ↑↓ navigate · Tab accept · Esc dismiss
```

**Visual Design — /command mode**

```
╔═ /command ════════════════════════════════════════════════════╗
║ /help          Show available commands             Built-in   ║  ← highlighted
║ /model         Switch active model [provider model] Built-in  ║
║ /clear         Clear the transcript                Built-in   ║
║ /skills        List available skills               Built-in   ║
║ /status        Show session status                 Built-in   ║
║ ─── Skills ──────────────────────────────────────────────── ║
║ /deep-research Deep research harness               Skills     ║
╚═══════════════════════════════════════════════════════════════╝
  ↑↓ navigate · Enter/Tab accept · Esc dismiss · ? for detail
```

**State Model**

| State | Description |
|---|---|
| `HIDDEN` | Not mounted |
| `MENTION_MODE` | Showing file/dir completions |
| `COMMAND_MODE` | Showing command completions |
| `LOADING` | Async file-system scan in progress |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `kind` | `"mention" \| "command"` | Which completion mode |
| `fragment` | `str` | Text after trigger character |
| `anchor_pos` | `Offset` | Cursor position in terminal cells for positioning |
| `completions` | `list[Completion]` | Pre-fetched completions |
| `is_loading` | `bool` | Show spinner row |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `CompletionAccepted` | `value: str, kind: str` | Tab or Enter selects item |
| `CompletionDismissed` | — | Escape pressed |
| `CompletionHighlighted` | `value: str` | Navigation changes highlighted item |

**Interactions**

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate items |
| `Tab` / `Enter` | Accept highlighted item |
| `Escape` | Dismiss dropdown |
| `?` | Show detail pane for highlighted command |
| Type more characters | Filter list in-place |

**Accessibility**  
- `role="listbox"` with `aria-activedescendant` pointing to highlighted item.
- Each item has `role="option"` with `aria-selected`.
- Keyboard-only navigation fully supported; no mouse required.

**Textual Widget**  
`OptionList` inside a `Float` container anchored to `InputBar`.  Use
`ContentSwitcher` to swap between mention/command layouts.

**Notes**  
- Completions for `@mention` are generated by `MentionCache` (fuzzy FS scan,
  cached with 2-second TTL).
- Completions for `/command` come from `UnifiedCommandRegistry`; groups are
  rendered as non-selectable separator rows.
- Max 8 visible items; scroll within the dropdown for more.
- The dropdown repositions if it would overflow the terminal height.

---

### 3.12 ApprovalRequest

**Purpose & Responsibilities**  
An inline approval gate rendered inside `ChatTranscript` when the agent requests
to execute a destructive or high-risk tool call (e.g. `write_file`, `run_bash`,
`git_commit`).  Blocks the agent until the user explicitly confirms or denies.
Includes a diff preview of the proposed change where applicable.

**Visual Design**

```
╔══ Approval Required ══════════════════════════════════════════╗
║  ⚠  write_file wants to modify  src/auth.py                  ║
║                                                              ║
║  ┌─ proposed change ─────────────────────────────────────┐  ║
║  │ @@ -85,7 +85,7 @@                                    │  ║
║  │ -     return verify(token)                            │  ║
║  │ +     return await verify(token)                      │  ║
║  └────────────────────────────────────────────────────────┘  ║
║                                                              ║
║  [Y] Allow    [N] Deny    [A] Allow all (this session)       ║
╚══════════════════════════════════════════════════════════════╝
```

For shell commands:

```
╔══ Approval Required ══════════════════════════════════════════╗
║  ⚠  run_bash wants to execute:                               ║
║                                                              ║
║     pytest tests/ -x --tb=short                              ║
║                                                              ║
║  [Y] Allow    [N] Deny    [A] Allow all    [E] Edit command  ║
╚══════════════════════════════════════════════════════════════╝
```

**State Model**

| State | Description |
|---|---|
| `PENDING` | Waiting for user action; input bar disabled |
| `CONFIRMED` | User allowed; agent proceeds |
| `DENIED` | User denied; agent receives denial event |
| `ALLOWED_ALL` | Session-wide allow for this tool granted |
| `TIMEOUT` | Auto-denied after configurable timeout (default none) |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `request_id` | `str` | Unique approval request ID |
| `tool_name` | `str` | Tool requesting approval |
| `description` | `str` | Human-readable description of the action |
| `proposed_diff` | `str \| None` | Unified diff if available |
| `command_text` | `str \| None` | Shell command if applicable |
| `risk_level` | `"low" \| "medium" \| "high"` | Informs colour and border |
| `timeout_s` | `int \| None` | Auto-deny countdown |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ApprovalGranted` | `request_id: str` | User confirms |
| `ApprovalDenied` | `request_id: str, reason: str` | User denies |
| `ApprovalAllGranted` | `tool_name: str` | Allow-all for session |

**Interactions**

| Key | Action |
|---|---|
| `y` / `Enter` | Confirm |
| `n` / `Escape` | Deny |
| `a` | Allow all for this tool this session |
| `e` | Edit command (only for `run_bash`-type tools) |
| `d` | Show full diff in `DiffViewer` overlay |

**Accessibility**  
- Alert role: `role="alertdialog"` with `aria-modal="true"`.
- Focus trapped inside the widget until resolved.
- Risk level conveyed by text ("high risk") and border colour.
- Countdown (if present) announced as an `aria-live="assertive"` region.

**Textual Widget**  
Custom `Widget` with `can_focus=True`.  Captures key events before routing to
parent.  Uses `textual.containers.Container` with `border_title`.

**Notes**  
- When `PENDING`, the `InputBar` enters `BLOCKED` state.
- The `ApprovalRequest` is the only component that can steal focus from `InputBar`.
- "Allow all" sessions persistence is via `ProjectMemoryLayer` key
  `approval.allowed_tools`.

---

### 3.13 ProgressIndicator

**Purpose & Responsibilities**  
A compact animated progress bar or spinner rendered inside `ToolCallBlock` while
a tool is executing.  For tools that report incremental progress (e.g. streaming
`run_bash` output), it shows a bounded bar.  For tools with unknown duration, it
shows a pulsing indeterminate spinner.

**Visual Design — indeterminate**

```
[◐◑◒◓]  running…
```

**Visual Design — determinate**

```
[████████████░░░░░░░░]  64 %  (writing 3 of 5 files)
```

**Visual Design — byte-streaming**

```
[▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒]  fetching…   4.2 KB / ?
```

**State Model**

| State | Description |
|---|---|
| `INDETERMINATE` | No progress value; spinner animation |
| `DETERMINATE` | 0–100 % known; progress bar |
| `COMPLETE` | Full; bar turns green briefly then hides |
| `ERROR` | Bar turns red; stops |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `mode` | `"indeterminate" \| "determinate"` | Bar type |
| `value` | `float \| None` | 0.0–1.0 for determinate |
| `label` | `str` | Status text beside bar |
| `width` | `int` | Bar width in characters (default 20) |

**Outputs**  
None.  Purely presentational; lifecycle controlled by parent `ToolCallBlock`.

**Interactions**  
None.  Not focusable.

**Accessibility**  
- `role="progressbar"` with `aria-valuenow`, `aria-valuemin`, `aria-valuemax`.
- Label text always present alongside the bar.
- Animation is purely text-character-based (no ANSI escape-based animation that
  screen readers skip).

**Textual Widget**  
`ProgressBar` from `textual.widgets` for determinate mode.  For indeterminate,
a `Static` widget with a `set_interval` timer cycling through `◐◑◒◓`.

**Notes**  
- The spinner frame rate is 8 fps (125 ms interval) to remain readable without
  being distracting.
- `COMPLETE` state shows the green full bar for 500 ms then auto-unmounts.

---

### 3.14 NotificationToast

**Purpose & Responsibilities**  
A transient, self-dismissing notification banner that appears at the top of the
terminal (above `SessionHeader`) for system messages: mode changed, session
saved, budget warning, plugin loaded, error outside of turn flow.  Non-blocking;
does not interrupt the agent or the input bar.

**Visual Design**

```
╔═ ℹ  Mode changed to PLAN ══════════════════════════════╗  ← top of screen
╚══════════════════════════════════════════════════════════╝
[auto-dismisses in 3 s]

╔═ ⚠  Approaching token budget (82 %) ════════════════════╗
╚══════════════════════════════════════════════════════════╝

╔═ ✗  Plugin "custom-tools" failed to load ═══════════════╗
║  ImportError: missing dependency 'httpx'                  ║
╚══════════════════════════════════════════════════════════╝
```

**State Model**

| State | Colour | Icon |
|---|---|---|
| `INFO` | dim white | `ℹ` |
| `SUCCESS` | green | `✓` |
| `WARNING` | yellow | `⚠` |
| `ERROR` | red | `✗` |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `message` | `str` | Notification body |
| `level` | `NotifLevel` | INFO / SUCCESS / WARNING / ERROR |
| `duration_ms` | `int` | Auto-dismiss timeout (default 3000; `0` = sticky) |
| `detail` | `str \| None` | Optional secondary line |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ToastDismissed` | `toast_id: str` | Dismissed (auto or user) |

**Interactions**

| Key | Action |
|---|---|
| `Escape` when focused | Dismiss immediately |
| Any key (unfocused) | No effect; does not steal focus |

**Accessibility**  
- `role="alert"` for WARNING and ERROR; `role="status"` for INFO and SUCCESS.
- `aria-live="assertive"` for ERROR; `"polite"` for others.
- Sticky toasts (`duration_ms=0`) must be manually dismissible.

**Textual Widget**  
`Notification` via `App.notify()` (Textual built-in toast system).  For custom
duration and styling, subclass `Toast` from `textual.widgets`.

**Notes**  
- A maximum of 3 toasts stack vertically; oldest is displaced when a 4th arrives.
- Toasts are not recorded in the transcript (ephemeral only).

---

### 3.15 SessionHeader

**Purpose & Responsibilities**  
A single-line header pinned at the very top of the inline TUI output.  Shows the
working directory, session ID (short form), creation time, and a breadcrumb of the
current task/intent label.

**Visual Design**

```
AgentHICC  /home/user/myproject  ·  Session abc123  ·  11:38  ·  Fix auth bug
```

Narrow terminal (< 80 cols):

```
AgentHICC  /myproject  ·  abc123
```

**State Model**  
Single reactive state: `session: SessionMeta`.  No interactive states.

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `cwd` | `Path` | Working directory |
| `session_id` | `str` | Full UUID; displayed as first 8 chars |
| `started_at` | `datetime` | Session start time |
| `current_intent` | `str \| None` | Latest intent label (truncated to 40 chars) |

**Outputs**  
None.

**Interactions**  
- Click session ID: copy full UUID to clipboard.

**Accessibility**  
- The header renders as a single `Static` line; all content is plain text.
- `aria-label` includes full session ID and working directory.

**Textual Widget**  
`Static` with `dock="top"`.  Updates via `watch_` reactive on `session_id` and
`current_intent`.

**Notes**  
- Header is always 1 line regardless of terminal width; excess content is truncated
  with `…` applied left-to-right priority: intent → cwd → session ID.
- In headless mode, this information is emitted as a JSON-line `session_started` event.

---

### 3.16 ThinkingIndicator

**Purpose & Responsibilities**  
An animated "Thinking…" label displayed inside `AgentMessage` between the moment
the agent turn starts and the first token arrives.  Conveys that the LLM is
processing, distinct from the `StreamingCursor` which appears during actual text
generation.

**Visual Design**

```
AGENT  11:43
─────────────────────────────────────
  ◐ Thinking…
```

After first token:

```
AGENT  11:43
─────────────────────────────────────
  I have found the issue…▌
```

(ThinkingIndicator removed; StreamingCursor appears.)

**State Model**

| State | Description |
|---|---|
| `ACTIVE` | Animated; waiting for first token |
| `HIDDEN` | Removed from DOM when first token arrives |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `spinner_frames` | `list[str]` | Default `["◐", "◑", "◒", "◓"]` |
| `label` | `str` | Default `"Thinking…"` |
| `interval_ms` | `int` | Animation speed, default 200 |

**Outputs**  
None.

**Interactions**  
None.

**Accessibility**  
- `aria-live="polite"` so screen readers announce "Thinking" once when it appears.
- Spinner is a character sequence, not a CSS animation.

**Textual Widget**  
`LoadingIndicator` from `textual.widgets` or a custom `Static` with interval timer.

**Notes**  
- During extended thinking (Claude extended-thinking mode), the label changes to
  `"Thinking (extended)…"` and the spinner colour changes to cyan.
- The indicator is unmounted, not hidden, when the first token arrives, to avoid
  residual layout space.

---

### 3.17 CommandPalette

**Purpose & Responsibilities**  
A full-screen overlay (activated by `Ctrl-/` or `Ctrl-P`) providing fuzzy-search
access to all registered slash commands across all sources (built-in, skills,
plugins, MCP).  Distinct from `TriggerDropdown`: this is a modal browser, not an
inline completion list.

**Visual Design**

```
╔══ Command Palette ════════════════════════════════════════════╗
║  > deep res_                                                  ║
║  ─────────────────────────────────────────────────────────── ║
║  /deep-research   Deep research harness         Skills        ║
║  /review          Review a PR                   Skills        ║
║  /code-review     Review code for bugs          Skills        ║
║  ─────────────────────────────────────────────────────────── ║
║  ── Built-in ───────────────────────────────────────────── ║  ║
║  /help            Show available commands       Built-in      ║
║  /model           Switch active model           Built-in      ║
║  ─────────────────────────────────────────────────────────── ║
║  ── MCP ────────────────────────────────────────────────── ║  ║
║  /mcp:github:pr   Open a GitHub pull request   MCP            ║
╚══════════════════════════════════════════════════════════════╝
  ↑↓ navigate · Enter execute · Esc cancel · Tab insert-only
```

**State Model**

| State | Description |
|---|---|
| `HIDDEN` | Not mounted |
| `OPEN` | Search input focused; list populated |
| `EXECUTING` | Selected command being dispatched |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `registry` | `UnifiedCommandRegistry` | All registered commands |
| `initial_query` | `str` | Pre-filled search text (from `/` in `InputBar`) |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `CommandSelected` | `command: Command, args: str` | User confirms a command |
| `PaletteDismissed` | — | Escape pressed |

**Interactions**

| Key | Action |
|---|---|
| `Ctrl-/` or `Ctrl-P` | Open palette |
| Type | Filter commands (fuzzy match on name + description) |
| `↑` / `↓` | Navigate |
| `Enter` | Execute selected command |
| `Tab` | Insert command name into `InputBar` without executing |
| `Escape` | Close palette |
| `/` + partial name | Pre-filter on open |

**Accessibility**  
- `role="dialog"` with `aria-label="Command Palette"`.
- Focus trapped inside while open.
- Search input has `aria-autocomplete="list"`.

**Textual Widget**  
`CommandPalette` from `textual.widgets` (built-in since Textual 0.29) extended
to support group-header rows and source-badge columns.

**Notes**  
- The palette is distinct from `TriggerDropdown`; they never appear simultaneously.
- When invoked via `Ctrl-P` with text already in `InputBar`, the palette
  pre-fills with that text.
- Fuzzy scoring: exact prefix match > word-boundary match > substring match.

---

### 3.18 MentionChip

**Purpose & Responsibilities**  
A styled inline token representing a resolved `@mention` (file, directory, URL,
or glob).  Appears in `UserMessage` widgets in the transcript, and as a preview
in the `InputBar` area (below the text area) during composition.  Clicking or
activating a chip previews the referenced resource.

**Visual Design**

```
[src/auth.py]          ← file chip, dim blue border
[src/]                 ← directory chip, dim cyan border
[https://example.com]  ← URL chip, dim green border
[src/**/*.py]          ← glob chip, dim yellow border
[?unknown]             ← unresolved chip, dim red border
```

**State Model**

| State | Description |
|---|---|
| `RESOLVED` | Path or URL confirmed to exist |
| `UNRESOLVED` | Path not found; styled with warning |
| `LOADING` | Async resolution in progress |
| `FOCUSED` | Keyboard focus; action hints shown |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `mention` | `Mention` | The parsed mention object |
| `label` | `str` | Display text (path or truncated URL) |
| `kind` | `MentionKind` | FILE / DIRECTORY / GLOB / URL / UNRESOLVED |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ChipActivated` | `mention: Mention` | Enter / click |
| `ChipRemoved` | `mention: Mention` | Delete key when focused |

**Interactions**

| Key | Action |
|---|---|
| `Enter` / Click | Preview file content (opens `ExpandableOutput`) |
| `Delete` / `Backspace` | Remove from composition |
| `o` (when focused in transcript) | Open file in `$EDITOR` |

**Accessibility**  
- `role="link"` for URL chips; `role="button"` for file chips.
- `aria-label` includes kind and full path/URL.
- Unresolved state announced as "unresolved mention: <path>".

**Textual Widget**  
`Button` styled as an inline chip.  Use `CSS` border-left with kind-specific
colour.

**Notes**  
- In the `InputBar`, chips are shown in a separate row below the text area, not
  inline within the text, to keep the text area cursor position simple.
- File chips include a character count suffix when the referenced file is large
  (> 10 KB): `[src/auth.py · 12 KB]`.

---

### 3.19 ErrorBlock

**Purpose & Responsibilities**  
Renders errors that occur during agent execution in-flow within the transcript.
Distinct from `NotificationToast` (ephemeral) — `ErrorBlock` is a permanent part
of the turn record.  Used for LLM API errors, tool failures that halt the agent,
and unhandled exceptions.

**Visual Design**

```
╔══ Error ══════════════════════════════════════════════════════╗
║  ✗  Tool run_bash failed                                     ║
║                                                              ║
║  CommandNotFound: 'pytest' is not installed                   ║
║                                                              ║
║  Try:  pip install pytest                                     ║
╚══════════════════════════════════════════════════════════════╝
  [Retry]  [Copy error]  [Dismiss]
```

**State Model**

| State | Description |
|---|---|
| `VISIBLE` | Rendered in transcript |
| `RETRYING` | User clicked Retry; spinner shown |
| `RESOLVED` | A subsequent successful action superseded this error |
| `DISMISSED` | Collapsed to a single dim line |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `error_id` | `str` | Unique identifier |
| `title` | `str` | Short error label |
| `message` | `str` | Full error text |
| `suggestion` | `str \| None` | Optional remediation hint |
| `retryable` | `bool` | Whether Retry action is offered |
| `source` | `str` | `"tool" \| "llm" \| "kernel" \| "network"` |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ErrorRetried` | `error_id: str` | User clicks Retry |
| `ErrorDismissed` | `error_id: str` | User dismisses |
| `ErrorCopied` | `message: str` | User copies error text |

**Interactions**

| Key / Event | Action |
|---|---|
| `r` when focused | Retry (if retryable) |
| `c` | Copy full error message |
| `d` / `Escape` | Dismiss to single line |
| Click `[Retry]` | Retry |

**Accessibility**  
- `role="alert"` with `aria-live="assertive"`.
- Error icon `✗` is always paired with text "Error".
- Source label (`tool`, `llm`, etc.) helps users understand where to look.

**Textual Widget**  
`Static` with bordered container; action buttons rendered as `Button` widgets in
a `Horizontal` row.

**Notes**  
- LLM API errors include the HTTP status code and request ID for support.
- Network errors include a retry countdown if auto-retry is configured.

---

### 3.20 ContextSummary

**Purpose & Responsibilities**  
A collapsible panel between `ChatTranscript` and `AgentStatusBar` that lists the
active context: referenced files, loaded skills, active MCP servers, current mode
constraints, and approximate context window usage.  Helps users understand what
the agent "knows" before sending a message.

**Visual Design — collapsed (default)**

```
 Context: 3 files · 2 skills · AUTO mode · 42 % of context window  [▶ expand]
```

**Visual Design — expanded**

```
╔══ Active Context ══════════════════════════════════════════════╗
║  Files                                                         ║
║    src/auth.py              342 lines  (mentioned)            ║
║    src/utils.py             127 lines  (mentioned)            ║
║    tests/test_auth.py        88 lines  (mentioned)            ║
║                                                                ║
║  Skills                                                        ║
║    deep-research  (auto-triggered)                            ║
║    code-review    (manual)                                    ║
║                                                                ║
║  MCP Servers                                                   ║
║    github   (connected)                                       ║
║    filesystem  (connected)                                    ║
║                                                                ║
║  Mode: AUTO  ·  No tool restrictions                          ║
║  Context window: ████████████░░░░░░░░  42 %  (~21,000 tokens) ║
╚════════════════════════════════════════════════════════════════╝
  [▼ collapse]
```

**State Model**

| State | Description |
|---|---|
| `COLLAPSED` | Single summary line |
| `EXPANDED` | Full detail panel |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `mentioned_files` | `list[Mention]` | Active file mentions |
| `active_skills` | `list[SkillDef]` | Loaded skills |
| `mcp_servers` | `list[MCPServerStatus]` | Connected MCP servers |
| `mode` | `Mode` | Current mode |
| `context_fraction` | `float` | 0.0–1.0 context window usage |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `ContextExpanded` | — | User expands panel |
| `ContextCollapsed` | — | User collapses |
| `FileRemoved` | `path: str` | User removes a file from context |

**Interactions**

| Key | Action |
|---|---|
| `Space` / `Enter` | Toggle expand/collapse |
| `r` on a file row | Remove file from context |
| `o` on a file row | Open in `$EDITOR` |

**Accessibility**  
- Summary line readable as complete sentence: "3 files, 2 skills, AUTO mode".
- Context window percentage expressed as both bar and number.
- Expandable region has `aria-expanded` attribute.

**Textual Widget**  
`Collapsible` from `textual.widgets` with custom inner layout.

**Notes**  
- The `ContextSummary` is hidden when there are no mentions, skills, or MCP
  servers active (fully empty context).
- Context window fraction is a visual approximation; exact token count requires
  a tokeniser call and is done lazily on expand.

---

### 3.21 ConversationDivider

**Purpose & Responsibilities**  
A thin horizontal separator rendered between conversation turns to visually group
messages and provide a turn counter.  Optionally shows a timestamp or session
resume marker.

**Visual Design — standard**

```
 ── Turn 4 ────────────────────────────────────────── 11:47 ──
```

**Visual Design — session resume**

```
 ━━ Session resumed  ·  abc123  ·  2 h 14 m ago ━━━━━━━━━━━━━
```

**Visual Design — compact (narrow terminal)**

```
 ── 4 ──
```

**State Model**  
Static; no interactive states.  `kind` prop determines visual style.

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `turn_number` | `int \| None` | Turn index for standard dividers |
| `timestamp` | `datetime \| None` | Shown at right margin |
| `kind` | `"turn" \| "resume" \| "compact"` | Visual variant |
| `session_id` | `str \| None` | For resume variant |
| `elapsed` | `timedelta \| None` | For resume variant |

**Outputs**  
None.

**Interactions**  
None.

**Accessibility**  
- Rendered as `role="separator"` with `aria-label="Turn 4"`.
- Horizontal rule character is `──` (box-drawing), which is universally supported.

**Textual Widget**  
`Rule` from `textual.widgets` with custom label.

**Notes**  
- Turn numbers count from 1 (human-visible) even though the kernel uses 0-indexed
  turn IDs.
- The timestamp on the right is the time the turn started, not the time the
  divider was rendered.

---

### 3.22 ExpandableOutput

**Purpose & Responsibilities**  
A collapsible wrapper for long tool output or agent text that would otherwise
dominate the transcript.  Shows a configurable number of lines with a "Show N
more lines" affordance.  Used inside `ToolCallBlock` and `AgentMessage`.

**Visual Design — collapsed**

```
│ Process exited with code 1                                  │
│ FAILED tests/test_auth.py::test_verify_jwt - AssertionError │
│ FAILED tests/test_auth.py::test_refresh_token - TypeError   │
│ … 44 more lines  [Show all]
```

**Visual Design — expanded**

```
│ Process exited with code 1                                  │
│ FAILED tests/test_auth.py::test_verify_jwt - AssertionError │
│ … (full 47 lines) …                                         │
│ [Show less]
```

**State Model**

| State | Description |
|---|---|
| `COLLAPSED` | Max preview lines shown; expand affordance visible |
| `EXPANDED` | Full content shown; collapse affordance visible |

**Inputs**

| Prop | Type | Description |
|---|---|---|
| `content` | `str` | Full output text |
| `preview_lines` | `int` | Lines visible when collapsed (default 5) |
| `syntax` | `str \| None` | Language for syntax highlighting (e.g. `"python"`) |
| `initially_collapsed` | `bool` | Default `True` when content > preview_lines |

**Outputs**

| Message | Payload | When |
|---|---|---|
| `OutputExpanded` | `widget_id: str` | User expands |
| `OutputCollapsed` | `widget_id: str` | User collapses |

**Interactions**

| Key | Action |
|---|---|
| `Space` / `Enter` | Toggle expand/collapse |
| `c` | Copy full content to clipboard |
| `s` | Save content to file (prompts for path) |

**Accessibility**  
- `aria-expanded` on the container.
- "Show 44 more lines" is announced as a button with that label.
- Full line count always stated in the affordance ("44 more lines", not "more").

**Textual Widget**  
`Collapsible` extended with line-count logic.  Inner content rendered via
`RichLog` (for streaming) or `Markdown` / `Syntax` (for static output).

**Notes**  
- Threshold: collapse if content exceeds `preview_lines` lines or 2000 characters.
- `syntax` triggers Rich `Syntax` highlighting if a valid language is detected.
- The expand/collapse state persists for the lifetime of the widget; it is not
  remembered across sessions.

---

## 4. Component Interaction Patterns

### 4.1 Message Submission Flow

```
InputBar (Enter)
  │
  ├─► MessageSubmitted message
  │
  ▼
App.on_message_submitted()
  │
  ├─► Kernel: emit(Intent event)
  ├─► ChatTranscript.append(UserMessage)
  ├─► ChatTranscript.append(ConversationDivider)
  ├─► ChatTranscript.append(AgentMessage [PENDING])
  └─► InputBar.clear() + set SUBMITTING → BLOCKED
```

### 4.2 Streaming Token Flow

```
Kernel EventProcessor (ToolCallComplete / StreamToken effect)
  │
  ▼
TUIEventAdapter.on_state_change()
  │
  ├─► AgentMessage.update(text)         [ThinkingIndicator → hidden]
  ├─► StreamingCursor.show()            [first token]
  └─► TokenMeter.update(stats)          [throttled 200 ms]
```

### 4.3 Tool Call Lifecycle

```
Kernel: ToolCallStarted event
  │
  ▼
TUIEventAdapter
  │
  ├─► AgentMessage.append(ToolCallBlock [PENDING → RUNNING])
  └─► ProgressIndicator.start()

Kernel: ApprovalRequired effect
  │
  ├─► ToolCallBlock state → APPROVAL_NEEDED
  ├─► ApprovalRequest rendered inside ToolCallBlock
  ├─► InputBar state → BLOCKED
  └─► ChatTranscript.scroll_to_bottom()

User: y / n
  │
  ▼
ApprovalRequest.ApprovalGranted / ApprovalDenied message
  │
  ├─► Kernel: emit(ApprovalResponse event)
  └─► InputBar state → IDLE

Kernel: ToolCallComplete event
  │
  ├─► ToolCallBlock state → SUCCESS / ERROR
  ├─► ProgressIndicator.stop()
  ├─► DiffViewer / ExpandableOutput mounted if output present
  └─► AgentStatusBar.update(stats)
```

### 4.4 @ Mention Resolution Flow

```
InputBar: @ typed
  │
  ├─► TriggerDetected message (kind="mention", fragment="")
  │
  ▼
App.on_trigger_detected()
  │
  ├─► MentionCache.search(fragment)     [async, 2s TTL]
  └─► TriggerDropdown.show(completions)

User: ↑↓ + Tab
  │
  ▼
TriggerDropdown: CompletionAccepted
  │
  ├─► InputBar.insert_completion(value)
  └─► TriggerDropdown.hide()

On submit:
  │
  ▼
parse_mentions(text) → list[Mention]
  │
  └─► UserMessage rendered with MentionChip per mention
```

### 4.5 Mode Change Flow

```
ModeIndicator: ModeCycleRequested (+1)
  │
  ▼
App.on_mode_cycle_requested()
  │
  ├─► ModeManager.next_mode()
  ├─► Kernel: emit(ModeChanged event)
  ├─► ModeIndicator.update(new_mode)
  ├─► AgentStatusBar.update(new_mode)
  └─► NotificationToast.show("Mode changed to PLAN", INFO, 3000 ms)
```

---

## 5. State Management for Components

### 5.1 Reactive Data Flow

All component state derives from two sources:

1. **Kernel `AppState`** — the single source of truth.  `TUIEventAdapter` subscribes
   to the `EventProcessor` queue and translates `AppState` diffs into Textual
   reactive attribute updates and `post_message()` calls.

2. **Local Textual reactives** — purely presentational state (collapsed/expanded,
   animation frame, focus) that does not need to survive page refreshes.

```
AppState (immutable)
    │
    ▼ TUIEventAdapter (subscriber)
    │
    ├── agent_state  →  AgentStatusBar.agent_state (reactive)
    ├── token_stats  →  TokenMeter.input_tokens / output_tokens / cost (reactive)
    ├── active_mode  →  ModeIndicator.mode (reactive)
    ├── transcript   →  ChatTranscript.items (reactive list)
    └── approvals    →  ApprovalRequest widgets (mount/unmount)
```

### 5.2 Component-Local State (not in kernel)

| Component | Local State | Why local |
|---|---|---|
| `ChatTranscript` | `auto_scroll: bool` | Pure viewport preference |
| `ExpandableOutput` | `collapsed: bool` | Cosmetic only |
| `TriggerDropdown` | `highlighted_index: int` | Ephemeral navigation |
| `StreamingCursor` | `blink_phase: bool` | Animation |
| `ThinkingIndicator` | `frame_index: int` | Animation |
| `ProgressIndicator` | `spinner_frame: int` | Animation |
| `NotificationToast` | `remaining_ms: int` | Auto-dismiss countdown |

### 5.3 Inter-Component Communication

Components communicate exclusively through Textual messages (never direct method
calls across widget boundaries).  The message routing table:

| From | Message | To |
|---|---|---|
| `InputBar` | `MessageSubmitted` | `App` → Kernel |
| `InputBar` | `TriggerDetected` | `App` → `TriggerDropdown` |
| `TriggerDropdown` | `CompletionAccepted` | `App` → `InputBar` |
| `ApprovalRequest` | `ApprovalGranted` | `App` → Kernel |
| `ModeIndicator` | `ModeCycleRequested` | `App` → `ModeManager` |
| `ToolCallBlock` | `ApprovalRequested` | `App` → `ApprovalRequest` mount |
| `TokenMeter` | `BudgetWarning` | `App` → `NotificationToast` |

---

## 6. Shared Design Tokens

These CSS variables are defined in `app.tcss` and applied across all components.

### 6.1 Colour Palette

```css
/* Semantic colours */
--color-agent-bg:       #1e1e2e;   /* agent message background */
--color-user-bg:        #181825;   /* user message background */
--color-tool-border:    #313244;   /* tool block border */
--color-tool-running:   #cba6f7;   /* purple: tool running */
--color-tool-success:   #a6e3a1;   /* green: tool success */
--color-tool-error:     #f38ba8;   /* red: tool error */
--color-tool-pending:   #6c7086;   /* dim: tool pending */

/* Mode badge colours */
--mode-auto:    #cdd6f4;   /* white */
--mode-plan:    #a6e3a1;   /* green */
--mode-ask:     #89dceb;   /* cyan */
--mode-review:  #89b4fa;   /* blue */
--mode-safe:    #f9e2af;   /* yellow */
--mode-debug:   #f38ba8;   /* red */
--mode-custom:  #cba6f7;   /* magenta */

/* Status colours */
--status-idle:     #6c7086;
--status-thinking: #f9e2af;
--status-running:  #89dceb;
--status-error:    #f38ba8;
--status-approval: #fab387;

/* Diff colours */
--diff-added:    #a6e3a1;
--diff-removed:  #f38ba8;
--diff-hunk:     #89dceb;
--diff-context:  #cdd6f4;

/* Toast colours */
--toast-info:    #89b4fa;
--toast-success: #a6e3a1;
--toast-warning: #f9e2af;
--toast-error:   #f38ba8;
```

### 6.2 Typography

```css
/* All text is monospace (terminal default) */
--font-size-base:    1;     /* 1 cell height */
--font-dim:          50%;   /* opacity for secondary labels */

/* Text styles */
--label-agent:   bold white;
--label-user:    bold cyan;
--label-time:    dim white;
--label-tool:    bold;
```

### 6.3 Spacing

```css
--padding-block:  0 1;   /* top/bottom padding for message containers */
--padding-inline: 1 2;   /* left/right padding */
--gap-section:    1;     /* vertical gap between major sections */
--border-tool:    round; /* border style for tool blocks */
```

### 6.4 Z-Order (Float layers)

```
Layer 0: SessionHeader, ChatTranscript, AgentStatusBar, InputBar
Layer 1: ContextSummary (docked above status bar)
Layer 2: TriggerDropdown, CommandPalette (Float overlays above InputBar)
Layer 3: NotificationToast (Float, docked top)
Layer 4: ApprovalRequest (in-flow, but captures focus; effectively modal)
```

### 6.5 Animation Constants

```python
SPINNER_FRAMES       = ["◐", "◑", "◒", "◓"]
SPINNER_INTERVAL_MS  = 125     # 8 fps
CURSOR_BLINK_MS      = 500     # 1 Hz
PROGRESS_COMPLETE_MS = 500     # green flash before hide
TOAST_DEFAULT_MS     = 3_000   # auto-dismiss
TOKEN_UPDATE_THROTTLE_MS = 200 # max token meter refresh rate
```

---

## 7. Component Dependencies Map

```
AgentHICC (App)
├── depends on: Kernel EventProcessor (via TUIEventAdapter)
├── depends on: ModeManager
├── depends on: UnifiedCommandRegistry
├── depends on: MentionCache
└── depends on: MemoryRouter (input history)

ChatTranscript
├── contains: ConversationDivider
├── contains: UserMessage → MentionChip (0..N)
├── contains: AgentMessage → ThinkingIndicator, StreamingCursor, ExpandableOutput
├── contains: ToolCallBlock → ProgressIndicator, DiffViewer, ExpandableOutput, ApprovalRequest
└── contains: ErrorBlock

AgentStatusBar
├── contains: ModeIndicator (← ModeManager)
└── contains: TokenMeter (← TokenStats from AppState)

InputBar
├── emits to: TriggerDropdown (via TriggerDetected message)
├── emits to: App (via MessageSubmitted message)
├── reads: ModeManager (for mode label hint)
└── reads: MemoryRouter (input history)

TriggerDropdown
├── data from: MentionCache (for @mention completions)
├── data from: UnifiedCommandRegistry (for /command completions)
└── emits to: InputBar (via CompletionAccepted)

CommandPalette
├── data from: UnifiedCommandRegistry
└── emits to: App (via CommandSelected)

ApprovalRequest
├── emits to: App → Kernel (ApprovalGranted / ApprovalDenied)
└── blocks: InputBar (sets BLOCKED state)

NotificationToast
└── receives from: App (triggered by BudgetWarning, ModeChange, system events)

ContextSummary
├── data from: AppState.mentions (mentioned files)
├── data from: SkillRegistry (active skills)
└── data from: MCPBridge (server status)
```

---

## Appendix A: Widget-to-Textual-Class Quick Reference

| Component | Textual Base | Notes |
|---|---|---|
| `ChatTranscript` | `VerticalScroll` | Dynamic widget mounting |
| `AgentMessage` | `Widget` + `Markdown` child | Incremental `update()` |
| `UserMessage` | `Static` | Static post-submission |
| `ToolCallBlock` | `Collapsible` extended | Custom status header |
| `DiffViewer` | `Static` + Rich `Syntax` | `RichLog` for streaming |
| `StreamingCursor` | `Static` + `set_interval` | 500 ms blink |
| `AgentStatusBar` | `Horizontal` docked | `dock="bottom"` relative to transcript |
| `TokenMeter` | `Static` reactive | Throttled updates |
| `ModeIndicator` | `Button` styled | Reactive `mode` attribute |
| `InputBar` | `TextArea` extended | Custom key bindings |
| `TriggerDropdown` | `OptionList` in `Float` | Above `InputBar` |
| `ApprovalRequest` | `Widget` + `can_focus=True` | Focus trapping |
| `ProgressIndicator` | `ProgressBar` + `Static` | Two modes |
| `NotificationToast` | `Toast` (Textual built-in) | Custom duration |
| `SessionHeader` | `Static` docked | `dock="top"` |
| `ThinkingIndicator` | `LoadingIndicator` or `Static` | Unmounted on first token |
| `CommandPalette` | `CommandPalette` (Textual built-in) | Extended |
| `MentionChip` | `Button` styled | Inline flow |
| `ErrorBlock` | `Static` + `Button` children | `role="alert"` |
| `ContextSummary` | `Collapsible` | Lazy token count |
| `ConversationDivider` | `Rule` | Custom label |
| `ExpandableOutput` | `Collapsible` extended | Line-count threshold |

---

## Appendix B: Accessibility Checklist

- [ ] Every status is conveyed by both colour and text/symbol (colour-blind safe).
- [ ] All interactive widgets are keyboard reachable via `Tab` order.
- [ ] Focus trapping is in place for `ApprovalRequest` and `CommandPalette`.
- [ ] `aria-live` regions cover all streaming and async content.
- [ ] `aria-expanded` used on all collapsible widgets.
- [ ] `aria-disabled` used on `InputBar` when BLOCKED.
- [ ] No animation that cannot be paused (all timers are `set_interval`, stoppable).
- [ ] All placeholder text meets WCAG AA contrast ratio (4.5:1).
- [ ] `role` attributes applied to all non-standard interactive widgets.
- [ ] Keyboard shortcuts documented in `SessionHeader` tooltip / `/help` output.

---

*End of Component Inventory v1.0*
