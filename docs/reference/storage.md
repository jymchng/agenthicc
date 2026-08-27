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
| `<id>/conversation-journal.jsonl` | `ConversationJournal` / `UsageLedger` | Messages, resets, turn markers, tool records, subagent worker/pool results, and versioned usage records | Rebuild memory, restore usage, resume interrupted turns, and recover complete subagent results |
| `<id>/.owner` | `SessionOwnerLease` | One live process owner for the whole durable session | Atomic claim/release; stale recovery only when process death is proven |
| `<id>/.owner.lock` | `SessionOwnerLease` | Short per-session critical section for owner publication, stale replacement, and release | OS advisory lock; never held for the session lifetime |
| `<id>/workflows/<run>/checkpoint.json` | `WorkflowCheckpointStore` | Versioned workflow context, phase/branch cursor, plugin fingerprint, journal cursor, and non-secret provider/profile/workspace identity | Rehydrate an explicitly acknowledged paused or interrupted workflow |
| `<id>/workflows/<run>/recovery-error.json` | `WorkflowCheckpointStore` | Bounded, redacted diagnostic for setup, context, or checkpoint-storage failures that cannot produce a typed checkpoint | Display diagnosis after restart; never resumable by itself |
| `<id>/workflows/<run>/.claim` | `WorkflowCheckpointStore` | Atomic live-owner lease metadata (PID/host/owner/process-start identity only) | Prevent duplicate resume; reclaim only provably dead local claims |
| `~/.agenthicc/session-service/index.json` | `SessionEventStore` | Bounded redacted metadata projection: id, project root, lifecycle, timestamps, sequence, capabilities, and file fingerprint | Atomic rebuild from authoritative service JSONL files; never used as event truth |
| `~/.agenthicc/session-service/index.lock` | `SessionEventStore` | Short cross-process index merge lock | OS advisory lock; never held for session or turn lifetime |
| `index.lock` | `SessionOpenCoordinator` | Short session-index read/modify/write critical section | OS advisory lock; never held for a turn or TUI lifetime |
| `<id>/cassette/` | testing/recording services | LLM and approval fixtures | Deterministic replay |

The session runner currently places the kernel log beside the session directory
and the conversation stores inside the directory. Keep these names distinct in
support tooling.

## Session ownership and resume races

`<id>/.owner` is the authority for whether a process is attached to a durable
session. It contains only a versioned schema, session ID, opaque owner token,
PID, host, process-start token when available, acquisition time, and entrypoint;
it never contains prompts, transcript text, tool arguments, credentials, or
full command lines. The record is written to a private fsynced temporary file
and atomically published with restrictive permissions.

Every durable attach path acquires this lease before opening the session
service, restoring the kernel or conversation journal, touching `last_active`,
constructing providers/tools/workflows, or replaying the TUI transcript. This
includes `--continue`, `--resume SESSION_ID`, sessions-list Enter, headless
workflow execution, and background jobs that explicitly target a session.
Subagents, phases, tools, and workflow runners inherit the parent lease. The
workflow run's `<run-id>/.claim` remains a nested, separate guard for one
workflow's resumable state.

`--continue` selects and claims the newest matching session while holding the
short-lived `index.lock`. If that selected session is already owned, the
command reports `session_already_active` and does not fall back to an older
session or create a fresh one. The conflict includes only bounded PID, host,
entrypoint, and age diagnostics. The stable conflict exit status is `3`.

An owner left by a crash is reclaimable only when the local PID is absent, a
known zombie, or its process-start identity no longer matches. Unknown hosts,
malformed records, permission failures, unsupported identity checks, and
ambiguous OS errors fail closed. Release is idempotent and compares the opaque
owner token before removing `.owner`, so late cleanup cannot delete a newer
owner. A forced unlock is intentionally not provided.

