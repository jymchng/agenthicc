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
| 2.2 | User message | `❯ text` printed when the user submits. The `❯` is **bold yellow** in all four rendering contexts (see §2.2a). |
| 2.2a | `❯` rendering paths | The chevron appears in four independent code paths that must always share the same colour. **(1)** `tui/input/renderer.py` — raw ANSI `\x1b[1;33m` for the CBREAK input bar (Live block not running). **(2)** `tui/workspace/components.py` `ComposerComponent` — Rich `style="bold yellow"` inside the Live Group. **(3)** `tui/workspace/overlays/prompt.py` `PromptOverlay._render_prompt_line()` — Rich markup `[bold yellow]` for overlay text-input states. **(4)** `tui/workspace/appender.py` `user_message` event — Rich markup `[bold yellow]` echoed to the scroll buffer. When changing the chevron colour all four must be updated. |
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
| 2.13 | Tool group collapse — configurable threshold | Consecutive `tool_complete` events between two `text` events form a group. The first `max_live_tool_calls` (default 5, configurable via `[tools] max_live_tool_calls` in `agenthicc.toml`) are printed individually to the scroll buffer. Tool calls beyond that threshold are not printed immediately. `ScrollBufferAppender` reads the threshold from `ToolSettings.max_live_tool_calls` passed through `Workspace` at construction. |
| 2.14 | Tool group collapse — live overflow indicator | While a tool group is over threshold, `ConversationStore.live_tool_overflow: Signal[int]` is updated with the current overflow count on each arriving call. `FooterComponent` renders a live `  ⎿ ...and N more tool call(s)` row in the footer that increments in real time. When the group closes (`text` or `error` event), the signal is reset to 0 (footer row disappears) and a permanent `  ⎿ ...and N more tool call(s)` line is printed to the scroll buffer. `FooterComponent.height()` accounts for this extra row when computing the Live block height. |
| 2.15 | Scroll-buffer event renderer registry | `ScrollBufferAppender` dispatches `ConversationEvent.kind` through a module-level `_RENDERERS: dict[str, Callable]` rather than a `match` block. Each renderer is registered with `@register_renderer("kind")` at module load time. Adding a new event kind requires only adding a new `@register_renderer` function — no changes to `_render_one()`. The registry, decorator, and all built-in renderers live in `tui/workspace/appender.py`. |

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
| 9.2 | Per-project command plugins | `.agenthicc/commands/*.py` exporting `COMMAND` (single `Command`) or `COMMANDS` (list). Full `Command` objects are registered directly into `UnifiedCommandRegistry`. Files beginning with `_` are skipped. |
| 9.2a | Command file format | Each file may export `COMMAND = Command(name, description, group, argument_hint, aliases, handler, menu_factory, source_id)` and/or `COMMANDS = [Command(…), …]`. `handler` is `Callable[[CommandContext], bool]` — returning `True` means handled (message never reaches agent); `False` falls through to agent. `menu_factory` takes precedence over `handler` when both are set. |
| 9.2b | Command dependency check | A plugin file may export `DEPENDENCIES = ["package>=version"]`. Missing packages are pre-checked before import; file is silently skipped with a warning if any dep is absent. |
| 9.2c | Command `source_id` | If omitted, auto-derived as `"command-plugin:<file_stem>"`. Used for namespaced unregistration and shown in `/commands` output. |
| 9.2d | Command surfaces | Every registered command automatically appears in (1) the `/` dropdown with group headers, (2) `/help` grouped list and detail view, and (3) `/commands` table — no extra registration step. |
| 9.2e | Command load order | User-global (`~/.agenthicc/commands/`) loaded first, then project-local (`.agenthicc/commands/`). Last-write-wins: project command with same name silently replaces user-global. |
| 9.3 | Per-project skill plugins | `.agenthicc/skills/<slug>/SKILL.md` files are discovered and shown in `/skills`. Each skill is registered as `/<slug>` in the command registry. Invoking a skill starts an agent turn immediately (no second Enter). The skill's instruction body is injected into the agent system prompt. |
| 9.3a | Skill directory format | Each skill is a **directory** (not a single file): `.agenthicc/skills/<slug>/` containing `SKILL.md` (required), `reference.md` (optional — injected via `{reference}`), and `template.md` (optional — appended to body). |
| 9.3b | Skill frontmatter fields | YAML block in `SKILL.md`: `name`, `description`, `author`, `tags`, `suggestedTopics` (list of words), `disallowAutoTriggering` (bool), `tools`, `disabledTools`, `maxTurnDepth`, `model`. All fields are optional except `SKILL.md` itself. |
| 9.3c | Skill body placeholders | `` !`shell-cmd` `` → replaced with command stdout (15 s timeout). `{0}`, `{1}` → positional args. `{session}` → session ID. `{effort}` → effort level. `{reference}` → contents of `reference.md`. |
| 9.3d | Skill auto-triggering | When the user's message contains any word from `suggestedTopics`, the skill body is automatically appended to the agent system prompt for that turn. `disallowAutoTriggering: true` disables this; the skill is only usable via `/{slug}`. |
| 9.3e | Skill injection point | Matched skills are appended to the system prompt as `## Skill: {name}\n{processed_body}`, after the mode/workflow prompt suffix and before the tool description block (`agent_turn.py:261–294`). |
| 9.3f | Skill load order | User-global (`~/.agenthicc/skills/`) loaded first, then project-local (`.agenthicc/skills/`). Same slug → project wins. |
| 9.4 | User-global plugins | `~/.agenthicc/tools/`, `~/.agenthicc/commands/`, and `~/.agenthicc/skills/` are loaded first; project-local plugins shadow user-global ones by name. |
| 9.5 | Mode plugins | `.agenthicc/modes/*.py` (project-local) and `~/.agenthicc/modes/*.py` (user-global) are scanned at startup by `discover_mode_plugins()` and registered into `ModeRegistry`. Project-local modes override user-global modes; both can override builtins of the same name. Failed loads are logged at WARNING with file path and error; the session continues. |
| 9.5a | Mode file format | Export `MODE = Mode(name, label, description, colour, system_patch, tool_filter, source_id)` or `MODES = [Mode(…), …]`. `label` becomes the badge shown in the footer. `colour` is the Rich colour applied to badge and name. `system_patch` is appended to the system prompt when the mode is active. `source_id` is auto-derived as `"mode-plugin:<stem>"` if left at default `"builtin"`. |
| 9.5b | Mode load order | `~/.agenthicc/modes/` loaded first (user-global), `.agenthicc/modes/` loaded second (project-local). Last-write-wins per name — project overrides user-global; user-global overrides builtins. |
| 9.5c | Workflow plugins | `.agenthicc/workflows/*.py` (project-local) and `~/.agenthicc/workflows/*.py` (user-global) are scanned by `build_workflow_registry()` at session startup. Each file must contain a `WorkflowPlugin` subclass with `name`, `description`, `mode_bindings`, and `phases`. Discovered workflows are passed to `ModeManager` so they appear in the mode → workflow binding map. |
| 9.5d | Workflow file format | `class MyFlow(WorkflowPlugin): name="my_flow"; description="…"; mode_bindings=["Plan"]; phases=[PhaseSpec(name="plan", agent_type="auto", next="execute"), …]`. `mode_bindings` declares which modes auto-trigger this workflow on user submit. |
| 9.5e | Workflow load order | `~/.agenthicc/workflows/` loaded first, `.agenthicc/workflows/` loaded second. Project-local workflow with the same name shadows user-global. |
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

