# PRD-208 — Reliable, Responsive `CTRL+X` Deletion in `agenthicc agents`

**Status:** Implemented  
**Type:** Bug fix / TUI reliability / Background-session lifecycle  
**Repository:** `jymchng/agenthicc`  
**Date:** 2026-09-29  
**Related work:** PRD-141, PRD-148, PRD-149, PRD-151, PRD-171, PRD-206, PRD-207

## 1. Summary

In the `agenthicc agents` interactive session manager, pressing `CTRL+X` can
appear to hang and the selected background session may remain visible instead
of being deleted. The current implementation has a partially asynchronous
deletion path, but the lifecycle is not integrated with the TUI event loop or
the durable background-session state machine.

This PRD defines a reliable deletion flow that:

1. recognizes `CTRL+X` consistently on POSIX and Windows terminals;
2. dispatches deletion immediately, without a confirmation prompt or second key;
3. targets immutable session IDs captured at dispatch time;
4. starts deletion without blocking keyboard input or rendering;
5. persists an in-progress deletion state so restart/recovery is safe;
6. moves artifacts to recoverable trash exactly once;
7. reports completion or failure promptly;
8. cleans up tasks and leases when the manager exits; and
9. proves the behavior through unit, integration, terminal, and end-to-end
   tests using the real interactive manager loop.

The desired user experience is:

```text
CTRL+X
  ↓ immediately
Deleting 1 session…
  ↓
success: row disappears and the next stable selection is shown
failure: row remains with an actionable error
```

`CTRL+X` remains a recoverable move-to-trash operation, not irreversible
purging. Permanent removal remains a separate administrative operation.

## 2. User-visible defect

Observed behavior:

```text
Background Sessions · page 1/1 · showing 1–9 of 9
...
Ctrl+X delete
```

After `CTRL+X`, the user can experience one or more of the following:

* no obvious deletion progress state;
* a deletion request that appears to stop responding while artifacts are moved;
* a selected row that remains on screen for a long time;
* a cancelled/orphaned/completed session that still appears in the list;
* an `agenthicc` process that remains busy after leaving the manager;
* deletion that never completes when the session has a large artifact directory
  or an active worker process;
* different behavior in tests/non-interactive mode and the real TUI.

The issue is especially confusing when synchronous cancellation or artifact
movement runs on the TUI path. The user cannot distinguish “deletion is
running” from “deletion failed” unless the operation has an explicit durable
phase and the screen repaints while it progresses.

## 3. Investigation findings

### 3.1 Key decoding is not the primary defect

The terminal input layer already defines `Key.CTRL_X` and maps the POSIX byte
`0x18` to it. The Windows backend also maps its control-key result to
`Key.CTRL_X`. Existing decoder tests cover the byte mapping.

The implementation must still add an end-to-end assertion that the actual
backend event reaches the `agenthicc agents` manager, because decoder unit
tests alone do not prove that the live event loop dispatches it.

### 3.2 `CTRL+X` was unnecessarily confirmation-gated

The previous `BackgroundManager.handle_key()` first set:

```python
self.pending_delete = True
self.pending_delete_ids = (...session IDs...)
```

It returned no action. Only a subsequent `y` or Enter called
`_confirm_delete()`. That extra modal state was the wrong interaction for this
recoverable move-to-trash operation: it delayed the requested action, made the
screen appear unchanged, and created an additional path that could be lost on
input interruption. The implemented product decision is to remove this
confirmation gate. `CTRL+X` now snapshots the targets and starts the durable
async operation in the same event-loop turn.

### 3.3 The interactive deletion path uses an unmanaged daemon thread

When `_interactive_delete` is true, `_confirm_delete()` starts a raw
`threading.Thread` and returns immediately. The thread calls the synchronous
`BackgroundSupervisor.delete()` method. Completion is discovered later by
polling `_deletion_thread` from the TUI loop.

This has several weaknesses:

* the operation is not represented as an `asyncio.Task` owned by the manager;
* the manager cannot await, cancel, or drain it during shutdown;
* a daemon thread may be terminated with the process while deletion is in
  progress;
