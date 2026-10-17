---
title: "PRD-245: Recoverable Background Handoff After Abrupt Process Exit"
status: Implemented
version: 1.0.0
date: 2026-10-17
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-141, prd-171, prd-204, prd-218, prd-240, prd-241]
tags: [background-sessions, detach, recovery, ownership, configuration]
---

# PRD-245 — Recoverable Background Handoff After Abrupt Process Exit

## 1. Executive summary

After the process running a session is killed, the user can run
`agenthicc --continue`, change the model/configuration, and then issue
`/detach`. The TUI reports:

```text
Unable to background session: InvalidSessionTransition: Session is already
managed by a background worker
```

The error is raised by `BackgroundSupervisor.handoff()` when the persisted
background-session record for the same session has any active status. This
guard does not check whether the recorded worker process is still alive or
whether its session-owner lease is still valid.

The reported sequence is consistent with two independent durable ownership
records getting out of sync after an abrupt kill:

```text
session-owner lease: old process is dead → --continue safely reclaims it
background record:   status remains RUNNING → /detach rejects the session
```

The exact stored PID/status for the reported session was not supplied, so the
incident-specific stale record cannot be independently confirmed. The code
path that produces the message is confirmed, and it can produce this failure
after a killed worker because stale-background reconciliation is not invoked
by `handoff()` or the normal `--continue` session selection path.

This PRD proposes exact-session, owner-fenced stale-worker reconciliation
before rejecting a handoff; idempotent handling of repeated detach requests;
actionable behavior when a worker is live or unverifiable; and preservation of
the foreground session's configuration source so detaching after a model
configuration change does not silently launch with a different configuration.

## 2. Incident sequence

The reported user journey is:

```text
1. A process running the session is killed abruptly.
2. User runs `agenthicc --continue`.
3. User changes the model/configuration.
4. User runs `/detach`.
5. TUI says the session is already managed by a background worker.
```

An abrupt termination can bypass the normal worker finalizer that would write a
terminal or orphaned status. The durable `BackgroundSession` record can
therefore retain `RUNNING`, `WAITING_INPUT`, or another active state and its
last worker metadata after the process has disappeared.

## 3. Source investigation and confirmed cause

### 3.1 The user-visible message originates in the foreground handoff bridge

`src/agenthicc/background/integration.py::_handoff()` is the code that prints
the exact `Unable to background session: ...` message. It calls
`BackgroundSupervisor.handoff()` for the current TUI session ID. The exception
is caught and rendered, so the TUI remains open and no new background attempt
is started.

### 3.2 Handoff rejects persisted active status without checking liveness

In `src/agenthicc/background/supervisor.py::handoff()`, Agenthicc loads the
existing record and immediately raises
`InvalidSessionTransition("Session is already managed by a background
worker")` when `existing.status in ACTIVE_STATUSES`.

This branch runs before a new `BackgroundRequest` is written or started. It
does not call `_worker_process_state()`, inspect the recorded worker PID, or
reconcile the record through `BackgroundStore.mark_orphaned()`.

Therefore this exact error means a background record for the current session
was found with an active lifecycle status. It does not, by itself, prove that
a live worker still owns the session.

### 3.3 `--continue` and background-record recovery are separate mechanisms

The `--continue` path in `src/agenthicc/runners/tui_session.py` selects the
latest session and acquires its `SessionOwnerLease` through
`SessionOpenCoordinator`. The owner lease detects and reclaims a dead prior
session owner. It does not update the distinct background-session lifecycle
record held by `BackgroundStore`.

This separation allows the foreground TUI to reopen a session while its
background record still says `RUNNING`. The TUI's current owner lease is
evidence about session ownership; the background status is a separate
projection and must be reconciled against its own worker identity.

### 3.4 Stale-worker reconciliation exists but is not on this path

