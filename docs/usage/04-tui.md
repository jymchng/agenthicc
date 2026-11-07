# The TUI

agenthicc's interactive UI is a **Rich Live workspace**: agent output, tool
calls, and approvals stream into a scroll buffer above a permanent live block.

## Screen model

```text
terminal
├── scroll buffer
│   ├── agent text
│   ├── tool results and collapsed tool groups
│   └── workflow/system notifications
└── live block
    ├── composer frame (session title, prompt, mode badge) or an overlay
    ├── status line (state, attention, context, model, workers, usage, branch)
    └── one contextual hint row, only while it applies
```

`/status` prints the session ID, directory, branch, model, mode, usage,
context use and the session's file paths.

## Input triggers

| Trigger | Picker |
|---|---|
| `/` | Command picker |
| `$` | Skill-only picker |
| `@` | Project file/mention picker |

Submitted `@` text is parsed conservatively. Existing files and directories,
explicit paths such as `@./missing.py`, globs, and `http://`/`https://` URLs
remain supported. An unresolved bare identifier such as `@bookTicker`,
`@depth20`, or `@100ms` is ordinary text and does not create a failed read or
mention warning. Use `./`, a path separator, or the picker when a missing bare
filename should be treated as a mention. Mentions inside Markdown inline or
fenced code are treated as literal examples.

## Modes

Shift+Tab cycles **Safe → Plan → Yolo**. `/mode` switches directly. The aliases
`Auto` (Yolo), `Guard`/`Ask` (Safe), and `Review` (Plan) remain accepted;
`Debug` is rejected and `Replay` is internal-only. See [Modes](05-modes.md).

## Approvals

Tools that write, run commands, or touch the network require approval depending
on the active mode. Requests appear as an inline prompt, and while an approval,
plan review, or question is pending the status line shows a stable
`◆ Waiting for you`.

## Bracketed paste

Large pastes stay behind a `[Pasted text #N ...]` composer placeholder while
you edit:

| Key | Effect |
|---|---|
| `Home` / `End` | Move within the visible projection |
| `Backspace` after the closing `]` | Delete the whole paste |
| `Ctrl+V` | Reveal the full text |
| `Esc` after the `]` | Discard it |

## Collapsed tool groups

Contiguous tool completions collapse into a group; the overflow count is
flushed to the scroll buffer as `...and N more tool calls` at the next
conversation boundary or on interrupt.

## Telemetry

Two surfaces report time and usage, and they use different formats.

**Scroll buffer** — after each turn, and at the end of a request when it adds
something:

```text
✾ Worked for 1m 5s
✾ Done in 2m 5s across 2 turns
```

`Worked for` is the elapsed time of one turn. `Done in` spans the outer user
activity from its start until IDLE, including internal turns and workflow
phases; it is printed only after several turns, or when it is longer than the
one turn. Durations use whole seconds with compact `s`, `m`, and `h` units.
Rows under one second are omitted.

**Status line** — one row under the composer (`StatusLine`,
`src/agenthicc/tui/workspace/components.py`):

```text
 ❁ Thinking 12s · ◆ auto → Finalize goals 3/5 · ctx 19% ▰▱▱▱▱ · opus-4.8 · ↑12k ↓3k · $0.42
```

Tokens and cost appear only when the provider reported them; `/usage` gives the
detailed breakdown and `/status` the session's identity and files.

While a prompt owns the terminal the animation frame is intentionally quiet, so
a still frame is not a hang. Use `/status` to check the session.

## Background sessions

Move long-running work to the background with `/bg` (or `/background`); the
agents hub then opens with that agent selected. Open the hub at any time with
`/agents`, Ctrl+G, or ← on an empty prompt; `/inbox` opens its Needs you tab
and `/workers` its Running tab. Enter on an agent opens its detail view and
Enter there attaches to it; when that session ends you return to the one you
left. The command-line equivalents live under `agenthicc agents` — see
[Background sessions](11-background.md).

## Recurring session prompts

Use `/loop [interval] <prompt>` to schedule a local recurring instruction in
the current session. Use `/loops` to open the table of all persisted schedule
jobs. For example:

```text
/loop 10m Review the current workflow for unresolved failures.
/loop status
/loop pause
/loop resume
/loop stop
/loops
```

The scheduler waits for the TUI session to be idle before dispatching and sends
the prompt through the same conversation and workflow path as ordinary input.
It never starts a concurrent turn. In the `/loops` table, Enter makes the
selected job due immediately and `d` then Enter deletes it after confirmation.
See [Slash commands](06-commands.md) for validation, persistence, and restart
semantics.

## Next

- [Modes →](05-modes.md)
- [Slash commands →](06-commands.md)
- Depth: [Terminal workspace](../guides/tui.md)
