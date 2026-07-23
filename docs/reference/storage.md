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
uv run agenthicc sessions inspect SESSION_ID
uv run agenthicc sessions export SESSION_ID --output session-export.json
```

`sessions inspect` reads the durable artifacts without printing conversation or
tool payloads. It reports available files, valid and corrupt record counts,
conversation/tool/token totals, workflow completion, and whether the journal
contains an incomplete turn that can be resumed. Add `--json` for automation.

`sessions export` writes one versioned JSON document containing the kernel
events, session metadata, conversation events, durable conversation journal,
and any cassette records for the selected session. Credential-shaped fields
and common API-key, bearer-token, and provider-token strings are replaced with
`<redacted>`. Corrupt JSONL lines are omitted and counted in the export
manifest, so a crash-damaged trailing record does not prevent support export.
The destination is written atomically and existing destination files are
replaced.

Exports are portable support artifacts, but inspect them before sharing: user
prompts, tool results, file paths, and model output can still contain sensitive
project information that cannot be identified reliably by generic redaction.

Never delete the entire home or workspace directory to clear a session. Remove
one identified session directory or use a future retention command once the
storage lifecycle work in PRD-138 P1.3 is implemented.

## Background-session registry

Background execution adds a local registry at `~/.agenthicc/background/`:

| Path | Owner | Contents | Recovery |
|---|---|---|---|
| `events.jsonl` | `background.BackgroundStore` | Ordered create/update/delete lifecycle events | Replayed on every read |
| `registry.lock` | `BackgroundStore` | Cross-process advisory lock | Recreated automatically |
| `requests/<id>.json` | `BackgroundSupervisor` | Mode-600 worker launch request | Read once by the owned worker |
| `trash/<id>-<nonce>/` | `BackgroundStore` | Exact deleted artifacts and manifest | `agenthicc jobs restore <id>` |

The background registry is a rebuildable index, not a second conversation or
workflow journal. Session artifacts remain under `~/.agenthicc/sessions/<id>/`
and are consumed by the existing session/kernel persistence code. Events are
written with append and fsync semantics; malformed trailing records are
ignored, while a deletion tombstone prevents an old worker from resurrecting a
deleted session.

Workers claim a lease before execution and heartbeat while active. A missing
worker or expired heartbeat is shown as `orphaned`; the default restart policy
does not relaunch it. Resume and retry are explicit operations. Background
deletion first cancels live work and moves only the resolved session directory
and its matching kernel journal into recoverable trash. It never recursively
targets the project root.