`BackgroundSupervisor._recover_stale_records()` checks worker process
identity, including dead and mismatched PID cases, and can move a stale active
record to `ORPHANED` using expected status, attempt, lease token, and activity
timestamp. `recover_stale()` invokes that reconciliation. The manager invokes
maintenance while it is open, but `handoff()` does not reconcile its exact
target before applying the unconditional active-status rejection. A direct
`--continue` followed by `/detach` can therefore miss recovery that would occur
if the user opened/refreshed the background manager first.

The implementation intentionally does not treat a delayed heartbeat alone as
proof that a worker is dead. The fix must preserve this fail-closed behavior:
only verified process death or identity mismatch, with appropriate lease and
attempt fencing, may make an active record eligible for reuse.

### 3.5 Changing the model is not the cause of this exception

The active-status guard runs before `BackgroundRequest` construction, so it
does not inspect or reject a provider or model. The model change is therefore
not the cause of this specific exception.

It does expose a related handoff correctness issue: `_handoff()` currently
passes `config_path=None` to `BackgroundSupervisor.handoff()` even though
`SessionContext` retains the path used to construct the foreground runtime.
When the foreground was started with a non-default config path, the eventual
background worker may load a different config (and therefore a different
provider/model) after the stale-record error is fixed. This PRD includes
preserving the selected configuration source and explicit overrides as part of
a correct handoff.

## 4. Problem statement

Agenthicc has two related but separate authorities:

```text
SessionOpenCoordinator       BackgroundStore / BackgroundSupervisor
foreground-vs-worker lease   worker lifecycle status and attempt metadata
```

The system currently allows the first authority to recover after process death
without synchronously reconciling the second. The foreground handoff then
interprets a stale `RUNNING` projection as definitive proof that another worker
still manages the session. Users receive a generic transition error and must
discover the unrelated manager-refresh workaround, even when the former worker
is already dead.

At the same time, simply ignoring the active status would be unsafe: a truly
live worker must never race a foreground TUI or a second background attempt on
the same journals, checkpoints, tools, or workspace.

## 5. Goals

1. Allow `/detach` to recover the same session after the previous worker is
   verifiably dead, without requiring an intermediate manager visit.
2. Never start a second worker when a verified worker still owns the session.
3. Reconcile only the exact session being handed off, using compare-and-set
   fencing over status, attempt, worker lease token, and worker identity.
4. Preserve the existing two-stage handoff ordering: persist the request,
   release the foreground lease, then allow the worker to claim the session.
5. Make repeated `/detach`, `/bg`, or `/background` submissions during one
   handoff idempotent and avoid duplicate workers or misleading errors.
6. Preserve the configuration source and runtime overrides used by the
   foreground session, including a non-default config path, so the detached
   worker uses the intended provider/model configuration.
7. Give actionable, distinct outcomes for recovered-stale, already-running,
   in-progress-dispatch, and unverifiable ownership states.
8. Retain prior attempt history and the same durable session ID; never create a
   parallel session merely to bypass the conflict.

## 6. Non-goals

- Killing a worker solely because its heartbeat is delayed.
- Taking over a verified live worker without explicit cancellation/foreground
  takeover semantics.
- Weakening `SessionOpenCoordinator` or allowing multiple owners for one
  session.
- Resetting or replaying a workflow from INIT to make a handoff succeed.
- Replacing the background lifecycle state machine or its audit history.
- Changing how `/model` works or claiming it hot-swaps the current runtime; the
  existing `/model` guidance says a restart is required.
- Automatically changing the selected model or provider during recovery.

## 7. Proposed behavior

### 7.1 Typed handoff resolution

Replace the single active-status exception as the only outcome with an explicit
handoff resolution. The exact public type and naming are implementation
decisions, but the result must distinguish at least:

```text
prepared / started
recovered stale worker and prepared / started
already managed by a verified live worker
dispatch or claim still in progress
ownership cannot be safely verified
```

The CLI/TUI layer should render user-facing messages from these typed outcomes
rather than parsing exception strings.

