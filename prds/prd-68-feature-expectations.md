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
| 2.16 | Blank line after user message | The `user_message` renderer calls `console.print()` (blank line) immediately after printing `❯ text`. One blank line always separates the user message from the first line of the agent turn that follows. |
| 2.17 | Blank line after LLM turn | `ConversationStore.end_turn()` emits a `turn_complete` event when a turn finishes. The `turn_complete` renderer calls `console.print()` (blank line). One blank line always follows the last line of each complete LLM turn. No blank line is inserted after individual LLM sub-turn text chunks — only after the whole turn. |

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
| 11.6 | Welcome screen — single print | `print_welcome()` calls `console.print(render_welcome(...))` exactly once; no surrounding blank lines are added. Dynamic column widths come from `shutil.get_terminal_size()`. The welcome box uses a yellow-dim border. |

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
| 17.4 | Built-in renderers | `turn_start`, `user_message`, `tool_complete`, `text`, `thinking_step`, `file_modified`, `error`, `mention_chips`, `turn_complete` — all registered at module load time in the same file. `turn_complete` emits a blank line after the full LLM turn (see §2.17). |
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

## 22. Windows Terminal Backend — msvcrt + ConPTY (PRD-106)

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
| 22.10 | Shift+Tab — legacy console | `\x00\x0f` (BIOS scan code 15) → `Key.SHIFT_TAB` in CMD and legacy PowerShell. |
| 22.11 | Shift+Tab — ConPTY | `\x1b[Z` (VT sequence from Windows Terminal, VS Code, new PowerShell) → `Key.SHIFT_TAB` via CSI parser. |
| 22.12 | Ctrl+C on Windows | `\x03` → `Key.CTRL_C`; cancellation works correctly. |
| 22.13 | `enter_raw_mode` no-op on Windows | `WindowsBackend.enter_raw_mode()` is a no-op context manager; `msvcrt.getwch()` bypasses line buffering without setup. |
| 22.14 | Non-interactive exit | `backend.is_interactive()` returns False in CI/pipe/redirect; `run()` returns cleanly without crashing. |
| 22.15 | PosixBackend passthrough on non-TTY fd | `enter_raw_mode()` on a non-TTY fd yields without configuring the terminal (tcgetattr guard). |
| 22.16 | Lone ESC preserved | `\x1b` with no following characters (`msvcrt.kbhit()` = False) returns `Key.ESC`, not `Key.SHIFT_TAB`. |
| 22.17 | VT arrow keys on ConPTY | `\x1b[A/B/C/D` → `UP/DOWN/LEFT/RIGHT`; `\x1b[H/F` → `HOME/END` via `_CSI_KEYS` table. |
| 22.18 | ConPTY detection mechanism | `msvcrt.kbhit()` is used (not `select.select`, which is unavailable for Windows console handles) to detect whether more characters follow `\x1b`. |
| 22.19 | Input bar visible on startup | `WindowsBackend.enter_raw_mode()` writes `\x1b[?25l\x1b[?2004h` and calls `sys.stdout.flush()` before yielding. This drains Rich's buffered Live block (including the `❯ ▌` input bar) from the OS stdout buffer to the terminal immediately — matching the side-effect of `cbreak_reader.raw_mode()`'s `sys.stdout.flush()` on POSIX. Without this, the input bar is rendered but invisible until the first keypress. |
| 22.20 | Terminal restored on Windows exit | `enter_raw_mode()`'s `finally` block and `restore()` write `\x1b[m\x1b[?2004l\x1b[?25h` (reset SGR, disable bracketed paste, show cursor). Harmlessly ignored by legacy CMD/PowerShell; honoured by ConPTY terminals. |

---

## 23. File-Creation Diff Preview (10-line cap)

When a tool writes a file that did not previously exist (`old_lines == []`), the
scroll buffer renders a compact creation preview instead of a full diff.

Detection: `file_modified` event where `old_lines` is an empty list.  No new
event type is required.

Visual output:

```
● Create(src/path/to/file.py)
└─ Created 42 lines

  1 + def hello():
  2 +     return "world"
  3 + ...
  ⋯ +39 more lines
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 23.1 | Create detection | `old_lines == []` in a `file_modified` event is the trigger; tool name is not used. |
| 23.2 | Compact preview | First 10 lines rendered with green `+` background; syntax-highlighted. |
| 23.3 | Truncation indicator | When the file has more than 10 lines: `⋯ +N more lines` (dim green) is shown below the table. |
| 23.4 | No truncation for short files | Files with ≤ 10 lines show all lines; no truncation indicator. |
| 23.5 | Header | `● Create(path)` header in green + bold. |
| 23.6 | Summary | `└─ Created N line(s)` (green); always shows total line count, not preview count. |
| 23.7 | Line limit constant | `diff_renderer.CREATE_PREVIEW_LINES = 10` is the single source of truth. |
| 23.8 | Update path unchanged | Non-empty `old_lines` continues to use `render_file_diff` with full context diff. |
| 23.9 | Public API | `diff_renderer.render_file_create(path, new_lines, *, max_lines, language)` is exported. |
| 23.10 | Empty file | A zero-line file shows the header and `Created 0 lines` without crashing. |

---

## 24. TUI Turn Recovery — Always-Recoverable Agent Turns (PRD-107)

After any exception raised during an agent turn (network timeout, tool crash,
LLM error, cancellation), the TUI returns to a clean, interactive `IDLE` state.

### Root causes fixed

| Bug | Old behaviour | Fixed behaviour |
|---|---|---|
| Double `fail_turn()` at two layers | `agent_state = ERROR` after exception | `agent_state = IDLE` always |
| `fail_turn()` set `ERROR` not `IDLE` | Status bar showed ERROR until restart | `close_turn()` always ends at IDLE |
| No turn timeout | Hung `ReadTimeout` locked TUI forever | `asyncio.wait_for` watchdog per turn |
| `_emit_intent_complete()` not in `finally` | Kernel intent stayed `"pending"` forever | Intent always marked `complete` or `failed` |
| No SIGTERM/SIGHUP handler | Terminal left in raw mode after hard kill | `atexit` + signal handlers restore terminal |

### Key API: `ConversationStore.close_turn()`

```python
store.close_turn()                          # success path
store.close_turn(error="ReadTimeout: ...")  # error path — still ends IDLE
```

Idempotent — safe to call multiple times. `end_turn()` and `fail_turn()` are
now thin wrappers that call `close_turn()`.

| # | Requirement | Expected behaviour |
|---|---|---|
| 24.1 | `agent_state` always IDLE after exception | After any tool or LLM exception, `agent_state` returns to `IDLE`. Never stuck at `ERROR`. |
| 24.2 | `InputMode` always IDLE after exception | `InputMode.STREAMING` → `InputMode.IDLE` on every exit path. |
| 24.3 | Exactly one error event per exception | The scroll buffer shows one error block per failure. No duplicates. |
| 24.4 | Error class name always shown | Error display format is `ExceptionType: message` (e.g. `ReadTimeout: ...`). `_fmt_exc()` is the single formatter. |
| 24.5 | `close_turn()` is idempotent | Calling `close_turn()` multiple times is safe and leaves no bad state. |
| 24.6 | `close_turn(error=None)` emits `turn_complete` | Successful turn emits a blank-line `turn_complete` event. |
| 24.7 | `close_turn(error=...)` emits `error` event | Error turn emits the error message to the scroll buffer (exactly once). |
| 24.8 | `is_turn_active` property | `ConversationStore.is_turn_active` is `True` between `begin_turn` and `close_turn`. Used by callers to avoid duplicate error events. |
| 24.9 | Turn watchdog | `execution.turn_timeout_s` (default `0` = no limit) cancels hung turns after N seconds. `TimeoutError: Turn timed out after Ns` appears in the scroll buffer. |
| 24.10 | Kernel intent always completed | `IntentStatusChanged(status="complete"/"failed")` is emitted in `AgentTurnRunner.run()`'s `finally`. |
| 24.11 | Crash-safe terminal restore | `atexit.register(_reset_terminal_on_exit)` + `SIGTERM`/`SIGHUP` handlers installed in `_run_tui()`. |
| 24.12 | `--resume` terminal is clean | ECHO/ICANON/cursor restored before process exits, so the next `--resume` finds the terminal in a usable state. |

---

## 25. HTTP Timeout Safety — No ReadTimeout Kills a Turn (PRD-108)

Network errors from tool HTTP calls return a clean error dict to the agent
instead of propagating to the agent turn layer and ending the turn.

### Tools affected

| Tool | Endpoint | Error boundary |
|---|---|---|
| `SearchWebTool` (`search_web`) | `api.search.brave.com` | `try/except` in `_brave_search()` → `{"ok": False, "recoverable": True}` |
| All Outlook tools | `graph.microsoft.com` | `_get()`/`_post()` raise `_OutlookNetworkError`; `_safe_call()` in `agent_tools.py` converts to dict |
| `FetchPageTool` (`fetch_page`) | arbitrary URLs | existing `try/except` updated to include exception class name |
| Auth OAuth calls | `agenthicc.ai/oauth/token` | `AuthNetworkError` with human-readable message; never a raw traceback |

### Shared HTTP client

All tool HTTP calls go through `agenthicc.tools.http.agenthicc_http_client()`.
Timeout is configured once at session startup from `[tools] http_timeout_s`.

```toml
[tools]
http_timeout_s = 30.0   # 0.0 = no read timeout
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 25.1 | Shared client | All tool HTTP calls use `agenthicc_http_client()` — no bare `httpx.AsyncClient()` in tools. |
| 25.2 | Configurable timeout | `ToolSettings.http_timeout_s = 30.0` (default); overridable via `[tools]` TOML. |
| 25.3 | Connect timeout always bounded | Connect timeout is always 10 s regardless of read timeout. |
| 25.4 | Zero timeout = unbounded | `http_timeout_s = 0.0` → `httpx.Timeout(None, connect=10)` (no read limit). |
| 25.5 | `is_network_error()` classifier | Returns True for `httpx.TimeoutException`, `httpx.HTTPError`, `TimeoutError`, `ConnectionError`, botocore errors. |
| 25.6 | `SearchWebTool` never propagates | `ReadTimeout` / `ConnectTimeout` from Brave API returns `{"ok": False, "recoverable": True}`. |
| 25.7 | Outlook tools never propagate | `_get()`/`_post()` raise `_OutlookNetworkError`; `_safe_call()` converts to error dict. |
| 25.8 | `FetchPageTool` error includes class name | `{"ok": False, "error": "ReadTimeout: ...", "recoverable": True}` — class name always present. |
| 25.9 | Auth `AuthNetworkError` | `_exchange_code()` and `_refresh()` raise `AuthNetworkError` on timeout; never a bare `ReadTimeout`. |
| 25.10 | Auth error is human-readable | `AuthNetworkError` message includes the exception class name AND advice to check the connection. |
| 25.11 | Non-network errors still propagate | `ValueError`, `KeyError`, etc. from tools are not caught by network boundaries — they propagate normally. |
| 25.12 | `configure()` called at startup | `_build_session_context()` calls `tools.http.configure(cfg.tools.http_timeout_s)` after `load_config()`. |

