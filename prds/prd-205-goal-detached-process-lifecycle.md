---
title: "PRD-205: Self-terminating detached goal runs and PID reporting"
status: Implemented
repository: jymchng/agenthicc
depends_on:
  - PRD-204
  - PRD-141
  - PRD-148
  - PRD-173
---

# PRD-205 — Self-terminating detached goal runs and PID reporting

## 1. Summary

Improve the detached goal entry point so that:

```bash
agenthicc --goal "Write a PRD that suggests 20 extensive features to greatly enhance this library" \
  --mode YOLO \
  --detach
```

starts one durable `goal_flow` run, reports the operating-system process ID of
the detached worker, and leaves the launcher immediately. Only the detached
worker may use the self-termination lifecycle defined by this PRD. The
attached `agenthicc --goal GOAL` process must never install or invoke this
finalizer. The detached worker must then terminate itself after it reaches a
terminal outcome:

1. `goal_flow` completes;
2. an irrecoverable error is recorded; or
3. the worker has returned from thinking to a confirmed idle state and has no
   pending user input, approval, retry, or workflow continuation.

The run record, session journal, workflow checkpoint, failure reason, and final
process metadata must be persisted before the worker exits. The process must
not remain alive merely because a background event loop, TUI animation,
heartbeat, terminal reader, or optional integration task is still running.

This is a follow-up to PRD-204. It does not introduce another worker runtime,
another session store, or another workflow implementation. It tightens the
existing `BackgroundSupervisor`, `BackgroundSession`, `GoalRunManager`,
`goal_flow`, and headless-worker lifecycle contracts.

## 2. User-facing example

The human-readable detached-start response must include the worker PID:

```text
Agenthicc detached run started

Run ID:  run_a4633f422fe44957905030ca6807c186
Goal:    Write a PRD that suggests 20 extensive features to greatly enhance this library
Status:  running
Session ID: 89dcaf5be09344789b3d2731c9eb2c93
PID:     3845622

Track:   agenthicc agents --run run_a4633f422fe44957905030ca6807c186
Attach:  agenthicc attach run_a4633f422fe44957905030ca6807c186
```

The JSON response must contain equivalent stable fields:

```json
{
  "run_id": "run_a4633f422fe44957905030ca6807c186",
  "workflow_name": "goal_flow",
  "detached": true,
  "status": "running",
  "main_session_id": "89dcaf5be09344789b3d2731c9eb2c93",
  "pid": 3845622,
  "worker_pid": 3845622
}
```

`pid` is the compatibility/display field. `worker_pid` is the canonical field
for the detached Agenthicc worker. If the implementation also exposes the
short-lived CLI launcher PID, it must use a distinct field such as
`launcher_pid`; it must never overload `pid` with the launcher PID.

## 3. Current behavior and gap

PRD-204 already maps `--goal` to the registry-validated `goal_flow` workflow
and supports `--detach`. The current detached response reports the run ID,
goal, status, main session, tracking command, and attach command, but does not
report the spawned worker PID. The run projection can contain a process ID on
an agent record, but the start response does not reliably expose it.

The current supervisor also treats process launch, session state, workflow
completion, and process shutdown as related but separate concerns. This leaves
several failure modes to investigate and close:

* the launcher may return before the persisted worker PID is visible;
* a very short-lived worker may finish before launch metadata is written;
* a completed workflow may leave an asyncio task, terminal manager, or
  heartbeat alive;
* an irrecoverable provider/workflow error may be persisted while the worker
  process remains alive;
* an idle agent state may be confused with a waiting-for-input or
  waiting-for-approval state;
* an ordinary detached worker may be killed by a cleanup path that belongs to
  the CLI launcher rather than the worker itself;
* a parent terminal closing must not leave an untracked orphan, but attaching
  or inspecting a run must not kill a healthy worker.

## 4. Goals

### G1 — Report the useful PID

After a successful detached start, display and serialize the PID of the
running worker that owns the main goal session.

### G2 — Make detached completion finite

A detached `goal_flow` worker must exit after its final outcome is durable. No
background task may keep the detached worker alive after the run is terminal.
This requirement MUST NOT change the lifetime of an attached TUI process.

### G3 — Preserve durable recovery