### 7.2 Exact-target reconciliation before rejecting

For a session whose background record is active:

1. Load the exact record and capture its `status`, `attempt`, worker PID,
   worker lease token, dispatch state/token, and last activity timestamp.
2. Inspect process identity with the existing `_worker_process_state()`
   contract. Do not rely on PID existence alone when the platform can verify
   the process command/start identity.
3. Confirm the current caller owns the foreground session lease for this same
   session. A stale worker record is recoverable only when the current
   foreground owner and background worker cannot both be valid owners.
4. If the worker is conclusively dead or mismatched, mark the exact captured
   attempt orphaned using compare-and-set expectations. Re-read the record
   after the transition and proceed only if the same session is now in an
   eligible resumable state.
5. If a dispatch reservation has no PID, respect its dispatch lease. Do not
   orphan a fresh `STARTING`/claimed reservation based only on missing PID or
   heartbeat. Wait/re-read within a bounded interval or return a specific
   `dispatch_in_progress` outcome.
6. If process identity or ownership is unknown, fail closed with instructions
   to inspect the session/worker and retry. Do not clear PID/lease metadata or
   launch a replacement.
7. If a worker is verified live, do not mutate its attempt. Report that the
   session is already managed, include bounded safe diagnostics (status,
   attempt, PID when available), and point to the supported attach/manager
   action.

Reconciliation must use existing durable transition methods and event
projections. It must not directly edit JSON or silently overwrite lifecycle
fields.

### 7.3 Resume the same session after stale recovery

When stale ownership is proven, the normal `handoff()` behavior may resume the
same ID from `ORPHANED` (or the appropriate terminal recovery state). The
resulting request must retain the previous attempt audit, workflow/checkpoint
state, pending recovery inputs, and current user intent according to the
existing background-session contracts. Reuse the current recovery arbitration;
do not infer a workflow restart or duplicate an interrupted turn.

The stale attempt must be finalized/reconciled before the next attempt is
started. Any race with a heartbeat, worker finalizer, new attempt, or dispatch
claim must cause a re-read and a safe typed outcome rather than a blind retry.

### 7.4 Idempotent in-process detach

The foreground TUI must track a handoff state such as `idle`, `preparing`,
`started`, or `failed`. Once the request has durably entered `preparing`, a
second `/detach` alias invocation must not call `supervisor.handoff()` again.
On success, request input-loop exit exactly once. On a recoverable failure,
reset the local state only after the supervisor confirms that no request was
accepted, so the user can retry safely.

The supervisor/storage boundary must remain safe across processes as well:
deduplicate by the same session/attempt and durable request identity, not only
by an in-memory flag. A repeated request must not rewrite an active request's
intent/configuration or launch another worker.

### 7.5 Preserve the foreground configuration

The handoff request must preserve the config path/source used by the current
foreground context, as well as its explicit CLI and secret overrides, working
directory, selected mode, requested profile, workflow, and run identity. In
particular, do not replace a non-default `ctx.config_path` with `None`.

Secrets must continue to follow the existing protected request-file and
redaction policy. Do not serialize a fully expanded config containing
credentials to logs or ordinary diagnostics. The launched worker must resolve
the same effective provider/model/profile as the foreground session unless the
user explicitly changes configuration before the next attempt; if exact
equivalence cannot be established, report the difference before starting the
worker.

This requirement does not make `/model` a live provider switch. A model change
that requires restart remains subject to existing configuration semantics; the
handoff must use the configuration selected for the session it is detaching.

### 7.6 Preserve two-stage ownership transfer

The existing safe ordering remains mandatory:

```text
resolve/reconcile exact record
    → persist prepared request
    → release foreground session-owner lease
    → start/dispatch background worker
    → return typed result and exit foreground TUI
```

Never release the foreground lease before durable request preparation. Never
start a worker while the current foreground still holds the lease. If worker
launch fails after lease release, preserve the durable request and recoverable
session state so the user can retry without losing context.

