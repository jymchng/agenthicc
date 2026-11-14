# Terminal workspace

The current interactive UI is a Rich Live workspace. It is not the older
prompt-toolkit `build_app()`/`TranscriptModel` design referenced by historical
docs.

The startup welcome panel keeps its `What's new` heading even when the remote
changelog cannot be reached. In that case it displays `No list`; on success it
renders the entries from `https://agenthicc.dev/changelog.json`.

The scroll buffer reads as a conversation, not a log (TUX-06):

- **Your message.** It is a `user_bg` band with the `accent` `❯` and a hanging
  indent. A message longer than 12 lines shows its first four lines and
  `[+N more lines]`; the event keeps the whole text.
- **Mentions.** They show as chips directly under the message
  (`@src/app.py  @docs/`). A mention that cannot be resolved (missing, outside
  the workspace, too large) is a `warning` chip with its reason,
  `! @docs/missing.md not found`. It is never a failed tool row, and the model
  still receives the unresolved-mention note.
- **Turn headers.** An assistant turn has no header while the agent is the
  default and the model has not changed; its text simply follows your message.
  A header such as `◆ reviewer · sonnet-4.6 · 14:02` marks a change of agent
  or model, or a return after more than 30 minutes. Times are local `HH:MM`;
  the time zone is shown only by `/status` and the transcript details.
- **Rhythm.** One blank row comes before each new block. Consecutive tool rows
  have none between them.

The completed `⎋ Compacted` notice is indented beneath its related activity;
the in-progress `⎋ Compacting` notice remains level 1.

## Runtime components

| Component | Location | Responsibility |
|---|---|---|
| Reactive state | `tui/conversation_store.py` | Conversation, input, metrics, mode, overlays, approvals, workflow progress |
| Workspace root | `tui/workspace/workspace.py` | Owns one live block for the application lifetime |
| Live region | `tui/workspace/live_region.py` | Writes the live block by line diff; repaints around scroll-buffer output |
| Frame scheduler | `tui/workspace/frames.py` | Merges redraw requests into capped frames; frame statistics |
| Scroll buffer | `tui/workspace/appender.py` | Prints conversation and tool events above the live block |
| Live components | `tui/workspace/components.py` | Status line, composer and its frame |
| Status-line inputs | `tui/statusline.py` | Git branch, the user status-line command, `/status` |
| Overlay host | `tui/workspace/overlay.py` | Shows one active overlay and redraws the workspace |
| Input session | `tui/input/unified_session.py` | One raw-mode lifetime and capability pipeline |
| Terminal backend | `tui/terminal/` | POSIX/Windows raw mode and key reads |
| Trigger system | `tui/trigger.py`, `tui/triggers/` | Slash commands and `@` mentions |

## Screen model

```text
terminal
├── scroll buffer
│   ├── idle/session headers
│   ├── agent text
│   ├── tool results and collapsed tool groups
│   └── workflow/system/retry notifications
└── permanent live block
    ├── overflow bridge (while a tool group is collapsed)
    ├── composer frame: title, prompt, mode badge (or an overlay in its place)
    ├── subagent panel (while a pool has rows)
    ├── status line
    └── one notice or one contextual hint row, or nothing
```

Idle, the live block is four rows (TUX-02). The composer sits between two
rules, as in Claude Code: the session title and the mode badge sit at the end
of the top rule, and one status line follows.

```text
────────────────────────────────────── fix flaky scheduler test · Safe ─
❯ ask, or type / for commands, @ for files
────────────────────────────────────────────────────────────────────────
 ● Idle · ◆ 2 sessions need you ← · ctx 12% ▰▱▱▱▱ · opus-4.8      /help
```

- **The chevron and rules** use the yellow `frame` theme token, agenthicc's
  brand colour (a darker yellow in the light theme).
- **The cursor** is Claude Code's block cursor: the character under it is
  drawn in reverse video, and a reversed space marks the end of a line. On an
  empty prompt it sits on the first letter of the dimmed placeholder.
- **The state segment.** While the agent works, it turns agenthicc's flower
  (`✿ ❀ ❁ ❃ …`), and a bold letter sweeps back and forth across `Thinking`.
  `reduced_motion` keeps the words and stops both animations.
- **Running calls.** Tool calls that are still running, and background
  terminals, show between the transcript and the composer. Each has a
  blinking `●` and, in parentheses after it, how long it has run; the dot blinks half a second on and
  half a second off, and holds still under `reduced_motion` or the screen
  reader. When a call completes, its row leaves the live region and its
  permanent row is printed to the transcript.

  ```text
  ● $ uv run pytest tests/unit -q (8m 10s)
  ● $ npm run dev (background · 3m 2s)
  ```

- **The title** is the session's first prompt (its first line, cut to 80
  characters as a background session's intent is), or `new session` before
  there is one. A resumed transcript restores it.
- **The mode badge** is the permission mode in its `mode_*` theme token,
  after the primary agent when another than `default` is chosen.
  `[ui] composer_frame = "box"` draws the prompt in a rounded box instead
  of between rules.
- **The status line** shows segments in a fixed priority order and drops the
  lowest ones when the line is too narrow; a segment is never cut mid-word:

  | Priority | Segment | Shown when |
  |---|---|---|
  | 1 | `● Idle`, a spinner with `Thinking` / `Running <tool>` and the elapsed time, or `◆ Waiting for you` | always |
  | 2 | `◆ N sessions need you ←` (AGX-10) | a background session needs you |
  | 3 | `ctx 42% ▰▰▱▱▱`, green below 60 %, amber below 85 %, red above | after the first response that reported usage |
  | 4 | the model, short (`opus-4.8`; the provider when it is not the default) | always |
  | 2 | the running workflow and its phase, from the workflow's own run handle for every runner (built-in or plugin): `◆ goal_flow → Decide goals 2/5` (shortened to the phase, then the workflow, never dropped first; `paused` when paused) | a workflow is running |
  | 5 | subagent workers `1/3 workers`, a `/workflow` selection, running background sessions, startup work still loading | when active |
  | 6 | tokens `↑12k ↓3k` (`≈` when partial) and cost `$0.42` (`~` when estimated) | only when the provider reported them |
  | 7 | the git branch, `*` when the tree has changes | in a repository |
  | right | the shortcuts hint (`/help`) | 60 columns and more |

  The context meter divides the input tokens the provider reported for the
  latest main-agent call (cache reads and writes included) by the model's
  context window (`ExecutionSettings.effective_context_window()`). Below 60
  columns the frame becomes two rules and the status line keeps only the
  state, attention and context segments; below 40 columns only the state.
- **The hint row** appears only for contextual keys: while a turn runs
  (`Esc pause · Ctrl+C cancel`, plus the subagent keys while a pool is shown),
  with a condensed paste, or during a background-terminal wait. Idle, there is
  none: the always-available keys are in `/help`. A notification, the `/loop`
  status or an attention toast takes the hint row's place, wrapped whole.
- **Absent values are not shown.** "cost unknown" and "usage unavailable" never
  appear on the status line, and the session ID and working directory are not
  in the live block: `/status` prints them, together with the model, provider,
  mode, turns, tokens, cost (or why there is none), context use and the paths
  of the session log and memory journal.