## 16. Workflow System (PRD-81, PRD-87, PRD-88, PRD-89, PRD-90, PRD-91)

Workflows are sequences of phases executed by purpose-specific agents.
Each phase is backed by a named entry in `AgentsRegistry`, which holds a
`@agent(system=…)`-decorated class as the canonical system-prompt source.
The active `RuntimeMode` determines which workflow runs when the user submits
a message.

### 16.1 Workflow phases

| # | Feature | Expected behaviour |
|---|---|---|
| 16.1 | Phase execution | Each phase calls `_run_agent_turn()` so conv_store lifecycle (begin_turn / end_turn), real-time text streaming, token accounting, and tool-signal routing work identically to a direct agent turn. |
| 16.2 | Phase-filtered tools | Each phase receives only the tools whose capabilities fit within (a) the session mode's `blocked_capabilities` ceiling and (b) the phase's `allowed_capabilities` (derived from `ROLE_DEFAULT_ALLOWED[agent_type]` when not explicitly set). |
| 16.3 | Workflow context injection | Each phase agent receives a `[WORKFLOW CONTEXT]` block in its prompt containing the original user intent and a truncated summary of every prior phase's output. |
| 16.4 | Parallel phases | Phases whose `PhaseSpec.parallel_with` is non-empty run concurrently via `asyncio.gather`; the next phase waits for all siblings to complete. |
| 16.5 | Transition logic | `_determine_transition` checks `PhaseOutput.metadata["__next_phase__"]` first (dynamic override), then `on_reject` (when `approved=False`), then `spec.next`. |
| 16.6 | Per-phase max-iterations guard | A phase with `max_iterations != -1` is terminated with a workflow failure once it has been entered that many times. The default is `-1` (unlimited per-phase; the global cap applies instead). |
| 16.6a | Global phase-run cap | The workflow stops with `status="failed"` when the total number of phase runs reaches `len(phases) + 1`. For a 4-phase workflow this cap is 5 — the normal linear path (4 runs) plus one retry before the loop is terminated. The error message "stopped after N phase runs (limit: M)" is appended to the transcript. |
| 16.6b | Ctrl+C / interrupt cancellation | Pressing Ctrl+C (or ESC) during a workflow immediately cancels the active LLM call and propagates `CancelledError` through the full workflow runner stack, terminating the workflow. The `_stream()` method re-raises `CancelledError` rather than swallowing it; `_run_phase`, `_run_phase_loop`, `run_turn`, and `agent_task_body` each propagate it correctly. |
| 16.7 | Kernel events | `WorkflowRunStarted`, `WorkflowPhaseStarted`, `WorkflowPhaseCompleted`, and `WorkflowRunCompleted` are emitted to the kernel event log. `WorkflowPhaseCompleted` carries `role`, `full_text`, `approved`, and `structured` so that `restore_from_log` can reconstruct a `WorkflowContext` for resume. |

