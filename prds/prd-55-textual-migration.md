---
title: "PRD-55: Textual TUI Migration — Replace Rich + Raw-ANSI Layer with Textual"
status: draft
version: 0.1.0
created: 2026-06-14
extends: prd-09-rich-tui.md
---

# PRD-55: Textual TUI Migration

## 1. Executive Summary

The current TUI is a hybrid of three rendering layers that have grown into conflict:

1. **Rich** (`Console`, `Live`, `Panel`, `Table`, `Spinner`) — styled scroll-buffer output.
2. **Raw ANSI + termios** (`mention_input.py`, `input_area.py`) — a custom CBREAK-mode
   input loop with hand-written cursor management, bracketed paste detection,
   dropdown rendering, keyboard-protocol negotiation, and a growing list of per-bug
   workarounds.
3. **`agent_turn.py` spinner** — a separate `rich.live.Live` block that must share the
   console with the input layer and fights it for cursor ownership during streaming.

The combination has produced recurring bugs: cursor `▌` ghosts on submitted messages,
pasted content overflowing the mode line, dropdown layout breakage on terminal resize,
streaming/idle input-bar inconsistency, and a 1,000-line `mention_input.py` that
must manually reinvent layout, animation, and event-loop integration that a modern TUI
framework provides for free.

This PRD specifies a complete migration to
[**Textual**](https://textual.textualize.io/) — a Python TUI framework built on Rich
that provides a reactive widget tree, a CSS layout engine, and an async event loop for
keyboard, mouse, and background work. All kernel, memory, tool, runner, plugin, and
non-UI code are **unchanged**.

---

## 2. Goals

| ID | Goal |
|----|------|
| G1 | Replace all `rich.live.Live`, `rich.console.Console.print()`, and raw-ANSI rendering with Textual's widget and layout system |
| G2 | Replace the `mention_input.py` CBREAK loop with Textual's keyboard model, eliminating `termios`/`os.read` from the TUI path entirely |
| G3 | Provide a unified, always-mounted input panel: top border → content rows (multi-line, paste, triggers) → bottom border → mode footer |
| G4 | Preserve all current features: @mention picker, slash-command picker, multi-line input (Ctrl+J), bracketed paste condense/expand, history navigation, mode cycling, queued messages during streaming |
| G5 | Preserve all existing public-facing contracts: `TranscriptModel`, `TUIEventAdapter`, `InlineRenderer.run(on_intent)` call signature, `agenthicc` entry point |
| G6 | All existing unit and integration tests continue to pass; new Textual Pilot tests added per widget |

---

## 3. Non-Goals

- Modifying `TranscriptModel`, `TUIEventAdapter`, or any kernel/runner/tool code.
- Supporting mouse beyond Textual's built-in scroll and focus.
- A multi-pane split view (sidebar, file tree) — future PRD.

---

## 4. Current Architecture (components being replaced)

```
tui/
  app.py            InlineRenderer     — Rich Console + Live + Panel + Spinner
  mention_input.py  Input loop         — CBREAK, raw ANSI, hand-drawn dropdowns
  input_area.py     Style helpers      — Rich markup + raw ANSI prompt/footer
  menu.py           MenuDriver         — raw ANSI overlay menus
  widgets/
    dropdown.py     DropdownWidget     — raw ANSI dropdown
    config_menu.py  ConfigMenu         — raw ANSI config overlay
runners/
  agent_turn.py     _spin() / _watch_input()  — rich.live.Live + CBREAK keystroke capture
```

**Kept unchanged:**
```
tui/
  transcript.py     TranscriptModel   — pure Python data model
  events.py         TUIEventAdapter   — kernel→model bridge
  trigger.py        TriggerRegistry + TriggerHandler protocol
  triggers/         AtMentionTrigger, SlashCommandTrigger
  input_area.py     PROMPT_CHAR, CURSOR_CHAR, get_mode_str (constants only after migration)
runners/
  tui_session.py    Orchestration (creates renderer, calls run())
```

---

## 5. Target Architecture

```
tui/
  app.py            AgenthiccApp       — Textual Application subclass (replaces InlineRenderer)
  transcript.py     TranscriptModel    — UNCHANGED
  events.py         TUIEventAdapter    — UNCHANGED
  trigger.py        TriggerRegistry    — UNCHANGED
  triggers/         At + Slash         — UNCHANGED
  input_area.py     Constants only     — PROMPT_CHAR, CURSOR_CHAR, get_mode_str
  widgets/
    transcript_view.py  TranscriptView    — scrollable log of agent turns + tool calls
    input_panel.py      InputPanel        — unified multi-line input widget
    trigger_menu.py     TriggerMenu       — @mention / slash-command dropdown overlay
    status_bar.py       StatusBar         — thinking wave + token counts during agent run
    spinner_panel.py    SpinnerPanel      — streaming tool-call list
    mode_footer.py      ModeFooter        — mode badge + key hints + notifications
    command_modals.py   CommandModals     — /status, /history, /models, /help, /skills
runners/
  agent_turn.py     _run_agent_turn()  — posts messages; no Live, no CBREAK
  tui_session.py    Orchestration      — UNCHANGED (creates AgenthiccApp instead of InlineRenderer)
```

### 5.1 Screen layout

```
┌──────────────────────────────────────────────────────────────┐
│ StatusBar   (1 row, docked=top, display:none when idle)      │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│ TranscriptView  (fills all remaining height, scrollable)     │
│   ● agent (mimo-v2.5-pro)  02:48:04                          │
│     Hello! How can I help?                                   │
│     ⎿ git_status()  ✓  10ms                                  │
│     ⎿ list_directory('.')  ✓  5ms                            │
│   SpinnerPanel  (inline, only during agent run)              │
│                                                              │
├──────────────────────────────────────────────────────────────┤  ← top border (always)
│ ❯ InputPanel content (1+ dynamic rows)                       │
│   TriggerMenu (dropdown, inline below cursor, when active)   │
├──────────────────────────────────────────────────────────────┤  ← bottom border (always)
│   ModeFooter  (1 row — mode badge, key hints, notifications) │
└──────────────────────────────────────────────────────────────┘
```

---

## 6. Component Specifications

### 6.1 `AgenthiccApp` (replaces `InlineRenderer`)

**File:** `tui/app.py`

`AgenthiccApp(textual.app.App)` is the root application. It composes the screen,
owns cross-widget reactive state, and exposes the same interface that
`tui_session.py` currently calls on `InlineRenderer`.

**Reactive properties:**

| Property | Type | Purpose |
|----------|------|---------|
| `agent_active` | `reactive[bool]` | Shows/hides `StatusBar` |
| `pending_queue` | `reactive[list[str]]` | Queued messages during streaming |
| `session_summary` | `reactive[SessionSummary]` | Tokens, cost, turns, session id for ModeFooter |

**Interface preserved for `tui_session.py`:**

```python
class AgenthiccApp(App):
    async def run(self, on_intent: Callable[[str], Awaitable[None]]) -> None: ...
    def on_intent_submitted(self) -> None: ...          # activates StatusBar
    def on_model_call_complete(self, ...) -> None: ...  # updates token counters
    def on_agent_run_complete(self) -> None: ...        # deactivates StatusBar
    def _flush_new_lines(self) -> None: ...             # posts TranscriptMessage
    @property
    def console(self) -> ConsoleShim: ...               # thin shim for agent_turn.py
```

`ConsoleShim.print(markup)` posts a `ConsolePrint(markup)` message handled by
`TranscriptView` — no direct stdout writes.

---

### 6.2 `TranscriptView`

**File:** `tui/widgets/transcript_view.py`

`TranscriptView(ScrollableContainer)` renders `AgentTurnEntry` objects from
`TranscriptModel`.

- Subscribes to `TranscriptUpdated` messages; calls `refresh()` on new turns or
  tool-call state changes.
- Each turn renders via `TranscriptModel.render()` (unchanged — returns Rich markup
  strings) fed into a `RichLog` or sequence of `Markup` widgets.
- Auto-scrolls to bottom unless the user has manually scrolled up.
- `SpinnerPanel` is mounted as a child at the bottom during agent runs, removed on
  completion.

**Reactive:**
- `auto_scroll: reactive[bool]` — toggled by scroll events.

---

### 6.3 `InputPanel` (replaces `mention_input.py` + `input_area.py` rendering)

**File:** `tui/widgets/input_panel.py`

`InputPanel(Widget)` is the unified, always-mounted input widget. It replaces the
entire `read_line_with_mention` state machine.

**Responsibilities:**

| Feature | Current location | Textual implementation |
|---------|-----------------|----------------------|
| Multi-line editable buffer | `buf: list[str]`, `cursor: int` in state machine | Internal `_buf` / `_cursor`; `render()` calls `prompt_markup()` from `input_area.py` |
| Enter → submit | `Key.ENTER` handler | `on_key(Key.ENTER)` → emit `InputSubmitted(value)` |
| Ctrl+J → newline | `Key.CTRL_ENTER` handler | `on_key("ctrl+j")` → insert `\n` |
| History ↑/↓ | hist_idx state | `_hist_idx`; history list injected at construction |
| Trigger activation (`@`, `/`) | `_find_trigger_tail`, `can_activate` | `on_key("@")` / `on_key("/")` → mount `TriggerMenu` |
| Bracketed paste condense/expand | `Key.PASTE`, `_paste_condensed` | `on_paste(Paste)` Textual event; `_paste_condensed: reactive[bool]` drives `render()` |
| Ctrl+V expand paste | `Key.CTRL_V` handler | `action_expand_paste()` |
| Backspace on paste | `_paste_range` delete | `on_key("backspace")` checks `_paste_condensed` |
| Ctrl+U clear | `Key.CTRL_U` | `action_clear_input()` |
| Cursor Left/Right/Home/End | explicit cursor moves | `on_key(...)` updates `_cursor` |
| Up/Down within multiline | line-col arithmetic | `on_key("up"/"down")` checks line count first |
| Mode cycling (Shift+Tab) | `Key.SHIFT_TAB` → `mode_manager.cycle()` | `on_key("shift+tab")` → `post_message(ModeCycled())` |

**Messages emitted:**
- `InputSubmitted(value: str)` — handled by `AgenthiccApp` to call `on_intent`
- `TriggerActivated(char: str, fragment: str)` — mounts `TriggerMenu`

**CSS:**
```css
InputPanel {
    height: auto;
    max-height: 30vh;    /* cap so paste never overflows terminal */
    border-top: heavy $primary;
    border-bottom: heavy $primary-darken-2;
}
```

**During agent streaming:** `InputPanel` remains mounted and interactive. Textual's
event loop is not blocked by agent work (which runs in a `Worker`). Characters typed
during streaming go through the same `on_key` handlers; `InputSubmitted` during
streaming posts to `AgenthiccApp.pending_queue` instead of calling `on_intent` directly.

---

### 6.4 `TriggerMenu` (replaces ANSI dropdown in `mention_input.py`)

**File:** `tui/widgets/trigger_menu.py`

`TriggerMenu(Widget)` is a floating overlay mounted at the bottom of `InputPanel`'s
content area when a trigger is active.

- Uses existing `TriggerHandler.get_matches()` and `TriggerHandler.can_activate()`
  protocols **without modification**.
- Keyboard: ↑/↓ navigate, Enter selects, Esc cancels.
- Emits `TriggerSelected(item: MatchItem)` and `TriggerCancelled()` to `InputPanel`.
- Visibility toggled via `display` reactive; no DOM mount/unmount on each keystroke.

---

### 6.5 `StatusBar`

**File:** `tui/widgets/status_bar.py`

`StatusBar(Static)` docked to the top, hidden when idle.

- `set_interval(0.05, self._tick)` drives the `_thinking_wave()` animation and
  elapsed-time counter — replaces the `asyncio.sleep(0.05)` loop in `_spin()`.
- Reactive: `input_tokens`, `output_tokens`, `session_cost_usd`, `spinner_frame`.
- Updated via `StatusBar.update_tokens(...)` called from `AgenthiccApp` signal handlers.

---

### 6.6 `SpinnerPanel` (replaces `rich.live.Live` + `_spin()` in `agent_turn.py`)

**File:** `tui/widgets/spinner_panel.py`

`SpinnerPanel(Widget)` renders tool-call progress inline inside `TranscriptView`.

- Mounted when an agent turn starts; removed on completion.
- One `ToolCallRow(Static)` child per tool call; updated via
  `ToolCallRow.update_state(state, duration_ms, diff)`.
- Replaces the `_live_calls: list[dict]` accumulator and `live.update(markup)` pattern.
- Ctrl+O expand/collapse and ↑/↓ scroll remain as `action_toggle_expand()` and
  `on_key("up"/"down")` — no raw CBREAK needed.

**Impact on `agent_turn.py`:**
- `live = Live(...)` and `live.start()` / `live.stop()` are removed.
- Signal handlers (`_on_tool_started`, `_on_tool_complete`) call
  `app.call_from_thread(spinner_panel.add_tool_call, ...)` instead.
- `_watch_input()` raw CBREAK loop is deleted; `InputPanel.on_key()` handles typing.
- `_spin()` coroutine is deleted; `StatusBar.set_interval()` drives animation.
- `pending_queue` is wired through `AgenthiccApp.pending_queue` reactive.

---

### 6.7 `ModeFooter` (replaces `_get_mode_line()` closure + `input_area.get_mode_str()`)

**File:** `tui/widgets/mode_footer.py`

`ModeFooter(Static)` — 1-row widget at the bottom of `InputPanel`.

- Reactive: `mode_name`, `mode_badge`, `notification: str | None`.
- `notification` overrides normal mode text for transient messages:
  - Ctrl+C first press → `"Press Ctrl+C again to exit."`
  - Condensed paste active → `"ctrl+v to expand paste"`
  - Mode switched → `"❖ Switched to {name} mode"` (auto-clears after 2 s)
- Key hint suffix (e.g., `│ ctrl+j = ↵`) appended from `input_area.get_mode_str()`.

---

### 6.8 `CommandModals` (replaces `SlashCommandHandler` Rich table output)

**File:** `tui/widgets/command_modals.py`

Textual `ModalScreen` subclasses for commands that currently print Rich tables:

| Command | Current | Textual replacement |
|---------|---------|---------------------|
| `/status` | `rich.Table` | `AgentStatusModal(DataTable)` |
| `/history` | `rich.Panel` | `HistoryModal(RichLog)` |
| `/models` | `rich.Table` + `Text` | `ModelsModal(DataTable + Static)` |
| `/help` | `rich.Table` | `HelpModal(DataTable)` |
| `/skills` | `rich.Table` | `SkillsModal(DataTable)` |

Non-table commands (`/expand`, `/model <provider>`, mode toggle) remain in-place
mutations dispatched through the existing `CommandDispatcher` — no modal needed.

---

## 7. Deleted Components

| Symbol / File | Replacement |
|---------------|-------------|
| `InlineRenderer` class (`app.py`) | `AgenthiccApp` |
| `_thinking_wave()`, `_print_status()`, `_update_spinner()` | `StatusBar` reactive |
| `_flush_new_lines()`, `_pending_running`, `_printed_count` | `TranscriptView` + `ConsolePrint` message |
| `SlashCommandHandler` class | `CommandModals` + existing `CommandDispatcher` |
| `mention_input.py` — entire file | `InputPanel` + Textual event system |
| `input_area.py` — `prompt_ansi`, `prompt_markup`, `footer_ansi`, `footer_markup` | CSS + `ModeFooter.render()` |
| `_raw_mode()`, `_scrub_cursor()`, `_erase_below()`, `_redraw()`, `_apply_cursor()` | Deleted (no raw ANSI) |
| `menu.py` — `MenuDriver`, `MenuWidget` | Textual `ModalScreen` |
| `widgets/dropdown.py` | `TriggerMenu` |
| `widgets/config_menu.py` | Textual modal or inline config widget |
| `agent_turn.py` — `_watch_input()`, `_spin()`, `Live(...)` | `SpinnerPanel` + Textual keys |

---

## 8. Public API Compatibility

| Interface | How preserved |
|-----------|--------------|
| `InlineRenderer(model, adapter, ...)` constructor | `AgenthiccApp` accepts same args; `_status: StatusState` kept |
| `renderer.run(on_intent)` | `AgenthiccApp.run(on_intent)` — same async signature |
| `renderer._flush_new_lines()` | Shim: posts `TranscriptUpdated` to `TranscriptView` |
| `renderer.console.print(markup)` | `ConsoleShim.print()` → posts `ConsolePrint` message |
| `renderer._status.*` fields | `AgenthiccApp._status: StatusState` unchanged; widgets observe via reactives |
| `TranscriptModel.render()` | Unchanged — still returns `list[str]` Rich markup |
| `TUIEventAdapter` | Unchanged |
| `TriggerRegistry` / `TriggerHandler` | Unchanged |
| `agenthicc` CLI entry point | Unchanged |

---

## 9. Dependency Changes

```toml
# pyproject.toml  (net change)
[project.dependencies]
# add:
textual = ">=0.80"
# rich stays (transitive dep of textual; TranscriptModel.render() still returns Rich markup)
```

---

## 10. Migration Plan

### Phase 1 — Foundation (no visible user change)
1. Add `textual` to dependencies.
2. Create `AgenthiccApp` shell that internally delegates to the existing
   `InlineRenderer` so all existing paths continue working.
3. Implement `TranscriptView` as a read-only viewer of `TranscriptModel.render()`.
4. Write headless Pilot tests for `TranscriptView`.

### Phase 2 — Input Panel
5. Implement `InputPanel` with buffer editing, cursor, history, multi-line (Ctrl+J),
   and bracketed paste condense/expand.
6. Implement `TriggerMenu` backed by existing `TriggerRegistry`.
7. Implement `ModeFooter` with all notification overrides.
8. Wire `InputPanel.InputSubmitted` into `AgenthiccApp`.
9. Delete `mention_input.py`; remove `_raw_mode`, `_redraw`, `_scrub_cursor`,
   `_erase_below`, `_apply_cursor`.

### Phase 3 — Streaming & Status
10. Implement `SpinnerPanel` and `StatusBar`.
11. Rewrite `_run_agent_turn` signal handlers to post messages to `SpinnerPanel`;
    delete `_spin()`, `_watch_input()`, and `Live(...)`.
12. Wire `pending_queue` through `AgenthiccApp.pending_queue` reactive.

### Phase 4 — Command Modals
13. Implement `CommandModals` screens.
14. Wire into `CommandDispatcher`; delete `SlashCommandHandler` Rich output.

### Phase 5 — Cleanup
15. Delete `app.py` `InlineRenderer`, `menu.py`, `widgets/dropdown.py`, `widgets/config_menu.py`.
16. Slim `input_area.py` to constants only (`PROMPT_CHAR`, `CURSOR_CHAR`, `get_mode_str`).
17. Update all import paths throughout the codebase.
18. Run full test suite; add Pilot tests for all new widgets.

---

## 11. Testing Strategy

| Layer | Framework | Coverage |
|-------|-----------|---------|
| `TranscriptModel` | `pytest` (existing) | Unchanged — render, diff_lines, spinner |
| `InputPanel` | `textual.testing.Pilot` | Buffer editing, trigger, paste condense/expand, history, multiline, Enter submit |
| `TriggerMenu` | `textual.testing.Pilot` | Open on `@`/`/`, navigate, select, cancel, `can_activate` contexts |
| `SpinnerPanel` | `textual.testing.Pilot` | Tool rows appear on `ToolCallStarted`, update on `ToolCallComplete` |
| `StatusBar` | `textual.testing.Pilot` | Tick animation, token counts, hidden when idle |
| `CommandModals` | `textual.testing.Pilot` | Tables render, keyboard dismiss, /model switch |
| `AgenthiccApp` | `textual.testing.Pilot` | Full session: type → submit → transcript → streaming → complete |
| E2E | `pytest` (existing) | `test_agent_runner_e2e.py`, `test_argon2_scenario.py` — unchanged |

---

## 12. Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Textual's built-in `Input` doesn't support trigger dropdowns or paste condensing | Use custom `Widget` subclass; Textual `Input` is a reference, not a requirement |
| Textual's `asyncio` loop conflicts with `asyncio.run()` in `tui_session.py` | Use `app.run_async()` and move `on_intent` execution into a Textual `Worker`; `call_from_thread` for signal-handler callbacks |
| Rich markup strings in `TranscriptModel.render()` may render differently in Textual | `RichLog.write()` and `Markup` both accept Rich markup natively — test each turn type in Pilot |
| Existing pyte E2E tests depend on ANSI frame rendering (`render_frame_ansi`) | Replace pyte tests with equivalent Pilot tests once all widgets exist; `render_frame_ansi` can emit Textual's `export_screenshot()` instead |
| `_run_agent_turn` has 500+ lines of tightly coupled streaming + UI logic | Decouple in Phase 3: extract a pure `StreamingSession` dataclass that holds `_live_calls`, `_streaming_text`, etc., passed to both the signal handlers and `SpinnerPanel` |
| Textual CSS learning curve for team | Scope CSS strictly to layout and colour; all business logic stays in Python; provide a reference CSS file in Phase 1 |