* exceptions are converted to strings inside a worker thread without a
  structured lifecycle result;
* there is no durable `deleting` state between the key press and the final
  tombstone;
* there is no operation ID or idempotency record for recovery after a crash;
* the visible row remains in the ordinary state until the whole synchronous
  operation finishes.

### 3.4 The synchronous delete operation can legitimately take a long time

`BackgroundSupervisor.delete()` performs two potentially slow operations:

1. For an active session, it calls `cancel()`, which signals the worker and
   waits in a loop for up to `cancel_grace_s` using `time.sleep()`.
2. It calls `BackgroundStore.delete()`, which moves the session artifact tree
   and related kernel files into recoverable trash using synchronous filesystem
   operations before appending the deletion event.

Large transcripts, screenshots, browser artifacts, generated books, or an
unresponsive worker can therefore make one deletion take seconds or much
longer. Running this in a raw thread prevents the main thread from blocking in
the common case, but does not give the user progress, timeout, cancellation,
or recovery semantics.

### 3.5 The TUI still performs synchronous store work during the poll loop

The manager refreshes its list and maintenance state while it waits for input.
`BackgroundStore.list()` folds JSONL state and performs filesystem reads. The
current store and supervisor APIs are synchronous. A slow read, lock, or
artifact operation can therefore still delay repainting and input handling.

PRD-207 defines the repository-wide async direction. PRD-208 requires the
deletion path to adopt that contract immediately or use the shared bounded
adapter introduced by PRD-207; it must not add another ad-hoc thread model.

### 3.6 Existing tests provide incomplete evidence

Current tests prove useful pieces:

* raw `0x18` decoding returns `Key.CTRL_X`;
* direct `handle_key(Key.CTRL_X)` starts deletion without a second key;
* a mocked slow delete does not block the direct deletion handler;
* synchronous/non-interactive deletion can tombstone and restore a session.

They do not prove:

* a real interactive manager receives `CTRL+X` and completion;
* the manager remains responsive while active cancellation and artifact moves
  are running;
* the operation has a visible in-progress state;
* a deletion task is awaited or cancelled on manager shutdown;
* a process restart can recover an interrupted deletion;
* one session is deleted exactly once when refreshes or repeated keys occur;
* failures leave the record and artifacts recoverable;
* a large artifact tree does not block the TUI event loop.

## 4. Goals

### G1 — Immediate, unambiguous interaction

`CTRL+X` must dispatch deletion within one TUI iteration and the next render
must show that deletion is running. No confirmation key or modal state is
required.

### G2 — Non-blocking deletion

Deletion must not block the TUI event loop, keyboard input, animation, or
rendering. It must use the async lifecycle contract from PRD-207, with a bounded
adapter only where the underlying filesystem/process API is still synchronous.

### G3 — Durable and recoverable lifecycle

Deletion must be represented durably from intent through completion/failure so a
restart cannot lose track of an operation or silently resurrect/delete the
wrong session.

### G4 — Exact-target safety

The session IDs selected at dispatch time are the only deletion targets.
Refreshes, pagination, status changes, and row removal must never retarget the
operation to a neighboring session.

### G5 — Idempotency and safe retries

Repeated `CTRL+X`, refreshes, and recovery attempts
must not move artifacts twice, append duplicate terminal deletion events, or
delete another session.

### G6 — Responsive failure handling

Worker cancellation timeout, filesystem failure, lock contention, malformed
trash metadata, and process interruption must result in an actionable error and
preserve recoverable state.

### G7 — Cross-platform behavior

POSIX and Windows key decoding and interactive manager behavior must follow the
same state machine.

## 5. Non-goals

This PRD does not:

* make `CTRL+X` permanently purge data;
* require a second confirmation key for the recoverable `CTRL+X` operation;
* delete arbitrary files outside the exact session artifact/trash paths;
* change the semantics of `jobs delete` except to share the reliable lifecycle;
* redesign the entire background-session scheduler;
* allow two owners to delete or mutate the same session concurrently;
* hide a failed delete by removing the row optimistically and discarding the
  durable error;