### 16.2 Status bar and footer during a workflow

| # | Feature | Expected behaviour |
|---|---|---|
| 16.8 | Phase badge in status | Status line 1 shows `│ Phase N/M: phase_name` alongside elapsed time and token counts while a workflow is running. N is the 1-based **definition position** of the current phase (`current_phase_index + 1`), not the cumulative run count. Plan always shows Phase 1/M, execute Phase 2/M, etc., regardless of how many times a phase has been retried via `on_reject`. |
| 16.9 | Workflow footer row | An optional third footer row shows `Workflow: name │ Phase N/M: phase_name` while `workflow_run.status == "running"`. Uses the same definition-position N as the status badge. Disappears after completion. |

### 16.3 Built-in workflows

| Workflow | Mode binding | Phases |
|---|---|---|
| `code_plan` | **Plan** | plan → execute → review → summarize (single agent, shared memory) |
| `plan_only` | Review | plan (read-only) |
| `review_only` | — | review (read-only) |
| `supervised` | — | plan → human_review → execute |
| `architect` | — | explore → plan → execute → verify |

### 16.4 AgentsRegistry

| # | Feature | Expected behaviour |
|---|---|---|
| 16.10 | Named agent classes | Each agent type (`planner`, `executor`, `reviewer`, `explorer`, `verifier`, `human`, `auto`) is backed by a `@agent(model=None, system=…)`-decorated Python class. The system prompt is stored on `AGENT_META` at decoration time and is the single source of truth. |
| 16.11 | Base system prompt | `BASE_SYSTEM_PROMPT` (from `agents/plugin.py`) is prepended to every agent's role-specific system prompt. It instructs all agents to use tools directly, never ask for information a tool can provide, and never invent file contents. |
| 16.12 | Tool schema delivery | `populate_agent_tools(agent_instance, tools)` (in `runners/tool_populator.py`) populates `meta.tools` from the registered tool list. Without this step the transport sends no tool schemas and the model falls back to text-based tool calls. This replaces the old `testing._build_runner_for_agent` call. |
| 16.13 | User/project agent plugins | Python files in `~/.agenthicc/agents/` (user-global) and `.agenthicc/agents/` (project-local) are discovered and registered, shadowing builtins by name. Files must export `AGENTS = [PluginClass]` or define an `AgentPlugin` subclass. |

### 16.5 Plan mode (`code_plan` workflow — PRD-90, PRD-91)

`code_plan` uses a **single agent with shared memory** across all four phases.
The agent builds up context progressively: what it explores in the plan phase
is available in the execute phase without any re-exploration.

| # | Feature | Expected behaviour |
|---|---|---|
| 16.14 | Single agent, shared memory | One `ShortTermMemory` instance (32 k tokens) is created at workflow start and shared across plan → execute → review → summarize. The agent carries full conversation history forward between phases. |
| 16.15 | Plan phase — write tools blocked | Plan mode has `blocked_capabilities = {WRITE, GIT_WRITE, EXECUTE, NETWORK}`. The plan phase runs in Plan mode, so the agent cannot call write or execute tools. `ToolCapabilityGate` enforces this; no per-phase filter needed. |
| 16.16 | Phase completion tools injected into every phase | `request_plan_approval`, `finalize_plan`, and `mark_execute_complete` are injected into every phase via `make_planner_tools()` + `make_executor_tools()`. All are `@tool()`-decorated so the LLM receives their schemas. The agent only calls the tool relevant to its current phase (guided by `system_prompt_override`). |
| 16.17 | `request_plan_approval` | Shows `PlanApprovalOverlay` (PRD-86) and suspends the agent via `ApprovalService.request_approval()`. Returns `{"approved": bool, "feedback": str}`. When `approved=True`, `feedback` includes an explicit instruction to call `finalize_plan()` next. The agent can call this multiple times if rejected. |
| 16.18 | `finalize_plan` enforcement | `finalize_plan` returns `{"ok": False, "error": "…"}` if called before `request_plan_approval` returned `approved=True`. The approval gate is machine-enforced in shared closure state (`approval_state["granted"]`). On success the message instructs the agent to write a short acknowledgment and stop — not begin implementing. |
| 16.19 | Plan not finalised → retry | If the plan phase ends without `finalize_plan()` being called, `_run_phase` returns `approved=False`. `on_reject="plan"` on the plan phase loops back. Up to 5 attempts (`max_iterations=5`) before the workflow fails. |
| 16.19a | Execute phase — `mark_execute_complete` gate | The execute phase requires the agent to call `mark_execute_complete(summary)` when all implementation tasks are done. Until that call is made, the phase loops in a continuation while-loop (see §16.16). |
| 16.19b | Review phase incomplete detection | If the review phase ends without a `<review>approved/rejected</review>` tag, `_parse_output_schema` returns `{"incomplete": True}`. `_run_phase` detects this and injects `metadata={"__next_phase__": "review"}`, retrying the review phase directly rather than routing back through execute. This distinguishes "deliberate rejection" (`on_reject="execute"`) from "review turn ended without a decision" (retry review). |

