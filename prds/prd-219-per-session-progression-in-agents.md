---
title: "PRD-219: Drive and observe per-session progression in `agenthicc agents`"
status: Proposed
version: 0.1.0
date: 2026-10-07
repository: jymchng/agenthicc
related_prds:
  - PRD-141  # Background sessions and lifecycle
  - PRD-150  # Client-neutral session projection
  - PRD-206  # Paginated agents manager
  - PRD-209  # Low-latency agents TUI
  - PRD-211  # Attempt-consistent status and activity
  - PRD-204  # Goal-run and worker projections
  - PRD-215  # Targeted input from session details
  - PRD-216  # Input to completed sessions
  - PRD-217  # Opaque session-input forwarding
  - PRD-218  # Same-session detach after a completed attempt
tags:
  - agents-manager
  - background-sessions
  - progression
  - resume
  - observability
---

# PRD-219 — Drive and observe per-session progression in `agenthicc agents`

## 1. Executive summary

Investigate why `agenthicc agents` can look like it is not driving the
individual sessions it lists, and define the implementation that makes those
sessions explicitly controllable and their real progress legible.

The source shows that `agents` is currently a background-session projection and
limited control surface, not a persistent scheduler or agent loop. A
background worker is a finite process: it claims one durable session, runs a
workflow attempt or one direct turn (plus already-accepted input), writes the
outcome, and exits. The manager refreshes the projection, reconciles stale
workers, and accepts some actions, but it does not automatically restart
completed, failed, or orphaned sessions. That safety boundary prevents
unrequested model calls and replay of side effects.

The perceived lack of progression has three causes:

1. **Control gap:** the `agents` TUI has no explicit per-session Resume or
   Retry action, even though `agenthicc jobs resume SESSION_ID` and
   `agenthicc jobs retry SESSION_ID` exist. The UI's Enter path attaches the
   whole selected session to the foreground; it is not a background resume.
2. **No autonomous scheduler:** normal worker completion is terminal for that
   attempt. Periodic manager maintenance detects dead/stale workers and
   recovers accepted deferred input; it does not generally restart every
   terminal session. This is intentional and must remain so unless a user
   explicitly requests another operation.
3. **Weak progress signal:** a one-second heartbeat updates `last_active` and
   `latest_activity` to `Worker active`; the heartbeat does not publish a
   workflow phase. The manager can therefore show a live worker without
   showing what it is doing, and can make liveness look like progress.

PRD-219 makes `agents` a genuine **per-session control plane**: it exposes
state-appropriate, explicit actions that start or resume work through the
existing supervisor and session runtime; shows which phase/operation is
actually advancing; and reports when a session is waiting, stalled, complete,
or needs an operator decision. It does not invent prompts, auto-continue
workflows, or reinterpret input. A selected session's literal submitted input
continues to obey PRD-217.

## 2. Investigation findings

### 2.1 Existing execution and control paths

