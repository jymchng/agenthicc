---
title: "PRD-241: Deliver Agents-Page Input Through Conflicting Recovery State"
status: Implemented
version: 1.0.0
date: 2026-10-16
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-169, PRD-170, PRD-182, PRD-184, PRD-215, PRD-216, PRD-217, PRD-240]
tags: [agents-manager, background-sessions, input, workflow-recovery, turn-recovery]
---

# PRD-241 — Deliver Agents-Page Input Through Conflicting Recovery State

## 1. Executive summary

When input is sent to a recoverable session from `agenthicc agents`, the
background worker can fail before the target session handles the input:

```text
Latest activity: Direct-turn recovery needs workflow attention
Latest input: not delivered · …
Error: Direct-turn recovery is blocked because workflow recovery records …
Failure category: workflow_recovery_conflict
Exit reason: Direct-turn recovery needs workflow attention
```

The current worker has detected an interrupted conversation turn and a
workflow recovery record. It intentionally refuses to run both recovery paths
at once. However, the refusal is session-wide rather than correlated to the
interrupted turn, happens before the queued input reaches the target router,
and finalizes the attempt as failed. This turns a safety guard into a dead end:
even an explicit target-side `/workflow resume` or `/workflow reset` input
cannot reach the code that handles it.

This PRD defines a recovery-owner decision and input-dispatch contract that
preserves the valid safety boundary without losing or rejecting user input.
The manager must keep forwarding the exact submitted payload to the selected
session. A recoverable workflow checkpoint must not be resumed merely because
it exists. During session recovery, however, Agenthicc should resume the exact
workflow that demonstrably owns an interrupted turn—or a single legacy
checkpoint that exactly matches the session's configured workflow, durable
conversation ID, and original intent—before delivering new queued input. When
ownership remains ambiguous, preserve the message and checkpoint, expose a
recoverable `waiting-for-recovery` condition, and do not label the message “not
delivered” or terminate the session as an irrecoverable failure.

## 2. Incident evidence and diagnosis

### 2.1 Evidence from the reported session

The details view reports session `3c1852fc…15f9`, background run
`run_7b7849b0…da18`, attempt 31, and failure category
`workflow_recovery_conflict`. Its latest input receipt says `not delivered`.

The session's durable event stream records a workflow recovery checkpoint for
run `948c9ee6…d0ef5`, an explicit workflow-resume start and successful resume
projection, and later an `error` followed by `workflow_paused_after_error` for
that workflow run. This establishes that workflow recovery state existed in
the same session. It does not establish the cause of the earlier workflow
error, nor that the new input itself was invalid.

### 2.2 Current code path

1. `agenthicc agents` persists input in `BackgroundInputInbox` and starts or
   resumes a background attempt for the same session.
2. `tui_session._build_session_context()` folds the existing conversation
   journal. If `RunCoordinator.detect_incomplete_turn()` finds an unfinished
   direct turn, it constructs `SessionContext.pending_resume` with the original
   turn ID and idempotency ledger.
3. In `background.worker.run_worker()`, the `pending_resume` branch runs before
   the ordinary deferred-input drain.
4. Before replaying that turn, `_direct_resume_workflow_conflict()` checks
   kernel workflow state and calls `WorkflowRecoveryCoordinator.inspect()`.
   It refuses recovery if it finds any non-terminal in-memory workflow or any
   non-terminal record returned by `inspect()` (including invalid recovery
   diagnostics). It does not prove that the record owns the specific
   interrupted journal turn.
5. On conflict, the worker returns `SessionStatus.FAILED` with
   `failure_category="workflow_recovery_conflict"`. This return occurs before
   `_drain_idle_inputs()`, before `/workflow ...` reaches
   `_handle_background_workflow_command()`, and before any user-message
   delivery boundary is recorded.
6. The session therefore finishes the attempt without handling the submitted
   payload. The UI reports `not delivered` for the observed receipt.

The error is not evidence of an OpenCode header failure, invalid command
syntax, or a rejected message body. It is a conflict between two recovery
signals and a dispatch-order bug: a broad preflight check blocks target input
before the target can act on it.

