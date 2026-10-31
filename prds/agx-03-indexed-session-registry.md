---
title: "AGX-03: Indexed session registry for O(page) open and search"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P0
series: AGX
related_prds: [agx-00, agx-04, agx-06, agx-14, prd-141, prd-176, prd-206, prd-209, prd-225]
tags: [agents, storage, performance, search, sqlite]
---

# AGX-03 — Indexed session registry for O(page) open and search

## Implementation status (2026-10-23)

Implemented. `BackgroundStore` reads from `index.sqlite3`
(`agenthicc.background.index.SessionIndex`); `events.jsonl` stays
authoritative and the JSON projection and its snapshot code are gone.

- **Schema (§4.2).** A `sessions` table with the full record plus the ordering,
  filter and search columns (and `idempotency_key`, so idempotent submission
  no longer scans the registry); an external-content FTS5 `trigram` index kept
  current by triggers, whose update trigger skips heartbeats; a single-row
  `position` table (inode, offset, mtime, sequence); `meta` with the schema
  version and search mode. A file stamped with another schema version is
  discarded and rebuilt.
- **Catch-up (§4.3).** Readers apply the complete tail inside one
  `BEGIN IMMEDIATE` transaction after re-reading the position, so concurrent
  readers never apply an event twice. A writer indexes its own append directly
  when the log grew by exactly that record; if the index is busy (another
  process rebuilding), the append still succeeds and a later read applies it.
  Heartbeats are folded once per session per batch, as in the old cold replay.
- **Reads (§4.4).** `get`, `list`, `query_page` (rows and total from one read
  transaction), `status_counts`, `active_sessions` and `with_idempotency_key`
  are SQL. The supervisor's capacity, dispatch and queue-position checks read
  only active sessions instead of decoding the whole registry. Decoded
  sessions are cached per index, keyed by their exact stored record.
- **Search.** Case-insensitive substring of ID, title, intent, labels, latest
  activity, workspace and workflow — the old substring semantics over more
  fields, and results stay in registry order. FTS5 trigram `MATCH` phrases
  accelerate queries whose words all have three or more characters; if a query
  has a shorter word, all terms use literal substring checks against the
  stored lowercase text. Without FTS5, `LIKE … ESCAPE` scans the plain column.
- **Rebuild (§4.5).** Progress via `rebuild_progress()`; the agents screen
  shows recent sessions read from the end of the log (created, or rewritten in
  full by a pre-AGX-04 update) until it finishes; leaving the screen cancels
  the rebuild, which rolls back.
- **Migration (§4.6), deviation.** Replaying a long log of full-record update
  events took minutes on a real registry, so the first open imports the old
  `projection-v1.json` snapshot when it still describes the log, replays only
  the events after it, and then deletes the file. This keeps one reference to
  `projection-v1` in `src/` (acceptance 4) and makes the upgrade as fast as the
  old warm start.

Measured on the development host (load average ≈ 40 on 8 vCPUs, so absolute
times are inflated), `scripts/benchmark_agents_manager.py`:

| Measure | 1k sessions / 50k events | 10k sessions / 200k events | Budget |
|---|---:|---:|---:|
| Fresh process: first page after imports, p50 | 4.2 ms | 6.9 ms | ≤ 50 ms (p95) |
| Page refresh, p50 | 0.3 ms | 1.9 ms | — |
| Search, p50 | 5.6 ms | 10.6 ms | ≤ 50 ms (p95) |
| Status counts, p50 | 0.2 ms | 1.5 ms | ≤ 10 ms |
| Catch-up of 1,000 events, p50 | 12–17 ms | 80 ms | ≤ 100 ms |
| Cold rebuild (heartbeat-dominated log) | 3.0 s | 33.5 s | ≤ 20 s for 1M |

A 90 MB log of 51k full-record update events: 43.6 s to rebuild cold, 1.3 s to
seed from its `projection-v1.json`, 6 ms per later open (the old store: 36.7 s
cold, 0.3 s from its snapshot). Rebuild time on full-record logs and the
write-bound maintenance pass (each reconciled session costs one durable append
plus one index transaction) are what AGX-04's delta events and due-driven
maintenance reduce.

Tests: `tests/unit/test_background_session_index.py` (search in both modes,
filters and counts, recovery, migration, cancellation, provisional view, and a
property test comparing incremental catch-up and cold rebuild with a
reference fold over 120 random event histories),
`tests/integration/test_background_index_concurrency.py` (two writer
processes and a page reader), and the rewritten PRD-206/209 store tests.

## 1. Summary