---

## 26. Case-Insensitive @Mention Matching (PRD-109)

All `@mention` candidates are matched case-insensitively using `str.casefold()`.
Original filesystem casing is always preserved in display and insertion.

### Matching engine — `mentions/matcher.py`

`filter_and_rank(query, items)` is the single implementation used by all
mention providers.  Ranking tiers (lower = better):

| Rank | Tier | Example: `@read` → `docs/README.md` |
|---|---|---|
| 0 | Exact match | query == filename or full path |
| 1 | Filename prefix | `README.md` starts with `read` |
| 2 | Path-segment prefix | any segment (`README.md`) starts with query |
| 3 | Filename substring | query appears inside the filename |
| 4 | Path substring | query appears anywhere in the full path |
| 5 | Fuzzy | `rdm` → `README.md` (chars in order) |

Within each tier results are sorted alphabetically by casefolded display string
(deterministic ordering).

| # | Requirement | Expected behaviour |
|---|---|---|
| 26.1 | Case-insensitive prefix | `@re`, `@RE`, `@Read` all match `README.md`. |
| 26.2 | Case preserved in display | Display and insertion always use actual filesystem casing. |
| 26.3 | Path-segment matching | `@read` matches `docs/README.md` because `README.md` is a path segment. |
| 26.4 | Substring matching | `@note` matches `release_notes.md`. |
| 26.5 | Fuzzy matching | `@rdm` matches `README.md` (sequential character containment in filename). |
| 26.6 | Ranking order | Exact → filename prefix → segment prefix → filename substr → path substr → fuzzy. |
| 26.7 | Deterministic | Identical query produces identical ordering. |
| 26.8 | `str.casefold()` not `str.lower()` | Unicode-safe normalisation throughout. |
| 26.9 | Cross-platform | Identical results on Linux, macOS, Windows regardless of filesystem semantics. |
| 26.10 | Directory matching | `@doc` matches `docs/`, `Documentation.md`, `docstrings.py`. |
| 26.11 | Centralised engine | `mentions/matcher.py` is the single matching implementation; `at_mention.py` and all future providers call `filter_and_rank()`. |
| 26.12 | Candidate pool | Top-level entries **and** their immediate children are all added to the pool before filtering, enabling path-segment matching without recursive crawl. |
| 26.13 | Performance — ≤ 10 ms | Matching completes within 10 ms for repositories containing ≤ 10,000 candidates. No visible UI lag while typing. |
| 26.14 | Normalisation is O(1) per call | `casefold()` is applied at comparison time, not pre-indexed. No per-keystroke re-normalisation overhead beyond the string comparison itself. |
| 26.15 | No slash-command regression | Skills served via `SlashCommandTrigger` use a separate registry path and are unaffected by the `@mention` matcher change. |
| 26.16 | UX — prefix before substring | `@read` shows `README.md` above `release_notes.md`; filename-prefix results always appear before substring results. |
| 26.17 | UX — `@rdm` fuzzy matches `README.md` | Sequential character containment (`r` … `d` … `m` all present in order inside `readme.md`) returns a result. Characters not in order do not match. |

---

## 27. Workflow Runner Dispatch via Factory Method (PRD-110)

Runner selection for each workflow is owned by the workflow plugin itself,
not by the caller.  `tui_session.py` calls `defn.build_runner()` and receives
the correct runner — no branching on workflow name.

### Mechanism

`WorkflowPlugin.runner_factory(cls, defn, config, mode_manager)` is a
classmethod that returns the appropriate `BaseWorkflowRunner`.  The default
implementation returns `WorkflowRunner`; `CodePlan` overrides it to return
`CodePlanRunner`.  `WorkflowPlugin.to_definition()` stores the bound
classmethod on `WorkflowDefinition.runner_factory` so the factory travels with
the definition through the registry.

```python
# Before — hardcoded name check duplicated in two places:
if wf_defn.name == "code_plan":
    runner = CodePlanRunner(config, mode_manager)
else:
    runner = WorkflowRunner(wf_defn, config, mode_manager)

# After — single call, no name check:
runner = wf_defn.build_runner(config, mode_manager)
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 27.1 | `runner_factory` field | `WorkflowDefinition` has a `runner_factory: Callable | None` field (compare=False, hash=False). |
| 27.2 | `build_runner()` method | `WorkflowDefinition.build_runner(config, mode_manager)` delegates to `runner_factory` or falls back to `WorkflowRunner`. |
| 27.3 | Default factory | `WorkflowPlugin.runner_factory()` returns `WorkflowRunner(defn, config, mode_manager)`. |
| 27.4 | `CodePlan` override | `CodePlan.runner_factory()` returns `CodePlanRunner(config, mode_manager)` — ignores `defn` by design. |
| 27.5 | `to_definition()` carries factory | `WorkflowPlugin.to_definition()` stores `type(self).runner_factory` on the `WorkflowDefinition`. |
| 27.6 | No name-based dispatch | `tui_session.py` contains no `if … name == "code_plan":` runner-selection branch. |
| 27.7 | No `CodePlanRunner` import in `tui_session` | Dispatch imports are gone; the factory is resolved inside `build_runner()`. |
| 27.8 | Third-party extensibility | A `WorkflowPlugin` subclass with a custom `runner_factory()` override automatically uses its own runner without any changes to `tui_session.py`. |
| 27.9 | All builtins carry factories | Every `WorkflowDefinition` returned by `load_builtin_workflows()` has a non-None `runner_factory`. |

---

## 28. Per-Workflow Tunable Parameters — `WorkflowParams` (PRD-111)

Each workflow plugin may declare a typed `WorkflowParams` subclass holding its
tunable parameters.  Parameters are loaded from TOML, CLI `--set`, and the
existing config layering model.  The primary use case is per-phase model
selection — the execute phase of `code_plan` can use a cheaper model than the
plan or review phases.

### Mechanism

```
[workflows.code_plan]              ← agenthicc.toml
execute_model = "claude-haiku-4-5"
plan_model    = ""                 # empty → use execution.model
```

`WorkflowParams` (base) → `CodePlanParams` (specialised) → stored in
`WorkflowConfig.params` → `WorkflowRunner` applies `params.model_for_phase()`
per phase via `dataclasses.replace(exec_cfg, model=phase_model)`.

```
# Priority order (highest → lowest)
CLI --set workflows.code_plan.execute_model=...
.agenthicc/agenthicc.toml [workflows.code_plan]
~/.agenthicc/agenthicc.toml [workflows.code_plan]
CodePlanParams field defaults ("")
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 28.1 | `WorkflowParams` base | Dataclass with `get_phase_models() → dict[str, str]` and `model_for_phase(phase, fallback) → str`. |
| 28.2 | Empty model = global | `model_for_phase()` returns *fallback* when the map value is `""`. |
| 28.3 | `CodePlanParams` | Typed subclass with `plan_model`, `execute_model`, `review_model`, `summary_model` string fields. |
| 28.4 | `WorkflowPlugin.params_factory(source)` | Default returns `WorkflowParams()` ignoring source. Override in subclasses. |
| 28.5 | `CodePlan.params_factory(source)` | Constructs `CodePlanParams` from source dict; unknown keys are silently filtered. |
| 28.6 | `WorkflowDefinition.params_factory` field | Carries the bound classmethod (same pattern as `runner_factory`). |
| 28.7 | `WorkflowDefinition.build_params(source)` | Delegates to `params_factory` or returns `WorkflowParams()`. |
| 28.8 | `to_definition()` stores factory | `type(self).params_factory` bound to definition at plugin discovery time. |
| 28.9 | `WorkflowConfig.params` field | `WorkflowParams \| None`; populated in `run_turn()` and `_resume_workflow_task()` before calling `build_runner()`. |
| 28.10 | `AgenthiccConfig.workflows` | `dict[str, dict[str, Any]]` populated from `[workflows]` TOML section. |
| 28.11 | Per-phase model override | `WorkflowRunner._run_phase()` calls `params.model_for_phase()` and applies the result via `dataclasses.replace(exec_cfg, model=…)`. |
| 28.12 | No-params fallback | When `params is None` or phase has no override, the global `execution.model` is used unchanged. |
| 28.13 | Third-party extensibility | A custom `WorkflowPlugin` overriding `params_factory()` automatically gets per-workflow TOML config with no changes to the runner. |
| 28.14 | All builtins carry factory | Every `WorkflowDefinition` from `load_builtin_workflows()` has a non-None `params_factory`. |

