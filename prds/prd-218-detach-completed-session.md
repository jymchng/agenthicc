---
title: "PRD-218: Detach a foreground session after a completed background attempt"
status: Implemented
version: 1.1.0
date: 2026-10-03
repository: jymchng/agenthicc
related_prds:
  - PRD-141  # Background sessions and lifecycle
  - PRD-170  # Workflow recovery
  - PRD-204  # Goal runner and detached orchestration
  - PRD-205  # Detached goal process lifecycle
  - PRD-211  # Attempt-consistent status and activity
  - PRD-216  # Input to completed background sessions
  - PRD-217  # Opaque agents-session input forwarding
tags:
  - background-sessions
  - detach
  - lifecycle
  - session-identity
  - workflows
---

# PRD-218 — Detach a foreground session after a completed background attempt

## 1. Executive summary

Fix `/detach` (and its `/bg` and `/background` aliases) when the foreground
session has the same ID as a background record whose previous attempt is
`completed`. Today the handoff persists a prepared worker request but leaves
the record in `COMPLETED`; starting that request then fails with:

```text
Unable to background session: InvalidSessionTransition:
Cannot start prepared completed session
```

The fix must regard the durable **session/conversation** as the unit of
identity and `COMPLETED` as the outcome of its previous background attempt.
Where the session has resumable work, detaching starts a new, owner-fenced
attempt for that same session and lets the established workflow recovery path
continue its saved state. It must retain the conversation, journal, workflow
checkpoint, phase history, runtime configuration, and existing goal-run
association. It must not create a replacement session or goal run, or replay a
completed goal merely to make the worker start.

If there is no work to execute or resume, detach must still complete as a
valid ownership handoff without fabricating another turn or replaying the
goal. A worker may briefly start to reconstruct the durable session and check
for a valid checkpoint, but it must make no model request when none exists.
The TUI must not report the prepared-session exception.

## 2. Observed failure and source-level cause

The foreground integration in `src/agenthicc/background/integration.py`
calls `BackgroundSupervisor.handoff(..., start=False)`, releases the
foreground owner lease, and then calls `start(session_id)` to launch the
prepared request.

In `BackgroundSupervisor.handoff()` (`src/agenthicc/background/supervisor.py`):

1. An existing `COMPLETED` record is not rejected; only an already-active
   background record is rejected.
2. The method transitions `FAILED`, `CANCELLED`, `ORPHANED`, and `ARCHIVED`
   records into a new start/retry state.
3. It has no corresponding transition for `COMPLETED`. It updates request
   metadata and writes the prepared request while the record remains
   `COMPLETED`.
4. `start()` reloads the prepared request and permits launch only when the
   record status is in `ACTIVE_STATUSES`.
5. Since `COMPLETED` is terminal and not in `ACTIVE_STATUSES`, `start()` raises
   the reported `InvalidSessionTransition`.

The lifecycle transition table already permits
`COMPLETED -> STARTING`; the missing behavior is in the prepared handoff path.
This is therefore an incomplete lifecycle transition, not a reason to reject
the user's session or create a new one.

The distinction is important: a background record can say that its previous
attempt completed while the same durable session is subsequently open in the
foreground. A previous attempt's terminal state must not be interpreted as
permanent closure of the session.

## 3. User story and product contract

> As a user working in a foreground session—particularly one already
> associated with `goal_flow`—I can run `/detach` after a prior background
> attempt completed and hand off that same session. The handoff preserves its
> existing state and either resumes genuinely recoverable work or exits
> cleanly when there is nothing left to execute.

Normative identity contract:

```text
session_id  = durable conversation/execution identity
attempt     = one worker execution of that session
run_id      = optional goal-level association/aggregation metadata
```

Starting another attempt does not create another session or another goal run.
Workflow checkpoints determine whether there is workflow work to resume; a
workflow name or a previous user prompt alone must not be treated as proof
that a completed goal should be replayed.

## 4. Goals

1. Make `/detach`, `/bg`, and `/background` valid for an eligible foreground
   session even if its prior background attempt is `COMPLETED`.
2. Start a new attempt on the same session ID only when recoverable or
   otherwise pending work exists.
3. Preserve all existing durable conversation, workflow, run, and security
   state across the handoff.
4. Prevent duplicate launches, stale-owner writes, duplicate goal execution,
   and lost prepared requests.
5. Report a successful and truthful outcome to the TUI for both resumed work
   and a session with no pending work.
6. Add regression coverage at lifecycle, supervisor, TUI integration, and
   end-to-end boundaries.

## 5. Non-goals

- Creating a new session to bypass a terminal lifecycle state.
- Creating a new `goal_run_id` or workflow run as part of detaching.
- Replaying the last user message merely because it was once the session's
  launch intent.
- Changing the meaning of input submitted through `agenthicc agents`; PRD-217's
  opaque pass-through contract remains unchanged.
- Changing the policy for deleted sessions, disabled background execution,
  worker capacity, or a session currently owned by another live worker.
- Redesigning all workflow recovery or the general background state machine.

