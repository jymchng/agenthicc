# Using agenthicc — the complete user manual

This manual is written for the **end user** of agenthicc: someone who wants to
install it, point it at an LLM provider, and use it to get code written —
safely and predictably. Every command, flag, and mode has been verified
against the current source (`src/agenthicc/cli/parser.py`,
`src/agenthicc/config.py`, `src/agenthicc/commands/builtins.py`,
`src/agenthicc/tui/runtime/mode_manager.py`).

## What agenthicc is

agenthicc is a **state-driven agent operating system for autonomous software
engineering**. It runs agent turns inside your project with full filesystem,
git, and command tooling, and keeps durable session records so you can
inspect, resume, and replay work at any time.

```bash
agenthicc            # launch the interactive TUI
agenthicc --headless # run without the TUI; emit JSON-lines to stdout
```

## Contents

| Guide | What it covers |
|---|---|
| [Installation](01-installation.md) | Requirements, install with uv, provider setup |
| [Configuration](02-configuration.md) | agenthicc.toml, `--set`, provider profiles, secrets |
| [Your first task](03-first-task.md) | Running the TUI, headless mode, workflows |
| [The TUI](04-tui.md) | Interactive workspace, triggers, approvals, telemetry |
| [Modes](05-modes.md) | Safe → Plan → Yolo, aliases, `/mode` |
| [Slash commands](06-commands.md) | Every `/command` in the TUI |
| [Sessions](07-sessions.md) | Persistence, `--resume`, `--continue`, `session` CLI |
| [Memory](08-memory.md) | Session/project/global memory, journaling |
| [Tools](09-tools.md) | Filesystem, git, command, MCP tools |
| [Security](10-security.md) | Approval gates, modes, sandboxing |
| [Background sessions](11-background.md) | `/bg`, `jobs`, detached work |
| [Troubleshooting](12-troubleshooting.md) | Common issues and fixes |
| [FAQ](13-faq.md) | Frequently asked questions |

## Quick orientation

- Bare `agenthicc` opens the interactive Rich-Live TUI.
- `agenthicc --headless` reads stdin lines and emits JSON-lines.
- `agenthicc --workflow NAME` starts the TUI with a workflow selected, or
  runs NAME per stdin line in headless mode.
- `agenthicc --mode Safe|Plan|Yolo` starts with a mode selected.
- `agenthicc --continue` resumes the most recent session; `--resume <id>`
  resumes a specific session.
- `agenthicc init` creates an empty `AGENTS.md`, `.agenthicc/`, and a
  commented config template.
- CLI subcommands: `login`, `logout`, `whoami`, `config`, `session(s)`,
  `skills`, `mcp`, `trust`, `workflows`, `jobs`/`agents`, `doctor`.

All commands and flags in this manual were verified against the source.
