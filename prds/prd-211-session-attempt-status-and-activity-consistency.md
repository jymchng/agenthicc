---
title: "PRD-211: Keep resumed session status and activity attempt-consistent"
status: Proposed
version: 1.0.0
date: 2026-09-30
repository: jymchng/agenthicc
related_prds:
  - PRD-204  # Goal runs and the run/session identity relationship
  - PRD-205  # Detached worker lifecycle and process identity
  - PRD-206  # Background-session manager and exact session selection
  - PRD-209  # Background-manager refresh and lifecycle maintenance
tags:
  - sessions
  - background-workers
  - retries
  - status-projection
  - activity
---

# PRD-211 — Keep resumed session status and activity attempt-consistent

## 1. Summary

Investigate and fix the case where a background worker is executing or has
been resumed, but `agenthicc agents` (or a goal-run projection) continues to
show an old `orphaned`/failed state and an earlier workflow error instead of
the current attempt's status and activity.

The implementation must treat a background `session_id` as the identity of
one durable conversation and its current execution lifecycle, and a goal
`run_id` as the identity of the larger logical goal that may contain a main
session and multiple worker sessions. A run ID may group and summarize
sessions; it must not replace the session ID when deciding an individual
worker's liveness, current attempt, activity, or control target.

This PRD addresses two independent but interacting defects found in the
current source:

1. Recovery can mark a worker `orphaned` because its heartbeat timestamp is
   stale even when the process identity check says that the worker is alive.
2. Resume/retry starts a new attempt without clearing or versioning the prior
   attempt's `error` and failure metadata. The `agents` UI gives that sticky
   error precedence over fresh activity, and goal-run reconciliation copies
   the same stale error back into the run projection.

The fix must preserve prior-attempt diagnostics durably while ensuring current
status, error, and activity are derived from the current session attempt.

## 2. User-visible symptom and evidence

The supplied `agenthicc agents` screen showed a selected session with a state
of `orphaned`, a recent `Updated` timestamp, and an error similar to:

```text
implement phase never called goal_implemented()
```

The report says the corresponding work appeared to still be running. The
screen alone does not prove a process was alive at that exact instant, so the
implementation and regression tests must capture both the persisted session
record and independently verified worker-process identity. It must not infer
process liveness from a recent timestamp or stale error text.

### 2.1 Confirmed source behavior

The current implementation establishes these facts:

* `BackgroundSession` has a stable `session_id`, a grouping `run_id`, an
  incrementing `attempt`, a current `error`, and `latest_activity`.
* `BackgroundSupervisor._recover_stale_records()` marks an active record
  orphaned when either its worker is not recognized **or** its
  `last_active` age exceeds `stale_after_s`. The timestamp condition can
  override a positive worker-process identity check.
* The background worker normally writes a heartbeat every second, but a
  delayed/event-loop-blocked heartbeat can cross the default 30-second stale
  threshold while a process remains alive.
* `resume()`/`handoff()` moves failed, cancelled, orphaned, or archived
  records back to `starting`; `BackgroundStore.claim()` moves them to
  `running` and increments `attempt`. Neither path clears or versions the
  prior `error`, `failure_category`, and completion metadata.
* `BackgroundManager` renders `session.error` instead of `latest_activity`
  whenever `error` is non-empty. The selected-details renderer likewise shows
  the error branch instead of the latest transcript activity.
* `GoalRunManager.resume()` clears run-level error fields, but its immediate
  `projection()` calls `reconcile()`, which takes `session.error` as current
  failure evidence and can copy the old error back into `failure_reason` and
  `attention_reasons`.
* The run projection links sessions by `run_id` and/or the explicit parent
  and main `session_id` relationships. This is the correct grouping role for
  `run_id`; however, session lifecycle must remain authoritative for each
  linked worker rather than inheriting one run-level failure as every
  session's current state.

Therefore, the observed stale error is directly explained by attempt-scoping
and projection behavior; “tracking run ID instead of session ID” is not by
itself proven as the cause. Run/session identity confusion remains an
important invariant to enforce, particularly in aggregate run views.

## 3. Problem

