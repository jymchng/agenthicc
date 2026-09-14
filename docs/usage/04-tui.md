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

Shift+Tab cycles **Safe → Plan → Yolo**. `/mode` switches directly. The aliases
`Auto` (Yolo), `Guard`/`Ask` (Safe), and `Review` (Plan) remain accepted;
`Debug` is rejected and `Replay` is internal-only. See [Modes](05-modes.md).

## Approvals

Tools that write, run commands, or touch the network require approval depending
on the active mode. Requests appear as an inline prompt, and while an approval,
plan review, or question is pending the status bar shows a stable waiting
label.

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

**Scroll buffer** — after a turn returns to IDLE the buffer prints a per-turn and
a cumulative line (`src/agenthicc/tui/workspace/appender.py:633,646`):

```text
✾ Worked for 1m 5s
✾ Total wall clock time since last IDLE: 2m 5s
```

**Status bar** — a permanently visible two-line block
(`StatusComponent`, `src/agenthicc/tui/workspace/components.py:111`):

```text
{flower} {state_animation} │ Runtime: mm:ss │ {active_tool}
{model_name} │ Tokens: Nk │ $N.NNNN
```

So the token and cost figures live on status-bar line 2 as
`model │ Tokens: Nk │ $N.NNNN` — a single total, not an in/out split. For the
detailed breakdown use the `/usage` command instead.

While a prompt owns the terminal the animation frame is intentionally quiet, so
a still frame is not a hang. Use `/status` to check the session.

## Background sessions

Move long-running work to the background with `/bg` (or `/background`), list
with `/bg list`, and re-attach with `/bg <n>`. The command-line equivalents
live under `agenthicc jobs` — see [Background sessions](11-background.md).

## Next

- [Modes →](05-modes.md)
- [Slash commands →](06-commands.md)
- Depth: [Terminal workspace](../guides/tui.md)
