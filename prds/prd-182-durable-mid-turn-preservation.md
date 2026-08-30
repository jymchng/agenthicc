---
title: "PRD-182: Durable preservation of failed mid-turn conversation state"
status: Proposed
version: 1.0.0
created: 2026-08-30
scope: "agent turn retries, lauren-ai streaming, conversation journals, TUI transcript, and workflow continuation"
related_prds:
  - PRD-126 # transport retry
  - PRD-129 # conversation durability and retry resilience
  - PRD-148 # unified interrupt and graceful cancellation
  - PRD-156 # resumable plan interrupts and workflow continuation
  - PRD-169 # tool-call transaction integrity
  - PRD-170 # workflow resume recovery
tags:
  - conversation
  - durability
  - retries
  - streaming
  - recovery
  - lauren-ai
---

# PRD-182 — Durable preservation of failed mid-turn conversation state

## 1. Executive summary

When an agent turn fails after it has already completed one or more internal
assistant/tool steps, the next turn can behave as though those steps never
happened. The user sees the partial work in the live UI, but a later request
may be built from a memory snapshot taken before the failed turn. In some
cases a retry also re-executes tools whose side effects have already happened.

The cause is a mismatch between two meanings of “turn”:

- To the TUI, a turn is one user submission.
- To `lauren-ai`, one streaming turn can contain multiple provider
  round-trips, tool exchanges, and committed assistant messages.

`AgentTurnRunner._stream_with_retry()` currently applies snapshot/restore
retry semantics to the whole streaming turn. A failure in a later provider
round therefore restores the memory state from before the first provider
round. `JournaledShortTermMemory.restore()` journals that replacement, so
durability does not prevent the loss. The durable tool ledger can prevent a
side effect from running twice, but it cannot restore the assistant/tool
messages that explain the work already performed.

This PRD changes the retry and persistence contract so that committed
provider steps are never rolled back by a later failure. Provider retries are
step-scoped, partial output is represented explicitly, and every failed
logical turn leaves a durable, inspectable outcome. The same session-scoped
conversation remains the source of truth for direct turns, workflows, resume,
and subsequent user messages.

## 2. Problem statement

Consider a request to refactor several files:

```text
H = previous conversation history
U = current user request
A1 = assistant tool-call message for step 1
R1 = tool results for step 1
A2 = assistant request for step 2
```

The intended state after step 1 is:

```text
H, U, A1, R1
```

If the provider fails while producing step 2, the current retry path behaves
approximately as follows:

```text
snapshot = H                         # captured before run_stream adds U
run_stream → U, A1, R1, A2(partial)
transport error
memory.restore(snapshot)             # U, A1, and R1 disappear
retry run_stream from H
```

The effects are:

1. The next provider request does not contain the completed step that the
   agent needs to understand its current state.
2. The TUI's live projection and the provider-facing memory disagree.
3. A phase loop or the next user message may start from an apparently fresh
   conversation.
4. A tool may be invoked again even though the first invocation changed the
   filesystem or another external system.
5. A process restart cannot reliably distinguish “failed but retained” from
   “cleanly completed” because the current handled-error path writes a
   terminal abort marker and does not preserve a resumable step cursor.
6. Partial streamed assistant text is visible while streaming but is not
   necessarily recorded as a durable transcript event because the current
   path emits final text at a stop boundary.

This is not a request to retain malformed provider protocol messages. A
partial tool-call JSON object must not be sent back to a provider as if it
were a completed tool call. The requirement is to preserve every valid
committed message and to make incomplete output visible and recoverable as an
explicit interruption record.

## 3. Current implementation and confirmed failure boundary

The implementation investigation found the following ownership boundaries:

| Layer | Current responsibility | Current limitation |
|---|---|---|
| `TUISession` | accepts one user submission, owns the shared session conversation, and advances queued messages | assumes the inner runner's result represents one atomic turn |
| `AgentTurnRunner._stream()` | builds the agent turn, handles compaction, consumes streaming chunks, and finalizes lifecycle markers | wraps the complete `run_stream()` invocation in one retry boundary |
| `AgentRunnerBase.run_stream()` in `lauren-ai` | adds the user message, loops over provider calls, commits assistant messages and tool results | the retry caller cannot observe each internal provider-step commit |
| `run_with_transport_retry()` | snapshots memory, invokes a callable, restores on transient failure, and retries | its callable is the whole streaming turn, not one provider request |
| `JournaledShortTermMemory` | journals appends and full-state `restore()` resets | a reset after a late failure durably removes already committed steps |
| `DurableIdempotencyLedger` | replays recorded tool results after retry | prevents duplicate effects but does not restore conversation messages |
| `ConversationStore` / session event log | renders and persists the TUI projection | final text is commonly emitted only after a stop boundary; projection is not provider memory |
| `RunCoordinator` | detects an unclosed crash-interrupted turn and prepares a replay | handled mid-turn errors are currently terminalized without a committed-step resume cursor |

The existing append-only journal and tool-exchange integrity checks remain
valuable. This PRD changes their transaction granularity and lifecycle
semantics; it does not create a second conversation store.

## 4. Goals

1. Preserve all valid assistant, user, and tool-result messages committed
   before a later error in the same logical turn.
2. Retry only the failed provider step when a retry is safe and configured.
3. Ensure an error cannot cause a side-effecting tool to run twice without an
   explicit user-directed retry and idempotency evidence.
4. Persist enough state during a turn to recover after process termination,
   not only after a clean turn boundary.
5. Keep the shared `conversation_id` and one
   `JournaledShortTermMemory` across direct turns, Plan mode, workflows,
   workflow phase changes, and resume.
6. Make the TUI transcript truthful: completed work remains visible, partial
   output is labeled as interrupted, and a failed turn is not presented as a
   successful assistant answer.
7. Preserve provider protocol validity. Incomplete tool calls are repaired or
   quarantined; malformed partial data is never blindly sent to the provider.
8. Keep retries bounded, observable, deterministic under tests, and compatible
   with the existing timeout, cancellation, approval, compaction, and
   checkpoint contracts.

## 5. Non-goals

- Treating a partial provider response as a successful final answer.
- Sending incomplete tool-call JSON, unsigned thinking blocks, or an
  unanswered tool call back to a provider.
- Keeping a second authoritative transcript in `ConversationStore`, SQLite,
  workflow checkpoints, or a provider-specific cache.
- Re-running the entire logical turn merely because a later provider step
  failed.
- Guaranteeing exactly-once execution for an external side effect whose
  outcome cannot be observed or whose tool does not participate in the
  idempotency contract. Such tools must remain explicitly marked uncertain.
- Silently discarding old journal records or requiring users to delete their
  sessions.

## 6. Definitions

### 6.1 Logical turn

One accepted user submission, identified by a stable `logical_turn_id`. It
starts when the user message is accepted and ends with one of `completed`,
`failed`, `cancelled`, or `recovered`.

### 6.2 Provider step

One provider request/response cycle inside a logical turn. A provider step can
produce an assistant text response or an assistant tool-call batch. A tool-call
step includes the tool execution and the result message before it becomes
committed.

### 6.3 Provider attempt

One network attempt for one provider step. An attempt may fail before the first
byte, during streaming, or after the response has become complete. An attempt
ID is never reused for a different request.

### 6.4 Committed message

A provider-valid message whose append and required tool-exchange lifecycle
metadata have been durably recorded. A later failure must not remove it.

### 6.5 Partial fragment

Streamed text, thinking, or tool-call input observed before a provider step
reached a valid completion boundary. It is durable transcript evidence, not a
provider message unless it can be converted into a valid, explicitly marked
message without violating the provider contract.

### 6.6 Safe retry point

A journal cursor and in-memory snapshot immediately after the last committed
provider step and before the next provider request. Restoring a safe retry
point may discard only uncommitted state belonging to the failed attempt.

## 7. Proposed architecture

### 7.1 Separate the four transaction scopes