## 6. Functional requirements

### 6.1 Completed status is attempt-scoped

The implementation must distinguish a terminal result for the last worker
attempt from permanent closure of the durable session. A `COMPLETED` session
record may be reactivated for a new attempt when a foreground owner is
explicitly handing off that same session and eligible work remains.

The reactivation must use the existing lifecycle transition
`COMPLETED -> STARTING` (or a narrowly equivalent, documented transition),
with the standard attempt increment and a new attempt-scoped resume marker.
It must not mutate the record directly around the store's transition rules.

### 6.2 Prepare and launch are one recoverable operation

The handoff must persist the complete background request and transition the
same session record to the correct pending/starting attempt before releasing
foreground ownership. Launch may occur only after the foreground session has
stopped writing and released its owner lease.

If the process stops between preparation, lease release, and process launch,
the same request and session must remain recoverable through existing
maintenance/recovery mechanisms. Retrying the handoff must not increment the
attempt twice or create duplicate workers for one attempt.

### 6.3 Same-session continuity

On a completed-attempt handoff with resumable work, the worker must use the
existing `session_id` and retain, without resetting:

- conversation journal and transcript;
- workflow name and active-workflow selection;
- resumable workflow run ID, phase/checkpoint state, phase history, and
  artifacts;
- existing goal `run_id` and main-session association;
- workspace, provider/model configuration, mode policy, security overrides,
  and session memory;
- prior attempt metadata as history, while recording the new attempt as
  current.

The worker must use the normal validated recovery path to resume an
in-progress `goal_flow` checkpoint. It must not initialize a fresh workflow
from phase one when a valid later checkpoint exists.

### 6.4 No implicit replay of completed work

The handoff must not treat `BackgroundSession.intent` or the last historical
user message as a new user submission solely to make a worker run. A
completed goal must not be executed a second time because the user requested
detachment.

If no recoverable workflow, pending input, or active work remains, the
operation must complete as an idle/ownership handoff: retain the existing
completed session and its data, exit the foreground TUI as requested, and
finalize the short-lived background attempt without a model request. It must
not replay the completed intent as a new turn. If the runtime cannot determine
whether work remains safely, fail without releasing ownership and provide a
specific, recoverable diagnostic rather than emitting the prepared-completed
error.

### 6.5 Preserve goal-run identity

For a session already associated with `goal_flow` or another goal run:

- retain the same session ID and goal run ID;
- do not create a second goal run;
- do not replace an existing run ID with an empty value from an incomplete
  handoff argument;
- update the existing run projection only after the same-session handoff is
  accepted, using the current attempt/session association;
- ensure a terminal prior attempt does not incorrectly make an active,
  recoverable goal appear complete or create a second main agent.

### 6.6 Eligibility and conflicts

The fix must not weaken existing safety checks:

- `DELETED` sessions cannot be detached or reactivated.
- A session already owned by a live background worker cannot be detached a
  second time.
- Worker/global/project capacity and background-enabled configuration remain
  enforced.
- Owner leases and expected attempt/version checks remain authoritative.
- On preparation or launch failure, the user receives a truthful result and
  the session remains recoverable; do not silently discard its request or
  workflow state.

## 7. State and user-visible behavior

When resumable work is accepted, the session detail projection should show the
same session ID, a new attempt number, and a starting/running state. The
activity text should identify a same-session handoff or workflow recovery,
not a new session or new goal.

When there is no pending work, `/detach` should return to the shell cleanly
with a concise notice such as:

```text
Session <short-id> detached; no background work was pending.
```

The exact copy is implementation-defined. It must not claim that a worker was
started when none was launched.

When validation fails, the TUI stays open and the error identifies the actual
constraint (deleted session, live owner, disabled background sessions,
capacity, or unsafe recovery state). The exact string
`Cannot start prepared completed session` must not be the user-facing result
of an eligible `/detach`.

## 8. Acceptance criteria

### Lifecycle and supervisor

- [ ] Given an existing `COMPLETED` record with resumable work, prepared
  handoff transitions that same record to `STARTING` exactly once and assigns
  the next attempt/resume marker.
- [ ] `start()` accepts the correctly prepared attempt and rejects a stale,
  mismatched, deleted, or otherwise invalid request.
- [ ] Attempt history retains the previous completed outcome and identifies
  the new attempt as current.
- [ ] Empty run metadata in a handoff does not erase an existing valid goal
  run association.
- [ ] Concurrent or repeated handoff calls cannot create two workers for the
  same session attempt.

### Workflow and continuity

- [ ] A completed background attempt followed by foreground reopening and
  `/detach` resumes the same valid later-phase `goal_flow` checkpoint on the
  same session ID.
- [ ] The test proves that the workflow does not restart at `INIT` when a
  later valid checkpoint is persisted.
- [ ] The conversation journal, checkpoint artifacts, phase history, and
  goal-run association survive the transition.
- [ ] Detach does not append a fabricated user/assistant continuation message
  and does not replay the old goal as a new turn.
