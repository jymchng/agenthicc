---
title: "PRD-254: Recover Background Registry Schema Before Detach"
status: Implemented
version: 1.0.0
date: 2026-10-26
repository: jymchng/agenthicc
priority: P1
related_prds: [agx-03, agx-14, prd-141, prd-208, prd-218, prd-245]
tags: [background-sessions, detach, sqlite, schema-migration, recovery]
---

# PRD-254 — Recover Background Registry Schema Before `/detach`

## Summary

Fix the background-session SQLite schema compatibility failure that makes
`/detach` print:

```text
Unable to background session: OperationalError: no such column: idempotency_key
```

The background SQLite index is derived data; `events.jsonl` remains the
durable lifecycle authority. Agenthicc must detect and repair an old or
partially initialized index before using the new schema, rather than failing
the foreground-to-background handoff or asking users to delete their store.

## Incident analysis and confirmed cause (pre-fix)

The `/detach` call path at the time of the incident was:

```text
/detach
  → background.integration._handoff()
  → BackgroundSupervisor.handoff()
  → BackgroundStore.get(session_id)
  → BackgroundStore._sync()
  → SessionIndex.state() / SessionIndex._connect()
  → creates the SQLite schema and dependent indexes
```

The pre-fix index schema declared `sessions.idempotency_key` and then created
the `sessions_idempotency` index on that column. However:

1. The old schema check only rejected a database when `meta.schema_version`
   exists and explicitly differs from `INDEX_SCHEMA_VERSION`. Missing `meta`
   or a missing version row is not classified as an incompatible schema.
2. `CREATE TABLE IF NOT EXISTS sessions (...)` does not add columns to an
   already-existing `sessions` table.
3. The dependent index DDL then referenced `sessions.idempotency_key`. SQLite
   raised `OperationalError: no such
   column: idempotency_key` before the store can read the session.
4. `BackgroundStore._sync()` re-raised every `sqlite3.OperationalError` as if
   it were transient contention. Thus a permanent schema mismatch did not
   reach the existing index recovery path.
5. `_handoff()` catches the exception and displays the reported message. The
   handoff fails before the supervisor prepares its durable background request
   or launches a worker. This is a local registry/schema failure, not a model,
   provider, or workflow error.

This failure was reproduced against an existing SQLite `sessions` table with
the then-current columns except `idempotency_key`; opening
`SessionIndex.state()` produced the exact reported exception. The likely
runtime precondition is an older or partially initialized index whose schema
marker is missing or unreliable. A database marked with an old version should
already be detected by the existing version check; therefore the migration
must inspect the actual table shape, not trust the marker alone.

## Problem statement

Users can have a valid foreground conversation and session lease but be unable
to detach because an old derived registry index prevents `BackgroundStore`
from loading the session. The append-only event history can still be intact,
yet the current code treats an index schema error like a fatal operational
failure. Requiring manual deletion is unsafe, unnecessary, and risks removing
recoverable background metadata.

## Goals

1. Make `/detach` and other background-store operations automatically recover
   compatible older and incomplete index schemas.
2. Preserve `events.jsonl` and reconstruct the exact registry state from the
   authoritative event history whenever an in-place migration is not safe.
3. Distinguish schema incompatibility/corruption from transient SQLite
   `locked`/`busy` conditions.
4. Preserve idempotency, ownership fencing, and the two-stage prepared-request
   handoff; a migration retry must not start duplicate workers or advance a
   session attempt twice.
5. Keep the migration safe across process interruption and concurrent
   Agenthicc processes.
6. Recover without asking users to remove files or reinstall Agenthicc.

## Non-goals

- Redesigning `/detach`, the background worker lifecycle, or session leases.
- Treating the SQLite index as authoritative over `events.jsonl`.
- Automatically recovering a corrupt or missing authoritative event log.
- Silently retrying arbitrary SQLite operational failures as though they were
  schema errors.

## Functional requirements

### FR-1: Validate schema structure before dependent DDL or queries