Self-termination must happen only after the run, session, workflow result,
checkpoint/failure evidence, and process-exit metadata are persisted. A user
must still be able to inspect the run after the process disappears.

### G4 — Distinguish terminal idle from user waits

The worker may self-terminate after a genuine `Thinking → IDLE` boundary only
when there is no pending approval, question, retry, continuation, or active
workflow phase. `WAITING_INPUT`, `WAITING_APPROVAL`, and recoverable retry
states remain durable and inspectable rather than being misclassified as
successful idle completion.

### G5 — Keep ownership safe

Only the detached worker may request its own final termination. The launcher,
TUI, an attached client, and an unrelated process must not terminate an
unrelated PID because of stale or corrupted metadata.

### G6 — Preserve attached behavior

`agenthicc --goal GOAL` without `--detach` continues to use the normal TUI
experience. The self-termination contract applies to the detached worker,
not to the user's interactive TUI process.

## 5. Non-goals

This PRD does not:

* change the meaning of `--goal`: it remains equivalent to selecting
  `goal_flow` and submitting the goal as the initial intent;
* add an arbitrary workflow selector to `--goal`;
* turn the run store into a process supervisor;
* guarantee that an operating-system process survives machine shutdown;
* kill processes based only on a PID read from untrusted or stale storage;
* terminate an attached TUI when its workflow becomes idle;
* delete logs, journals, checkpoints, worktrees, or failed-run evidence;
* replace `BackgroundSupervisor` with a daemon, service manager, or remote
  scheduler.

## 6. Terminology and process identities

### 6.1 Launcher process

The short-lived process executing the `agenthicc --goal ... --detach` CLI
command. Its job ends after the run and worker launch are durably accepted.
Its PID is not the PID users need to monitor.

### 6.2 Detached worker process

The child process launched by `BackgroundSupervisor` that executes the existing
headless session runner. This is the canonical `worker_pid` and the PID shown
in detached-start output.

### 6.3 Run ID

The durable product-level identifier, for example `run_<opaque-id>`. A run ID
is not a PID, session ID, workflow-run ID, or Git worktree ID.

### 6.4 Main session ID

The durable conversation/session identity owned by the detached worker. It is
used for attach, journal replay, checkpoint recovery, and provider history.

### 6.5 Terminal outcome

A persisted `completed`, `failed`, `cancelled`, or `lost` run state, with a
corresponding final session status and result/failure evidence.

## 7. Functional requirements

### FR-1 — Canonical detached command

`--goal GOAL --detach` MUST:

1. validate that `GOAL` is non-empty;
2. resolve `goal_flow` through the workflow registry;
3. validate the repository/configuration before launching;
4. create one durable run record;
5. create one durable main background session linked to that run;
6. launch the worker through `BackgroundSupervisor`;
7. wait until the session metadata is sufficient to identify the worker;
8. print the run ID and worker PID; and
9. return without waiting for workflow completion.

The command must not submit the goal twice, create duplicate main sessions, or
start a second workflow runner while waiting for PID metadata.

### FR-2 — PID acquisition and race handling

The supervisor must persist `worker_pid` as part of the launch transaction or
as an immediately reconciled follow-up. The launcher must handle all of these
cases deterministically:

* the worker is still active and has a valid PID;
* the worker finishes before the parent records launch metadata;
* the worker fails to start;
* the process ID is unavailable because the platform cannot provide it;
* the worker reaches a terminal state before the response is rendered.

If the worker is already terminal, the response must report the terminal run
status and the final known PID when available. It must not claim `running`.
If launch fails before a worker PID exists, the command returns the existing
launch failure contract and records the failure durably.

The implementation must not use a fixed sleep as its only race solution. It
must use persisted state, bounded polling, or an atomic supervisor result.

### FR-3 — Human-readable and JSON output

For detached starts, human-readable output MUST include:

* `Run ID`;
* `Goal`;
* `Status`;
* `Main` session ID;
* `PID` for the detached worker;
* `Track` command; and
* `Attach` command.

JSON output MUST include:

* `run_id`;
* `goal`;
* `workflow_name` equal to `goal_flow`;
* `detached: true`;
* `status`;
* `main_session_id`;
* `pid` and `worker_pid` when known;
* `created_at`/`started_at` when known; and
* a structured error/failure field when launch is not successful.

