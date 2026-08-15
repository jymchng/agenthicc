# Slash commands

The canonical command definitions live in `src/agenthicc/commands/builtins.py`
(verified).

## Command reference

| Command | Purpose |
|---|---|
| `/help` | Show help |
| `/clear` | Clear the transcript |
| `/compact` | Compact the conversation |
| `/config` | Open the configuration overlay |
| `/mode` | Switch mode (Safe → Plan → Yolo) |
| `/model` / `/models` | Switch / list models |
| `/workflow` / `/workflows` | Select / list workflows |
| `/skills` | Inspect and reload skills |
| `/tools` | List tools |
| `/commands` | List commands |
| `/status` | Show session status |
| `/usage` | Show local token/cost snapshot |
| `/history` | Show history |
| `/replay` | Replay a turn |
| `/cancel` / `/interrupt` | Cancel / interrupt the active turn |
| `/stop` / `/stop-terminal` | Stop the run / terminal |
| `/processes` / `/ps` | List processes |
| `/expand` | Expand a collapsed tool group |
| `/mcp` | MCP server status / reload |
| `/init` | Create AGENTS.md / config scaffold |

## Trigger pickers

- `/` — command picker
- `$` — skill-only picker
- `@` — project file/mention picker

## Local vs agent commands

Local read-only commands (`/usage`, `/config`) and run controls execute
immediately while the agent is responding; ordinary requests queue in FIFO
order.

## Next

- [Sessions →](07-sessions.md)
- [Tools →](09-tools.md)
