---
title: "PRD-185: Dynamic goal-list mutation for goal_flow"
status: Implemented
version: 1.1.0
date: 2026-08-31
scope: "goal_flow goal-list tools, stable goal identity, scheduling, persistence, and resume"
related_prds:
  - PRD-100 # code_plan architecture
  - PRD-156 # resumable plan-mode interrupts
  - PRD-163 # cache-stable workflow prompts and generated workflows
  - PRD-169 # transaction-safe tool-call conversations
  - PRD-170 # durable workflow recovery
  - PRD-179 # generated workflow phase annotations and checkpoints
  - PRD-182 # durable mid-turn preservation
  - PRD-184 # active workflow phase after exceptions
tags:
  - goal-flow
  - dynamic-goals
  - workflow-tools
  - checkpoints
  - resume
  - stable-identity
---

# PRD-185 — Dynamic goal-list mutation for `goal_flow`

## 1. Executive summary

`goal_flow` currently asks the agent to produce one ordered list of goals in
the `DECIDE_GOALS` phase. The list is then treated as fixed while the runner
implements and verifies each item. In real work, implementation and
verification frequently reveal a missing prerequisite, a newly discovered
acceptance criterion, or a follow-up task that must be completed before the
workflow can honestly claim success.

This PRD adds two agent tools:

```python
append_goal(goal: str)
insert_goal(index: int, goal: str)
```

`append_goal` adds one pending goal at the end of the current list.
`insert_goal` adds one pending goal at a zero-based position, including the
beginning and the position immediately after the current last item. Neither
tool changes phase by itself. The current goal remains active until its
normal implementation and verification handoff completes.

The implementation must use stable goal identities rather than treating a
mutable list index as identity. Every mutation must be validated, applied
atomically, durably checkpointed, and reflected in the resumed typed context.
Insertion before the active goal must not restart or discard that active goal;
after the active goal is verified, pending goals are selected in list order
while verified goals are skipped. A new goal inserted before an already
verified item is still pending and will be executed exactly once.

The feature must preserve the existing `goal_flow` contracts:

- phase transitions remain tool-only;
- the session-wide conversation and memory remain shared;
- stable system-prompt content remains cache-eligible;
- workflow checkpoints are the source of durable cursor truth;
- `--continue`, `--resume`, and TUI recovery use the existing workflow
  recovery path; and
- an ordinary error after a goal mutation resumes the same run with the
  mutated list, not a new run or the original list.

This is a product and implementation specification. It does not itself alter
the runtime until the implementation and verification work described below is
completed.

## 2. Problem statement

### 2.1 User need

The agent may discover during `IMPLEMENT_GOAL` or `VERIFY_GOAL` that the
original decomposition is incomplete. Today it has no control-plane operation
for expressing that discovery. It can mention the missing work in prose, but
prose cannot change the state machine. It can ask the user to restart and
provide a new list, but that loses the current run's durable progress and may
replay side effects. It can silently perform extra work without adding a goal,
which makes verification and the final summary incomplete.

The desired behavior is:

1. the agent discovers a necessary goal;
2. it calls `append_goal(goal)` or `insert_goal(index, goal)`;
3. the runner records the new pending goal without leaving the current phase;
4. the current goal continues normally;
5. after the current goal is verified, the scheduler selects the next pending
   goal according to the updated list order; and
6. a restart, interruption, rate limit, or other ordinary exception restores
   the same ordered list, statuses, identities, cursor, and mutation history.

### 2.2 Current implementation evidence

The current tree was inspected on 2026-08-31.

| Surface | Current behavior | Gap |
|---|---|---|
| `GoalContext` in `src/agenthicc/workflows/goal_flow/runner.py` | Stores `goals: list[str]`, `goal_index`, and separate `goal_attempts`, `goal_evidence`, and `goal_files` lists | A list insertion can shift an index while its per-goal records remain associated with the wrong goal |
| `_make_decide_goals_tools` | Exposes only `finalize_goals(goals)` and replaces the initial list | No mutation operation exists after decision; the model must submit the whole list at once |
| `_make_implement_tools` | Exposes `goal_implemented(summary, files)` | It can complete the current goal but cannot record newly discovered work |
| `_make_verify_tools` | Exposes `verify_goal(satisfied, evidence)` | A failed verification can retry the current goal, but cannot add a pending prerequisite or follow-up |
| `GoalFlowRunner._verify_goal` | Advances using `goal_index + 1` and checks `len(ctx.goals)` | Dynamic insertion cannot be scheduled safely without identity-based cursor logic |
| checkpoint codec | Serializes the goal list and parallel arrays | A mutation must preserve order, status, evidence, and cursor atomically across restart |
| phase prompts and metadata | Describe the fixed clarify → decide → implement/verify → summarize flow | They do not teach the agent when or how to add a goal |
| existing tests | Cover initial goal decisions, per-goal checkpoints, rejection loops, and resume | No append/insert, mutation durability, index-shift, or dynamic-resume coverage exists |

