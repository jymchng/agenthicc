---
title: "PRD-194: Runtime phase control for goal_flow"
status: Implemented
version: 1.0.0
date: 2026-09-20
scope: "goal_flow planned-phase scheduling, safe mutation tools, checkpoints, and recovery"
related_prds:
  - PRD-100  # code_plan architecture and tool-gated phase transitions
  - PRD-163  # cache-stable workflow prompts and generated workflows
  - PRD-169  # transaction-safe tool-call conversations
  - PRD-170  # durable workflow recovery
  - PRD-179  # generated workflow annotations and boundary checkpoints
  - PRD-182  # durable mid-turn preservation
  - PRD-184  # preserve the active workflow phase after errors
  - PRD-185  # dynamic goal-list mutation for goal_flow
  - PRD-186  # profile-aware workflow checkpoint topology
tags:
  - goal-flow
  - runtime-phase-control
  - postpone
  - skip
  - bring-forward
  - delete
  - checkpoints
  - resume
---

# PRD-194 — Runtime phase control for `goal_flow`

## 1. Executive summary

Extend `goal_flow` so the agent can safely change the execution plan while a
run is in progress. During implementation or verification, the agent must be
able to:

- postpone a planned phase until the other eligible phases have had a chance
  to run;
- skip a phase that is no longer required or cannot be completed in the
  current run;
- bring a pending or postponed phase forward so it becomes the next eligible
  phase; and
- delete a phase from the active execution plan.

The feature extends PRD-185's dynamic, stable-ID goal list. In this PRD, a
**planned phase** is one `GoalRecord` in `goal_flow`'s ordered work plan. The
existing `CLARIFY`, `DECIDE_GOALS`, `IMPLEMENT_GOAL`, `VERIFY_GOAL`, and
`SUMMARIZE` values are **control phases**: they are the workflow's mandatory
execution rails and are not themselves deletable or reorderable by the agent.
This distinction is intentional. Letting an agent delete the checkpoint,
verification, or summary rails would turn a scheduling feature into an
unbounded topology mutation and could make durable recovery unsafe.

The agent-facing API is four small tool calls:

```python
postpone_phase(phase_id: str, reason: str)
skip_phase(phase_id: str, reason: str)
bring_forward_phase(phase_id: str, reason: str)
delete_phase(phase_id: str, reason: str)
```

Each operation is a tool-controlled workflow mutation, not prose. It is
validated against the current owner, phase, plan revision, and stable phase
identity; checkpointed atomically; and then projected into the same
conversation, event log, and TUI status path as the rest of `goal_flow`.

Successful operations do not create a new workflow run, conversation, memory
store, or provider path. They preserve completed work and the active phase's
identity across a provider error, interruption, restart, `--continue`, and
`--resume`.

When the target is the currently active planned phase, the operation takes
effect at the next safe tool boundary. The runner must not abruptly roll back
filesystem or command side effects, and it must not allow the model to keep
performing meaningful work under a phase that has just been skipped, deleted,
or postponed. The current turn is finalized as an interrupted/control-directed
turn, the mutation is durably checkpointed, and the scheduler selects the next
eligible phase according to the operation's semantics.

This PRD specifies the product contract and implementation approach. The
runtime implementation is now present in
`src/agenthicc/workflows/goal_flow/runner.py`, with its journal/TUI projection
in the canonical conversation-store and scroll-appender paths. The acceptance
tests below remain the verification contract for future changes.

## 2. Current-state evidence and problem statement

### 2.1 Current implementation

The current source tree was inspected on 2026-09-20.

| Surface | Current behavior | Gap addressed by this PRD |
|---|---|---|
| `GoalContext` | Stores stable `GoalRecord` identities, ordered records, `active_goal_id`, derived `goal_index`, and list revision | The plan can be appended to or inserted into, but cannot be reprioritized or retired through the agent control plane |
| `append_goal` / `insert_goal` | Available during `IMPLEMENT_GOAL` and `VERIFY_GOAL`; durable, owner-checked, and checkpointed | No postpone, skip, bring-forward, or delete operation |
| `GoalStatus` | `pending`, `active`, and `verified` | No durable disposition for a deferred, skipped, or logically deleted phase |
| `_verify_goal` | Selects the first pending record after verification and skips verified records | It cannot honor an explicit phase disposition or priority change |
| checkpoint codec | Persists goal records, active ID, revision, and bounded mutation receipts | The codec needs versioned phase-control state and mutation receipts |
| phase prompts | Explain append/insert and tool-only transitions | They do not teach the model when a phase should be deferred, skipped, reprioritized, or removed |
| recovery path | Rehydrates the same typed context and shared session conversation | An in-flight phase-control request needs deterministic safe-boundary recovery |
| TUI projection | Shows the active workflow phase and existing goal-list notices | It needs bounded operation-specific notices without dumping the entire plan |

### 2.2 User need

Real implementation plans change while work is underway:

- a phase depends on an external decision and should wait until other work is
  complete;
- a planned phase is obsolete because another change already satisfied it;
- a high-priority regression phase is discovered and should run next; or
- a phase is duplicated, superseded, or outside the current request and should
  be removed from the active run.

Today the agent can only describe those decisions in prose. Prose cannot
change the scheduler, so the agent either keeps executing stale work, asks the
user to restart and lose durable progress, or silently changes the effective
plan without an auditable state transition. All three outcomes make resume,
verification, and final summaries unreliable.

### 2.3 Why this is not a generic topology editor

`goal_flow` has two layers:

```text
fixed control topology
  CLARIFY → DECIDE_GOALS → [IMPLEMENT_GOAL ↔ VERIFY_GOAL]* → SUMMARIZE

mutable planned work phases
  Phase A → Phase B → Phase C → ...
  (each record is executed through the IMPLEMENT/VERIFY control cycle)
```

The requested operations apply to the second layer. This gives the agent the
requested runtime control while preserving the invariants that make workflow
checkpoints resumable. A future product may define controlled topology
variants, but that is a separate PRD and must not be smuggled into this one.

## 3. Goals

1. Expose dedicated agent tools to postpone, skip, bring forward, and delete a
   planned `goal_flow` phase during an active run.
2. Keep every planned phase addressable by a stable opaque ID; never use a
   mutable numeric index as identity.
3. Define deterministic ordering and eligibility semantics for all four
   operations, including operations targeting the active phase.
4. Preserve the active workflow run, shared conversation, memory, workspace
   policy, checkpoints, approvals, and security boundaries.
5. Make every accepted operation durable before the tool reports success.
6. Preserve partial work and the latest safe typed context if a provider/tool
   error or process interruption occurs around a mutation.
7. Make operations idempotent, owner-safe, compare-and-swap protected, and
   diagnosable after resume.
8. Teach the agent precisely when to use each operation and when to continue
   the current phase.
9. Add bounded TUI/event observability without leaking prompts, secrets, or
   unbounded plan text.
10. Provide unit, integration, and end-to-end coverage for normal, invalid,
    concurrent, interrupted, resumed, and terminal-plan cases.

## 4. Non-goals

- Mutating the fixed control topology (`CLARIFY`, `DECIDE_GOALS`, the
  implementation/verification machinery, or `SUMMARIZE`).
- Allowing arbitrary workflow Python code, shell commands, filesystem paths,
  provider calls, or tool arguments through a phase-control tool.
- Creating a second workflow runner, conversation store, memory store,
  scheduler, checkpoint store, or owner lease.
- Hard-deleting historical evidence, receipts, checkpoints, transcript entries,
  or files created by a phase. “Delete” means logical removal from the active
  plan; a tombstone remains for audit and recovery.
- Automatically undoing filesystem, Git, database, browser, or external API
  side effects when a phase is skipped, postponed, or deleted.
- Allowing the model to bypass approvals, mode restrictions, workspace scope,
  network/browser/MCP policy, tool capabilities, or cancellation behavior.
- Allowing a mutation to claim that a phase was completed or verified.
- Replacing `finalize_goals`, `goal_implemented`, `verify_goal`, or
  `complete_workflow` as the existing transition tools.
- Letting a phase-control operation silently create a new workflow run after a
  failure.
- Providing a user-facing `/phase` command in this PRD. The first release is
  an agent control-plane capability; a later user override surface would need
  its own permissions and confirmation design.
- Natural-language schedule times. “Postpone” means reorder/defer within this
  run, not a wall-clock reminder or background job.
- Automatically inferring intent from model prose. Only successful typed tool
  calls mutate the plan.

## 5. Terminology and state model

### 5.1 Planned phase versus control phase

The user-facing term **phase** refers to one planned work item represented by
an existing `GoalRecord`. A record's `goal_id` is exposed in dynamic context
as `phase_id`; the stored identifier remains the same to preserve PRD-185
compatibility. Existing integrations may continue to call these records
goals, but the new tools and documentation use “planned phase” to make the
runtime scheduling behavior clear.

The following control phases are immutable rails:

| Control phase | Role | Phase-control tools |
|---|---|---|
| `CLARIFY` | Resolve material ambiguity using `ask_user` | unavailable |
| `DECIDE_GOALS` | Establish the initial planned-phase list | unavailable; use `finalize_goals` |
| `IMPLEMENT_GOAL` | Execute the active planned phase | four control tools available |
| `VERIFY_GOAL` | Verify the active planned phase | four control tools available |
| `SUMMARIZE` | Produce the final workflow result | unavailable |

The mutation tools are available only after the initial list is finalized and
while the runner is in `IMPLEMENT_GOAL` or `VERIFY_GOAL`. They can target
pending, postponed, or the active planned phase according to the operation
matrix below. Verified, skipped, and deleted records remain in the checkpoint
as immutable history and cannot be revived by these tools.

### 5.2 Planned-phase lifecycle

The canonical durable disposition is extended to:

```text
pending → active → verified
pending → postponed
active  → postponed      (at the next safe boundary)
pending → skipped
active  → skipped        (at the next safe boundary)
pending → deleted
postponed → deleted
active  → deleted         (at the next safe boundary)
```

`verified`, `skipped`, and `deleted` are terminal dispositions for this PRD.
`postponed` is eligible for `bring_forward_phase`, which returns it to the
pending execution queue. `active` is a scheduling state, not proof that the
phase's work is complete. A postponed or skipped phase retains its summary,
evidence, attempt count, and affected-file metadata accumulated before the
mutation.

The existing `GoalStatus` compatibility projection must continue to serve old
callers, but new code must use the canonical disposition and stable record
fields. A safe compatibility mapping is:

| Canonical disposition | Legacy projection |
|---|---|
| `pending` or `postponed` | `PENDING` |
| `active` | `ACTIVE` |
| `verified` | `VERIFIED` |
| `skipped` or `deleted` | non-eligible terminal record, never represented as pending |

The implementation may choose a more explicit enum name, but it must not
silently map a skipped or deleted record back to pending during checkpoint
restore.

### 5.3 Operation semantics