### 2.3 Why the current guard exists—and what is wrong with it

The guard was added to PRD-240 to avoid replaying an interrupted direct turn
concurrently with a workflow checkpoint that might own the same model/tool
transaction. That safety goal is valid: neither recovery path may duplicate
tool side effects or create an invalid assistant/tool exchange.

The current implementation is too coarse in three ways:

- **No ownership correlation:** any workflow recovery record in the session
  blocks any pending direct-turn plan, even when the checkpoint is unrelated
  or when the explicit input is itself the command that would resolve it.
- **Dispatch is preempted:** the conflict check runs before the session-owned
  input router, contrary to PRD-217's exact-payload pass-through contract.
- **A recoverable conflict becomes a terminal failure:** the worker exits as
  `FAILED` instead of retaining a recoverable state and the accepted input.

## 3. User story and desired behavior

> As an operator, I can send any text or command to a selected session from
> `agenthicc agents`. Agenthicc forwards the exact payload to that session;
> the target handles it. If target execution must wait for safe recovery, the
> input and checkpoint remain intact and the UI explains the actionable
> recovery state instead of claiming the input was delivered or discarding it.

Required behavior:

- Never rewrite, prefix, summarize, or reinterpret the submitted payload in
  the manager or worker transport.
- Do not select or discard a workflow merely because its checkpoint exists.
  Resume only a run linked to the interrupted turn, or one uniquely matching
  the session's configured workflow, conversation ID, and original intent.
  This restores the session; it does not reinterpret newly submitted text.
- Do not send a new model turn across an incomplete provider/tool transaction
  until the canonical recovery logic establishes a safe conversation
  boundary.
- Do not let a session-wide workflow-record check prevent safe target-side
  command handling, including explicit `/workflow resume` and
  `/workflow reset`.
- If safe model execution is not yet possible, keep the input durably pending
  and actionable. Do not turn this condition into an irrecoverable session
  failure or tell the user to resend an input that is already stored.

## 4. Goals and non-goals

### Goals

1. Resolve direct-turn versus workflow-checkpoint ownership using durable,
   per-run/per-turn evidence rather than session-wide presence checks.
2. Route all agents-page input through the target session's canonical input
   dispatcher before deciding that no input can be handled.
3. Preserve PRD-169/182 journal integrity, original turn IDs, and tool
   idempotency during direct-turn replay.
4. Preserve PRD-217's opaque pass-through semantics while restoring a proven
   interrupted workflow before delivering later input.
5. Keep accepted input, workflow checkpoints, and recovery state durable and
   observable across worker/process restarts.
6. Give users an accurate, actionable receipt for waiting, handled, delivered,
   failed, or completed input.

### Non-goals

- Select a workflow based on the contents or apparent intent of a new message.
- Rewrite an ordinary message as “continue,” a workflow prompt, or the prior
  launch intent.
- Delete, invalidate, or silently replace a workflow checkpoint to make direct
  turn recovery easier.
- Change foreground workflow semantics, provider retry policy, or general
  workflow checkpoint topology.
- Claim that an input has reached the LLM merely because it was accepted into
  the durable inbox.

## 5. Recovery ownership and input flow

The implementation should replace the current Boolean conflict test with an
explicit recovery assessment. The assessment identifies:

```text
session ID
pending direct-turn ID and resume-ledger identity, if any
workflow run IDs and recovery status
workflow/turn correlation, when available
current target-input operation
selected recovery owner: input router | direct turn | workflow | unresolved
```

Expected dispatch:

```text
accepted exact payload
        │
        ▼
target-session input dispatcher
        │
        ├─ safe target-side command/trigger ──> handle unchanged; record receipt
        │
        ├─ direct turn with no workflow owner ─> resume original turn safely,
        │                                         then deliver queued input
        │
        ├─ explicit workflow recovery command ─> target workflow handler owns
        │                                         that requested operation
        │
        └─ ambiguous unsafe provider boundary ─> persist pending input and
                                                  show recovery-required state
```

