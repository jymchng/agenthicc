# Storage reference

agenthicc uses several stores. They are not interchangeable and do not all
have the same replay guarantees.

## Session files

The default root is `~/.agenthicc/sessions/`.

| Path | Owner | Contents | Recovery |
|---|---|---|---|
| `<id>.jsonl` | kernel `EventProcessor` | Serialized domain events | `restore_from_log()` folds valid events |
| `<id>/metadata.json` | `tui.runtime.session_log` | cwd, model, timestamps | Session discovery/index |
| `<id>/conversation.jsonl` | `SessionEventLog` | Reactive conversation events | Replay renderer/metrics |
| `<id>/conversation-journal.jsonl` | `ConversationJournal` | Messages, resets, turn markers, tool records | Rebuild memory and resume interrupted turns |
| `<id>/cassette/` | testing/recording services | LLM and approval fixtures | Deterministic replay |

The session runner currently places the kernel log beside the session directory
and the conversation stores inside the directory. Keep these names distinct in
support tooling.

## Project and global stores

| Store | Default location | Data |
|---|---|---|
| Project memory | `.agenthicc/memory/project.db` | Namespaced KV and artifacts |
| Workspace file cache | `.agenthicc/cache/file-cache.db` | Freshness-validated file contents |
| Plugin trust | `.agenthicc/trusted_plugins.json` | Approved plugin hashes/decisions |
| Plugin audit | `.agenthicc/plugin_audit.jsonl` | Load/trust audit records |
| Global memory | `~/.agenthicc/global.db` | User-wide KV values |

Paths can be configured where the corresponding settings support it. Inspect
the current session context before assuming a custom path is active.

## Durability rules

- Kernel and conversation journal writes are append-oriented JSONL.
- The journal fsyncs transitions so an interrupted turn can be detected.
- A corrupt trailing JSONL line may be the signature of a crash during a write;
  readers currently tolerate it according to their fold policy.
- SQLite layers survive process restarts but need schema/version migration
  planning before format changes.
- Session memory and in-process semantic fallback are not durable by themselves.
- Cassettes can contain prompts, outputs, paths, and approval data; treat them
  as sensitive test artifacts.

## Safe cleanup

Use the CLI to inspect sessions before removing files:

```bash
uv run agenthicc sessions list
uv run agenthicc sessions show SESSION_ID
```

Never delete the entire home or workspace directory to clear a session. Remove
one identified session directory or use a future retention command once the
storage lifecycle work in PRD-138 P1.3 is implemented.
