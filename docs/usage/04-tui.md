# The TUI

agenthicc's interactive UI is a **Rich Live workspace**: agent output, tool
calls, and approvals stream into a scroll buffer above a permanent live block
with the status bar, composer, and footer.

## Screen model

```text
terminal
├── scroll buffer
│   ├── agent text
│   ├── tool results and collapsed tool groups
│   └── workflow/system notifications
└── live block
    ├── status component (mode, tokens, cost)
    ├── composer or active overlay
    └── footer
```

## Input triggers

| Trigger | Picker |
|---|---|
| `/` | Command picker |
| `$` | Skill-only picker |
| `@` | Project file/mention picker |

## Modes

Shift+Tab cycles **Safe → Plan → Yolo** (see [Modes](05-modes.md)). `/mode`
switches directly. `Auto` (Yolo), `Guard`/`Ask` (Safe), and `Review` (Plan)
remain accepted aliases; `Debug` is rejected.

## Approvals

Tools that write, run commands, or touch the network require approval
depending on the active mode. Approval requests show an inline prompt; while
an approval, plan review, or question is pending, the status bar shows a
stable waiting label.

## Bracketed paste

Large pastes stay behind a `[Pasted text #N ...]` composer placeholder while
you edit: `Home`/`End` move within the visible projection, `Backspace` after
the closing `]` deletes the whole paste, `Ctrl+V` reveals the full text, and
`Esc` after the `]` discards it.

## Collapsed tool groups

Contiguous tool completions collapse into a group; the overflow count is
flushed to the scroll buffer as `...and N more tool calls` at the next
conversation boundary or on interrupt.

## Telemetry

After a turn returns to IDLE the scroll buffer prints:

```text
✾ Worked for 1m 5s
✾ Total wall clock time since last IDLE: 2m 5s
Tokens: 12.4k in / 3.1k out · Cost: $0.42
```

## Background sessions

Move long-running work to the background with `/bg` (or `/background`), list
with `/bg list`, and re-attach with `/bg <n>`. From the CLI, `agenthicc jobs
list|status|cancel|resume|retry|approve|reject|input|rename|labels|purge|
archive|delete|restore` manage detached sessions.

## Next

- [Modes →](05-modes.md)
- [Slash commands →](06-commands.md)
- [Background sessions →](11-background.md)