The dispatcher may classify input only as part of the target session's normal
command/trigger routing. The agents manager must not parse commands to decide
their meaning. In particular, `/workflow ...` is forwarded unchanged and the
target's existing router owns its interpretation.

### Recovery decision rules

1. **No pending direct turn and no workflow operation requested:** deliver the
   input using existing PRD-215/216/217 behavior. A checkpoint's existence
   alone does not select or resume it.
2. **Pending direct turn with no workflow ownership evidence:** re-drive the
   original turn with its original turn ID and durable idempotency ledger;
   deliver accepted user input only at a valid boundary, preserving FIFO and
   exactly-once tool effects.
3. **Input is an explicit target-side recovery command:** allow the normal
   target command handler to receive it unchanged even when a direct-turn plan
   and workflow checkpoint coexist. The command's existing validation, claim,
   resume, or reset behavior remains authoritative. For a legacy interrupted
   turn with no execution provenance, an explicit `/workflow resume [run-id]`
   is the user's recovery-owner choice; the selected run ID is durably attached
   when the original logical turn is re-driven. A turn already marked direct,
   or linked to another workflow run, is never reassigned this way.
4. **A workflow checkpoint is proven to own the interrupted turn:** do not
   independently run the same turn as a direct `ResumePlan`. Resume the exact
   run through the target's canonical workflow recovery handler before later
   queued input is delivered. For legacy turns without a run link, automatic
   selection is permitted only when exactly one recoverable checkpoint matches
   the session's configured workflow, conversation ID, and original intent.
   This restores the existing session; it does not interpret the incoming
   message.
5. **Ownership is ambiguous or checkpoint data is invalid:** do not issue a
   provider request, mutate/discard the checkpoint, or mark the inbox item
   delivered. Persist a recoverable waiting state with the relevant run ID(s)
   and exact next action. A subsequent explicit target-side command or
   operator recovery action must be able to resolve it without resending the
   original payload.
6. **Multiple queued messages:** preserve message IDs and payloads. Recovery
   control commands needed to unlock a blocked FIFO head may be handled by the
   target command router without appending/reordering ordinary user turns;
   ordinary messages must retain their accepted order at the conversation
   boundary. Every such control-path decision must be visible in receipts.

The implementation must specify and test how rule 6 avoids deadlock without
allowing a later ordinary message to overtake an earlier one. It must not
silently prioritize or drop arbitrary messages.

## 6. Functional requirements

### FR-1 — Durable recovery assessment

Build a typed assessment from the journal's pending `ResumePlan`, live kernel
workflow projection, and `WorkflowRecoveryCoordinator` records. Include
validated workflow run IDs and available checkpoint provenance. Do not infer
ownership from “there is at least one record.” New turn-start journal entries
use schema version 3 and record `execution_kind=direct` or
`execution_kind=workflow`; workflow entries also record `workflow_run_id`.
Readers continue to accept schema-v1/v2 entries. A legacy turn with no
provenance and coexisting workflow state is unresolved until the user selects
a recovery action.

### FR-2 — Target-owned input routing

Share the target's command/trigger dispatch primitives so the background
worker does not maintain a separate pre-routing veto that bypasses the target
router. Preserve the exact input bytes/characters accepted by the composer
according to PRD-217, including its rule that ordinary agents-page text does
not select or start a workflow based on the message's contents. Session
recovery may independently resume the unique existing workflow that owns an
interrupted turn. The manager never creates a synthetic continuation message.

### FR-3 — Explicit workflow commands remain reachable

Inputs such as `/workflow resume`, `/workflow resume <run-id>`, and
`/workflow reset <run-id>` must reach the selected target session unchanged
even when both recovery signals are present. The target's canonical command
handler determines whether the action is valid. A manager-side command error
or recovery preflight must not replace the user's command with a different
intent.

### FR-4 — Correct direct-turn replay

When a direct turn is the validated recovery owner, replay its original user
message with its original turn ID and `DurableIdempotencyLedger`. Do not
duplicate completed tool effects. Do not append the newly accepted input
before the recovered turn reaches the safe boundary required by PRD-169.

