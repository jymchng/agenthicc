# PRD-68 — agenthicc TUI: Full Feature Expectations

This document is the authoritative list of every user-facing feature the
agenthicc TUI must deliver.  It is written from the user's perspective and
serves as a regression checklist and acceptance-test reference.

---

## 1. Live Status Bar (always-on, blank separator above)

The Live block sits at the bottom of the terminal.  A blank line separates it
from the scroll buffer above (PRD-73).  The status bar is the first element
inside the Live block and never bounces when tool calls are added to the scroll
buffer (PRD-73: `transient=True`, `auto_refresh=False`, single `_redraw()` path).

| # | Feature | Expected behaviour |
|---|---|---|
| 1.1 | Flower animation | A Unicode flower icon (`✿❀❁❃✾❋✽❊`) cycles every ~100 ms while the agent is active. |
| 1.2 | State label | `Thinking` (one bold character bouncing left↔right) while the LLM generates; `Running` while a tool executes; `↻ Recovering` (red) while the LLM responds to a tool failure; `Idle` otherwise. |
| 1.3 | Elapsed time | `│ Ns` for under a minute; `│ Mm Ns` for a minute or more (e.g. `│ 22s`, `│ 1m 5s`, `│ 10m 30s`). Displayed while the agent is active. No `Runtime:` prefix. |
| 1.4 | Token counts | `│ ↑ N,NNN ↓ N,NNN` (cyan input / green output) shown on line 1 alongside elapsed time, when non-zero. |
| 1.5 | Active tool name | `│ tool_name` appears next to the state when a tool is running. |
| 1.6 | Model name (line 2) | Always shows `provider/model` (e.g. `openai/poolside/laguna-xs.2`). |
| 1.7 | Session info (line 3) | `session-id  │  N turns  │  $cost`. Token counts are on line 1, not repeated here. |
| 1.8 | Width-safe | All three lines are truncated to terminal width. Never wrap or overflow. |
| 1.9 | Blank separator | A blank line appears between the last scroll-buffer line and the top border of the status bar. The blank line is the first element of the Live block; it never appears in the scroll buffer. |
| 1.10 | Dynamic height | `StatusComponent.height()` always equals the actual number of terminal rows rendered (including the blank separator). The Live block resizes correctly when status content changes. |

### §1 Illustration — status bar states

**Idle (no agent running)**
```
────────────────────────────────────────────────────────────────────────────────
✿ Idle
openai/poolside/laguna-xs.2
de88d5d3-cf1a-4571  │  3 turns  │  $0.012
────────────────────────────────────────────────────────────────────────────────
```

**Thinking — under one minute**
```
────────────────────────────────────────────────────────────────────────────────
❀ Thinking │ 22s │ ↑ 1,024 ↓ 0
openai/poolside/laguna-xs.2
de88d5d3-cf1a-4571  │  3 turns  │  $0.012
────────────────────────────────────────────────────────────────────────────────
```

**Thinking — over one minute**
```
────────────────────────────────────────────────────────────────────────────────
❁ Thinking │ 1m 5s │ ↑ 8,192 ↓ 512
openai/poolside/laguna-xs.2
de88d5d3-cf1a-4571  │  4 turns  │  $0.031
────────────────────────────────────────────────────────────────────────────────
```

**Running a tool**
```
────────────────────────────────────────────────────────────────────────────────
❃ Running │ 2m 30s │ ↑ 4,096 ↓ 256 │ read_file
openai/poolside/laguna-xs.2
de88d5d3-cf1a-4571  │  4 turns  │  $0.018
────────────────────────────────────────────────────────────────────────────────
```

**Recovering after a tool error**
```
────────────────────────────────────────────────────────────────────────────────
✾ ↻ Recovering │ 10m 30s │ ↑ 16,384 ↓ 2,048
openai/poolside/laguna-xs.2
de88d5d3-cf1a-4571  │  7 turns  │  $0.091
────────────────────────────────────────────────────────────────────────────────
```

Line 1 layout (left to right, width-truncated):
`{flower} {state}  │  {elapsed}  │  ↑ {in} ↓ {out}  │  {active_tool}`

Elapsed format rules:
- `0 – 59 s` → `Ns` (e.g. `22s`)
- `≥ 60 s` → `Mm Ns` (e.g. `1m 5s`, `10m 30s`)

