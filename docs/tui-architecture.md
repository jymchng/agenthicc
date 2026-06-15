# agenthicc TUI — Component Architecture Reference

This document is the canonical reference for the reactive TUI. All future work
should use the names defined here. When a component is added, renamed, or
split, this document must be updated first.

---

## 1. Mental model

```
Terminal
├── Scroll Buffer (above Live block — grows via console.print())
│     └── managed by ScrollBufferAppender
│
└── Live Block (bottom N rows, always-on, auto_refresh=False, transient=True)
      ├── (blank separator line)          ← PRD-73
      ├── StatusComponent
      ├── ─ (border)
      ├── ComposerComponent  OR  Overlay  ← mutually exclusive
      ├── ─ (border)
      └── FooterComponent
```

The **Scroll Buffer** and the **Live Block** are the two terminal regions.
They are managed by different subsystems and must never write to each other's
region. `console.print()` goes to the Scroll Buffer. The Live Block is owned
entirely by `Workspace` via a Rich `Live` object.

---

## 2. State layer

### 2.1 `AppState`

**Location:** `tui/conversation_store.py`  
**Instantiation:** `AppState.create()`

The root reactive container. Holds all mutable TUI state as `Signal[T]`
values. Every component reads from `AppState` — nothing writes to it except
the designated mutators below.

| Field | Type | Description |
|---|---|---|
| `conversation` | `ConversationStore` | Agent-turn state, transcript events, metrics |
| `input` | `InputState` | Buffer contents, cursor, paste mode |
| `overlay` | `Signal[str]` | Name of the active overlay (`""` = none) |
| `modal_open` | `Signal[bool]` | True when any overlay is active |

---

### 2.2 `ConversationStore`

**Location:** `tui/conversation_store.py`  
**Accessed as:** `app_state.conversation`

Owns all agent-turn and transcript state.

#### Signals (read by components)

| Signal | Type | Set by |
|---|---|---|
| `agent_state` | `Signal[AgentState]` | `begin_turn`, `end_turn`, `fail_turn`, `set_tool`, `clear_tool` |
| `active_tool` | `Signal[str]` | `set_tool`, `clear_tool` |
| `elapsed_s` | `Signal[float]` | `tick()` |
| `tokens_in` | `Signal[int]` | `add_tokens` |
| `tokens_out` | `Signal[int]` | `add_tokens` |
| `cost_usd` | `Signal[float]` | `add_tokens` |
| `session_id` | `Signal[str]` | `tui_session.py` startup |
| `model_name` | `Signal[str]` | `tui_session.py` startup |
| `mode_str` | `Signal[str]` | `ModeManager` |
| `active_mode_name` | `Signal[str]` | `ModeManager` |
| `active_mode_badge` | `Signal[str]` | `ModeManager` |
| `notification` | `Signal[str | None]` | `UnifiedInputSession`, `_handle_send`, `_advance` |
| `turns` | `Signal[list[ConversationTurn]]` | `begin_turn` |

#### Computed signals

| Signal | Type | Depends on |
|---|---|---|
| `is_running` | `Computed[bool]` | `agent_state` |
| `turn_count` | `Computed[int]` | `turns` |
| `total_tokens` | `Computed[int]` | `tokens_in`, `tokens_out` |

#### `AgentState` enum

| Value | Meaning |
|---|---|
| `IDLE` | No agent turn in progress |
| `THINKING` | LLM generating a response |
| `RUNNING` | A tool call is executing |
| `RECOVERING` | A tool failed; LLM generating its error response |
| `COMPLETE` | Turn finished successfully (brief transition state) |
| `ERROR` | Turn ended with a fatal error (`fail_turn` called) |

#### Animation state (internal, not signals)

| Field | Type | Description |
|---|---|---|
| `_thinking_frame` | `int` | Increments each tick; drives the bouncing bold char |
| `_flower_frame` | `int` | Cycles 0-7; selects from `_FLOWERS` |

#### Mutator methods

| Method | Transitions |
|---|---|
| `begin_turn(agent_name, turn_id)` | → `THINKING` |
| `end_turn()` | → `IDLE` |
| `fail_turn(error)` | → `ERROR` |
| `set_tool(name)` | → `RUNNING` |
| `clear_tool(success=True)` | → `THINKING` (success) or `RECOVERING` (failure) |
| `add_tokens(inp, out, cost)` | updates metrics signals |
| `append_event(kind, payload)` | adds to current turn; notifies event subscribers |
| `tick()` | advances animation frames; updates `elapsed_s` |