The existing `goal_flow` workflow is registered and checkpoint-aware. This
PRD extends that implementation; it does not create a second workflow or
another persistence system.

### 2.3 Why an index-only patch is insufficient

Simply inserting into `ctx.goals` and incrementing `goal_index` is unsafe:

- evidence and files can become attached to a different textual goal;
- a completed goal can be replayed or skipped after an insertion;
- a checkpoint can restore an integer cursor that no longer identifies the
  same goal;
- an exception between mutation and checkpoint can lose the new goal; and
- two clients or repeated tool calls can apply the same logical mutation
  differently.

Stable identity, atomic mutation, and durable cursor reconciliation are
therefore part of the feature rather than optional implementation details.

## 3. Goals

1. Let the agent append one new pending goal through a dedicated tool.
2. Let the agent insert one new pending goal at any valid list position through
   a dedicated tool.
3. Keep the current active goal and phase unchanged when a list mutation is
   accepted.
4. Preserve the order, identity, status, attempts, summaries, evidence, and
   files of existing goals.
5. Execute each newly added goal exactly once unless the user explicitly resets
   or discards the workflow.
6. Make mutations durable before reporting tool success.
7. Resume the same workflow run with the mutated list after interruption,
   provider failure, process restart, `--continue`, or `--resume`.
8. Keep mutation details in dynamic workflow context and keep the stable
   cacheable prompt contract deterministic.
9. Give the agent precise prompts and tool descriptions so it knows when to
   add work and when to continue the current goal.
10. Provide unit, integration, and E2E evidence for normal, rejected, repeated,
    interrupted, and resumed mutation paths.

## 4. Non-goals

- Allowing the agent to delete, reorder, edit, reprioritize, or mark an
  existing goal complete through this feature.
- Allowing a goal mutation to advance, rewind, or otherwise bypass a phase
  transition tool.
- Replacing `finalize_goals` as the initial planning handoff.
- Making a prose statement such as “I found another task” mutate workflow
  state.
- Replaying completed implementation or verification work merely because a
  new goal was inserted before it.
- Creating a second conversation store, memory store, checkpoint store, or
  workflow runner.
- Guaranteeing a provider prompt-cache hit. The requirement is to preserve
  cache eligibility and stable prompt prefixes.
- Silently truncating a goal, silently deduplicating goals, or silently
  clamping an invalid insertion index.
- Allowing an invalid or uncheckpointable mutation to appear successful.

## 5. Users and primary journeys

### 5.1 Add a follow-up at the end

The agent implements goal 1 and discovers a necessary documentation task. It
calls `append_goal("Document the new configuration behavior")`. The tool
returns a new goal ID and index, the current implementation continues, and
the documentation goal runs after all earlier pending goals according to the
updated order.

### 5.2 Add a prerequisite before a pending goal

The agent is verifying goal 2 and discovers that a migration test must happen
before goal 3. It calls `insert_goal(2, "Add the migration regression test")`.
The new goal is pending at index 2. Existing goal identities and evidence do
not change. Once the current goal is completed, the scheduler selects the
first pending goal in list order, so the inserted prerequisite runs before
the old goal 3.

### 5.3 Insert before the active goal without preempting it

The agent discovers a missing prerequisite while implementing goal 2 and calls
`insert_goal(1, "Prepare the fixture required by goal 2")`. The active goal
object remains active and the runner does not jump phases or discard its
attempt. After the active goal reaches its normal verified boundary, the new
pending goal is selected before later pending goals. The runner does not
re-run a verified goal merely because its numeric position changed.

### 5.4 Recover after a failure

The agent appends a goal, the provider then returns a 429, and the process is
restarted. `--continue`, `--resume`, and TUI continuation rehydrate the same
run. The appended goal, its stable ID, list revision, active goal ID, current
phase, existing evidence, and original conversation remain available. The
agent receives the current dynamic goal context and does not see a fresh
`DECIDE_GOALS` phase unless the active cursor was actually there.

### 5.5 Invalid request

The agent calls `insert_goal(-1, "...")`, supplies an empty goal, or calls a
mutation tool when no typed goal context is available. The tool returns a
structured rejection, changes no in-memory or durable state, and does not
signal a phase transition. The agent can correct the call in the same phase.

## 6. Product semantics

### 6.1 Canonical tool contracts

The provider-facing schemas shall be exactly equivalent to:

```json
{
  "type": "object",
  "properties": {"goal": {"type": "string"}},
  "required": ["goal"],
  "additionalProperties": false
}
```