| Operation | Pending | Postponed | Active | Verified | Skipped | Deleted |
|---|---|---|---|---|---|---|
| postpone | move to deferred queue | idempotent success | request safe-boundary deferral | reject | reject | reject |
| skip | mark skipped | mark skipped | request safe-boundary skip | reject | idempotent success | reject |
| bring forward | move to front of eligible queue | release and move to front | reject (already current) | reject | reject | reject |
| delete | tombstone | tombstone | request safe-boundary deletion | reject | reject | idempotent success |

Repeated identical requests must not create duplicate records, duplicate
side effects, or unbounded duplicate receipts. A successful idempotent result
may report `changed: false` and the existing revision.

### 5.4 Deterministic queue order

The scheduler selects the first `pending` phase in ordered plan position after
the active phase completes. `postpone_phase` moves a phase after every
currently eligible pending phase but before terminal tombstones; it retains a
stable ID and a `postponed` disposition. `bring_forward_phase` releases a
postponed phase, moves it to the front of the eligible queue, and keeps the
current active phase running until its normal safe boundary.

`skip_phase` removes a phase from eligibility while preserving a bounded
reason. `delete_phase` does the same but records a deletion tombstone. Neither
operation changes the relative order or evidence of unrelated records.

When no ordinary `pending` record remains, the scheduler promotes the oldest
eligible `postponed` record in deterministic plan order back to `pending` and
continues. Thus postpone means “run later in this workflow”, not “wait
forever”; `bring_forward_phase` is the explicit escape hatch when the agent
needs that phase before the normal deferred queue.

If an operation targets a phase before the active phase, the active record is
still identified by `active_goal_id`; its numeric index is recalculated rather
than trusted. A verified record is never replayed merely because a phase moved
around it.

## 6. Agent-facing tool contract

### 6.1 Schemas

The provider-facing schemas must be equivalent to the following. All four
tools have the same small shape to minimize model planning errors:

```json
{
  "type": "object",
  "properties": {
    "phase_id": {"type": "string"},
    "reason": {"type": "string"}
  },
  "required": ["phase_id", "reason"],
  "additionalProperties": false
}
```

The Python annotations must use concrete `str` types. The implementation must
not use `object`, untyped dictionaries, or provider-specific schema hacks that
produce the lauren-ai unrecognized-annotation fallback. The runner owns the
operation, status, position, revision, and timestamps; the agent cannot pass
those values.

### 6.2 Tool contracts

```python
postpone_phase(phase_id: str, reason: str)
```

Defers a pending or active planned phase. A pending phase is moved to the
deferred queue immediately. An active phase receives a safe-boundary control
request; its current tool exchange is allowed to commit, then the runner
stops scheduling additional work for that phase and persists it as postponed.

```python
skip_phase(phase_id: str, reason: str)
```

Marks a pending, postponed, or active planned phase as skipped. An active
phase is stopped at the next safe tool boundary. Skipping is not verification,
does not imply that the requested work was satisfied, and must appear as
skipped in the final summary and durable history.

```python
bring_forward_phase(phase_id: str, reason: str)
```

Moves a pending or postponed phase to the front of the eligible queue. It does
not interrupt the current active phase; the selected phase becomes the next
phase after the current safe boundary. Bringing forward a phase that is already
the next eligible phase is an idempotent no-op.

```python
delete_phase(phase_id: str, reason: str)
```

Logically removes a pending, postponed, or active phase from future execution.
An active phase is stopped at a safe tool boundary. The phase record becomes a
deleted tombstone; its ID, reason, prior evidence, and mutation history remain
in the checkpoint. No filesystem or external side effects are rolled back.

### 6.3 Input validation

Every operation must reject, without mutation:

- a missing, non-string, or blank `phase_id`;
- a missing, non-string, or blank `reason`;
- a reason exceeding the configured bounded receipt size;
- an unknown phase ID;
- an ID belonging to a control phase rather than a planned phase;
- a terminal phase when the operation is not idempotently valid;
- a mutation attempted outside `IMPLEMENT_GOAL` or `VERIFY_GOAL`;
- a call made without a typed goal context or durable workflow handle;
- a call from a process that does not hold the workflow run claim; or
- a stale revision / compare-and-swap condition.

Errors return `ok: false`, a stable `error_code`, a bounded explanation, and a
corrective action. They do not set a phase-control event, change the plan
revision, or write a success receipt.

### 6.4 Success response

Each successful response contains only bounded metadata:

```json
{
  "ok": true,
  "operation": "postpone_phase",
  "phase_id": "opaque-stable-id",
  "changed": true,
  "disposition": "postponed",
  "position": 3,
  "next_phase_id": "another-opaque-id-or-null",
  "phase_plan_revision": 7,
  "message": "Phase postponed; continue only until the safe control boundary."
}
```

The response must not include the complete plan, full prompt history, tool
arguments, credentials, or filesystem contents. It must clearly distinguish
“mutation committed” from “phase completed”.

## 7. Active-phase safe-boundary behavior

### 7.1 Why active phases need special handling

A model may call `skip_phase`, `postpone_phase`, or `delete_phase` after it has
already changed files or executed tools. The runtime cannot safely undo those
side effects. It also cannot let the same turn continue making arbitrary
changes while the phase is now marked as no longer eligible. The runner needs a
deterministic boundary between the successful control-tool result and the next
agent turn.

### 7.2 Control request protocol

An active-target operation shall:

1. validate and checkpoint a `phase_control_requested` record containing the
   active phase ID, operation, reason, current run/phase cursor, and revision;