---

## 29. Workflow Package Structure Reorganisation (PRD-112)

Code-plan-specific classes live inside `workflows/code_plan/`; generic
workflow infrastructure lives inside the new `workflows/default/` subpackage.
Old import paths are retained as backward-compat shims.

### New layout

```
workflows/
  code_plan/
    __init__.py      exports CodePlan, CodePlanParams, CodePlanRunner, …
    definition.py    CodePlan (WorkflowPlugin), CodePlanParams  ← was in builtins.py
    runner.py        CodePlanRunner (unchanged)
    state.py         CodePlanState, CodePlanContext (unchanged)
  default/           ← NEW subpackage
    __init__.py
    definition.py    PlanOnly, ReviewOnly, Supervised, Architect  ← was in builtins.py
    runner.py        WorkflowRunner, build_workflow_runner  ← was top-level runner.py
  builtins.py        re-export shim (backward compat)
  runner.py          re-export shim (backward compat)
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 29.1 | `CodePlan` canonical location | Defined in `workflows/code_plan/definition.py`; importable from `agenthicc.workflows.code_plan`. |
| 29.2 | `CodePlanParams` canonical location | Same file and package as `CodePlan`. |
| 29.3 | `WorkflowRunner` canonical location | Defined in `workflows/default/runner.py`; importable from `agenthicc.workflows.default`. |
| 29.4 | Generic workflows canonical location | `PlanOnly`, `ReviewOnly`, `Supervised`, `Architect` defined in `workflows/default/definition.py`. |
| 29.5 | Backward-compat shims | `from agenthicc.workflows.runner import WorkflowRunner` and `from agenthicc.workflows.builtins import CodePlan` both continue to work. |
| 29.6 | `workflows/__init__.py` exports all | All public symbols (both subpackages) importable from `agenthicc.workflows`. |
| 29.7 | `loader.py` uses canonical paths | `load_builtin_workflows()` imports from `code_plan.definition` and `default.definition`. |
| 29.8 | No regressions | All existing tests pass after restructuring. |

---

## 30. Configuration Inheritance via `extends` (PRD-113)

Any agenthicc TOML config file may declare parent files using an `extends`
key.  Parents are loaded and merged first; the declaring file is applied on
top.  Users maintain one base config and layer environment-specific overrides
without duplicating shared settings.

### Syntax

```toml
# agenthicc-dev.toml
extends = "agenthicc.toml"          # single parent (relative to this file)

[execution]
model = "claude-haiku-4-5"          # only the override; rest inherited
```

```toml
extends = ["../../shared/team.toml", "secrets.toml"]   # list — merged left-to-right
```

### Config file selection

```bash
agenthicc --config agenthicc-dev.toml        # --config flag (already in CLI parser, now wired)
AGENTHICC_CONFIG=agenthicc-dev.toml agenthicc  # env var alternative
```

### Merge order (unchanged semantics, extends applied at each layer)

```
hardcoded defaults  →  user-global + extends chain  →  project file + extends chain  →  env vars  →  --set
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 30.1 | Single parent | `extends = "base.toml"` loads the parent first, merges child on top. |
| 30.2 | List of parents | `extends = ["a.toml", "b.toml"]` merges left-to-right; child wins. |
| 30.3 | Relative paths | Parent paths resolve relative to the file containing `extends`, not CWD. |
| 30.4 | `~` expansion | `extends = "~/.agenthicc/team-base.toml"` expands the home directory. |
| 30.5 | Chained extends | A parent's own `extends` is resolved recursively (grandparent → parent → child). |
| 30.6 | Deep merge | Only differing keys need to appear in child; parent keys are fully inherited. |
| 30.7 | `extends` stripped | The `extends` key never appears in `AgenthiccConfig` or `_dict_to_config` input. |
| 30.8 | Cycle detection | Any cycle raises `ConfigExtendsCycleError` immediately. |
| 30.9 | Missing parent | A non-existent file named in `extends` raises `FileNotFoundError`. |
| 30.10 | `--config <file>` wired | `CLIContext.config_path` is now threaded through to `load_config(config_path=…)`. |
| 30.11 | `AGENTHICC_CONFIG` env var | Sets the project-level config file path when `--config` is not given. |
| 30.12 | `--config` beats env var | An explicit `--config` flag takes priority over `AGENTHICC_CONFIG`. |
| 30.13 | User-global also resolves extends | `~/.agenthicc/agenthicc.toml` may itself use `extends`. |
| 30.14 | No regression | All existing config tests pass; missing files still silently skipped at auto-discovery. |

---

## 31. Composite Workflows via Runner Inheritance (PRD-114)

End users extend existing workflows by subclassing the runner and plugin.
`CodePlanRunner.run()` returns a typed context so subclasses can call
`ctx = await super().run(intent)` and continue with additional phases.
`/workflow <name>` switches the active workflow within the current mode.

### Pattern (end user writes one file)

```python
class CodePlanDocsRunner(CodePlanRunner):
    async def run(self, intent: str) -> None:
        ctx = await super().run(intent)          # full code_plan unchanged
        await self.run_phase(                    # public stable API
            intent=intent,
            text=f"[PLAN]\n{ctx.plan}\n\nUpdate docs.",
            system_prompt="You are a documentation writer.",
            mode="Auto", max_turns=10,
            shared_memory=ctx.shared_memory,
        )

class CodePlanDocs(CodePlan):
    name = "code_plan_docs"
    mode_bindings = ["Plan"]

    @classmethod
    def runner_factory(cls, defn, config, mode_manager):
        return CodePlanDocsRunner(config, mode_manager)
```

Activate: `/workflow code_plan_docs` · Deactivate: `/workflow reset`

| # | Requirement | Expected behaviour |
|---|---|---|
| 31.1 | `BaseWorkflowRunner.run()` returns `Any` | ABC contract allows subclasses to declare tighter return types. |
| 31.2 | `CodePlanRunner.run()` returns `CodePlanContext` | Subclasses receive fully populated context: `ctx.plan`, `ctx.execute_summary`, `ctx.review_summary`, `ctx.shared_memory`. |
| 31.3 | `WorkflowRunner.run()` returns `WorkflowContext` | Symmetric change for the generic runner. |
| 31.4 | `CodePlanRunner.run_phase()` public API | Stable extension point: `run_phase(intent, text, system_prompt, mode, max_turns, shared_memory)`. Delegates to `_run_turn()` + `_base_tools()` internally. |
| 31.5 | `super().run()` pattern works | `ctx = await super().run(intent)` in a subclass runner returns typed context. |
| 31.6 | `code_plan` is unchanged in logic | Only the `return ctx` statement is added; all internal phases, retry caps, and state machine are identical. |
| 31.7 | `/workflow <name>` command | Sets `TUISession._workflow_override` and `ConversationStore.workflow_override` signal. Shows notification. |
| 31.8 | `/workflow reset` | Clears the override; reverts to mode's `default_workflow`. |
| 31.9 | Unknown workflow name | Shows available workflows in the notification; does not crash. |
| 31.10 | Status bar `⬡ name` indicator | `ComposerComponent` shows `⬡ workflow-name` (cyan dim) when an override is active. |
| 31.11 | Plugin discovered automatically | Composite plugin in `.agenthicc/workflows/` requires no extra registration. |
| 31.12 | `/create-workflow` default skill | Bootstrap installs a skill that guides the LLM on writing workflows, including the composite `run_phase()` pattern. |