One durable background session can have several execution attempts. Today,
the current record mixes lifecycle facts from different attempts:

```text
Attempt 1: workflow fails; error = "implement phase never called ..."
Attempt 2: worker is launched/resumed; status becomes running
           error still contains Attempt 1's failure
UI:        status/activity is rendered with Attempt 1's error
Run view:  reconciliation copies that old error into the run again
```

Additionally, the stale-recovery rule can turn a live but temporarily
unresponsive worker into a terminal `orphaned` session. Once marked orphaned,
the run manager may project the logical goal as `lost` even though the worker
continues executing. These behaviors make users unsure whether to wait, retry,
attach, or start duplicate work.

## 4. Goals

1. Show the actual lifecycle state of the current worker attempt.
2. Never label a positively identified live worker as orphaned solely because
   its heartbeat timestamp is old.
3. Never show an earlier attempt's error as if it were the current attempt's
   activity or failure.
4. Preserve previous attempt diagnostics and audit history; fixing stale UI
   must not discard evidence.
5. Keep run-level state as an aggregate of its currently linked sessions and
   workflow/worktree state, not a substitute for per-session state.
6. Keep actions such as resume, cancel, attach, and activity lookup targeted
   by exact `session_id`; use `run_id` only for goal-level selection and
   aggregation.
7. Make recovery and projections safe under worker restart, PID reuse,
   heartbeat delay, concurrent resume, and process races.

## 5. Non-goals

* Replacing background sessions or goal runs with a new persistence system.
* Making every provider/workflow error retryable.
* Hiding genuine current-attempt failures.
* Automatically retrying a worker merely because its heartbeat is late.
* Treating a run ID as an alias for an arbitrary worker session ID.
* Changing workflow-specific completion requirements such as the requirement
  to call `goal_implemented()`.

## 6. Required identity and authority contract

The implementation must document and enforce this mapping:

| Fact or operation | Authoritative identity/source |
| --- | --- |
| Worker PID, lease, attempt, lifecycle status | `session_id` record and current lease/attempt |
| Conversation transcript and latest text activity | `session_id` journal/artifact stream |
| Resume/cancel/attach target | Exact `session_id` |
| Goal membership and aggregate view | `run_id`, plus explicit parent/main session links |
| Workflow checkpoint phase | Checkpoint identity scoped to the owning `session_id` and workflow run |
| Current attempt failure | Error outcome associated with the current attempt/lease |
| Prior attempt failure | Durable attempt history/audit; not current activity |

Do not join an activity file by `run_id`, reuse one session's activity for
another worker in the same run, or map a run-level failed state onto a worker
whose current session attempt is running.

## 7. Proposed implementation

### 7.1 Make attempt outcomes explicit and durable

Treat `(session_id, attempt, lease_token)` as the execution-attempt identity.
On successful claim of a new attempt, atomically:

* increment `attempt` and establish its lease;
* set the current attempt to `running`;
* clear current-attempt `error` and `failure_category`;
* clear terminal timestamps/outcome fields that would otherwise describe the
  previous attempt, or move them into a clearly named previous-attempt record;
* set a new attempt-start timestamp and current activity;
* retain the prior attempt's status, error, exit details, and timestamps in
  append-only events or a bounded typed attempt-history projection.

Clearing must happen only at the atomic successful claim boundary, not before
the replacement process has acquired the session lease. A failed launch or
failed claim must not erase the last known failure. Legacy records without
attempt history must remain readable; their existing error is retained as
historical evidence when the next attempt successfully claims the session.

Every worker update/finalization must compare both the current attempt and
lease token (in addition to status) so a late callback from attempt N cannot
overwrite attempt N+1's status, activity, or error.

### 7.2 Separate liveness from heartbeat freshness

Replace the current `worker_missing OR lease_expired` decision with an
explicit reconciliation policy:

* A worker with a positively verified process identity and matching request,
  store root, session, and lease remains active even if its heartbeat is
  delayed. Record a stale-heartbeat/unresponsive health signal separately and
  do not transition it to terminal `orphaned` from age alone.
* A worker whose process is positively absent, or whose PID is positively
  identified as a different process, may be marked orphaned after a
  race-safe re-read of the current session and attempt.