2. return the bounded success response to the model;
3. set an internal control-directed stop signal for the current phase loop;
4. allow the current tool result and journal transaction to commit;
5. prevent new phase work from being dispatched after the control result;
6. convert the request into the final `postponed`, `skipped`, or `deleted`
   disposition at the safe boundary;
7. persist a `phase_plan_mutated` checkpoint with the new cursor and receipt;
8. release or retain the workflow claim according to the existing normal
   phase/resume lifecycle; and
9. select the next eligible pending phase, or enter `SUMMARIZE` if none
   remain.

The tool must not directly cancel the entire session, kill a subprocess, or
roll back a provider request. If the process dies between steps 2 and 7, the
durable control-request record is recovered deterministically: it is either
completed from the persisted request or reported as an unresolved recovery
diagnostic, never silently dropped or applied twice.

### 7.3 Postpone and resume semantics

When an active phase is postponed, its already recorded attempts, summaries,
files, and evidence remain attached to that phase. A later execution resumes
the same phase record with a dynamic note that it was postponed and why; it
does not receive a new ID or become a new goal. The phase is not considered
verified merely because it was postponed.

### 7.4 Skip and delete semantics

Skipping or deleting an active phase explicitly accepts that the phase will
not reach `verify_goal(satisfied=true, ...)` in this run. The final summary
must list the disposition and reason. The workflow may complete only when no
phase remains `pending`, `postponed`, or `active`; `complete_workflow` must
reject a premature completion just as it currently rejects pending goals.

If every planned phase is skipped or deleted, the workflow may enter
`SUMMARIZE` only after the agent supplies a summary that states no planned work
was verified and why. The implementation must not auto-report success or
pretend that deletion is verification.

## 8. Persistence, revisions, and recovery

### 8.1 Canonical state

Extend the PRD-185 record/checkpoint representation with bounded fields
equivalent to:

```text
PhaseControlState
  disposition: pending | active | postponed | verified | skipped | deleted
  last_control_operation: postpone | skip | bring_forward | delete | null
  last_control_reason: bounded string
  control_request: null | {
      operation: postpone | skip | delete
      phase_id: string
      reason: bounded string
      requested_revision: positive integer
      requested_phase: IMPLEMENT_GOAL | VERIFY_GOAL
  }
  postponed_count: non-negative integer
```

The exact class names may follow current conventions, but the durable payload
must preserve the phase ID, plan order, active ID, disposition, reason,
revision, and pending active-control request. Keep `GoalRecord` compatibility
fields and migrate old PRD-185 checkpoints additively.

Add a monotonic `phase_plan_revision` (or extend the existing
`goal_list_revision` with a documented version) and a bounded
`PhaseMutationReceipt`:

```text
PhaseMutationReceipt
  revision: positive integer
  operation: postpone | skip | bring_forward | delete
  phase_id: opaque stable string
  previous_disposition: string
  new_disposition: string
  previous_position: non-negative integer
  new_position: non-negative integer or null
  control_phase: IMPLEMENT_GOAL | VERIFY_GOAL
  active_phase_id: string or null
  reason_digest: bounded redacted digest or bounded reason
```

Receipts are for audit and recovery, not a copy of the conversation. Retain a
bounded window under the same resource policy as PRD-185's mutation receipts.

### 8.2 Atomic mutation transaction

Every operation follows this transaction:

```text
verify live workflow owner and allowed control phase
  → load current typed context and revision
  → validate phase_id, disposition, reason, and operation
  → build candidate plan/order/control request without publishing it
  → increment revision and append one bounded receipt
  → atomically checkpoint candidate with current workflow cursor
  → publish candidate context in memory only after save succeeds
  → append one redacted phase-plan event
  → return success to the model
```

If validation, ownership, serialization, revision checking, or checkpoint
storage fails, the live context and previous checkpoint remain unchanged. The
tool returns a structured recoverable error and does not signal the phase
control event. A success response is a promise that the mutation is durable,
not merely in-memory.

Use a distinct checkpoint reason such as `phase_plan_mutated` for ordinary
mutations and `phase_control_requested` for an active-phase boundary request.
Neither reason advances the workflow topology index or creates a new run.

### 8.3 Error, interruption, and restart behavior

If a provider or tool error happens before a mutation checkpoint succeeds, the
mutation is absent and the error diagnostic remains attached to the interrupted
turn. If it happens after the mutation checkpoint succeeds, the mutation is
present exactly once and the same run resumes from the saved typed cursor.

If the process dies while an active control request is pending, resume must:

1. validate session ID, conversation ID, workflow name, plugin/profile
   fingerprint, workspace identity, and checkpoint topology;
2. inspect the durable control request and last mutation receipt;
3. complete the requested disposition once if the request revision has not
   already been committed;
4. clear the request only after the resulting checkpoint is durable; and
5. invoke the existing `runner.resume(context)` path at the resulting safe
   boundary.

Resume must never reconstruct the initial goal list, inject `DECIDE_GOALS`,
reset to `CLARIFY`, or create a second run merely because a control operation
was interrupted. Ambiguous or corrupt requests remain on disk with a bounded
recovery diagnostic and require the existing explicit recovery/reset path.

### 8.4 Ownership and concurrency

Only the live workflow owner may mutate the plan. Serialize mutations under
the existing runner lock and workflow checkpoint claim. The checkpoint
revision is a compare-and-swap guard: a stale tool call cannot reorder or
delete a newer plan. A second process receives a typed owner/revision error;
it cannot steal the run or apply a mutation against a transcript it does not
own.

## 9. Agent prompts and cache contract

### 9.1 Stable policy