### 16.16 Phase continuation loop (`require_explicit_completion`)

`PhaseSpec.require_explicit_completion: bool = False` makes a phase run in a **while loop** inside `_run_phase` rather than advancing after a single `_run_agent_turn`. The loop exits only when the phase's completion tool fires.

| # | Feature | Expected behaviour |
|---|---|---|
| 16.51 | While-loop execution | When `require_explicit_completion=True`, `_run_phase` runs `_run_agent_turn` in a loop. Iteration 1 receives the full phase prompt. Subsequent iterations receive a short continuation prompt: "Continue implementing — you have not yet called mark_execute_complete(). Resume from where you left off…". The shared `ShortTermMemory` carries the full conversation history so each continuation is a genuine resume. |
| 16.52 | Loop exit on completion | The loop exits when `execute_event.is_set()` (agent called `mark_execute_complete`). The phase then returns `PhaseOutput` with the summary as `full_text`. |
| 16.53 | Loop exit on Ctrl+C | `CancelledError` / `KeyboardInterrupt` inside any iteration propagates immediately out of the loop. No retry on cancellation. |
| 16.54 | Per-iteration error handling | If an individual turn raises a non-cancellation exception (e.g. `JSONDecodeError` in a tool response), the error is logged and the loop breaks rather than crashing the whole phase. The event-not-set path then returns `approved=False`. |
| 16.55 | Max continuations cap | `max_iterations` on the `PhaseSpec` caps the number of continuation turns (not phase-level retries). `-1` (default) → 10 continuations. Exhausting the cap returns `approved=False`. For `code_plan` execute: `max_iterations=-1` (10 continuations × 40 sub-turns = 400 LLM sub-turns max). |
| 16.56 | No `on_reject` needed | Because the while loop retries internally, `on_reject` is not set on the execute phase. The phase-history accumulates only one entry per execute cycle regardless of how many continuation turns were needed. |
| 16.57 | Mode override wraps the whole loop | `PhaseSpec.mode_override` is applied once before the loop and restored in a `finally` block after the loop exits — not per-iteration. The execute phase runs in Auto mode for all its continuation turns. |
| 16.20 | Execute phase — mode switches to Auto | `PhaseSpec.mode_override="Auto"` on the execute phase temporarily sets `active_mode` to Auto for the duration of the turn. Write/execute tools are available. The original mode is restored in a `finally` block. |
| 16.21 | Review phase — read-only | Review phase runs in Plan mode (no override). The agent inspects changes, runs tests, and outputs `<review>approved</review>` or `<review>rejected: reason</review>`. |
| 16.22 | Per-phase focus via `system_prompt_override` | Each `PhaseSpec` carries a `system_prompt_override` that is prepended before the role's registry system prompt, guiding the single agent's focus per phase. |
| 16.23 | Summarize phase | Agent writes a concise summary of what was planned, implemented, and verified. |
| 16.24 | Mode auto-reset | After the workflow completes with `status="complete"`, the active mode switches automatically to Auto and a `✓ Workflow complete — switched to Auto mode` notification appears for ~2 s. |
| 16.25 | Intent threaded through every phase system prompt | `ctx.intent` (the original user message) is appended to the system prompt of every phase as a `[USER INTENT]` block. This ensures that if the LLM stops early within a phase (e.g. token limit, abrupt generation end) and the phase retries, the next turn's system prompt still carries the original request — independent of whether `shared_memory` conversation history is available. The intent appears in the system prompt even on retry turns where the `text` message is a short continuation reminder rather than the full original message. |

### 16.6 `PlanApprovalOverlay` (PRD-86, PRD-88)

