# PRD-64 — Feature Completeness Registry

## Purpose

This document is the **definitive checklist** ensuring no current TUI feature
is lost during the architectural migration defined in PRDs 58–63. Every feature
in the existing codebase is mapped to its exact location in the new architecture,
with coverage status and gap flags.

Migration slices MUST NOT close as complete unless every feature in that slice's
scope has a ✅ in this registry.

---

## 1. Input System

### 1.1 Core Key Handling

| Feature | Current location | New location | Status |
|---|---|---|---|
| CBREAK setup | `cbreak_reader.raw_mode` | `UnifiedInputSession.__init__` — one context at startup | ✅ PRD-62 |
| `Key` enum (CTRL_C, ENTER, AT, CHAR, …) | `cbreak_reader.py` | Unchanged | ✅ |
| `read_key(fd)` | `cbreak_reader.py` | Unchanged | ✅ |
| Left/Right cursor movement | `InputBuffer.move_left/right` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out in PRD-62 |
| Home/End cursor movement | `InputBuffer.move_home/end` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out |
| Up/Down multi-line cursor | `InputBuffer.move_up/down` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out |
| Up/Down history navigation | `HistoryNavigator.up/down` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out in PRD-62 |
| Multi-line input (Ctrl+J) | `Key.CTRL_ENTER` → `buf.insert("\n")` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out |
| Ctrl+U (clear buffer) | `buf.clear()` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out |
| Ctrl+D (exit or submit) | `_dispatch_normal` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out |
| Ctrl+V (expand paste) | `paste.expand()` | `UnifiedInputSession._dispatch_idle` | ⚠️ not spelled out |
| Shift+Tab (cycle mode) | `mode_manager.cycle()` | `UnifiedInputSession._dispatch_idle` | ❌ **MISSING** — see PRD-65 |
| Bracketed paste (`Key.PASTE`) | `PasteState.apply` | `UnifiedInputSession._dispatch_idle` + `_dispatch_streaming` | ✅ PRD-62 |
| Paste condensation label | `paste_label="[Pasted text with N chars]"` | `InputState.paste_condensed + paste_label` signals | ✅ PRD-62 |
| Backspace on condensed paste | `paste.backspace(buf)` | `UnifiedInputSession._dispatch_*` | ✅ PRD-62 |
| Double Ctrl+C → exit | `_ctrl_c_sequence` → `_EXIT` | `UnifiedInputSession._ctrl_c_sequence` | ✅ PRD-62 |

### 1.2 Trigger System

| Feature | Current location | New location | Status |
|---|---|---|---|
| `@` activates AtMentionTrigger | `_activate_trigger("@")` | `TriggerPickerOverlay` via `OverlayHost` | ✅ PRD-62 |
| `/` activates SlashCommandTrigger | `_activate_trigger("/")` | `TriggerPickerOverlay` via `OverlayHost` | ✅ PRD-62 |
| Fragment accumulation while typing | `_trigger.fragment += ch` | `TriggerPickerOverlay.handle_key` | ✅ PRD-62 |
| Up/Down to navigate matches | `_trigger.selected ±= 1` | `TriggerPickerOverlay.handle_key` | ✅ PRD-62 |
| Enter/Tab to select | `_select_trigger()` | `TriggerPickerOverlay.handle_key` | ✅ PRD-62 |
| Esc to cancel | `_cancel_trigger()` | `TriggerPickerOverlay.handle_key` | ✅ PRD-62 |
| Backspace through trigger char | `delete_before()` + exit trigger | `TriggerPickerOverlay.handle_key` | ✅ PRD-62 |
| Trigger re-entry via backspace into token | `_find_trigger_tail()` | `TriggerPickerOverlay` init from `initial_buf` | ✅ PRD-62 |
| Hint text below dropdown | `MatchItem.hint` | `TriggerPickerOverlay.render()` | ✅ PRD-62 |
| `can_activate()` guard | `handler.can_activate(buf)` | `TriggerPickerOverlay._init_trigger` | ✅ PRD-62 |
| Trigger during streaming | `StreamingSession._is_trigger_char` | `UnifiedInputSession._dispatch_streaming` | ✅ PRD-62 |
| `Key.PASTE` in TriggerPickerOverlay | Not currently handled | `TriggerPickerOverlay.handle_key` must handle | ❌ **MISSING** |