Conversation-memory journals also contain bounded tool-exchange lifecycle
records. `tool_exchange_started`, `tool_exchange_result_recorded`,
`tool_exchange_committed`, and `tool_exchange_aborted` describe transaction
state without storing tool arguments or output in diagnostics; call IDs are
stored as short hashes. The canonical message append/reset records remain the
source used to rebuild memory. If a crash leaves an assistant tool batch with
missing results, `JournaledShortTermMemory` inserts deterministic interruption
results, writes one durable reset, and then the shared validator must pass
before provider I/O. A known cancellation/queued-continuation race is also
repaired by moving its matching late result back beside the assistant call and
writing one durable reset. Invalid unknown, duplicate, empty, or ambiguous
non-adjacent exchanges fail closed rather than being silently rewritten.

### Subagent result records

Subagent workers use isolated, ephemeral `ShortTermMemory` instances. Their
provider messages are not folded into the parent conversation. The durable
result boundary is instead recorded in the same
`conversation-journal.jsonl`:

- `subagent_worker_result` is written after each worker finishes and contains
  its complete final text, status, error, tool names, changed-path hints, and
  pool/task identity;
- `subagent_pool_result` is written only after every worker succeeds and
  contains the complete labelled aggregate plus its task fingerprint;
- `fold()` ignores both record kinds because they are not provider messages;
  `fold_subagent_worker_results()` and `fold_subagent_pool_results()` project
  them for diagnostics and complete-pool resume;
- each record is flushed and fsync'd before the worker/pool completion reaches
  the parent. This closes the interval in which a parent cancellation could
  otherwise lose output before lauren-ai committed the parent tool result.

The full result is intentionally stored here. TUI scroll events and kernel
events expose only bounded previews, so a short `subagent_worker_done` line is
not evidence that the full worker output was discarded. Session exports and
journal files may contain user-provided prose, source code, paths, or secrets
returned by a worker and must be handled as sensitive artifacts.

## Client-neutral session projection

The service projection is stored separately at
`~/.agenthicc/session-service/<session-id>.jsonl` by default:

| Store | Owner | Contents | Recovery |
|---|---|---|---|
| `<id>.jsonl` | `session_service.SessionEventStore` | Versioned client-visible events, command acceptance records, and lifecycle metadata | Replay into `SessionSnapshot`; malformed lines are skipped |

This is a coordination/read-model ledger, not a replacement for the kernel
event log or conversation journal. Durable event sequences are per session.
`SessionService.compact()` explicitly removes records before a sequence and
clients requesting an older cursor receive `replay_gap` and must refresh their
snapshot. Ephemeral presentation events are never written to this store.

Service exports contain only the policy-filtered snapshot and durable event
projection. Project roots, workflow/agent fields, credentials, and private
payload keys are filtered before delivery; a support export can still contain
user prompts and tool results and must be reviewed before sharing.

Workflow checkpoints deliberately do not duplicate provider messages or
credentials. They store typed workflow state, the cursor into the session
journal, and only non-secret provider profile/workspace identity; the resumed
session resolves current environment secrets during startup. Writes use a
flushed temporary file followed by an atomic replacement, and checkpoint files
are kept under the session directory with restrictive permissions. Corrupt,
unreadable, stale, or plugin-mismatched checkpoints fail closed. Workflow
contexts have no framework-imposed serialized byte ceiling; they must still be
JSON-compatible, and available filesystem capacity is the practical limit.

### `reconstruct_site` evidence package

The reconstruct workflow keeps large, typed research and validation bodies in a
workspace-scoped evidence package rather than duplicating them in the session
checkpoint:

```text
.agenthicc/reconstruct_site/<run-id>/
  manifest.json
  phases/<phase>/<attempt>/<kind>-<content-hash>.<suffix>
```

`manifest.json` is schema-versioned, revisioned, mode-600 where supported, and
published with the same atomic replacement primitive as workflow checkpoints.
Each complete artifact records a content hash, byte count, media type, phase,
attempt, source, and workspace-relative path. Repeated writes with the same
run/phase/attempt/kind/content hash return the existing record. Stale records
remain visible after a validation re-entry so the user can audit what changed;
they are not accepted as current evidence.