The output must never expose API keys, provider headers, session contents, or
secret override values.

### FR-4 — PID in all relevant projections

The canonical worker PID must be available in:

* detached-start text and JSON output;
* `agenthicc runs show RUN_ID`;
* `agenthicc agents --run RUN_ID` and its TUI projection;
* the client-neutral session/run projection where process metadata is already
  exposed; and
* persisted run/agent records used for recovery.

The PID is advisory process metadata. Run and session state remain the source
of truth for lifecycle status.

### FR-5 — Detached-only terminalization after workflow completion

When the existing `goal_flow` runner returns a successful terminal result in a
`--detach` worker, that worker MUST execute this ordered shutdown protocol:

1. stop accepting new goal input;
2. drain or cancel owned child tasks and optional integrations;
3. persist the workflow result and final phase metadata;
4. persist the main session as completed;
5. persist the run as completed, including `completed_at`, exit code, and
   result summary;
6. persist a worker-exit event containing the run ID, session ID, PID, reason,
   and exit code;
7. release the session lease and close owned resources; and
8. terminate the worker process with a success exit code.

The finalization sequence must be idempotent. A retry, signal, or duplicate
cleanup callback must not overwrite a completed result with `failed`, append a
second final result, or delete durable evidence.

### FR-6 — Detached-only irrecoverable-error terminalization

An error in a `--detach` worker is eligible for self-termination when the
existing recovery policy classifies it as irrecoverable or the bounded retry
budget is exhausted. This
includes, subject to the current lauren-ai/provider classification:

* invalid configuration or malformed workflow state;
* unrecoverable checkpoint/topology validation failure;
* authentication or authorization failure that cannot be retried;
* an exhausted bounded retry sequence; and
* an explicit fatal workflow failure.

Transient provider errors, active retry states, waiting approvals, and waiting
questions MUST NOT be converted into an irrecoverable exit merely because one
attempt failed.

For an irrecoverable error in detached mode, the worker MUST:

1. stop retrying according to the existing bounded policy;
2. preserve the last valid conversation/tool/workflow state;
3. persist a structured failure reason and error classification;
4. persist the run/session terminal state;
5. emit a worker-exit event with `exit_reason: irrecoverable_error`; and
6. terminate itself with a non-zero exit code.

### FR-7 — Detached-only confirmed `Thinking → IDLE` shutdown

The detached worker MUST recognize a confirmed outer activity boundary in which
the agent was actively thinking and then became idle. The boundary must be
based on the canonical session/agent state or durable event stream, not on a
single animation frame or a stale snapshot.

The worker may self-terminate for this reason only if all are true:

* the prior meaningful state was `Thinking`/active agent work;
* the subsequent state is canonical `IDLE`;
* no workflow phase is still executing;
* no tool call, provider retry, compaction, or child worker is active;
* no approval or question is pending;
* no continuation marker or queued user input exists; and
* the run has a durable summary/result or an explicit idle outcome.

The persisted exit reason is `idle_after_thinking`.

`WAITING_INPUT`, `WAITING_APPROVAL`, `RETRYING`, `CANCELLING`, and an active
workflow phase are not terminal idle. They must remain visible and recoverable.

The implementation must define and test a debounce/settling rule so that a
short internal idle boundary between tool calls does not kill the worker.

The finalizer MUST be absent or disabled for attached execution. An attached
session returning to `IDLE`, completing a turn, or encountering an
irrecoverable error must return control to the normal attached TUI/error
handling path; it must not self-terminate because of this PRD.

### FR-8 — Self-termination is scoped to detached workers

The finalizer MUST first prove that it is running in a `--detach` worker
context, and then prove that it is running in the detached worker context
before requesting process termination:

* compare the current PID with the persisted worker PID where available;
* verify the run ID and session lease belong to this process;
* never signal the launcher PID, parent shell, TUI process, or an unrelated
  worker; and
* treat missing/stale PID metadata as a reconciliation condition, not as
  permission to kill a guessed process.

On POSIX, normal completion should use a graceful `SystemExit`/return path;
only a bounded cleanup fallback may send `SIGTERM` to `os.getpid()`. On
Windows, use the platform-equivalent self-termination mechanism. `SIGKILL`,
`kill -9`, or process-group termination is not the normal successful path.

