# Terminal workspace

The current interactive UI is a Rich Live workspace. It is not the older
prompt-toolkit `build_app()`/`TranscriptModel` design referenced by historical
docs.

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

Tool results use the same operation-style block: `● Read(...)`, `● Run(...)`,
`● Search(...)`, and similar headers are followed by a status summary and a
bounded numbered output preview. File edits use the richer `● Update(...)`
block with its unified diff.

## Modes

The built-in mode cycle is Auto → Plan → Ask → Review → Safe → Debug. Modes
change system instructions, available workflows, tool filters, approvals, and
display metadata. Plan, Ask, Review, and Safe restrict actions; Debug exposes
diagnostic information.

Press Shift+Tab to cycle modes when the input backend is interactive. Workflow
availability is derived from the workflow registry. `/mode [name]` performs an
explicit switch.

## Input and triggers

`UnifiedInputSession` enters raw mode once and dispatches each key through the
active capability list. IDLE supports triggers, history, cursor movement,
paste, mode cycling, and submission. STREAMING reduces the capability set so
the user can queue input or interrupt an active turn.

Current triggers include:

- `/` — command picker backed by the unified command registry;
- `$` — skill-only picker backed by discovered skill records;
- `@` — project file/mention picker;
- trigger selection may update the input buffer or submit immediately.

While the agent is responding, submitted input is classified before it enters
the session queue. Local read-only commands and run controls can execute
immediately; ordinary requests, skills, mutations, and workflow actions show
their queue position and wait in FIFO order. The slash picker labels the same
outcomes, so picker selection and manual typing cannot disagree. `/usage`
shows the current local token/cost snapshot without sending a message to the
agent. `/cancel`/`/interrupt` share the Ctrl+C cancellation owner, and
`/bg`/`/background` use the background-session control plane.

The Windows backend uses `ReadConsoleInputW` so Shift+Tab preserves its
modifier. POSIX raw mode is a no-op for non-TTY file descriptors and restores
the previous terminal state on exit.

## Overlays and approvals

The workspace can show help, configuration, trigger-picker, plan-review,
questions, and generic tool approval overlays. Approval requests are stored in
reactive state and route to the overlay registry in `TUISession`.

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