Extend `goal_flow`'s stable `CACHE_CONTRACT` only with deterministic policy:

```text
During IMPLEMENT_GOAL and VERIFY_GOAL, if the ordered planned work changes,
use the phase-control tools with the phase's stable ID and a concise reason.
Postpone defers work, skip records that it will not be verified, bring-forward
makes pending work next, and delete removes work from future execution. These
are durable control operations; do not claim their effect in prose. Continue
only until the current safe tool boundary after controlling the active phase.
```

Do not add the current phase list, IDs, reasons, dispositions, revisions,
attempts, or receipts to the stable prompt. Those are dynamic context and must
remain in the same cache-safe dynamic region used by PRD-185.

### 9.2 Dynamic phase instructions

The implementation and verification prompts must include the current ordered
phase table with bounded fields:

```text
phase_id | position | disposition | short phase text
```

The agent must be instructed to:

- inspect this table before mutating the plan;
- use the exact stable `phase_id`, never a guessed numeric index;
- postpone work that should remain in the run but wait until later;
- skip work that is intentionally not being verified in this run;
- bring forward work that must execute before other pending work;
- delete only work that is obsolete, duplicated, or explicitly outside scope;
- supply a concise reason that will help a later resumed agent understand the
  decision;
- continue the active goal after controlling a non-active target; and
- stop performing meaningful work after controlling the active target because
  the runner will finish the safe-boundary handoff.

Prompts must explicitly state that:

- a control operation is not implementation or verification;
- postponing, skipping, and deleting do not undo side effects;
- a skipped/deleted phase must be disclosed in the final summary;
- a postponed phase remains incomplete and may run later; and
- only successful typed tool calls mutate the plan.

### 9.3 Tool-only transitions

The four tools are phase-control operations. They may request a safe-boundary
handoff for the active phase, but they cannot finish a goal's implementation,
declare verification, or complete the workflow. `goal_implemented`,
`verify_goal`, and `complete_workflow` retain their existing transition-only
responsibilities.

## 10. Data flow

### 10.1 Non-active target

```text
agent sees bounded dynamic phase table
  → calls bring_forward_phase(phase_id, reason)
  → tool wrapper validates concrete schema
  → runner verifies live owner + current revision
  → candidate plan reorders one stable PhaseRecord
  → atomic checkpoint: phase_plan_mutated
  → append redacted phase_plan_mutated event
  → publish candidate context and return committed revision
  → agent continues current IMPLEMENT/VERIFY phase
  → normal phase transition selects first eligible pending phase
```

### 10.2 Active target

```text
agent calls skip_phase(active_id, reason)
  → candidate control_request checkpoint is saved
  → tool result is journaled and returned
  → runner stops the phase loop at the tool boundary
  → control request becomes skipped and active ID is reconciled
  → final phase_plan_mutated checkpoint is saved
  → next eligible phase is selected, or SUMMARIZE is entered
  → ordinary shared conversation and workflow resume path continues
```

### 10.3 Failure window

```text
before candidate checkpoint
  → no plan change; preserve typed context and structured error

after candidate checkpoint, before event projection
  → resume sees committed revision and emits/reconciles one event

after event projection, before next turn
  → resume loads same plan, receipt, and active cursor

never
  → start a new workflow, recreate the initial list, reset to CLARIFY, or
    replay a verified phase solely because a mutation was interrupted
```

## 11. Functional requirements

### FR-01 — Postpone phase

The agent can postpone a pending or active planned phase with a stable ID and
reason. The phase remains in durable history, is not verified, and becomes
eligible only after it is explicitly brought forward or the scheduler reaches
the deferred queue according to the deterministic ordering rules.

### FR-02 — Skip phase

The agent can mark a pending, postponed, or active planned phase as skipped.
The phase is excluded from future execution, remains auditable, and is
represented as skipped in status and final summary. Skipped is not success.

### FR-03 — Bring phase forward

The agent can bring a pending or postponed planned phase to the front of the
eligible queue. It cannot interrupt a currently active phase. The operation is
idempotent when the phase is already next.

### FR-04 — Delete phase

The agent can logically delete a pending, postponed, or active planned phase.
The record becomes a durable tombstone and is excluded from future execution.
Historical evidence is retained, and no external side effect is undone.

### FR-05 — Stable identity

All operations address a stable `phase_id`/`GoalRecord.goal_id`. Numeric
positions are presentation-only and may change after any mutation.

### FR-06 — Safe active-phase boundary

Active-target postpone, skip, and delete requests stop further meaningful work
for that phase at a safe tool/journal boundary and preserve all prior side
effects and partial evidence.

### FR-07 — Deterministic scheduling

The next phase is selected from the first eligible pending record after each
normal verification or control-directed handoff. Verified, skipped, and
deleted records are never replayed.

### FR-08 — Atomic durability

The tool reports success only after the candidate plan, revision, cursor, and
receipt are durably checkpointed. Failed writes leave the prior plan intact.

### FR-09 — Resume correctness

Explicit resume after an exception, interruption, process restart, or provider
retry restores the same run, conversation, memory, plan, dispositions,
control request, and active stable ID.

### FR-10 — Idempotency and CAS

Repeated identical requests and stale table/tool calls are safe. A stale call
cannot apply a mutation to a newer revision or delete a replacement record.

### FR-11 — Prompt and schema guidance

Prompts expose bounded dynamic phase IDs and teach operation semantics. Tool
schemas are concrete, minimal, provider-neutral, and free of unrecognized
annotation fallbacks.

### FR-12 — Existing policy inheritance