#### Event kinds (`ConversationEvent.kind`)

`turn_start` · `tool_complete` · `text` · `thinking_step` · `file_modified` ·
`error` · `mention_chips` · `user_message` · `tokens`

---

### 2.3 `InputState`

**Location:** `tui/conversation_store.py`  
**Accessed as:** `app_state.input`

Owns the raw input buffer state.

| Signal | Type | Description |
|---|---|---|
| `buf` | `Signal[list[str]]` | Character list of current buffer content |
| `cursor` | `Signal[int]` | Insertion-point index into `buf` |
| `paste_condensed` | `Signal[bool]` | True when a paste is condensed to a label |
| `paste_label` | `Signal[str]` | The `[Pasted text with N chars]` label string |

**Only `UnifiedInputSession._push()` writes to `InputState`.**
No component should write to these signals directly.

---

## 3. Live Block components

All components in this section render inside the `Workspace` Live Block. They
are instantiated by `Workspace` and never instantiated elsewhere.

### 3.1 `Workspace`

**Location:** `tui/workspace/workspace.py`  
**Role:** Root owner of the Rich `Live` object. Subscribes to all signals that
require a redraw. Builds the `Group` renderable on every redraw via `_build()`.

**Constructor:** `Workspace(app_state, console)`

**Child components (owned fields):**

| Field | Type |
|---|---|
| `status` | `StatusComponent` |
| `composer` | `ComposerComponent` |
| `footer` | `FooterComponent` |
| `overlays` | `OverlayHost` |
| `scroll` | `ScrollBufferAppender` |

**Signals subscribed (trigger `_redraw`):**

`agent_state` · `active_tool` · `elapsed_s` · `model_name` · `tokens_in` ·
`tokens_out` · `cost_usd` · `mode_str` · `notification` · `active_mode_name` ·
`buf` · `cursor` · `paste_condensed` · `paste_label` · `overlay`

**Live Block config:**
- `auto_refresh=False` — no background thread racing with `console.print()`
- `transient=True` — prevents orphaned Live content in the Scroll Buffer

**`_build()` output order:**

```
Text("")                      ← blank separator (PRD-73)
StatusComponent.render()
_border(cols)
OverlayHost.render()          ← if overlay active
  OR
ComposerComponent.render()    ← if no overlay active
_border(cols)
FooterComponent.render()
```

**Rule:** Only `_redraw()` calls `Live.update()`. No other code may call
`Live.update()` or `Live.refresh()`.

---

### 3.2 `StatusComponent`

**Location:** `tui/workspace/components.py`  
**Role:** Renders the agent-state animation, runtime clock, model name, and
session metrics. Always rendered; never hidden.

**Reads from `ConversationStore`:**
`agent_state` · `elapsed_s` · `active_tool` · `model_name` · `session_id` ·
`turn_count` · `cost_usd` · `tokens_in` · `tokens_out` ·
`_flower_frame` · `_thinking_frame`

**Layout (3 lines when `model_name` is set, 1 line otherwise):**

```
Line 1: {flower} {state_animation} │ Runtime: mm:ss │ {active_tool}
Line 2: {provider}/{model}
Line 3: {session_id} │ {N} turns │ ${cost} ↑ {tokens_in} ↓ {tokens_out}
```

**State→colour mapping:**

| AgentState | Colour | Label |
|---|---|---|
| `IDLE` | dim | Idle |
| `THINKING` | yellow | Thinking (animated bounce) |
| `RUNNING` | cyan | Running (animated bounce) |
| `RECOVERING` | red | ↻ Recovering (animated bounce) |
| `ERROR` | red | Error |
| `COMPLETE` | green | Complete |

**`height(cols)` must always equal the number of lines `render()` produces.**
The blank separator is counted here (PRD-73).

---

### 3.3 `ComposerComponent`

**Location:** `tui/workspace/components.py`  
**Role:** Renders the input prompt line (`❯ text▌`). Only rendered when no
overlay is active.

**Reads from `InputState`:**
`buf` · `cursor` · `paste_condensed` · `paste_label`

**Layout:**

```
❯ {text}{▌}{rest}          ← single line; multi-line buffers use \n\r joins
```