for `append_goal`, and:

```json
{
  "type": "object",
  "properties": {
    "index": {"type": "integer", "minimum": 0},
    "goal": {"type": "string"}
  },
  "required": ["index", "goal"],
  "additionalProperties": false
}
```

for `insert_goal`. The implementation must use concrete annotations and must
not use `object` as a parameter annotation that causes the lauren-ai schema
fallback warning.

Each call adds exactly one goal. The tools must not accept a hidden summary,
phase, goal ID, status, files list, or arbitrary metadata parameter. The
runner owns those values.

### 6.2 Valid goal text

The `goal` value is trimmed before validation and storage. A call is rejected
when:

- the value is not a string;
- the trimmed value is empty; or
- the value exceeds the configured per-goal size limit.

The error must identify the rejected field and provide a corrective example.
The original text must not be silently truncated. Identical text is allowed
as separate goals; every call receives a different stable ID because two
equal strings may represent different work.

The default per-goal size and total goal-list limits shall be explicit,
configurable, and documented. A limit is a resource-safety guard, not a
semantic deduplication rule. Reaching a limit returns a structured rejection
and leaves the list unchanged.

### 6.3 Valid insertion positions

Positions are zero-based and refer to the list as it exists immediately before
the call. `insert_goal(index=len(goals), goal=...)` is valid and is equivalent
to inserting after the current last item, although the response identifies
the operation as an insertion. Any negative index, boolean index, non-integer
index, or index greater than the current list length is rejected. The tool
must not normalize invalid positions by clamping them.

The returned index is the committed index after the operation. If another
mutation is attempted concurrently, the owner-side mutation lock and
checkpoint revision check determine a single serialized order; the tool must
return the position from that committed order.

### 6.4 Mutation response

Successful calls return a bounded structured response containing at least:

```json
{
  "ok": true,
  "operation": "append_goal",
  "goal_id": "opaque-stable-id",
  "index": 3,
  "goal_count": 4,
  "goal_list_revision": 2,
  "message": "Goal added. Continue the current goal; the new goal is pending."
}
```

The response is not a phase transition. It must not claim that the new goal
was implemented or verified. It must not include the entire goal list or
unbounded context.

Rejected calls return `ok: false`, a stable error code, a bounded explanation,
and a corrective action. They do not set the transition event.

### 6.5 Tool availability

`append_goal` and `insert_goal` are available when a typed `GoalContext` has
an initial finalized goal list and the workflow is in `IMPLEMENT_GOAL` or
`VERIFY_GOAL`. This is where new work is normally discovered and avoids
complicating the one-time initial decision handoff. The prompts must clearly
state that the tools are for newly discovered necessary work, not for
replacing the initial list.

The implementation may also expose the tools to a future dynamic planning
phase, but it must not expose them in a way that allows an uninitialized
context to mutate a list that has not passed `finalize_goals` validation.

The tools are not available in `CLARIFY` or before `finalize_goals` succeeds.
`SUMMARIZE` must not silently accept a mutation after it has determined that
all goals are complete. If product design later permits discoveries during
summary, it must explicitly route back to `IMPLEMENT_GOAL` through a new
transition contract; this PRD does not implicitly add that behavior.

## 7. State model and scheduling

### 7.1 Stable goal record

Replace index-aligned ownership with a serializable goal record. The precise
class name may follow repository conventions, but its durable shape shall be
equivalent to:

```text
GoalRecord
  goal_id: opaque non-empty string
  text: bounded string
  status: pending | active | verified
  attempts: non-negative integer
  implementation_summary: bounded string
  verification_evidence: bounded string
  files: bounded list of bounded relative paths
  created_revision: positive integer
  created_phase: workflow phase
```

`GoalContext.goals` is the ordered list of these records. The current cursor
is `active_goal_id`, not an integer. A derived `goal_index` may remain in the
context for existing UI and compatibility consumers, but it must be computed
from `active_goal_id` whenever both are present. It must never be trusted as
the only identity source.

`completed_goal_indices` may remain as a compatibility projection during
migration, but the canonical completion set is stable goal IDs. Per-goal
attempts, evidence, and files must travel with the record instead of relying
on parallel array positions.

### 7.2 Goal-list revision and mutation audit

`GoalContext` shall include a monotonic `goal_list_revision`. It starts at the
initial finalized-list revision and increments once for every successful
append or insert. A compact mutation receipt shall record, at minimum:

```text
GoalMutationReceipt
  revision: positive integer
  operation: append | insert
  goal_id: string
  index: non-negative integer
  phase: IMPLEMENT_GOAL | VERIFY_GOAL
  active_goal_id: string | null
```