---

## 2. Scroll Buffer (conversation transcript)

Content appears above the always-on Live block and scrolls naturally.
All content is written by `ScrollBufferAppender` — the only component allowed
to call `console.print()`.  All events for one batch are flushed in a single
`with console:` context to prevent the status bar from flickering between items.

| # | Feature | Expected behaviour |
|---|---|---|
| 2.1 | Turn header | `● assistant (model-name)  HH:MM:SS` printed once when the agent starts. |
| 2.2 | User message | `❯ text` printed when the user submits. |
| 2.3 | Tool call line | `  ⎿ tool_name(key=val, …)  ✓/✗  Nms` printed when a call completes. Full args always shown. |
| 2.4 | Tool output preview | Up to 4 lines of output shown indented below the tool call line. |
| 2.5 | LLM text | Markdown-rendered text printed after each LLM sub-turn. |
| 2.6 | Thinking steps | `  → step text` (in-progress) and `  ✓ step text` (done) for extended thinking. |
| 2.7 | File modified | `  Modified: path/to/file` when a write/patch tool changes a file. |
| 2.8 | Error block | `ERROR message` in red when a turn fails. |
| 2.9 | @mention chips | `  @path/to/file  preview…` inline before the first tool call of a turn. |
| 2.10 | No duplicates | Each item appears exactly once. User message (`❯ text`) is appended once in `_handle_send` before the agent task is created. |
| 2.11 | No status bar content in scroll buffer | `✿ Idle`, separators, or footer lines must never leak into the transcript. |
| 2.12 | Single-flush batch | `ScrollBufferAppender._flush_batch()` wraps all `console.print()` calls for one batch in a single `with console:` context — one terminal write per batch, no intermediate renders. |
| 2.13 | Tool group collapse | Consecutive `tool_complete` events between two `text` events form a group. The first 5 in a group are printed in full. If a group exceeds 5, only the first 5 are shown and the remainder collapses to a single `  ⎿ ...and N more tool call(s)` line printed just before the next `text` or `error` event. Each new `text` or `turn_start` event begins a fresh group. `ConversationStore.tool_group_count: Signal[int]` tracks the live count for any Live-block component that wishes to display it. |

---

## 3. Input Bar

| # | Feature | Expected behaviour |
|---|---|---|
| 3.1 | Visible prompt | `❯ text▌` with a block cursor at the insertion point. Cursor moves with every keypress. |
| 3.2 | Left / Right | Move cursor one character left or right. |
| 3.3 | Home / End | Jump to start / end of the current logical line. |
| 3.4 | Up / Down (cursor) | Move cursor to the same column on the previous / next logical line inside a multi-line buffer. |
| 3.5 | Up / Down (history) | When on the first / last line, navigate command history. |
| 3.6 | Backspace | Delete the character to the left of the cursor. |
| 3.7 | Ctrl+U | Clear the entire buffer. |
| 3.8 | Ctrl+J | Insert a newline at the cursor (multi-line input). |
| 3.9 | Enter | Submit the current buffer as a new message. Input bar clears immediately. |
| 3.10 | Paste condensation | Bracketed paste inserts the text but shows `[Pasted text #N M chars]` (or `+L lines`). Backspace deletes the whole paste. Ctrl+V expands the full content into the composer. Any other key exits condensed mode. While condensed, footer row 2 shows `Ctrl+V Expand paste │ Backspace Delete │ Enter Submit as-is` instead of the normal hints. |
| 3.11 | Soft-wrap, no truncation | All non-condensed composer content (normal typing, multi-line input, expanded paste) is rendered by `_render_multiline`. Content is never truncated with `…`. Lines longer than the terminal width are soft-wrapped by Rich. The Live block grows to fit. |
| 3.12 | Unified composer renderer | `ComposerComponent.render()` has exactly two paths: (a) condensed paste label → single fixed line with `_fit`; (b) everything else → `_render_multiline`. There is no separate single-line path. Single-line, multi-line, and expanded paste all share one rendering function. |

### §3 Illustration — composer states

**Normal single-line typing**
```
────────────────────────────────────────────────────────────────────────────────
❯ what does the main function do?▌
────────────────────────────────────────────────────────────────────────────────
  AUTO  (shift+tab to cycle)  │  ctrl+j = ↵
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
```