| Concern | Current behavior | Evidence in source | User-visible consequence |
|---|---|---|---|
| `agenthicc agents` entry | Opens `run_background_manager`; JSON mode only lists background sessions. | `src/agenthicc/cli/commands/background.py`, `agents()` | The command is not itself a workflow runner. |
| Worker startup | `BackgroundSupervisor._launch()` writes a request and starts one subprocess. | `src/agenthicc/background/supervisor.py` | There is no persistent in-process agent loop owned by the TUI. |
| Worker lifetime | `run_worker()` runs the selected workflow or direct turn, drains accepted input, finalizes the attempt, and exits. | `src/agenthicc/background/worker.py`, `run_worker()` | A completed attempt will not advance again unless another explicit operation arrives. |
| Manager maintenance | `recover_stale_batch()` reconciles worker liveness and retries only accepted deferred-input starts. | `BackgroundManagerService.maintain_async()`; `BackgroundSupervisor.recover_stale_batch()` and `_recover_pending_input_records()` | An orphaned record without pending input remains orphaned until an operator resumes/attaches it. |
| Manager actions | The UI has inspect, attach, input, approval, cancel, archive, pin, and delete paths. It has no Resume/Retry key action. | `BackgroundManager.handle_key()` and help/footer in `src/agenthicc/tui/workspace/background_manager.py` | Recovery requires a separate CLI command or foreground attach. |
| CLI recovery | `jobs resume` and `jobs retry` call `BackgroundSupervisor.resume()` / `.retry()`. | `src/agenthicc/cli/commands/background.py`, `_mutate()` | The lifecycle capability exists but is not available where the session is being inspected. |
| Goal-run view | `agents --run RUN_ID` shows a run projection; Enter attaches the main session and `c` cancels the run. It has no selectable per-agent session controls. | `src/agenthicc/tui/workspace/goal_run_manager.py` | Worker rows are informational rather than individually actionable. |
| Liveness display | Heartbeat runs every second with `activity="Worker active"` and no phase argument. `BackgroundStore.heartbeat()` assigns that activity to `latest_activity` and advances `last_active`. | `src/agenthicc/background/worker.py`; `src/agenthicc/background/store.py` | Updated/active indicates worker liveness, not task progress. |
| Phase display | Phase history/current phase are updated from the final worker outcome; the periodic heartbeat retains the current phase unchanged. | `worker._outcome_changes()` and heartbeat call site | Long-running work can appear phase-static until the attempt ends. |

### 2.2 Why current behavior is not automatically a defect

`BackgroundSession` is the durable conversation/session identity; each worker
process is an attempt against that identity. A successful `COMPLETED` status
means that the last attempt ended successfully, not that the session is a
permanent daemon waiting to call the model again. Automatically starting
another attempt could repeat a completed request or side effect. Similarly,
automatically restarting every orphan could race an unknown live owner or
repeat a tool call whose durable receipt is incomplete.

Therefore the fix is not to make the UI inject a synthetic “continue” message
or to restart sessions just because they are listed. It is to expose explicit
operator intent as typed lifecycle operations and show reliable progress and
eligibility evidence.

### 2.3 Scope assumption

For this PRD, “drive progression” means **allow the operator to advance the
exact selected session through safe, explicit lifecycle actions, and make the
resulting execution progress observable**. It does not mean an always-on
autonomous loop that invents follow-up requests. The default remains
user-driven. Any future unattended retry policy must be a separately
configured, opt-in feature with its own idempotency and approval contract.

## 3. Problem statement

Users can see multiple sessions in `agenthicc agents`, but the interface does
not answer all of the questions needed to manage them:

- Is the session still executing, waiting for a person, or merely reporting a
  recent heartbeat?
- If it stopped, is there a valid checkpoint or interrupted turn to resume?
- Which explicit action will cause the selected session to do more work?
- Can one worker session be resumed without attaching to or cancelling the
  parent goal run?
- Did the request start, reach a worker, advance a workflow phase, or fail
  before doing so?

The operator must leave the manager and know `jobs resume`/`jobs retry`, or
attach to the foreground and operate the session there. For
`agents --run`, visible worker rows do not expose their linked session IDs as
selectable action targets. This makes the screen look like an observer even
though the underlying supervisor can perform some of the needed operations.

## 4. User stories

1. As a user, I can choose one exact session in `agenthicc agents`, select
   Resume or Retry when that operation is safe, and see the same session ID
   enter a new attempt.
2. As a user, I can tell the difference between a live heartbeat and actual
   progress through a phase, tool, or turn.
3. As a user, I can inspect why a session is not advancing and see whether the
   correct action is to answer, approve, resume, retry, attach, or take no
   action.
4. As a user viewing `agenthicc agents --run RUN_ID`, I can select a linked
   worker session and act on that worker without accidentally targeting the
   main session or whole run.
5. As an automation client, I can request the same lifecycle operation using
   the CLI/service surface and receive a stable operation receipt.

## 5. Goals

### Product goals

- Put explicit per-session resume/retry controls in the `agents` UI.
- Make all actions target a stable session ID and current attempt, never a row
  index or a run ID by accident.