Receipts are bounded and persisted with the typed context or the workflow's
existing compact receipt mechanism. They are for auditability and resume
diagnostics; the full conversation is not copied into the checkpoint.

### 7.3 Insertion and active cursor rules

An accepted mutation has these effects:

1. create one new record with status `pending` and a new `goal_id`;
2. insert it at the requested position or append it;
3. preserve every existing record object and its status/evidence/files;
4. preserve `active_goal_id` and the active phase;
5. derive the new numeric `goal_index` from the unchanged active ID;
6. increment `goal_list_revision`; and
7. write one mutation receipt.

An insertion before the active record changes its numeric index but does not
preempt it. An insertion after the active record does not affect the active
index. An insertion before a verified record creates a new pending record;
the verified record remains verified and is never replayed.

### 7.4 Selecting the next goal

After a successful `verify_goal(satisfied=true, ...)` call:

- mark the active record `verified` and persist its evidence;
- select the first `pending` record in current list order;
- mark that record `active` and enter its implementation phase;
- if no pending record remains, enter `SUMMARIZE`; and
- never select a record whose status is `verified` solely because its index
  changed.

While a goal is active, a mutation does not change its implementation or
verification phase. A failed verification continues to route back to the
same active goal's implementation phase. New pending goals wait until the
current goal's normal cycle reaches a verified boundary.

If a valid legacy checkpoint has no statuses or stable IDs, migration must
construct deterministic IDs and mark the goal at the saved cursor as active,
prior goals represented by the legacy completion audit as verified, and later
goals as pending. Ambiguous legacy state must produce a recovery diagnostic;
it must not guess by restarting at `CLARIFY` or silently replaying all goals.

## 8. Atomicity and durability

### 8.1 Mutation transaction

Each mutation is a small transaction:

1. verify owner and workflow phase;
2. validate the requested text and index against the current list revision;
3. construct a candidate context without mutating the live context;
4. update the candidate record list, cursor projection, revision, and receipt;
5. persist the candidate through the existing `WorkflowRunHandle` and
   checkpoint store while retaining the current phase and lifecycle;
6. commit the candidate in memory only after durable persistence succeeds; and
7. return the success response.

If serialization, validation, storage, or revision checking fails, the live
context and checkpoint remain unchanged and the tool returns a structured
recoverable error. It must not set a transition event. The operation must be
safe when failure finalization runs immediately afterward.

The implementation may use a carefully scoped copy/rollback operation rather
than a literal transaction object, but the externally observable contract is
atomic: success means the mutation is durable; failure means it is absent.

### 8.2 Checkpoint reasons and ordering

Successful mutations shall use a distinct bounded checkpoint reason such as
`goal_list_mutated`. The checkpoint must contain:

- the same session and conversation identity;
- the same workflow `run_id`;
- the same workflow name and plugin fingerprint;
- the current phase and active goal ID;
- the complete bounded goal records and list revision; and
- the mutation receipt.

The mutation checkpoint is not a phase transition checkpoint. It must not
advance `phase_index`, mark the goal verified, or create a second workflow
run. The existing phase-completion checkpoint remains the stronger boundary
after verification.

### 8.3 Failure and mid-turn recovery

If an ordinary exception occurs after a successful mutation, PRD-182 and
PRD-184 behavior applies. The failure finalizer must preserve the candidate
typed context and checkpoint, pause the same run, release its claim, and make
the run resumable. Resume must call `runner.resume(context)` with the goal
mutation present. A provider error must not select a new `run(intent)`,
recreate the initial goal list, or inject `DECIDE_GOALS` merely because the
mutation happened during an interrupted turn.

If the error occurs before mutation persistence completes, the mutation is
absent and the tool's structured error is retained in the conversation as
diagnostic tool output. If the error occurs after persistence, the mutation is
present exactly once. Repeated cleanup or finalization must not duplicate a
goal or receipt.

## 9. Agent prompts and tool guidance

### 9.1 Stable contract

The cache-stable `CACHE_CONTRACT` must gain only deterministic policy text,
for example:

```text
If implementation or verification discovers necessary work that is not in the
current goal list, record it with append_goal(goal) or insert_goal(index, goal).
Goal-list mutations are durable control-plane operations, not prose. A mutation
does not finish the current goal or change phase; continue the current goal
until its normal transition tool is called.
```

The stable contract must not contain the current goal list, list revision,
goal IDs, tool output, attempt count, or mutation history.

### 9.2 Dynamic phase instructions

`IMPLEMENT_GOAL` and `VERIFY_GOAL` dynamic instructions shall tell the agent:

- inspect the current goal list before deciding whether work is missing;
- add only concrete, necessary, testable work;
- use `append_goal` for work that belongs after all existing items;
- use `insert_goal` with a zero-based position for work that must occur at a
  particular point in the pending order;