The checkpoint carries only the manifest path/revision, required and stale
artifact IDs, screenshot IDs, profile/plan version, and a compact digest. The
body is rehydrated from disk on resume and verified against its hash and byte
count first. A missing file, malformed manifest, path escape, symlink escape,
or content mismatch is a typed recoverable error, not an implicit restart or a
successful phase. There is no framework-imposed one-megabyte checkpoint
ceiling; large bodies are externalized and normal JSON, filesystem, and
configured operational limits still apply.

Browser screenshots remain owned by the existing browser artifact store. The
reconstruct manifest links a validated workspace-relative browser path and
records the capture identity (route, sanitized URL, viewport, dimensions,
device scale, page state, role, backend, artifact ID, hash, and status). It
never stores cookies, authorization headers, URL credentials, or fabricated
image bytes. Identical capture identity plus content hash is deduplicated.
Missing Playwright/CloakBrowser is recorded as `degraded` evidence with the
reason and no artifact ID.

The package also records profile-skipped phases and re-entry history. These
records make a final evidence package explain both what was verified and what
was intentionally outside the selected scope.

Active
`running`, `pausing`, and `resuming` records are classified as interrupted on
startup and are never executed automatically. `/workflow resume` claims one
record atomically and rehydrates it into the existing `SessionConversation`.
Error finalization releases the claim after durable persistence. A deliberate
same-process Esc pause may retain its owner claim for the existing fast resume
path; terminal completion, failure, reset, and clean shutdown release it.

Workflow error checkpoints retain structured `pause_reason`, `failure_kind`,
bounded sanitized failure text, the last safe phase boundary, and an error
revision. A valid typed context is stored with `status="paused"`, making a
provider, tool, phase, timeout, or unexpected-cancellation error recoverable
without restarting phase one. During startup, a bootstrap checkpoint with
`context_ready=false` is intentionally diagnostic-only. If context encoding or
the primary checkpoint write fails, `recovery-error.json` records the safe run
identity and error category atomically. Its record revision cannot move
backward. It contains an intent digest rather than raw user intent, is capped
and mode 600, and is removed by an explicit workflow reset; it is never used
to fabricate a context for resume.

Workflow claims are published differently from ordinary checkpoints: the
complete, fsynced JSON metadata is installed atomically, so a process killed
while acquiring a claim cannot leave an empty or partially written `.claim`
that blocks recovery forever. On platforms exposing `/proc`, the claim also
stores the owner's process-start identity. A reused PID is therefore treated
as the old owner being gone, and a zombie process is reclaimable because it
cannot execute a workflow. A genuinely live owner remains protected; the
`run_already_claimed` diagnostic includes bounded owner/PID/host metadata and
instructs the user to close or resume the run in the other agenthicc process.
Legacy claims without a process-start identity retain fail-closed PID
behaviour.

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

Completed provider calls are stored as `kind: "usage_record"` entries with
`schema_version: 1`. The message fold ignores these entries; the usage fold
keeps the latest valid record for each local `record_id` and tolerates a
corrupt trailing line. Usage records contain token/cost metadata only, never
prompts, completions, tool arguments, or credentials. Legacy `tokens` events in
`conversation.jsonl` remain a read-only compatibility fallback.

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

### Background-terminal registry

Owned `run_bash`/`run_command` terminals use a separate child registry:

| Path | Owner | Contents | Recovery |
|---|---|---|---|
| `terminals/events.jsonl` | `background.TerminalStore` | Versioned terminal upserts, bounded output, lifecycle state | Folded by terminal ID |
| `terminals/registry.lock` | `TerminalStore` | Cross-process advisory lock | Recreated automatically |

Records link `terminal_id` to the originating `session_id`, `parent_job_id`,
and optional tool-call ID. Active records found without a live manager are
marked `orphaned`; they are never relaunched and their stored PID is not used
as an arbitrary process-discovery mechanism. Parent-session cancellation uses
only the persisted process group associated with that exact session to request
cleanup. Output and metadata obey the configured terminal byte/retention
limits and are redacted before persistence or JSON display.

PRD-151 terminal records also retain the declared `lifecycle`, readiness result
and evidence, deadline owner, termination reason, cancellation flag, and
cleanup result. A service remains an owned `running` record after readiness;
its readiness milestone is not stored as a finite-command `exited` result.