- Distinguish worker liveness from forward progress.
- Explain the current wait/recovery condition and show applicable actions.
- Make individual worker sessions selectable from the goal-run view.
- Preserve the opaque input pass-through contract from PRD-217.

### Engineering goals

- Reuse `BackgroundSupervisor`, `BackgroundManagerService`, the existing
  session lease, workflow checkpoint, conversation journal, input inbox, and
  idempotency contracts.
- Keep one canonical lifecycle implementation shared by CLI and TUI.
- Persist operation acceptance before launching a process and provide
  attempt-fenced status receipts.
- Avoid duplicate workers, duplicate turns, stale owner writes, and replay of
  successfully completed side effects.
- Keep TUI input/render latency bounded while work is started or inspected.

## 6. Non-goals

- Keeping one process alive forever for every conversation.
- Automatically sending “continue”, the original intent, or any other
  synthetic user message to a session.
- Automatically resuming all failed/orphaned sessions on manager startup.
- Replacing the workflow runner with a manager-owned phase engine.
- Changing the meaning of text, slash commands, skills, or mentions submitted
  from the session input composer.
- Creating a second session store, transcript, worker runtime, or checkpoint
  format.
- Semantically resolving merge conflicts or taking over a live worker whose
  ownership cannot be proven.
- Adding automatic retries for provider/tool failures in this PRD.

## 7. Product and lifecycle contract

### 7.1 Identity

```text
session_id  = durable conversation and execution identity
attempt     = one worker execution against that session
run_id      = optional goal-level aggregate identity
agent_id    = run projection identity, linked to session_id when available
```

Every UI and CLI control operation must resolve to a concrete `session_id` and
include the attempt/lease revision observed when the action was offered. A
goal-run action may target the whole run only when explicitly labelled as a
run-level operation.

### 7.2 Explicit progression, not implicit prompting

Progression is requested by one of the following distinct actions:

- **Answer / input:** deliver the exact submitted text to the session's normal
  input path; the manager does not rewrite or interpret it (PRD-217).
- **Approve / reject:** settle the exact pending approval through the existing
  approval service.
- **Resume:** continue an eligible interrupted session from validated durable
  state/checkpoint without re-submitting the original intent as a new user
  message.
- **Retry:** request a new attempt for an eligible failed operation only when
  the existing recovery/idempotency evidence supports it. A completed action
  must not be replayed just because the operator pressed Retry.
- **Attach:** transfer ownership to the foreground TUI; it does not itself
  start a second worker or imply a new user turn.

The UI must not translate Resume into a fake composer message. Internal
checkpoint restoration is runtime state, not user-authored transcript content.

### 7.3 Worker attempt lifecycle

One attempt has a truthful lifecycle:

```text
accepted → starting → running ↔ waiting_for_user → completed | failed | cancelled | orphaned
```

When a worker completes a one-shot request, that attempt is terminal. The
session remains addressable and can receive a later explicit input. Session
completion does not schedule another model call by itself.

### 7.4 Separate liveness and progress

The system must represent at least two independent clocks/signals:

- `last_heartbeat_at` / worker health: evidence that the process is alive and
  still owns the attempt;
- `last_progress_at` plus a bounded `progress_kind`/`progress_summary`:
  evidence that meaningful agent work advanced, such as turn start, workflow
  phase entry/exit, tool start/completion, approval/input wait, or turn finish.

Heartbeats must not overwrite meaningful `latest_activity` or advance
`last_progress_at`. A live heartbeat with no recent progress is “live, no
recent progress observed”, not silently “progressing” and not automatically
“dead”. Provider calls may legitimately take a long time; lack of progress is
an attention signal, not authorization to kill or restart the worker.

## 8. State-aware action contract

The manager computes offered actions from the latest durable record and
validated recovery evidence. The following is the baseline; the exact action
set must be exposed by a shared policy function, not reimplemented in key
handlers.