* A process that cannot be inspected is **unknown**, not automatically
  confirmed dead. Preserve a recoverable/nonterminal state or expose an
  explicit reconciliation-needed condition until a bounded grace policy
  provides sufficient evidence. The chosen policy must be documented and
  tested on supported platforms.
* A record with no PID during `queued`/`starting` receives a bounded launch
  grace period; startup delay alone must not immediately mark it orphaned.
* Before writing `orphaned`, re-read the session and verify the observed
  attempt/lease is still current. A fresh heartbeat or changed lease between
  inspection and transition cancels the stale decision.

The exact user-facing health representation (for example, a stale-heartbeat
flag, a nonterminal `unresponsive` state, or a recovery-needed marker) should
follow existing status/API compatibility constraints. It must not overload
`orphaned` to mean merely “heartbeat late.”

### 7.3 Make status and activity projections attempt-aware

The `agents` manager must:

* render current status from the latest `BackgroundSession` attempt;
* show current `latest_activity`/latest meaningful session transcript while an
  attempt is active;
* show a current error as an error only when it belongs to the current failed
  attempt;
* label older errors explicitly as previous-attempt history rather than
  letting them override current activity;
* invalidate activity caches on attempt/lease changes as well as session ID
  and journal-file changes;
* continue to use exact selected `session_id` for actions.

For a new retry/resume, the table should say that the worker is starting or
running and display its new activity, while details may retain a separate
“Previous attempt” diagnostic section. For a genuinely failed current attempt,
the error remains prominent.

### 7.4 Reconcile goal runs from current linked sessions

Update `GoalRunManager.reconcile()` and `projection()` so that:

* the main session's current attempt determines main-agent lifecycle;
* worker records are refreshed from all current sessions linked by explicit
  `run_id`/parent relationships and retain distinct `session_id`s;
* run status is derived using documented aggregation rules for active,
  waiting, failed, orphaned/unknown, completed, and integrating workers;
* stale run-level `failure_reason` and `attention_reasons` are cleared when
  the relevant session begins a new attempt, while prior errors remain
  available in attempt history;
* a failed old attempt does not force a currently running main/worker session
  to `failed`, `lost`, or `needs_attention`;
* errors from one worker are attributed to that worker and are not copied as
  current errors onto sibling workers;
* the projection does not persist a less-current result over a newer session
  attempt when reconciliation races with a heartbeat or retry.

Run-level failure is still appropriate when the current main attempt has
failed or when the aggregate policy determines that a required worker failure
blocks completion. This PRD does not prescribe that every active worker makes
the goal run `running`; statuses such as `integrating`, `waiting`, and
`needs_attention` must remain meaningful.

### 7.5 Observability

Expose enough bounded diagnostic information to distinguish:

* current session ID and goal run ID;
* attempt number and whether its process/lease was verified;
* last heartbeat/activity time;
* current attempt status/activity/error;
* previous attempt's outcome where available;
* why recovery changed or did not change state.

Diagnostics must redact credentials and must not emit full prompts or raw
provider payloads. Avoid adding a high-volume log entry on every heartbeat.

## 8. Data migration and compatibility

* Existing session and run event records remain readable without new fields.
* New attempt data is additive and versioned/defaulted for older records.
* Existing `session_id` and `run_id` values are not rewritten.
* Existing `agenthicc agents --run RUN_ID` behavior remains run-filtered; a
  selected row's actions still target its exact session ID.
* Existing `agenthicc attach <session-id>` semantics remain exact-session
  attachment; this PRD does not reinterpret positional IDs.
* Historical errors remain inspectable after migration/retry, but are no
  longer returned as the current attempt's error.
* Security, redaction, event-log integrity, session ownership, and workflow
  checkpoint validation remain unchanged.

## 9. Acceptance criteria

1. When an attempt fails with error E1 and a later attempt successfully claims
   the same session, the live manager shows the new active status and activity,
   not E1 as the current error.
2. E1 remains available as a clearly labeled prior-attempt diagnostic after
   resume and after process restart.