### FR-9 — Shutdown deadline and leak prevention

Finalization must have a bounded deadline. If an optional child task or
integration does not stop, the worker must:

1. record the cleanup timeout;
2. preserve the primary workflow outcome;
3. release what can be released safely;
4. mark the run `needs_attention`/`failed` only when the cleanup condition
   affects correctness; and
5. terminate the worker without hanging indefinitely.

The process must not stay alive because of non-daemon heartbeat loops,
unawaited tasks, open terminal readers, MCP startup tasks, or session-service
subscriptions after the run is terminal.

### FR-10 — Durable final process metadata

Persist at least:

```text
worker_pid
worker_started_at
worker_finished_at
worker_exit_code
worker_exit_reason
worker_finalization_attempts
worker_cleanup_error (optional)
```

The record must distinguish:

```text
completed
irrecoverable_error
idle_after_thinking
cancelled
lost/orphaned
launch_failed
cleanup_timeout
```

Existing `BackgroundSession` and `GoalRun` fields may be extended, but the
implementation must preserve backwards-compatible decoding of records written
by PRD-204.

### FR-11 — Recovery and reconciliation

On `runs`, `runs show`, `agents --run`, `attach`, and startup recovery, the
system must reconcile persisted process metadata with authoritative local
process/session state:

* a live PID with a matching session lease remains running;
* a missing PID for a non-terminal session becomes `lost` or
  `needs_attention`, not silently completed;
* a dead PID with a terminal session remains terminal;
* a dead PID with an active session is marked orphaned/recoverable according to
  existing background recovery rules; and
* PID reuse is never treated as proof that the original worker is alive.

Attach must never start a second worker if the original worker is still live.
If the worker self-terminated after a valid terminal outcome, attach opens the
existing session in its terminal/read-only/recovery mode according to current
TUI semantics.

### FR-12 — Cancellation remains distinct

`runs cancel`, process interruption, and user cancellation must persist
`cancelled`/`cancelling` semantics and `exit_reason: cancelled`. They must not
be reported as successful idle completion or irrecoverable provider failure.

Cancellation still preserves journals, checkpoints, logs, worktrees, and
partial worker results. Cleanup policies remain governed by PRD-204/PRD-203;
self-termination must not imply artifact deletion.

### FR-13 — Attached mode is never self-killed by this lifecycle

The attached command:

```bash
agenthicc --goal "..."
```

must continue to return control through the normal TUI lifecycle. A user may
send another message after an agent turn becomes idle. None of the completion,
irrecoverable-error, or idle-after-thinking finalizers from FR-5–FR-8 may be
installed or invoked in the attached TUI process.

### FR-14 — Goal semantics remain exact

Both forms:

```bash
agenthicc --goal GOAL
agenthicc --goal GOAL --detach
```

must be equivalent to selecting `goal_flow` and submitting `GOAL` as the
initial user intent. The goal is submitted exactly once, and a worker resume,
attach, or recovery path must not submit it again.

## 8. State machine

The detached worker lifecycle must be expressible as a durable state machine:

```text
CREATED
  ↓
LAUNCHING ───────────────→ LAUNCH_FAILED
  ↓
RUNNING / THINKING
  ├──→ WAITING_INPUT ───→ RUNNING
  ├──→ WAITING_APPROVAL → RUNNING
  ├──→ RETRYING ────────→ RUNNING
  ├──→ COMPLETED ───────→ FINALIZING → EXITED       (detach only)
  ├──→ IRRECOVERABLE ───→ FINALIZING → EXITED_ERROR (detach only)
  ├──→ IDLE_AFTER_THINKING → FINALIZING → EXITED_IDLE (detach only)
  └──→ CANCELLING ─────→ CANCELLED → FINALIZING → EXITED_CANCELLED
```

`IDLE_AFTER_THINKING` is a terminal run outcome only after the guards in FR-7
pass and only for a detached worker. A visual `IDLE` label alone is
insufficient. Attached execution has no transition from an agent idle state
to this worker-exit state.

## 9. Event and persistence contract

Add or adapt durable events following existing event naming conventions:

```text
DetachedWorkerLaunched
DetachedWorkerPidRecorded
DetachedWorkerThinkingStarted
DetachedWorkerIdleAfterThinking
DetachedWorkerWorkflowCompleted
DetachedWorkerIrrecoverableError
DetachedWorkerFinalizationStarted
DetachedWorkerFinalizationCompleted
DetachedWorkerExited
DetachedWorkerCleanupTimedOut
```

Each event must include bounded metadata:

```json
{
  "run_id": "run_...",
  "session_id": "...",
  "worker_pid": 3845622,
  "exit_reason": "completed",
  "exit_code": 0,
  "timestamp": 1790680000.0
}
```

Do not put provider prompts, API keys, full tool arguments, or unbounded model
responses into process-lifecycle events.

## 10. CLI and exit-code contract

### 10.1 Detached start

`agenthicc --goal GOAL --detach` returns success once the run and worker have
been durably accepted, not once the goal is completed. A successful launch
returns exit code `0` even if the worker later fails; the later failure is
available through run inspection.

### 10.2 Launch failure

Validation/configuration/launch failure before detached acceptance returns the
existing non-zero launch error code and a structured error in JSON mode. It
must not print a misleading PID.

### 10.3 Worker exit codes

The detached worker uses:

```text
0 — completed or confirmed idle-after-thinking outcome
1 — irrecoverable error, cleanup failure affecting correctness, or workflow failure
2 — explicit cancellation where the existing worker contract distinguishes it
```

The exact numeric mapping must follow existing CLI conventions if they differ,
but it must be stable and documented. Run status and `worker_exit_reason` are
authoritative over numeric codes.

## 11. Security and safety requirements

1. Never trust a persisted PID without matching run/session ownership evidence.
2. Never signal a PID belonging to the parent launcher or attached TUI.
3. Use `start_new_session`/equivalent so closing the launcher does not
   accidentally terminate the worker through a shared terminal group.
4. Do not expose secrets in text, JSON, logs, process-lifecycle events, or
   command titles.
5. Preserve the existing capability, approval, workspace, network, and
   dangerous-mode policies in detached execution.
6. A failed finalizer must not delete unintegrated worktrees or journals.
7. Make finalization idempotent under duplicate signals, process recovery, and
   concurrent `runs show`/`attach` calls.

## 12. Observability requirements

The run projection and logs must allow an operator to answer:

* Which PID is the detached worker?
* Is that PID still alive and owned by this run?
* Why did it exit?
* Was the workflow complete, irrecoverably failed, cancelled, or idle?
* Were all final results/checkpoints persisted before exit?
* Did cleanup finish or time out?
* Can the same session be attached or resumed safely?

Use bounded, human-readable log entries such as:

```text
Detached worker 3845622 finalized run run_...: completed; exit=0
Detached worker 3845622 finalized run run_...: irrecoverable_error; exit=1
Detached worker 3845622 finalized run run_...: idle_after_thinking; exit=0
```

Do not emit repetitive polling messages for every heartbeat or animation
frame.

## 13. Implementation approach

### Phase 0 — Archaeology and contract map

Inspect and document the exact ownership boundaries in:

* `src/agenthicc/runs/cli.py`;
* `src/agenthicc/runs/manager.py`;
* `src/agenthicc/runs/model.py` and `store.py`;
* `src/agenthicc/background/supervisor.py`;
* `src/agenthicc/background/worker.py`;
* `src/agenthicc/background/model.py` and `store.py`;
* `src/agenthicc/runners/headless.py`;
* `src/agenthicc/runners/session_lease.py`;
* `src/agenthicc/tui/conversation_store.py`;
* the `goal_flow` runner and workflow checkpoint store; and
* the existing `agents`/run projections.

Record where the canonical thinking/idle transitions are emitted and how the
worker currently exits. Do not infer idle from a rendered TUI frame.

### Phase 1 — PID propagation

Add a typed process identity field to the existing records/projections. Make
supervisor launch return or durably expose the actual child PID, including
short-lived-worker races. Add human-readable and JSON output.

### Phase 2 — Finalization coordinator

Implement one idempotent detached-worker finalizer responsible for completion,
irrecoverable errors, confirmed idle, and cancellation. It should be invoked
from the existing worker entry point rather than from a second loop.

### Phase 3 — Idle/error classification