## 8. State resolution table

| Background record | Worker/dispatch evidence | Required behavior |
|---|---|---|
| Missing | No prior record | Create the normal background handoff for the current session ID. |
| Terminal (`completed`, `failed`, `cancelled`, `orphaned`, eligible archived) | No live owner | Resume using current handoff rules and preserve prior attempt history. |
| Active | Worker PID verified dead or mismatched; foreground owns session lease | CAS to `orphaned`, re-read, then prepare the next attempt. |
| `STARTING` with fresh claimed dispatch and no PID | Dispatch lease still valid | Do not orphan; report/retry boundedly as dispatch-in-progress. |
| Active | Worker verified live | Do not create another attempt; report active worker and supported actions. |
| Active | PID/owner identity unverifiable | Fail closed with recovery instructions; do not clear state or launch. |
| Handoff already preparing/started for this same attempt | Matching durable request exists | Return the existing outcome; do not launch or mutate a duplicate request. |

## 9. Acceptance criteria

### Recovery and ownership

1. Reproduction test: create a session with a `RUNNING` background record and a
   worker PID that is verifiably dead, acquire the same session through the
   foreground resume path, then issue `/detach`. The operation reconciles the
   stale attempt and starts one new background attempt for the same session ID
   without the `already managed` error.
2. The stale transition is fenced by expected session status, attempt,
   background lease token, and observed activity/dispatch version. A concurrent
   heartbeat/finalizer/new attempt prevents stale mutation.
3. A verified live worker is never duplicated, overwritten, or orphaned by
   `/detach`.
4. A PID that exists but belongs to a different process is treated according
   to the existing mismatched-process proof, not as a live worker.
5. A missing PID in a fresh claimed dispatch reservation is not treated as
   proof of death; dispatch lease expiry/recovery governs it.
6. An unverifiable worker identity produces a distinct actionable result and
   leaves the record unchanged.
7. Recovery is exact-target and bounded; `/detach` does not scan or mutate
   unrelated sessions.
8. Existing workflow checkpoints, current phase, conversation journal,
   accepted input inbox, run ID, and attempt history survive stale recovery.

### Idempotency and user experience

9. Repeated `/detach`, `/bg`, or `/background` submissions while one handoff is
   preparing result in at most one prepared request and one worker launch.
10. A successful handoff exits the foreground TUI exactly once. A failed
    handoff leaves the TUI usable and does not display a success notification.
11. Verified-live, stale-recovered, dispatch-in-progress, and unverifiable
    states have separate user-facing messages; messages include only safe,
    bounded metadata and actionable next steps.
12. Retrying after a launch failure uses the existing prepared request or
    creates a safely fenced replacement; it never duplicates accepted work.

### Configuration continuity

13. A handoff started from a non-default `--config` path passes that path to
    the background request and worker.
14. CLI overrides, secret overrides, cwd, mode, workflow, run ID, and requested
    profile are preserved according to their existing contracts.
15. A test changes the selected config's provider/model, resumes the session,
    detaches it, and proves the worker resolves the intended configuration
    rather than silently loading a different default config.
16. API keys and other secrets are absent from normal logs, status text, and
    error messages.

### Verification

17. Unit tests cover every row in the resolution table, stale/live races,
    repeated invocations, and exact handoff error rendering.
18. Integration tests kill a real or controlled worker process, resume the
    session in the foreground, then detach and verify lifecycle events and
    ownership lease ordering.
19. End-to-end tests reproduce the reported kill → `--continue` → config/model
    change → `/detach` journey and verify one active worker owns the session
    afterward.
20. Existing live-worker duplicate rejection, foreground attach/worker stop,
    worker recovery, retry, and detached-session input tests continue to pass.

## 10. Test plan

### Unit