| # | Feature | Expected behaviour |
|---|---|---|
| 16.21 | Plan display | The overlay renders the plan as Markdown in a scrollable viewport. The viewport height adapts to the terminal: `plan_visible = min(20, max(4, rows − 18))`, where 18 is the fixed chrome overhead (workspace blank + status + 2 borders + overlay header + top-border + indicator + bottom-border + 3 options + bottom-border + hint + workspace bottom-border + footer-with-workflow-line). The overlay height is therefore constant on every redraw — the content area is always padded to exactly `plan_visible` rows plus a fixed indicator row — preventing the Rich Live block from bleeding old content when the height would otherwise vary. |
| 16.21a | Dynamic viewport by terminal size | `plan_visible` is recomputed on every render from the live terminal dimensions (`shutil.get_terminal_size()`). Representative values: \| Terminal rows \| `plan_visible` \| \|---\|---\| \| 24 \| 6 \| \| 30 \| 12 \| \| 40 \| 20 (max) \| |
| 16.22 | Scrolling | `[` scrolls the plan viewport up one line; `]` scrolls down. Clamped at top and bottom. A scroll indicator (`↑ · lines A–B of Total · ↓`) is shown when the plan overflows the dynamic viewport. |
| 16.23 | Three options | `▶ Approve`, `Reject — add feedback`, `Approve — add instructions`. Navigated with `↑`/`↓`, confirmed with `Enter`. |
| 16.24 | Prompt input | Options 2 and 3 enter a PROMPTING state where the user types feedback or instructions. `Enter` submits; `Esc` returns to SELECTING. |
| 16.25 | Approval feedback to planner | The user's typed message is returned as `response.message` and delivered to the planner as the `feedback` or `instructions` field of the tool result. |

### 16.7 Phase mode switching (`PhaseSpec.mode_override`)

| # | Feature | Expected behaviour |
|---|---|---|
| 16.30 | Per-phase mode override | `PhaseSpec.mode_override: str \| None` — when set to a `RuntimeMode` name (e.g. `"Auto"`), `WorkflowRunner._run_phase` switches `app_state.active_mode` to that mode for the duration of the agent turn, then restores it. |
| 16.31 | `ToolCapabilityGate` enforcement | `ToolCapabilityGate` reads `active_mode().blocked_capabilities` on every tool call, so the mode switch takes effect immediately at the first tool call of the phase. |
| 16.32 | Restoration on error | The original mode is restored in a `finally` block — even if the agent turn raises an exception, cancellation, or keyboard interrupt. |
| 16.33 | Unknown mode silently skipped | If `mode_override` names a mode not in the registry, a warning is logged and the phase runs in the current mode unchanged. |

### 16.9 WorkflowConfig (PRD-95)

`WorkflowRunner.__init__` takes exactly three parameters: `definition`, `config: WorkflowConfig`, and `mode_manager`.

| # | Feature | Expected behaviour |
|---|---|---|
| 16.38 | `WorkflowConfig` dataclass | Holds all session-scoped singletons: `conv_store`, `app_state`, `processor`, `agent_runner`, `approval_svc`, `cfg`, `skills`, `plugin_tools`, `mcp_registry`, `mention_cache`, `agents_registry`, `completed_turns`. Frozen; `dataclasses.replace` is used to update `completed_turns` per run. |
| 16.39 | Constructed once per session | `TUISession.__init__` builds one `_wf_config_base` from `SessionContext`. Each workflow run gets a `replace`d copy with the current `completed_turns`. Resume uses `_wf_config_base` directly. |

### 16.10 Workflow resume (PRD-94, PRD-98)

| # | Feature | Expected behaviour |
|---|---|---|
| 16.40 | Kernel `Workflow` entry | `WorkflowRunStarted` creates a `Workflow` entry in `AppState.workflows` keyed by `run_id`, storing `name` (definition name) and `intent_text` (original user message). |
| 16.41 | Per-phase kernel node | `WorkflowPhaseCompleted` appends a `WorkflowNode` to the workflow with `result = {full_text, role, approved, structured}`. These nodes are replayed by `restore_from_log` to reconstruct `WorkflowContext`. |
| 16.42 | Auto-resume on `--resume` | `TUISession._schedule_workflow_resume()` scans `processor.get_state().workflows` after the session starts. Any workflow with `status != complete/failed` is reconstructed via `_reconstruct_workflow_context()` and resumed by `WorkflowRunner.resume(context)`. |
| 16.43 | `WorkflowRunner.resume()` | Skips phases already in `context.phase_outputs` using `_find_resume_phase()`, which replays the phase-transition graph (respecting `on_reject` paths). Runs `_run_phase_loop` from the first incomplete phase. |

### 16.11 Workflow plugin discovery

| # | Feature | Expected behaviour |
|---|---|---|
| 16.34 | Python-only plugins | Workflow plugins are Python `WorkflowPlugin` subclasses. TOML workflow files are not supported. |
| 16.35 | Discovery paths | Builtin → `~/.agenthicc/workflows/*.py` (user-global) → `.agenthicc/workflows/*.py` (project-local). Later sources shadow earlier ones by workflow name. |
| 16.36 | Mode binding | A workflow's `mode_bindings` list determines which modes offer it. The first binding wins for `default_workflow`; all bindings appear in `mode.workflows`. |
| 16.37 | Shadow warning | A project workflow shadowing a user workflow emits a startup warning. A user workflow shadowing a builtin logs at DEBUG level only. |

### 16.13 Phase display — definition position, not cumulative count