```text
logical user turn
│
├── provider step 1
│   ├── provider attempt(s)
│   └── committed assistant/tool messages + journal receipt
│
├── provider step 2
│   ├── provider attempt(s)
│   └── committed assistant/tool messages + journal receipt
│
└── terminal turn outcome
```

The logical-turn owner must never restore a snapshot taken before a committed
provider step. The provider-step owner may restore only its own uncommitted
attempt state.

### 7.2 Step-scoped retry in `lauren-ai`

`lauren-ai` must expose an internal, provider-neutral step boundary in its
streaming runner. The exact public API can follow the existing project
conventions, but it must provide these semantics:

1. Before `transport.complete()`, capture a step checkpoint containing the
   current memory cursor, active exchange state, and attempt ID.
2. Retry a transient transport failure at the provider-request boundary.
3. Do not call the outer `run_stream()` again for a provider retry.
4. After a complete provider response and valid tool exchange, append the
   assistant/result messages and emit a `step_committed` event.
5. Advance the safe retry point only after the append and lifecycle receipt
   are durable.
6. If streaming fails after partial output, emit a `step_interrupted` event,
   discard or quarantine only the incomplete response, and leave all earlier
   committed messages untouched.
7. If a failure occurs after a tool side effect but before its provider result
   is committed, consult the turn ledger and commit/recover the known result;
   never blindly execute the tool again.

`lauren-ai` should own provider-step mechanics because only it knows when its
internal stream loop has committed an assistant message, started a tool
exchange, or appended the consolidated result batch.

### 7.3 agenthicc integration

`AgentTurnRunner` must use the step-aware contract instead of wrapping the
entire streaming generator in a destructive snapshot/restore retry. It must:

- create one logical-turn identity and one ledger for the full turn;
- pass the shared session memory and stable `conversation_id` to every step;
- consume step lifecycle events and advance the durable safe retry cursor;
- preserve committed steps when a later step fails;
- emit one terminal logical-turn outcome after the step runner stops; and
- retain a compatibility adapter for older `lauren-ai` versions only if that
  adapter can prove that no committed step will be erased. Otherwise it must
  fail closed with a retained failure record instead of using whole-turn
  rollback.

The old `run_with_transport_retry()` helper may remain for stateless calls and
for operations whose callable is truly atomic. It must not be used around a
multi-step `run_stream()` invocation after this change.

### 7.4 Durable journal extensions

Extend `conversation-journal.jsonl` with versioned, redacted lifecycle records.
Existing `append` and `reset` records remain readable. The preferred records
are:

```json
{"kind":"turn_started","schema_version":2,"turn_id":"T","conversation_id":"S","base_cursor":41}
{"kind":"step_started","turn_id":"T","step_id":"T.1","attempt_id":"T.1.a","base_cursor":42}
{"kind":"step_committed","turn_id":"T","step_id":"T.1","cursor":47,"message_count":3}
{"kind":"step_interrupted","turn_id":"T","step_id":"T.2","attempt_id":"T.2.a","reason":"transport","partial_chars":128}
{"kind":"turn_failed","turn_id":"T","last_committed_step":"T.1","cursor":47,"retryable":true,"error_kind":"transport"}
```

Requirements for these records:

- sequence numbers are monotonic and fsynced with the corresponding state;
- IDs are stable within a logical turn and unique across attempts;
- error messages are bounded and redacted; secrets and raw authorization
  headers are forbidden;
- partial content is stored according to the session transcript retention
  policy and is never inserted into a provider request automatically;
- a record written twice with the same idempotency key folds once;
- an interrupted trailing JSONL write does not invalidate earlier records;
- old journals containing only `append`, `reset`, `turn_started`,
  `turn_completed`, `turn_aborted`, and `tool_recorded` still fold correctly.

`reset` remains valid for compaction and an attempt-local rollback. A reset
must include the safe cursor/step it belongs to. A reset that would remove a
committed step from the same logical turn must be rejected and logged as a
durability invariant failure.

### 7.5 Provider-memory projection

The session journal remains authoritative. `JournaledShortTermMemory` is its
live projection and must expose or internally implement:

- `begin_logical_turn()`;
- `begin_provider_step()`;
- `commit_provider_step()`;
- `rollback_uncommitted_attempt()`;
- `record_partial_fragment()`; and
- `finalize_turn_failure()`.

Names may differ, but the ownership and ordering contract must be explicit.
`restore()` must distinguish an attempt-local restore from an arbitrary full
state replacement. A caller cannot accidentally restore to a pre-turn
snapshot while committed steps exist.

### 7.6 Transcript projection

The TUI's `ConversationStore` and `SessionEventLog` remain projections of the
same turn identity. During streaming:

- deltas may be rendered live;
- bounded partial fragments are persisted at configured checkpoints and at
  failure/cancellation;
- committed assistant text/tool events are persisted once;
- the terminal event is one of `turn_completed`, `turn_failed`, or
  `turn_cancelled`;
- retry notices are linked to the attempt, not emitted as extra assistant
  messages; and
- a later turn cannot overwrite or replace the previous failed turn.

The UI should show a concise “turn interrupted/failed; completed work
preserved” state and offer the existing retry/resume controls. It must not
claim that an incomplete provider response was completed.

## 8. Failure semantics

| Failure point | Durable memory | Transcript | Next action |
|---|---|---|---|
| Before first provider byte | user message and prior history retained | failed turn with no assistant result | bounded step retry or terminal failure |
| Mid-stream text, no valid completion | prior committed messages retained; partial excluded from provider memory | partial fragment marked interrupted | retry the same step or wait for user input |
| Mid-stream tool input | prior committed messages retained; malformed tool call quarantined | tool/step interruption diagnostic | retry step without replaying committed tools |
| After assistant/tool exchange commit | all committed messages retained | committed step visible | continue next provider step |
| Tool side effect completed, result acknowledgment delayed | ledger/result receipt retained | tool outcome marked known or uncertain | replay receipt or request explicit user retry; no blind execution |
| Permanent provider error | all committed messages retained | terminal failed turn with sanitized error | user may submit a new turn with full context |
| User cancellation | all committed messages retained; dangling exchange repaired | cancelled turn with preserved work | no automatic retry unless explicitly requested |
| Process death | journal replay restores committed steps and open turn | resume marker on next attach | resume from last safe step, not from pre-turn history |
| Compaction failure | pre-compaction committed state retained | compaction failure diagnostic | do not reset conversation to an older turn boundary |

## 9. Data flow

```text
User submits U
      │
      ▼
TUISession creates logical_turn_id T
      │
      ├── append user message + turn_started(T) ── fsync
      │
      ▼
Lauren step runner receives shared memory + conversation_id
      │
      ├── step_started(T, S1, A1)
      │       │
      │       ├── provider stream
      │       │     ├── complete → assistant/tool execution → results
      │       │     └── error → discard only A1 partial attempt
      │       │
      │       └── step_committed(T, S1) ── append messages + fsync
      │
      ├── step_started(T, S2, A2)
      │       │
      │       └── provider error
      │
      ├── step_interrupted(T, S2, A2, partial metadata) ── fsync
      ├── turn_failed(T, last_committed=S1) ── fsync
      │
      ▼
Shared memory = H, U, committed S1 messages
      │
      ├── TUI transcript shows S1 and failed/interrupted S2
      └── next user message D appends after T and sends H, U, S1, D
```

The critical invariant is:

```text
failure in step N may remove only uncommitted attempt N;
it may never remove committed steps 1 through N-1.
```

## 10. Functional requirements

### FR-1 — Stable identity

Every accepted user submission has one logical-turn ID. Provider step IDs and
attempt IDs are children of that ID. Retries reuse the step ID only when they
are retries of the same semantic request; they always receive a fresh attempt
ID.

### FR-2 — Commit ordering

A provider step is considered committed only after its valid memory mutation,
tool ledger receipt, and journal record are durable. The commit operation is
idempotent and safe to call again after an acknowledgement timeout.

### FR-3 — Step-scoped transport retry

Transient network failures retry the current provider step within configured
attempt, deadline, and backoff limits. The retry cannot restore a snapshot
older than the last committed step.