**Multi-line input (Ctrl+J inserts newlines)**
```
────────────────────────────────────────────────────────────────────────────────
❯ Please refactor this function:
  it has too many arguments and
  no docstring▌
────────────────────────────────────────────────────────────────────────────────
  AUTO  (shift+tab to cycle)  │  ctrl+j = ↵
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
```

**Paste condensed**
```
────────────────────────────────────────────────────────────────────────────────
❯ [Pasted text #1 +47 lines]▌
────────────────────────────────────────────────────────────────────────────────
  AUTO  (shift+tab to cycle)  │  ctrl+j = ↵
Ctrl+V Expand paste  │  Backspace Delete  │  Enter Submit as-is
```

**Paste expanded — multi-line (Ctrl+V)**
```
────────────────────────────────────────────────────────────────────────────────
❯ def greet(name: str) -> str:
      """Return a greeting."""
      return f"Hello, {name}"▌
────────────────────────────────────────────────────────────────────────────────
  AUTO  (shift+tab to cycle)  │  ctrl+j = ↵
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
```

**Paste expanded — long single line (Ctrl+V, no newlines in paste)**
```
────────────────────────────────────────────────────────────────────────────────
❯ {"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"max_tokens":
  512,"temperature":0.7,"stream":true}▌
────────────────────────────────────────────────────────────────────────────────
  AUTO  (shift+tab to cycle)  │  ctrl+j = ↵
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
```

Content is soft-wrapped at terminal width; `…` never appears.

---

## 4. Footer

| # | Feature | Expected behaviour |
|---|---|---|
| 4.1 | Mode line (row 1) | `⏵⏵ ModeName  (shift+tab to cycle)  │  ctrl+j = ↵` — updated when mode changes, unchanged during streaming unless mode is switched. |
| 4.2 | Context hints (row 2) | `Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention` — unchanged during streaming. |
| 4.3 | Notification | Transient text replaces row 2 for ~2 s (e.g. `❖ Switched to Plan mode`, `Press Ctrl+C again to exit.`). Clears on timeout or next keypress. |
| 4.4 | Recovering hint | When `AgentState.RECOVERING`, row 2 shows `ESC Cancel  (LLM responding to tool error)`. |
| 4.5 | Paste-condensed hint | When a paste is condensed, row 2 shows `Ctrl+V Expand paste │ Backspace Delete │ Enter Submit as-is` instead of the normal hints. Priority is: transient notification > paste hint > normal state hints. The hint disappears automatically when Ctrl+V expands the paste or Backspace deletes it. |

---

## 5. Trigger System

| # | Feature | Expected behaviour |
|---|---|---|
| 5.1 | `@` opens file picker | Typing `@` (or `@` followed by a path fragment) opens the @-mention dropdown. |
| 5.2 | `/` opens command picker | Typing `/` at the start of a word opens the slash-command dropdown. |
| 5.3 | Dropdown navigation | Up/Down arrows navigate matches; selected item is highlighted. |
| 5.4 | Enter selects and submits if no match | Enter inserts the selected item (if any) and closes the overlay. When there are **no matches**, Enter commits the typed text AND submits the message immediately (one key press). |
| 5.5 | Tab selects without submitting | Tab commits the selected item (or typed text if no match) into the buffer and closes the overlay. No message submission — the user continues typing. |
| 5.6 | Space commits command, exits | Space in the slash-command picker commits the highlighted command with a trailing space so the user can type arguments without a second Enter. |
| 5.7 | Esc to cancel | Closes the overlay, restores the buffer to its pre-trigger state. |
| 5.8 | Backspace into token | Backspace at the end of a committed `@path` or `/cmd` token re-opens the picker with the existing fragment. |
| 5.9 | Hint text | A short hint/description appears below the match list. |
| 5.10 | Works during streaming | `@` and `/` open the picker while the agent is running. The Live block stays active. |
| 5.11 | No double input bar | When the overlay is active the composer is NOT also rendered — exactly one prompt line is visible. |
| 5.12 | Typing after committed mention re-enters overlay | Selecting `@docs/` then typing `.` re-opens the overlay with fragment `docs/.`. Enter submits; the input bar clears correctly. |
| 5.13 | `_init_trigger` walks backward | The overlay correctly identifies the trigger char even when the last character of the initial buffer is not a trigger character (e.g. `["@","d","o","c","s","/","."]` → trigger `@`, fragment `docs/.`). |