| Session state/evidence | Actions shown | Progression behavior |
|---|---|---|
| `queued` / `starting` | Inspect, cancel | Show launch/claim operation state; do not launch a duplicate. |
| `running` | Inspect, send input, cancel, attach | Input follows PRD-217; attach performs the existing single-owner handoff. |
| `waiting_approval` | Approve, reject, cancel, inspect | Only the pending approval can be settled; text is not treated as approval. |
| `waiting_input` | Answer, cancel, inspect | Preserve existing ask-user response path and exact submitted value. |
| `orphaned` with resumable checkpoint/interrupted turn | Resume, inspect, attach, cancel/archive | Resume same session from validated durable cursor. |
| `orphaned` without safe recovery evidence | Inspect, attach, archive; possibly send a new explicit input | Explain why resume is unavailable; do not replay the original request. |
| `failed` with recoverable evidence | Resume; Retry only if separately determined safe; inspect/attach/archive | Show failure class and recovery decision before starting. |
| `failed` with non-replayable/unknown evidence | Inspect, attach, archive; send new input where valid | No blind replay. Surface actionable diagnosis. |
| `cancelled` | Resume if state is recoverable, inspect, attach, archive | New attempt keeps the session ID and prior attempt history. |
| `completed` | Inspect, attach, archive; send new input | No implicit resume/retry of completed work. New input is a new literal user submission. |
| `archived` | Restore, inspect | Resume only after explicit restore and normal eligibility checks. |
| `deleted` | No execution actions | Remains excluded from ordinary session results; restore is a separate trash operation. |

No action may be enabled from a stale cached record without a fresh
attempt/lease validation at the supervisor boundary.

## 9. Functional requirements

### FR-1 — Add explicit per-session Resume and Retry to `agents`

The detail view and keyboard/help affordances shall expose Resume and Retry
when the shared eligibility policy says they are safe. The user can choose an
action from a visible menu or documented shortcut; hidden/unlisted shortcuts
are not sufficient. The interface must label which operation it will perform
and show the selected session ID/title before starting.

The operation reuses the existing session record, conversation, journal,
workflow checkpoint, mode/security configuration, and prior attempt history.
It must not create a new session or silently attach to foreground.

### FR-2 — Use one canonical operation service

Add async manager-service methods for resume/retry (and any required
eligibility inspection) that call the same supervisor policy as CLI
`jobs resume`/`jobs retry`. The CLI and TUI must not maintain separate state
transition logic.

Each request carries an operation/message ID, exact `session_id`, expected
attempt, and expected lease/revision. Duplicate request IDs are idempotent;
stale revisions fail with a safe “session changed; refresh and retry” result.
The TUI renders `accepted`, `starting`, `completed`, or `failed` receipts and
refreshes the record after the operation.

### FR-3 — Validate recovery before exposing Resume or Retry

The runtime shall inspect existing durable evidence before launch, including
the session status, worker/owner lease, workflow identity and checkpoint
cursor, interrupted-turn journal state, pending accepted input, and
attempt-scoped idempotency receipts. It must:

- resume an existing eligible workflow checkpoint rather than restart phase
  one;
- recover an interrupted logical turn through its existing recovery path;
- never replay `session.intent` merely because it is stored on the session;
- never repeat a tool side effect whose completion receipt is durable;
- reject ambiguous/corrupt recovery state with an actionable diagnostic;
- retain the current single-owner guarantee across races with attach, input,
  cancel, and another resume request.

Retry is not a synonym for Resume. If a retry would repeat non-idempotent work,
the UI must disable it or require an explicit confirmation that names the
operation and risk. A user can still choose Attach or submit a new literal
input where the session contract permits it.

### FR-4 — Make goal-run agent rows individually addressable

In `agenthicc agents --run RUN_ID`, the agents table shall support selection
of each `RunAgentRecord` with a non-empty `session_id`, preserve selection by
that ID across refresh, and offer the same eligible per-session actions as the
main manager. The main session and each worker are separate targets. Run-level
Cancel remains explicitly run-level.

