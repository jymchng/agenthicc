# Terminal workspace

The current interactive UI is a Rich Live workspace. It is not the older
prompt-toolkit `build_app()`/`TranscriptModel` design referenced by historical
docs.

The startup welcome panel keeps its `What's new` heading even when the remote
changelog cannot be reached. In that case it displays `No list`; on success it
renders the entries from `https://agenthicc.dev/changelog.json`.

Completed LLM responses end with a blank line in the scroll buffer, keeping
the response visually separated from the next tool, user message, or system
event.

## Runtime components

| Component | Location | Responsibility |
|---|---|---|
| Reactive state | `tui/conversation_store.py` | Conversation, input, metrics, mode, overlays, approvals, workflow progress |
| Workspace root | `tui/workspace/workspace.py` | Owns one live block for the application lifetime |
| Live region | `tui/workspace/live_region.py` | Writes the live block by line diff; repaints around scroll-buffer output |
| Frame scheduler | `tui/workspace/frames.py` | Merges redraw requests into capped frames; frame statistics |
| Scroll buffer | `tui/workspace/appender.py` | Prints conversation and tool events above the live block |
| Live components | `tui/workspace/components.py` | Status, composer, footer |
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
    ├── blank separator
    ├── status component
    ├── composer or active overlay
    ├── border
    └── footer
