---
title: "PRD-216: Send New Input to Completed Background Sessions"
status: Implemented
version: 1.1.0
date: 2026-10-02
repository: jymchng/agenthicc
related_prds:
  - PRD-141  # Background sessions and lifecycle
  - PRD-150  # Session service and event projection
  - PRD-170  # Workflow recovery
  - PRD-206  # agents session manager
  - PRD-211  # Attempt-consistent status and activity
  - PRD-215  # Targeted input from session details
tags:
  - agents-manager
  - background-sessions
  - input
  - lifecycle
  - tui
---

# PRD-216 — Send New Input to Completed Background Sessions

## 1. Executive summary

Allow an operator to press `i` on a `completed` session's Details page in
`agenthicc agents`, compose a message, and send it to that exact session. The
input starts a new background worker attempt for the existing conversation and
is delivered as a new target-session input. It must not attach the session to
the foreground, replay the original launch intent, or infer that a workflow
should resume.

This extends the target-bound composer specified in PRD-215. PRD-215's
completed-session rejection rule is intentionally superseded by this PRD;
deleted sessions remain ineligible. All text—including slash commands such as
`/workflow ...`—must be forwarded unchanged and interpreted only by the target
session's normal input/command path.

## 2. Observed behavior and cause

The attached reproduction shows a session with status `completed` and the
Details-page input action does not open the composer. This is currently an
explicit status gate, not an editor, keyboard, or rendering failure:

1. `BackgroundManager._open_input_composer()` permits live owners and the
   recoverable set `{orphaned, failed, cancelled, archived}`. `completed` is
   excluded, so pressing `i` sets an “Input unavailable” notice and returns
   before creating the composer.
2. Even if the UI gate were removed, submission reaches
   `BackgroundSupervisor.enqueue_input_or_recover()`. That method only defers
   and recovers the same four terminal states; `completed` is rejected as a
   non-active session.
3. `BackgroundSupervisor.resume()` also excludes `completed`,
   `handoff()` does not transition a completed record back to an active state,
   and the lifecycle transition table currently allows
   `completed -> archived` only.
4. The current inbox has a durable deferred-input mechanism, and the worker
   already processes deferred messages before considering the old launch
   intent. However, that mechanism is not wired to the completed state.

The three guards consistently enforce the existing PRD-215 policy. Fixing
only the Details-page gate would therefore make `i` appear to work but fail on
submission. The feature requires a deliberate lifecycle operation across the
UI, inbox, supervisor, worker startup, and persistence layers.

## 3. Problem and user story

A completed background run has stopped executing, but its session and
conversation history remain available. The user may want to continue the
conversation, ask a follow-up, or send a command without attaching that session
to the foreground. Today the UI treats `completed` as permanently input-closed
even though the record is retained and other recoverable states can receive
input.

> As an operator viewing a completed session, I can send a new message from
> Details and have the same session process it in the background, with its
> existing conversation context, without restarting the old task or silently
> changing the meaning of my input.

“Completed” means the previous background execution attempt ended; it does
not mean the conversation is immutable. A new input is a new user turn, not a
retry of the completed run.

## 4. Goals

1. Make `i` open the target-bound composer for a completed, non-deleted
   session.
2. Durably accept input before launching a new worker attempt.
3. Process accepted text in the existing session/conversation under the same
   session ID, workspace, and applicable configuration and policy.
4. Preserve exact submitted text and FIFO/idempotent inbox semantics.
5. Make the distinction between a new input-driven turn and explicit workflow
   recovery unambiguous in code, state, and user-visible receipts.
6. Preserve deletion, ownership, attempt-fencing, and recovery safety.

## 5. Non-goals

- Re-running the original goal, prompt, workflow, or launch intent.
- Automatically attaching the completed session to a foreground TUI.
- Inferring workflow continuation from ordinary text, including “continue”.
- Parsing, rewriting, or executing slash commands in the manager process.
- Making deleted sessions or arbitrary purged data recoverable.
- Changing behavior for live, waiting, failed, cancelled, orphaned, or archived
  sessions except where required to share safe input-driven startup machinery.
- Reopening an archived session through a new archive-management policy; its
  existing input behavior remains unchanged.

## 6. Product and lifecycle contract

### 6.1 Meaning of a completed-session input

