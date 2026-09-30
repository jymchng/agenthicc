---
title: "PRD-209: Low-latency, non-blocking `agenthicc agents` TUI"
status: Implemented
version: 1.0.0
date: 2026-09-30
repository: jymchng/agenthicc
related_prds:
  - PRD-141  # Background sessions and Session Manager TUI
  - PRD-150  # Client-neutral session service and event projection
  - PRD-202  # Paginated /ps terminal overlay
  - PRD-206  # Paginated agents manager
  - PRD-207  # Async-first runtime
  - PRD-208  # Responsive Ctrl+X deletion
tags:
  - agents
  - sessions
  - tui
  - performance
  - latency
  - async
---

# PRD-209 — Low-latency, non-blocking `agenthicc agents` TUI

## 1. Executive summary

The `agenthicc agents` screen is functionally complete but remains noticeably
laggy. Keyboard actions can pause before the next frame appears, opening the
screen can become slower as the background registry grows, and idle refreshes
can compete with worker activity. PRD-206 bounded the visible viewport and
PRD-208 made `Ctrl+X` deletion asynchronous, but the manager still has a
broader synchronous hot path.

This PRD defines a thorough performance fix. It treats the manager as a
latency-sensitive client of the durable background-session projection rather
than as a place that repeatedly reconstructs the entire registry. It also
requires every user action that can touch the filesystem, process table,
JSONL event log, terminal records, or worker lifecycle to leave the TUI event
loop immediately and report completion through an owned task.

The target experience is:

```text
open agenthicc agents
  ├─ first useful frame appears quickly
  ├─ selection moves immediately
  ├─ refresh coalesces while the store is busy
  ├─ background updates do not monopolize the event loop
  └─ cancel / archive / attach / delete show progress without freezing input
```

The implementation must preserve the existing session store, event history,
leases, redaction, ownership rules, exact-ID selection, recoverable deletion,
and complete JSON/scriptable views. This is a performance and scheduling
change, not permission to weaken durability or silently omit sessions.

## 2. Current evidence and diagnosis

The investigation is based on the current source tree at the date above. The
primary surfaces are:

* `src/agenthicc/tui/workspace/background_manager.py`;
* `src/agenthicc/background/store.py`;
* `src/agenthicc/background/supervisor.py`;
* `src/agenthicc/background/terminals/`;
* `src/agenthicc/cli/commands/background.py`; and
* the PRD-206/208 manager, integration, and E2E tests.

### 2.1 The refresh is logically cached but still globally expensive

`BackgroundStore._fold()` has a process-local projection cache keyed by an
events-file fingerprint. That avoids rereading the file when nothing changed,
but any append invalidates the cache and reconstructs every session by parsing
the complete `events.jsonl` history. `BackgroundStore.list()` then scans every
projected session, applies filters, and sorts the full result.

The manager calls `refresh()` from the run loop and from rendering-related
paths. Therefore the cost of one worker heartbeat or lifecycle event can become
the cost of parsing and sorting the complete registry on the next refresh.
The work is proportional to total historical events and total sessions, not to
the visible page.

### 2.2 Maintenance performs a second full scan and can run on the UI path

`BackgroundManager.run()` invokes `maintain()` on every loop iteration. The
maintenance cadence is currently derived from `refresh_s`, whose minimum is
short enough to make stale-worker recovery frequent. `recover_stale()` calls
`store.list()` and may inspect `/proc`, process liveness, command lines, and
leases for every active record. When maintenance runs, it forces another
manager refresh even when no record changed. This creates the sequence:

```text
recover_stale → full store list → process inspection
              → forced full manager refresh → full store list again
```

This work can happen immediately after a key event and competes with repaint
and input scheduling.

### 2.3 Several key handlers still perform blocking operations inline

The manager's `handle_key()` is synchronous. In addition to navigation, it
directly invokes operations such as cancellation, archive, restore, approval,
input delivery, forced maintenance/refresh, and bulk actions. These operations
can acquire the registry lock, append and fsync JSONL, signal a worker, wait
for a process grace period, stop owned terminals, move artifacts, or inspect
the filesystem. `Ctrl+X` has a dedicated async path after PRD-208, but the
other actions can still freeze the same event loop.