* replace process-level and workspace capability enforcement.

## 6. Proposed state machine

### 6.1 UI state

The manager must distinguish these states:

```text
IDLE
  ├── CTRL+X ──> DELETING(operation ID, target IDs)
  └── normal input

DELETING(operation ID, target IDs)
  ├── completion ──> IDLE + refresh + success notice
  ├── failure ─────> DELETE_FAILED + visible error
  ├── Ctrl+C/Esc ──> cancellation policy / leave operation recoverable
  └── repeated delete -> ignored, never retargeted

DELETE_FAILED
  ├── r ───────────> retry same exact target only after explicit action
  ├── u/t ─────────> inspect/restore according to current trash state
  └── normal navigation
```

`CTRL+C` must continue to exit the agents screen even during deletion, but
deletion, but exiting must first apply the operation shutdown policy described
below. It must not silently abandon an untracked deletion.

### 6.2 Durable lifecycle

The background session event stream must record an idempotent operation boundary:

```text
delete_requested
  → delete_started
  → delete_cancel_requested       (if worker is active)
  → artifacts_moved_to_trash
  → deleted                         (terminal tombstone)
```

Failure branches:

```text
delete_requested / delete_started
  → delete_failed (error, retryable, operation ID)

cancel timeout
  → delete_failed (worker still active, no artifact move)

artifact move partial failure
  → delete_failed (reconciliation metadata preserved)
```

The exact event names may follow current event conventions, but the durable
state must identify:

* `operation_id`;
* exact target `session_id` or sorted target IDs;
* request timestamp and owner/process identity;
* current phase and last progress message;
* retry count and idempotency key;
* artifact source and trash destination where known;
* terminal success/failure and recovery instructions.

A new enum value such as `DELETING` is acceptable if it is compatible with
existing list/projection behavior. If a new status is not desirable, equivalent
operation metadata must prevent the session from appearing as an ordinary
deletable record while work is active.

## 7. Functional requirements

### FR-001 — Canonical control-key normalization

Use one canonical path for `Key.CTRL_X` from POSIX bytes, Windows console input,
and test/injected keys. The manager must not depend on the character argument
being populated when the enum value is present, and must accept `"CTRL_X"` only
through the same normalized path.

Add tests for:

* POSIX `0x18`;
* Windows control-key decoding;
* enum, string, and character injection;
* Ctrl+C while deletion is in progress.

### FR-002 — Immediate deletion dispatch

When `CTRL+X` is pressed with a selected session or marked set:

* capture exact IDs immediately;
* create an operation ID and start the deletion task synchronously from the
  key handler;
* invalidate the render cache;
* render a dedicated deleting marker/banner with target count and the current
  operation phase;
* call the canonical async deletion service, never a blocking delete on the
  event-loop thread;
* do not refresh selection in a way that changes target IDs.

If no session is selected, show a non-error “no session selected” notice and do
not start an operation.

### FR-003 — Exact target snapshot

At the moment deletion dispatches, persist/carry an immutable deletion request:

```text
operation_id
target_session_ids
selected_workspace/project
requesting_ui/session ID
```

After this point, list refreshes, pagination, sort order, row removal, or
selection movement must not alter the request. Bulk marked deletion must use a
stable, de-duplicated, deterministic ID tuple.

### FR-004 — Async deletion dispatch

The `CTRL+X` handler must schedule an owned async deletion task and return
control to the TUI immediately. The task must not be a detached raw daemon
thread. It must be tracked in manager-owned state and expose completion,
exception, and cancellation state. No second confirmation key is accepted or
required.

The implementation may initially call synchronous legacy supervisor/store APIs
through the shared bounded async adapter, but direct blocking calls on the TUI
event-loop thread are forbidden.

### FR-005 — Async background deletion API

Add an async supervisor operation, for example:

```python
async def delete_async(
    self,
    session_ids: tuple[str, ...],
    *,
    operation_id: str,
) -> DeleteResult: ...
```

It must:

* validate all targets before mutation;
* enforce session ownership/lease rules;
* cancel active workers with an async, deadline-aware wait;
* stop only terminals associated with exact target sessions;
* move artifacts using the async persistence/filesystem boundary;
* append one idempotent tombstone per target;
* return structured per-target results;
* preserve partial progress and retry metadata.

Single-session legacy methods may delegate to this operation during migration,
but the TUI must use the async API.

### FR-006 — Visible progress

While deletion runs, the manager must show:

* target count and short titles;
* current operation phase (`stopping worker`, `moving artifacts`, `recording
  tombstone`, or equivalent);
* elapsed time or a bounded progress indicator;
* that navigation remains available where safe;
* the exact operation is not being retargeted.

The existing row may remain visible with a deleting marker, or may be removed
from the normal projection only if the durable operation remains inspectable.
It must never look like an idle unchanged session.

### FR-007 — Completion reconciliation

When the task completes, schedule/perform a non-blocking projection refresh:

* successful targets disappear from the default list;
* marked IDs are cleared only for successful targets;
* selection remains on the same ID when it still exists, otherwise moves to the
  nearest deterministic neighbor;
* include-deleted/trash view can show the tombstone;
* a concise success notice includes count and operation ID;
* partial success reports each failed target without hiding successful ones.

### FR-008 — Structured failure handling

Failures must be captured as typed/structured results, not only printed from a
worker thread. The UI must distinguish:

* target not found/already deleted (idempotent success or explicit no-op);
* active worker did not stop before deadline;
* lock/lease conflict;
* artifact move failure;
* malformed/missing trash manifest;
* permission/disk-full error;
* unexpected internal failure.

The failure must preserve the session and enough metadata for retry or manual
recovery. It must not make the TUI task exception unobserved.

### FR-009 — Idempotent repeated input

While deleting:

* repeated Ctrl+X does not enqueue another operation;
* refresh does not duplicate deletion events;
* reopening `agenthicc agents` observes the durable operation and reconciles it;
* retry uses the same target identity but a new attempt record linked to the
  original operation.

### FR-010 — Shutdown and Ctrl+C policy

On manager exit while deletion is active, the manager must:

1. stop accepting new deletion requests;
2. cancel or shield the current operation according to its commit phase;
3. await it up to a bounded shutdown deadline;
4. persist `delete_failed`/`delete_recovery_required` if it cannot finish;
5. release all locks and task references;
6. restore the terminal state.

It must not leave a daemon thread mutating session state after the TUI has
returned, and it must not claim deletion succeeded merely because the screen
closed.

### FR-011 — Recovery on next open

When `agenthicc agents` opens, it must reconcile pending deletion operations:

* completed tombstone + artifacts in trash → mark operation complete;
* operation started but no tombstone → inspect exact source/trash paths and
  resume or report recovery-required;
* active owner/process still alive → show deleting/owned state and do not race;
* stale owner → recover according to existing lease policy;
* malformed metadata → preserve artifacts and show a manual-recovery error.

Recovery must never infer a target from the current selected row.

### FR-012 — Async/non-interactive parity

The CLI `jobs delete` command, TUI `CTRL+X`, bulk deletion, and future session
service deletion must share the canonical deletion service and event contract.
Only presentation differs. Tests must verify equivalent durable
outcomes.

### FR-013 — Safe artifact and trash semantics

Deletion continues to use recoverable trash. The implementation must validate:

* artifact source is the exact persisted path for the target;
* trash destination is a dedicated direct child under the configured trash
  root;
* no path traversal or broad recursive deletion is possible;
* source and destination behavior is idempotent after interruption;
* restore can reverse a completed delete without losing metadata.

### FR-014 — Observability

Emit redacted structured events/metrics for operation ID, target count,
queue/start/finish time, phase, duration, success/failure, cancellation, and
recovery. Never log prompts, transcript contents, API keys, or unrestricted
artifact paths where current privacy policy forbids them.

## 8. Implementation design

### 8.1 Recommended ownership model