---

## 6. Agent Interaction

| # | Feature | Expected behaviour |
|---|---|---|
| 6.1 | Submit message | Enter sends the current buffer to the agent. All submission paths go through `_prepare_submission()` — a single method that clears the buffer, resets paste state, resets the Ctrl+C counter, and updates the display. The input bar is always empty after submission. |
| 6.2 | Queue during streaming | Typing and pressing Enter while the agent runs queues the message with `⌛ Queued` confirmation. Queued messages are dispatched sequentially after the current turn completes or is interrupted. Slash commands in the queue are dispatched through the command registry (never forwarded raw to the agent). The `⌛ Queued` notification clears once the queue is fully processed. Queued messages appear in the transcript exactly as directly-submitted messages. |
| 6.3 | ESC cancels agent | Pressing ESC while the agent is streaming cancels the current turn immediately. Status returns to Idle. Any messages queued before the interrupt are sent as the next turn. |
| 6.4 | Ctrl+C cancels agent | Same as ESC during streaming. Queued messages are preserved. |
| 6.5 | Double Ctrl+C exits | First press clears the buffer and shows `Press Ctrl+C again to exit.` on the footer. Second press shows the resume hint and exits. Any other key between presses resets the counter. |
| 6.6 | Session resume hint | On exit the terminal shows `agenthicc --resume <id>` / `agenthicc --continue`. |
| 6.7 | Recovering state visible | When a tool fails and the LLM is generating a response to the error, the status bar shows `↻ Recovering` (red, animated). The footer hint reads `ESC Cancel  (LLM responding to tool error)`. |

---

## 7. Mode System (PRD-65, PRD-75)

Mode is a first-class reactive value (`Signal[RuntimeMode]` on `AppState`).
`ModeManager` owns all writes to this signal.

| # | Feature | Expected behaviour |
|---|---|---|
| 7.1 | Shift+Tab cycles modes | Cycles through the registered modes (Auto → Plan → Ask → Review → Safe → Debug → Auto) in **both idle and streaming** input modes. |
| 7.2 | Mode badge in footer | Footer line 1 updates immediately when mode changes. |
| 7.3 | Mode notification | `❖ Switched to Plan mode` appears briefly (2 s) on the footer. |
| 7.4 | Mode system prompt | The active mode's `system_prompt_suffix` is prepended to the agent's system prompt at turn start. |
| 7.5 | Mode blocks tool capabilities | In Plan / Ask / Review / Safe modes, tools with blocked capabilities (WRITE, GIT_WRITE, EXECUTE, NETWORK) are blocked at runtime via `ToolCapabilityGate`. The model receives a structured error result instead of the tool executing. |
| 7.6 | Mode change takes effect immediately | Switching mode mid-turn via Shift+Tab takes effect on the next tool invocation in the same turn. `ToolCapabilityGate` reads `app_state.active_mode()` at call time, not at turn start. |

---

## 8. Commands

Built-in and project commands are dispatched by the **command registry** and
never passed to the agent as free-text queries.  `menu_factory` always takes
priority over `handler` regardless of whether args are present (PRD-70).

| # | Feature | Expected behaviour |
|---|---|---|
| 8.1 | `/config` | Opens the configuration editor overlay. Navigate Up/Down, edit with Enter, save with `s`, close with Esc. |
| 8.2 | `/model` | Shows or switches LLM provider/model. |
| 8.3 | `/models` | Lists all available providers and models. |
| 8.4 | `/status` | Displays session info. |
| 8.5 | `/skills` | Prints a table of all loaded skills from the in-process registry. |
| 8.6 | `/help` | Opens the interactive help overlay (PRD-70). `/help /config` opens the detail view for `/config` directly. |
| 8.7 | `/cancel` | Cancels the currently running agent turn. |
| 8.8 | `/clear` | Clears the conversation transcript display. |
| 8.9 | `/mode [name]` | Shows or switches the active mode. |
| 8.10 | `/commands` | Lists all registered commands with their source and group. |
| 8.11 | Interception before agent | Any `/command` is dispatched to the command registry first. Registered commands (with or without a handler) never reach the agent. Unknown commands fall through to the agent as free text. |
| 8.12 | Project commands with no handler | Project `CommandSpec` entries with no Python handler print `Command /gen has no handler. Add a handler in .agenthicc/commands/` and return without involving the agent. |
| 8.13 | `/help` overlay | `/help` opens a scrollable grouped command list (LIST view). Enter on a command opens a DETAIL view showing name, description, group, args, aliases, and source. Esc in DETAIL returns to LIST; Esc in LIST closes the overlay. `/help /cmd` opens DETAIL for `/cmd` directly. |