The problem is not solved by the input reader running in an executor: the
reader only waits for a key. The synchronous action handler still executes on
the event-loop thread after the key is returned.

### 2.4 Idle rendering still performs work outside the final Rich cache

The manager avoids calling `Live.update()` when its render key is unchanged,
but reaching that decision still performs work. The run loop calls refresh,
maintenance, and `render()` on every iteration. The render path may:

* calculate the selected record and page bounds;
* stat the selected `conversation.jsonl` for activity projection;
* stat the same file again while constructing the render key;
* construct filtering and projection tuples; and
* inspect all records needed by the current store list.

The selected journal tail is bounded and cached by fingerprint, which is good,
but fingerprint checks and cache bookkeeping must not occur on every idle frame
when there is no journal-change notification.

### 2.5 Rich rendering and input scheduling are not coalesced as one frame loop

The manager uses `Live(auto_refresh=False)` and updates only when a new
renderable object is produced, but every timeout/key cycle still executes the
full scheduling body. Rapid input can create repeated refresh attempts before
the previous projection work has become useful. Background changes can also
arrive faster than the terminal can display them.

The implementation needs explicit dirty generations, one in-flight projection
read, refresh coalescing, and a render scheduler that drops obsolete
intermediate frames while preserving the newest state.

### 2.6 The current tests prove bounded layout, not latency under contention

The existing tests cover pagination, selection identity, bounded activity,
interactive deletion, and several CLI paths. They do not establish:

* key-to-frame latency while the registry is large;
* frame latency while workers append events continuously;
* behavior while stale-worker recovery scans many records;
* cancellation/archive/restore actions during a slow filesystem or process
  operation;
* concurrent refresh requests and stale-result suppression;
* first-paint time separately from complete registry indexing; or
* event-loop responsiveness using a real scripted terminal backend under load.

PRD-209 therefore requires instrumentation and deterministic performance
fixtures before and after implementation.

## 3. Goals

### Product goals

1. Make `agenthicc agents` feel immediate for navigation, marking, filtering,
   help, pause, and quit.
2. Keep the first useful frame fast even when the durable registry contains
   thousands of sessions or millions of historical events.
3. Keep the table, selected details, progress notices, and footer accurate
   while background state is being indexed or refreshed.
4. Ensure cancel, archive, restore, approval, input, attach, and deletion do
   not freeze keyboard input or terminal repainting.
5. Preserve complete and timely visibility: optimization may defer or page
   work, but may not silently lose a session or show an obsolete action target.

### Engineering goals

1. Move blocking store, process, terminal, and artifact work behind an owned
   asynchronous service boundary, using bounded adapters where native async
   APIs are unavailable.
2. Make projection cost proportional to changed data and the visible page,
   rather than repeatedly proportional to the complete event history.
3. Coalesce refresh and repaint requests and discard obsolete in-flight
   results safely.
4. Separate first paint, projection indexing, maintenance, activity extraction,
   and Rich rendering into independently measurable stages.
5. Add performance budgets, tracing, regression tests, and a repeatable load
   harness to prevent future latency regressions.

## 4. Non-goals

This PRD does not:

* change background worker semantics, workflow phases, or session ownership;
* remove append-only durability, fsync boundaries, or cross-process locking;
* return incomplete JSON output from `agents --json` or `jobs list --json`;
* delete or truncate conversation journals to improve the screen speed;
* remove redaction, capability checks, workspace boundaries, or approval rules;
* replace Rich or the current terminal abstraction without evidence that the
  abstraction itself is the bottleneck;
* make stale sessions disappear merely because recovery is expensive;
* make operations fire-and-forget without durable result/error handling; or
* weaken PRD-208's recoverable, exact-target deletion contract.

## 5. Performance contract and budgets

All budgets must be measured in a declared CI/Linux reference environment and
reported as p50, p95, and p99. The benchmark must record CPU count, Python
version, terminal dimensions, session count, event count, and artifact layout.
The exact hardware may vary, but the fixture and thresholds must remain
constant so regressions are comparable.

### 5.1 Interactive latency targets

For a registry containing 10,000 sessions and 1,000,000 historical events:

| Operation | p95 target | p99 target | Constraint |
|---|---:|---:|---|
| First useful frame | 250 ms | 500 ms | Does not wait for full stale recovery |
| Up/down/Home/End/page key to visible state | 50 ms | 100 ms | No disk/process work on the key path |
| Mark/filter/help/pause/quit key | 50 ms | 100 ms | Input acknowledgement is immediate |
| Idle frame scheduling | 16 ms | 50 ms | No unbounded synchronous callback |
| Store projection refresh after a change | 250 ms | 750 ms | May complete off-loop; newest state wins |
| Attach/cancel/archive/restore dispatch | 50 ms | 100 ms | Dispatch latency only; operation completion is separate |

The user-facing screen must show an explicit `loading`, `refreshing`, or
operation-progress state when completion cannot fit within the dispatch
budget. A slow operation must never be represented as a frozen screen.

### 5.2 Resource targets

* At most one registry projection read and one activity-read task may be in
  flight per manager instance.
* Maintenance must not run more often than its configured cadence and must
  never run twice concurrently.
* A stale projection result must not trigger a Rich repaint after a newer
  generation has been accepted.
* Idle frames must not allocate or rebuild a full table/group when no visible
  state, layout, activity, or notice changed.
* Interactive memory must be bounded by the projection/index representation,
  visible-page records, and bounded activity tails; it must not retain every
  journal line or every rendered frame.

## 6. Target architecture and data flow

The manager should become a thin reactive client over a shared projection and
operation service:

```text
BackgroundStore events.jsonl / snapshot / change token
                 │
                 ▼
       BackgroundProjectionService
       ├─ incremental fold/index
       ├─ page/filter/count query
       └─ activity/change notifications
                 │ async, generation-tagged
                 ▼
       BackgroundManager reactive state
       ├─ selected session ID / marks
       ├─ visible page
       ├─ loading/error/progress notices
       └─ dirty flags and render generation
                 │
                 ▼
       FrameScheduler → Rich Live
```

User actions use a separate operation boundary:

```text
key → validate exact ID/current mode
    → enqueue owned operation task
    → render immediate acknowledgement
    → store/process/terminal/filesystem work off-loop
    → durable result/event
    → coalesced projection refresh
    → newest-generation frame
```

No `handle_key()` branch may synchronously wait for a process, lock, fsync,
artifact move, journal parse, or full registry scan.

## 7. Functional requirements

### FR-001 — Instrumented latency model

Add a small redacted performance model for manager lifecycle stages:

```text
manager_open
first_frame_requested
first_frame_rendered
key_received
action_dispatched
projection_requested
projection_ready
frame_requested
frame_rendered
maintenance_started
maintenance_finished
```

Record monotonic durations and bounded counts only. Do not record transcript
text, prompts, secrets, full paths, or session intent. The metrics must be
disabled or sampled cheaply in normal use and enabled deterministically in
tests/diagnostic mode.

### FR-002 — Progressive first paint

Opening `agenthicc agents` must render a useful frame before optional full
projection indexing and stale-worker recovery complete. The first frame may
show a bounded loading state and an accurate known count, but must not show
misleading actions for records that are not loaded.

The first paint path must:

* load the smallest safe page/projection needed for the initial viewport;
* avoid process-table scans and journal reads;
* avoid waiting for complete historical event folding when an existing
  snapshot/index is available; and
* schedule the rest as an owned task.

### FR-003 — Incremental durable projection

Extend `BackgroundStore` or introduce a store-owned projection service with:

* an atomic, versioned snapshot/index for current session metadata;
* an event sequence/change token identifying the newest applied event;
* incremental application of events appended after that token;
* safe rebuild from `events.jsonl` after corruption, missing snapshot, or
  schema/version change; and
* cross-process invalidation that does not reread the full history when only a
  tail can be applied.

The snapshot is a derived cache, never the authority. Crash-safe replacement,
file locking, schema versioning, and replay tests are mandatory. A concurrent
writer must not cause the manager to display a partial snapshot as authoritative.

### FR-004 — Page-oriented projection queries

Provide a typed query for the interactive view that returns:

```text
visible records for page
total matching count
page/cursor metadata
projection generation/change token
```

Filtering and ordering must remain deterministic and must preserve the exact
selected session ID. The query may use an in-memory index or SQLite-backed
derived index, but it must not materialize and sort all records on every frame.
The existing complete `list()` API and JSON output remain available and
semantically unchanged.

### FR-005 — Async manager service boundary

Introduce typed async manager operations for projection, maintenance, activity,
and user actions. Blocking compatibility methods may remain for CLI callers,
but the interactive manager must call async methods or `asyncio.to_thread`
through one bounded adapter owned by the runtime.

Required operations include:

```python
refresh_page_async(...)
maintain_async(...)
cancel_async(session_id)
archive_async(session_id)
restore_async(session_id)
provide_input_async(session_id, value)
attach_prepare_async(session_id)
```

`delete_async` from PRD-208 must be reused, not duplicated. Each operation
returns a structured result with the exact target ID, operation ID, phase,
success/failure category, and redacted message.

### FR-006 — Immediate key dispatch and action state

Navigation and local UI actions must update reactive state synchronously without
touching durable storage. Durable actions must:

1. validate the current exact session ID;
2. create an idempotency/operation ID;
3. mark the operation pending in the manager state;
4. schedule the owned async task;
5. return to input handling; and
6. repaint an acknowledgement/progress state.

Repeated actions for the same pending operation must be ignored or coalesced.
Actions for a session that disappeared must fail safely rather than applying to
the row that shifted into its index.

### FR-007 — Refresh coalescing and newest-generation wins

Refresh requests from timer ticks, key handlers, external store changes,
operation completion, and resize events must be coalesced. The manager must
not start a second equivalent projection read while one is pending.

Every request carries a generation or change token. When a result arrives:

* a newer result replaces it;
* an older result is discarded without repainting;
* pending refresh demand is replayed once, not once per triggering event; and
* selection/marks are reconciled by session ID.

### FR-008 — Dirty-frame scheduler

Replace the unconditional run-loop refresh body with explicit dirty causes:

```text
projection_changed
activity_changed
operation_changed
layout_changed
notice_changed
help_changed
selection_changed
```

Only dirty causes schedule a new render. Multiple causes in one event-loop
turn produce one frame. A frame already queued for terminal output may be
replaced by a newer frame before `Live.update()` executes. The scheduler must
retain the most recent state and never starve input.

### FR-009 — Activity projection off the render path

The selected journal activity reader must be asynchronous and generation-aware.
It must:

* read only a bounded tail;
* parse only the event kinds needed for the current summary;
* deduplicate requests for the same session/fingerprint;
* cache the parsed result by durable fingerprint/change token;
* cancel or supersede a read when selection changes; and
* update the details panel only if the result still belongs to the selected
  session and current generation.

A render frame must use the last known activity value and must not call
blocking `Path.stat()`, `read_bytes()`, or JSON parsing for the first time.

### FR-010 — Maintenance isolation and backoff

Stale-worker recovery, process identity checks, terminal cleanup checks, trash
retention, and orphan reconciliation must run in an owned maintenance task,
never in `handle_key()` or the render callback.

Maintenance must be:

* single-flight;
* independently scheduled from repaint cadence;
* skipped or exponentially backed off after repeated failures;
* bounded by a per-pass work budget; and
* able to resume from a stable cursor rather than rescanning every record on
  every pass.

A manual `r` refresh may request maintenance, but must return an immediate
`refresh requested` state and let the task finish asynchronously.

### FR-011 — Non-blocking action parity

The following actions must remain responsive under a deliberately slow store,
worker, terminal, or filesystem fixture:

* `c` cancel;
* `a` archive;
* `u` restore;
* approval and input delivery;
* marked/bulk cancel and archive;
* `Enter` attach preparation;
* `Ctrl+X` deletion; and
* `r` refresh/maintenance.

The UI must display `starting`, `waiting`, `completed`, or `failed` state and
must not report success before the durable operation has completed.

### FR-012 — Resize and terminal-output coalescing

Resize events must invalidate layout state once per debounce window. They must
not trigger a full store reload unless the query/page semantics require it.
Terminal output must be bounded and coalesced so a burst of worker activity
does not enqueue an unbounded sequence of Rich updates.

