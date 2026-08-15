# Sessions

Sessions are durable, resumable units of work. agenthicc keeps conversation
records so you can inspect, resume, and replay at any time.

## CLI

```bash
agenthicc sessions list        # list sessions
agenthicc sessions show <id>   # show one session
agenthicc sessions export <id> # export a session
agenthicc sessions inspect <id># inspect internals
```

(Verified: `sessions list | show | export | inspect` in
`src/agenthicc/cli/commands/sessions.py`.)

## Resume from the CLI

```bash
agenthicc --continue            # most recent session in this directory
agenthicc --resume <session-id> # a specific session
```

On resume, the newest 20 complete turns are replayed into the transcript with
a `Loading transcript…` label (chunked so the TUI stays responsive). Set
`[behaviour] resume_transcript_turns = N` to change the bound (`0` = full).

## How sessions work

- Each session has a status lifecycle (Active → Completed / Failed /
  Cancelled).
- Conversations are stored durably (SQLite behind the conversation store)
  with event sourcing.
- Resume loads the prior conversation and replays it presentation-only — the
  events are not re-persisted.
- Session records support replay, export, and background execution.

## Background sessions

Use `agenthicc jobs list|status|cancel|resume|retry|approve|reject|input|
rename|labels|purge|archive|delete|restore` to manage detached background
sessions (see [Background sessions](11-background.md)).

## Next

- [Memory →](08-memory.md)
- [TUI →](04-tui.md)