---

## 32. Per-Phase Model Override and Phase-Aware TUI Updates (PRD-115)

### Bug fixed: per-phase model was silently ignored

`AgentTurnRunner._resolve_model()` previously read only the transport's baked-in model, ignoring `exec_cfg.model` entirely. PRD-111's `WorkflowParams` per-phase override constructed the right `exec_cfg` but the modified model never reached `@agent_decorator`. This is now fixed.

### `AppState.update_workflow_phase()`

Single atomic method replacing scattered `dataclasses.replace(wf_run, ...) + workflow_run.set()` boilerplate in each phase method.

### Per-phase model class attributes + TOML config

```toml
[workflows.code_plan]
plan_model    = "deepseek-v4-pro"    # flagship for planning
execute_model = "deepseek-v4-flash"  # cheap for execution
```

Or as a subclass static override:
```python
class CodePlanDocsRunner(CodePlanRunner):
    plan_model = "deepseek-v4-pro"
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 32.1 | `_resolve_model()` priority | `exec_cfg.model` (non-empty) > transport config > `"unknown"`. |
| 32.2 | PRD-111 WorkflowRunner fix | `WorkflowRunner`'s `WorkflowParams` per-phase model now reaches `@agent_decorator`. |
| 32.3 | `AppState.update_workflow_phase()` | Sets `workflow_run` signal atomically from named args; creates fresh `WorkflowRun` if none exists. |
| 32.4 | Per-phase class attrs | `CodePlanRunner.plan_model`, `execute_model`, `review_model`, `summary_model` default to `""`. |
| 32.5 | TOML config path | `[workflows.code_plan] plan_model = "..."` overrides only the plan phase model. |
| 32.6 | Priority: TOML > class attr | `_phase_model(name)` reads `WorkflowParams.model_for_phase()` first, class attr second. |
| 32.7 | `_run_turn(model_override=...)` | Non-empty override replaces `exec_cfg.model` before calling `_run_agent_turn`. |
| 32.8 | `_set_phase(name, index, ctx)` | Calls `update_workflow_phase` with all runner invariants filled; each phase uses it. |
| 32.9 | Subclass override | `class MyRunner(CodePlanRunner): plan_model = "model"` applies for plan phase only. |
| 32.10 | No override → global model | Empty string falls back to `execution.model` from global config. |

---

## 33. Remove `WorkflowDefinition` — `WorkflowPlugin` as Registry Artifact (PRD-116)

`WorkflowDefinition` is deleted.  The registry stores plugin *classes* directly
(wrapped in `WorkflowEntry` for provenance).  All workflow metadata lives on
`WorkflowPlugin` class attributes and classmethods.

### Before → After

```python
# Before
defn = WorkflowDefinition(name="x", phases=(...))
registry.register(defn)
runner = defn.build_runner(config, mode_manager)

# After
class MyWorkflow(WorkflowPlugin):
    name = "x"
    phases = [...]
registry.register(MyWorkflow, source="user")
runner = MyWorkflow.build_runner(config, mode_manager)
```

### New on `WorkflowPlugin`

| Addition | Purpose |
|---|---|
| `max_total_phase_runs: int = 0` | Class attr moved from `WorkflowDefinition` |
| `first_phase() -> PhaseSpec \| None` | Classmethod query helper |
| `get_phase(name) -> PhaseSpec \| None` | Classmethod query helper |
| `phase_names() -> list[str]` | Classmethod query helper |
| `build_runner(config, mode_manager) -> BaseWorkflowRunner` | Factory classmethod (replaces `runner_factory`) |
| `build_params(source) -> WorkflowParams` | Factory classmethod (replaces `params_factory`) |

### `WorkflowEntry` — provenance record

```python
@dataclass(frozen=True)
class WorkflowEntry:
    plugin_cls: type[WorkflowPlugin]
    source: str        # "builtin" | "user" | "project"
    path: str | None   # filesystem path for user plugins
```

### Deleted

`WorkflowDefinition`, `WorkflowPlugin.to_definition()`, `WorkflowPlugin.runner_factory`,
`WorkflowPlugin.params_factory`, `WorkflowPlugin.determine_transition()` (dead code),
`build_workflow_runner()` (orphan factory).

| # | Requirement | Expected behaviour |
|---|---|---|
| 33.1 | `WorkflowDefinition` gone | `from agenthicc.workflows.plugin import WorkflowDefinition` raises `ImportError`. |
| 33.2 | Registry stores classes | `registry.get(name)` returns `type[WorkflowPlugin] \| None`. |
| 33.3 | `WorkflowEntry` provenance | `registry.get_entry(name)` returns `WorkflowEntry(plugin_cls, source, path)`. |
| 33.4 | `build_runner()` factory | `PluginCls.build_runner(config, mode_manager)` — default returns `WorkflowRunner(PluginCls, ...)`. |
| 33.5 | `build_params()` factory | `PluginCls.build_params(source_dict)` — default returns `WorkflowParams()`. |
| 33.6 | `WorkflowRunner` takes plugin class | `WorkflowRunner(PluginCls, config, mode_manager)` — reads `PluginCls.phases` etc. |
| 33.7 | `max_total_phase_runs` on plugin | `WorkflowPlugin.max_total_phase_runs = 0` class attr; subclasses override. |
| 33.8 | Loader returns classes | `load_builtin_workflows()` returns `list[type[WorkflowPlugin]]`. |
| 33.9 | Backward-compat shims | `from agenthicc.workflows.builtins import CodePlan` still works. |
| 33.10 | No `Any` | No `typing.Any` in any changed file. |

---

## 34. Permanent Error Early Exit for Workflow Phase Loops (PRD-117)

HTTP 4xx errors (except 429) during a workflow phase exit immediately with a
single clear diagnostic instead of retrying up to the maximum attempt cap.

**Before:** `TransportError: 400 — model 'gpt-4o' not supported` appeared
10 times followed by `code_plan failed: Plan phase exhausted 10 attempts`.

**After:** One error event, immediate exit, TUI returns to idle.

### How it works

`_is_permanent_error(exc)` checks the HTTP status code on the exception and
its chained causes.  `_stream()` re-raises permanent errors after emitting
the TUI error event (the `finally` block still runs → `close_turn()` called).
The phase loop's `except Exception` catches the re-raised error, sets
`ctx.fail_reason` to the actual message, and returns `FAILED` immediately.

Transient errors (5xx, network, timeouts, 429 rate-limit) continue to be
swallowed by `_stream()` — phase loops still retry those normally.

| # | Requirement | Expected behaviour |
|---|---|---|
| 34.1 | `_http_status_code(exc)` | Returns the HTTP status integer from `exc`, its `__cause__`, or `__context__`; `None` when absent. |
| 34.2 | `_is_permanent_error(exc)` | `True` for 4xx (except 429); `False` for 5xx, no-status, 429. |
| 34.3 | `_stream()` re-raises 4xx | Emits TUI error event then re-raises; `finally` still runs (`close_turn()` called). |
| 34.4 | `_stream()` swallows 5xx | Transient errors are still swallowed; phase loops retry as before. |
| 34.5 | `_plan()` exits on first 4xx | Returns `CodePlanState.FAILED` on attempt 1; `ctx.fail_reason` = exception message. |
| 34.6 | `_execute()` exits on first 4xx | Same — returns `FAILED` immediately, not after exhausting 10 attempts. |
| 34.7 | `_review()` exits on first 4xx | Same. |
| 34.8 | 429 retried, not permanent | Rate-limit is treated as transient — phase loop continues. |
| 34.9 | Single TUI error message | User sees one error event, not 10 identical ones. |
| 34.10 | Clear `fail_reason` | `ctx.fail_reason` contains the exception type + message, not "exhausted N attempts". |

---

## 35. Per-Phase Model Display in Status Bar (PRD-118)

When a workflow phase runs with a per-phase model override, the status bar
line 2 shows the **actual model in use** instead of the global session model.
The display reverts automatically when the workflow ends — no cleanup needed.

```
# During execute phase with execute_model = "deepseek-v4-flash":
deepseek-v4-flash           ← actual phase model