### FR-5 — Restore the proven workflow owner before input delivery

An ordinary input does not choose a workflow or become a synthetic
`/workflow resume` command. If durable turn provenance identifies the owning
workflow, resume that exact run before delivery. For a legacy turn without
provenance, auto-resume only one recoverable run whose workflow name,
conversation ID, and original intent exactly match the persisted session.
Preserve FIFO input and stable IDs during recovery. If no unique safe owner
exists, keep input pending and expose an explicit recovery action rather than
guessing.

### FR-6 — Recoverable conflict lifecycle

A recovery conflict by itself must not finalize the background session as an
irrecoverable `FAILED` attempt. Use the existing recoverable/orphaned lifecycle
or an additive explicit waiting-for-recovery projection. Preserve the accepted
input and checkpoint. The user must be able to submit the explicit resolving
command through `agenthicc agents` without attaching to the session.

### FR-7 — Accurate input receipt

Distinguish at minimum:

- accepted and waiting for a worker;
- waiting for recovery/target action;
- delivered to the target input boundary;
- handled by a target command/trigger;
- completed successfully;
- failed after delivery; and
- rejected before acceptance.

An input blocked before target dispatch must never display `delivered`,
`processed`, or generic `not delivered` with a resend recommendation if the
original payload remains durably stored. Expose a bounded recovery reason and
run ID without exposing the message body in session-list output.

### FR-8 — Retry and restart idempotency

Reopening details, retrying a worker, or restarting the manager must not
duplicate a target message, create a new input ID for an idempotent retry,
replay an already-completed tool effect, or consume an input whose receipt
already reached a terminal state. Recovery and receipt transitions must be
owner/attempt fenced.

### FR-9 — Preserve prior workflow state

Workflow checkpoints, phase history, approval state, summaries, claims, and
artifact references remain unchanged unless the target explicitly resumes or
resets the identified run. An unresolved direct-turn marker must not silently
rewind the workflow phase or create a new workflow run.

### FR-10 — Actionable diagnostics

Show the recovery owner decision, relevant run ID(s), input receipt state,
and next safe action in the details view and durable attempt history. Keep
diagnostics bounded and redacted; never include raw message bodies, provider
credentials, or full serialized conversations.

## 7. Persistence and compatibility

- Use existing session journals, workflow checkpoints, inbox event logs, and
  ownership leases as the durability authorities; do not create a competing
  transient-only state machine.
- If a turn-to-workflow correlation field or new inbox receipt state is needed,
  version it and support older event records. Unknown historical ownership
  must resolve to a safe, visible pending state—not a guessed owner.
- Keep session ID, conversation ID, workflow run ID, worker attempt ID, and
  inbox message ID distinct. In particular, a workflow run ID is not a
  conversation turn ID.
- PRD-215/216/217 remain authoritative for eligibility, exact payload
  forwarding, and no implicit workflow selection. PRD-169/182 remain
  authoritative for transaction-safe conversation history and interrupted
  turn replay. PRD-170/173/184 remain authoritative for checkpoint ownership,
  validation, and resumable workflow failures. This PRD resolves their
  background-input interaction; it does not weaken their invariants.
- PRD-240's direct-turn recovery remains valid where direct-turn recovery is
  the proven owner. Its unconditional session-wide workflow conflict behavior
  is superseded by this PRD once implemented.

## 8. Acceptance criteria

1. A regression fixture with both an incomplete journal turn and a valid
   workflow checkpoint reproduces the present `workflow_recovery_conflict`
   path before the fix.
2. The same fixture accepts a new input without failing the session or losing
   the checkpoint. Its receipt accurately remains pending until the target
   handles it.
3. An explicit `/workflow resume <run-id>` submitted from the details page is
   delivered unchanged and reaches the target's normal workflow command
   handler even when the dual-recovery condition exists.
4. An explicit `/workflow reset <run-id>` is similarly target-routed and only
   discards the selected checkpoint through the existing guarded path.
