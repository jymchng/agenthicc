# Sessions

Sessions are durable, resumable units of work, with conversation records you
can inspect, resume, and replay.

## The two CLI groups

| Group | Reads |
|---|---|
| `sessions` | Kernel and conversation artifacts |
| `session` | The client-neutral projection |

```bash
agenthicc sessions list              # list sessions
agenthicc sessions show <id>         # show one session
agenthicc sessions export <id>       # export a session
agenthicc sessions inspect <id>      # inspect internals
```

`sessions list` opens a paginated selector in a terminal and prints a
deterministic page when output is redirected. Use
`--page`/`--page-size` to control paging.

!!! note "Singular versus plural"
    New adapters should use the singular `session` group, which reads the
    client-neutral projection: `session create|list|show|export|events|send|control|serve`.
    Note that `session send` takes its text **positionally**
    (`session send SESSION_ID TEXT`) — there is no `--text` flag.

## Resume

```bash
agenthicc --continue            # most recent session in this directory
agenthicc --resume <session-id> # a specific session
```

On resume, the newest complete turns are replayed into the transcript
presentation-only — the events are not re-persisted. Set
`[behaviour] resume_transcript_turns = N` to change how many turns are replayed
(`0` means the full transcript).

## How sessions work

- Session state is a 13-value lifecycle (`SessionStatus`,
  `src/agenthicc/background/model.py:11`):
  `queued`, `starting`, `running`, `waiting_approval`, `waiting_input`,
  `retrying`, `cancelling`, `completed`, `failed`, `cancelled`, `orphaned`,
  `archived`, `deleted`. The `waiting_*` states are how a parked job is
  represented — see [Background sessions](11-background.md).
- Conversation history is durable JSONL under the session directory:
  `sessions/<session-id>/conversation.jsonl` for conversation events and
  `conversation-journal.jsonl` for the journal
  (`src/agenthicc/memory/journal.py:98`).
- Kernel events are appended to `.agenthicc/events.jsonl`
  (`src/agenthicc/kernel/state.py:138`).
- `sessions list` merges the historical project-local index with the current
  user-wide index (`src/agenthicc/sessions.py:60`), which is why sessions from
  both runtime generations appear.

!!! warning "Do not confuse the two conversation stores"
    `tui/conversation_store.py` is a **reactive** container for presentation
    state (scroll events, input buffer, overlays) and lives for the application
    lifetime. Its module docstring names an `agenthicc.conversation_store`
    module, but no such module exists in the tree — that reference is stale, and
    importing it raises `ModuleNotFoundError`. The durable record is the JSONL
    files above, not a SQLite-backed store.

## Next

- [Memory →](08-memory.md)
- [TUI →](04-tui.md)
- Depth: [Client-neutral sessions](../guides/session-service.md)