- remember that inserting before the active goal does not preempt it;
- continue the active goal after a successful mutation;
- call `goal_implemented` or `verify_goal` only for the active goal; and
- never claim a mutation, implementation, or verification in prose alone.

Prompts must explain that duplicate goal text is allowed but creates a
separate goal identity, and that the tool response—not the agent's memory—is
the source of the committed index and ID.

### 9.3 Tool-only transitions

`append_goal` and `insert_goal` return success without setting the phase
transition event. The runner remains in its current inner loop. Only
`goal_implemented`, `verify_goal`, and the existing phase tools can move the
state machine. This prevents a goal-discovery side effect from accidentally
skipping implementation or verification.

## 10. UI, events, and observability

The existing TUI workflow projection shall continue to show the active goal
and phase. After a successful mutation it should be able to display a bounded
notice equivalent to:

```text
Goal added at 3 (4 goals total); continuing goal 2.
```

The exact presentation may follow existing appender/event conventions. The
notice must not dump the full goal list or unbounded goal text.

Emit a structured, redacted `goal_list_mutated` event containing:

- workflow name and run ID;
- operation and committed index;
- opaque goal ID or a safe short form;
- list revision and total count;
- active goal ID/index; and
- current phase.

Do not emit API keys, authorization headers, full conversation contents, raw
provider errors, or unrestricted filesystem data. Mutation event persistence
must follow existing session and workflow event ownership; it must not become
a second source of goal state.

## 11. Functional requirements

### FR-1 — Append tool

In `IMPLEMENT_GOAL` and `VERIFY_GOAL`, the agent can call
`append_goal(goal: str)`. A valid call durably adds one pending record at the
end and returns its ID, index, count, and revision.

### FR-2 — Insert tool

In `IMPLEMENT_GOAL` and `VERIFY_GOAL`, the agent can call
`insert_goal(index: int, goal: str)`. A valid call durably adds one pending
record at the exact requested position, including index 0 and `len(goals)`.

### FR-3 — No implicit transition

Neither mutation tool changes phase, active goal identity, goal status, or
transition event except for the new pending record and the derived numeric
index projection.

### FR-4 — Stable identity

Every goal has a stable unique ID. Existing records retain their ID when a
new record is appended or inserted. Completion, evidence, files, attempts,
and summaries are keyed by that identity.

### FR-5 — Deterministic scheduling

After the active goal is verified, the runner selects the first pending goal
in list order and skips all verified records. The ordering behavior is the
same in a fresh run and a resumed run.

### FR-6 — Atomic durable mutation

Successful mutation responses are preceded by a durable checkpoint. Any
validation or persistence failure leaves both in-memory and on-disk state
unchanged.

### FR-7 — Cursor preservation

Insertion before the active goal updates the derived numeric index but
preserves the active goal ID and phase. No completed goal is replayed because
of an index shift.

### FR-8 — Recovery continuity

The same run ID, conversation ID, workflow name, intent, goal IDs, list
revision, order, statuses, and mutation receipts survive ordinary exceptions,
TUI continuation, `--continue`, `--resume`, and process restart.

### FR-9 — Checkpoint compatibility

The checkpoint codec serializes and restores the new records without live
objects, memory handles, or unbounded conversation content. Existing valid
goal-flow checkpoints remain readable through an explicit migration path.

### FR-10 — Prompt and schema correctness

All relevant prompts describe the mutation tools accurately, tool schemas use
concrete types, and no schema-generation warning is emitted for these tools.

### FR-11 — Completion safety

`complete_workflow` cannot produce a successful terminal result while any goal
record is pending or active. A goal added before summary is not silently
ignored. If summary-time mutation is not supported, the tool must reject
mutation attempts rather than creating an inconsistent terminal state.

### FR-12 — Owner and concurrency safety

Only the live workflow owner may mutate the goal list. Concurrent mutation
calls are serialized and receive monotonic revisions. A stale or conflicting
checkpoint write fails closed and cannot overwrite a newer list.

## 12. Non-functional requirements

### NFR-1 — Reliability

No accepted goal may disappear after a successful tool response. Repeated
failure finalization, retry cleanup, or resume must not duplicate it.

### NFR-2 — Determinism

Given the same initial checkpoint and ordered mutation calls, the resulting
goal IDs/order/status/cursor are deterministic except for the opaque generated
ID values. Tests must compare identity relationships and revisions rather than
wall-clock timestamps.

### NFR-3 — Performance

Normal tool calls must not rescan the workspace, replay the whole conversation,
or rebuild the provider prompt. List mutation is O(number of goals) at most;
the operation must not scale with artifact size. Checkpoint payloads contain
bounded goal metadata, not implementation files or conversation transcripts.

### NFR-4 — Cache stability

