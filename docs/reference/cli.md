# CLI reference

The entry point is `agenthicc.__main__:main`. Command discovery is decorator-
based and implemented in `cli/registry.py`.

## Global options

| Option | Meaning |
|---|---|
| `--headless` | Read stdin and emit JSON-lines |
| `--config PATH` | Select a configuration file |
| `--version` | Print the package CLI version string |
| `--continue` | Continue the latest session for the current directory |
| `--resume ID` | Resume a specific session |
| `--record-cassette [DIR]` | Record provider/approval interactions |
| `--set KEY=VALUE` | Override a config field; repeatable |
| `--dangerously-skip-permissions` | Disable session approval prompts; CLI-only escape hatch |

## Subcommands

| Command | Purpose |
|---|---|
| `config show` | Print effective configuration |
| `config init [--force]` | Create `.agenthicc/agenthicc.toml` |
| `sessions list` | List saved sessions for the current directory |
| `sessions show SESSION_ID` | Print stored event summaries |
| `trust cli` | Trust project-local `.agenthicc/cli/` plugins |
| `login` | Authenticate with agenthicc.ai |
| `logout` | Revoke stored credentials |
| `whoami` | Show current authentication state |

Run any command with `--help` for generated argument details.

## TUI slash commands

TUI commands are a separate registry from CLI subcommands. Current built-ins
include `/help`, `/commands`, `/status`, `/history`, `/mode`, `/workflow`,
`/model`, `/models`, `/skills`, `/mcp`, `/config`, `/compact`, `/replay`,
`/cancel`, `/clear`, and `/expand`.

`/workflow` and `/compact` are intercepted by `TUISession` because they need
session-local state. Both must remain visible in picker completion as well as
executable when submitted.