5. An ordinary message does not select, reset, or create a workflow. When one
   exact workflow owner is established from durable turn/checkpoint identity,
   Agenthicc resumes that run first and then delivers the unchanged message. If
   ownership is ambiguous, the message remains durable until explicit
   resolution.
6. A direct-turn-only recovery reuses the original turn ID/ledger, completes
   before the new ordinary message is appended, and does not repeat completed
   tool effects.
7. A workflow-owned interrupted turn is not independently replayed by the
   direct-turn runner.
8. An invalid or ambiguous recovery association blocks provider execution but
   does not falsely mark an accepted message delivered, reject it with a
   resend prompt, or permanently fail the session.
9. Two or more inputs, including a recovery command behind a blocked ordinary
   message, cannot deadlock; ordinary user-turn order remains deterministic
   and every message has a durable, inspectable receipt.
10. Process interruption after input acceptance, during recovery assessment,
    during explicit workflow recovery, and after target dispatch preserves
    idempotent retry behavior.
11. No change to the selected session's owner, workspace, security policy, or
    conversation identity is introduced by retrying the input.
12. Details-page diagnostics distinguish `waiting for recovery` from `not
    delivered` and show the exact safe action without revealing input content.
13. A legacy interrupted turn with exactly one recoverable checkpoint matching
    the session workflow, conversation ID, and original intent automatically
    resumes that checkpoint; queued input is delivered afterward, unchanged.
14. Multiple matching checkpoints, an invalid matching checkpoint, or an
    identity mismatch never triggers automatic selection or an automatic
    maintenance retry loop.

## 9. Test plan

### Unit tests

- Recovery-assessment decision table: direct only, workflow only, unrelated
  checkpoint, matching workflow-owned turn, multiple checkpoints, malformed
  checkpoint, and unknown historical linkage.
- Input dispatch ordering for ordinary text, slash commands, skills, and
  explicit workflow recovery commands.
- Receipt state transitions at acceptance, hold, dispatch, completion,
  rejection, worker crash, and retry.
- Verify no recovery conflict returns `FAILED` when accepted input and
  recoverable checkpoint state can be preserved.
- Verify exact payload and stable message ID are retained; no synthetic
  continuation or duplicate user message is created.
- Verify direct-turn replay uses the original turn ID/ledger and workflow-owned
  recovery is never sent through the direct-turn resume path.

### Integration tests

- Disk-backed `BackgroundInputInbox` + `BackgroundStore` + workflow checkpoint
  + conversation journal across process-style worker reconstruction.
- Send a target-side `/workflow resume <id>` from the agents input API with
  both recovery signals present; assert target handling and correct claims.
- Send ordinary text in the same state; assert a uniquely correlated workflow
  is resumed before the exact text is delivered, with no transcript-order
  violation.
- Simulate worker exit at each receipt boundary; restart and verify there is
  neither message loss nor duplicate tool execution.
- Test FIFO and recovery-command unblocking with multiple queued messages.

### End-to-end tests

- Reproduce the reported sequence: recoverable goal workflow, interrupted
  model turn, new agents-page input, and the current conflict failure.
- Verify a uniquely correlated workflow is recovered automatically and its
  pending input handled exactly once; verify an ambiguous workflow can still
  be explicitly resolved through `agenthicc agents` without attaching.
- Verify a direct-only interrupted session still resumes and then processes
  the new input.
- Verify status, latest activity, failure category, and input receipt remain
  consistent after restart and after a subsequent attempt.

All tests must use fake providers and temporary stores; no live credentials or
network requests are required.

## 10. Rollout and operational safeguards

- Ship additive receipt/correlation metadata with backward-compatible readers.
- Existing pending inputs must remain visible and must not be re-enqueued with
  new IDs during migration.
- When the worker encounters old records without enough provenance, resume
  automatically only when one exact checkpoint matches the configured
  workflow, conversation ID, and original intent. Otherwise retain the
  checkpoint and input, show `waiting for recovery`, and support an explicit
  target-side recovery command. Never guess between candidates.