An accepted input to a completed session creates a new worker attempt for the
same durable session. The worker receives the queued input as the initiating
user turn in the existing conversation. It does not execute the stored launch
intent first. After handling the input, the worker may complete normally,
remain waiting for input, or enter another existing supported state.

The session ID and transcript/journal identity stay the same. The attempt
number and worker lease advance according to the existing attempt-claim
contract. Historical completion metadata remains available in attempt history
or equivalent audit data; it must not be mistaken for the new attempt's
completion state.

### 6.2 Input is opaque to the manager

The manager forwards accepted text unchanged. It does not special-case
`/workflow`, any other slash command, or ordinary prose. The target session's
normal router decides whether a message is a command, a skill invocation, or a
normal user turn. A workflow changes only when the exact input is handled by
the target's existing explicit workflow-command behavior. No implicit
workflow selection, checkpoint lookup, resume, reset, or discard is permitted
on the manager/recovery path.

### 6.3 Status eligibility

| Session state | Details `i` behavior | Submission behavior |
|---|---|---|
| `completed` | Open composer | Persist deferred input, reactivate the same session for a new attempt, process the input first |
| Live/starting/waiting states already supported by PRD-215 | Preserve existing behavior | Deliver/queue to the current owner under existing policy |
| `orphaned`, `failed`, `cancelled`, `archived` | Preserve existing behavior | Preserve existing deferred recovery behavior |
| `deleted` | Do not open composer | Reject; never recreate or launch |
| `cancelling` or unsupported states | Preserve bounded explanation | Reject without losing the draft |

The UI must refresh eligibility from the current record when opening and
submitting. A state or ownership change since the details snapshot must not
retarget the message.

## 7. Functional requirements

### FR-1 — Open composer for completed sessions

Pressing `i` on a completed session's Details page opens the existing
target-scoped composer. The composer remains bound to the selected session ID,
attempt, and owner revision captured at open. Existing editing, paste,
registered-trigger, and draft-retention behavior from PRD-215 is unchanged.

### FR-2 — Durable acceptance precedes reactivation

The exact text and stable message ID must be written to the session's durable
input inbox before attempting to launch a worker. The accepted receipt must
survive manager or supervisor process failure between enqueue and launch.
Repeated submission with the same message ID is idempotent; reusing that ID
with different text is rejected.

### FR-3 — Explicit input-driven reactivation

Implement an explicit lifecycle path for a completed session receiving a new
message. It must atomically or recoverably:

1. validate the expected session ID, attempt, and owner revision;
2. verify that the session is completed and not deleted;
3. persist/confirm the deferred inbox item;
4. transition the session into a launchable state for a new attempt;
5. launch at most one worker for that transition; and
6. retain a recoverable receipt if launch fails.

Use compare-and-set/transition fencing so concurrent submissions cannot
launch duplicate workers or consume an input against an obsolete attempt. A
second distinct input accepted while startup is in progress must either join
the ordered inbox for the new attempt or receive a clear retryable response;
it must never be silently dropped.

This path should be distinct in intent from retrying/resuming unfinished work.
It may share lower-level worker startup code, but must not use an ambiguous
“resume old run” operation whose behavior can replay the previous launch
intent.

### FR-4 — Deliver the new input before considering prior intent

The new worker rehydrates the same session conversation and processes the
accepted deferred message first. The old request's `intent` is not submitted as
a new user turn before or after the accepted message. If the worker cannot
safely deliver the message, it reports an explicit recoverable failure and
does not proceed by replaying the old intent.

### FR-5 — Workflow and command neutrality

The manager and supervisor do not parse input text to decide whether it means
“resume”, “continue”, “workflow”, or any other operation. `/workflow <anything>`
and all other slash commands cross the boundary unchanged. Any command
semantics, including workflow checkpoint operations, belong to the target
session's established command handler. An input-driven worker start alone
must not select a workflow or consume a workflow checkpoint.

### FR-6 — Attempt and ownership accounting

Each successful input-driven worker claim advances the attempt and establishes
a fresh lease using the existing session-store contract. The Details page,
session projections, and worker lifecycle events must consistently report the
new attempt. The previous attempt's provider error, exit reason, completion
time, and latest activity must not appear as if they describe the new attempt.