If a run projection row has no session ID or points to a missing/deleted
session, show it as non-actionable with a reason; never substitute the main
session ID. Attach must attach the selected exact session. Worker resume/retry
must retain its worker/task/worktree/branch/run association and must not
accidentally route through main-session workflow recovery.

### FR-5 — Publish real progress signals

The worker/runtime shall publish bounded progress events at meaningful
boundaries already available in the runtime, including:

- attempt accepted, worker claimed, mode applied;
- direct turn started/completed;
- workflow run selected, phase entered, phase transition/checkpoint committed;
- tool call started/completed/failed when such events are already observable;
- approval/input wait entered and resolved;
- recovery started and selected checkpoint/turn boundary;
- worker finalization.

These signals are session/attempt fenced and persisted through the existing
background event projection. Avoid emitting one durable write per token or
stream chunk. The progress payload is bounded, redacted, and does not contain
prompts, tool arguments, secrets, or provider response bodies.

### FR-6 — Do not confuse heartbeat with progress

The heartbeat updates worker liveness only. It must not replace a meaningful
activity summary with a repeated generic string, nor be presented as task
progress. Existing consumers of `last_active` must be audited before changing
its compatibility meaning; introduce explicit fields if necessary.

The UI shows distinct labels, for example:

```text
Worker: alive · heartbeat 2s ago
Progress: IMPLEMENT · tool completed · 18s ago
```

If a worker is live but has not emitted progress for a configured/display
threshold, show “No recent progress observed” and retain inspect/cancel/attach
actions. The manager must not automatically kill, retry, or send input due to
this condition.

### FR-7 — Explain why a session is not advancing

Details shall show a derived, bounded state explanation, such as:

- waiting for approval/input (with the existing request summary);
- running provider/tool/workflow phase;
- worker exited successfully; explicit input is needed for another turn;
- worker disappeared; resumability is being checked;
- no safe checkpoint/interrupted turn found;
- resume/retry request accepted but waiting for capacity/claim;
- action failed due to a live owner, capacity limit, policy, or invalid state.

The explanation is derived from authoritative status, event/activity, and
checkpoint evidence. It must not assert that a model call is active based only
on a stale `running` status or heartbeat.

### FR-8 — Preserve input pass-through semantics

The manager's input composer remains purpose-agnostic. It forwards the exact
submitted text unchanged to the target session, including slash commands such
as `/workflow ...`. It must not convert input into Resume, invent a
“Workflow continuation” message, re-submit the original intent, or use input
text to choose a session/control operation. Lifecycle actions are separate
explicit UI controls.

### FR-9 — Show operation and capacity failures truthfully

If `max_workers`/per-project capacity prevents an explicit resume or retry,
the request must not appear to have started. The manager shows the exact
target, capacity error, and safe next action. If a durable dispatch queue is
selected during implementation, queue acceptance and execution must be
separate states with an observable queue position; an accepted queued request
must eventually be dispatched or fail durably, even if the TUI closes. The
queue must never infer intent from a terminal session status.

### FR-10 — Maintain background/foreground ownership

Only one worker or foreground runtime may own a session attempt. Resume,
attach, and input must share the session lease and compare-and-set transitions.
Do not run a foreground model loop from inside the manager while a background
worker owns the same session. A failed handoff must leave the prior owner or a
recoverable state intact.

### FR-11 — Keep activity/history privacy-safe

Progress summaries and action receipts use existing redaction and bounds.
Detailed model prompts, user input, raw tool payloads, provider headers, and
secrets must not be copied into the background registry as progress metadata.
The session transcript remains the canonical place for user/assistant content.

## 10. Proposed interaction flow