---

## 9. Plugin System

| # | Feature | Expected behaviour |
|---|---|---|
| 9.1 | Per-project tool plugins | `.agenthicc/tools/*.py` exporting `TOOLS = [fn1, fn2]` (`@tool()`-decorated async functions). Tools are immediately available to the agent. |
| 9.2 | Per-project command plugins | `.agenthicc/commands/*.py` exporting `COMMANDS = [Command(…)]` with handlers, or bare `CommandSpec` entries for dropdown hints. Full `Command` objects are registered directly; `CommandSpec` entries are wrapped. |
| 9.3 | Per-project skill plugins | `.agenthicc/skills/<slug>/SKILL.md` files are discovered and shown in `/skills`. Each skill is registered as `/<slug>` in the command registry. Invoking a skill starts an agent turn immediately (no second Enter). The skill's instruction body is sent as the full turn text. |
| 9.4 | User-global plugins | `~/.agenthicc/tools/`, `~/.agenthicc/commands/`, and `~/.agenthicc/skills/` are loaded first; project-local plugins shadow user-global ones by name. |
| 9.5 | Mode plugins | `.agenthicc/modes/*.py` can register custom modes. |
| 9.6 | No conflict crashes | Conflicting tool names log a warning (last writer wins); the application never crashes on plugin load errors. |
| 9.7 | Dependency declaration | Plugin files may export `DEPENDENCIES = ["package>=version"]`; missing deps produce a clear error, not an import crash. |
| 9.8 | Startup confirmation | Loaded plugin counts are printed once at startup (`Loaded 5 tool(s) from .agenthicc/tools/`). |

---

## 10. Tool Capability Gate (PRD-76)

Every `@tool()`-decorated function carries a capability annotation via
`@set_metadata("capabilities", frozenset({ToolCapability.WRITE, …}))`.  The
pre-built shorthands (`@tool_read`, `@tool_write`, `@tool_execute`, etc.) are
the standard way to annotate tools.

| # | Feature | Expected behaviour |
|---|---|---|
| 10.1 | Capability annotation | Every in-tree tool (`read_file`, `git_commit`, `run_bash`, `send_email`, etc.) carries a `@tool_read` / `@tool_write` / `@tool_execute` / `@tool_git_read` / `@tool_git_write` / `@tool_network` shorthand decorator. |
| 10.2 | Mode-based blocking | `ToolCapabilityGate` (a global `ToolHook`) checks `app_state.active_mode().blocked_capabilities` on every tool call. In Plan / Ask / Review / Safe modes, `{WRITE, GIT_WRITE, EXECUTE, NETWORK}` are blocked. |
| 10.3 | Structured error on block | When a tool is blocked, the model receives `{"ok": false, "error": "Tool 'write_file' requires write — blocked in Ask mode. Switch to Auto or Debug mode."}`. The tool never executes. |
| 10.4 | Open-by-default | Tools without a `@set_metadata("capabilities", …)` annotation are never blocked regardless of mode. |
| 10.5 | Live mode switching | Switching mode via Shift+Tab mid-turn takes effect on the next tool call. `ToolCapabilityGate` reads the live mode signal, not a snapshot from turn start. |
| 10.6 | Project tool annotation | Project plugin authors annotate their tools with the same `@tool_read` / `@tool_write` shorthands from `agenthicc.tools.capabilities`. |

---

## 11. Session Lifecycle

| # | Feature | Expected behaviour |
|---|---|---|
| 11.1 | New session on startup | A UUID session ID is created and shown in the status bar. |
| 11.2 | `--resume <id>` | Resumes a previous session; prior conversation is shown in the scroll buffer. |
| 11.3 | `--continue` | Finds the most recent session for the current directory and resumes it. |
| 11.4 | Session persistence | All conversation events are persisted to `~/.agenthicc/sessions/<id>/conversation.jsonl`. |
| 11.5 | Terminal restored on exit | After any exit path (Ctrl+C ×2, Ctrl+D, exception), ECHO, ICANON, cursor visibility, and bracketed paste are all restored. No broken terminal. |