- Active record + dead PID is reconciled and handed off with CAS fencing.
- Active record + mismatched PID follows verified stale recovery.
- Active record + live PID returns `already_managed` without lifecycle writes.
- Unknown PID/process identity and missing owner evidence fail closed.
- Fresh no-PID dispatch claim remains protected until its dispatch lease expires.
- Races between reconciliation and heartbeat/finalizer/new attempt re-read the
  canonical record.
- Duplicate local requests and repeated commands are idempotent.
- `config_path` and all captured overrides are serialized into the request and
  preserved after restart.

### Integration and E2E

- Kill a worker after its durable status reaches `RUNNING`; resume the same
  session with `--continue`; issue `/detach`; verify `ORPHANED` reconciliation,
  the next attempt, and a single worker PID.
- Repeat while a worker remains alive; verify no second process is launched.
- Exercise crash windows before request persistence, after request persistence,
  after foreground lease release, and after subprocess launch.
- Verify provider/model resolution from a non-default config path and ensure
  secrets do not appear in persisted diagnostics.

## 11. Rollout and operational guidance

1. Add a regression test matching the killed-worker sequence before changing
   lifecycle behavior.
2. Implement the typed resolution and exact-target reconciliation using
   existing process identity and store transition primitives.
3. Add idempotent detach state and preserve the selected config path.
4. Run unit, integration, and subprocess E2E tests on supported platforms;
   platform-specific unknown process identity must continue to fail closed.
5. Update the background-session and session-recovery guides with commands for
   live, stale, and unverifiable cases.

If the current run is still open and displays this error, the immediate safe
recovery path is to inspect `agenthicc agents` and refresh/maintain the exact
session so a dead worker is marked orphaned, then retry `/detach`. Do not delete
the session or clear its worker metadata manually. This is a workaround, not
the intended post-fix behavior.

## 12. Security and data safety

- Never infer worker death from heartbeat age alone.
- Never reuse a session while the background worker or another session owner
  may still be writing its journals, workflow checkpoints, or workspace.
- Reconcile with process identity and durable expected-field comparisons; do
  not clear PID, lease, or dispatch metadata directly.
- Preserve existing trust, approval, network, and filesystem policies when the
  next worker is launched.
- Keep secrets protected in request files and redacted from logs/output.
- Preserve all session artifacts and prior attempt evidence on recovery.

## 13. Definition of done

After an abrupt worker/process exit, the user can reopen the same session with
`agenthicc --continue` and safely `/detach` it again. Agenthicc identifies and
reconciles a conclusively dead background worker, starts no duplicate when the
worker is live or uncertain, preserves the current session's configuration and
recovery state, and reports a clear result instead of the generic
`InvalidSessionTransition` message.

## 14. Implementation record

Implemented in the background supervisor and TUI handoff bridge:

- Active exact-session records are reconciled only when the worker PID is
  conclusively dead/mismatched and the current TUI's durable session-owner
  lease is verified against its on-disk owner record.
- Stale transitions are compare-and-set against attempt, status, worker lease
  token, activity timestamp, and dispatch state. Live, claimed-without-PID,
  and uninspectable workers fail closed with distinct recovery guidance.
- `/detach` carries the current config path, CLI/secret overrides, selected
  mode, workflow, and requested profile to the persisted request. The existing
  same-session ID, attempt history, workflow journal/checkpoints, and input
  inbox remain authoritative.
- In-process repeated handoff calls cannot prepare a second attempt. A
  confirmed worker-launch failure restores foreground ownership only after
  the failed attempt is terminal; uncertain ownership exits safely.
- The background-session and goal-run guides document the abrupt-kill
  recovery path and the live/unverifiable-worker behavior.

Regression coverage includes supervisor unit tests for stale/live/unknown/
dispatch/race outcomes, a TUI-bridge test for the exact `--continue` lease plus
changed-model configuration path, and an E2E test that kills a real controlled
worker subprocess, reacquires the foreground lease, detaches, and verifies
exactly one replacement worker owns the same session.