- **Overlays.** The agents hub owns the bottom rows: while it is open the
  composer frame and its hint row are hidden, so the hub's hint row is the only
  one. Closing it restores the composer exactly (draft, cursor and paste). Any
  other overlay is drawn between the frame's two rules.

### A user status line

`[ui.statusline] command` replaces segments 3–7 with the first line a command
prints:

```toml
[ui.statusline]
command = "~/.agenthicc/statusline.sh"
```

The command runs in the working directory with the same scrubbed environment
and resource limits as the agent's `[tools.exec]` children, at most once a
second and only after the state changed, with a 500 ms timeout. It reads a JSON
object on stdin with `session_id`, `title`, `cwd`, `git_branch`, `model`,
`provider`, `mode`, `state`, `context_used_pct`, `tokens_in`, `tokens_out`,
`cost_usd`, `agents_need_you` and `workers_running`; a value that is not known
is `null`. Every control sequence except SGR colour is removed from its output,
which is fitted to the space left. A command that fails or is too slow brings
the built-in segments back and is named once in a notice. A project's
`.agenthicc/agenthicc.toml` may set the command only once the file is trusted
(`agenthicc trust add .agenthicc/agenthicc.toml`); an untrusted project's
command is ignored.

`Workspace.start()` is called once, before the processor and input loop begin;
`Workspace.stop()` is called once during teardown. Starting/stopping the block
per turn causes cursor races and duplicated status lines.

Terminal resize signals are debounced into one repaint after the new geometry
settles. That frame erases the block at its old height and paints every row
again, in one write, so an active plan review remains a single overlay instead
of leaking duplicate frames into scrollback.

### Rendering

Redraws cost what changed, not what is on screen (AGX-11):

- **Frames.** Signals request a frame instead of drawing. The scheduler draws
  at most one frame per `[behaviour] frame_interval_ms` (16 ms), merging every
  request made meanwhile. Typing, cursor moves and overlay keys draw on the next
  event-loop iteration. The spinner advances at 10 Hz, and only while a turn or
  compaction is running; an idle TUI draws no frames.
- **Rows.** Each frame is a list of rows rendered at exactly the terminal width,
  so the terminal never wraps one. Status-line and frame rows are built as
  markup and rendered once per distinct row, width and theme, so an unchanged
  row parses no markup. The status line's segments 2–7 are rebuilt only when a
  signal they read changes; a spinner frame recomputes the state segment alone.
- **Composer.** Each logical line of the prompt is folded at the width and the
  fold is cached per line, so a keystroke in a 200-line prompt re-folds one line.
  At most `[behaviour] composer_max_rows` rows (12) are shown around the cursor;
  `↑ N more lines` / `↓ N more lines` stand for the rest.
- **Writes.** The live region keeps the rows it last wrote and rewrites only the
  rows that differ. Within a changed row, segments shared with the old row at
  its start and end are not written again, so a spinner frame overwrites the
  one spinner cell, well under 80 bytes. A resize, a
  width change or a frame that changes more than half of the rows is a full
  redraw. Output printed while the block is live (the scroll buffer, or anything
  written to stdout/stderr) is printed above it and the block repainted below.
- **Synchronized output.** On POSIX terminals the first raw-mode entry asks
  (DECRQM) whether mode 2026 is supported; the key reader consumes the answer.
  If it is, every write is bracketed with `CSI ? 2026 h` … `CSI ? 2026 l` so the
  terminal presents it at once. Windows is not asked, because its console input
  would deliver the answer as typed characters.

`/perf` prints how many frames the session drew, render and write time
percentiles, characters per frame and whether synchronized output is on. It
also prints event-loop lag. Keys, frames and every subagent share one asyncio
loop, so lag is a direct measure of how unresponsive the TUI is. A sampler
wakes every 50 ms and reports p50, p95 and the maximum lateness. When the loop
has not ticked for 250 ms, a watchdog thread records the loop thread's stack,
and `/perf` lists the last five stalls with their duration and the code that
was running (`stall 401 ms at path/to/tool.py:19 in read_everything`). A
garbage collection holds the GIL and stalls the loop from whichever thread
triggered it, while the loop thread looks idle, so collections are timed too:
a stall that was mostly collection reads `garbage collection (450 ms)`.
`tests/performance/test_subagent_loop_lag.py` gates this with six subagents
searching a 33,000-file project: worst stall under 250 ms and p95 under 25 ms.

The shared animation frame advances only while an animated status is active:
thinking, tool execution, recovery, or compaction. Idle, complete, and error
states do not publish frame ticks, and the workspace defensively ignores
compatibility frame writes in those static states. This prevents captured
terminals that do not interpret Rich's erase controls from accumulating
identical `● Idle` status lines while preserving active animation and ordinary state
redraws.

