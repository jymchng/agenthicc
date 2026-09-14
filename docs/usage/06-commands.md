# Slash commands

The canonical registry is `BUILTIN_COMMANDS` in
`src/agenthicc/commands/builtins.py` and contains **23** entries:

| Command | Argument hint | Purpose |
|---|---|---|
| `/replay` | `[session-id]` | Replay a session |
| `/cancel` | — | Cancel the active turn (alias `/interrupt`) |
| `/clear` | — | Clear the transcript |
| `/commands` | `[reload]` | List commands |
| `/tools` | `[reload]` | List tools |
| `/workflows` | `[runs\|reload]` | List workflows or runs |
| `/config` | — | Open the configuration overlay |
| `/expand` | `[tool-id-or-@path]` | Expand a collapsed tool group |
| `/help` | `[/command]` | Show help |
| `/history` | — | Show history |
| `/init` | `[write] [--force]` | Create the AGENTS.md / config scaffold |
| `/mcp` | `[status\|reload\|connect NAME\|disconnect NAME\|refresh NAME\|doctor [NAME]]` | MCP status and control |
| `/model` | `[provider] [model]` | Switch model |
| `/models` | — | List models |
| `/skills` | `[reload]` | Inspect and reload skills |
| `/status` | — | Show session status |
| `/startup` | — | Print the bounded startup phase report |
| `/ps` | `[terminal-id] [--json]` | List processes (alias `/processes`) |
| `/stop` | `[terminal-id\|all] [--force]` | Stop a terminal (alias `/stop-terminal`) |
| `/usage` | — | Show the local token/cost snapshot |
| `/mode` | `[Safe\|Plan\|Yolo]` | Switch mode |
| `/workflow` | `<name> \| resume [run-id] \| reset [run-id]` | Select or resume a workflow |
| `/compact` | — | Compact the conversation |

!!! note "Commands registered outside `BUILTIN_COMMANDS`"
    Counting the registry under-reports the surface. `/background` (alias
    `/bg`) is injected by the background integration, and `/create-tools` and
    `/create-commands` come from bootstrap skills.

!!! note "`/workflow` and `/compact` carry `handler=None`"
    Both are intercepted in `TUISession.route()` so they can reach
    session-local state. They exist in the registry purely so the trigger
    picker can display and complete them, which is why invoking them by a
    non-TUI path does not work.

## Trigger pickers

- `/` — command picker
- `$` — skill-only picker
- `@` — project file/mention picker

## Local versus queued

`/usage`, `/config`, `/status`, and the run controls execute immediately even
while the agent is responding. Ordinary requests queue in FIFO order.

## Next

- [Sessions →](07-sessions.md)
- [Tools →](09-tools.md)
- Depth: [User-defined commands](../guides/commands.md)