### FR-7 — Exact text, FIFO, and receipts

Accepted content is preserved byte-for-byte at the Python string level,
including whitespace and line breaks. Existing validation may reject blank or
oversized messages but must not trim, truncate, or rewrite accepted input.
Messages are processed FIFO and at most once. The UI distinguishes durable
acceptance, worker startup, target delivery, and rejection/recovery-needed
states. It must not call an inbox append “delivered” before the worker
acknowledges it.

### FR-8 — Safe failure and retry

If launch fails after enqueue, retain the accepted input as pending and show a
bounded recovery notice with a stable message ID. Do not clear the draft or
invite an immediate duplicate send if acceptance already occurred. A later
recovery action may start a worker to consume the same pending input. If the
target session is deleted before launch, the worker must not start and the
receipt must be explicitly rejected or marked unrecoverable according to the
existing deletion contract.

### FR-9 — No foreground attachment

Sending from Details never calls the attach/foreground handoff path. The
`agenthicc agents` process remains a manager, and the new worker remains the
single owner of the session for that attempt.

## 8. Persistence and concurrency requirements

- The inbox remains the durable source of truth for accepted input; do not rely
  on the manager's in-memory composer state or a transient event projection.
- Persist a reason/source for the worker reactivation (new input versus retry
  or workflow resume), either in typed lifecycle metadata or auditable events.
- Startup/reconciliation must recognize a completed session with accepted,
  unconsumed deferred input and be able to continue its launch safely after a
  crash.
- Keep existing session ownership leases authoritative. A stale manager
  snapshot cannot launch a worker after a different attempt has claimed the
  session.
- Concurrent `i` submissions to the same completed record cannot create two
  owning workers. The winning startup consumes the FIFO inbox; competing
  submissions join it or receive an explicit retryable response.
- Do not add a second session, transcript, workflow checkpoint, or ownership
  store for this feature.

## 9. User-visible behavior

- Details help text advertises `i input` for completed sessions as well as
  other eligible states.
- Pressing `i` on a completed session opens the regular composer, not an
  “Input unavailable” notice.
- On acceptance, show a receipt such as `Accepted for background recovery ·
  <message-id>`; update it as the worker starts and acknowledges delivery.
- On a definitive rejection, retain the composed draft and state the reason.
- If the worker cannot launch, show that input is saved and requires recovery;
  do not report success as delivery.
- A session selected in the table remains the target if refresh, navigation,
  or background list updates happen while the composer is open.

## 10. Security and operational constraints

- Never weaken mode, capability, workspace, tool, network, or approval policy
  to make a completed session restart.
- Reuse the original session's effective configuration and trusted workspace
  context; do not accept a manager-provided arbitrary working directory.
- Do not log message text in generic manager/supervisor logs. Receipts and
  diagnostics use bounded, redacted errors and stable IDs.
- Preserve max-worker and per-project concurrency limits. A completed-session
  input cannot bypass those limits.
- The completed session's artifact directory and durable transcript must be
  preserved; cleanup must not remove them before the new worker claims and
  consumes accepted input.

## 11. Acceptance criteria

### Unit tests

1. `_open_input_composer()` opens for `completed` and remains closed for
   `deleted` and unsupported states.
2. Eligibility uses the current selected record; the composer is fenced to the
   captured session and revision.
3. Completed-session input is persisted before the reactivation/start call.
4. The lifecycle model permits only the intended completed-to-new-attempt
   transition; ordinary terminal transitions remain constrained.
5. A completed session cannot be reactivated by a retry/resume path that would
   replay its original intent.
6. Worker startup with a deferred input delivers that exact message in the
   existing conversation before any old intent could be considered.
7. Input text such as `/workflow resume`, `/workflow goal_flow`, `continue`,
   multiline text, and whitespace-bearing content is not parsed or rewritten
   by the manager.
8. Duplicate message IDs are idempotent; conflicting reuse is rejected.
9. Concurrent submissions produce one worker owner and preserve FIFO or an
   explicit retryable result.
10. Stale attempt/lease, deletion races, queue saturation, and process-launch
    errors preserve safety and an honest receipt.
11. A worker launch failure leaves accepted input recoverable and does not
    falsely mark it delivered or lose the draft.
12. The previous attempt's terminal metadata is not presented as current
    attempt activity after reactivation.