The footer and controls remain within the viewport under all supported positive
terminal dimensions, including during loading, errors, deletion, and activity
updates.

### FR-013 — Correct concurrent projection semantics

The optimized path must preserve:

* event ordering and sequence numbers;
* append-only durability and fsync behavior;
* cross-process writer locking;
* current status, phase, error, deletion, lease, and run metadata;
* selection and marked IDs across refreshes; and
* exact-session attach/delete/cancel targeting.

The manager must expose a stale/loading indicator when its projection is behind
the durable change token. It must never silently render a newer count with an
older selected record without indicating the generation mismatch.

### FR-014 — Bounded work and cancellation

Every manager-owned async task must have an owner, cancellation path, done
callback/result observation, and shutdown policy. No task may become an
unobserved exception or continue mutating state after the manager has closed.

Thread adapters must use a bounded executor or shared runtime adapter rather
than creating an unbounded executor per refresh/action. Cancellation must not
claim that an underlying synchronous operation stopped when it may still be
running; durable recovery metadata remains authoritative.

### FR-015 — Non-interactive and API compatibility

`agenthicc agents --json`, `jobs list --json`, direct store consumers, session
service projections, and scriptable commands must retain complete records and
existing field semantics. Pagination is an interactive query optimization, not
a data-loss behavior.

Public compatibility shims may delegate to the new projection/service, but
there must be one source of truth for session state and one owner-safe attach
path.

### FR-016 — Configuration and safe defaults

Expose bounded settings only where operational tuning is useful, for example:

```toml
[background.manager]
refresh_interval_s = 0.25
maintenance_interval_s = 5.0
projection_batch_size = 256
activity_tail_bytes = 64000
frame_debounce_ms = 16
max_in_flight_operations = 4
metrics = false
```

Names and defaults must follow existing configuration conventions. Values must
be validated, bounded, and safe. A user must not be able to disable ownership,
redaction, locking, or durable result recording through performance settings.

## 8. Proposed implementation design

### 8.1 Projection storage options

Phase 0 must benchmark two candidates against the existing event log:

1. a versioned JSON snapshot plus incremental event-tail replay; and
2. a small SQLite-derived index with transactional event-generation updates.

Select the simpler option that meets the budgets while retaining atomic
recovery and portable local installation. The event log remains authoritative.
Do not introduce SQLite merely to move the same full scan to another format.

### 8.2 Manager state split

Separate state into:

```text
durable projection state: generation, counts, page records
reactive UI state: selection, marks, filters, notices, layout, dirty causes
operation state: operation ID, exact target, phase, task handle, result
```

The render function becomes a pure projection of the current reactive state.
It may not call store, process, journal, or filesystem methods.

### 8.3 Scheduler model

Use one manager-owned task group or equivalent lifecycle registry for:

* projection refresh;
* activity read;
* maintenance; and
* user operations.

The scheduler must offer a `request_refresh(cause)` operation that coalesces
causes and a `request_render(cause)` operation that marks the frame dirty. It
must expose a small diagnostic snapshot for tests and `/status`-style tooling:

```text
projection_generation
render_generation
refresh_pending
refresh_in_flight
maintenance_in_flight
activity_in_flight
last_frame_duration_ms
queued_frame_count
```

### 8.4 Store notifications

Prefer a cheap file-generation/change-token check or an existing event-store
subscription over polling the complete registry. Notifications are hints, not
authority; the projection still validates sequence continuity and falls back
to replay/rebuild when a gap is detected.

### 8.5 Action adapters

Each blocking operation should be wrapped once in the canonical async adapter.
The adapter must preserve exception identity/categories, report progress, and
be testable with injected slow/failing implementations. The manager should
never know whether the underlying store uses native async I/O or a bounded
thread adapter.

## 9. Acceptance criteria

### AC-001 — Baseline benchmark exists

A deterministic benchmark fixture creates at least 10,000 sessions,
1,000,000 events, representative artifacts, and configurable active workers.
It records first-frame, key-to-frame, refresh, maintenance, activity, and
action-dispatch timings before and after the implementation.

