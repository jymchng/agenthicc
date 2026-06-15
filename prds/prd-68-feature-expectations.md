# PRD-68 — agenthicc TUI: Full Feature Expectations

This document is the authoritative list of every user-facing feature the
agenthicc TUI must deliver.  It is written from the user's perspective and
serves as a regression checklist and acceptance-test reference.

---

## 1. Live Status Bar (always-on, never bounces)

The status bar sits at the top of the always-on Live block and never moves
when tool calls are added to the scroll buffer.

| # | Feature | Expected behaviour |
|---|---|---|
| 1.1 | Flower animation | A Unicode flower icon (`✿❀❁❃✾❋✽❊`) cycles every ~100 ms while the agent is active. |
| 1.2 | State label | Shows `Thinking` (with one bold character bouncing left↔right) while the LLM generates, `Running` while a tool executes, `Idle` otherwise. |
| 1.3 | Runtime | `│ Runtime: mm:ss` appears while the agent is active, counting up. |
| 1.4 | Active tool name | `│ tool_name` appears next to the state when a tool is running. |
| 1.5 | Model name (line 2) | Always shows `provider/model` (e.g. `openai/poolside/laguna-xs.2`). |
| 1.6 | Session info (line 3) | `session-id  │  N turns  │  $cost  ↑ tokens_in  ↓ tokens_out`. Updates live. |
| 1.7 | Width-safe | All three lines are truncated with `…` if they exceed the terminal width. Never wrap or overflow. |

---

## 2. Scroll Buffer (conversation transcript)

Content appears above the always-on Live block and scrolls naturally.

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
| 2.10 | No duplicates | Each item appears exactly once. No repeated turn headers or tool calls. |
| 2.11 | No status bar content in scroll buffer | `✿ Idle`, separators, or footer lines must never leak into the transcript. |

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
| 3.9 | Enter | Submit the current buffer as a new message. |
| 3.10 | Paste condensation | Bracketed paste (`\x1b[200~…\x1b[201~`) inserts the text but shows `[Pasted text with N chars]`. Backspace deletes the whole paste. Ctrl+V expands. Any other key exits condensed mode. |
| 3.11 | Width-safe | Long lines are wrapped/truncated; the Live block height never overflows the terminal. |

---

## 4. Footer

| # | Feature | Expected behaviour |
|---|---|---|
| 4.1 | Mode line (row 1) | `⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵` — unchanged during streaming. |
| 4.2 | Context hints (row 2) | `Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention` — unchanged during streaming. |
| 4.3 | Notification | Transient text replaces row 2 for ~2 s (e.g. `❖ Switched to Code mode`, `Press Ctrl+C again to exit.`). Clears on timeout or next keypress. |

---

## 5. Trigger System

| # | Feature | Expected behaviour |
|---|---|---|
| 5.1 | `@` opens file picker | Typing `@` (or `@` followed by a path fragment) opens the @-mention dropdown in the Live block overlay. |
| 5.2 | `/` opens command picker | Typing `/` at the start of a word opens the slash-command dropdown. |
| 5.3 | Dropdown navigation | Up/Down arrows navigate matches; selected item is highlighted. |
| 5.4 | Enter / Tab to select | Enter inserts the selected item and closes the overlay. Tab inserts and appends a space. |
| 5.5 | Esc to cancel | Closes the overlay without inserting, restores the buffer. |
| 5.6 | Backspace into token | Backspace at the end of a committed `@path` or `/cmd` token re-opens the picker with the existing fragment. |
| 5.7 | Hint text | A short hint/description appears below the match list. |
| 5.8 | Works during streaming | Typing `@` or `/` while the agent is running opens the picker (Live block stays active). |
| 5.9 | No double input bar | When the overlay is active, the composer is NOT also rendered — exactly one prompt line is visible. |

---

## 6. Agent Interaction

| # | Feature | Expected behaviour |
|---|---|---|
| 6.1 | Submit message | Enter sends the current buffer to the agent. |
| 6.2 | Queue during streaming | Typing and pressing Enter while the agent runs queues the message with `⌛ Queued` confirmation. Queued messages are dispatched sequentially after the current turn. |
| 6.3 | ESC cancels agent | Pressing ESC while the agent is streaming cancels the current turn immediately. Status returns to Idle. |
| 6.4 | Ctrl+C cancels agent | Same as ESC during streaming. |
| 6.5 | Double Ctrl+C exits | First press clears the buffer and shows `Press Ctrl+C again to exit.` on the footer. Second press shows the resume hint and exits. |
| 6.6 | Any key clears Ctrl+C prompt | Pressing any key other than Ctrl+C after the first press resets the counter and clears the notification. |
| 6.7 | Session resume hint | On exit the terminal shows `agenthicc --resume <id>` / `agenthicc --continue`. |