The agents screen reads the background registry by folding `events.jsonl`
into an in-memory dictionary, checkpointed as a JSON snapshot. That makes every
fresh process pay O(all sessions) before it can show one page: 3.6–6.0 s with
the snapshot and 33 s without it at 10,000 sessions, against PRD-209's 250 ms
budget for the first useful frame. Search is a linear substring scan over four
fields. Replace the JSON snapshot with a derived SQLite index — sessions table,
secondary indexes and full-text search — that a process can query for one page
without loading the registry. `events.jsonl` stays the authority.

## 2. Problem and evidence

### 2.1 Current read path

`BackgroundStore._fold_locked` (`src/agenthicc/background/store.py:585`) loads
`projection-v1.json` (or replays the whole log), applies the event tail and
returns a `dict[str, BackgroundSession]`. Pages come from:

- **No filter:** a slice of the in-memory order index — fast once loaded.
- **Any filter or query:** `_matching` / `_query_page_locked` walk every
  session and test `query in " ".join((session_id, title, cwd, workflow_name))`.
  Intent, labels and activity are not searchable.

`agenthicc agents --json`, maintenance (`recover_stale`) and every manager
refresh go through the same fold.

### 2.2 Measured cost (AGX-00 §4.2)

| Registry | Case | Time to first page |
|---|---|---:|
| 10k sessions / 1M events (150 MB) | fresh process, no snapshot | 33.1 s (32.2 s CPU) |
| 10k / 1M | fresh process, 16.7 MB snapshot | 3.6–6.0 s |
| 10k / 1M | in-process cold projection (benchmark) | 51.0 s |
| 1k / 50k (8.2 MB) | in-process cold projection | 1.12 s |
| 1k / 50k | SQLite prototype page + count | 0.44 ms |

PRD-209's benchmark reports the placeholder frame (55–71 ms), which hides
these numbers: a user sees "loading session index…" until the fold finishes.

### 2.3 Why it grows

Sessions are retained until archived and purged, and every lifecycle change,
heartbeat-adjacent update and activity summary appends an event. Registries
grow monotonically with use; a JSON snapshot must be parsed in full and turned
into 10,000 dataclasses before the first row is drawn.

## 3. Goals and non-goals

**Goals**

1. A fresh process shows the first useful page in O(page size), independent of
   registry size.
2. Search covers title, intent, labels, latest activity, ID and workspace, and
   returns ranked matches in milliseconds.
3. Status, project, workflow, run, parent and "needs attention" filters and
   counts are index lookups.
4. The index is derived, rebuildable and never authoritative.

**Non-goals**

- Moving the authority from `events.jsonl` to SQLite.
- Indexing conversation transcripts (AGX-07 reads transcripts on demand).

## 4. Design

### 4.1 Files

```text
~/.agenthicc/background/
  events.jsonl            authoritative append-only log (unchanged)
  index.sqlite3           derived index (new, WAL mode)
  projection-v1.json      deleted after migration
```

### 4.2 Schema

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema_version, events_inode, applied_offset, applied_sequence

CREATE TABLE sessions (
  session_id     TEXT PRIMARY KEY,
  status         TEXT NOT NULL,
  dispatch_state TEXT NOT NULL,
  needs_attention INTEGER NOT NULL,      -- status is waiting_approval or waiting_input
  priority       TEXT NOT NULL,
  pinned         INTEGER NOT NULL,
  cwd            TEXT NOT NULL,
  workflow_name  TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  parent_session_id TEXT NOT NULL,
  role           TEXT NOT NULL,
  title          TEXT NOT NULL,
  last_active    REAL NOT NULL,
  created_at     REAL NOT NULL,
  worker_pid     INTEGER,
  heartbeat_deadline REAL,                -- used by AGX-04
  record         BLOB NOT NULL            -- the full BackgroundSession as JSON
);
CREATE INDEX sessions_order ON sessions (pinned DESC, last_active DESC, session_id);
CREATE INDEX sessions_status ON sessions (status, last_active DESC);
CREATE INDEX sessions_attention ON sessions (needs_attention, last_active DESC);
CREATE INDEX sessions_cwd ON sessions (cwd, last_active DESC);
CREATE INDEX sessions_run ON sessions (run_id) WHERE run_id <> '';
CREATE INDEX sessions_parent ON sessions (parent_session_id) WHERE parent_session_id <> '';
CREATE INDEX sessions_heartbeat ON sessions (heartbeat_deadline) WHERE heartbeat_deadline IS NOT NULL;