| # | Feature | Expected behaviour |
|---|---|---|
| 16.47 | `current_phase_index` on `WorkflowRun` | `WorkflowRun.current_phase_index: int = 0` holds the zero-based definition position of the active phase. Set by `_run_phase_loop` alongside `current_phase` using `next(i for i, p in enumerate(def.phases) if p.name == phase_name)`. |
| 16.48 | Status badge uses definition position | `Phase N/M` in both the status bar and footer uses `current_phase_index + 1` (not `len(phase_history) + 1`). Plan always shows Phase 1/M, execute Phase 2/M, review Phase 3/M — regardless of how many times a phase has been retried. |

### 16.14 Global phase-run cap removed; opt-in only

| # | Feature | Expected behaviour |
|---|---|---|
| 16.49 | No default global cap | `WorkflowDefinition.max_total_phase_runs: int = 0` defaults to 0 (no cap). The execute↔review retry loop runs freely. Per-phase `max_iterations` and Ctrl+C are the only termination mechanisms by default. |
| 16.50 | Opt-in cap | Set `max_total_phase_runs = N` on a `WorkflowDefinition` to enforce a hard ceiling. Used in tests and available for workflow authors who want a safety net. |

### 16.15 Scroll buffer tool-call collapse (configurable, live)

| # | Feature | Expected behaviour |
|---|---|---|
| 16.44 | Configurable threshold | The number of individually-rendered tool calls before collapsing is `ToolSettings.max_live_tool_calls` (default 5). Set via `[tools] max_live_tool_calls = N` in `agenthicc.toml`. Passed to `Workspace` → `ScrollBufferAppender` at startup. |
| 16.45 | Live overflow bridge | While a tool group exceeds the threshold, `ConversationStore.live_tool_overflow: Signal[int]` holds the current overflow count. The workspace renders `⎿ ...and N more tool call(s)` directly above the status bar (flush against the scroll-buffer content, no blank line between it and the last printed tool call). |
| 16.46 | Permanent scroll-buffer record | When the group closes (`text` or `error` event), `live_tool_overflow` is reset to 0 (bridge disappears) and a permanent `⎿ ...and N more tool call(s)` line is appended to the scroll buffer. |

---

## 17. Extensibility Registries

Three dispatch tables eliminate `if/elif` / `match` chains and make each
extension point open-closed: new behaviour is added by registering a new
entry, never by editing the dispatcher.

### 17.1 Scroll-buffer event renderer registry (`appender.py`)

| # | Feature | Expected behaviour |
|---|---|---|
| 17.1 | Registry location | `_RENDERERS: dict[str, _EventRenderer]` at module level in `tui/workspace/appender.py`. Populated at import time by `@register_renderer` decorators. |
| 17.2 | Decorator | `@register_renderer("kind")` — maps a `ConversationEvent.kind` string to a callable `(ScrollBufferAppender, ConversationEvent) -> None`. |
| 17.3 | Dispatcher | `ScrollBufferAppender._render_one()` does `renderer = _RENDERERS.get(ev.kind); if renderer: renderer(self, ev)`. Unknown kinds are silently ignored. |
| 17.4 | Built-in renderers | `turn_start`, `user_message`, `tool_complete`, `text`, `thinking_step`, `file_modified`, `error`, `mention_chips` — all registered at module load time in the same file. |
| 17.5 | Adding a new kind | Define a module-level function decorated with `@register_renderer("new_kind")` anywhere that imports the appender module. No changes to `_render_one()` needed. |

### 17.2 Approval overlay factory registry (`tui_session.py`)

| # | Feature | Expected behaviour |
|---|---|---|
| 17.6 | Registry location | `_overlay_registry: dict[str, type[Overlay]]` built inline inside `_on_approval_change()` in `tui_session.py`. |
| 17.7 | Dispatch | `factory = _overlay_registry.get(kind, ApprovalOverlay)` — `ApprovalRequest.kind` selects the overlay class; the default (`ApprovalOverlay`) handles all unregistered kinds. |
| 17.8 | Built-in entries | `"plan_review"` → `PlanApprovalOverlay`, `"questions"` → `QuestionsOverlay`. All others fall back to `ApprovalOverlay`. |
| 17.9 | Adding a new overlay kind | Add an entry to `_overlay_registry` in `_on_approval_change()` and import the new overlay class. |

### 17.3 Kernel bridge event handler registry (`kernel_bridge.py`)