When `paste_condensed` is True, renders `❯ [Pasted text with N chars]▌`
instead of the raw buffer.

**`height(cols)`** counts newlines in `buf` + 1.

---

### 3.4 `FooterComponent`

**Location:** `tui/workspace/components.py`  
**Role:** Renders the mode badge and context hints. Always 2 lines.

**Reads from `ConversationStore`:**
`mode_str` · `notification` · `agent_state`

**Layout:**

```
Line 1: {mode_badge}  (shift+tab to cycle)  │  ctrl+j = ↵
Line 2: {hints}   OR   {notification}
```

`notification` (when set) temporarily replaces `hints` on line 2.
Hints are selected from a table keyed on `agent_state.name.lower()`.

**`height(cols)`** always returns 2.

---

### 3.5 `OverlayHost`

**Location:** `tui/workspace/overlay.py`  
**Role:** Manages the single active overlay. Provides `show()` and `hide()`.
Writes `overlay` and `modal_open` signals on `AppState`.

**Interface:**

| Method/Property | Description |
|---|---|
| `active` (property) | True when an overlay is showing |
| `widget` (property) | The current `Overlay` instance or None |
| `show(overlay)` | Calls `overlay.on_mount()`; writes signals; redraws |
| `hide()` | Calls `overlay.on_unmount()`; clears signals; redraws |
| `render()` | Delegates to `overlay.render()` |
| `handle_key(key, ch)` | Delegates to `overlay.handle_key(key, ch)`; redraws |
| `set_redraw_callback(fn)` | Wires the `_redraw` callback (called by Workspace) |

---

## 4. Overlays

All overlays inherit `Overlay` (ABC) and implement `render() → Any` and
`handle_key(key, ch) → bool`. They are shown via `OverlayHost.show()` and
always replace the `ComposerComponent` in the Live Block.

An overlay receives a `close` callback at construction time; calling it
invokes `OverlayHost.hide()`.

### 4.1 `Overlay` (ABC)

**Location:** `tui/workspace/overlay.py`

| Method | Required | Description |
|---|---|---|
| `on_mount()` | No (default noop) | Called when overlay becomes active |
| `on_unmount()` | No (default noop) | Called when overlay is dismissed |
| `render()` | **Yes** | Returns a Rich renderable |
| `handle_key(key, ch)` | **Yes** | Returns True if key consumed |

**Class variable:** `name: str = "overlay"` — used to set `AppState.overlay`
signal.

---

### 4.2 `TriggerPickerOverlay`

**Location:** `tui/workspace/overlays/trigger_picker.py`  
**Opened by:** `UnifiedInputSession._open_trigger_overlay_with_initial()`  
**Closed by:** `on_complete` callback (sets buffer, hides overlay)

**Purpose:** Shows a dropdown for `@mention` and `/command` triggers. Renders
inside the Live Block — no raw-mode nesting or pause required.

**Constructor:** `TriggerPickerOverlay(initial_buf, registry, cwd, on_complete)`

**Internal state:**

| Field | Description |
|---|---|
| `_trigger` | `SimpleNamespace(handler, char, fragment, pre_buf)` |
| `_matches` | Current `list[MatchItem]` from `handler.get_matches()` |
| `_selected` | Index of highlighted item |
| `_scroll` | Index of first visible item (line-count based) |
| `_hint` | Hint text below dropdown |

**Scroll model:** `_MAX_LINES = 12` is a **terminal-line budget**, not an item
count. Each item may occupy multiple lines (via `handler.get_lines()`).
`_clamp_scroll()` ensures the selected item is fully visible.

**Key bindings:**

| Key | Action |
|---|---|
| ESC | Cancel — calls `handler.on_cancel()`, then `on_complete(TriggerResult)` |
| Enter / Tab | Select — calls `handler.on_select()`, then `on_complete(TriggerResult)` |
| Space | Commit selection + space (so user can type arguments without second Enter) |
| Up / Down | Navigate items |
| Backspace | Shrink fragment; cancel if fragment empty |
| Any CHAR | Extend fragment; update matches |

**`on_complete` contract:** receives `TriggerResult | None`. `None` means cancel
(buffer not changed). Non-None means set `result.buffer`, apply `result.cursor`,
and if `result.submit` dispatch `SendMessageCommand`.

---

### 4.3 `ConfigMenuOverlay`