### AC-002 — First frame is progressive

On the reference fixture, the first useful manager frame meets the p95/p99
targets in §5 without waiting for complete stale-worker recovery or reading
full conversation journals. Loading state and generation metadata are visible
when the full projection is not ready.

### AC-003 — Navigation is independent of store latency

With store reads delayed by at least two seconds, Up/Down/Home/End,
PageUp/PageDown, marking, help, pause, and quit acknowledge within the key
dispatch budget and continue to repaint.

### AC-004 — Large registry does not cause frame stalls

With 10,000 sessions and continuous event appends, p95 and p99 key-to-frame
latency remain within the stated targets. No frame performs a full event-log
fold or full journal parse.

### AC-005 — Maintenance cannot freeze the UI

With thousands of active records and deliberately slow process/command-line
inspection, the manager keeps accepting input. Maintenance is single-flight,
bounded, observable, and eventually reconciles stale workers without duplicate
transitions.

### AC-006 — Durable actions are asynchronous

Slow cancel, archive, restore, approval, input, attach preparation, bulk
actions, refresh, and PRD-208 deletion all show an immediate pending state;
they do not block the event loop, and their results are observed exactly once.

### AC-007 — Activity remains correct and bounded

Changing the selected session or journal while an activity read is pending
cannot display another session's text. Repeated unchanged frames perform no
new journal read or stat. Redaction and bounded tail behavior remain intact.

### AC-008 — Refresh coalescing works

One hundred simultaneous refresh causes produce no more than one in-flight
projection read and one eventual newest-generation frame. Obsolete results
are discarded, and no stale frame overwrites a newer selection or operation.

### AC-009 — Selection and action identity remain safe

When records are inserted, deleted, reordered, or filtered during a pending
refresh, the selected ID remains selected if present. If it disappears,
actions do not target the row that shifted into its former index. Marks remain
exact-ID based.

### AC-010 — JSON and complete listing compatibility

`agents --json`, `jobs list --json`, direct `BackgroundStore.list()`, and
session-service projections return the same complete logical records and
ordering semantics as before the optimization.

### AC-011 — Resize and notice behavior

Repeated resize events and bursts of progress/error notices are debounced and
coalesced. The complete Rich frame remains within the terminal height and the
footer never disappears.

### AC-012 — Shutdown is clean

Closing the manager during projection, activity, maintenance, cancellation,
attach, or deletion leaves no unobserved task, orphaned executor mutation, or
unrestored terminal mode. In-flight durable operations are completed, safely
cancelled, or marked recoverable according to their operation contract.

### AC-013 — Cross-process consistency

A second process appending lifecycle events while the manager is open is
eventually visible without full-log reread on every frame. Sequence gaps,
truncated tails, snapshot corruption, and concurrent writes trigger safe
rebuild/recovery rather than stale or fabricated records.

### AC-014 — Security and privacy are unchanged

Performance instrumentation and projections never expose secrets, prompts,
transcripts, lease tokens, unrestricted artifact paths, or network data. All
existing capability, workspace, ownership, and redaction tests remain green.

### AC-015 — Quality gates

The focused unit, integration, E2E, benchmark, lint, type, type-audit, docs,
and full-suite gates pass. Any pre-existing unrelated failure is recorded with
the exact command, file, and reason; it cannot be attributed to the TUI
optimization without a regression comparison.

## 10. Test and benchmark plan

### Unit tests

Add deterministic tests for:

* snapshot/index versioning, atomic writes, replay, truncation, and corruption;
* incremental event-tail application and sequence-gap fallback;
* page/filter/count query ordering and stable exact-ID selection;
* dirty-cause coalescing and newest-generation result handling;
* activity-read deduplication, cancellation, fingerprint caching, and stale
  selection suppression;
* single-flight maintenance and backoff;
* action adapter dispatch, structured errors, duplicate suppression, and
  shutdown cancellation;
* frame-cache invalidation and no-op rendering; and
* bounded memory/caches and configuration validation.

### Integration tests

Use temporary stores, real JSONL events, slow fake filesystems/process probes,
and a scripted terminal backend to verify:

* first paint while a projection is still loading;
* live appends from a second store instance;
* worker completion/orphan recovery during navigation;
* slow cancellation/archive/restore/attach/delete actions;
* large journals and many sessions;
* event-loop tick frequency during every blocking operation;
* snapshot rebuild after interruption and cross-process lock contention; and
* complete JSON/API parity.

### End-to-end tests

Drive the real `BackgroundManager.run()` with a deterministic backend. Assert
rendered loading/progress/error frames, key responsiveness, stable selection,
eventual state reconciliation, terminal restoration, and clean task shutdown.
Include a load test with at least 10,000 sessions and a burst of lifecycle
events; keep it bounded and deterministic for CI, with a larger optional
performance job for release validation.

### Profiling and regression evidence

The implementation PR must include before/after benchmark output and a short
profile identifying the largest removed hot-path costs. A performance change
is not accepted solely because a single test became faster; it must meet the
latency and correctness budgets under contention.

## 11. Rollout plan

### Phase 0 — Instrument current behavior

Add disabled-by-default stage timings and benchmark fixtures. Capture baseline
numbers for small, medium, and large registries before changing scheduling.

### Phase 1 — Async action boundary

Move every blocking manager action off the event loop, add owned task/result
handling, and prove input remains responsive with slow fakes.

### Phase 2 — Projection/index optimization

Implement and benchmark the selected snapshot/index strategy, incremental
replay, page queries, safe invalidation, and corruption recovery.

### Phase 3 — Scheduler and activity pipeline

Add dirty causes, refresh coalescing, newest-generation wins, async activity
projection, maintenance backoff, and resize/frame debouncing.

### Phase 4 — Load validation and documentation

Run the large-store benchmark, full test matrix, failure/restart scenarios,
and update architecture/storage/background-session documentation with the new
ownership and performance contracts.

The rollout must be reversible through a feature flag or compatibility mode
until snapshot/index replay and cross-process recovery have passed production
fixtures.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Snapshot/index diverges from the event log | Versioned atomic snapshot, sequence continuity checks, and full replay fallback |
| Background refresh shows stale state | Generation/change-token indicator and newest-result-wins reconciliation |
| Async adapter leaks work after shutdown | One owner registry, bounded executor, observed results, and durable recovery state |
| More concurrency causes duplicate actions | Exact IDs, operation IDs, single-flight guards, and lease checks |
| Incremental index hides newly created sessions | Cross-process change token, tail replay, and periodic authoritative reconciliation |
| Activity text appears under the wrong row | Session/generation tagged activity requests and stale-result discard |
| Performance metrics leak user data | Numeric timings and bounded IDs/categories only; redact all text/path fields |
| Lower refresh frequency feels stale | Event hints plus configurable cadence; show projection age/generation visibly |
| Optimization adds too much architecture | Benchmark JSON snapshot and SQLite candidates; select the smallest contract meeting SLOs |
| Existing callers depend on synchronous APIs | Keep compatibility adapters and make only the interactive path async-first |

## 13. Definition of done

The PRD is implemented when `agenthicc agents` can open and navigate a large,
actively changing background-session registry without perceptible input lag;
all durable actions are non-blocking and observable; projection and activity
work is incremental, cached, and generation-safe; first paint, key dispatch,
refresh, maintenance, and frame budgets are proven by deterministic
benchmarks; JSON/API semantics, leases, redaction, deletion recovery,
selection identity, and terminal cleanup remain correct; and the complete
verification matrix plus updated architecture and operational documentation
passes.

## 14. Implementation evidence

Implemented on 2026-09-30. `BackgroundStore` now maintains a versioned,
mode-0600 atomic JSON projection beside the authoritative event log, replays
only appended complete JSONL records when possible, and batches cold-replay
activity heartbeats so a session object is not reconstructed for each
`last_active` update. Projection pages carry a generation/stale marker and
maintenance scans use stable bounded session-ID pages. `BackgroundManager`
uses a bounded owned service for projection reads, activity tails, maintenance,
and durable actions; its first frame is a loading frame rather than a blocking
registry scan. Activity text is normalized and cropped to one physical line,
with the `Updated` timestamp placed ahead of it so long or multiline agent
responses cannot push the timestamp out of the details viewport. Delete remains
confirmation-free and is never routed through foreground attachment.