---

## 12. Resize Handling

| # | Feature | Expected behaviour |
|---|---|---|
| 12.1 | SIGWINCH triggers redraw | Resizing the terminal redraws the Live block at the new width immediately. |
| 12.2 | Width-safe rendering | All components truncate to the new width. |

---

## 13. Headless Mode

| # | Feature | Expected behaviour |
|---|---|---|
| 13.1 | `--headless` flag | Reads prompts from stdin (one per line), outputs JSON-lines to stdout. No TUI. |
| 13.2 | JSON event schema | Each event is `{"type": "…", "payload": {…}, "timestamp": float}`. |

---

## 14. Input Capability Pipeline (PRD-74)

The `UnifiedInputSession` dispatches keystrokes through an ordered pipeline of
`Capability` instances.  Each input mode (`IDLE`, `STREAMING`) declares its
capability list as a module-level constant — the single source of truth for
what each mode supports.

### 14.1 Capabilities

| Capability | Handles | Present in |
|---|---|---|
| `OverlayCapability` | Routes all keys to the active overlay when one is open | IDLE, STREAMING |
| `CtrlCCapability` | Double-Ctrl+C exit sequence; resets counter on any other key | IDLE |
| `CtrlDCapability` | Submit non-empty buffer or exit on empty buffer | IDLE |
| `InterruptCapability` | Ctrl+C / ESC → `InterruptAgentCommand` (cancel agent) | STREAMING |
| `TriggerCapability` | All registered trigger chars (`@`, `/`, `#`, `!`) via `TriggerManager.resolve()` | IDLE, STREAMING |
| `PasteCapability` | Bracketed paste and Ctrl+V expansion | IDLE, STREAMING |
| `SubmitCapability` | Enter; `commit_history=True` in idle, `False` in streaming | IDLE, STREAMING |
| `NewlineCapability` | Ctrl+Enter / Ctrl+J — insert literal newline | IDLE, STREAMING |
| `BackspaceCapability` | Backspace; re-enters trigger overlay when cursor is inside a committed trigger token | IDLE, STREAMING |
| `ClearCapability` | Ctrl+U — clear entire buffer | IDLE, STREAMING |
| `CursorCapability` | Left / Right / Home / End — move insertion cursor | IDLE |
| `HistoryCapability` | Up / Down — navigate command history | IDLE |
| `ModeCycleCapability` | Shift+Tab — cycle through registered input modes | **IDLE, STREAMING** |
| `InsertCapability` | Key.CHAR fallback — insert char; re-enters trigger overlay when typing into existing token. **Must always be last.** | IDLE, STREAMING |

### 14.2 Invariants

| # | Invariant |
|---|---|
| 14.1 | `IDLE_CAPABILITIES` and `STREAMING_CAPABILITIES` are the single source of truth for what each mode supports. |
| 14.2 | Adding a new trigger char requires one `manager.register()` call — no changes to `capabilities.py`. |
| 14.3 | Adding a new input mode requires only declaring a new capability list. |
| 14.4 | Trigger chars (`@`, `/`, etc.) work identically in IDLE and STREAMING via `TriggerManager.resolve()`. |
| 14.5 | Shift+Tab (mode cycling) works in **both** IDLE and STREAMING. |
| 14.6 | All submission paths go through `_prepare_submission()` — the single place for buffer-clear + InputState-update before dispatch. |

---

## 15. CLI (PRD-79)

The CLI uses a decorator-based registry with three-layer command discovery.
All command extensions — built-in, user-global, and project-local — are
wired through the same `@command()` / `@group()` decorators and dispatched
by the same `main()` with no special-casing per depth.

### 15.1 Subcommand system