Every phase-control call inherits the current workflow owner, workspace scope,
mode, capability, approval, network, browser, MCP, cancellation, journal,
conversation, and checkpoint policy.

### FR-13 — Observability

Emit bounded, redacted structured events for requested, committed, rejected,
recovered, and idempotent operations. Project concise notices into the TUI
without flooding the scroll appender.

### FR-14 — Resource bounds

Bound phase-plan count, phase text, reason text, receipts, tombstones retained
in active checkpoint context, and pending control requests using explicit
configuration/defaults. Reject over-limit calls; never silently truncate.

### FR-15 — Summary integrity

`complete_workflow` cannot complete while a planned phase is pending,
postponed, or active. It must include skipped/deleted dispositions in the
bounded final summary context and must not equate them with verification.

## 12. Acceptance criteria

### Tool availability and validation

- **AC-01:** After `finalize_goals` and during `IMPLEMENT_GOAL` or
  `VERIFY_GOAL`, the provider sees exactly the four phase-control tools with
  concrete `phase_id: str` and `reason: str` schemas.
- **AC-02:** The tools are absent or rejected in `CLARIFY`, `DECIDE_GOALS`,
  `SUMMARIZE`, and terminal states; control-phase IDs cannot be targeted.
- **AC-03:** Blank/non-string/unknown IDs, blank/oversized reasons, and stale
  revisions are rejected without changing memory, plan order, revision,
  checkpoint, or transition events.
- **AC-04:** Every successful operation returns the stable phase ID, changed
  flag, disposition, committed position or null, next eligible ID, and plan
  revision without dumping the full plan.

### Semantics

- **AC-05:** Postponing a pending phase moves it to the deferred queue, keeps
  its ID/evidence, and does not execute it until it becomes eligible again.
- **AC-06:** Postponing an active phase stops further phase work at the safe
  tool boundary, preserves partial work, and later resumes the same phase
  record rather than creating a replacement.
- **AC-07:** Skipping a pending/postponed phase prevents execution and marks
  it skipped with a reason; it is not reported as verified.
- **AC-08:** Skipping an active phase completes the safe-boundary handoff and
  the next eligible phase is selected without replaying or rolling back prior
  side effects.
- **AC-09:** Bringing a pending or postponed phase forward places it first in
  the eligible queue without interrupting the current active phase.
- **AC-10:** Bringing forward an already-next phase is idempotent and does not
  create an unbounded receipt/event stream.
- **AC-11:** Deleting a pending/postponed phase creates a tombstone excluded
  from execution; its ID, reason, and prior evidence remain recoverable.
- **AC-12:** Deleting an active phase stops it at a safe boundary, preserves
  partial work, and never attempts an implicit filesystem/Git/API rollback.
- **AC-13:** Verified, skipped, and deleted phases cannot be accidentally
  revived or replayed by index shifts, resume, or a later mutation.

### Durability and recovery

- **AC-14:** Every accepted operation is checkpointed before success is
  returned, with the same run/session/conversation/workspace/topology identity.
- **AC-15:** A checkpoint failure leaves the pre-operation typed context and
  durable plan unchanged and returns a structured recoverable error.
- **AC-16:** A provider error after a committed mutation resumes the same run
  with the mutation exactly once and never restarts at CLARIFY/DECIDE_GOALS.
- **AC-17:** A process interruption during an active control request resolves
  the request deterministically on explicit resume or reports a bounded
  recovery diagnostic; it never drops or duplicates the disposition.
- **AC-18:** Concurrent owner/revision races cannot reorder, skip, or delete a
  newer plan; only the live workflow owner can commit mutations.
- **AC-19:** Legacy PRD-185 checkpoints migrate without losing stable IDs,
  verified evidence, active cursor, or list order; ambiguous state fails closed
  with recovery evidence rather than resetting the workflow.

### Prompt, UI, and security

- **AC-20:** Dynamic prompts show bounded phase IDs, positions, dispositions,
  and operation guidance; stable prompt text remains deterministic and free of
  rolling plan state.
- **AC-21:** The TUI displays one bounded notice per committed/rejected
  operation and does not print full phase text, reasons, prompts, or secrets.
- **AC-22:** Structured events contain run/phase IDs, operation, disposition,
  revision, and reason digest/bounded reason only; no credentials, headers,
  raw provider errors, or unrestricted tool output.
- **AC-23:** Phase-control operations inherit the current mode, capability,
  approval, workspace, network/browser/MCP, ownership, journal, and
  cancellation policy; they cannot widen authority.
- **AC-24:** Final summaries and status distinguish verified, skipped,
  postponed, and deleted work, and cannot report completion while an eligible
  phase remains unresolved.

### Testing and quality

- **AC-25:** Unit tests cover every operation/disposition matrix entry,
  validation, idempotency, revision conflict, ordering, bounds, and schema.
- **AC-26:** Integration tests cover candidate checkpoint rollback, event
  projection, owner claims, shared conversation/memory, active safe-boundary
  handoff, and exact-once resume.
- **AC-27:** E2E tests run a real deterministic `goal_flow` with scripted tool
  calls that postpone, skip, bring forward, and delete phases across multiple
  implement/verify cycles.
- **AC-28:** E2E recovery tests inject a provider error before and after a
  mutation checkpoint and verify the same run resumes at the saved phase.
- **AC-29:** Relevant Ruff, mypy, type-audit, documentation, unit,
  integration, and E2E gates pass, with unrelated pre-existing failures
  explicitly reported rather than hidden.

## 13. Events and TUI projection