Connect finalization to the canonical workflow/session state. Add explicit
guards for pending input, approvals, retries, active tools, child workers,
compaction, and continuation markers.

### Phase 4 — Resource closure and self-exit

Close processor/session-service/terminal/MCP/browser/heartbeat resources with a
bounded deadline, persist the exit event, release ownership, and return from
the worker entry point. Add a platform-safe self-termination fallback only if
normal return cannot close the process.

### Phase 5 — Recovery and projection

Reconcile stale/dead processes, expose exit reason/PID in runs and agents, and
ensure attach/resume never duplicates a healthy worker.

### Phase 6 — Documentation and release gates

Update CLI, background-session, goal-run, storage, and architecture guides;
update `llms.txt`/`llms-full.txt` for public types; and add implementation
evidence to this PRD.

## 14. Testing requirements

### Unit tests

Cover:

* PID field serialization/deserialization and legacy records without PID;
* detached response text and JSON output;
* PID launch races and missing-PID failures;
* successful finalization idempotency;
* irrecoverable error classification and bounded retries;
* transient error/retry states remaining alive;
* idle debounce and `Thinking → IDLE` detection;
* waiting input/approval not being mistaken for idle;
* active tool/child-worker/continuation guards;
* cancellation and cleanup timeout outcomes;
* self-termination targeting only the current worker PID;
* PID reuse/stale metadata fail-closed behavior; and
* resource closure and duplicate finalizer calls.

### Integration tests

Using temporary stores and fake providers:

1. start a detached goal and assert the returned PID is the worker PID;
2. verify the PID is persisted in the background session, run projection, and
   `agents --run` projection;
3. complete `goal_flow` and assert the worker exits and the run remains
   inspectable;
4. produce an irrecoverable error and assert non-zero worker exit plus durable
   failure evidence;
5. return through a genuine thinking-to-idle boundary and assert clean idle
   self-exit;
6. leave a pending question/approval and assert the worker remains alive and
   waiting;
7. kill the worker externally and assert recovery marks the run lost/orphaned;
8. race completion against PID persistence;
9. attach while running and assert no duplicate worker is created; and
10. run cancellation during finalization without corrupting the final record.

### End-to-end tests

Run a real subprocess in a temporary Git repository:

```text
agenthicc --goal "Write a 20-feature PRD" --mode YOLO --detach
```

Assert:

* the command returns promptly;
* output includes `Run ID`, `Main`, and a numeric `PID`;
* JSON includes `pid == worker_pid`;
* the worker PID belongs to the detached child, not the launcher;
* the worker eventually disappears after completion;
* `runs show` retains the final result and exit reason;
* no active background session or orphaned event-loop task remains; and
* a failed detached run exits and remains attachable/reviewable without losing
  its journal or checkpoint; and
* an attached `--goal` run does not exit when it reaches idle or completes an
  agent turn.

Tests must be deterministic and must not depend on a real provider, network,
or the developer's process table beyond the subprocess under test.

## 15. Acceptance criteria

### AC-1 — PID is printed

Given a valid detached goal, the start response prints a numeric detached
worker PID and JSON exposes `pid` and `worker_pid`.

### AC-2 — PID is correct

The printed PID matches the process recorded in the background session and the
run's main-agent projection. It is not the short-lived CLI launcher PID.

### AC-3 — Completion self-exits

When detached `goal_flow` completes, the worker persists completion and exits
without requiring an external kill. The equivalent attached command remains
alive under normal TUI lifecycle rules.

### AC-4 — Irrecoverable error self-exits

When the bounded recovery policy identifies an irrecoverable error in detached
mode, the worker persists failure evidence and exits non-zero without looping
indefinitely. Attached mode does not self-terminate through this path.

### AC-5 — Idle-after-thinking self-exits

When the canonical state transitions from meaningful thinking to confirmed idle
with no pending work/input in detached mode, the worker persists
`idle_after_thinking` and exits successfully.

### AC-6 — Waiting states survive

Pending approval, question, retry, active tool, active child worker, or
continuation state prevents idle self-termination and remains recoverable.

### AC-7 — Final state is durable

After the worker disappears, `runs show`, `agenthicc agents --run`, and the
session/workflow recovery path show the same final outcome, exit reason, PID,
and timestamps.