Mutation data belongs in the dynamic prompt/context region. The stable system
prompt and tool definitions must remain byte-stable across list revisions,
apart from the intentional presence of the same deterministic tool schemas.

### NFR-5 — Security

Validate goal text, indices, IDs, revisions, and checkpoint payloads. Do not
accept paths or executable instructions as special control values. Files remain
runner-owned metadata and are not accepted by mutation tools. Redact goal text
in telemetry where existing policy requires it.

### NFR-6 — Backward compatibility

Old checkpoints containing string goals and parallel arrays must either be
migrated to valid stable records or reported as diagnostic-only. They must
never be treated as a new run, silently reset to an empty list, or replay all
goals without an auditable cursor decision.

### NFR-7 — Maintainability

Mutation validation, record serialization, scheduling, checkpointing, and UI
projection must have clear ownership. Do not duplicate goal-list mutation
logic in TUI, headless, and workflow-specific callers.

## 13. Proposed implementation design

### 13.1 Goal record and compatibility layer

Introduce a small serializable `GoalRecord`/`GoalStatus` model in the
`goal_flow` package. Move per-goal state from parallel arrays into records.
Keep compatibility properties or a codec adapter for callers that still read
`goal_index`, `goal_evidence`, `goal_files`, and `completed_goal_indices`.
Those properties must be derived from records and must not become a second
mutable store.

The codec should write a versioned goal-list section, for example:

```json
{
  "goal_list_version": 2,
  "goal_list_revision": 4,
  "active_goal_id": "g-…",
  "goals": [
    {
      "goal_id": "g-…",
      "text": "Add migration tests",
      "status": "pending",
      "attempts": 0,
      "implementation_summary": "",
      "verification_evidence": "",
      "files": [],
      "created_revision": 4,
      "created_phase": "VERIFY_GOAL"
    }
  ],
  "mutation_receipts": []
}
```

The exact generated ID format is internal and must not be supplied by the
agent. The codec must reject duplicate IDs, invalid statuses, negative
revisions, malformed positions, and inconsistent active/completed records.

### 13.2 Shared mutation service

Add one runner-owned helper responsible for both tools. Its conceptual API is:

```python
mutate_goal_list(
    operation: Literal["append", "insert"],
    goal: str,
    index: int | None = None,
) -> GoalMutationResult
```

The helper validates, creates a candidate context, persists it through the
existing handle, commits it, and returns a bounded result. The two provider
tools are thin adapters that call this helper. They must not each implement
their own list/index/checkpoint rules.

Use the existing workflow owner/claim and checkpoint revision mechanisms. Add
an async or synchronous lock only at the narrow mutation boundary needed by
the current executor; do not create a new global workflow lock.

### 13.3 Runner integration

Pass the mutation tools into the implementation and verification phase turns.
After a successful mutation, refresh the dynamic phase context used by the
next inner-loop turn so the agent sees the current count/order/cursor without
rewriting the stable prompt. Keep `goal_implemented` and `verify_goal` as the
only tools that set their respective phase events.

Refactor `_verify_goal` to mark records by ID and select the next pending
record, rather than incrementing an integer and indexing parallel arrays.
Refactor `_implement_goal` and `_verify_goal` prompts to render the active
record from its ID. Keep `goal_index` only as a derived UI/checkpoint field.

### 13.4 Checkpoint and resume integration

Use the existing `WorkflowRunHandle.save_checkpoint()` path for accepted
mutations and the existing `WorkflowRecoveryCoordinator` for rehydration.
Do not add special `goal_flow` resume dispatch. After rehydration:

1. decode and validate goal records;
2. resolve the active record by `active_goal_id`;
3. derive its current index;
4. repair only safe derived projections;
5. preserve the saved workflow phase; and
6. enter the existing `runner.resume(context)` path.

If a checkpoint's ID and index disagree but the ID is valid, the ID wins and
the repair is recorded. If the ID is missing or ambiguous, fail closed with a
recovery diagnostic rather than selecting `CLARIFY` or a new run.

### 13.5 Prompt/cache integration

Update the stable contract, dynamic phase prompts, `PhaseSpec` metadata, and
`llms-full.txt` guidance consistently. The current list, active goal, list
revision, and mutation receipts are dynamic. Tool names, schemas, and policy
wording are stable. Do not add a rolling goal-list summary to the stable
system prompt.

## 14. Acceptance criteria

### AC-1 — Append during implementation

Given two finalized goals and an active first goal, calling
`append_goal("third goal")`:

- returns `ok: true`, a new ID, index 2, and revision 1;
- keeps the phase and active first-goal ID unchanged;
- leaves the first two records byte-for-byte equivalent except for derived
  list metadata;
- creates one pending third record; and
- allows the existing implementation transition to proceed.