### FR-4 — No destructive whole-turn retry

No production path may call whole-turn snapshot/restore around a multi-step
`run_stream()`. A compatibility path must either use step receipts or return a
retained failure without retrying.

### FR-5 — Preservation after error

After any non-cancellation error, all prior valid user, assistant, and
tool-result messages remain in live memory, the folded journal, and the next
provider request. The error itself is represented by a lifecycle/diagnostic
record, not by deleting history.

### FR-6 — Partial output handling

Partial deltas are captured with bounded size and explicit status. Partial
tool calls and invalid thinking blocks are quarantined from provider memory.
The UI can display them as interrupted output, but a later provider request
does not include them unless a provider-neutral recovery representation is
explicitly defined and validated.

### FR-7 — Tool idempotency

A tool execution receipt includes logical turn, step, attempt, tool name,
canonical arguments, result status, and uncertainty status. A retry consults
the receipt before executing a side-effecting tool. A known result is replayed;
an uncertain result is surfaced for recovery instead of executed blindly.

### FR-8 — Terminal failure marker

The journal records `turn_failed` with the last committed cursor/step and a
sanitized error category. `turn_aborted` remains available for intentional
user cancellation, but must not be used to conceal a provider failure or make
valid committed history disappear.

### FR-9 — New-turn continuity

When the user submits a new message after a failed turn, the same session
conversation and `conversation_id` are used. The provider request contains
all prior valid committed context and the new message exactly once.

### FR-10 — Crash recovery

On restart or `--resume`, journal replay reconstructs the committed memory
projection and identifies an open/failed turn. Recovery resumes from its last
safe step when the user chooses to resume, or leaves the failed turn retained
when the user starts a new message. Recovery must never roll back to a
pre-turn count merely because the turn ended abnormally.

### FR-11 — Workflow continuity

Workflow phase runners, Plan mode, Yolo/Safe direct turns, subagents, and
workflow resume all use the same step-aware session memory contract. A failed
phase retains committed steps and artifacts; phase checkpoint restoration must
not replace the shared conversation with a pre-phase snapshot.

### FR-12 — Compaction safety

Compaction may replace the live context only through a journaled,
meaning-preserving compaction record. An error during compaction restores the
last valid compacted state or the current committed state, never the state
before the logical turn began.

### FR-13 — Observability

Emit structured, bounded diagnostics for logical-turn start, step start,
attempt retry, step commit, step interruption, turn failure, and recovery.
Diagnostics include IDs, counts, durations, and categories, but never prompt
contents, secrets, tool arguments, or raw tool output unless the existing
redacted transcript policy explicitly permits it.

### FR-14 — Backward-compatible journal folding

Existing journals and session exports remain readable. Missing step records are
interpreted conservatively: existing message order is retained, and the
session is marked as legacy/uncertain rather than reset or silently discarded.

## 11. Non-functional requirements

### NFR-1 — Durability

A successful commit receipt survives process termination after the write
returns. Journal writes retain existing flush/fsync guarantees and handle
partial trailing records safely.

### NFR-2 — Atomicity

A step commit is all-or-nothing from the perspective of the provider-memory
projection. A failed commit must be recoverable from the journal without
duplicating messages or tool results.

### NFR-3 — Idempotency

Replaying the same journal, retrying an acknowledgement, or opening the same
session twice must not duplicate user messages, assistant messages, tool
results, or terminal lifecycle records.

### NFR-4 — Performance

Step receipts must be bounded metadata. They must not copy the full transcript
for every provider attempt. Normal successful turns should add only the
messages and lifecycle records they actually produce.

### NFR-5 — Provider neutrality

The contract must work for Anthropic, OpenAI-compatible, Modal, local, and
test transports. No provider-specific exception string may be required to
determine whether a committed step is safe.

### NFR-6 — Cancellation safety

`CancelledError`, keyboard interrupts, TUI pause, and process shutdown must
not be converted into transport retries. Any completed step remains durable,
and the active step is either cleanly interrupted or marked uncertain.

### NFR-7 — Security