**Location:** `tui/workspace/overlays/config_menu.py`  
**Opened by:** `/config` command's `menu_factory`  
**Closed by:** `on_close` callback (ESC key or `s` save-and-close)

**Purpose:** Interactive configuration editor. Reads sections from
`AgenthiccConfig` dataclass fields.

**Constructor:** `ConfigMenuOverlay(cfg, on_close)`

**Internal state:**

| Field | Description |
|---|---|
| `_sections` | `list[_Section]` built from config dataclass fields |
| `_cursor` | `(section_idx, field_idx)` — field_idx `-1` means section header |
| `_state` | `_State.NAVIGATE` or `_State.EDIT` |
| `_scroll` | First visible row index |
| `_edit_buf` | String being typed during `EDIT` mode |

**Key bindings (NAVIGATE state):**

| Key | Action |
|---|---|
| Up / Down | Move cursor |
| Enter / Right | Enter EDIT mode for field, or expand section |
| Left | Collapse section |
| `s` | Save all changed fields to config |
| ESC | Close overlay |

**Key bindings (EDIT state):**

| Key | Action |
|---|---|
| Enter | Commit edit |
| ESC | Abort edit |
| Backspace | Delete character |
| Any CHAR | Append to `_edit_buf` |

---

### 4.4 `HelpOverlay`

**Location:** `tui/workspace/overlays/help.py`  
**Opened by:** `/help` command's `menu_factory`  
**Closed by:** `on_close` callback

**Purpose:** Scrollable command browser with detail view per command.

**Constructor:** `HelpOverlay(registry, on_close, initial_query="")`

**`initial_query` routing:**

| Query | Opens in |
|---|---|
| `""` | `LIST_VIEW`, cursor at top |
| `"/config"` (exact match) | `DETAIL_VIEW` for that command |
| `"/con"` (partial) | `LIST_VIEW`, cursor on first match |

**Internal state:**

| Field | Description |
|---|---|
| `_view` | `_View.LIST` or `_View.DETAIL` |
| `_rows` | Flat list: `str` = group header, `Command` = selectable row |
| `_cmd_indices` | Positions of `Command` rows in `_rows` |
| `_cursor_pos` | Index into `_cmd_indices` |
| `_scroll` | First visible row index |
| `_detail_cmd` | Command shown in DETAIL_VIEW |

**Key bindings (LIST_VIEW):**

| Key | Action |
|---|---|
| Up / Down | Move cursor (skips group headers) |
| Enter | Open DETAIL_VIEW for highlighted command |
| ESC | Close overlay |

**Key bindings (DETAIL_VIEW):**

| Key | Action |
|---|---|
| ESC | Return to LIST_VIEW |

---

## 5. Scroll Buffer

### 5.1 `ScrollBufferAppender`

**Location:** `tui/workspace/appender.py`  
**Role:** The **only** code allowed to call `console.print()`. Subscribes to
`ConversationStore.on_event()` and renders each `ConversationEvent` into the
Scroll Buffer above the Live Block.

**Constructor:** `ScrollBufferAppender(app_state, console)`

**Event kind → rendered output:**

| Kind | Output |
|---|---|
| `turn_start` | `● {agent_name} ({model}) HH:MM:SS` |
| `user_message` | `❯ {text}` |
| `tool_complete` | `  ⎿ {name}({args})  ✓/✗  Nms` + up to 4 output lines |
| `text` | Markdown-rendered LLM text |
| `thinking_step` | `  → step` (in progress) or `  ✓ step` (done) |
| `file_modified` | `  Modified: {path}` |
| `error` | `ERROR {message}` in red |
| `mention_chips` | `  @{path}  preview…` chips |

**Batching:** events are coalesced via `asyncio.get_event_loop().call_soon()`
to avoid a `console.print()` per keypress. The `_flush_batch()` method
processes all accumulated events in one pass.

**Rule:** `ScrollBufferAppender` must never call `Live.update()` and
`Workspace._redraw()` must never call `console.print()`.

---

## 6. Input layer

### 6.1 `UnifiedInputSession`

**Location:** `tui/input/unified_session.py`  
**Role:** Owns the CBREAK raw-mode terminal read loop. Routes keystrokes to
either the active overlay, the streaming dispatcher, or the idle dispatcher.

**Constructor:**
```
UnifiedInputSession(app_state, command_bus, trigger_registry, mode_manager,
                    overlay_host, cwd, cfg)
```