### Integration tests

13. From the `agents` Details page, press `i`, enter a message, submit, and
    verify a new attempt starts for the same session ID without a foreground
    attach.
14. Verify transcript/session journal continuity and that the new input is the
    first new user turn delivered in the new worker attempt.
15. Verify the original launch intent is not replayed when the worker starts
    because a deferred input exists.
16. Simulate manager restart after inbox acceptance but before worker launch;
    recovery starts at most one worker and consumes the same message once.
17. Simulate worker startup/claim races and confirm attempt, lease, status, and
    receipt projections remain consistent.
18. Verify all existing PRD-215 live, stale, ask-user, approval, cancellation,
    attach, and deleted-session behavior remains intact.

### End-to-end tests

19. A user can continue a completed session through `agenthicc agents` without
    attaching; the assistant responds in the same session and the new attempt
    is visible in session details.
20. Sending `/workflow <subcommand>` through the completed-session composer
    passes it unchanged to the target; only the target command handler decides
    its effect.
21. Sending ordinary text to a workflow-backed completed session does not
    select/resume/restart a workflow or replay the previous goal.
22. A deleted session cannot receive input or be resurrected, including when
    deletion races with a submission.
23. Under provider or worker-start failure, the UI shows pending/recovery
    status, retains durable input, and does not create duplicate turns when
    the operator retries recovery.

## 12. Rollout and compatibility

This is an additive lifecycle capability and does not change the on-disk
session identity or input inbox format unless implementation inspection shows
that a versioned event is needed. Existing completed session records remain
readable and become eligible for a new input-driven attempt. No bulk migration
or automatic restart of completed sessions occurs; only an explicit user
submission from the composer triggers reactivation.

PRD-215 remains authoritative for editor parity, opaque target-bound input,
ordering, ownership, and the ten-row/deleted-session list behavior. PRD-216
supersedes only PRD-215's rule that completed sessions must reject input. All
other PRD-215 requirements remain in force.

## 13. Implementation sequence

1. Add a typed input-driven reactivation operation and define its legal state
   transition and attempt/reason metadata.
2. Extend inbox/supervisor handling for `completed` with expected-revision
   fencing, enqueue-before-launch ordering, concurrency control, and
   recoverable launch failures.
3. Ensure worker initialization consumes deferred input first and never replays
   the stored original intent on this path.
4. Add completed to the Details composer eligibility predicate and make
   receipts reflect acceptance versus delivery.
5. Add unit, integration, and TUI end-to-end regression coverage for the full
   status-to-delivery lifecycle.
6. Update the background-session guide and PRD index after behavior is
   implemented and verified.

## 14. Definition of done

From a completed session's Details page, pressing `i` opens the composer and
submitting any valid text durably targets that same conversation. The system
starts no more than one new background attempt, delivers the exact input once,
preserves transcript and policy context, and never replays the old launch
intent or performs implicit workflow recovery. Deleted sessions remain
unavailable. Input and worker status are recoverable and accurately visible
across process failures. Unit, integration, and end-to-end tests verify the
acceptance criteria above.

## 15. Implementation and verification record

Implemented across the existing background-session lifecycle, durable input
inbox, supervisor, worker bootstrap, and `agents` Details composer. Completed
input uses a compare-and-set transition to `starting`, records an
`input:<message-id>` resume marker, preserves the saved launch configuration
while clearing detached goal-run bookkeeping, and routes accepted input before
any prior intent. Periodic stale-session maintenance recovers accepted input
left pending across a manager restart. The input inbox rebinds an accepted but
undelivered live-owner message to deferred delivery, while terminal receipts
remain idempotent and do not start empty workers.

Verification in the implementation checkout:

- `uv run pytest tests/ -q` — **4,105 passed, 16 skipped**.
- Focused background-session unit, integration, and Details-page E2E tests —
  **91 passed**.
- `uv run ruff check src/ tests/ scripts/` — passed.
- `uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json`
  — passed.
- `uv run mypy` on the five changed runtime modules — passed.
- Repository-wide `uv run mypy src/agenthicc` remains blocked by 250 errors in
  existing unrelated modules. Repository-wide `ruff format --check` identifies
  eight unrelated files requiring formatting; the changed implementation and
  test files are formatted.