```

`Workspace.start()` is called once, before the processor and input loop begin;
`Workspace.stop()` is called once during teardown. Starting/stopping the block
per turn causes cursor races and duplicated status lines.

Terminal resize signals are debounced into one repaint after the new geometry
settles. That frame erases the block at its old height and paints every row
again, in one write, so an active Plan Review remains a single overlay instead
of leaking duplicate frames into scrollback.

### Rendering

Redraws cost what changed, not what is on screen (AGX-11):

- **Frames.** Signals request a frame instead of drawing. The scheduler draws
  at most one frame per `[behaviour] frame_interval_ms` (16 ms), merging every
  request made meanwhile. Typing, cursor moves and overlay keys draw on the next
  event-loop iteration. The spinner advances at 10 Hz, and only while a turn or
  compaction is running; an idle TUI draws no frames.
- **Rows.** Each frame is a list of rows rendered at exactly the terminal width,
  so the terminal never wraps one. Status and footer rows are built as markup
  and rendered once per distinct row and width, so a spinner frame renders one
  row and an unchanged footer parses no markup.
- **Composer.** Each logical line of the prompt is folded at the width and the
  fold is cached per line, so a keystroke in a 200-line prompt re-folds one line.
  At most `[behaviour] composer_max_rows` rows (12) are shown around the cursor;
  `↑ N more lines` / `↓ N more lines` stand for the rest.
- **Writes.** The live region keeps the rows it last wrote and rewrites only the
  rows that differ — a spinner frame is one row, about 50 bytes. A resize, a
  width change or a frame that changes more than half of the rows is a full
  redraw. Output printed while the block is live (the scroll buffer, or anything
  written to stdout/stderr) is printed above it and the block repainted below.
- **Synchronized output.** On POSIX terminals the first raw-mode entry asks
  (DECRQM) whether mode 2026 is supported; the key reader consumes the answer.
  If it is, every write is bracketed with `CSI ? 2026 h` … `CSI ? 2026 l` so the
  terminal presents it at once. Windows is not asked, because its console input
  would deliver the answer as typed characters.

`/perf` prints how many frames the session drew, render and write time
percentiles, characters per frame and whether synchronized output is on.

The shared animation frame advances only while an animated status is active:
thinking, tool execution, recovery, or compaction. Idle, complete, and error
states do not publish frame ticks, and the workspace defensively ignores
compatibility frame writes in those static states. This prevents captured
terminals that do not interpret Rich's erase controls from accumulating
identical `✿ Idle` panels while preserving active animation and ordinary state
redraws.

Tool results use the same operation-style block: `● Read(...)`, `● Run(...)`,
`● Search(...)`, and similar headers are followed by a status summary and a
bounded numbered output preview. File edits use the richer `● Update(...)`
block with its unified diff. Each contiguous change block shows at most six
changed rows; longer blocks retain their first and last three rows and show a
single `...` omission row so large edits do not flood the scroll buffer.
When a tool group is collapsed, its `...and N more tool calls` line is flushed
to the scroll buffer at the next conversation boundary and immediately when
the agent is interrupted; it is never left only in the live status footer.

Transient provider failures use a structured retry notice rather than dumping
the SDK exception representation into the transcript. The scroll buffer shows
the retry number and delay, a short category such as `Request timed out` or
`Provider temporarily unavailable`, and a bounded provider message when one is
available. Full chained exception details remain at debug log level.

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
the user can queue input or interrupt an active turn while retaining cursor
movement and basic editing.

### Key bindings

Keys are not hard-coded (AGX-13). Each key resolves, once per keystroke, to a
named action in the composer's context — `composer.submit`, `composer.newline`,
`composer.interrupt` … — and the footer's hints are the applicable actions'
labels from the same registry (`agenthicc.tui.actions`), so the footer can no
longer advertise a key that does nothing. An action can depend on the
composer's conditions (`running`, `paste`, `paste_at_cursor`): Esc deletes a
condensed paste when the cursor is right after it and pauses a running turn
otherwise; Ctrl+C cancels a running turn and starts the double-press exit when
idle.

| Key | Action | When |
|---|---|---|
| Enter | `composer.submit` — send (queued while a turn runs) | always |
| Ctrl+J | `composer.newline` | always |
| `/`, `@` | command picker, mention picker (fixed) | always |
| Ctrl+V / Esc | expand / delete a condensed paste | a paste is condensed (Esc: cursor right after it) |
| Esc | `composer.interrupt` — pause the turn | a turn is running |
| Ctrl+C | `composer.cancel` / `composer.quit` | running / idle (press twice) |
| Ctrl+D | `composer.eof` — send, or exit on an empty prompt | idle |
| Ctrl+U, ←→, Home/End, ↑↓, Backspace | clear, cursor, history, delete | ↑↓ idle only |
| Shift+Tab | `composer.mode_cycle` | always |
| Ctrl+G, ← | `agents.open_hub` — open the agents hub over the conversation | ← only on an empty prompt |
| Ctrl+O | `subagents.focus` — move to the subagent panel | while the panel is shown |

← opens the hub only when the prompt is empty. If the prompt was just emptied
by Backspace, Ctrl+U or history recall, the first ← says "Press ← again to open
agents" instead, so a key held down to delete text does not open it by
accident. In a rebinding, `left_on_empty` names that gesture.

Every screen has a context: `composer`, `agents` (operations on an agent),
`agents.list`, `agents.peek` and `agents.detail` (the hub's list, peek panel
and detail view), `transcript`, `subagents` and `overlay`; `global` actions
apply everywhere. `agents.list` and `agents.detail` inherit `agents`, and
`agents.peek` inherits `agents.list`, so an agent operation such as
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
parse is not applied: the defaults stay in effect and the footer says why.
`/keybindings` creates the file (every action listed, commented out) and shows
the effective bindings and where each comes from; `agenthicc keybindings
edit` opens the file in `$VISUAL`/`$EDITOR` from the shell.

The decoder knows the whole keyboard on POSIX and Windows: Ctrl+A–Z, Alt+key,
Shift/Alt/Ctrl with arrows, Home and End, Delete, Insert, F1–F12, focus
reports, and SGR mouse reports. A key nothing binds is ignored — before AGX-13,
Ctrl+letters, Alt combinations and unknown sequences arrived as Esc.

### Accessibility and the mouse

```toml
[ui]
screen_reader = false   # no spinners or box drawing; state changes also printed as sentences
reduced_motion = false  # keep the layout, replace animation with a static ●
mouse = "auto"          # "auto": full-screen agents hub only; "on": also here; "off": never
```

With `screen_reader`, the status bar and footer use commas instead of
box-drawing separators and words instead of arrows, the borders are dropped,
nothing animates, and each change — the agent thinking, running a tool,
finishing or failing, a prompt waiting for approval, a notification, a mode
switch — is printed once as a plain sentence in the scroll buffer.
Announcements are not written to the session's event log. With
`mouse = "on"`, the wheel moves the composer's cursor through a tall prompt;
`"off"` never writes a mouse-reporting sequence. `NO_COLOR` is respected.

ESC cancels the active turn and immediately returns the input pipeline to IDLE,
even while asynchronous task cleanup is still finishing. This keeps the
normal double-Ctrl+C exit sequence responsive after an interrupt on Windows.

Current triggers include:

- `/` — command picker backed by the unified command registry;
- `$` — skill-only picker backed by discovered skill records;
- `@` — project file/mention picker;
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
agent. The live status and one-time idle header compact large token totals for
scanning (for example, `↑ 230M ↓ 1.5M`); `/usage` remains the exact-value view
and is not affected by display rounding. A partial usage snapshot adds a compact
`?` to the token display and an explicit `usage partial` label on the metadata
row. Unavailable usage is written as `usage unavailable` on the secondary
metadata row, never as a known zero. Estimated cost uses `~$0.123` visually and
is spoken as `estimated cost $0.123`; `cost unknown` stays separate from token
quality. With `screen_reader`, input and output are announced as exact integer
counts with direction and units, followed by the quality wording. The status row
prioritizes state, elapsed time, active tool, pool progress, then usage; the model
may shorten to fit, while the full session ID and current directory occupy
separate rows with cell-aware wrapping and no added labels. Cross-session
attention counts appear on their own footer row immediately before the composer
shortcuts. `/config` opens the configuration overlay immediately, including during
a response, while its edits remain local to the overlay until saved. `/cancel`/
`/interrupt` share the Ctrl+C cancellation owner, and `/bg`/`/background` use the
background-session control plane.

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
and assistant responses. This is presentation-only replay: the events are not
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
status bar changes from the animated Thinking state to a stable waiting label.
The Thinking duration is the total wall-clock time for the outer user activity:
it continues across internal LLM turns and workflow phases and ends only when
the activity returns to IDLE. Resize redraws do not derive a new timestamp
directly; while a prompt owns the terminal, the tick retains the activity
start point without publishing a timer redraw every 50 ms. The older per-turn
active-work clock remains available for turn-level telemetry and waiting-modal
behavior. When the activity returns to IDLE, the scroll buffer emits a compact
completion recap after the final turn. It shows each eligible per-turn duration
(for example, `✾ Turn · 1m 5s`) and an indented `Total · 2m 5s` row for the
enclosing activity. The total spans the outer user activity across internal turns
and workflow phases; it is not model-only time or the sum of the visible turn rows.
Durations use whole-second `s`, `m`, and `h` units, and the recap ends with one
blank separator before subsequent output.

The Windows backend uses `ReadConsoleInputW` so Shift+Tab preserves its
modifier. POSIX raw mode is a no-op for non-TTY file descriptors and restores
the previous terminal state on exit.

## Overlays and approvals

The workspace can show help, command and skill listings, configuration,
trigger-picker, plan-review, questions, and generic tool approval overlays.
`/commands` and `/skills` build their listings from the live command/skill
registries and keep the results in the overlay rather than appending them to
the conversation scroll buffer. Approval requests are stored in reactive state
and route to the overlay registry in `TUISession`.

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

### Reading long `ask_user` questions

The Questions overlay wraps long or multi-line question text to the current
terminal width and keeps it in a fixed-height viewport. In SELECTING mode,
press `[` to scroll up one question line and `]` to scroll down one line;
`↑`/`↓` remain option navigation and `←`/`→` remain question navigation. A
range indicator shows when more question text is above or below the viewport.

When entering an `Other` answer, Page Up/Page Down scroll the question by one
viewport while `[` and `]` remain ordinary answer characters. Question text is
rendered literally, so model text such as `[red]` is not interpreted as Rich
markup. Scrolling and resizing change presentation state only; the answer
payload and approval response remain unchanged.

## Slash commands

The canonical command definitions are in `commands/builtins.py`. Stateful
commands such as `/workflow` and `/compact` are intentionally intercepted by
`TUISession` because they need session fields. The legacy completion constants
and `CommandRegistry` in `tui/input/completions.py` are compatibility adapters
over the canonical registry; new commands must not be added there.

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