Before creating indexes, triggers, or issuing queries that depend on
`idempotency_key`, inspect `sqlite_master`, `PRAGMA table_info(sessions)`, and
the schema metadata. An existing but unversioned database must be treated as
an unknown existing schema, not as a fresh database. A version marker is
evidence, not proof: required columns and indexes must agree with the declared
schema version.

### FR-2: Apply explicit, idempotent schema migrations

Maintain ordered migration steps for supported index schema versions and
known partial states. The migration for `idempotency_key` must:

- add the column only when absent;
- use a safe empty default for records with no idempotency key;
- backfill values from each serialized `sessions.record` where present,
  without relying on optional SQLite JSON extensions;
- create the `sessions_idempotency` index after the column exists;
- update the schema version only after the migration succeeds.

DDL, backfill, index creation, and version update must be transactional where
SQLite permits it. Re-running after a crash must safely finish or repeat the
migration.

### FR-3: Rebuild only when migration cannot safely reconcile the index

If required columns or structural invariants are missing beyond a supported
additive migration, classify the index as incompatible and rebuild it using
the existing canonical recovery path from `events.jsonl` (and any still-valid
legacy snapshot import supported by AGX-03). Preserve the event log and do not
discard session state because the derived index is stale.

The rebuild must coordinate open connections and WAL/SHM files. Do not unlink
or replace an index while another process may be using it. Use the existing
cross-process registry/index locking model or an equivalent exclusive,
recoverable protocol; publish a rebuilt index atomically only after it is
complete.

### FR-4: Classify SQLite operational errors precisely

Only lock/busy errors are transient contention. Schema errors such as
`no such column`, `no such table`, and malformed schema must invoke schema
reconciliation/rebuild. Other `OperationalError` cases must retain their
specific diagnosis and must not trigger destructive recovery without
evidence that the index is rebuildable and incompatible.

### FR-5: Keep detach idempotent and ownership-safe

After schema recovery, `/detach` must continue through the existing
owner-checked, prepared handoff. If migration is busy or fails, do not claim
that the session was backgrounded, do not start a worker, do not release the
foreground owner prematurely, and do not create a second attempt on retry.
Successful retry must create or reuse exactly one background request for the
same session/attempt according to existing idempotency rules.

### FR-6: Report actionable failure when safe recovery is unavailable

If the index cannot be migrated/rebuilt (for example, the authoritative log
is unreadable or another process holds an incompatible live connection),
display a concise explanation that identifies the index recovery failure and
recommends retrying after the other Agenthicc process exits. Do not expose
session transcript contents or secrets. Retain the underlying exception in
diagnostic logs.

## Non-functional requirements

- No event-log or session data loss during migration, crash, or retry.
- Migration is deterministic and safe to rerun.
- Existing valid schema opens without rebuild or avoidable full-log replay.
- Busy/locked databases are not mistaken for corrupt schemas and are not
  deleted.
- Startup and `agenthicc agents` remain responsive during a necessary full
  rebuild, using the existing progress/cancellation behavior where available.
- Work with supported SQLite builds both with and without FTS5.

## Recommended implementation shape

1. Split fresh schema creation from migration: create base metadata/tables,
   inspect actual structure, reconcile columns, then create dependent indexes
   and triggers.
2. Introduce a schema inspector that returns a typed result such as
   `CURRENT`, `MIGRATABLE`, `REBUILD_REQUIRED`, or `BUSY`, with a reason.
3. Implement versioned migrations for known additive changes, including
   `idempotency_key`; use a full event-log rebuild only for unsupported or
   structurally inconsistent layouts.
4. Make `_sync()` catch only SQLite busy/locked errors as contention. Route
   schema incompatibility through the inspector and the canonical recovery
   path.
5. Use an interprocess-safe migration claim and atomic completion marker so
   concurrent readers either use the old valid index or wait for/use the new
   valid index, never a half-created schema.
6. Keep the handoff pipeline unchanged after the store becomes readable; add
   only structured error mapping around storage recovery if needed.