Emit the following bounded lifecycle categories through the existing
conversation/event projection. The exact event names may follow current naming
conventions, but operation and committed state must remain machine-readable:

```text
phase_control_requested
phase_plan_mutated
phase_control_rejected
phase_control_idempotent
phase_control_recovered
```

Each event includes workflow name, run ID, phase ID, operation, old/new
disposition, old/new position where applicable, plan revision, active phase ID,
and a bounded reason or digest. It excludes full phase text by default,
provider messages, tool arguments/results, authorization headers, and secrets.

The TUI may render notices such as:

```text
Phase postponed · id=4f7c… · revision=8 · next eligible=phase-2
Phase skipped · id=21ab… · revision=9
Phase brought forward · id=8c0d… · runs next after the active phase
Phase deleted from plan · id=ae31… · retained as audit tombstone
```

The notice is presentation-only. The typed checkpoint remains the source of
truth, and redraw/replay must not append duplicate lifecycle events.

## 14. Cache and context contract

The phase-control tools must preserve the existing prompt-cache contract:

- stable system policy and tool schemas are deterministic;
- current plan rows, dispositions, IDs, reasons, attempts, evidence, and
  receipts are dynamic context;
- a mutation response is a normal tool exchange in the same conversation;
- the runner must not prepend a new summary or rewrite old messages to make a
  mutation visible;
- control metadata must not become a growing system-prompt suffix; and
- compaction, journal repair, and workflow checkpointing continue to use their
  existing boundaries.

The same `SessionConversation` and `ConversationStore` continue across every
phase-control operation. The plan checkpoint stores compact typed metadata,
not a second transcript or copy of provider memory.

## 15. Security, safety, and reliability

- Require the current workflow owner claim and checkpoint compare-and-swap
  revision for every mutation.
- Treat the `reason` as untrusted bounded user/model data; never interpret it
  as code, a path, a command, or a provider instruction.
- Reapply all existing mode, capability, approval, workspace, network,
  browser, MCP, and tool checks at the ordinary phase boundary.
- Preserve side effects and partial journal evidence; never pretend a skipped
  or deleted phase was rolled back.
- Stop active-phase work only at a journal/tool-safe boundary, not by killing
  arbitrary subprocesses or corrupting a provider exchange.
- Keep tombstones and receipts bounded so repeated control operations cannot
  exhaust checkpoint or prompt budgets.
- Make stop/cancel/error precedence explicit: a user interruption or provider
  failure is preserved, while a committed phase-control request is resolved
  once on recovery.
- Fail closed on corrupt/ambiguous phase-control payloads and leave diagnostic
  evidence beside the checkpoint.
- Do not expose phase text, reasons, tool output, or workflow contents in
  telemetry beyond the existing local session boundary.

## 16. Configuration and migration

The first release should use safe finite defaults aligned with PRD-185's
existing limits. If new settings are needed, place them under
`[workflows.goal_flow]` and validate them with the existing parameter loader.
The minimum configurable bounds are:

```toml
[workflows.goal_flow]
max_goals = 1000
max_goal_text_chars = 4096
max_phase_control_reason_chars = 2048
max_phase_mutation_receipts = 128
```

The exact key names may follow current configuration conventions, but invalid
values must fail with field-specific diagnostics. No setting may create an
unbounded plan, unbounded reason, or unbounded receipt history.

The checkpoint codec must introduce a versioned additive representation. A
PRD-185 checkpoint with only `GoalStatus` and append/insert receipts remains
readable. Existing `pending`, `active`, and `verified` records migrate to the
corresponding canonical dispositions. Existing verified records become
immutable history. A malformed new field must not be silently discarded; the
run remains recoverable only through the established diagnostic/reset path.

No transcript, journal, or workflow run may be rewritten solely to migrate the
plan schema. Migration occurs when the typed checkpoint is decoded and the
next durable checkpoint is written.

## 17. Implementation plan

1. Audit PRD-185's `GoalRecord`, checkpoint codec, phase loops, workflow handle,
   event projection, and resume finalizer; document the exact compatibility
   boundary.
2. Add a canonical disposition/control-request model and a versioned additive
   checkpoint payload with bounded receipts.
3. Implement a shared owner/revision-checked phase-plan mutation transaction
   used by all four tools.
4. Add `postpone_phase`, `skip_phase`, `bring_forward_phase`, and
   `delete_phase` with identical minimal schemas and capability metadata.
5. Add active-phase safe-boundary signaling and exact-once recovery for an
   in-flight control request.
6. Update queue selection, `complete_workflow` guards, dynamic phase tables,
   final summaries, and compatibility projections.
7. Extend the stable/dynamic prompt contract and phase-specific tool guidance.
8. Add structured events, bounded TUI notices, and diagnostics without a
   second event or persistence path.
9. Add the unit, integration, and E2E tests required by Section 18 before
   enabling the feature for all `goal_flow` runs.
10. Update workflow, storage, configuration, TUI, FAQ, changelog, and PRD
    index documentation; record implementation evidence and any unrelated
    gate failures.

## 18. Testing strategy

### 18.1 Unit tests

- Exact tool schemas, annotations, required fields, and capability metadata.
- Reason/ID validation, unknown IDs, control-phase IDs, phase availability,
  configured bounds, and error-code stability.
- Each operation against pending, postponed, active, verified, skipped, and
  deleted records.
- Stable-ID ordering when a mutation occurs before, after, or at the active
  position.
- Postponed queue and bring-forward queue selection.
- Idempotent repeats and no duplicate receipts/events.
- Active control request creation, safe-boundary finalization, and invalid
  request rejection.
