# CLI reference

The entry point is `agenthicc.__main__:main`. Command discovery is decorator-
based and implemented in `cli/registry.py`.

## Global options

| Option | Meaning |
|---|---|
| `--headless` | Read stdin and emit JSON-lines |
| `--workflow NAME` | Run NAME for each non-empty stdin line in headless mode |
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
| `init [--write] [--force]` | Preview or explicitly write project guidance to `AGENTS.md` |
| `config show` | Print effective configuration |
| `config init [--force]` | Create `.agenthicc/agenthicc.toml` |
| `sessions list` | List saved sessions for the current directory |
| `sessions show SESSION_ID` | Print stored event summaries |
| `sessions inspect SESSION_ID [--json]` | Summarize durable state and resume health |
| `sessions export SESSION_ID [--output PATH]` | Write a redacted portable session export |
| `workflows list [--json]` | List available workflow plugins and phase topology |
| `workflows run NAME --intent TEXT [--json]` | Execute one workflow headlessly |
| `trust cli` | Trust project-local `.agenthicc/cli/` plugins |
| `login` | Authenticate with agenthicc.ai |
| `logout` | Revoke stored credentials |
| `whoami` | Show current authentication state |

Run any command with `--help` for generated argument details.

## TUI slash commands

TUI commands are a separate registry from CLI subcommands. Current built-ins
include `/help`, `/commands`, `/status`, `/history`, `/mode`, `/workflow`, `/init`,
`/model`, `/models`, `/skills [reload]`, `/mcp`, `/config`, `/compact`, `/replay`,
`/cancel`, `/clear`, and `/expand`.

Default project-authoring skills also provide `/create-tools <instructions>`
and `/create-commands <instructions>`. They send the supplied instructions to
the lauren-ai agent with repository-specific implementation, testing, and
security guidance; generated Python remains executable project code and must
be reviewed. See the [user-defined commands guide](../guides/commands.md) and
[user-defined tools guide](../guides/tools.md) for the current trust and
capability boundaries.

`/workflow` and `/compact` are intercepted by `TUISession` because they need
session-local state. Both must remain visible in picker completion as well as
executable when submitted.

`/init` is a local project bootstrap command. It previews by default and uses
`/init write` or `/init write --force` for explicit writes; it does not invoke
the model or inspect arbitrary source files.