Tool calls are one row each; see [Tool rows](#tool-rows). When a tool group
is collapsed (more than `[tools] max_live_tool_calls` successful calls in a
row), its `└ …and N more tool calls` row is flushed to the scroll buffer at the
next conversation boundary and immediately when the agent is interrupted; it is
never left only in the live status footer. A failed call always prints.
Subagent pool headings, worker-completion rows, and pool summaries are level-1
scrollback rows alongside tool headings; only the worker previews below a pool
heading are indented as child rows. A pool that does not fully succeed is
labeled `Subagents Failed` and ends with one blank row before the next scrollback
block.

Transient provider failures use a structured retry notice rather than dumping
the SDK exception representation into the transcript. The scroll buffer shows
the retry number and delay, a short category such as `Request timed out` or
`Provider temporarily unavailable`, and a bounded provider message when one is
available. Full chained exception details remain at debug log level.

### Tool rows

Each tool call is one row: the operation, a human summary of its target, and
quiet detail (TUX-06). Success is quiet: there is no `Completed` and no output,
and the duration appears, right-aligned, only from one second. As in Claude
Code, every tool call is followed by one blank line.

```text
● Read src/agenthicc/tui/workspace/appender.py:480-560

● $ uv run pytest tests/unit -q                                   2.4s
  └ 1,284 passed, 3 skipped

✗ $ npm test  in objective-create-a/                     exit 1 · 8.1s
  │ FAIL src/App.test.tsx
  │   ● renders learn react link
  │     Unable to find an element with the text: /learn react/i
  └ 41 more lines · ctrl+t to expand

● Edit src/app/main.py  +12 −3

✾ Worked for 1m 15s | 10:20 AM
```

- **Narrow rows.** A command or text target is cut at its end
  (`$ git status && echo …`) and a path in its middle; the quiet detail is
  shown whole, cut only when at least 16 cells fit, and otherwise dropped.

- **Summaries.** A command shows the command itself (`$ npm test`); a leading
  `cd … &&` becomes the quiet `in objective-create-a/`, and a multi-line script
  shows its first line and `+N lines`. Paths are relative to the workspace and
  shortened in the middle (`src/…/appender.py`) so the file name always shows.
  Searches read `"TODO" in src/ → 14 matches`, listings
  `src/agenthicc/ → 23 entries`, git calls `status → 3 changed`, memory reads
  `memory: "deploy notes"` (never raw JSON), and fetches the host and path.
  MCP tools read `server · tool` and their first string argument.
- **Commands** add one result row: a recognised test summary (pytest,
  jest, vitest, cargo, go test) or the last line of the output.
- **Failures** expand: the `danger` `✗`, the exit code or error code, the
  last 8 lines of the output (where errors are) and the expand hint.
- **Running and waiting** keep their words: `running`, `ready`, `waiting`,
  `timed out`, `cancelled`.
- **Edits** show `● Edit path  +12 −3` and the diff, at most 20 rows. Each
  contiguous change block shows at most six changed rows; longer blocks keep
  their first and last three rows around one `...` omission row.
- **Exploration groups** ([exploratory calls](exploratory-tool-calls.md))
  collapse to one row, `● Explored 9 files, 3 searches`, with the targets as
  muted detail.

The detail level sets how much more is shown:

```toml
[ui]
tool_detail = "compact"   # compact | normal | verbose
```

`normal` adds a four-line preview of successful reads and commands, and
`verbose` prints the whole bounded output (the first 8 and last 32 lines of a
call, with the gap marked) and the exploration group's individual calls.
`/details` cycles the level for the session, and `/details verbose` sets it.
The change applies to the next tool call; rows already printed are never
re-rendered, so the scroll buffer stays an append-only log. Opening the last
tool's full output with Ctrl+T belongs to the transcript view (TUX-09).

Tool events carry data, not markup: the summary, a bounded and redacted
projection of the arguments, the duration in milliseconds, the bounded output
and the exit and error codes. Every colour is applied when the row is drawn, so
a theme change restyles history too. Session logs written before TUX-06 are
migrated once, as they are read. A plugin tool can supply its own summary;
see [tool summaries](plugins.md#transcript-summaries).

### Notices

Every error, warning and info line in the scroll buffer is one component,
`tui/workspace/notice.py: Notice(severity, title, body, actions)` (TUX-08):

```text
 ✗ No API key for anthropic.
   Set ANTHROPIC_API_KEY, or run /model to switch provider.

 ! MCP server is unavailable; continuing without its tools.   /mcp

 · Compacted → ~12,400 tokens
```

| Severity | Marker | Token | Used for |
|---|---|---|---|
| `error` | `✗` | `danger` | turn failures, a required MCP server, a command error |
| `warning` | `!` | `warning` | retries, an optional MCP server, a refused command |
| `info` | `·` | `muted` | compaction, recovery, model changes |
| `success` | `✓` | `success` | results the user asked for ("Theme: dark") |

The title wraps under itself, the body sits below it in `muted`, and the
commands a notice suggests follow the title. With ASCII glyphs the markers are
`x ! . +`; with `screen_reader` they are words (`Error:`, `Warning:`, `Note:`,
`Done:`), so a severity never depends on a glyph or a colour. Screen-reader
announcements are printed exactly as their text, with no marker.

Notices are **persistent** when something happened in the conversation: they
are printed into the scroll buffer. **Transient** notices are confirmations and
FYIs ("Do not disturb on", "Mode → Plan"): `ConversationStore.notify(severity,
text)` sets `ConversationStore.transient_notice` for 4 seconds
(`TRANSIENT_NOTICE_S`) for the status line's right segment, and nothing reaches
the scroll buffer or the session log. A newer notice replaces the previous one.
Slash commands report through `CommandContext.notify(severity, text, body=...,
actions=...)`: a one-line `info` or `success` goes to the status line when the
session has one; errors, warnings and anything with a body are printed as a
persistent notice.

### Turn failures

A failed turn's `error` event names a fixed **kind** and agenthicc's own copy
for it; the provider's exception text is never shown or persisted (the session
JSONL, workflow checkpoints, headless JSON and background rows all carry the
copy). `runners/failure_kinds.py: classify_turn_failure(exc, provider)` reads
the exception's type, HTTP status and provider error code; for a missing key,
where the SDK has neither, it matches a fixed allow-list of SDK message
prefixes, tested against the installed SDKs.

| `kind` | Message (example) | Action |
|---|---|---|
| `missing_credentials` | No API key for anthropic. | Set ANTHROPIC_API_KEY, or run /model to switch provider. |
| `rejected_credentials` | anthropic rejected the API key (profile work). | Check the key, or run /model to switch provider. |
| `rate_limited` | Rate limited by anthropic; retried 3 times. | Wait 40 s, then send the message again. |
| `overloaded` | anthropic is overloaded. | Send the message again to retry. |
| `context_too_long` | The conversation is longer than opus-4.8's context. | Run /compact, then send the message again. |
| `model_not_found` | anthropic has no model named claude-x. | Run /model to choose another model. |
| `billing` | anthropic refused the request for billing reasons. | Check the anthropic account's plan and credit. |
| `network` | Cannot reach api.anthropic.com. | Check the network or proxy, then send the message again. |
| `timeout` | anthropic did not answer in 600 s. | Send the message again to retry. |
| `cancelled` | (an interrupt; no error row) | |
| `internal` | The turn failed unexpectedly (TypeError). | Run /logs to see the diagnostics log at ~/.agenthicc/logs/<session>.log. |

An event from an older log has no `kind` and renders its stored message.

### Diagnostics log

While the TUI is live, anything a library writes to `sys.stderr`, and every
Python warning, goes to the session's diagnostics log,
`~/.agenthicc/logs/<session>.log`, instead of being printed between frames.
The first stray write of a session shows the transient notice
"Diagnostics were written to the log · /logs". `/logs` opens the log's last
200 lines in a pager (Esc closes it). The log is installed right after the
first frame. Child processes capture their own stderr (MCP `stderr_tail`,
`tools/exec`), and a unit test checks that no subprocess a session starts
inherits fd 2. `agenthicc --debug` keeps stderr and warnings on the screen.

## Modes

The selectable mode cycle is **Safe → Plan → Yolo → Safe**. Safe is the default:
read, search, and git-read tools run directly, while writes, git changes,
commands, network access, and unannotated tools require approval. Plan is
read-only and hard-blocks those capabilities. Yolo preserves the former Auto
behaviour: tools run without per-action approval.

`Auto` (Yolo), `Guard` and `Ask` (Safe), and `Review` (Plan) remain accepted as
non-displayed compatibility aliases. `Debug` is not an alias and is rejected.
Replay remains an internal state used during session replay and is not
selectable through `/mode` or cycling.

Press Shift+Tab to cycle modes when the input backend is interactive. Workflow
availability is derived from the workflow registry. `/mode [name]` performs an
explicit switch and reports the canonical choices for an unknown name.

## Input and triggers

`UnifiedInputSession` enters raw mode once and dispatches each key through the
active capability list. IDLE supports triggers, history, cursor movement,
paste, mode cycling, and submission. STREAMING reduces the capability set so
the user can queue or interject input, edit the queue, or interrupt an active
turn while keeping every editing key.

### Key bindings

Keys are not hard-coded (AGX-13). Each key resolves, once per keystroke, to a
named action in the composer's context — `composer.submit`, `composer.newline`,
`composer.interrupt` … — and the hint row shows the applicable contextual
actions' labels from the same registry (`agenthicc.tui.actions`), so it can no
longer advertise a key that does nothing. An action can depend on the
composer's conditions (`running`, `paste`, `paste_at_cursor`): Esc deletes a
condensed paste when the cursor is right after it and pauses a running turn
otherwise; Ctrl+C cancels a running turn and starts the double-press exit when
idle.

| Key | Action | When |
|---|---|---|
| Enter | `composer.submit` — send; while a turn runs, queue it for after the turn | always |
| Ctrl+J, Shift+Enter | `composer.newline` | always |
| Alt+Enter, Ctrl+Enter | `composer.newline_alt` — a line break | idle |
| Alt+Enter, Ctrl+Enter | `composer.interject` — deliver at the turn's next tool boundary | a turn is running |
| `/`, `@` | command picker, mention picker (fixed) | always |
| Ctrl+V / Esc | expand / delete a condensed paste | a paste is condensed (Esc: cursor right after it) |
| Esc | `composer.interrupt` — pause the turn | a turn is running |
| Ctrl+C | `composer.cancel` / `composer.quit` | running / idle (press twice) |
| Ctrl+D | `composer.eof` — exit | idle, empty prompt |
| Delete, Ctrl+D | `composer.delete_forward` — delete the character under the cursor | non-empty prompt |
| Ctrl+A / Ctrl+E, Home / End | line start / end; pressed again, start / end of the whole prompt | always |
| Alt+B, Ctrl+← / Alt+F, Ctrl+→ | `composer.word_left` / `composer.word_right` | always |
| Ctrl+W, Alt+Backspace | `composer.delete_word_back` (into the kill ring) | always |
| Alt+D | `composer.delete_word_forward` (into the kill ring) | always |
| Ctrl+K | `composer.kill_to_end` — to the line end (into the kill ring) | always |
| Ctrl+U | `composer.clear` — to the line start; at the start, everything | always |
| Ctrl+Y | `composer.yank` — insert the last kill | always |
| Ctrl+Z / Ctrl+Shift+Z, Alt+Z | `composer.undo` / `composer.redo`, by edit group | always |
| Ctrl+R | `composer.history_search` — reverse history search | always |
| Ctrl+P | `composer.palette` — open the command palette; on a non-empty prompt, the previous prompt | always |
| Ctrl+X Ctrl+E (or Ctrl+X alone) | `composer.external_editor` — edit in `$VISUAL`/`$EDITOR` | always |
| Ctrl+L | `composer.redraw` — repaint the live region | always |
| ←→, ↑↓, Backspace | cursor, history, delete | ↑↓ history idle only |
| ↑ | `composer.queue_focus` — select a queued message | running, empty prompt, a message is queued |
| Shift+Tab | `composer.mode_cycle` | always |
| Ctrl+G, ← | `agents.open_hub` — open the agents hub over the conversation | ← only on an empty prompt |
| Ctrl+O | `subagents.focus` — move to the subagent panel | while the panel is shown |

Word motion is Unicode-aware: letters, digits and `_` form words, and path
separators stop, so Ctrl+W on `src/agenthicc/tui` deletes `tui`. Consecutive
kills join in the kill ring as in readline; consecutive typing is one undo
group (at most 200 groups are kept). Ctrl+Z replaces nothing: raw mode already
disables job-control suspend.

Shift+Enter and Ctrl+Enter are distinct keys only where the terminal reports
them. In kitty, WezTerm, Ghostty and foot (outside tmux/screen) the session
pushes the kitty keyboard protocol's "disambiguate" mode (`CSI > 1 u`) on
entering raw mode and pops it (`CSI < u`) on leaving — including around the
external editor — so `CSI 13;2u` arrives as Shift+Enter. Every CSI-u key decodes
to the key a legacy terminal sends for it (Ctrl+A stays Ctrl+A). The Windows
console reports Enter's modifiers directly. Elsewhere use Ctrl+J or Alt+Enter:
most terminals send Shift+Enter and Ctrl+Enter as a plain Enter, which sends
(or queues) the prompt. The footer therefore shows `Ctrl+J newline` and, while
a turn runs and the footer is wide enough, `Alt+Enter interject`.

**Pickers.** The `/`, `$` and `@` pickers have no text of their own: the
trigger is the text from the trigger character to the cursor. A picker handles
only navigation and choice (the `picker` context: ↑/↓, Ctrl+P/N, PageUp/PageDown,
Tab, Enter, Esc); every other key edits the prompt and the picker follows it.
Ctrl+U, Ctrl+W, Ctrl+A/E, ←/→, paste and undo therefore work with a picker
open; when the cursor leaves the trigger, or the trigger character is
deleted, the picker closes. In the `/` picker one Enter runs the highlighted
command when it takes no required argument (`/help`); a command with a
required `<argument>` or with argument completions (`/workflow`) is completed
with a space and the picker lists its arguments. Tab only completes. In the
`@` picker Enter inserts the path; a second Enter sends. Enter with no match
sends the prompt as typed.

**History search.** Ctrl+R shows a search row above the prompt
(`(search) git rebas▌   3 of 41 · Ctrl+R older · Enter use · Esc cancel`). It
matches earlier prompts by substring, case-insensitively, newest first, on any
line of a multi-line entry. Ctrl+R again is older, Ctrl+S newer; Enter or →
puts the match in the prompt to edit (it is not sent); Esc leaves the draft
exactly as it was.

**External editor.** Ctrl+X then Ctrl+E (or Ctrl+X alone, after 300 ms)
writes the prompt to a private temporary file (mode 0600) in the session's
directory under `~/.agenthicc/sessions/`, hands the terminal to `$VISUAL`,
`$EDITOR` or `vi`, and loads the file back with the cursor at the end. A
non-zero exit keeps the draft and says so.

**Queued messages.** A message sent while a turn runs shows above the prompt,
one muted row each (at most three, then `+N more`); an interjection is marked
with an arrow and `at next tool`. ↑ on an empty prompt selects the newest row
(the `queue` context): ↑/↓ move, Enter moves the message back into the prompt
to edit, Delete or Backspace removes it, Esc returns to the prompt. Messages
sent with Enter wait for the turn to end and then run in order; Ctrl+Enter or
Alt+Enter sends an interjection that is added to the running turn at its next
tool boundary. While a workflow runs, which can take hours, a message sent with
Enter is an interjection too: every workflow takes it at its next tool boundary,
and the row says `at next tool`. Commands (`/…`, `$…`) always wait for the turn
or workflow run to end.

← opens the hub only when the prompt is empty. If the prompt was just emptied
by Backspace, Ctrl+U or history recall, the first ← says "Press ← again to open
agents" instead, so a key held down to delete text does not open it by
accident. In a rebinding, `left_on_empty` names that gesture.

Every screen has a context: `composer`, `picker`, `search` and `queue` (the
composer's pickers, Ctrl+R search and queued rows), `agents` (operations on an agent),
`agents.list`, `agents.peek` and `agents.detail` (the hub's list, peek panel
and detail view), `transcript`, `subagents`, `overlay` and `dialog` (every
decision dialog: approvals, plan review and questions, see
[Decision dialogs](#decision-dialogs)); `global` actions apply everywhere.
`agents.list` and `agents.detail` inherit `agents`, and `agents.peek` inherits `agents.list`, so an agent operation such as
`agents.stop` works in all three. A key bound in a context shadows the same key
inherited from a parent, which is how `y` approves in the hub without
affecting anything else. The subagent panel (Ctrl+O) uses `subagents` and a
worker's transcript `transcript` — see [Subagents](subagents.md#the-subagent-panel).
`/keybindings` lists every context's actions.

Rebind them in `~/.agenthicc/keybindings.toml` (`AGENTHICC_KEYBINDINGS`
overrides the path). A value is a key, a list of keys, or `"none"`:

```toml
leader = "ctrl+t"          # optional: enables "<leader>a" two-step bindings
leader_timeout_ms = 2000

[composer]
"composer.newline" = ["alt+enter", "ctrl+j"]
"composer.clear" = "none"

[agents]
"agents.stop" = ["c", "ctrl+k"]

[agents.list]
"agents.search" = ["/", "ctrl+f"]
```

Edits apply at once in running sessions. An edit that names an unknown
action, binds two actions that can apply together to one key, or does not
parse is not applied: the defaults stay in effect and a notice says why.
A file that already binds a key one of the TUX-04 editing actions now uses by
default (Ctrl+K, Alt+Enter …) keeps its binding: the default action gives the
key up, and a notice and `/keybindings` say so.
`/keybindings` creates the file (every action listed, commented out) and shows
the effective bindings and where each comes from; `agenthicc keybindings
edit` opens the file in `$VISUAL`/`$EDITOR` from the shell.

The decoder knows the whole keyboard on POSIX and Windows: Ctrl+A–Z, Alt+key,
Shift/Alt/Ctrl with arrows, Home and End, Delete, Insert, F1–F12, focus
reports, and SGR mouse reports. A key nothing binds is ignored — before AGX-13,
Ctrl+letters, Alt combinations and unknown sequences arrived as Esc.

### Pickers and the command palette

One component, `tui.workspace.picker.Picker`, draws every list you choose from:
the `/`, `$` and `@` pickers, the Ctrl+P palette and the `/model`, `/mode` and
`/agent` pickers.

```text
 ❯ /co
   ─ Suggested ──────────────────────────────────────────────
 ▌ /compact        Summarise the conversation to free context
   /config         Open configuration editor
   ─ Session ────────────────────────────────────────────────
   /commands       List all registered commands with their source
                                                         1 of 3 · ↑↓ tab enter
```

- The selected row has an accent bar and background. The characters you typed
  are bold in the accent colour. A muted rule heads each group. A key binding
  is right-aligned (`/agents  ctrl+g`), and it is the key you have bound, not
  the default.
- Descriptions wrap with a hanging indent, two rows at most. Below 60 columns a
  description goes on its own row, and no row is wider than the terminal.
- A row that cannot run now is dimmed, with the reason under it
  (`not available while a turn runs`). The footer reads `N of M` and the keys.
- The list uses up to `min(12, rows / 3)` rows and never covers the composer.

**The `/` picker.** Matching is fuzzy over the name, the aliases and the
description. The order is: exact name or alias, name prefix, alias prefix, the
name's characters in order, then a description word (`/summar` finds
`/compact`). Ties go to the command you run more often, then more recently, in
this project; the counts live in `~/.agenthicc/state/command-usage.json`
(`AGENTHICC_COMMAND_USAGE` overrides the path) and hold only command names, counts and
times. With nothing typed the list is grouped: **Suggested** (your five most
used), **Session**, **Agents**, **Workflows**, **Configuration**, then
**Skills**, **Plugins** and **MCP**. An alias is not a row of its own: typing
`/bg` or `/detach` ranks `/background` first, and its row says
`(aliases /bg, /detach)`.

**The `@` picker.** A directory row starts with `▸` and shows its entry count; a
file row starts with `·` and shows its size; the path is muted and the file name
plain, with the matched characters bold. Files you mentioned in this session and
files `git status` reports as changed come first, and with nothing typed they
are the whole top of the list. Entries `.gitignore` ignores are left out unless
you type their exact name. `git status` runs on a worker thread when the session
starts and at most every two seconds after, never on the event loop, and only
the first 100 rows are measured, so typing stays smooth in a 10,000-file
project. A listing holds at most 200 rows: type to narrow it.

**The command palette.** Ctrl+P on an empty prompt opens a centred modal (60 %
of the width, between 60 and 100 columns) with a search field. It lists every
command, every key action that has a binding (`Open agents hub  ctrl+g`,
`Cycle mode  shift+tab`) and every theme, in the same groups as the `/` picker.
Enter runs the item: a command that needs an argument is put in the prompt for
you to finish, and one with argument completions (`/workflow`) lists them in the
same modal. Esc closes it. On a non-empty prompt Ctrl+P keeps its history
meaning.

**`/model`, `/mode`, `/agent`.** With no argument each opens a picker.

- `/model` lists the providers (each with its default model and the current
  one) and your saved `[providers.*]` profiles. A row shows the context window
  and whether the credentials resolve: `✓`, or `✗ set ANTHROPIC_API_KEY`. A
  row without credentials is dimmed and cannot be chosen until the variable is
  set. Choosing a row applies it **from the next turn**, without a restart: the
  session builds a new transport through the same factory it used at start-up,
  and the conversation shows `◆ model → gpt-4o (from next turn)`. If a turn is
  running the change waits for it to end. Press `s` after choosing to save it
  as the default (`[execution] provider` and `model`, or `profile`). `/model
  openai gpt-4o [--save]` applies directly. No credential value appears in the
  notice or in any event.
- `/mode` lists Safe, Plan, Yolo and plugin modes with a one-line summary.
- `/agent` lists the primary agents (AGX-09); `/theme` is described under
  [Themes and glyphs](#themes-and-glyphs).

### Themes and glyphs

Every colour on screen comes from a semantic token of the active theme
(`agenthicc.tui.theme`): `text`, `muted`, `subtle`, `frame`, `accent`,
`accent_bg`, `user_bg`, `success`, `warning`, `danger`, `info`, `agent`, the
diff tokens and the three mode badges. No other module names a colour, and
`tests/unit/test_theme_tokens.py` enforces that (TUX-01).

Tokens carry meaning, not hue: failures are `danger`; anything waiting for you
is `warning`; running work and progress are `info`; finished work is
`success`; subagent identity is `agent`; metadata is `muted`; the composer's
frame — its box or its two rules — is `frame`, the yellow of the welcome panel
(the light theme darkens it to stay readable on white), and so is the `❯` of the
prompt it frames; every other rule and border is `subtle`. The Safe, Plan and
Yolo badges (`✓ Safe`, `◆ Plan`,
`❯❯ Yolo`; `+`, `*` and `>>` with ASCII glyphs) use `mode_safe`, `mode_plan`
and `mode_yolo`. In list views the selected row is marked by an `accent` bar
(`▌`). In the agents hub (and its answer form), the subagent panel, `/history`,
`/help` and the command, skill, tool and workflow lists, the selected row also
sits on the `accent_bg` background; `/config` keeps reverse video for its
focused row. Renderers read the theme each time they draw, so a
`/theme` switch recolours them on the next frame.

```toml
[ui]
theme = "auto"    # auto | dark | light | high-contrast | ansi | <custom name>
glyphs = "auto"   # auto | unicode | ascii
```

- **`auto`.** At start-up the TUI asks the terminal for its background
  colour (OSC 11), followed by a device-attributes query (DA1) that every
  terminal answers. The query runs only on an interactive terminal, with
  echo off. It reads until the DA1 answer arrives, at most 500 ms, so a slow
  reply over SSH is never printed as `^[]11;rgb:…`. Without an answer it
  uses `COLORFGBG`, and then `dark`.
- **`ansi`.** Uses only the 16 terminal palette colours, so it follows your
  terminal's own scheme.
- **`high-contrast`.** Meets WCAG AAA (7:1) for body text.
- **`NO_COLOR`.** When set, it wins: every colour is removed, and bold, dim and
  reverse remain.
- **`glyphs = "auto"`.** Picks `ascii` only when the terminal output
  encoding is not UTF, the same test Rich uses for its box characters.
  `TERM` is not consulted, because many Unicode terminals report `linux`.
  `screen_reader = true` always uses `ascii`. The `unicode` set
  uses only characters found in DejaVu Sans Mono, Noto Sans Mono, Menlo,
  Consolas and Cascadia Mono, and never emoji.

`/theme` opens a picker that previews a user message, a tool row, a diff and
the status line in each theme. Enter applies the highlighted theme at once,
without a restart, and `s` saves it as `[ui] theme` in your user
configuration. `/theme light --save` does the same without the picker.

A custom theme is a TOML file in `.agenthicc/themes/<name>.toml` (project) or
`~/.agenthicc/themes/<name>.toml` (user). The project file wins. It sets any
subset of tokens over a built-in base:

```toml
extends = "dark"          # dark | light | high-contrast | ansi
syntax_theme = "monokai"  # any Pygments style, for code and diffs
[tokens]
accent = "#ff79c6"
user_bg = "#202233"
```

An unknown token or an invalid colour is reported with the file and line,
for example `themes/acme.toml:3: unknown token 'acent'`. The session then
starts with `auto`. `agenthicc config show` prints the effective theme and
glyph set.

### Accessibility and the mouse

```toml
[ui]
screen_reader = false   # no spinners or box drawing; state changes also printed as sentences
reduced_motion = false  # keep the layout, replace animation with a static ●
mouse = "auto"          # "auto": full-screen agents hub only; "on": also here; "off": never
```

With `screen_reader`, the status line uses commas instead of separators and
words instead of arrows and meters, the composer and dialogs have no frame,
nothing animates, and each change — the agent thinking, running a tool,
finishing or failing, a dialog waiting for a decision, a notification, a mode
switch — is printed once as a plain sentence in the scroll buffer. A dialog's
sentence is its title, summary, reason and option count, for example "Run a
command: npm test. Safe mode asks before running commands. 4 options." Between
turns the status line is announced too, only when it changes: "Idle. Opus 4.8.
Context 12 percent."
A transient status-line notice is announced too, with its severity as a word
("Done: Theme: dark"). Announcements are not written to the session's event log. With
`mouse = "on"`, the wheel moves the composer's cursor through a tall prompt;
`"off"` never writes a mouse-reporting sequence. `NO_COLOR` is respected.

ESC cancels the active turn and immediately returns the input pipeline to IDLE,
even while asynchronous task cleanup is still finishing. This keeps the
normal double-Ctrl+C exit sequence responsive after an interrupt on Windows.

Current triggers include:

- `/` — command picker backed by the unified command registry (see
  [Pickers and the command palette](#pickers-and-the-command-palette));
- `$` — skill-only picker backed by discovered skill records;
- `@` — project file/mention picker;
- Ctrl+P on an empty prompt — the command palette;
- trigger selection may update the input buffer or submit immediately.

The `@` picker accepts path-like fragments, including `./`, `../`, absolute
paths, `~`-prefixed paths, and platform-native `/` or `\\` separators. A
second `@` is a delimiter rather than part of the path, so accidental `@@`
input remains ordinary text. The same boundary rule is used when submitted
messages are parsed into file, directory, glob, URL, or unresolved mentions.
Submitted messages use a conservative candidate policy: an existing bare file
or directory is accepted, while an unresolved bare identifier such as
`@bookTicker`, `@depth20`, or `@100ms` remains ordinary text. Missing targets
must use explicit path evidence such as `@./missing.py`, `@docs/missing`, or a
glob. Mentions inside Markdown inline code and fenced code blocks are also
literal, so pasted protocol and source examples do not trigger reads.

Large bracketed pastes stay behind a single `[Pasted text #N ...]` composer
placeholder while the user edits the input. Ordinary typing, whitespace,
newlines, cursor movement, and history navigation do not expand the paste or
redraw its full contents. Use `Ctrl+V` when the full pasted text should be
shown. Backspace deletes typed suffix text one character at a time. When the
cursor is immediately after the pasted placeholder's closing `]`, Backspace
deletes the entire hidden paste; at other cursor positions it deletes one
hidden character at a time while keeping the placeholder condensed. Enter
submits the remaining original contents and edits. `Esc` also deletes the
entire hidden paste only when the cursor is immediately after its placeholder;
with a different cursor position, `Esc` keeps its normal mode behavior.
`Home` and `End` operate on the visible one-line projection rather than the
hidden payload's internal newlines. Typing after `Home` inserts before the
placeholder; typing after `End` inserts after it, without corrupting the
payload-to-placeholder range.

While the agent is responding, submitted input is classified before it enters
the session queue. Local read-only commands and run controls can execute
immediately; ordinary requests, skills, mutations, and workflow actions show
their queue position and wait in FIFO order. The slash picker labels the same
outcomes, so picker selection and manual typing cannot disagree. `/usage`
shows the current local token/cost snapshot without sending a message to the
agent. The status line compacts large token totals for scanning (for example,
`↑230M ↓1.5M`, with `≈` in front when the usage is partial) and shows them only
when the provider reported usage; `/usage` and `/status` remain the exact-value
views and are not affected by display rounding. Unavailable usage and an
unknown cost are absent from the status line, never shown as a known zero;
`/status` says "not reported by <provider>". With `screen_reader`, token counts
are spoken as exact integers ("about 230,044,280 tokens in, 1,506,692 out").
`/config` immediately opens the configuration
overlay, including during a response, while its edits remain local to the overlay
until saved. `/cancel`/`/interrupt` share the Ctrl+C cancellation owner, and
`/bg`/`/background` use the background-session control plane.

### Resumed transcript

When the TUI is opened with an existing session, it mounts the Live workspace
before reading transcript history. It then loads a strict-bounded recent page
and renders it in chronological order. The default page is limited to 20
complete turns, 128 events, 128 KiB of serialized event data, and 16 KiB per
event. `[behaviour] resume_transcript_turns = 0` removes only the turn limit;
the hard page limits still apply. See the
[configuration guide](configuration.md#resume-transcript-size) for tuning and
hard maxima.

The same renderers are used for old and live turns, tool completions, errors,
and assistant responses. Each event is drawn with its own stored time, never the
time of the resume, and the history opens with
`── replayed from 2 Nov 05:04 · 827 events ──`. `/replay
SESSION_ID` shows another session the same way. This is presentation-only replay: the events are not
appended to `ConversationStore` and therefore are not written to
`conversation.jsonl` a second time. Accepted user messages for input-history
navigation are projected from the same bounded page; the resumed input editor
does not scan the transcript a second time. Provider memory, usage, and
workflow state continue to restore through their existing durable stores.

The initial preview is bounded before rendering. Individual over-limit events
appear as markers, and malformed tail records are skipped. Use `/history` to
open a paginated overlay for older events: `[`/`PageUp` loads an older page,
`]`/`PageDown` returns toward newer pages, `Enter` opens one event's details,
and `Esc` returns to the live workspace. Historical pages are not appended to
live scrollback; newly arriving events retain their normal order. `--continue`,
`--resume SESSION_ID`, `agenthicc attach`, and the session-manager attach path
share this same TUI bootstrap.

Use `/startup` to inspect payload-free durations for session context, first
frame, transcript page read, input-history projection, transcript rendering,
interactive-input readiness, and the agent runtime that loads after the first
frame (provider-journal restoration and the provider itself).
For repeatable large-log measurements without provider credentials, run
`uv run python scripts/bench_resume_transcript.py --events 100000 --samples 5`;
use `--tool-event-every 1 --large-event-bytes 0` for an all-tool-event log.

When a tool approval, plan review, or `ask_user()` question is pending, the
status line changes from the animated Thinking state to a stable
`◆ Waiting for you`.
The Thinking duration is the total wall-clock time for the outer user activity:
it continues across internal LLM turns and workflow phases and ends only when
the activity returns to IDLE. Resize redraws do not derive a new timestamp
directly; while a prompt owns the terminal, the tick retains the activity
start point without publishing a timer redraw every 50 ms. The older per-turn
active-work clock remains available for turn-level telemetry and waiting-modal
behavior. When the activity returns to IDLE, the scroll buffer emits a compact
completion recap after the final turn. Each turn of a second or more reads
`✾ Worked for 1m 5s | 10:20 AM`, ending with the local time it finished.
The request's total, `✾ Done in 2m 5s across 3 turns | 10:22 AM`,
follows only when it adds something: after several turns, or when it is
longer than the one turn. The total spans the outer user activity across
internal turns and workflow phases; it is not model-only time or the sum of
the visible turn rows.
Durations use whole-second `s`, `m`, and `h` units. One blank row separates the
recap from subsequent scrollback or the persistent live status block.

The Windows backend uses `ReadConsoleInputW` so Shift+Tab preserves its
modifier. POSIX raw mode is a no-op for non-TTY file descriptors and restores
the previous terminal state on exit.

## Overlays and approvals

The workspace can show help, command and skill listings, configuration and
trigger-picker overlays, and the decision dialogs for tool approvals, plan
review and questions.
`/commands` and `/skills` build their listings from the live command/skill
registries and keep the results in the overlay rather than appending them to
the conversation scroll buffer. Approval requests are stored in reactive state
and route to the overlay registry in `TUISession`, which gives each dialog
the session's keymap, `screen_reader` setting, mode, composer context and
workspace scope (`DialogSettings`).

The `/ps` background-terminal overlay is a responsive paginated projection.
`Workspace` measures the Live layout and gives the active overlay its
available width and height through `OverlayHost`; the terminal overlay uses
that viewport to reserve its table header, bounded detail area, and footer.
It renders only the current page, preserves selection by terminal ID across
refreshes and resize, and uses `PageUp`/`PageDown`, `Home`, and `End` for
navigation. Overlay viewport delivery is an optional no-op hook on the base
`Overlay` contract so existing overlays remain compatible. The interactive
view is presentation-only: the terminal manager remains the source of truth,
and `/ps --json` still returns all owned records.

An overlay must not write directly to the terminal outside the workspace. It
should update its state/callback and let the workspace redraw. New approval
kinds need an overlay class, registry entry, and tests for approve/reject,
cancel, and terminal resize behaviour.

### Decision dialogs

Tool approvals, workspace-access approvals, plan review and `ask_user`
questions share one frame, `Dialog` (`tui/workspace/dialog.py`, TUX-07):

```text
 ╭─ Run a command ──────────────────────────────────────────────── Safe ─╮
 │                                                                       │
 │  in objective-create-a                                                │
 │  $ npm ci && npm test                                                 │
 │                                                                       │
 │  Safe mode asks before running commands.                              │
 │                                                                       │
 │ ▌ 1  Yes                                                              │
 │   2  Yes, and allow commands for the rest of this turn                │
 │   3  Yes, and allow commands this session                             │
 │   4  No, and tell the agent what to do instead                        │
 │                                                                       │
 ╰─ ↑↓ choose · Enter confirm · Esc no ──────────────────────────────────╯
```

- **Title and badge.** A verb phrase, never a tool's internal name; the badge
  is the mode, or the subagent worker that asks (`o` opens its transcript).
- **Body.** It explains the action: the whole command, syntax-highlighted and
  wrapped (a `cd X &&` prefix or a `cwd` argument reads "in X"); for
  `write_file`, `patch_file`, `apply_diff` and `batch_write`, a diff against
  the current file read inside the workspace, with `+N -M` per file, and a
  new file's first 40 lines marked "new file"; paths and sizes for delete,
  move and copy; the method, host and path of a network call and the
  `network_allow_list` entry that admits it (or that none does); the exact
  targets, workspace root and mode of a workspace-access approval (never its
  raw arguments); and a key/value table for MCP and plugin tools. A long
  body scrolls with `↑ N more` / `↓ N more`, and `Ctrl+T` shows the full
  arguments or diff, scrollable inside the dialog (`Ctrl+T` or Esc returns).
  Nothing is redacted more or less than before.
- **Reason.** One muted line from the mode, capability or workspace boundary
  that asked ("Safe mode asks before running commands.").
- **Options.** `1`–`9` choose and confirm, `↑↓` (or `j`/`k`) and Enter as
  well; `y`, `a`, `A` and `n` remain aliases for an approval (`n` denies at
  once). "No, and tell the agent what to do instead" opens the embedded
  composer in the dialog; what you type is returned to the model with the
  denial (the tool result's `error` and `user_instructions`).
- **Esc is the safe exit.** It denies an approval; folds the plan review to
  one line with the plan still pending (Enter reopens it); and steps back,
  then cancels, in questions.
- **Size.** The width is `clamp(cols − 4, 60, 120)`, centred above 124
  columns; the height is at most the overlay area, and a short body shrinks
  the dialog with no empty viewport.
- **Screen readers.** With `[ui] screen_reader = true` the dialog draws no
  box, and opening it announces one sentence, for example "Run a command: cd
  objective-create-a && npm ci && npm test. Safe mode asks before running
  commands. 4 options."

Keys come from the `dialog` keymap context, so `~/.agenthicc/keybindings.toml`
can rebind them (`[dialog] "dialog.details" = "ctrl+e"`), and `/shortcuts`
lists them. A `leader` key only shadows a plain binding of the same key in
the contexts that have two-step bindings, so a dialog keeps `Ctrl+T` when the
composer uses it as the leader.

**Plan review** shows the rendered plan in the body and offers "Approve and
run in Safe mode", "Approve and run in Yolo mode", "Keep planning" and
"Reject, with feedback" (a request without execution modes offers
"Approve"). Tab adds a note to any choice through the embedded composer.

### Reading long `ask_user` questions

The questions dialog is titled "The agent asks". With several questions the
body shows `Question 1 of 3`, and `←`/`→` move between them. A long question
scrolls in the body: `[` and `]` move one line, Page Up/Page Down a page.
There is no countdown: when a timeout is configured the reason line reads
"Answer within 5 min.".

When entering an `Other` answer, the input is the main composer embedded in
the overlay (`EmbeddedComposer`):

- paste (including condensed large pastes), Shift+Enter/`Ctrl+J` newline,
  the readline editing keys (word motion, kill and yank, undo/redo), Ctrl+R
  history search, Ctrl+X Ctrl+E (the editor takes the session's terminal)
  and history all work;
- `@` opens the mention picker, and mentions are resolved into the answer as
  they would be in a message;
- `/` opens the command and skill picker. A submitted command runs under the
  waiting turn's busy policy instead of becoming the answer: `/usage` runs
  at once, `/cancel` cancels the turn, and queued-only commands are refused
  with a notice. Text such as `/etc/hosts` that is not a command is an
  ordinary answer;
- Esc closes an open picker first, then returns to the options, keeping what
  was typed.

The pickers render inside the dialog, so the questions stay mounted.
Page Up/Page Down scroll the question by one page while `[` and `]` remain
ordinary answer characters. Question text is
rendered literally, so model text such as `[red]` is not interpreted as Rich
markup. Scrolling and resizing change presentation state only; the answer
payload and approval response remain unchanged.

## Slash commands

The canonical command definitions are in `commands/builtins.py`. Stateful
commands such as `/workflow` and `/compact` are intentionally intercepted by
`TUISession` because they need session fields. The legacy completion constants
and `CommandRegistry` in `tui/input/completions.py` are compatibility adapters
over the canonical registry; new commands must not be added there.

Every built-in command belongs to a group the `/` picker and the palette show:
`Session`, `Agents`, `Workflows`, `Configuration` or `MCP` (`Command.group`;
`commands.command.COMMAND_GROUPS` lists them with `Skills` and `Plugins`, the
default for a command that names none). `/background` carries the aliases
`/bg` and `/detach`, `/cancel` has `/interrupt`, and `/model` has `/models`: an
alias is a name for the same command, not a second row.

Skills use the `$skill-name` or `$alias` trigger and are deliberately excluded
from the `/` picker. `/skills` and `/skills reload` remain slash commands for
inspecting and refreshing skills. The removed `/skill-name` spelling is not
dispatched, even if a stale skill record is manually present in a registry.

## Testing UI changes

- Test conversation and signal mutations as unit tests.
- Test `ScrollBufferAppender._flush_batch()` with a fake or captured Rich
  console for new event renderers.
- Test input capabilities with synthetic `Key` values.
- Test workspace startup/shutdown and non-TTY input in integration tests.
- Inspect frames with `workspace._build_lines()` and
  `live_region.lines_text()`; feed a forced-terminal console's output to
  `pyte` to check what the terminal shows. Frames are capped to the frame
  interval, so wait with `tests.tui_frames.settle_frames()` before asserting on
  `LiveRegion.update` calls.
- Test terminal backends with pure key-decoding cases on Linux and actual
  interactive behaviour where the platform is available.

Avoid asserting against the old `render_frame_ansi` or `screen.buffer` contract;
those belong to the removed prompt-toolkit implementation.

## Try it

The TUI itself is interactive-only, so verify the pieces it composes from a
plain shell. The version line comes from the side-effect-free parser:

```bash
PYTHONPATH=src python -m agenthicc --version
```

```text
agenthicc 0.1.0
```

The slash-command registry the TUI completes and dispatches from is a plain
list literal, so its size is a stable fact:

```bash
PYTHONPATH=src python -c "
import ast, pathlib
tree = ast.parse(pathlib.Path('src/agenthicc/commands/builtins.py').read_text())
for node in ast.walk(tree):
    if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', None) == 'BUILTIN_COMMANDS':
        print('BUILTIN_COMMANDS entries:', len(node.value.elts))
"
```

```text
BUILTIN_COMMANDS entries: 23
```

That count is *not* the whole command surface: `/background` (alias `/bg`) is
injected separately by the background integration, and `/create-tools` and
`/create-commands` come from bootstrap skills.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A project slash command is missing right after launch | Extension discovery is deferred until after the first TUI frame | Wait for the shell phase to be `ready`, then `/commands reload` |
| `/workflow` or `/compact` appears in the picker but errors when run | Both are registry entries with `handler=None` on purpose | They are intercepted in `TUISession.route()` so they can reach session-local state; they are listed so the picker can display and complete them |
| A command the docs mention does not autocomplete | It is not in `BUILTIN_COMMANDS` and is not injected | Check the source of truth for the command: `BUILTIN_COMMANDS`, the background integration for `/background`, or bootstrap skills for `/create-*` |
| Output is garbled or the layout breaks after a resize | A stale frame from a partial render | Redraw, and report it — rendering is Rich Live based, so a broken layout is a bug rather than a terminal limitation |
| The UI looks frozen during a long turn | The animation frame is intentionally quiet while idle or while a prompt owns the terminal | Check `/status` and `/ps`; use `Esc` to cancel the awaited operation if the turn is genuinely stuck |
| `Esc` did not stop the thing you wanted | It cancels the terminal currently being awaited | Use `/stop <terminal-id>` to stop one exact owned process group |
| A modal overlay is dismissed accidentally | Approvals and overlays capture input | Answer the overlay deliberately; a grant now applies only to the scope you choose |
| Input pastes as multiple submissions | Bracketed paste handling | Use the paste state the input panel maintains rather than sending raw newlines |
| The first frame takes a long time on a slow network or big store | That is exactly what progressive startup prevents | The first frame should not wait on MCP, browsers, or history replay. If it does, inspect `/startup` for a phase that is loaded synchronously |
| Sessions from an older runtime generation do not appear | Two indexes exist | `sessions list` merges the historical project-local index with the current user-wide TUI index by design |
| "Diagnostics were written to the log" appears | A library wrote to stderr or raised a warning while the TUI was live | `/logs` shows it; run with `--debug` to see such output on the screen instead |

### Which `AppState` am I looking at?

There are two types with that name. The TUI's reactive container lives in
`tui/conversation_store.py` and holds conversation turns, scroll events,
token/cost/activity signals, input buffer and paste state, the active runtime
mode, overlay and approval state, and workflow progress. The frozen kernel
model (`agenthicc.kernel.AppState`) holds intents, workflows, tasks, agents,
tools, hooks, settings, and policy. Terminal-only state belongs in the
reactive container and must never be written to the event log.

```bash
PYTHONPATH=src python -c "import agenthicc.kernel as k; print(k.AppState.__module__)"
```

```text
agenthicc.kernel.state
```

If your traceback names a `tui` module for `AppState`, you are holding the
reactive store, not the kernel state.

### A feature is invisible after a restart

`TUISession` and workflow runners emit kernel events for durable domain changes
and update reactive signals for immediate presentation. If a change survived in
the session but not after restart, it was written to the reactive side.
Consolidating this boundary is tracked as PRD-138 P0.3, so state which model is
authoritative and how replay behaves before adding a field to either.