### AC-8 — No duplicate owners

Attach/resume while the worker is live is rejected or safely hands off through
the existing lease protocol; it never starts two workers for one main session.

### AC-9 — No collateral termination

Self-termination cannot signal the launcher, attached TUI, unrelated PID, or
another run, even with stale/corrupted process metadata.

### AC-10 — Attached mode unchanged

An attached `--goal` session remains interactive after an agent returns to
idle, completes a workflow turn, or encounters an error; it never
self-terminates under the detached lifecycle rules.

### AC-11 — Backwards compatibility

Existing PRD-204 records without process-exit fields decode and display safely;
existing `runs`, `agents`, `attach`, and ordinary workflow invocations retain
their current behavior.

## 16. Documentation requirements

Update in the same implementation:

* `README.md` with detached PID and lifecycle examples;
* `docs/guides/goal-runs.md` with launcher-versus-worker PID semantics;
* `docs/guides/background-sessions.md` with worker finalization/recovery;
* `docs/reference/cli.md` with output fields and exit codes;
* `docs/reference/storage.md` with process metadata retention;
* `docs/guides/architecture.md` with the finalization ownership boundary;
* `llms.txt` and `llms-full.txt` for public process/run fields; and
* `prds/README.md` with this PRD and implementation evidence.

Documentation must make this distinction explicit:

```text
The launcher PID ends after accepting the run.
The worker PID executes the goal and self-terminates after finalization.
The run/session/checkpoint remain durable after both processes are gone.
```

## 17. Open decisions to resolve during implementation

1. Whether a confirmed idle-after-thinking outcome should use `completed` or
   a distinct run status. Recommendation: retain `completed` for compatibility
   and store `worker_exit_reason=idle_after_thinking`.
2. Whether the launch response should poll for a PID for a bounded interval or
   have `BackgroundSupervisor.submit()` return an atomic launch result.
   Recommendation: return an atomic typed result while preserving the existing
   `BackgroundSession` API.
3. Whether finalization should use normal return only or include a self-SIGTERM
   fallback. Recommendation: normal return first; fallback only after a
   bounded cleanup failure and only against the current PID.
4. The exact canonical idle event and debounce duration. This must be derived
   from current state/event contracts, not selected from a TUI animation rate.
5. Whether `worker_pid` should be renamed to a generic `process_id` in a future
   storage migration. For this PRD, preserve `worker_pid` as the explicit
   detached-worker field and provide compatibility aliases only at projection
   boundaries.

## 18. Definition of done

The feature is complete when a user can run:

```bash
agenthicc --goal "Write a PRD that suggests 20 extensive features to greatly enhance this library" \
  --mode YOLO \
  --detach
```

and immediately receive a run ID and the correct detached worker PID; then,
without manually killing the worker, observe that the detached worker exits
after `goal_flow` completion, an irrecoverable error, or a confirmed
thinking-to-idle terminal boundary. The durable run remains inspectable with
the final status, exit reason, PID, timestamps, logs, conversation, and
workflow checkpoint. Running the same goal without `--detach` remains
interactive through idle, completion, and ordinary error handling, and no
unrelated process is terminated.

## 19. Implementation evidence

Implemented in the existing background/runtime boundaries:

* `background.supervisor` propagates the child PID, starts detached workers
  in an independent POSIX session, preserves detached cancellation metadata,
  and fails closed during recovery when a live PID does not match the exact
  Agenthicc worker request and store identity.
* `background.worker` owns one idempotent detached finalizer, classifies
  workflow completion, recoverable checkpoints, irrecoverable failures,
  cancellation, confirmed idle, and cleanup timeout, and persists the bounded
  `worker_exited` audit event.
* `BackgroundSession`, `GoalRun`, and `RunAgentRecord` serialize worker
  lifecycle fields with legacy decoding and compatibility PID aliases.
* `runs`, `runs show`, `agents --run`, and the goal-run TUI expose the worker
  PID and exit reason without exposing secrets.

Verification includes unit, integration, and subprocess/E2E coverage for PID
serialization, launch races, terminal finalization, cancellation, idle
guards, recovery, and attached-mode compatibility. The complete suite passes
with `3978 passed, 15 skipped`; repository lint, type-audit, and strict docs
build gates pass for the implementation surface.