- Candidate-context rollback when checkpoint serialization or storage fails.
- Revision/CAS conflicts and owner-claim failures.
- Summary guard behavior for unresolved, skipped, deleted, and all-terminal
  plans.
- Legacy checkpoint migration and corrupt/ambiguous control-request handling.
- Redaction and bounded event/TUI projection.

### 18.2 Integration tests

- Tool call → runner transaction → checkpoint → event projection.
- Shared `SessionConversation`, `ConversationStore`, journal, memory, and
  workflow handle remain the same before and after each mutation.
- Active-phase control request interrupts only at the safe tool boundary and
  selects the correct next phase.
- Provider error before/after candidate checkpoint preserves the exact plan
  revision and current run.
- Two owners and stale revisions cannot commit conflicting operations.
- Compaction, interruption cleanup, and workflow claim release preserve a
  committed control mutation.
- Mode, capability, approval, workspace, network, browser, MCP, and
  cancellation policies remain unchanged.
- TUI event projection emits one concise notice and no repeated scroll flood.

### 18.3 End-to-end tests

Use a deterministic fake provider/clock and a temporary session directory:

1. create four planned phases and postpone a pending phase; verify it runs
   after the current eligible queue;
2. skip a pending phase and verify no implementation or verification turn is
   generated for it;
3. bring a postponed phase forward and verify it is next after the active
   phase;
4. delete a pending phase and verify a tombstone remains while execution
   excludes it;
5. call postpone/skip/delete on the active phase and verify safe-boundary
   handoff, preserved partial evidence, and correct next cursor;
6. inject errors before and after each mutation checkpoint and resume the same
   run without a CLARIFY/DECIDE_GOALS reset;
7. repeat and race operations to verify idempotency and owner/CAS behavior;
8. complete a plan containing verified, skipped, and deleted records and verify
   the final summary distinguishes all dispositions; and
9. inspect TUI output and durable records for bounded, redacted diagnostics.

Tests must not sleep for real time or depend on a live provider.

## 19. Rollout and observability

Initially enable the tools behind a validated `goal_flow` capability flag or
development configuration while the active-boundary and recovery tests
stabilize. The default rollout may enable them once no-overlap, exact-once
checkpoint, and security inheritance tests pass.

Measure only bounded categories and IDs: operation requested, committed,
rejected, idempotent, recovered, owner conflict, revision conflict, and
checkpoint failure. Do not record full phase text, reasons, prompts, provider
errors, or tool output in remote telemetry.

If a severe defect is discovered, disabling the feature must reject new
mutations while preserving the existing PRD-185 append/insert state and
durable checkpoints. It must not silently skip, delete, or rewrite phases.

## 20. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Active phase continues mutating after skip/delete | Control-directed stop signal and safe journal/tool boundary |
| A mutation is acknowledged but lost on restart | Save candidate checkpoint before success response |
| Stable index points at a different phase after reorder | Stable phase IDs and derived positions |
| Provider error restarts the entire workflow | Preserve typed context and use existing `runner.resume` path |
| Delete is interpreted as rollback | Logical tombstone semantics, explicit prompt and summary wording |
| Postponed work is never revisited | Explicit deferred queue state, status, and bring-forward operation |
| Operations race between owners | Existing workflow claim plus checkpoint revision CAS |
| Cache hit rate degrades | Keep plan state dynamic and stable policy/schema unchanged |
| Model uses controls for ordinary planning noise | Prompt criteria, bounded reasons, and operation-specific validation |
| Plan/checkpoint grows without bound | Finite plan, reason, receipt, and tombstone bounds |
| Skipped/deleted work is reported as complete | Separate terminal dispositions and summary/completion guard |

## 21. Open decisions resolved for v1

- **Unit of mutation:** planned `GoalRecord` work phases, exposed as
  `phase_id`; fixed control rails remain immutable.
- **Postpone destination:** the end of the currently eligible queue, retaining
  a postponed disposition.
- **Bring-forward destination:** the front of the eligible queue, without
  interrupting the active phase.
- **Active-target behavior:** commit a safe-boundary control request, preserve
  side effects, and hand off deterministically; never roll back arbitrary work.
- **Delete meaning:** logical deletion with a durable tombstone, not physical
  removal or side-effect rollback.
- **Completion:** skipped/deleted phases are terminal dispositions but are not
  verification; unresolved pending/postponed/active phases block completion.
- **Identity:** `phase_id` is the existing stable `GoalRecord.goal_id`; list
  positions are derived presentation values only.
- **Persistence:** extend the PRD-185 workflow checkpoint and event path; do
  not create a second plan store.

## 22. Definition of done

- All four tools are registered only in the correct `goal_flow` phases and
  expose the exact bounded schemas.
- Postpone, skip, bring-forward, and delete semantics are implemented for
  pending, postponed, and active planned phases with idempotent terminal
  behavior.
- Active-target operations stop at a safe boundary and preserve side effects,
  journal state, and typed context.
- Checkpoint persistence, owner/CAS protection, migration, interruption
  recovery, and resume behavior satisfy every acceptance criterion.
- The stable cache contract remains deterministic and the dynamic phase table
  provides the IDs and current dispositions required by the agent.
- Unit, integration, E2E, lint, type, type-audit, and documentation evidence
  is recorded, including unrelated environmental blockers.
- User, contributor, storage, workflow, configuration, TUI, and PRD index
  documentation explains the new phase-control contract.
- The PRD status is changed to `Implemented` only after the implementation and
  verification evidence is recorded.