```text
operator selects session/worker by stable session_id
        │
        ▼
manager refreshes authoritative record and recovery eligibility
        │
        ├─ waiting input/approval → existing answer/approve controls
        ├─ live worker            → inspect / exact input / cancel / attach
        ├─ recoverable terminal   → explicit Resume or eligible Retry
        └─ completed              → inspect / attach / send a new input
        │
        ▼
typed operation request (session_id + attempt + lease + operation_id)
        │
        ▼
BackgroundManagerService → canonical BackgroundSupervisor
        │                         │
        │                         ├─ validate state/checkpoint/idempotency
        │                         ├─ compare-and-set new attempt
        │                         └─ launch existing worker runtime
        ▼
durable operation receipt + session progress events
        │
        ▼
manager refreshes same selected session and shows real attempt progress
```

For `agents --run`, selecting a worker row first resolves its linked
`session_id`; all later steps use that ID. The run ID remains context and
aggregation metadata, not a substitute execution target.

## 11. Non-functional requirements

### Reliability

- A manager crash after request acceptance must not lose or duplicate an
  operation.
- Competing resume, retry, attach, input, and cancel actions must have one
  durable winner or a clear conflict result.
- A worker crash during resume remains recoverable from the same session and
  preserves previous attempt history.
- A transient progress-event write failure must not corrupt the transcript or
  checkpoint; use the existing bounded diagnostic behavior.

### Performance

- Resume eligibility inspection must be bounded to the selected session and
  relevant checkpoint/journal tail; it must not fold all session histories on
  the TUI event loop.
- Progress writes must be boundary-based, not token-based.
- Navigation and rendering remain responsive during worker startup, recovery
  inspection, and operation polling.
- Large goal runs use paginated/viewport-bounded rendering while preserving
  stable selected IDs.

### Security and compatibility

- The worker inherits the same workspace, provider, mode, approval, network,
  and tool policy as the canonical session runtime.
- The feature adds no privilege to background or worker sessions.
- Existing CLI `jobs resume/retry`, `agents` JSON, `agents --run`, and input
  forwarding remain compatible; behavior is unified rather than replaced.
- Old persisted session records without new progress fields remain readable
  and display “progress telemetry unavailable” instead of fabricated data.

## 12. Acceptance criteria

1. The selected session's available actions are visibly documented and agree
   with its freshly validated state and recovery evidence.
2. An operator can resume an orphaned/cancelled eligible session from
   `agenthicc agents` without leaving the manager; its `session_id` remains
   unchanged and attempt history increments exactly once.
3. A safe failed attempt offers the correct recovery action; an unsafe or
   ambiguous replay is rejected with a specific explanation and no provider
   request.
4. A completed session is not automatically restarted on manager open or
   refresh. A new turn occurs only after explicit input or another explicitly
   supported operation.
5. Input text submitted from the manager is byte-for-byte/string-for-string
   preserved through the inbox and delivered to the target; `/workflow` input
   does not cause manager-generated continuation text.
6. In `agents --run`, selecting a worker targets its linked session; selecting
   or cancelling the main run cannot be confused with acting on a child.
7. When a worker is active, heartbeat time changes without overwriting the
   latest meaningful progress event or phase.
8. A long provider call with a healthy heartbeat and no new progress is
   displayed as live/no-recent-progress, not falsely as advancing or dead; no
   automatic retry/kill occurs.
9. Workflow phase entry and committed phase transitions appear in the
   selected session's progress view before worker finalization.
10. Simultaneous stale UI actions are rejected by attempt/lease fencing and do
    not target a neighboring row after refresh.
11. A duplicate operation ID does not start a second worker/attempt.
12. Worker-slot exhaustion is reported as not started, or as a durable queued
    operation with truthful queue status if the implementation elects to add
    the dispatcher.
13. TUI remains responsive while recovery inspection or worker startup is in
    flight; progress and errors arrive without blocking key input.
14. No progress metadata contains API keys, environment values, provider
    request bodies, full prompts, tool arguments, or unredacted transcripts.
15. Existing `jobs resume`, `jobs retry`, input, attach, and cancel semantics
    continue to work through the shared operation implementation.

## 13. Verification plan

### Unit tests

- State-to-action eligibility for every `SessionStatus`, including missing,
  stale, corrupt, archived, and deleted records.