| # | Feature | Expected behaviour |
|---|---|---|
| 17.10 | Registry location | `_EVENT_HANDLERS: dict[str, _InjectedHandler]` at module level in `tui/runtime/kernel_bridge.py`. Populated at import time by `@register_event_handler` decorators. |
| 17.11 | Decorator | `@register_event_handler("type")` — maps an event `type` string (from the legacy `inject_event()` bridge) to a callable `(KernelBridge, dict) -> None`. |
| 17.12 | Dispatcher | `KernelBridge.inject_event()` does `handler = _EVENT_HANDLERS.get(event.get("type", "")); if handler: handler(self, event)`. |
| 17.13 | Built-in handlers | `"agent_state_change"`, `"session_summary"`, `"notification"` — registered at module load time. |
| 17.14 | Adding a new event type | Define a module-level function decorated with `@register_event_handler("new_type")`. No changes to `inject_event()` needed. |

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
11. In Plan mode, the `code_plan` workflow runs: plan (explore + seek approval) → execute → review → summarize. All phases share one agent and one memory.
12. During the plan phase of `code_plan`, calling `write_file` or `run_bash` returns a capability-blocked error — the file is never modified.
13. During the execute phase of `code_plan`, `write_file` and `run_bash` succeed (mode switches to Auto for that phase only).
14. After the `code_plan` workflow completes successfully, the active mode automatically switches to Auto.
15. If the agent skips `finalize_plan()` after a plan rejection, the plan phase loops back (up to 5 times) before failing.
16. `--resume` on a session with an interrupted `code_plan` run restarts from the last completed phase; phases already recorded in the kernel JSONL are not re-run.
17. `⎿ ...and N more tool calls` appears flush against the last printed tool call (no blank line between them), updates live as calls arrive, and is printed permanently to the scroll buffer when the group closes.
18. If the execute phase ends without calling `mark_execute_complete()`, the phase continues in-loop with a continuation prompt rather than advancing to review. The agent resumes from full memory context.
19. If the review phase ends without a `<review>` tag, the review phase itself retries — the agent is not routed back through execute.
20. `Phase N/M` in the status bar always shows the workflow-definition position of the current phase — plan is always 1, execute always 2 — regardless of how many retry iterations have occurred.

---

## 20. Default Global Skills Bootstrap (PRD-104)

On first launch agenthicc automatically populates `~/.agenthicc/skills/` with a
curated set of starter skills.  These are loaded through the normal skill
discovery pipeline — no special runtime path exists.

| # | Requirement | Expected behaviour |
|---|---|---|
| 20.1 | Auto-install on first launch | `~/.agenthicc/skills/` is created if absent; missing default skills are written. |
| 20.2 | Six built-in skills | `review`, `refactor`, `architect`, `docs`, `debug`, `commit` are installed. |
| 20.3 | Normal skill format | Each skill is a directory with a `SKILL.md` using standard YAML frontmatter. |
| 20.4 | No overwrite | A skill directory that already exists is left untouched. |
| 20.5 | Deletion marker | Deleting a default skill records `"deleted"` in `~/.agenthicc/default_skills.json`; the skill is not recreated on subsequent launches. |
| 20.6 | Project skills still win | A project-local skill with the same slug overrides the default. |
| 20.7 | Startup message | `Installed N default skill(s).` printed at `[dim]` when skills are newly installed. |
| 20.8 | Opt-out via TOML | `[skills] install_default_skills = false` disables bootstrap entirely. |
| 20.9 | Custom directory | `[skills] default_skill_directory = "..."` overrides the default `~/.agenthicc/skills` root. |
| 20.10 | Skill metadata | Each default `SKILL.md` carries `source: default` and `version: 1` frontmatter fields. |

---

## 21. Cross-Platform Terminal Capability Detection (PRD-105)

Agenthicc never crashes on startup due to missing terminal APIs.  All
terminal-dependent features degrade gracefully when unavailable.

| # | Requirement | Expected behaviour |
|---|---|---|
| 21.1 | No `ModuleNotFoundError: termios` | `import termios`/`import tty` inside `raw_mode()` are wrapped in `try/except ImportError`; missing module yields a passthrough context instead of crashing. |
| 21.2 | No `termios.error: Inappropriate ioctl` | `termios.tcgetattr(fd)` is wrapped in `try/except`; a non-TTY fd (pipe, redirect) yields a passthrough context instead of crashing. |
| 21.3 | Non-TTY `run()` exits cleanly | `UnifiedInputSession.run()` checks `sys.stdin.isatty()` first; returns immediately on non-interactive stdin so `TUISession.run()` cancels background tasks normally. |
| 21.4 | No `fileno()` crash | `sys.stdin.fileno()` in `run()` is wrapped in `try/except`; an `io.StringIO`-backed stdin returns cleanly. |
| 21.5 | Centralized capability model | `tui/terminal_caps.py` exports `TerminalCapabilities` (frozen dataclass) and `TerminalCapabilityDetector.detect()`. |
| 21.6 | Capability fields | `is_tty`, `supports_raw_mode`, `supports_alt_screen`, `supports_colors`, `supports_mouse`, `supports_resize_events` all probed at runtime. |
| 21.7 | SIGWINCH already safe | `workspace.py` SIGWINCH handler is wrapped in `try/except (AttributeError, OSError)` — no change needed. |
| 21.8 | Shutdown always safe | `_reset_terminal_on_exit()` is fully wrapped in `try/except` — terminal restoration never re-raises. |
| 21.9 | Windows startup succeeds | No Unix-only import escapes a guard; Windows users launch without `ModuleNotFoundError`. |
| 21.10 | CI/pipe startup succeeds | Non-TTY launch (GitHub Actions, redirected stdin) exits the input loop cleanly without crashing. |