### AC-2 — Insert at every valid position

For a list of three goals, insertion at 0, 1, 2, and 3 succeeds and produces
the expected order. Index 3 is accepted as the end position. Negative,
boolean, fractional/non-integer, and greater-than-length positions are
rejected without mutation.

### AC-3 — Active goal is not preempted

Insert a goal before the active record during implementation and during
verification. The active ID, phase, attempt count, implementation summary,
and current evidence remain unchanged. The inserted record is pending and is
selected only after the active goal reaches its normal verified boundary.

### AC-4 — Verified goals are not replayed

Verify goal A, insert a new goal before A, and resume the runner. A remains
verified and is not implemented or verified again. The new goal is processed
exactly once, followed by the remaining pending goals in list order.

### AC-5 — Duplicate text has separate identity

Append or insert the same text twice. Both calls succeed with different IDs,
two pending records, two execution cycles, and independent evidence/status.

### AC-6 — Invalid payloads fail closed

Empty/whitespace text, wrong types, invalid indices, unavailable phase, missing
typed context, and exceeded configured limits return structured errors. No
event, in-memory list, checkpoint revision, or goal count changes.

### AC-7 — Mutation is durable before success

After a successful tool result, load the checkpoint from a fresh store object
and assert that the new record, order, active ID, list revision, and receipt
are present. Inject a checkpoint failure and assert that the tool returns an
error and the old checkpoint/list remain unchanged.

### AC-8 — Mid-turn failure and resume

Mutate the list, inject a provider 429 or arbitrary ordinary exception before
the next transition, finalize the workflow failure, restart the session, and
resume. Assert the same session ID, conversation ID, run ID, phase, active goal
ID, list order, statuses, revision, and receipt. Assert that `run(intent)` and
the initial goal decision are not called.

### AC-9 — Multiple mutations are ordered

Perform a sequence of append and insert calls in one and multiple turns.
Assert monotonic revisions, serialized order, unique IDs, correct final list,
and exactly one checkpoint per successful mutation plus the normal phase
boundaries.

### AC-10 — Normal completion

Run a real scripted workflow that adds a goal during implementation, verifies
all original and new goals, and summarizes. Assert all records are verified,
the final summary includes the added work, and no goal is skipped or replayed.

### AC-11 — Completion guard

Attempt `complete_workflow` while a dynamically added goal is pending. The
tool or runner rejects completion, keeps the run non-terminal, and exposes the
pending goal in dynamic context. Completion succeeds only after that goal is
verified.

### AC-12 — Legacy checkpoint migration

Load a valid pre-feature checkpoint with string goals and parallel arrays.
Assert deterministic stable IDs, preserved active/completed semantics, a
versioned migrated payload, and resume from the saved phase. Corrupt or
ambiguous legacy payloads produce diagnostics and never start a new initial
run.

### AC-13 — Cache contract

Across list mutations, assert that the stable workflow prompt and tool schema
fingerprint remain unchanged while dynamic phase context changes only in the
goal-list region. No mutation injects messages at the beginning of the
conversation or copies the full list into the stable prefix.

### AC-14 — Owner and concurrency behavior

A second owner cannot mutate a live run. Concurrent calls from the valid owner
are serialized, produce unique monotonic revisions, and cannot overwrite a
newer checkpoint. A stale revision is rejected and does not alter the list.

## 15. Testing strategy

All tests use temporary session/checkpoint stores, deterministic IDs or ID
factories, fake providers, and isolated conversations. No test depends on a
real provider, home directory, browser, MCP server, or network rate limit.

### 15.1 Unit tests

- exact JSON schemas for both tools;
- trimming, empty text, type, length, and resource-limit validation;
- valid boundary indices and invalid index types/values;
- append order and insert order at every position;
- duplicate text with distinct IDs;
- stable active ID when insertion shifts its derived index;
- per-record evidence/files/attempts remain associated after insertion;
- monotonic list revisions and mutation receipts;
- no transition event from either mutation tool;
- atomic rollback on codec/storage failure;
- owner and stale-revision rejection;
- next-pending scheduler skipping verified records;
- completion guard with pending records;
- stable-ID codec round trip and malformed-payload rejection; and
- legacy checkpoint migration and ambiguity diagnostics.

### 15.2 Integration tests

- real `GoalFlowRunner` implementation turn with `append_goal`;
- real verification turn with `insert_goal` before and after the active item;
- checkpoint reload through `WorkflowRunHandle.from_checkpoint`;
- mutation followed by phase-boundary checkpoint;
- mutation followed by rejection/retry;
- mutation followed by arbitrary exception and universal failure finalizer;
- TUI projection and bounded `goal_list_mutated` event;
- `--continue` and explicit `--resume` dispatch to `resume(context)`;
- claim conflict and concurrent mutation serialization; and
- cache-contract fingerprint stability across mutation revisions.