Partial fragments, exception strings, and journal metadata are bounded and
sanitized. API keys, authorization headers, credentials, and untrusted tool
payloads must not appear in lifecycle diagnostics.

## 12. Acceptance criteria

### AC-1 — Reproduction case is fixed

With a scripted multi-step turn where step 1 commits an assistant tool call
and tool result and step 2 fails during streaming, the next provider request
or next user turn contains the prior history, the current user message, and
step 1 exactly once. Step 1 is not erased by retry or terminal failure.

### AC-2 — Retry scope is observable

The fault-injection test proves that a transient failure in step 2 retries
step 2, not the whole logical turn. The event sequence contains one
`step_committed` for step 1 and bounded attempts for step 2.

### AC-3 — Tools are not duplicated

A side-effecting fake tool increments a counter and then the provider fails.
Retry/recovery results in one side effect and one replayed result receipt. The
counter does not increment again.

### AC-4 — Terminal error retains state

After retries are exhausted, live `JournaledShortTermMemory`, folded journal,
TUI transcript, and a reopened session all contain the committed step. They
also contain one sanitized failed-turn marker.

### AC-5 — Partial output is truthful

Text emitted before a mid-stream failure is visible as an interrupted partial
fragment, is bounded, is not presented as a completed assistant answer, and
is not sent to the next provider request as malformed provider history.

### AC-6 — New message continuity

Submitting message `D` after the failed turn sends the same session
`conversation_id`; it includes all valid retained context and `D` once. The
previous error does not cause a fresh `ShortTermMemory` to be created.

### AC-7 — Restart recovery

A process terminated after step 1 commit and during step 2 reopens the journal,
rehydrates step 1, identifies step 2 as interrupted, and resumes or reports it
according to the user's choice. It does not roll back to the pre-turn count.

### AC-8 — Workflow recovery

A workflow phase that fails after one or more successful provider steps retains
those messages, phase artifacts, checkpoint state, and shared conversation.
Resuming the workflow does not repeat committed side effects or restart the
phase from a clean conversation.

### AC-9 — Protocol validity

After failures at every scripted boundary, the next provider request passes
the existing tool-history validator. No orphaned tool call, duplicate result,
unknown result ID, or malformed partial tool block is serialized.

### AC-10 — Existing sessions

Fixtures containing the pre-PRD journal format fold without data loss. A
legacy/uncertain status is surfaced where step provenance cannot be inferred;
the implementation never “repairs” the ambiguity by deleting messages.

### AC-11 — Error and cancellation separation

Permanent provider errors, transient exhaustion, explicit cancellation, and
process interruption produce distinct lifecycle markers and UI states. A
handled provider error is not mislabeled as a successful completion.

### AC-12 — No regression in ordinary turns

A successful direct turn, a tool turn, a queued follow-up, compaction, a
workflow phase transition, and a normal `--resume` preserve their current
message ordering, usage accounting, approval behavior, and checkpoint
contracts.

## 13. Test plan

### 13.1 Unit tests

- step checkpoint creation, advancement, and attempt-local rollback;
- rejection of a rollback that crosses a committed step;
- journal encoding/folding for all new records and corrupt trailing lines;
- idempotent duplicate commit and terminal-marker handling;
- legacy journal folding without resets or message loss;
- partial fragment size limits, redaction, and provider-memory exclusion;
- transient/permanent/cancellation error classification;
- tool receipts for success, failure, uncertain outcome, and replay;
- exact user-message-once behavior across retries;
- compaction failure preserving the latest valid cursor;
- TUI projection of committed, interrupted, failed, and cancelled states.

### 13.2 Integration tests

Use a deterministic fake transport and fake tools that can fail at each
boundary:

1. before the first byte;
2. after N text deltas;
3. after a complete assistant tool-call batch;
4. after one tool side effect and before result acknowledgement;
5. after a complete tool-result commit but before the next provider call;
6. during approval, cancellation, compaction, and timeout handling.

For each case assert memory, journal fold, ledger state, transcript events,
provider request messages, tool execution count, and terminal lifecycle.
Also test direct turns, `code_plan`, `create_workflow`, `make_book`, and one
generated workflow using the same shared session conversation.