### 1.3 Streaming Input (during agent runs)

| Feature | Current location | New location | Status |
|---|---|---|---|
| Queue message while agent runs | `_pending_queue.append(text)` | `UnifiedInputSession._queued.append` + `SendMessageCommand` | ✅ PRD-61, 62 |
| "⌛ Queued" confirmation print | `StreamingSession._read_loop` | `UnifiedInputSession._dispatch_streaming` | ⚠️ not spelled out |
| Drain queue after turn | `while _pending_queue:` in `run()` | `AgentRuntime._run_turn` `finally` → drain | ✅ PRD-61 |
| Ctrl+C/Esc → cancel agent | `_on_interrupt()` | `UnifiedInputSession._dispatch_streaming` → `InterruptAgentCommand` | ✅ PRD-62 |

---

## 2. Live Panel

### 2.1 Status Bar

| Feature | Current location | New location | Status |
|---|---|---|---|
| Flower icon cycling | `StatusBarState._FLOWERS` + `_flower_frame` | `StatusComponent._flower_frame` + tick | ✅ PRD-60 |
| "Thinking" char-bounce animation | `StatusBarState._thinking_markup()` | `StatusComponent.render()` | ✅ PRD-60 |
| State label (Thinking/Running/Idle) | `StatusBarState._state` | `ConversationStore.agent_state` Signal | ✅ PRD-59 |
| Active tool name | `StatusBarState._tool` | `ConversationStore.active_tool` Signal | ✅ PRD-59 |
| Runtime MM:SS | `StatusBarState._elapsed` | `ConversationStore.elapsed_s` Signal | ✅ PRD-59 |
| Model name (line 2) | `StatusBarState._session_id` | `ConversationStore.model_name` Signal | ✅ PRD-59 |
| Tokens (line 2) | `StatusBarState._input/output_tokens` | `ConversationStore.tokens_in/out` Signals | ✅ PRD-59 |
| Cost (line 2) | `StatusBarState._cost_usd` | `ConversationStore.cost_usd` Signal | ✅ PRD-59 |
| Width-safe truncation | `fit()` + `visible_len()` | `StatusComponent.render()` using `_fit()` | ✅ PRD-60 |
| Status bar height = 2 when model set | `StatusBarState.height()` | `StatusComponent` always 2 lines | ✅ PRD-60 |

### 2.2 Footer

| Feature | Current location | New location | Status |
|---|---|---|---|
| Mode string row (⏵⏵ Auto …) | `FooterState.mode_str` | `ConversationStore.mode_str` Signal | ✅ PRD-59 |
| Context hints (Esc Interrupt etc.) | `FooterState.HINTS[mode]` | `FooterComponent._HINTS[agent_state]` | ✅ PRD-60 |
| Notification text (transient) | `FooterState.notify_text()` | `ConversationStore.notification` Signal | ⚠️ not explicit in PRD-59 |
| Drop hints to fit width | `FooterState.render()` pop segments | `FooterComponent.render()` | ✅ PRD-60 |

### 2.3 Input Bar in Live Panel

| Feature | Current location | New location | Status |
|---|---|---|---|
| `❯ text▌` rendering | `InputBarState.render_prompt(cols)` | `ComposerComponent.render()` | ✅ PRD-60 |
| Paste condensed label in input bar | `InputBarState.update(paste_condensed=True)` | `ComposerComponent` reads `InputState.paste_condensed` | ✅ PRD-60 |
| Multi-line prompt height | `InputBarState.height(cols)` | `ComposerComponent` + Live block recalc | ⚠️ not spelled out |
| Cursor position tracking | `InputBarState.cursor` | `InputState.cursor` Signal | ✅ PRD-59 |

### 2.4 Live Panel Stability

| Feature | Current location | New location | Status |
|---|---|---|---|
| No background refresh thread | `auto_refresh=False` | `Workspace.start()` Live init | ✅ PRD-58, 60 |
| Always-on (never start/stop per turn) | New architecture | `Workspace.start()` / `Workspace.stop()` | ✅ PRD-58 |
| SIGWINCH resize | `live_panel._on_sigwinch` | `ResizeHandler` + `Workspace._redraw()` | ✅ PRD-62 |