## Acceptance criteria

1. An existing `sessions` table without `idempotency_key` and without a
   `schema_version` row is migrated or rebuilt automatically; opening it does
   not raise `no such column`.
2. The same recovery works when the column is absent but metadata incorrectly
   claims the current version.
3. After recovery, existing session records and their idempotency keys are
   queryable and match the authoritative event history.
4. A database with a supported older version is migrated; an unsupported
   schema is rebuilt without modifying `events.jsonl`.
5. A process interruption during migration is recoverable on the next open.
6. A busy/locked index is reported as contention and is neither deleted nor
   rebuilt as corrupt.
7. `/detach` against a legacy index succeeds, prepares and starts one worker,
   and preserves foreground ownership/handoff ordering.
8. A failed migration does not falsely report success, start a worker, lose
   the foreground lease, or duplicate a request on retry.
9. Existing current-version startup, indexed queries, FTS5 fallback, and
   crash-replay behavior remain unchanged.
10. If the authoritative event log cannot be accessed during index recovery,
    recovery aborts without publishing an empty index; `/detach` stays in the
    foreground, starts no worker, and reports a concise retry instruction.

## Test plan

### Unit

- Current schema and accurate current version marker.
- Old schema missing `idempotency_key`, with no `meta` table.
- Existing `meta` table with no version row.
- Missing column with a falsely current version marker.
- Supported older version migration and idempotent second open.
- Backfill idempotency keys from JSON records, including empty keys.
- Unknown missing-column layout routes to rebuild, not blind DDL.
- Lock/busy errors do not invoke schema deletion or rebuild.
- Interrupted migration leaves a state that the next opener can recover.
- Event-log stat/read failures abort replay and leave the event bytes intact.
- A missing event log with a nonempty indexed position does not replace the
  existing derived rows with an empty registry.

### Integration

- Disk-backed upgrade preserves all sessions and the event log.
- Concurrent process open/read while another process migrates or rebuilds.
- Rebuilt index preserves FTS5/LIKE semantics, ordering, filters, and
  `with_idempotency_key()` results.
- Corrupt index recovery continues to use the existing rebuild progress and
  cancellation contract.

### End-to-end

- Start a foreground session using a fixture with a legacy `index.sqlite3`,
  issue `/detach`, and assert the session reaches the normal background path
  exactly once.
- Inject schema migration failure and assert `/detach` remains foreground,
  reports a retryable actionable error, and starts no worker.
- Inject an event-log access failure and assert `/detach` preserves the
  foreground lease, starts no worker, leaves the request and event log
  unchanged, and records the underlying exception only in diagnostics.
- Retry after recovery and verify no duplicate session attempt or worker.

Use mocked worker launch and deterministic event logs; tests must not call an
external LLM provider.

## Rollout and recovery

Apply migrations automatically when `BackgroundStore` opens. Do not require
manual deletion of `~/.agenthicc/background/index.sqlite3`. `events.jsonl`
must remain untouched. If an index must be rebuilt, retain the existing
recoverable behavior for partial progress and make the operation safe to
repeat after process restart. Emit schema version and migration outcome in
diagnostics without logging session contents.

The implementation inspects actual table columns before dependent DDL,
migrates the supported idempotency-column gap in a transaction, and rebuilds
unsupported schemas by replaying the event log. A cross-process maintenance
lock serializes schema initialization and recovery; open index handles detect
replacement by file identity before reusing cached state. Failed SQLite
handoffs are logged for diagnostics while the foreground message remains
concise and does not expose database paths or transcript data.

## Evidence and limitation

The local code path was reproduced with a legacy SQLite table missing
`idempotency_key`, yielding the exact exception. The local runtime's default
`~/.agenthicc/background/index.sqlite3` was separately inspected read-only and
has schema version 4 with the column present; it is not the failing database.
The exact user's configured store file was not available in this workspace,
so the exact metadata state of that file remains unverified. The code defect
and reproducible schema precondition are confirmed.