CREATE VIRTUAL TABLE sessions_fts USING fts5(
  session_id UNINDEXED, title, intent, labels, latest_activity, cwd,
  tokenize = 'unicode61 remove_diacritics 2'
);
```

Rows store the full record, so the detail view needs no fold. Columns exist only
for ordering, filtering and counting.

### 4.3 Writes and catch-up

- Appends to `events.jsonl` keep their current lock and fsync discipline.
- After appending, the writer applies the new events to the index in one
  transaction under the same inter-process lock and advances
  `applied_offset`/`applied_sequence`.
- Readers that find `applied_offset` behind the log's size apply the tail
  themselves (idempotent by sequence number) before querying. A crash between
  the append and the index update therefore costs one catch-up, never
  divergence.
- A sequence gap, inode change or schema-version mismatch triggers a rebuild
  (§4.5), matching today's `invalidate_projection` rules.

### 4.4 Reads

| Need | Query |
|---|---|
| First page | `SELECT record FROM sessions ORDER BY pinned DESC, last_active DESC, session_id LIMIT :n` |
| Next page | Keyset pagination on `(pinned, last_active, session_id)` — no `OFFSET` scans |
| Counts per tab/status | `SELECT status, count(*) FROM sessions GROUP BY status` (index-only) |
| Needs attention | `WHERE needs_attention = 1` |
| Search | `sessions_fts MATCH :q ORDER BY bm25(sessions_fts)`, joined to `sessions` |
| Children of a session or run | `WHERE parent_session_id = :id` / `WHERE run_id = :id` |
| Stale workers (AGX-04) | `WHERE heartbeat_deadline < :now` |

`BackgroundStore.query_page`, `get` and `list` keep their signatures and return
the same `BackgroundSession` values; their implementation becomes SQL. The
manager keeps only the visible page and the selected record in memory.

If the Python build lacks FTS5, `sessions_fts` is replaced by a normalised,
lower-cased `search_text` column queried with `LIKE`; still linear, but in C and
measured within budget at 10,000 rows. The choice is recorded in `meta` and
covered by a test that disables FTS5.

### 4.5 Rebuild with useful partial results

A missing or invalid index is rebuilt by streaming `events.jsonl` with batched
`executemany` inserts in one transaction per batch. While it runs:

1. The store first reads the last few megabytes of the log and materialises the
   most recently active sessions it finds, marked *partial*, so the screen can
   show recent work within a second.
2. The manager shows rebuild progress ("Indexing 412,000 of 1,000,000 events")
   instead of "loading session index… 33s".
3. The finished index atomically replaces the partial view.

### 4.6 Migration

On first open after upgrade the store builds `index.sqlite3` from the log
(§4.5) and deletes `projection-v1.json` and the JSON snapshot code. There is no
period in which both representations are maintained.

### 4.7 Scope beyond the background registry

The foreground session index used by `agenthicc sessions list` (`index.json`)
has the same shape of problem. AGX-05's History tab needs both registries
searchable; the same module (`agenthicc/storage/session_index.py`) serves both
with separate databases.

## 5. Budgets and acceptance criteria

Fixture: `scripts/benchmark_agents_manager.py --sessions 10000 --events 1000000`,
extended by AGX-14 to measure a **fresh process**:

| Operation | Budget |
|---|---:|
| Fresh process: open store and return first page (index current) | p95 ≤ 50 ms after imports; first useful frame p95 ≤ 250 ms |
| Search over 10k sessions | p95 ≤ 50 ms |
| Status counts | p95 ≤ 10 ms |
| Catch-up of 1,000 new events | ≤ 100 ms |
| Full rebuild of 1M events | ≤ 20 s, with recent sessions visible within 1 s |
| Manager memory with 10k sessions | ≤ 50 MB above interpreter baseline |

1. `query_page`, `get`, `list` and `agents --json` return identical results to
   the current fold for the benchmark fixture and the existing store tests.
2. Killing a writer between append and index update loses nothing: the next
   reader catches up.
3. Corrupting or deleting `index.sqlite3` triggers a rebuild with progress and
   no data loss.
4. `projection-v1.json` handling is removed; `rg projection-v1` finds nothing in
   `src/`.

## 6. Test plan

- Property test: random event sequences produce the same sessions through the
  old fold (kept in the test suite as an oracle) and the index.
- Crash-injection tests around append/index update.
- Concurrency: two processes appending while a third reads pages.
- FTS5-disabled variant.
- AGX-14 fresh-process benchmark at 1k and 10k sessions.

## 7. Risks

- **Write amplification.** Each append also writes the index. Mitigation: batch
  index updates per append call; WAL mode; measure append latency in the
  benchmark (budget: p95 ≤ 5 ms added).
- **SQLite on network filesystems.** WAL needs shared memory; `~/.agenthicc` on
  NFS is unsupported for WAL. Mitigation: detect and fall back to
  `journal_mode=DELETE` with a warning.

## Sources

- SQLite FTS5: <https://www.sqlite.org/fts5.html>
- SQLite WAL and its limitations: <https://www.sqlite.org/wal.html>
