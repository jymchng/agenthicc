# Background sessions

Long-running work can be detached from the foreground TUI and managed
independently.

## From the TUI

- `/bg` (or `/background`) — move the current task into the background
- `/bg list` — list running background sessions
- `/bg <n>` — re-attach and replay buffered output

## From the CLI

The `jobs` command manages background sessions (verified in
`src/agenthicc/cli/commands/background.py`):

```bash
agenthicc jobs list                # list background sessions
agenthicc jobs status <id>         # show one session
agenthicc jobs cancel <id>         # cancel a session
agenthicc jobs resume <id>         # resume a session
agenthicc jobs retry <id>          # retry a failed session
agenthicc jobs approve <id>        # approve a waiting session
agenthicc jobs reject <id>         # reject a waiting session
agenthicc jobs input <id>          # provide input to a waiting session
agenthicc jobs rename <id>         # rename a session
agenthicc jobs labels <id>         # set comma-separated labels
agenthicc jobs archive <id>        # archive a session
agenthicc jobs delete <id>         # move to recoverable trash
agenthicc jobs restore <id>        # restore a deleted session
agenthicc jobs purge               # permanently remove expired trash
```

`agents` (or `jobs`) opens the background sessions manager UI.

## Next

- [Sessions →](07-sessions.md)
- [Troubleshooting →](12-troubleshooting.md)