# After workflow completes:
openai/deepseek-v4-flash    ← session model restored
```

| # | Requirement | Expected behaviour |
|---|---|---|
| 35.1 | `WorkflowRun.current_phase_model: str` | New field, defaults to `""`. Non-empty when phase uses a model override. |
| 35.2 | `update_workflow_phase(model_id=...)` stored | Both the `dataclasses.replace` and fresh-`WorkflowRun` branches write `model_id` to `current_phase_model`. |
| 35.3 | Empty `model_id` stored as `""` | No override; status bar shows session model. |
| 35.4 | Status bar line 2 — phase model wins | When `wf_run.status == "running"` and `current_phase_model != ""`, line 2 shows `current_phase_model`. |
| 35.5 | Status bar line 2 — session model reverts | When run ends (`status != "running"`) or `current_phase_model == ""`, line 2 shows `conv.model_name()`. |
| 35.6 | No `Any` introduced | `current_phase_model` is typed `str`; no type regressions. |

---

## 36. Conversation Compaction (PRD-119)

Automatically summarise an oversized conversation history into a compact two-message form before each LLM turn, preventing "context window exceeded" 400 errors when large tool results accumulate.

### Architecture

| Component | Role |
|---|---|
| `src/agenthicc/memory/compactor.py` | `should_compact()` / `compact_memory()` — pure async functions |
| `ExecutionSettings.auto_compact` | Toggle (default `True`); TOML key `[execution] auto_compact` |
| `ExecutionSettings.compact_threshold_tokens` | Fire threshold (default `1_000_000`); TOML key `[execution] compact_threshold_tokens` |
| `ConversationStore.compaction_active` | `Signal[bool]` — `True` while the summarisation LLM call is in flight |
| `StatusComponent.render()` | Shows `⠋ Compacting…` spinner on line 2 when `compaction_active` is `True` |
| `AgentTurnRunner._stream()` | Calls `compact_memory()` after `ensure_valid()`, before `run_stream()` |
| `/compact` command | Triggers compaction on the session memory; intercepted in `TUISession.route()` |

### Acceptance criteria

| # | Criterion |
|---|---|
| 36.1 | `should_compact` returns `False` when `auto_compact=False` or `token_estimate < compact_threshold_tokens` |
| 36.2 | `compact_memory` replaces `memory._messages` with exactly two messages: `role:"user"` summary and `role:"assistant"` acknowledgement |
| 36.3 | `compaction_active` is `False` after both successful and failed compaction (enforced by `finally`) |
| 36.4 | Status bar shows a cycling `⠋`-style spinner with `" Compacting…"` label while the LLM call is in flight |
| 36.5 | `/compact` triggers `compact_memory` on the current session memory and shows a `notify_transient` confirmation |
| 36.6 | `auto_compact` and `compact_threshold_tokens` are readable from `[execution]` TOML section |
| 36.7 | Default `compact_threshold_tokens` is `1_000_000` (matches DeepSeek model's context window) |

---

## 37. Unified Tick Frame Counter (PRD-120)

Consolidates five separate animation-related fields (`_thinking_frame`, `_flower_frame`, `elapsed_s` Signal, `compact_tick`, and their assorted `tick()` branches) into a single `frame: Signal[int]` that increments unconditionally every 50 ms. All animated UI elements derive their frame index from `frame() % N`.

### Before vs After

| Before | After |
|---|---|
| `_thinking_frame: int` | removed |
| `_flower_frame: int` | removed |
| `elapsed_s: Signal[float]` (display + redraw) | `elapsed_s: @property → float` (display only) |
| `compact_tick: Signal[int]` (compaction redraw) | removed |
| 3 workspace subscriptions for animation | 1 subscription (`frame`) |
| `tick()` has conditional branches per feature | `tick()` is one line: `self.frame.set(self.frame() + 1)` |

### Acceptance criteria

| # | Criterion |
|---|---|
| 37.1 | `frame` increments on every `tick()` call, regardless of agent state or compaction state |
| 37.2 | `frame` never resets between turns — all consumers use `frame() % N` |
| 37.3 | `elapsed_s` is a read-only property returning `time.monotonic() - _start_time` (0.0 when idle) |
| 37.4 | `compact_tick`, `_thinking_frame`, `_flower_frame` do not exist on `ConversationStore` |
| 37.5 | Workspace subscribes to `frame` (not `elapsed_s` or `compact_tick`) for animation redraws |
| 37.6 | Flower, thinking animation, and compaction spinner all read `conv.frame() % N` |

---

## 38. Concurrent Typed Subagents (PRD-124)

Any agent in any mode or workflow can optionally spawn a pool of specialised
subagents to run in parallel. The parent LLM decides whether and what to spawn
via the `spawn_subagents` tool; the pool returns a plain-text labelled digest
the parent reads as prose.

### Architecture

| Component | Role |
|---|---|
| `src/agenthicc/subagents/types.py` | `SubagentTypeSpec`, `SubagentAggregator`, `SubagentTypeRegistry`, `DEFAULT_REGISTRY` |
| `src/agenthicc/subagents/pool.py` | `SubagentWorker`, `SubagentPool`, `SubagentPoolState`, `WorkerState`, `AggregatedResult` |
| `src/agenthicc/subagents/tool.py` | `make_spawn_subagents_tool()` factory — produces `spawn_subagents` `@tool()` |
| `AgentTurnRunner._build_agent()` | Injects `spawn_subagents` into every agent turn |
| `ConversationStore.subagent_pool_state` | `Signal[SubagentPoolState \| None]` — live pool state for TUI |
| `StatusComponent` line 1 | `N/M subagents` counter in magenta while pool is active |
| `FooterComponent` worker grid | Per-worker status row (`○ pending`, `⠸ running`, `✓ done`, `✗ failed`) |
| Scroll buffer renderers | `subagent_pool_started`, `subagent_worker_done`, `subagent_pool_done` |
| Resume cache | `subagent_pool_result` event stores fingerprint → cached text; same task set reuses result |
| Plugin ecosystem | `SUBAGENT_TYPES = [SubagentTypeSpec(...)]` in `.agenthicc/tools/` auto-registers types; `SubagentAggregator` for custom digests |

### Built-in subagent types

| Type | Writes | Executes | Purpose |
|---|---|---|---|
| `explorer` | ✗ | ✗ | Read-only codebase investigation |
| `planner` | ✗ | ✗ | Produces a numbered implementation plan |
| `implementer` | ✓ | expr-only | Carries out a scoped code change |
| `tester` | ✓ | ✓ | Writes or runs tests |
| `reviewer` | ✗ | expr-only | Reviews code, returns APPROVED / NEEDS CHANGES |
| `documenter` | ✓ | ✗ | Writes or updates documentation |
| `verifier` | ✗ | ✓ | Adversarially checks a requirement holds |
| `researcher` | ✗ | ✗ | Searches local files to answer a question |

### Acceptance criteria

| # | Criterion |
|---|---|
| 38.1 | `spawn_subagents(tasks=[...])` available in every agent turn (all modes and workflows) |
| 38.2 | Workers run concurrently bounded by `asyncio.Semaphore(max_concurrent)` |
| 38.3 | Each worker uses an isolated `ShortTermMemory` and the parent's transport |
| 38.4 | `ToolCapabilityGate` enforces capability intersection per worker |
| 38.5 | Scroll buffer shows `▶ Spawning N`, per-worker `✓/✗ [N/M]`, and `◈ N/M complete` |
| 38.6 | Status bar shows `N/M subagents` counter while pool is active |
| 38.7 | Footer shows per-worker status grid while pool is active |
| 38.8 | `subagent_pool_result` event enables resume: same task fingerprint returns cached result |
| 38.9 | `SUBAGENT_TYPES` in plugin files registers custom types into `DEFAULT_REGISTRY` |
| 38.10 | `SubagentAggregator` subclass produces custom text digest for a named type |
| 38.11 | No `Any` in public signatures — concrete types throughout |

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

---

## 39. Tool Namespace (PRD-125)

Groups the 50 built-in tools into named domains for structured system-prompt
sections, cross-group collision warnings, and glob patterns in subagent specs.

### Architecture

| Component | Role |
|---|---|
| `ToolGroup` dataclass | `name`, `label`, `description`, `tools`, `priority` |
| `ToolRegistry.register_group(group)` | Registers tools and records `tool_name → group.name` membership |
| `ToolRegistry.glob_expand("fs.*")` | Expands to all tool `__name__` values in group `"fs"` |
| `ToolRegistry.describe()` | Grouped Markdown sections ordered by priority |
| `BUILTIN_GROUPS` in `agent_tools.py` | `[FS_GROUP, GIT_GROUP, EXEC_GROUP, OUTLOOK_GROUP]` |
| `build_registry()` | Uses `register_group()` instead of flat `register_many()` |
| `_expand_allowed(allowed, registry)` | Expands globs in `SubagentTypeSpec.allowed_tools` at pool-creation time |

### Built-in groups

| Group | Key | Priority | Count |
|---|---|---|---|
| File System | `fs` | 4 | 24 |
| Git | `git` | 3 | 11 |
| Shell / Exec | `exec` | 2 | 6 |
| Outlook / Calendar | `outlook` | 1 | 9 |

### Acceptance criteria

| # | Criterion |
|---|---|
| 39.1 | `ToolGroup` dataclass has `name`, `label`, `description`, `tools`, `priority` |
| 39.2 | `register_group()` records group membership for every registered tool |
| 39.3 | `glob_expand("fs.*")` returns all 24 FS tool names |
| 39.4 | `glob_expand("git_status")` returns `frozenset({"git_status"})` (literal pass-through) |
| 39.5 | Cross-group shadowing emits WARNING; same-group override stays at DEBUG |
| 39.6 | `describe()` groups tools into labelled sections with count and italic description |
| 39.7 | `BUILTIN_GROUPS` exported from `agent_tools.py` |
| 39.8 | `build_registry()` uses `register_group()` for all four built-in groups |
| 39.9 | Subagent `allowed_tools=frozenset({"fs.*"})` expands to all 24 FS tool names |

---

## 40. Transport Retry with Memory Rollback (PRD-126)

Prevents `ReadTimeout` and other transient network errors from permanently
failing a workflow phase.  A snapshot-rollback mechanism retries the agent turn
with a clean memory state, avoiding the double-user-message problem that makes
naïve transport-level retries produce a 400.

### Architecture

Retry lives at the **single choke point** — `AgentTurnRunner._stream()` — via
the shared helper `runners/retry.py::run_with_transport_retry`.  Every workflow
phase, `run_phase`, and direct TUI turn flows through `_stream`, so all paths
get retry with no call-site wrappers.  (This is also the only place it can fire:
`_stream` catches transient errors and swallows them per PRD-117, so a
call-site wrapper could never observe them.)

| Component | Role |
|---|---|
| `runners/retry.py::run_with_transport_retry` | Shared helper: snapshot-rollback, jitter, total-duration cap, deadline awareness, `reset_fns`, async/sync `on_retry` |
| `runners/retry.py::RetryConfig` | `max_retries`, `base_delay_s`, `max_total_duration_s`, `jitter` |
| `AgentTurnRunner._stream_with_retry()` | Wraps the `run_stream` + chunk loop; resets approval-turn state between attempts; emits `TransportRetryScheduled` + scroll event |
| `_is_transient_network_error(exc)` | Matches `TransientTransportError` + library timeout names; **excludes** bare `TimeoutError` (= `asyncio.TimeoutError`) |
| `SubagentWorker._execute()` | Wraps `runner.run()` (subagents bypass `_stream`); retry config threaded from `exec_cfg` |
| `tui_session.run_turn()` | Computes `retry_deadline_monotonic` from `turn_timeout_s`; threaded via `_run_agent_turn` |
| `ExecutionSettings` | `transport_max_retries` (turn-level, 3), `transport_retry_base_delay_s` (1.0), `transport_retry_max_total_s` (0=off), `llm_sdk_max_retries` (SDK, 2) |
| `build_llm_config()` | Passes `llm_sdk_max_retries` (not `transport_max_retries`) so SDK + turn layers don't multiply |

### Error taxonomy

| Exception | Permanent? | Transient network? | Action |
|---|---|---|---|
| HTTP 400–499 (not 429) | Yes | No | Fail immediately |
| HTTP 429, 5xx | No | No | Swallow → phase retries whole turn |
| `TransientTransportError` | No | Yes | Snapshot-rollback retry |
| `ReadTimeout`, `APITimeoutError`, `ConnectError`, … | No | Yes | Snapshot-rollback retry |
| bare builtin `TimeoutError` (= `asyncio.TimeoutError`) | No | **No** | Not retried (would mask `wait_for` timeouts) |

### Why snapshot-rollback is required

`run_stream()` / `runner.run()` call `memory.add_user(message)` internally.  A
naïve retry with the same `ShortTermMemory` would add the user message twice,
producing an invalid conversation the API rejects with a 400.  Restoring the
pre-turn snapshot ensures every attempt starts with a clean history.

### Acceptance criteria

| # | Criterion |
|---|---|
| 40.1 | Retry config fields on `ExecutionSettings`: `transport_max_retries` (3), `transport_retry_base_delay_s` (1.0), `transport_retry_max_total_s` (0.0), `llm_sdk_max_retries` (2) |
| 40.2 | `_is_transient_network_error` True for `TransientTransportError` + library timeout names; False for bare `TimeoutError` and HTTP 4xx |
| 40.3 | Retry lives in `AgentTurnRunner._stream` via `run_with_transport_retry` — covers workflow phases, `run_phase`, and direct TUI turns |
| 40.4 | On transient error, memory is restored to its pre-turn snapshot |
| 40.5 | Approval-turn state is reset between attempts (`reset_fns`); gates re-present cleanly |
| 40.6 | `TransportRetryScheduled` kernel event + `⟳ Network error — retrying N/M…` scroll event emitted on each retry |
| 40.7 | Exponential backoff has jitter; `transport_retry_max_total_s` caps total wall-clock |
| 40.8 | A retry is skipped when it cannot run before the turn-timeout deadline |
| 40.9 | Subagent workers retry transient errors via the shared helper |
| 40.10 | `CancelledError` never retried; `transport_max_retries = 0` disables retry |
| 40.11 | `build_llm_config()` passes `llm_sdk_max_retries` (not `transport_max_retries`) to all providers |

---

## 41. Windows Shift+Tab Mode Cycling (PRD-127)

Fixes Shift+Tab failing to cycle the operational mode on Windows.

### Root cause

`WindowsBackend.read_key()` read input via `msvcrt.getwch()`, which reads
translated console key events and **cannot report the SHIFT modifier on Tab** —
Shift+Tab collapsed to plain `Key.TAB`, which no capability consumes.  The
VT `\x1b[Z` decode path added by PRD-106 was dead because `getwch()` never sees
the VT byte stream (it requires `ENABLE_VIRTUAL_TERMINAL_INPUT` + raw reads,
neither of which `getwch()` does).

### Fix — `ReadConsoleInputW` via ctypes

| Component | Role |
|---|---|
| `_decode_key_event(vk, unicode_char, ctrl_state)` | Pure decoder; `VK_TAB` + `SHIFT_PRESSED` → `Key.SHIFT_TAB`. Unit-tested on Linux |
| `_INPUT_RECORD` / `_KEY_EVENT_RECORD` | ctypes structs with portable `c_*` types (no `ctypes.wintypes`) |
| `_next_input_event()` | Thin `ReadConsoleInputW` reader (the only Windows-API call; monkeypatched in tests) |
| `_read_key_console()` | Loops, skipping key-up / non-key / modifier-only events |
| `enter_raw_mode()` | Clears `ENABLE_LINE_INPUT`/`ECHO`/`PROCESSED` input flags; restores on exit |
| getwch fallback | Used when no real console handle is available |

### Acceptance criteria

| # | Criterion |
|---|---|
| 41.1 | `VK_TAB` + `SHIFT_PRESSED` decodes to `Key.SHIFT_TAB`; plain `VK_TAB` → `Key.TAB` |
| 41.2 | `VK_RETURN` + CTRL → `CTRL_ENTER`; arrows/HOME/END/BACKSPACE/ESC decode by virtual key |
| 41.3 | Control chars (Ctrl+C/D/U/V), `@` → `AT`, printable → `CHAR`; modifier-only → ignored |
| 41.4 | `_read_key_console` skips key-up, non-key, and failed reads |
| 41.5 | Module imports on Linux (portable ctypes types); decoder fully unit-tested |
| 41.6 | getwch fallback preserved for non-console environments |

---

## 42. Remove the unwired kernel runtime trio (PRD-128)

The original PRD-01/PRD-03 kernel agent-execution layer — `CommunicationTools`,
`AgentPool` / `AgentRecord`, and `Scheduler` in `src/agenthicc/runtime/` — is dead
production code. It was superseded by lauren-ai's `AgentRunnerBase` and the
`agenthicc.workflows` runners. This PRD removes it (Phase 1: self-contained cut).

| Aspect | Expectation |
|---|---|
| No live importers | Nothing outside `runtime/` imported the trio; it was not re-exported at the package top level |
| Self-referential events | Only `comm_tools.py` / `scheduler.py` emitted `AgentSpawnRequest` / `TaskCreated` / `TaskAssigned` / `WorkflowNodeAdded` / `WorkflowNodeRemoved` |
| Effects discarded | Every live `EventProcessor` uses `NoOpEffectExecutor`; `spawn_agent` / `assign_task` / `start_workflow_node` effects were produced and dropped |
| State unread | `AppState.agents` / `AppState.tasks` (the dicts those reducers fill) were read by no live code |
| `mcp_connect` test-only | `CommunicationTools.mcp_connect` (PRD-30) had no live caller; the live MCP path uses `McpToolRegistry` |
| Tests removed | `test_agent_pool.py`, `test_comm_tools.py`, `test_scheduler.py`, `test_mcp_connect.py`, `test_runtime_cycle.py` |
| Docs synced | CLAUDE.md, AGENTS.md, CONTRIBUTING.md, README.md, `llms.txt`, `llms-full.txt`, the `docs/` site, and the reference skills no longer reference the trio (or the already-removed PRD-116 `agenthicc.workflow` package) |

### Acceptance criteria

| # | Criterion |
|---|---|
| 42.1 | `src/agenthicc/runtime/` no longer exists; the five trio test files are removed |
| 42.2 | `grep -rn "agenthicc.runtime\|CommunicationTools\|AgentPool\|AgentRecord\|\bScheduler\b" src/ tests/` returns nothing |
| 42.3 | No active (non-historical-PRD) doc references the trio; `docs/reference/communication-tools.md` and `docs/guides/agents.md` are removed with no dangling `mkdocs.yml` nav entry |
| 42.4 | `uv run pytest tests/ -q`, `uv run mypy src/agenthicc`, and `uv run ruff check src/ tests/` pass |
| 42.5 | Phase 2 (kernel reducer / state / config pruning) is documented as a deferred follow-up |

---

## 43. Conversation Durability & Retry Resilience — Phases 1, 2 & 3 (PRD-129)

A transport-retry (PRD-126) rolls a whole agent turn back to its pre-turn memory
snapshot and re-runs it.  PRD-129 fixes the consequences of that design:
already-completed tools were **re-executed** on the retry, conversation state had
no **mid-turn durability** (only a turn-boundary SQLite snapshot, so a crash lost
the in-flight turn), and an interrupted turn could not be **resumed**.

### Phase 1 — Idempotent tool execution

A turn-scoped `IdempotencyLedger` (lauren-ai `_tools/_idempotency.py`) records
each successful tool result by `(name, canonical-input)`.  Threaded through
`run` / `run_stream` → `AgentContext` → `_execute_single_tool`, a replayed call
returns the recorded result instead of re-dispatching (checked *before* the HITL
gate, so an already-approved side effect is not re-prompted).  agenthicc creates
one ledger per turn in `AgentTurnRunner._stream`, outside the retry loop, so it
survives across attempts.

Replay is **scoped to a rollback**, not to content alone: `record` adds to a
*pending* set, a rollback `promote`s pending → *committed*, and only *committed*
results are replayed (consumed FIFO).  So a retried call replays its earlier
result, but a **legitimate repeat call within one attempt** (reading a file
twice, or after writing it) still runs live and sees fresh data.  Replayed
results are **rekeyed to the current `tool_use_id`** — the model issues a fresh
id every attempt, and leaking the recorded id corrupts the conversation with a
`tool_result` that has no matching `tool_use` block (a hard provider 400).

| Aspect | Expectation |
|---|---|
| Side-effect once | A `write_file` / `run_bash` / `git_commit` that completed before a rollback is **replayed**, not re-run, on the retry |
| Repeat runs live | The same `(name,input)` called twice within one attempt executes both times (no content-dedup) |
| Correct id | A replayed result's `tool_use_id` is rekeyed to the current call (no orphaned `tool_result` 400) |
| Errors re-run | Only successful (`not is_error`) results are recorded; a failed tool runs again on retry |
| HITL respected | A committed-replay hit skips dispatch *and* the approval gate (the effect already happened) |
| Opt-in / inert | `idempotency_ledger=None` (default) preserves byte-identical legacy behavior |

### Phase 2 — Durable ConversationJournal

`ShortTermMemory` becomes a *projection* of an append-only, `fsync`-ed
`ConversationJournal` (`memory/journal.py`).  `JournaledShortTermMemory`
(`memory/journaled.py`) mirrors every `add_user` / `add_assistant` /
`add_tool_results` as an `append` entry and every `restore` (retry rollback) /
compaction as a `reset` entry.  Folding the journal reconstructs the exact
message list, so resume and crash recovery are transparent.

| Aspect | Expectation |
|---|---|
| Mid-turn durability | Every transition is `fsync`-ed as it happens — a crash mid-turn no longer loses the in-flight turn |
| Resume by fold | On `--resume` (`session_id == resume_id`) the journal is folded back into memory at construction; no separate load step |
| Rollback-correct | A retry `restore` writes a `reset`; folding honours it, so failed-attempt appends are superseded |
| Crash-safe fold | A corrupt trailing line (crash mid-write) is skipped; everything before it is intact |
| No dual paths | The turn-boundary SQLite `memory_snapshots` mechanism (`conversation_store.py`) is **removed**, superseded by the journal |

### Phase 3 — Resumable execution (RunCoordinator)

The journal gains **turn-lifecycle markers** (`turn_started`/`turn_completed`)
and **durable tool records** (`tool_recorded`).  Every direct turn runs under a
`DurableIdempotencyLedger` (`runners/durable_ledger.py`) that `fsync`s each tool
result keyed by the turn id, and emits `turn_started` (with the pre-turn
rollback point) at the start and `turn_completed` in the `finally` — so only a
hard process death (SIGKILL) leaves a turn unmarked.  On resume,
`RunCoordinator` (`runners/run_coordinator.py`) folds the journal for an
incomplete turn, rolls memory back to its pre-turn point, seeds a ledger with the
tools it already ran, and re-drives it — replaying completed side effects rather
than repeating them.

| Aspect | Expectation |
|---|---|
| Crash detected | A `turn_started` with no `turn_completed` is found by `fold_resume_state`; a clean session yields `None` |
| Resume from step | The re-driven turn reuses the original `turn_id`; the seeded ledger replays already-run tools |
| Rollback point | `JournaledShortTermMemory.rollback_to(base_count)` returns memory to the pre-turn state before re-driving |
| Handled ≠ crash | `turn_completed` fires for success, handled errors, and cancellation — only SIGKILL flags a turn for resume |
| Defers to workflows | Auto-resume fires only for a direct turn (no in-progress workflow); workflow crashes stay with PRD-94 |
| Durable replay | Tool records survive a process restart — re-execution is skipped even across a crash |

### Acceptance criteria

| # | Criterion |
|---|---|
| 43.1 | A successful side-effecting tool requested again after a rollback (same ledger) executes exactly once |
| 43.2 | The same `(name,input)` called twice within one attempt runs live both times (no content-dedup) |
| 43.3 | A replayed result is rekeyed to the current `tool_use_id` (no orphaned-`tool_result` 400) |
| 43.4 | A tool that errors is not recorded and re-runs on the next attempt |
| 43.5 | `idempotency_ledger=None` leaves runner behavior unchanged (regression suite green) |
| 43.6 | Folding a journal reproduces the live `ShortTermMemory` message list exactly; corrupt trailing line skipped |
| 43.7 | Re-opening a journal path reconstructs prior history without duplication (resume) |
| 43.8 | The SQLite `conversation_store.py` is removed; no live importer remains |
| 43.9 | `fold_resume_state` finds an incomplete turn (no `turn_completed`) and `None` for a clean/re-driven-completed session |
| 43.10 | A `DurableIdempotencyLedger` seeded from journal records replays them without re-execution |
| 43.11 | Auto-resume defers to the workflow path when a workflow run is in progress |
| 43.12 | New suites green: `test_idempotency.py` (lauren-ai), `test_conversation_journal.py`, `test_journaled_memory.py`, `test_run_resume.py` |
| 43.13 | Phase 4 (lower the retry boundary to per-round-trip; streaming delta checkpoints) remains deferred per PRD-129 |

---

## 44. Context Reuse — Prompt Caching + File Cache (PRD-132, L0+L1)

A turn that reads many files re-paid full input price for those file contents on
*every* later turn (prompt caching covered only system + tools, and defaulted
off), and `read_file` re-read from disk with no durable record.  PRD-132
implements the first two layers of the PRD-131 reuse stack.

### L0 — Incremental conversation prompt caching

lauren-ai marks the **last content block of the last message** with
`cache_control: ephemeral` each request (`_apply_conversation_cache`), so the
provider serves the file-heavy history prefix from cache (~90% cheaper) instead
of re-billing it.  Gated by `LLMConfig.cache_conversation`; agenthicc's
`prompt_cache` flag (default **on**) enables system + tools + conversation
caching via `build_llm_config`.

| Aspect | Expectation |
|---|---|
| History cached | The conversation prefix (where file reads live) is a cache breakpoint, not re-billed each turn |
| Normalised | String message content is normalised to a text block before marking |
| Breakpoint budget | Adds 1 breakpoint (history) atop system + tools = 3 of Anthropic's 4 |
| Provider-safe | Cache flags are read only by the Anthropic transport — a clean no-op on OpenAI/Ollama/litellm |
| Configurable | `[execution] prompt_cache` (default `true`); `--set execution.prompt_cache=false` |

### L1 — Durable, freshness-validated workspace file cache

`WorkspaceFileCache` (`tools/fs/file_cache.py`) — a per-project SQLite store
keyed by absolute path with `(sha256, mtime, size, encoding, content)`.
`ReadFileTool` serves a cached read **only** when the file is unchanged;
otherwise it reads and records.  Wired via a process singleton configured at
session startup; disabled → the read path is unchanged.

| Aspect | Expectation |
|---|---|
| Freshness | A cached read is served only when `(mtime, size, encoding)` match; any change misses and re-reads (never stale) |
| Durable | The cache is SQLite — a new session/process resolves a prior read |
| Tagged | A cache hit returns `cached: True`; a miss reads disk and stores |
| Substrate | The durable, content-addressed record is the base PRD-131 L2 (repo map) / L3 (RAG) build on |
| Configurable | `[execution] file_cache` (default `true`) |

### Acceptance criteria

| # | Criterion |
|---|---|
| 44.1 | With `cache_conversation`, the request's last message carries `cache_control: ephemeral`; off → it does not |
| 44.2 | `_apply_conversation_cache` normalises string content and adds exactly one breakpoint (the last block) |
| 44.3 | `prompt_cache` (default `true`) enables system/tools/conversation caching; settable from TOML + `--set` |
| 44.4 | Cache flags are a no-op on non-Anthropic providers |
| 44.5 | `WorkspaceFileCache.get_fresh` returns content iff `(mtime, size, encoding)` match; changed/deleted file misses |
| 44.6 | The cache is durable across `WorkspaceFileCache` instances on the same DB |
| 44.7 | `ReadFileTool` serves a fresh hit (`cached: True`), stores on miss, and is a no-op when the cache is disabled |
| 44.8 | New suites green: `test_prompt_cache.py` (lauren-ai), `test_file_cache.py` (agenthicc) |
| 44.9 | L2 (repo map) and L3 (durable file RAG) remain deferred per PRD-131 |

---

## 45. Context-Window Overflow Guard — Bounded Output, Pre-Send Cap, Model-Aware Budget (PRD-133, A+B+C+D+E)

A `code_plan` turn 400'd with **1.5M tokens vs a 1.048M model limit** after a
recursive `list_directory`/`search_files` on a tree containing `.venv`/`.git`.
Nothing guaranteed the request fit the model.  PRD-133 ships all five layers:
**C** (memory char-budget invariant) + **A** (remove the trigger), then
**B** (model-aware budget), **D** (accurate `count_tokens` accounting), and
**E** (graceful failure) — so the request is provably ≤ the *model's actual*
usable window, measured exactly, with an actionable error for the irreducible
case instead of an opaque provider 400.

### Layer C — Hard pre-send budget guarantee

`ShortTermMemory.messages()` now ends with `_enforce_char_budget`: after the
sliding-window trim (which never drops past the last conversational user message),
it **truncates block contents** — never removing blocks, so `tool_use`/`tool_result`
pairing stays valid — until the list fits the memory budget.  A single oversized
turn (huge tool result) can no longer escape the budget, so a context-length 400
is **structurally impossible**.

| Aspect | Expectation |
|---|---|
| Hard cap | `messages()` output is always within `max_tokens` (chars/4), even for a single un-droppable oversized turn |
| Floor still holds | The last conversational user message is kept (never empty list) — it's truncated, not dropped |
| Structure preserved | `tool_use`/`tool_result` blocks survive (ids intact); only text/content strings shrink; `tool_use` inputs untouched |
| No-op when fitting | A normal in-budget conversation is returned unchanged |

### Layer A — Bound tool output at the source

`list_directory` (recursive), `search_files`, and `grep_files` enumerate via
**`git ls-files --cached --others --exclude-standard`** — the project's own
`.gitignore` defines relevance (tracked + untracked-not-ignored), far more
complete than a hardcoded blocklist — and **fall back to a full walk when there's
no git** (completeness).  Results are capped at `_MAX_LIST_ENTRIES`; `read_file` /
`read_lines` cap content at `_MAX_TOOL_OUTPUT_CHARS` (~25k tokens) with a
truncation marker (and `read_file` still caches the full bytes).

| Aspect | Expectation |
|---|---|
| Git-aware | In a git repo, ignored paths (`.venv`/`.git`/`node_modules`/build) are excluded via `git ls-files` |
| Fallback | No git → full walk (read everything), capped + backstopped by Layer C |
| Entry cap | `list_directory`/`search_files` capped at `_MAX_LIST_ENTRIES` with a `truncated` flag |
| Read cap | `read_file`/`read_lines` capped at `_MAX_TOOL_OUTPUT_CHARS` with a marker; full bytes still cached (L1) |

### Layer B — Model-aware context budget

The budget is now derived from the **model's real context window** instead of a
hardcoded 1M constant.  lauren-ai gains a `MODEL_CONTEXT_WINDOWS` registry +
`context_window_for(model)` (longest-prefix match; conservative default for
unknown/proxied models), and `AgentConfig.context_window` →
`usable_context_budget` = `window − max_tokens_per_turn − max(4k, window/25)`.
agenthicc resolves the window via `ExecutionSettings.effective_context_window()`
— an explicit `[execution] model_context_window` override wins, else the registry
— and `agent_turn` derives the per-turn summarisation window and the hard-guard
ceiling from it (replacing the old `compact_threshold × 0.8` math).

| Aspect | Expectation |
|---|---|
| Registry | `claude-opus-4-*`/`-sonnet-4-*` → 1M; `gpt-4o` → 128k; unknown → 200k default |
| Override | `[execution] model_context_window = N` is authoritative for proxied/gateway models |
| Derivation | summarise fires at 80 % of usable; the hard guard ceiling is the same window |

### Layer D — Accurate token accounting

Every transport's `count_tokens` now routes its heuristic fallback through one
shared, **dict-safe** `estimate_message_tokens` (the runner passes dict messages
*and* dict tool schemas — the old attribute-access heuristic raised
`AttributeError` on exactly the non-native-endpoint path the guard needs).  The
runner adds `_fit_to_context` at **both** `complete()` choke points: a cheap
local estimate (≈3.5 chars/token, counts `tool_use` inputs) gates an **exact**
`count_tokens` call, which drives truncation only when genuinely near the limit —
no per-turn network round-trip in the common case.

### Layer E — Graceful failure

When a request cannot be reduced below the window even after maximal truncation —
an irreducible mandatory item such as a single huge `tool_use` input — the runner
raises `AgentContextOverflowError` (required/budget tokens + model, with an
actionable message) instead of letting the provider reject it with an opaque 400.

| Aspect | Expectation |
|---|---|
| Hard ceiling | No request is sent whose **exact** `count_tokens` exceeds `usable_context_budget` |
| Truncatable | Oversized `tool_result` content is shrunk to fit; `tool_use`/`tool_result` pairing preserved |
| Irreducible | A single over-window `tool_use` input → `AgentContextOverflowError`, not a 400 |
| Cheap path | An in-budget turn does no `count_tokens` round-trip (cheap-estimate gate) |
| Dict-safe | `count_tokens` / the guard accept the runner's dict messages **and** dict tool schemas |

### Acceptance criteria

| # | Criterion |
|---|---|
| 45.1 | `messages()` output never exceeds the budget, even with a single 500k-char tool result (no overflow) |
| 45.2 | The last conversational user message survives (truncated, not dropped); list is never empty |
| 45.3 | `tool_use`/`tool_result` pairing and ids are preserved through truncation; `tool_use` inputs untouched |
| 45.4 | A normal in-budget conversation is returned unchanged |
| 45.5 | In a git repo, `search_files`/`list_directory`/`grep_files` exclude `.gitignore`d paths via `git ls-files` |
| 45.6 | Without git, the tools fall back to a full walk (completeness), still entry-capped |
| 45.7 | `read_file`/`read_lines` cap output with a marker; `read_file` still caches the full content (PRD-132 L1) |
| 45.8 | New suites green: `test_context_budget.py` + `test_context_window_guard.py` (lauren-ai), `test_fs_output_bounds.py` + `test_model_context_budget.py` (agenthicc) |
| 45.9 | The `code_plan` "concise the docs" scenario no longer triggers a context-length 400 |
| 45.10 | **Layer B**: budgets derive from `context_window_for(model)` / the `[execution] model_context_window` override, not a hardcoded constant |
| 45.11 | **Layer D**: the pre-send guard uses exact `Transport.count_tokens`; its heuristic fallback is dict-safe for both messages and tool schemas (no `AttributeError`) |
| 45.12 | **Layer D**: an in-budget turn skips the exact `count_tokens` round-trip via the cheap-estimate gate |
| 45.13 | **Layer E**: an irreducible over-window request raises `AgentContextOverflowError` (actionable), never a provider 400 |
