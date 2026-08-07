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
| Workspace root | `tui/workspace/workspace.py` | Owns one Live block for the application lifetime |
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
└── permanent Rich Live block
    ├── blank separator
    ├── status component
    ├── composer or active overlay
    ├── border
    └── footer
```

`Workspace.start()` is called once, before the processor and input loop begin;
`Workspace.stop()` is called once during teardown. Starting/stopping Live per
turn causes cursor races and duplicated status lines.

Terminal resize signals are debounced into one repaint after the new geometry
settles. The workspace clears Rich's pre-resize live geometry first, so an
active Plan Review remains a single overlay instead of leaking duplicate
frames into scrollback.

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
agent. `/config` opens the configuration overlay immediately, including during
a response, while its edits remain local to the overlay until saved.
`/cancel`/`/interrupt` share the Ctrl+C cancellation owner, and
`/bg`/`/background` use the background-session control plane.

### Resumed transcript

When the TUI is opened with an existing session, the newest 20 complete turns
are loaded from the tail of the session log after the Live workspace mounts and
sent directly to `ScrollBufferAppender` in their original order. This bounded
projection prevents a very large historical log from delaying startup. Set
`[behaviour] resume_transcript_turns = N` to choose a different number; set it
to `0` to request the complete visual transcript. The setting does not delete
or trim provider memory, usage, workflow state, or the durable event log.

The same renderers are used for old and live turns, tool completions, errors,
and assistant responses. This is presentation-only replay: the events are not
appended to `ConversationStore` and therefore are not written to
`conversation.jsonl` a second time. Provider memory, usage, and workflow state
continue to restore through their existing durable stores.

While the transcript is being replayed, the status bar shows a
`Loading transcript…` label. Replay is chunked so the TUI remains responsive
until the scroll appender has finished loading the history; this applies
equally to `--continue` and `--resume SESSION_ID`.

When a tool approval, plan review, or `ask_user()` question is pending, the
status bar changes from the animated Thinking state to a stable waiting label.
The Thinking duration is the total wall-clock time for the outer user activity:
it continues across internal LLM turns and workflow phases and ends only when
the activity returns to IDLE. Resize redraws do not derive a new timestamp
directly; while a prompt owns the terminal, the tick retains the activity
start point without publishing a timer redraw every 50 ms. The older per-turn
active-work clock remains available for turn-level telemetry and waiting-modal
behavior. When the activity returns to IDLE, the scroll buffer
prints a final `✾ Total wall clock time since last IDLE: …` line after the per-turn
`✾ Worked for …` line; the latter is separated from following output by a blank
line.

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

An overlay must not write directly to the terminal outside the workspace. It
should update its state/callback and let the workspace redraw. New approval
kinds need an overlay class, registry entry, and tests for approve/reject,
cancel, and terminal resize behaviour.

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
- Test terminal backends with pure key-decoding cases on Linux and actual
  interactive behaviour where the platform is available.

Avoid asserting against the old `render_frame_ansi` or `screen.buffer` contract;
those belong to the removed prompt-toolkit implementation.