- [ ] If no resumable or pending work exists, detach exits cleanly without a
  model call or creating a new goal/session; any short-lived worker used to
  inspect and finalize the same session records `no_resumable_work`.

### TUI and failure recovery

- [ ] `/detach`, `/bg`, and `/background` share the corrected behavior.
- [ ] An active foreground workflow still uses the established cancellation,
  checkpoint, lease-release, and background-start path.
- [ ] If preparation fails, foreground ownership is retained.
- [ ] If launch fails after durable preparation, the same request can be
  recovered without losing or duplicating workflow work.
- [ ] Deleted sessions, live-worker conflicts, disabled background mode, and
  capacity limits remain enforced.
- [ ] The original reproduction no longer displays
  `InvalidSessionTransition: Cannot start prepared completed session`.

## 9. Test plan

### Unit tests

- Cover the transition from `COMPLETED` to a prepared next attempt, including
  expected status/attempt fencing and exactly-once attempt advancement.
- Cover preservation of workflow, run, mode, configuration, and prior attempt
  metadata.
- Cover refusal for deleted sessions and duplicate/live ownership.
- Cover the no-work path, asserting that no model request is made and no
  historical intent is synthesized as fresh input.

### Integration tests

- Exercise `BackgroundSupervisor.handoff(start=False)` followed by lease
  release and `start()` for a completed record.
- Simulate interruption after request persistence and before launch; verify
  recovery launches the same attempt/request exactly once.
- Verify request metadata and the background store remain consistent when
  launch fails.
- Exercise existing `goal_flow` checkpoint recovery from a later phase and
  assert transcript/checkpoint/run identity continuity.

### End-to-end tests

- Reproduce a foreground `goal_flow` session whose prior background attempt
  is `COMPLETED`, invoke `/detach`, and verify it returns cleanly and resumes
  eligible work in the same session.
- Verify the no-pending-work completed-session case exits without rerunning
  the original goal or issuing a model request.
- Verify aliases, failure notices, deleted/live-worker exclusions, and
  recovery after process interruption.

Tests must use temporary stores/workspaces and fake worker launchers; they
must not call a live model provider or depend on timing-sensitive sleeps.

## 10. Rollout and compatibility

This is an additive correction to the foreground detach lifecycle. Existing
session IDs, event logs, request files, and goal-run records remain valid; no
storage migration should be required. Existing active, failed, cancelled,
orphaned, and archived handoff behavior must remain compatible.

The implementation should first ship behind the existing detach path without
changing slash-command syntax. Older terminal records must be handled using
the store's existing deserialization defaults. If attempt history or
checkpoint state is inconsistent, fail safely and preserve artifacts for
manual recovery rather than guessing that the original goal should run again.

## 11. Security and operational considerations

- Do not weaken owner leases, workspace boundaries, capability policy, or
  worker limits.
- Do not log the conversation, user prompt, credentials, or full request
  payload as part of lifecycle diagnostics.
- Keep session/run IDs and attempt numbers in redacted operational output.
- Reconcile the prepared request and session record on startup so interrupted
  handoffs are observable and recoverable.
- Preserve prior-attempt errors as history while ensuring projections show
  the new current attempt accurately.

## 12. Implementation sequence

1. Add a focused regression test reproducing the completed-record handoff and
   asserting the session lifecycle/state before worker start.
2. Implement attempt-scoped reactivation for `COMPLETED` in the supervisor's
   prepared handoff path, using the canonical store transition and existing
   owner/attempt fencing.
3. Preserve existing run/configuration metadata when optional handoff fields
   are omitted.
4. Determine pending/recoverable work before launching; use normal workflow
   recovery for a valid checkpoint and make the no-work path a clean detach,
   never a replay of the old intent.
5. Verify preparation, owner release, launch, and recovery failure paths for
   idempotency and truthful TUI outcomes.
6. Add integration and E2E regression coverage, then update background-session
   documentation and implementation status in this PRD.

## 13. Definition of done

The PRD is implemented when an eligible foreground session whose prior
background attempt is `COMPLETED` can be detached without the reported
exception; recoverable goal/workflow work continues under the same session and
run identities; completed work is not replayed; failures are recoverable; and
the unit, integration, and E2E acceptance tests pass.

## 14. Implementation and verification

Implemented in the background supervisor, request contract, worker, and TUI
handoff bridge. Completed records transition through the canonical store to
`STARTING`, retain their existing run identity, and preserve the previous
attempt in history. Idle handoffs use `resume_existing_only`: workers resume a
valid checkpoint or finish without a model request when no checkpoint exists.

Verification:

- `uv run pytest tests/ -q` — 4,110 passed, 16 skipped.
- Focused background-session tests — 98 passed.
- Focused mypy for the three changed runtime modules — passed.
- `uv run ruff check src/ tests/ scripts/` — passed.
- `uv run mkdocs build --strict` — passed.
- `uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json` — passed.

The repository-wide `uv run mypy src/agenthicc` command still reports 250
existing errors across unrelated modules; the changed runtime modules pass the
focused check above.