---

## 7. Mode System

| # | Feature | Expected behaviour |
|---|---|---|
| 7.1 | Shift+Tab cycles modes | Cycles through the registered modes (Auto, and any from plugins). |
| 7.2 | Mode badge in footer | `⏵⏵ ModeName  (shift+tab to cycle)  │  ctrl+j = ↵` after cycling. |
| 7.3 | Mode notification | `❖ Switched to ModeName mode` appears briefly on the footer. |
| 7.4 | Mode passed to agent | The active mode's system_prompt_suffix is appended to the agent's system prompt. |

---

## 8. Commands

| # | Feature | Expected behaviour |
|---|---|---|
| 8.1 | `/config` | Opens the configuration editor overlay. Navigate with Up/Down, edit with Enter, save with `s`, close with Esc. |
| 8.2 | `/model` | Shows or switches LLM provider/model. |
| 8.3 | `/models` | Lists all available providers and models. |
| 8.4 | `/status` | Displays running agents and their states. |
| 8.5 | `/skills` | Lists available skills. |
| 8.6 | `/help` | Lists all available commands. |
| 8.7 | `/cancel` | Cancels the currently running agent turn. |
| 8.8 | `/clear` | Clears the conversation transcript display. |
| 8.9 | `/expand [id]` | Expands a tool output or @mention that was truncated. |
| 8.10 | `/mcp [connect …]` | Shows MCP server status or connects a new server. |

---

## 9. Plugin System

| # | Feature | Expected behaviour |
|---|---|---|
| 9.1 | Per-project tool plugins | `.agenthicc/tools/*.py` files exporting `TOOLS = [fn1, fn2]` are loaded at startup. Tools are immediately available to the agent. |
| 9.2 | Per-project command plugins | `.agenthicc/commands/*.py` files exporting `COMMANDS = [CommandSpec(…)]` are loaded and appear in the `/` dropdown. |
| 9.3 | Mode plugins | `.agenthicc/modes/*.py` files can register custom modes via `ModeRegistry`. |
| 9.4 | No conflict crashes | Conflicting tool names log a warning (last writer wins); the application never crashes on plugin load errors. |
| 9.5 | Dependency declaration | Plugin files may export `DEPENDENCIES = ["package>=version"]`; missing deps produce a clear error, not an import crash. |

---

## 10. Session Lifecycle

| # | Feature | Expected behaviour |
|---|---|---|
| 10.1 | New session on startup | A UUID session ID is created and shown in the status bar. |
| 10.2 | `--resume <id>` | Resumes a previous session; prior conversation is shown in the scroll buffer. |
| 10.3 | `--continue` | Finds the most recent session for the current directory and resumes it. |
| 10.4 | Session persistence | All conversation events are persisted to `~/.agenthicc/sessions/<id>/conversation.jsonl`. |
| 10.5 | Terminal restored on exit | After any exit path (Ctrl+C ×2, Ctrl+D, exception), ECHO, ICANON, cursor visibility, and bracketed paste are all restored. No broken terminal. |

---

## 11. Resize Handling

| # | Feature | Expected behaviour |
|---|---|---|
| 11.1 | SIGWINCH triggers redraw | Resizing the terminal redraws the Live block at the new width immediately. |
| 11.2 | Width-safe rendering | All components truncate to the new width; no line wrapping that breaks cursor tracking. |

---

## 12. Headless Mode

| # | Feature | Expected behaviour |
|---|---|---|
| 12.1 | `--headless` flag | Reads prompts from stdin (one per line), outputs JSON-lines to stdout. No TUI. |
| 12.2 | JSON event schema | Each event is `{"type": "…", "payload": {…}, "timestamp": float}`. |

---

## Acceptance Criteria (summary)

A release is shippable when:

1. **All 12 sections** above pass manual verification in a real terminal.
2. **Zero terminal corruption**: running 50 agent turns produces no broken cursor state, no stray control characters in the scroll buffer, and no loss of ECHO on exit.
3. **ESC and Ctrl+C cancel the agent** within 200 ms of the keypress.
4. **Triggers work**: `@` and `/` open dropdowns with correct matches during both idle and streaming modes.
5. **No duplicate rendering**: each tool call, turn header, and LLM text line appears exactly once.
6. **Plugin hot-path**: creating a new `.agenthicc/tools/my_tool.py` with `TOOLS=[fn]` in the project directory makes the tool available on the next launch.
