# Modes

Modes gate what the agent is allowed to do. The selectable cycle is
**Safe → Plan → Yolo** (verified in `src/agenthicc/tui/runtime/mode_manager.py`:
`SELECTABLE_MODE_NAMES = ("Safe", "Plan", "Yolo")`, `DEFAULT_MODE_NAME = "Safe"`).

## The cycle

| Mode | Meaning |
|---|---|
| **Safe** | Default. Read-only: read/search/git-read tools run directly; writes, git changes, commands, network require approval. |
| **Plan** | Read-only. Hard-blocks writes, git changes, commands, network; produces a plan. |
| **Yolo** | Full capabilities; tools run without per-action approval. |

## Aliases

| Alias | Canonical |
|---|---|
| `Auto` | `Yolo` |
| `Guard`, `Ask` | `Safe` |
| `Review` | `Plan` |

`Debug` is **not** an alias and is rejected. `Replay` is internal-only and not
selectable.

## Switching

```bash
Shift+Tab          # cycle Safe → Plan → Yolo
/mode Plan         # switch directly
/mode Auto         # alias → Yolo
/mode Debug        # rejected
/mode              # list modes
```

## Modes and approval

- **Safe**: read-only tools auto-run; mutations and commands need approval.
- **Plan**: mutation capabilities are hard-blocked (approval cannot override).
- **Yolo**: tools run without per-action approval.

## Next

- [Security →](10-security.md)
- [Slash commands →](06-commands.md)