### 13.3 End-to-end tests

- Start a TUI session, run a scripted two-step tool turn, inject a late
  provider failure, submit a follow-up, and inspect the exact provider
  message history.
- Kill a subprocess after the first step commit, reopen with `--resume`, and
  verify the retained step and recovery choice.
- Run a workflow that fails in a later phase, restart the process, resume the
  workflow, and verify checkpoints, artifacts, conversation, and idempotent
  tools.
- Verify the rendered transcript does not duplicate retry notices or show
  interrupted text as a completed answer.
- Run the provider matrix against a stub, Anthropic-shaped, and
  OpenAI-compatible streaming transport.

### 13.4 Regression tests

Retain explicit cases for:

- whole-turn retry erasing a prior tool exchange;
- `JournaledShortTermMemory.restore()` writing a reset past a committed step;
- a final transient failure being swallowed and the next queued turn starting
  without the failed turn's valid context;
- a process restart after a handled error losing the turn because only
  `turn_completed`/`turn_aborted` were considered;
- partial stream output stranded in the live footer and absent from the
  durable transcript;
- duplicated side effects after retry;
- malformed tool history after cancellation.

## 14. Implementation plan

1. Add fault-injection tests that demonstrate the current loss before changing
   behavior.
2. Implement the provider-step lifecycle and safe checkpoint callback in
   `lauren-ai`'s streaming runner.
3. Add versioned journal records and a fold/recovery projection in agenthicc.
4. Update `JournaledShortTermMemory` and the durable ledger to enforce commit
   ordering and attempt-local rollback.
5. Replace the whole-stream `AgentTurnRunner` retry boundary with the
   step-aware adapter; retain a fail-closed compatibility path.
6. Update TUI/session-service projection and error rendering.
7. Wire direct turns, workflows, subagents, and workflow resume to the same
   logical-turn/step contract.
8. Run unit, integration, E2E, provider-matrix, crash-recovery, and static
   quality gates. Update memory, workflow, architecture, and testing guides.

## 15. Rollout and migration

Use a capability-gated rollout:

1. **Shadow diagnostics:** emit step boundaries and compare folded state with
   the current live projection without changing retry behavior.
2. **Step retry enabled:** disable destructive whole-turn restore for runners
   that advertise the new contract; leave unsupported runners fail-closed.
3. **Durable failure records enabled:** persist partial/failed lifecycle state
   and expose recovery in TUI/session inspection.
4. **Default enforcement:** reject any production runner that attempts a
   whole-turn restore after a committed step.

No existing conversation journal is deleted or rewritten in place. New
records are additive and versioned. On first open, older records are folded
using the existing rules; subsequent writes use the new schema.

## 16. Assumptions and open decisions

- “Preserve all messages” means preserve all valid committed provider
  messages plus a durable, explicitly marked representation of partial
  transcript output. Invalid provider fragments are not injected into future
  provider requests.
- The preferred implementation changes both agenthicc and the installed
  `lauren-ai` dependency because the inner streaming runner owns the only
  reliable provider-step boundary.
- A provider acknowledgement timeout after a side effect is inherently
  uncertain unless the tool has an idempotency/reconciliation API. The system
  must surface that uncertainty rather than claim exactly-once execution.
- The journal remains the only durable conversation authority. Workflow
  checkpoints store cursors and workflow context, not copied conversation
  messages.
- Partial fragment retention limits and privacy redaction should reuse the
  existing session retention/configuration policy; they must be finalized
  before implementation is marked complete.
- The existing `turn_aborted` marker remains meaningful for explicit user
  cancellation. A new failure marker is required so provider errors are not
  confused with intentional cancellation.

## 17. Definition of done

The PRD is complete when the acceptance criteria pass against a clean
checkout and the implementation demonstrates, with fault injection and a
restart test, that a late error cannot remove any provider step already
committed in the same logical turn. The same retained state must be visible to
the TUI, the journal fold, the next user turn, and workflow resume, with no
duplicate side effects or malformed provider history.
