# Using agenthicc — the complete user manual

This manual is the **task-oriented path**: install it, point it at a provider,
run your first task, then learn the surfaces you will actually touch. Each
chapter ends with a link into the deeper `guides/` reference for that subsystem.

Every command and flag here was checked against the current source
(`src/agenthicc/cli/parser.py`, `src/agenthicc/cli/registry.py`,
`src/agenthicc/config.py`, `src/agenthicc/commands/builtins.py`,
`src/agenthicc/tui/runtime/mode_manager.py`).

## What agenthicc is

A **state-driven agent operating system for autonomous software engineering**.
It runs agent turns inside your project with filesystem, git, and command
tooling, and keeps durable session records so you can inspect, resume, and
replay work at any time.

```bash
agenthicc              # launch the interactive TUI
agenthicc --headless   # run without the TUI; emit JSON-lines to stdout
```

## Read this first

```bash
agenthicc --version          # confirm the install
agenthicc --help             # global flags and command groups
agenthicc config validate    # confirm your configuration parses
```

Each of those is read-only and answers without opening a session or contacting
the network.

## Contents

| Chapter | What it covers | Depth |
|---|---|---|
| [Installation](01-installation.md) | Requirements, install with uv, provider setup | [Configuration guide](../guides/configuration.md) |
| [Configuration](02-configuration.md) | Config precedence, `--set`, profiles, secrets | [Configuration guide](../guides/configuration.md) |
| [Your first task](03-first-task.md) | Running the TUI, headless mode, workflows | [Quickstart](../guides/quickstart.md) |
| [The TUI](04-tui.md) | Workspace layout, triggers, telemetry | [Terminal workspace](../guides/tui.md) |
| [Modes](05-modes.md) | Safe → Plan → Yolo, aliases, `/mode` | [Security model](../guides/security.md) |
| [Slash commands](06-commands.md) | Every `/command` in the TUI | [User-defined commands](../guides/commands.md) |
| [Sessions](07-sessions.md) | Persistence, `--resume`, `--continue`, `session` CLI | [Client-neutral sessions](../guides/session-service.md) |
| [Memory](08-memory.md) | Session/project/global memory, journaling | [Memory guide](../guides/memory.md) |
| [Tools](09-tools.md) | Filesystem, git, command, MCP tools | [User-defined tools](../guides/tools.md) |
| [Security](10-security.md) | Capabilities, boundaries, approval | [Security model](../guides/security.md) |
| [Background sessions](11-background.md) | `/bg`, `jobs`, detached work | [Background sessions](../guides/background-sessions.md) |
| [Troubleshooting](12-troubleshooting.md) | Common issues and fixes | [Fact base](../reference/fact-base.md) |
| [FAQ](13-faq.md) | Frequently asked questions | [Architecture](../guides/architecture.md) |

## Quick orientation

Bare `agenthicc` opens the interactive Rich-Live TUI.

| Flag | Effect |
|---|---|
| `--headless` | Read stdin lines, emit JSON-lines |
| `--workflow NAME` | Start the TUI with NAME selected, or run NAME per stdin line |
| `--mode Safe\|Plan\|Yolo` | Start with a mode selected |
| `--config PATH` | Use an explicit config file |
| `--continue` | Resume the most recent session for this directory |
| `--resume ID` | Resume a specific session |
| `--set KEY=VALUE` | Override a config key (repeatable) |
| `--set-secret KEY=ENV_VAR` | Read a secret from an environment variable |
| `--record-cassette [DIR]` | Record LLM calls and approvals for replay |
| `--dangerously-skip-permissions` | Auto-approve capability prompts for this session |

Command groups (not a flat list): `config`, `mcp`, `session`, `sessions`,
`skills`, `trust`, `workflows`, `jobs`, `agents`, `run`, `init`, `login`,
`logout`, `whoami`.

!!! note "`agenthicc doctor` does not exist"
    Diagnostics are subcommand-scoped. Use `agenthicc mcp doctor [NAME]` for
    MCP connectivity, and `agenthicc config validate` for configuration.

## Next

[Installation →](01-installation.md)