| # | Feature | Expected behaviour |
|---|---|---|
| 15.1 | Arbitrarily nested subcommands | `agenthicc plugin trust add my-plugin` (three levels deep) works. Depth is unlimited. |
| 15.2 | Single-decorator registration | Adding a new subcommand at any depth requires one `@command(*path)` decorator entry — no changes to `main()`, `parse_cli()`, or any other handler. |
| 15.3 | CLIContext injection by type | `CLIContext` is injected into handler parameters by annotation type only. The parameter name is irrelevant — `ctx`, `app`, `c`, `session: CLIContext` all work identically. |
| 15.4 | Signature-driven argparse | Handler parameters become argparse arguments automatically: `str` (no default) → positional; `bool = False` → `--flag`; `str = "val"` → `--option VALUE`. |
| 15.5 | Source badges in `--help` | `agenthicc --help` shows `[builtin]`, `[user]`, or `[project]` next to each command so provenance is always visible. |

### 15.2 Configuration wiring

| # | Feature | Expected behaviour |
|---|---|---|
| 15.6 | `--set` for config values | `--set execution.model=gpt-4o` merges into `AgenthiccConfig` via the normal TOML precedence chain (layer 5 — highest for config). |
| 15.7 | `BehaviourSettings` TOML section | `[behaviour]` in `agenthicc.toml` sets developer convenience defaults (e.g. `verbose = true`). These ARE storable in TOML. |
| 15.8 | `CLIFlags` — ephemeral, not TOML-settable | Security-bypassing flags (`--dangerously-skip-permissions`, etc.) live in `CLIFlags`, are frozen at session startup, and have **no TOML path** — the user must retype them every invocation. |
| 15.9 | `AppState.cli_flags` | `CLIFlags` is set once on `AppState` at startup and never changes. Runtime components (`ApprovalGate`, etc.) read it directly. |
| 15.10 | `--dangerously-skip-permissions` | Disables all `ApprovalGate` prompts for the session, in all modes (Guard, Ask, Review, etc.). The flag is intentionally not settable in `agenthicc.toml`. |

### 15.3 User-defined commands

| # | Feature | Expected behaviour |
|---|---|---|
| 15.11 | Built-in layer | `agenthicc/cli/commands/*.py` — lowest priority. |
| 15.12 | User-global layer | `~/.agenthicc/cli/*.py` — discovered at startup without any trust step. Shadows builtins. |
| 15.13 | Project-local layer | `.agenthicc/cli/*.py` — discovered after `agenthicc trust cli` writes `.agenthicc/trusted_cli.json`. Shadows builtins and user-global. |
| 15.14 | Python plugin format | A file in `.agenthicc/cli/` using `@command(*path)` and `@group(*path)` from `agenthicc.cli.registry` registers commands that appear in `agenthicc --help` with `[project]` badge. |
| 15.15 | TOML shorthand | `.agenthicc/cli.toml` with `[[command]]` entries using `run = "script {arg}"` creates working commands without any Python code. |
| 15.16 | Shadow semantics | A project command with the same path as a built-in or user-global command silently replaces it. |
| 15.17 | Trust enforcement | Project-local `.agenthicc/cli/*.py` files are NOT loaded until `agenthicc trust cli` is run (unless `PluginSettings.auto_trust = true`). Modified files invalidate the trust manifest and block loading with a warning. |
| 15.18 | `ContextVar` source tagging | `@command()` decorators executed during file loading are tagged with their source (`"builtin"`, `"user"`, `"project"`) via a `ContextVar` — no caller changes needed. |

---

## Acceptance Criteria (summary)

A release is shippable when:

1. All sections above pass manual verification in a real terminal.
2. Zero terminal corruption: 50 agent turns produce no broken cursor state, no stray control characters, no loss of ECHO on exit.
3. ESC and Ctrl+C cancel the agent within 200 ms of the keypress.
4. Triggers work: `@` and `/` open dropdowns with correct matches in both idle and streaming modes.
5. No duplicate rendering: each tool call, turn header, and LLM text line appears exactly once.
6. Plugin hot-path: `.agenthicc/tools/`, `.agenthicc/commands/`, and `.agenthicc/skills/` are picked up on the next launch.
7. Mode cycling (Shift+Tab) works during both idle and live streaming and the footer updates immediately.
8. In Plan / Ask / Review / Safe mode, WRITE / GIT_WRITE / EXECUTE / NETWORK tools are blocked and the model receives a structured error.
9. `agenthicc deploy staging` works when `.agenthicc/cli/deploy.py` defines `@command("deploy", "staging")`.
10. `agenthicc --dangerously-skip-permissions` disables all approval prompts for the session without requiring a config file change.