---

## 3. Scroll Buffer (Conversation Surface)

| Feature | Current location | New location | Status |
|---|---|---|---|
| Turn header `● assistant model HH:MM:SS` | `_on_assistant_start` | `ScrollBufferAppender._on_event("turn_start")` | ✅ PRD-60 |
| Tool call: name + args + ✓/✗ + ms | `_on_tool_complete` | `ScrollBufferAppender._on_event("tool_complete")` | ✅ PRD-60 |
| Tool call: output preview lines | `tc.output_lines[:4]` | `ScrollBufferAppender._on_event("tool_complete")` | ✅ PRD-60 |
| LLM text (Markdown rendered) | `_on_assistant_complete` → text events | `ScrollBufferAppender._on_event("text")` | ✅ PRD-60 |
| Thinking steps | `transcript.print_thinking_step()` | `ScrollBufferAppender._on_event("thinking_step")` | ❌ **MISSING** — see §4.2 |
| File modified notification | `transcript.print_file_modified()` | `ScrollBufferAppender._on_event("file_modified")` | ❌ **MISSING** — see §4.3 |
| Error block (red) | `transcript.print_error()` | `ScrollBufferAppender._on_event("error")` | ⚠️ partially in PRD-60 |
| `@mention` chips inline | `AgentTurnEntry.mention_chips` | `ScrollBufferAppender._on_event("mention_chips")` | ❌ **MISSING** — see §4.4 |
| Idle status header before prompt | `_print_idle_status()` | `ContextStripComponent.print_idle_header()` | ✅ PRD-60 |
| User message block | `transcript.print_user()` | `ScrollBufferAppender._on_event("user_message")` | ⚠️ not explicit in PRD |

---

## 4. Missing Event Types (require PRD-66 additions)

### 4.1 Kernel Event Bridge (entire category missing from PRDs 58–63)

The current system receives events from the **kernel event bus** (the
`EventProcessor` MPSC queue that feeds `root_reducer`). These are separate
from the agent turn events. PRD-66 specifies the bridge.

| Kernel event | Current handler | New mapping |
|---|---|---|
| `AgentStateChangeEvent` | `_on_agent_state` → status update | Kernel bridge → `ConversationStore.agent_state.set()` |
| `SessionSummaryEvent` | `_on_session_summary` → model name | Kernel bridge → `ConversationStore.session_id/model_name.set()` |
| `NotificationEvent` | `_on_notification` → footer text | Kernel bridge → `ConversationStore.notification.set()` |
| `ModeChangedEvent` | `_on_mode_changed` → mode_str | Kernel bridge → `ConversationStore.mode_str.set()` |
| `InputChangedEvent` | `_on_input_changed` → input bar | Direct from `UnifiedInputSession._push()` |
| `ErrorEvent` | `_on_error` → error block | Kernel bridge → `ConversationStore.append_event("error")` |

### 4.2 ThinkingStep events

