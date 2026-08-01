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
| `config profiles` | List configured provider profiles without secret values |
| `config validate` | Validate the selected provider profile and required secret references |
| `config init [--force]` | Create `.agenthicc/agenthicc.toml` |
| `sessions list` | List saved sessions for the current directory |
| `sessions show SESSION_ID` | Print stored event summaries |

Global configuration overrides include `--set KEY=VALUE` and the safer
`--set-secret KEY=ENV_VAR`. The latter stores a symbolic environment-variable
reference, rather than placing the secret value in the command arguments:

```bash
agenthicc --set-secret execution.default_headers.Modal-Key=MODAL_KEY config show
```
| `sessions inspect SESSION_ID [--json]` | Summarize durable state and resume health |
| `sessions export SESSION_ID [--output PATH]` | Write a redacted portable session export |
| `session list [--project-root PATH] [--json]` | List client-neutral session snapshots |
| `session show SESSION_ID [--json]` | Show one client-neutral session snapshot |
| `session events SESSION_ID [--after N]` | Replay the shared durable event projection |
| `session export SESSION_ID [--output PATH]` | Export a redacted snapshot and event projection |
| `session send SESSION_ID --text TEXT` | Queue a message through the shared command envelope |
| `session control SESSION_ID KIND [--payload JSON]` | Submit a typed control command |
| `session serve [--host HOST] [--port PORT] [--auth-token TOKEN]` | Serve local snapshots/events over HTTP/SSE |
| `workflows list [--json]` | List available workflow plugins and phase topology |
| `workflows run NAME --intent TEXT [--json]` | Execute one workflow headlessly |
| `skills add SOURCE [--project | --global] [--name NAME] [--skill NAME[,NAME]] [--all]` | Download and install validated skill(s) |
| `mcp add NAME URL [--project | --global]` | Register an MCP server in TOML configuration |
| `trust cli` | Trust project-local `.agenthicc/cli/` plugins |
| `login` | Authenticate with agenthicc.ai |
| `logout` | Revoke stored credentials |
| `whoami` | Show current authentication state |

Run any command with `--help` for generated argument details.

`agenthicc skills add SOURCE` installs into the current project's
`.agenthicc/skills/` by default. Use `--global` for `~/.agenthicc/skills/` or
`--project` to make the project target explicit. `SOURCE` may be a local skill
directory/file, a local repository, a direct HTTPS `SKILL.md` URL, a GitHub
repository URL (including `.git`), GitHub `owner/repo` shorthand, or a GitHub
`/tree/<revision>/<path>`/`/blob/<revision>/<path>/SKILL.md` link. Repository
sources discover all valid skills by default, while `--skill NAME[,NAME]`
selects specific skills and `--all` explicitly selects the full discovered set.
Use `--name` only when installing one skill and need to override its directory
name. Existing skills are never overwritten.

`agenthicc mcp add NAME URL` appends a validated `[[tools.mcp_servers]]` entry
to the project configuration by default. Use `--global` for the user config,
`--project` to make project scope explicit, and `--transport` for `stdio`,
`ws`, `websocket`, `streamable`, or `http`. Use `--token-env ENV_VAR` to store
an environment-variable reference such as `${MCP_TOKEN}`; the command never
accepts or prints a raw token. `--no-auto-connect`, `--reconnect-attempts`,
and `--reconnect-delay-seconds` configure the existing MCP bridge. The command
only updates configuration; it does not start or connect to the server. When
the stdio URL is an existing local directory (or `.py` server file), the CLI
stores a `uv run --project ... lmcp run ... --stdio` launcher so Lauren MCP
examples can be registered directly.

The singular `session` group is the canonical client-neutral surface. Its
commands read the service projection and event cursor rather than writing
kernel or conversation journals directly. `session serve` binds to loopback by
default, does not start an agent runner, and requires `--auth-token` before a
non-loopback bind is accepted.

## TUI slash commands

TUI commands are a separate registry from CLI subcommands. Current built-ins
include `/help`, `/commands`, `/tools [reload]`, `/workflows [reload]`, `/status`, `/history`,
`/mode`, `/workflow`, `/init`, `/model`, `/models`, `/skills [reload]`, `/mcp`,
`/config`, `/compact`, `/replay`, `/cancel`, `/clear`, and `/expand`.

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

To author a workflow interactively, submit
`/workflow create_workflow`, then enter the intent as the next ordinary input.
Its phases are `design → generate → validate → summarize`: the design is gated on
your approval, the generate phase writes a complete package directly to
`.agenthicc/workflows/<name>/runner.py` (with workflow-specific helpers in the
same directory), and the validate phase imports that package and
loops back to generate until it loads cleanly. There is no staging directory or
publish phase. Run `/workflows reload` after the run completes. `/workflow resume`
is not used for this direct-write authoring flow; run `/workflow create_workflow`
again if the run is interrupted.

Project tools and commands remain available through the `/create-tools` and
`/create-commands` skills and their respective reload commands; they are not
workflow selectors.

`/tools` and `/workflows` are registry overlays; adding `reload` rescans their
respective live registries without restarting. `/tools` shows the effective
built-in, project, and MCP tools for the session and labels each one
`builtin` or `plugin`. `/workflows` shows loaded
workflow sources, phases, mode bindings, and whether the plugin provides a
custom runner. Press Enter on a details page to place the selected command,
skill, or workflow invocation in the input panel without submitting it.

`/init` is a local project bootstrap command. It previews by default and uses
`/init write` or `/init write --force` for explicit writes; it does not invoke
the model or inspect arbitrary source files.