### 15.3 End-to-end tests

Use a scripted streaming provider and real workflow tools to:

1. finalize an initial list;
2. implement goal 1;
3. append a follow-up goal;
4. insert a prerequisite before a later goal;
5. verify every goal in the resulting order;
6. inject a provider failure after one mutation;
7. close and reopen the session;
8. resume the same run; and
9. complete the workflow.

Assert tool call order, phase annotations, goal IDs/order/status, checkpoint
revisions, transcript continuity, no duplicate implementation calls, no fresh
initial-decision call, and final summary correctness. Add separate controls
for a no-mutation run and an explicit reset/new-run flow.

## 16. Error handling and diagnostics

Use stable error codes such as:

```text
goal_mutation_unavailable_phase
goal_mutation_context_missing
goal_text_empty
goal_text_too_long
goal_list_limit_reached
goal_index_invalid
goal_checkpoint_conflict
goal_checkpoint_unavailable
goal_context_invalid
```

Tool errors must be bounded and actionable. Checkpoint and owner errors must
not be represented as successful mutations. Recovery diagnostics should show
workflow, phase, run ID, list revision, and a safe explanation, but never raw
provider payloads, secrets, or full goal/workspace contents.

## 17. Rollout and migration

1. Add the versioned record/codec and migration tests behind no user-visible
   behavior change.
2. Add the shared mutation service and unit tests.
3. Add tools to implementation and verification phases with prompt/schema
   contract tests.
4. Switch scheduling and completion checks to stable identity.
5. Add checkpoint/recovery integration and E2E failure/resume coverage.
6. Update workflow, storage, and public symbol documentation.
7. Enable the behavior by default after the full test matrix passes.

Existing valid checkpoints must remain loadable. A checkpoint written during a
deployment race must be handled by the existing revision/claim rules. No
compatibility path may silently convert a failed dynamic run into a fresh
initial goal decision.

## 18. Documentation requirements

Update in the same implementation change:

- `docs/guides/workflows.md` with the two tools, insertion semantics, and
  resume behavior;
- `docs/reference/storage.md` with the goal-record/checkpoint version and
  mutation receipt retention;
- `llms-full.txt` and, if applicable, `llms.txt` with public tool and context
  contracts;
- the `goal_flow` module docstring and public symbol docstrings;
- `prds/README.md` with this PRD and its status; and
- relevant workflow findings in `docs/reference/workflow-review.md`.

Prompts and documentation must use the same zero-based indexing, stable-ID,
active-goal, and pending/verified terminology. Do not describe insertion as a
phase transition.

## 19. Open questions and assumptions

### Assumptions

- One goal mutation is the smallest useful atomic operation; batch insertion
  is deliberately excluded to keep provider schemas and error recovery small.
- Duplicate text is valid and represents separate work; stable IDs disambiguate
  it.
- New goals are discovered primarily during implementation and verification,
  so those are the initial supported phases.
- The active goal finishes before a newly inserted goal is scheduled; this
  avoids preempting side effects mid-goal.
- The existing checkpoint store and universal exception recovery are the only
  durability and recovery mechanisms.

### Questions to resolve during implementation

1. What default and maximum values should configuration expose for per-goal
   text and total goal count, given the repository's checkpoint-size policy?
2. Should the TUI offer a compact expandable goal-list view, or only the
   mutation notice and active-goal counter in the first release?
3. Should mutation receipts be retained for the full workflow or compacted to
   the latest bounded window after a configured count?
4. Should a future summary-time discovery flow be a separate transition phase
   rather than extending `SUMMARIZE` with mutation tools?

The implementation may resolve these questions with documented defaults, but
must not weaken stable identity, atomicity, cursor preservation, or resume
requirements.

## 20. Definition of done

- Both tools exist with the exact minimal schemas and concrete annotations.
- Append and insert work at every valid position and reject invalid calls
  without side effects.
- The active goal remains active during mutation and verified goals are never
  replayed because of index movement.
- Goal records have stable IDs and per-record evidence/attempt/file state.
- Every successful mutation is checkpointed before the tool reports success.
- The same workflow run resumes with all dynamic goal-list state after ordinary
  exceptions, process restart, `--continue`, and `--resume`.
- Completion is impossible while a dynamically added goal remains pending.
- Prompts, schemas, TUI projection, storage documentation, and generated
  symbol inventories agree with runtime behavior.
- Unit, integration, and E2E acceptance tests cover normal, invalid, repeated,
  concurrent, interrupted, migrated, and completed flows.
- Relevant lint, type, type-audit, and test checks pass, with unrelated
  repository blockers reported explicitly.