```text
BackgroundManager (TUI projection/input)
        │ owns task handles and presentation state
        ▼
AsyncDeletionService / BackgroundSupervisor.delete_async
        │ owns lifecycle, deadline, lease, and operation result
        ▼
Async BackgroundStore
        │ owns durable event and artifact transaction
        ▼
filesystem/process adapter
```

The TUI must not implement artifact movement, worker cancellation, or durable
event sequencing itself. It submits an exact request and renders the result.

### 8.2 Operation object

Use a typed request/result model rather than shared mutable fields such as
`_deletion_result`:

```python
@dataclass(frozen=True)
class DeleteRequest:
    operation_id: str
    session_ids: tuple[str, ...]
    requested_by: str

@dataclass(frozen=True)
class DeleteResult:
    operation_id: str
    completed: tuple[str, ...]
    failed: tuple[DeleteFailure, ...]
    phase: str
    recoverable: bool
```

Progress can use a bounded async queue or callback owned by the manager. The
queue must not allow a slow render consumer to block deletion.

### 8.3 Async migration path

If PRD-207's full store migration is not complete, implement the first version
with the shared bounded I/O adapter:

```python
result = await io_executor.run(
    "background.delete",
    supervisor.delete_sync,
    request,
)
```

This is transitional. The adapter must have an explicit timeout, cancellation
policy, and shutdown drain. A new one-off `threading.Thread` is not acceptable.

### 8.4 Active worker cancellation

Cancellation must no longer use a blocking `time.sleep()` loop in an async
path. Use monotonic deadlines and async polling/process wait. If the worker is
still alive at the deadline, do not move artifacts or append a successful
tombstone; return a recoverable failure with the worker identity.

### 8.5 Cache invalidation

On every state change affecting the table, deleting marker,
success, or failure:

* invalidate the manager render key;
* do not reuse a cached renderable with stale deletion state;
* keep durable deletion progress separate from transcript/activity caching.

## 9. Acceptance criteria

### AC-001 — Key delivery

On POSIX and Windows test backends, pressing `CTRL+X` in `agenthicc agents`
starts deletion within one event-loop turn. The key is not swallowed,
interpreted as ordinary `x`, delayed until another key arrives, or converted
into a confirmation prompt.

### AC-002 — Immediate deletion clarity

The rendered manager clearly states that deletion is running, including the
target count and current phase. No confirmation key is required, and a second
`CTRL+X` is ignored while the operation is active.

### AC-003 — No event-loop blocking

With a deletion operation deliberately delayed for at least two seconds, the
interactive manager continues to accept input and refresh/render. A measured
key-to-dispatch latency regression is not introduced.

### AC-004 — Actual interactive sequence

An integration/E2E test drives the real manager loop with:

```text
CTRL+X → delayed delete completion
```

and proves that the exact target eventually becomes deleted and disappears from
the default list.

### AC-005 — Large artifacts

A session with a large artifact tree is deleted without blocking the TUI. The
UI shows progress and the operation completes or reports a bounded failure.

### AC-006 — Active worker

Deleting a running session asynchronously requests cancellation, waits using a
deadline, and only moves artifacts after the worker is confirmed stopped. A
worker that does not stop remains recoverable and is never falsely marked
deleted.

### AC-007 — Exact target

After Ctrl+X captures session A, removing/reordering/refreshing the list or
moving selection cannot cause session B to be deleted. Bulk target IDs are
stable and de-duplicated.

### AC-008 — Idempotency

Repeated Ctrl+X, refresh ticks, and reopening the manager produce one
terminal deletion result and no duplicate artifact moves or tombstones.

### AC-009 — Failure visibility

Injected lock, permission, disk, malformed metadata, and worker-timeout errors
remain visible in the manager, preserve recovery metadata, and do not crash or
hang the TUI.

### AC-010 — Shutdown

Exiting with Ctrl+C/Esc while deletion is active leaves no unobserved daemon
thread or unowned task. The operation is completed, cancelled at a safe point,
or durably marked recovery-required before the manager returns.

### AC-011 — Recovery

Restarting `agenthicc agents` after interruption during each deletion phase
reconciles source artifacts, trash metadata, tombstone, operation owner, and
session projection without data loss or wrong-target deletion.

