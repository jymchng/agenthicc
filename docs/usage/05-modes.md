# Modes

Modes gate what the agent is allowed to do. The selectable cycle is
**Safe → Plan → Yolo** (`SELECTABLE_MODE_NAMES = ("Safe", "Plan", "Yolo")`,
`DEFAULT_MODE_NAME = "Safe"` in `src/agenthicc/tui/runtime/mode_manager.py`).

## The cycle

| Mode | Meaning |
|---|---|
| **Safe** | Default. Read/search/git-read tools run directly; writes, git changes, commands, and network require approval. |
| **Plan** | Read-only. Hard-blocks writes, git changes, commands, and network; produces a plan. |
| **Yolo** | Full capabilities; tools run without per-action approval. |

The restricted set that Safe prompts for and Plan blocks is
`WRITE`, `GIT_WRITE`, `EXECUTE`, `NETWORK`, and `UNDECLARED`.

!!! warning "`UNDECLARED` is not a safe default"
    A tool that declares no capability metadata lands in `UNDECLARED`, which
    prompts in Safe and is blocked in Plan. See [Security](10-security.md).

## Aliases

| Alias | Canonical |
|---|---|
| `Auto` | `Yolo` |
| `Guard`, `Ask` | `Safe` |
| `Review` | `Plan` |

`Debug` is **not** an alias and is rejected. `Replay` is internal-only and not
selectable. Aliases resolve at the registry boundary, so they never appear as
separate entries in the mode cycle.

## Switching

```text
Shift+Tab          # cycle Safe → Plan → Yolo
/mode Plan         # switch directly
/mode Auto         # alias → Yolo
/mode Debug        # rejected
/mode              # list modes
```

`ModeRegistry.resolve` raises `UnknownModeError` for a name that is neither
canonical nor a known alias; the selection helper used by `/mode` and `--mode`
accepts a canonical name or an alias and returns `None` for unknown input or
for an internal mode rather than raising. Resolution is case-insensitive.

## Approval behaviour

- **Safe** — read-only tools auto-run; mutations, commands, and network prompt.
- **Plan** — mutation capabilities are hard-blocked; approval cannot override.
- **Yolo** — no per-action approval prompt.

Blocked capabilities are read live on every tool call, so a mode change applies
from the next call even inside one turn.

## Next

- [Security →](10-security.md)
- [Slash commands →](06-commands.md)
- Depth: [Security model](../guides/security.md) · [Fact base](../reference/fact-base.md)