---

## 22. Windows Terminal Backend — msvcrt (PRD-106)

A platform-independent `TerminalBackend` abstraction replaces direct
`termios`/`tty` calls in application code.  Windows uses an `msvcrt` backend;
POSIX uses the existing `cbreak_reader` logic behind a `PosixBackend` wrapper.

### Package layout

```
tui/terminal/
├── __init__.py          re-exports TerminalBackend, get_backend
├── backend.py           TerminalBackend Protocol + get_backend() factory
├── posix_backend.py     PosixBackend — wraps cbreak_reader.raw_mode / read_key
└── windows_backend.py   WindowsBackend — exclusive owner of all msvcrt calls
```

### Factory rule

`get_backend()` is the **only** permitted platform-specific branch:

```python
if os.name == "nt":   → WindowsBackend
else:                 → PosixBackend
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 22.1 | Backend abstraction | `TerminalBackend` Protocol exposes `is_interactive()`, `read_key()`, `enter_raw_mode()`, `restore()`. |
| 22.2 | POSIX backend | `PosixBackend` delegates to `cbreak_reader.raw_mode` and `cbreak_reader.read_key`; no termios calls leak outside. |
| 22.3 | Windows backend | `WindowsBackend` uses `msvcrt.getwch()` for input; no termios/tty/fcntl imports. |
| 22.4 | Factory selection | `get_backend()` returns `WindowsBackend` when `os.name == "nt"`, `PosixBackend` otherwise. |
| 22.5 | `unified_session.run()` uses backend | `run()` calls `get_backend()`, checks `is_interactive()`, enters `backend.enter_raw_mode()`, and calls `backend.read_key()` via `run_in_executor`. |
| 22.6 | Backend isolation | No file outside `tui/terminal/windows_backend.py` imports `msvcrt`. |
| 22.7 | Key enum unchanged | `Key` stays in `cbreak_reader`; all 11 existing importers continue to work without change. |
| 22.8 | Printable Unicode input | `WindowsBackend.read_key()` returns `(Key.CHAR, ch)` for printable Unicode via `msvcrt.getwch()`. |
| 22.9 | Arrow keys on Windows | `\xe0H/P/K/M` → `UP/DOWN/LEFT/RIGHT`; `\xe0G/O` → `HOME/END`. |
| 22.10 | Shift+Tab on Windows | `\x00\x0f` → `Key.SHIFT_TAB`. |
| 22.11 | Ctrl+C on Windows | `\x03` → `Key.CTRL_C`; cancellation works correctly. |
| 22.12 | `enter_raw_mode` no-op on Windows | `WindowsBackend.enter_raw_mode()` is a no-op context manager; `msvcrt.getwch()` bypasses line buffering without setup. |
| 22.13 | Non-interactive exit | `backend.is_interactive()` returns False in CI/pipe/redirect; `run()` returns cleanly without crashing. |
| 22.14 | PosixBackend passthrough on non-TTY fd | `enter_raw_mode()` on a non-TTY fd yields without configuring the terminal (tcgetattr guard). |

---

## Known Lauren-AI gaps (future PRDs)

These are friction points in agenthicc that require reaching into private lauren-ai internals.
Each entry is a candidate for a lauren-ai PRD.

| Gap | Current workaround in agenthicc | Ideal API in lauren-ai |
|---|---|---|
| No public model accessor | `getattr(runner, "_transport", None)._config.model` in `agent_turn.py` and `workflow/runner.py` | `AgentRunnerBase.model_id: str` property |
| No public signal bus accessor | `getattr(runner, "_signals", None)` in `agent_turn.py` and `tui_session.py` | `AgentRunnerBase.signals: SignalBus` public property |
| `meta.tools` not populated after `@agent` + `@use_tools` | `populate_agent_tools()` in `runners/tool_populator.py` uses private `AGENT_META`, `TOOL_META`, `_add_to_tool_map` | Public `build_tool_map(tools) -> dict` or auto-populate in decorator |
| Global hooks only settable at construction | New `AgentRunnerBase` created per turn in `_build_agent()` to inject `global_hooks` | `run_stream(..., global_hooks=...)` per-call override |
| No `on_turn_start` hook | `approval_svc.reset_turn_memory()` called manually in `run_turn()` | `ToolHook.on_turn_start(ctx)` lifecycle method |
| `_add_to_tool_map` is private but production-required | `from lauren_ai._tools import _add_to_tool_map` in `tool_populator.py` | Make public; rename to `add_to_tool_map` |
| No `AgentConfig.memory_window_tokens` | `ShortTermMemory(max_tokens=32_000)` constructed outside runner | `AgentConfig.memory_window_tokens: int` field |