### AC-012 — CLI parity

`agenthicc jobs delete SESSION_ID` and TUI deletion share the same durable
operation semantics, recoverable trash behavior, and idempotent result.

### AC-013 — Cross-platform tests

The key mapping, immediate-dispatch state machine, and async deletion contract pass
on supported POSIX and Windows backends, with platform-specific filesystem or
process behavior isolated in adapters.

### AC-014 — Quality gates

Focused unit/integration/E2E tests pass, followed by the repository's lint,
format, type, full test, coverage, and documentation gates. Existing background
session, terminal, session ownership, and trash-restore tests remain green.

## 10. Test plan

### Unit tests

Add coverage for:

* normalized Ctrl+X events from enum/string/raw character;
* every UI state transition and invalid key;
* immutable exact-target capture and bulk de-duplication;
* render-cache invalidation for deleting/success/failure;
* async task registration, completion, exception, cancellation, and duplicate
  suppression;
* structured delete results and failure categories;
* selection reconciliation after successful/partial deletion;
* safe trash path validation and idempotent recovery;
* Ctrl+C/Esc shutdown behavior.

### Integration tests

Use temporary stores and repositories to verify:

* `BackgroundManager` + real `BackgroundStore` + fake delayed supervisor;
* active worker cancellation and timeout;
* artifact moves with large trees;
* partial bulk deletion;
* lock contention and restart/reconciliation;
* direct CLI and TUI durable parity;
* a real asyncio loop remains schedulable during deletion.

### E2E tests

Drive `run_background_manager()` with a scripted terminal backend or PTY. The
test must use the actual `run()` method rather than only calling
`handle_key()` directly. It must assert rendered states, input responsiveness,
durable events, final list contents, and clean shutdown.

### Regression test for the reported defect

At minimum, add a test named along the lines of:

```text
test_agents_ctrl_x_deletes_selected_session_without_confirmation_or_hanging
```

It must fail against the old unmanaged-thread/ambiguous-state behavior and pass
only when the real interactive lifecycle is fixed.

## 11. Rollout plan

### Phase 1 — Reproduce and instrument

* Add real-loop scripted input test.
* Add operation IDs, state logging, and timing metrics.
* Verify key delivery separately from deletion latency.

### Phase 2 — Async operation contract

* Add typed request/result models.
* Add `delete_async` through the PRD-207 adapter or native async store.
* Remove raw daemon deletion thread.

### Phase 3 — Durable recovery

* Add operation events/state and exact-target reconciliation.
* Add active-worker timeout and artifact transaction tests.

### Phase 4 — UI and shutdown

* Add explicit deleting/failure presentations.
* Track and drain tasks on exit.
* Verify Ctrl+C/Esc behavior and terminal restoration.

### Phase 5 — CLI parity and release gates

* Route CLI delete through the same service.
* Run full quality matrix and document the behavior.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Users need an undo path | Keep `CTRL+X` recoverable via trash and retain restore controls; do not add a blocking confirmation modal |
| Artifact move exceeds user patience | Show progress, keep operation async, provide bounded recovery state |
| Worker remains alive | Do not move/tombstone; show timeout and preserve target |
| Process exits during deletion | Durable operation record and startup reconciliation |
| Partial bulk deletion | Per-target results and retry exact failures only |
| Wrong row deleted after refresh | Immutable target IDs, never current index |
| Duplicate delete requests | Operation ID/idempotency key and state guard |
| Async adapter thread cannot be force-killed | Commit-point semantics and shutdown deadline; report recovery-required |
| Trash metadata corruption | Preserve source/trash paths, fail closed, manual recovery notice |
| TUI render cache hides state | Invalidate cache on every operation transition |

## 13. Definition of done

The defect is fixed when a user can open `agenthicc agents`, select a session,
press `CTRL+X`, continue using the TUI while deletion is
running, and observe the exact session leave the default list after a durable,
recoverable move to trash. Slow workers, large artifacts, repeated keys,
refreshes, process interruption, and shutdown must produce safe, visible,
recoverable outcomes rather than a hang or silent no-op.