Extended thinking (Claude's `<thinking>` blocks) produces `ThinkingStepEvent`.

```
New domain event: ThinkingStepStarted / ThinkingStepCompleted
ConversationStore.append_event("thinking_step", {"step": text, "done": bool})
ScrollBufferAppender: renders "  → [dim]step text[/dim]" or "  ✓ [dim]step text[/dim]"
```

### 4.3 FileModified events

Tool executions that modify files publish `FileModifiedEvent`.

```
New domain event: FileModified
ConversationStore.append_event("file_modified", {"path": str})
ScrollBufferAppender: renders "  [dim]Modified:[/dim] [cyan]path[/cyan]"
```

### 4.4 @mention chips

When the agent processes `@path` mentions, it resolves them and attaches chips
to the turn for transcript display.

```
New ConversationEvent kind: "mention_chips"
Payload: {"chips": [{"raw": "@README.md", "content_preview": "..."}]}
ScrollBufferAppender: renders chip inline above tool calls
```

---

## 5. Mode System (entire feature missing from PRDs 58–63)

Covered by PRD-65. Summary of what must be preserved:

| Feature | Current location |
|---|---|
| `ModeManager` with `ModeRegistry` | `agenthicc/modes.py` |
| `build_default_registry()` | `modes.py` |
| Mode dataclass: `name`, `badge`, `description` | `modes.py` |
| `mode_manager.active` property | `modes.py` |
| `mode_manager.cycle()` → returns new mode | `modes.py` |
| `Shift+Tab` → cycle() → `_mode_notification` | `input/session.py` |
| Mode badge in footer: `⏵⏵ Badge Name …` | `mention_input._get_mode_str()` |
| Mode notification on switch: `❖ Switched to X mode` | `InputSession._mode_line()` |
| Mode passed to agent context | `tui_session.py` |

---

## 6. Session Lifecycle (entire feature missing from PRDs 58–63)

Covered by PRD-67. Summary:

| Feature | Current location |
|---|---|
| Session UUID creation | `tui_session.py` |
| Session index JSON | `sessions.py` |
| `_register_session()` | `sessions.py` |
| `_touch_session()` | `sessions.py` |
| `_find_latest_session_for_cwd()` | `sessions.py` |
| `--resume <id>` CLI flag | `tui_session._run_tui()` |
| `--continue` CLI flag | `tui_session._run_tui()` |
| `resume_id` in Ctrl+C exit hint | `InputSession._ctrl_c_sequence` |
| `_loaded_config` attribute on renderer | `tui_session.py:79` |
| Headless JSON-lines mode | `runners/headless.py` |

---

## 7. Commands (current builtins — must survive migration)

| Command | Current handler | New location |
|---|---|---|
| `/config` | `_menu_config` → `ConfigurationMenu` | `RunBuiltinCommand("config")` → `ConfigMenuOverlay` |
| `/model [provider] [model]` | `commands/builtins.py` | `ChangeModelCommand` |
| `/models` | `commands/builtins.py` | `RunBuiltinCommand("models")` |
| `/status` | `commands/builtins.py` | `RunBuiltinCommand("status")` |
| `/skills` | `commands/builtins.py` | `RunBuiltinCommand("skills")` |
| `/history` | `commands/builtins.py` | `RunBuiltinCommand("history")` |
| `/help` | `commands/builtins.py` | `RunBuiltinCommand("help")` |
| `/cancel` | `commands/builtins.py` | `InterruptAgentCommand` |
| `/clear` | `commands/builtins.py` | `ClearConversationCommand` |
| `/expand [id]` | `commands/builtins.py` | `RunBuiltinCommand("expand")` |
| `/mcp [connect …]` | `commands/builtins.py` | `RunBuiltinCommand("mcp")` |

---

## 8. Configuration Menu (ConfigurationMenu widget)

Not addressed in PRDs 58–63 beyond naming `ConfigMenuOverlay`. Requires full spec:

| Feature | Status |
|---|---|
| Navigate sections with Up/Down | ❌ not in any PRD |
| Expand/collapse sections with Enter/Left | ❌ not in any PRD |
| Edit field values (EDIT state) | ❌ not in any PRD |
| Save changes with "s" key | ❌ not in any PRD |
| Esc closes menu | ✅ implied by Overlay.handle_key |
| Empty config shows helpful message | ❌ not in any PRD |

**Action**: PRD-65 includes `ConfigMenuOverlay` full spec.

---

## 9. Migration Slice Checklist

For each migration slice (PRD-63), this registry must be re-audited. A slice
is complete ONLY when every ⚠️ and ❌ in its scope is resolved.

| Slice | Scope | Registry items | Complete when |
|---|---|---|---|
| 0 | Reactive state | §1 cursor/history signals | All ⚠️ in §2 resolved |
| 1 | Always-on Live | §2 Live panel | All ⚠️ in §2 resolved |
| 2 | ScrollBufferAppender | §3 + §4.2–4.4 | All ❌ in §3 resolved |
| 3 | CommandBus + AgentRuntime | §7 commands | All commands mapped |
| 4 | UnifiedInputSession | §1 all rows | All ⚠️ in §1 resolved |
| 5 | Overlays | §1.2 triggers + §8 config | §8 all resolved |
| 6 | Delete legacy | All registry rows | All rows ✅ |