3. A genuinely failed new attempt shows its own error E2 as current; E1 and E2
   are distinguishable by attempt number.
4. A test with a live, identity-verified worker and an expired heartbeat does
   not transition the session to `orphaned` solely due to heartbeat age.
5. A positively dead or mismatched worker is recoverable as orphaned without
   erasing its last activity, failure context, or artifacts.
6. A heartbeat/lease refresh racing stale recovery prevents an obsolete
   recovery observation from changing the current attempt's status.
7. A late finalization from an older attempt cannot overwrite the status,
   error, activity, or PID of a newer attempt.
8. Goal-run resume followed immediately by projection remains running/starting
   when its main session is active and does not restore an old failure reason.
9. In a run containing several sessions, each agent row displays activity and
   errors from its own session ID; one worker's failure does not appear as
   another worker's current error.
10. Run-level aggregate status is consistent with all currently linked
    session records and the documented aggregation rules.
11. `agenthicc agents`, `agenthicc agents --run`, `agenthicc runs show`, and
    exact-session attach/resume retain their documented identity semantics.
12. Legacy store/run fixtures without attempt-history fields load and can be
    resumed without losing their old diagnostic evidence.
13. Unit tests cover status transitions, attempt/lease fencing, error history,
    recovery outcomes, and projection rules.
14. Integration tests cover persisted failure → restart → resume → heartbeat
    → fresh activity → restart/reprojection.
15. E2E tests reproduce the user-visible stale-error symptom and verify that
    the row/detail panel moves from prior failure to current attempt state
    without losing the prior diagnostic.

## 10. Test plan

### Unit

* `BackgroundStore.claim()` starts a new attempt and atomically versions prior
  outcome metadata.
* Stale recovery with verified-live, verified-dead, mismatched, unknown, and
  missing-PID workers.
* Recovery races with a new heartbeat, attempt, or lease.
* Old-attempt worker finalization is rejected after a new claim.
* Background-manager table/detail selection of current activity versus prior
  error and cache invalidation on attempt change.
* Goal-run aggregation over active, failed, waiting, orphaned/unknown, and
  completed main/worker combinations.
* Run resume/reconcile does not resurrect stale failure metadata.

### Integration

Use temporary stores and deterministic fake process inspection to exercise:

```text
attempt 1 fails
→ registry and run state persisted
→ manager/process restarts
→ same session resumes as attempt 2
→ heartbeat and activity update
→ run/session projections refresh
→ attempt 1 error remains historical, attempt 2 is shown as active
```

Also test real subprocess identity matching where supported, with platform
specific behavior isolated behind a process-inspection adapter.

### End-to-end

Create a session and goal run with a prior workflow failure, simulate/resume a
new active attempt, open `agenthicc agents` and `agenthicc agents --run`, and
assert the current status/activity is visible while the old error is labeled
as historical. Then terminate the new attempt and verify its terminal state
and error are surfaced accurately.

## 11. Rollout and operational considerations

* Land the attempt identity and projection changes with compatibility tests
  before changing stale recovery behavior.
* Add structured, redacted reconciliation diagnostics so unexpected orphan
  transitions can be audited in support reports.
* Do not automatically relaunch sessions as part of this fix; a late
  heartbeat is an observation problem, not authorization to duplicate work.
* If process identity is unavailable on a platform, document the selected
  conservative fallback and ensure users can explicitly inspect/reconcile the
  session without losing its work.
* Update background-session and goal-run documentation with the authoritative
  identity model, attempt semantics, and meaning of orphaned/stale heartbeat.

## 12. Related implementation surfaces

The implementation is expected to inspect and update, as needed:

* `src/agenthicc/background/model.py` and `store.py`;
* `src/agenthicc/background/supervisor.py` and `worker.py`;
* `src/agenthicc/runs/model.py`, `store.py`, and `manager.py`;
* `src/agenthicc/tui/workspace/background_manager.py`;
* background settings and process-inspection boundaries;
* background-session and goal-run unit, integration, and E2E tests;
* `docs/guides/background-sessions.md`, goal-run documentation, and storage
  reference documentation.

No implementation is included in this PRD-writing change.