**Mode (`InputMode` enum):**

| Value | Description |
|---|---|
| `IDLE` | Full feature set: triggers, history, cursor movement, mode cycling |
| `STREAMING` | Reduced: queue messages, interrupt agent, paste, basic editing |

`set_mode(mode)` is called by `tui_session._run_turn()` at turn start/end.

**Keystroke routing (priority order):**

1. If `overlay_host.active` → `overlay_host.handle_key(key, ch)` and continue
2. If `mode == STREAMING` → `_dispatch_streaming(key, ch)`
3. Else → `_dispatch_idle(key, ch)`

**`_dispatch_idle` key table:**

| Key | Action |
|---|---|
| Ctrl+C | `_ctrl_c_sequence()` |
| Ctrl+D | Submit text or exit |
| @ / trigger char | `TriggerManager.resolve()` → `_open_trigger_overlay()` |
| Enter | `_submit(text)` |
| Ctrl+J / Ctrl+Enter | Insert newline |
| Backspace | Delete char or re-enter trigger |
| Ctrl+U | Clear buffer |
| Left / Right | Move cursor |
| Home / End | Jump to line start/end |
| Up / Down | Cursor movement or history navigation |
| Shift+Tab | Cycle mode via `ModeManager` |
| Char | Insert or detect new trigger |

**`_dispatch_streaming` key table:**

| Key | Action |
|---|---|
| Ctrl+C / ESC | Dispatch `InterruptAgentCommand` |
| Enter | Dispatch `SendMessageCommand` (may queue) |
| Ctrl+J / Ctrl+Enter | Insert newline |
| Backspace | Delete char |
| Ctrl+U | Clear buffer |
| Char | Insert or detect trigger |

**`_push()`** — the single method that writes to `InputState`. Called after any
buffer mutation. No other code writes to `InputState`.

**Ctrl+C sequence:**
- First press: clear buffer, set notification `"Press Ctrl+C again to exit."`
- Second press: call `show_exit_hint()`, return `_EXIT` sentinel to stop the loop

---

## 7. Trigger system

### 7.1 `TriggerManager`

**Location:** `tui/trigger.py`  
**Role:** Maps single trigger characters to their handlers. The single place
where `Key.*` enum values are normalised to character strings.

**Key method:** `resolve(key, ch) → str | None`  
Returns the registered trigger char for a keystroke, or None.
`Key.AT → "@"` normalisation lives here and **nowhere else**.

### 7.2 `TriggerHandler` (Protocol) and `TriggerHandlerBase` (mixin)

**Location:** `tui/trigger.py`

`TriggerHandler` is the **type-annotation protocol** (pure signatures).  
`TriggerHandlerBase` is the **concrete mixin** with default implementations.
In-tree handlers inherit `TriggerHandlerBase`; external plugins satisfy
`TriggerHandler` structurally.

**Protocol methods:**

| Method | Required | Default in Base |
|---|---|---|
| `get_matches(fragment, ctx)` | Yes | — |
| `on_select(item, fragment, buf)` | Yes | — |
| `on_cancel(fragment, buf)` | Yes | — |
| `can_activate(buf)` | No | `return True` |
| `get_hint(item)` | No | `return None` |
| `get_lines(item, available_width)` | No | `[item.display[:available_width]]` |

**Registered handlers (in-tree):**

| Char | Class | Label |
|---|---|---|
| `@` | `AtMentionTrigger` | Mention File |
| `/` | `SlashCommandTrigger` | Command |

### 7.3 `TriggerResult`

Return type of `on_select`. Carries the new buffer and optional signals:

| Field | Default | Meaning |
|---|---|---|
| `buffer` | (required) | New buffer content |
| `submit` | `False` | If True, dispatch `SendMessageCommand` immediately |
| `cursor` | `None` | Explicit cursor position; None = end of buffer |

### 7.4 `MatchItem`

One row in a trigger dropdown:

| Field | Description |
|---|---|
| `display` | Computed single-line fallback string |
| `value` | Text inserted into buffer on selection |
| `hint` | Optional below-dropdown annotation |
| `label` | Left column (structured; e.g. command name) |
| `detail` | Right column — full untruncated description |

---

## 8. Command bus

### 8.1 `CommandBus`

**Location:** `tui/runtime/commands.py`