### Candidate and latency measurements

The deterministic fixture was generated with 10,000 sessions, 1,000,000
lifecycle events, 32 active workers, Python 3.13.13, 8 reported CPUs, and a
120×25 terminal. The pre-batched replay and final implementation were measured
on this host with the same benchmark script:

| Measurement | Before heartbeat batching | Final implementation |
|---|---:|---:|
| Cold 10k/1m projection | 104,573 ms | 15,331 ms |
| First useful frame | 36.1 ms | 60.1 ms |
| Key-to-render p95 / p99 | 1.055 / 1.496 ms | 0.494 / 0.629 ms |
| Warm page refresh p95 | 0.040 ms | 0.031 ms |
| Incremental refresh p95 | 0.848 ms | 0.317 ms |
| Activity-tail read | 8.6 ms | 3.9 ms |
| Action dispatch | 0.177 ms | 0.146 ms |

Cold projection construction remains off the UI loop and visibly reports
`loading session index…`; its duration is not represented as an interactive
key/frame delay. The profile that motivated heartbeat batching used 100,000
events under `cProfile`: `_apply_event` consumed 34.7 s cumulative out of
39.9 s, and `BackgroundSession.evolve` consumed 28.7 s cumulative. Deferring
the common heartbeat update until the next non-heartbeat event (or end of
replay) removed repeated full-record normalization while preserving
per-session event order; all other updates still use `evolve`.

The benchmark offers `--compare-sqlite`, an offline transactional
materialized-projection prototype using the same create/heartbeat fixture. At
1,000 sessions and 100,000 events, the JSON projection took 1,922 ms to cold
replay; the SQLite prototype took 4,566 ms to import/replay/index and 0.45 ms
for a page-plus-count query. The JSON projection's warm page query was 0.02 ms.
This prototype is intentionally not production storage code; JSON was selected
because it is simpler, faster for this workload, and meets the interactive
budgets while retaining the JSONL event log as the sole authority.

### Verification

The 10k/1m opt-in E2E load gate passed after the final replay optimization.
Focused regressions cover long activity text and visible `Updated`, projection
reopen/tail replay/corruption/partial writes, same-size rewrites, bounded
maintenance pages, stale generations, configuration bounds, deletion without
confirmation, no foreground-attach call on delete, and key handling during a
slow projection read. Lint, touched-file formatting, the type-safety audit,
changed-source mypy, strict MkDocs build, and public-symbol documentation
checks passed. The complete suite passed with 4,019 tests and 16 skips. The
package build and both wheel/sdist `twine check` validations passed; the nox
`build_check` wrapper could not locate twine in its environment, so the check
was run with `uv run --with twine twine check dist/*`. The strict docs build
reported only the existing unnav-listed `guides/question-timeouts.md` page and
an upstream MkDocs Material warning. The changed-source mypy check passed.
`uv run mypy src/agenthicc` still reports 250 existing errors in these
untouched files: `cli/commands/session_service.py`, `memory/compactor.py`,
`runners/agent_turn.py`, `runners/process_lease.py`,
`tools/fs/agent_tools.py`, `tools/workspace_access.py`,
`tui/workspace/overlays/approval.py`, `workflows/copy_website/runner.py`,
`workflows/make_agenthicc_tool/runner.py`, `workflows/make_book/runner.py`,
`workflows/name_that_ui.py`, `workflows/reconstruct_site/phase_impl.py`,
`workflows/reconstruct_site/runner.py`, and
`workflows/site_imitate/runner.py`. The full
`uv run ruff format --check src/ tests/ scripts/` gate likewise identifies
only these untouched formatting-baseline files: `cli/commands/mcp.py`,
`cli/mcp_config.py`, `tools/mcp.py`, `workflows/copy_website/runner.py`,
`workflows/make_book/build_book.py`, `tests/e2e/test_mcp_session_lifecycle_e2e.py`,
`tests/integration/test_mcp_manager_integration.py`, and
`tests/unit/test_mcp_config.py`. The isolated deletion, external-tail-replay,
and PTY regressions also passed on their direct rerun.