- Recovery policy separates Resume from Retry and denies unsafe replay.
- Operation idempotency and expected-attempt/lease compare-and-set behavior.
- Heartbeat updates liveness without replacing progress/activity.
- Progress event redaction, payload bounds, and legacy-record defaults.
- Goal-run row selection maps exact `agent_id`/`session_id` and never falls
  back to the main session.

### Integration tests

- `BackgroundManagerService` resume/retry to supervisor to worker launch.
- Workflow checkpoint resume preserves the active phase, summaries, artifacts,
  and idempotency receipts.
- Interrupted direct-turn recovery uses journal recovery and does not submit
  the original request as a duplicate turn.
- Concurrent attach/input/resume/cancel produces one owner and a durable
  operation receipt.
- Worker heartbeat and lifecycle/progress events update distinct timestamps
  and projections.
- A progress-event persistence failure does not lose final outcome or damage
  the session journal.

### End-to-end tests

- Select an orphaned session in the actual scripted TUI, choose Resume, and
  verify the same session advances from its stored checkpoint.
- Select a goal-run worker and resume only that worker session; verify main
  session and other workers are untouched.
- Complete a background turn, open/refresh `agents`, and prove no model call is
  made until explicit input/action.
- Send a literal slash command through the details composer and verify no
  synthetic continuation/user event is added.
- Simulate a healthy heartbeat during a delayed provider call and verify UI
  liveness/progress labels remain distinct.
- Exercise worker-capacity failure or queued operation, manager exit/reopen,
  and eventual truthful completion if queueing is implemented.

## 14. Implementation phases

### Phase 0 — Confirm recovery eligibility contract

Map existing workflow checkpoint, direct-turn journal recovery, background
input inbox, attempt history, and side-effect receipt APIs. Define one typed
eligibility/result type with actionable denial reasons. Do not introduce a
second resume implementation.

### Phase 1 — Shared per-session operation surface

Add async Resume/Retry service calls and state-aware action availability to
the `agents` detail view. Make `jobs resume/retry` use the same supervisor
operation semantics. Add stable operation receipts and concurrency fencing.

### Phase 2 — Progress telemetry

Separate heartbeat/liveness from meaningful progress; wire existing worker,
workflow, and tool lifecycle boundaries into bounded session progress updates.
Keep compatibility with old records and public JSON.

### Phase 3 — Goal-run worker selection

Make agent rows navigable and actionable using each row's exact session ID.
Keep run-level actions separate and clearly labelled.

### Phase 4 — Capacity behavior and operational hardening

Decide whether explicit accepted resume/retry requests need a durable
capacity queue. If yes, implement it without dependence on the TUI staying
open; otherwise expose deterministic capacity rejection and allow explicit
retry after capacity frees. Test crash/restart and duplicate dispatch paths.

## 15. Decisions and open questions

### Decision: no automatic continuation

Opening or refreshing `agenthicc agents` must never itself trigger an LLM call.
Only a live worker, a valid waiting response, or an explicit user action can
advance work. This preserves safety and PRD-217's exact-input contract.

### Open question: automatic retries

Should provider-transient retries remain wholly inside the current worker
attempt (current transport behavior), or should a later PRD expose retry
budgets and retry scheduling in the manager? PRD-219 does not add autonomous
retries.

### Open question: queued explicit progression

If a user explicitly requests Resume while all worker slots are occupied,
should Agenthicc reject immediately or durably queue the request? The
implementation phase must choose based on current worker finalization and
recovery architecture. Either choice must distinguish “accepted” from
“started”; silently leaving a session unchanged is not acceptable.

## 16. Definition of done

The `agents` surfaces can explain why each selected session is or is not
advancing, offer and execute the safe explicit operation for that exact
session, show durable progress distinct from worker liveness, and recover
through existing session/workflow state without injecting prompts or
duplicating completed work. The same lifecycle policy is used by the TUI and
CLI, and the critical journeys above have deterministic unit, integration,
and E2E coverage.