- During rollout, log only bounded event IDs, run IDs, turn IDs, recovery
  decision codes, and receipt states; redact payload and provider content.
- Do not raise worker retry counts to address this deterministic local
  conflict. Retries are useful only after a recovery owner or state transition
  changes.

## 11. Definition of done

- The reported failure is reproduced and traced to the current pre-dispatch
  guard using unit and E2E evidence.
- A typed recovery assessment selects one safe owner or a visible recoverable
  waiting state; it does not run direct-turn and workflow recovery
  concurrently.
- A uniquely correlated workflow checkpoint is rehydrated before pending input
  is delivered; truly ambiguous state remains actionable and is retried only
  after a new user or operator action.
- Agents-page input reaches the target router unchanged whenever target-side
  handling is safe, including explicit workflow recovery commands.
- Ambiguous state preserves the original input and checkpoint without
  terminal failure, false delivery, duplicate execution, or resend advice for
  an already-persisted payload.
- All acceptance criteria pass, with unit, integration, and E2E regression
  coverage.
- The background-session and workflow-recovery guides accurately describe
  receipt states, uniquely correlated automatic recovery, and explicit
  resolution for ambiguous state.

## 12. Implementation and verification record

Implemented in the session journal, run coordinator, workflow execution
context, background input inbox/worker, agents details projection, and related
tests. New journal turn markers identify direct versus workflow execution;
workflow markers include the workflow run ID. The worker correlates those
markers with recoverable checkpoint records, directly replays only a proven
direct turn, resumes a uniquely identified workflow owner, and leaves truly
ambiguous inputs accepted with a bounded recovery reason rather than failing
the session. Input-driven restarts use an `input:<message-id>` attempt marker
instead of generic launch-intent resume, and a legacy waiting-recovery receipt
is retried once after upgrade. Re-arming clears the old wait reason only when a
replacement attempt is scheduled; a new unresolved attempt writes a fresh
reason, preventing maintenance retry loops. Target-side `/workflow resume` and
`/workflow reset` messages can be routed by stable inbox ID even when an
ordinary message is ahead of them; that ordinary message remains pending and
is not appended out of order. An explicit resume can claim a legacy unlinked
turn as its selected workflow's recovery only when the turn has no contradictory
direct or different-run provenance.

Verification coverage includes the ownership decision table, journal schema
compatibility and provenance retention, durable inbox wait/rebind/claim
behavior, target command routing with a blocked FIFO head, explicit workflow
resume using the original turn ledger, direct-turn recovery regression, and
the agents details receipt projection. The follow-up regression reproduces the
reported orphaned `waiting for recovery` screen and verifies the worker restores
the unique matching `goal_flow` checkpoint before forwarding its queued input.
Input-driven retries now set an `input:<message-id>` marker, so a process crash
after acceptance is recoverable. A legacy waiting-recovery record receives one
upgrade retry; if it remains ambiguous, its durable recovery reason prevents a
maintenance retry loop until a new input or explicit action arrives.

Verification completed on 2026-10-16:

- `uv run pytest tests/ -q` — **4317 passed, 16 skipped**.
- Focused PRD-241 regression set — **112 passed**.
- `uv run ruff check src/ tests/ scripts/` — passed.
- `uv run ruff format --check` on all changed Python files — passed.
- `uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json` — passed.
- `uv run mypy` on the changed recovery modules — passed (4 files).
- `uv run mkdocs build --strict` — passed.
- `git diff --check` — passed.

Repository-wide `uv run ruff format --check src/ tests/ scripts/` still reports
eight untouched files (`cli/commands/mcp.py`, `cli/mcp_config.py`,
`tools/mcp.py`, `workflows/copy_website/runner.py`,
`workflows/make_book/build_book.py`, `tests/e2e/test_mcp_session_lifecycle_e2e.py`,
`tests/integration/test_mcp_manager_integration.py`, and
`tests/unit/test_mcp_config.py`). Repository-wide `uv run mypy
src/agenthicc` also reports existing issues in 11 modules outside this change;
the changed modules type-check cleanly. These unrelated baseline issues were
left untouched.