The synchronous/async message bus connecting `UnifiedInputSession` to
`tui_session` handlers. Commands are frozen dataclasses.

**Registered command types and their handlers (in `tui_session.py`):**

| Command | Handler | Effect |
|---|---|---|
| `SendMessageCommand` | `_handle_send` | Route slash commands or start agent turn |
| `InterruptAgentCommand` | `_handle_interrupt` | Cancel the running `_agent_task` |

### 8.2 `EventBus`

**Location:** `tui/runtime/domain_events.py`

A separate pub/sub bus for domain events (agent lifecycle, tool calls,
text chunks, etc.). Used internally by `agent_runtime.py`.

**Domain event classes:** `AgentStarted` · `AgentCompleted` · `AgentFailed` ·
`AgentInterrupted` · `ToolStarted` · `ToolCompleted` · `TextChunk` ·
`TextFinalized` · `TokensAccounted` · `ThinkingStepEvent` · `ResizeDetected` ·
`OverlayRequested` · `OverlayClosed` · `FileModifiedEvent` · `MessageSubmitted` ·
`InputChanged`

---

## 9. Invariants

These rules must not be violated. If a change would violate one, the rule must
be explicitly updated in this document first.

| # | Invariant |
|---|---|
| I-1 | `console.print()` is called only by `ScrollBufferAppender`. |
| I-2 | `Live.update()` / `Live.refresh()` is called only by `Workspace._redraw()`. |
| I-3 | `InputState` is written only by `UnifiedInputSession._push()`. |
| I-4 | `ConversationStore` signals are written only by `ConversationStore` mutator methods. |
| I-5 | The Composer and any Overlay are mutually exclusive in the Live Block Group. |
| I-6 | `TriggerManager.resolve()` is the only place `Key.*` enums are mapped to trigger chars. |
| I-7 | `_agent_task` is created only by `_handle_send` and `_advance()` in `tui_session.py`. |
| I-8 | Messages entering the agent (whether from `_handle_send` or `_advance`) always have their `user_message` event appended and their slash commands routed via `_route()` first. |
| I-9 | `OverlayHost.show()` is the only way to activate an overlay; `OverlayHost.hide()` is the only way to dismiss one. No component calls `app_state.overlay.set()` directly. |
| I-10 | `StatusComponent.height()` must equal the number of terminal rows `render()` produces, including the blank separator line counted in PRD-73. |

---

## 10. Naming glossary

| Term | Canonical name | Notes |
|---|---|---|
| The fixed bottom section | **Live Block** | The Rich `Live` object owned by `Workspace` |
| The growing top section | **Scroll Buffer** | Printed via `console.print()` |
| The gap between them | **Blank separator** | A `Text("")` as first element of the Live Block Group |
| The flower + state line | **Status bar** (line 1) | Part of `StatusComponent` |
| The model name line | **Model line** (line 2) | Part of `StatusComponent` |
| The session info line | **Metrics line** (line 3) | Part of `StatusComponent` |
| The `❯ text▌` line | **Composer** | `ComposerComponent` |
| The mode badge + hints lines | **Footer** | `FooterComponent` |
| The `─────` lines | **Borders** | `_border(cols)` helper in `workspace.py` |
| The dropdown popup | **Trigger picker** | `TriggerPickerOverlay` |
| The `/config` editor | **Config overlay** | `ConfigMenuOverlay` |
| The `/help` browser | **Help overlay** | `HelpOverlay` |
| The host for overlays | **Overlay host** | `OverlayHost` |
| The input buffer | **Composer buffer** | `InputState.buf` signal |
| The reactive root | **AppState** | `AppState.create()` |
| The transcript state | **ConversationStore** | `app_state.conversation` |
| The input state | **InputState** | `app_state.input` |
| Agent is thinking | `AgentState.THINKING` | LLM generating response |
| Agent tool running | `AgentState.RUNNING` | Tool call in progress |
| Agent recovering | `AgentState.RECOVERING` | Tool failed; LLM responding to error |
| The read loop | **Input session** | `UnifiedInputSession` |
| Idle / streaming mode | `InputMode.IDLE` / `InputMode.STREAMING` | `UnifiedInputSession._mode` |
| The trigger map | **Trigger manager** | `TriggerManager` |
| One dropdown row | **Match item** | `MatchItem` |
| The scroll-buffer writer | **Appender** | `ScrollBufferAppender` |
