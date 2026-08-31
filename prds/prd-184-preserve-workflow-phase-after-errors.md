---
title: "PRD-184: Preserve the active workflow phase after transient errors"
status: Implemented
version: 1.0.0
date: 2026-08-31
scope: "workflow error recovery, phase cursors, checkpoint reconciliation, and continue/resume dispatch"
related_prds:
  - PRD-148
  - PRD-156
  - PRD-169
  - PRD-170
  - PRD-173
  - PRD-177
  - PRD-178
  - PRD-182
  - PRD-183
tags:
  - workflows
  - error-recovery
  - rate-limits
  - checkpoints
  - resume
  - phase-cursor
  - reconstruct_site
---

# PRD-184 — Preserve the active workflow phase after transient errors

## 1. Executive summary

When a provider returns a recoverable error in the middle of a workflow, such
as HTTP 429 `RateLimitError`, the next `continue` or resume operation must
continue the same workflow run at its last safe phase. It must not create a
new run, generate a new evidence manifest, call `run(intent)` again, or inject
the `INIT` prompt merely because the process or agent turn was interrupted.

The reported symptom is:

```text
ERROR TransientTransportError: Error code: 429 - {'error': 'Rate limit exceeded'}

continue

The current phase instructions point to ARCHITECTURE ...
The init -> research_gate pipeline already ran ...
I'm in ARCHITECTURE for this run.
```

In other occurrences of the same defect, the resumed UI starts at `INIT` even
though the persisted manifest, phase receipts, journal, and the model's
conversation indicate that several later phases completed. The contradictory
signals are evidence that different layers are selecting different workflow
identities or different phase cursors. They are not evidence that the agent
forgot the work.

This PRD defines an investigation and implementation plan for one authoritative
workflow resume decision. It extends the recovery and mid-turn durability
contracts from PRD-170, PRD-173, and PRD-182. It does not replace those
contracts, create a second conversation store, or make every transient provider
error automatically retry forever.

The central invariant is:

> A recoverable workflow error resumes the existing `(session_id, run_id)` at
> the latest validated safe phase boundary. Only an explicit reset or a new
> user-selected workflow run may start at `INIT`.

## 2. Problem statement

### 2.1 User-visible failure

The user runs a multi-phase workflow. Earlier phases complete and write
artifacts. A later phase is active when the provider responds with a transient
transport error, for example:

```text
ERROR TransientTransportError: Error code: 429 -
{'error': 'Rate limit exceeded'} | provider='openai' |
status_code=429 | caused by: RateLimitError(...)
```

The application returns to an idle or error state. After the user enters
`continue`, one of the following happens:

1. the workflow prompt is injected again for `INIT`;
2. an earlier research phase is reopened even though its receipt is durable;
3. the TUI transcript says the workflow is in a later phase while the runner
   creates a new context in `INIT`;
4. a new workflow run ID or evidence manifest ID is created; or
5. the agent asks a question that reveals that it has received a fresh or
   contradictory state projection.

The example where the assistant correctly says it is in `ARCHITECTURE` is also
important. It demonstrates that the conversation summary and evidence can
contain the correct later state while the runtime still needs to prove that it
is resuming the same durable run. A plausible model response cannot be used as
the phase state source of truth.

### 2.2 Why a 429 exposes the defect

A 429 is normally recoverable: the user can wait, change provider settings, or
retry later. It creates a path through all the difficult boundaries at once:

```text
provider request
  -> transient exception
  -> agent-turn cleanup
  -> workflow failure finalizer
  -> checkpoint write
  -> claim release
  -> process/TUI continuation selection
  -> runner.resume(context)
```

If any boundary loses the active phase, the next invocation can take the
normal new-run path. A new-run path deliberately initializes its context with
`INIT`, so the visible restart is a deterministic consequence of selecting the
wrong execution path rather than an LLM reasoning failure.

### 2.3 Scope

This PRD covers:

- transient provider, rate-limit, timeout, tool, and ordinary phase errors;
- TUI `continue`, `/workflow resume`, session selection, `--continue`, and
  `--resume` workflow dispatch;
- workflow run identity and phase-cursor persistence;
- checkpoint, phase receipt, evidence manifest, journal, and kernel projection
  reconciliation;
- same-process and process-restart recovery;
- `reconstruct_site`, `code_plan`, built-in workflows, and workflows generated
  by `create_workflow`; and
- observability and deterministic test coverage for phase-rewind regressions.

It does not change the meaning of a deliberately invalidated artifact. If an
integrity check proves that a later phase depends on corrupt or stale evidence,
the workflow may intentionally rewind to the narrowest affected recovery
phase. Such a rewind must be explicit, durably recorded, and must never be
mistaken for a generic restart at `INIT`.

## 3. Investigation findings and current dataflow

The investigation examines the current ownership boundaries in:

- `src/agenthicc/runners/tui_session.py`;
- `src/agenthicc/runners/headless.py`;
- `src/agenthicc/runners/workflow_handle.py`;
- `src/agenthicc/runners/workflow_recovery.py`;
- `src/agenthicc/runners/workflow_checkpoint_store.py`;
- `src/agenthicc/workflows/checkpoint.py`;
- `src/agenthicc/workflows/reconstruct_site/runner.py`;
- `src/agenthicc/workflows/code_plan/runner.py`;
- the generic workflow runner and plugin contracts; and
- the session journal, kernel projection, and resume tests.

### 3.1 Confirmed execution paths

The current implementation already has a durable workflow handle and typed
checkpoint model. The following paths are distinct:

| Path | Current behaviour | Risk exposed by a 429 |
|---|---|---|
| New TUI workflow message | creates a new `WorkflowRunHandle` with a new UUID, initializes the first phase, and calls `runner.run(intent)` | correct for a new run, incorrect if recovery discovery failed or the paused handle was detached |
| Same-process ordinary `continue` | `_start_workflow_continuation()` can use the attached paused handle and dispatch `_resume_workflow_task()` | correct only while the exact paused handle remains attached and classified as recoverable |
| Explicit `/workflow resume` | finds or rehydrates a recovery record, claims it, then calls `runner.resume(context)` | safe in principle, but selection and rehydration must be authoritative at execution time |
| TUI process restart | startup discovers records and may attach one paused handle without claiming it | multiple, invalid, stale, or mismatched records can prevent attachment; a later ordinary message can then look like a new run |
| Headless workflow execution | `execute_workflow()` currently creates a new `WorkflowRunHandle` and calls `runner.run(intent)` for the supplied intent | `--continue` may restore the session but still start a new workflow run instead of resuming the saved workflow run |
| `reconstruct_site.resume()` | restores typed context, rehydrates evidence, reconciles the cursor, then enters the phase loop | reconciliation must not be bypassed by a fresh `run()` call or allowed to treat a different manifest as the same run |

The headless distinction is especially important: restoring a session
conversation is not the same as restoring a workflow run. The former preserves
messages; the latter preserves phase state, run identity, artifacts, claims,
and the exact runner resume method.

### 3.2 Current checkpoint state

The workflow handle persists fields including:

- `run_id` and workflow name;
- session conversation identity;
- typed context and `context_ready`;
- `current_phase` and `phase_index`;
- phase iteration and checkpoint revision;
- lifecycle (`running`, `paused`, `resuming`, and terminal states);
- failure kind/message and last safe boundary;
- provider profile and workspace identity; and
- redacted cache metadata.

The recovery coordinator validates the plugin fingerprint, context codec,
conversation cursor, provider profile, workspace, and tool-tail state before
rehydration. The checkpoint store writes checkpoint bytes atomically and keeps
a separate claim file for one live owner.

These are necessary foundations, but they do not by themselves guarantee that
every entry point uses the checkpoint. A valid checkpoint can be ignored if a
caller decides to create a new handle before consulting recovery, and a
conversation can contain correct later text while the runner receives a fresh
typed context.

### 3.3 Likely failure mechanisms to verify

The implementation phase must prove or reject each mechanism with tracing and
regression tests:

1. **Fresh-run fallback after failed recovery discovery.** A paused checkpoint
   may be excluded because it is marked diagnostic-only, has `context_ready` as
   false, has a plugin/profile/workspace mismatch, or has a corrupt companion
   record. If the caller then accepts an ordinary message as a new workflow
   intent, it creates a new run at `INIT` instead of reporting that recovery is
   unavailable.
2. **Session continuation is confused with workflow continuation.** A restored
   `conversation_id` and transcript do not prove that the workflow handle and
   typed context were restored. `--continue` must select a workflow run, not
   merely the latest session.
3. **Headless path bypasses workflow recovery.** The current headless workflow
   executor constructs a fresh UUID and calls `runner.run(intent)`. This can
   happen even when `--continue` selected an existing durable session.
4. **Stale phase cursor at failure finalization.** The active runner may know its
   phase in typed context while the handle still contains the first phase, or
   the finalizer may capture the handle before the runner attaches its latest
   context. The failure checkpoint then truthfully persists stale data.
5. **Transition/checkpoint ordering window.** A phase transition may update
   typed state, evidence, or the journal in one order and update the workflow
   checkpoint in another. A crash between those writes can leave a later
   receipt with an earlier checkpoint cursor, or a later checkpoint with
   evidence that is not yet durable.
6. **Manifest/run identity split.** The reported IDs show that a workflow may
   be reading one evidence manifest while a new run or new manifest is being
   initialized. The implementation must determine whether the IDs represent
   separate valid runs, a stale output directory, or a run-to-manifest
   association that is not persisted.
7. **Reconciliation is too eager or too weak.** Recovery can either rewind to
   `INIT` because it trusts a default enum, or jump forward from untrusted
   artifacts. Reconciliation must use only same-run, integrity-checked,
   contiguous evidence and must preserve a valid current phase when the error
   occurred inside that phase.
8. **Duplicate error finalization.** Both the workflow runner and TUI wrapper
   can observe the same provider error. If their writes race or the later
   finalizer replaces a precise checkpoint with a generic one, the phase cursor
   can regress.
9. **Resume prompt is mistaken for phase execution.** A resume marker added to
   the shared conversation is expected. It must not be accompanied by the
   phase's initial prompt unless the restored state says that phase is active.

The investigation report must record which mechanisms reproduce, which are
already prevented by existing code, and which are ruled out. It must include
the run ID, checkpoint revision, phase, phase index, context state, manifest
ID/revision, receipt prefix, journal cursor, and dispatch path for every
reproduction, with prompt contents and credentials redacted.

## 4. Definitions and invariants

### 4.1 Execution identities

The following identities are different and must not be substituted for one
another:

| Identity | Meaning |
|---|---|
| `session_id` / `conversation_id` | the durable conversation and journal that contain the shared user/LLM history |
| `run_id` | one workflow execution, including its phase state, lifecycle, and checkpoint revisions |
| `workflow_name` | the plugin topology selected for the run |
| `manifest_id` | the evidence/output ledger for the run or target workspace |
| `checkpoint_revision` | monotonic version of the durable workflow cursor and context |
| `conversation_cursor` | position in the shared conversation used by the checkpoint |
| `phase_receipt` | integrity-checked evidence that one phase completed |

The same session can contain multiple workflow runs. The same workflow name can
have multiple runs. A transcript alone cannot select between them.

### 4.2 Safe resume semantics

For a phase `P`:

- an error before `P` begins resumes at the prior committed boundary;
- an error during `P`, before its transition tool commits, resumes `P` with
  the current typed context and committed provider/tool history;
- a successful transition from `P` to `Q` commits `Q` as the next phase before
  `Q` is invoked, so an error after that boundary resumes at `Q`;
- a completed phase is not re-run merely because its provider turn failed in a
  later phase;
- a deliberately invalidated artifact may request a bounded rewind, such as
  `architecture` to `visual_research`, but must not default to `INIT`; and
- only an explicit reset, a new run command, or a validated incompatible
  checkpoint may prevent exact resume.

### 4.3 Anti-rewind invariant

For one `run_id`, a durable checkpoint revision may not move to an earlier
phase index unless the checkpoint contains a structured invalidation reason,
the affected artifact IDs/kinds, the target recovery phase, and an audit link
to the evidence integrity decision. Generic error handling must never write an
earlier phase than the latest safe cursor.

## 5. Goals

1. Resume the same workflow run after a recoverable 429 or other transient
   error.
2. Preserve the latest validated phase, typed context, phase iteration,
   artifacts, evidence manifest, and committed conversation/tool history.
3. Make `continue`, `/workflow resume`, `--continue`, and `--resume` use one
   recovery selection and dispatch contract.
4. Guarantee that an existing recoverable run never reaches the new-run path
   accidentally.
5. Make cursor reconciliation deterministic when checkpoint, receipts, journal,
   manifest, and kernel projection differ.
6. Keep failure finalization idempotent and monotonic.
7. Give users an actionable explanation when recovery is unavailable, rather
   than silently starting at `INIT`.
8. Make the guarantee automatic for built-in and `create_workflow` workflows.
9. Preserve provider cache stability and shared session conversation identity
   while adding bounded resume metadata.

## 6. Non-goals

- Removing legitimate workflow phases or changing their business semantics.
- Treating the LLM's summary as authoritative workflow state.
- Replaying every provider request from the beginning of a turn.
- Retrying a 429 forever or bypassing provider rate limits.
- Automatically deleting or overwriting output artifacts to make state appear
  consistent.
- Using the presence of a file without an integrity receipt as proof that a
  phase completed.
- Adding a second conversation or checkpoint store.
- Silently taking a live workflow claim from another process.

## 7. Proposed solution

### 7.1 One `WorkflowResumeCoordinator`

Introduce or consolidate a session-independent coordinator responsible for:

1. resolving an explicit `run_id`, session `--continue`, or a picker choice;
2. loading the latest checkpoint after selection, not trusting a stale list
   snapshot;
3. validating the workflow plugin, context codec, conversation identity,
   workspace/profile, evidence identity, and tool-tail state;
4. reconciling the phase cursor before any phase prompt or provider request;
5. claiming the exact run atomically;
6. building the workflow configuration with the restored handle and shared
   conversation; and
7. invoking only `runner.resume(restored_context)` for an existing run.

The coordinator must be used by the TUI, headless runner, session picker,
background worker, and any future client-neutral session service. The caller
may provide a new user continuation message, but that message is a resume
instruction, not a new workflow intent.

### 7.2 Separate selection from dispatch

The selection result must carry an explicit disposition:

```text
NewWorkflow(intent)
ResumeWorkflow(
    session_id,
    run_id,
    workflow_name,
    checkpoint_revision,
    restored_context,
    current_phase,
)
RecoveryUnavailable(run_id?, reason, diagnostic_id?)
AmbiguousRecovery(candidate_run_ids)
```

No code may infer `NewWorkflow` merely because a paused handle is not attached
in memory. The coordinator must refresh durable recovery records first. If a
recoverable run exists but cannot be rehydrated, it must report the exact
reason and require `/workflow reset` or an explicit new-run action.

### 7.3 Stable run identity through failure and resume

At workflow bootstrap, persist one run identity before the first failure-prone
provider/tool operation. On failure:

```text
same session_id + same run_id
  -> capture execution snapshot
  -> classify and redact transient error
  -> persist paused checkpoint at snapshot phase
  -> persist error revision and safe boundary
  -> emit one failure event
  -> release claim
  -> user invokes continue/resume
  -> reload latest checkpoint
  -> reconcile same-run evidence and journal
  -> claim same run_id
  -> runner.resume(restored_context)
```

The failure path must not create a replacement run ID. A new manifest is also
forbidden unless the workflow explicitly starts a new run after the user
chooses reset/new run.

### 7.4 Capture an execution snapshot before finalization

The workflow handle must expose an atomic or lock-protected snapshot operation
that captures, together:

- `run_id`, workflow name, and conversation ID;
- typed context reference or serialized context;
- current phase and phase index;
- phase attempt/iteration;
- last committed transition and last safe boundary;
- checkpoint revision and conversation cursor;
- evidence manifest ID/revision and phase receipt prefix;
- active provider step/turn ID, if any; and
- failure classification.

The finalizer must use this snapshot rather than reconstructing the phase from
the initial workflow definition. Runner-side context attachment must happen
before every phase/provider operation and before the finalizer can observe
failure.

### 7.5 Monotonic checkpoint commits

Adopt a commit protocol for phase transitions:

```text
phase P is active
  -> provider/tool work commits to shared conversation journal
  -> transition tool validates the requested edge
  -> phase output/evidence receipt is written atomically
  -> typed context is set to Q
  -> checkpoint revision N+1 records Q and manifest revision
  -> only then may phase Q begin
```

For a failure inside `P`, the finalizer writes revision `N+1` with `P` and
`failure_kind=provider_transient` (or the applicable category). It must not
write the bootstrap phase because that is the initial default, not the active
cursor.

Checkpoint saves need compare-and-swap semantics or an equivalent owner lock:

- a write with an older revision is rejected;
- a write with the same revision and same digest is idempotent;
- a write with the same revision but different cursor/context is a conflict;
- a second finalizer observes the already committed disposition and does not
  replace it; and
- a stale cleanup path cannot overwrite a newer checkpoint.

### 7.6 Deterministic cursor reconciliation

Create one reusable reconciler with these inputs:

```text
checkpoint cursor
typed context state
phase receipts for this run and manifest
journal phase-boundary events for this run
evidence manifest revision and integrity status
kernel workflow projection
```

Resolution rules:

1. reject records whose `run_id`, session, workflow fingerprint, or manifest
   association does not match;
2. prefer the newest valid checkpoint revision as the execution cursor;
3. permit a later phase only when a contiguous, integrity-checked receipt or
   journal boundary proves the transition and the corresponding checkpoint
   write is missing or older;
4. preserve the checkpoint's active phase for an in-phase provider failure;
5. apply only explicitly recorded artifact invalidations when moving backward;
6. never use enum defaults, a missing field, a model summary, or a different
   manifest to select `INIT`;
7. if sources cannot be reconciled safely, pause with
   `workflow_cursor_conflict` and show the run ID and diagnostic reason; and
8. write one reconciliation checkpoint before constructing the next phase
   prompt.

The reconciler must return both the selected cursor and a provenance object:

```json
{
  "phase": "architecture",
  "phase_index": 6,
  "source": "checkpoint_revision",
  "checkpoint_revision": 17,
  "manifest_id": "...",
  "manifest_revision": 42,
  "receipt_prefix": ["init", "recon", "visual_research", "research_gate", "bootstrap"],
  "rewind": false,
  "reason": "429 occurred during architecture turn; no architecture transition committed"
}
```

The example is illustrative; IDs and unbounded content must not be exposed in
normal logs or prompts.

### 7.7 Correct 429 handling

Rate-limit and transient transport errors must:

- be classified as recoverable provider failures unless policy or context
  integrity makes resume unsafe;
- be persisted with bounded provider/status metadata and no secret headers;
- leave committed conversation/tool steps intact under PRD-182;
- avoid busy retry loops after the configured retry/deadline budget;
- mark the workflow `paused`, not a fresh `INIT` run;
- display the active phase and exact run ID; and
- resume through the coordinator after the user chooses `continue` or resume.

The provider retry mechanism may retry the failed internal provider step when
safe. It must not re-run completed phase transitions or side-effecting tools
without the idempotency rules from PRD-169 and PRD-182.

### 7.8 TUI and headless behaviour

#### TUI

- After a recoverable error, retain a recovery record even if the in-memory
  handle is detached.
- Ordinary `continue` on a session with exactly one recoverable workflow must
  invoke the same path as `/workflow resume <run-id>`.
- If there are multiple recoverable runs, do not pick the newest by guess; show
  a selection UI or require an explicit run ID.
- If there is a recovery record but it is invalid, do not start a new workflow
  implicitly. Show the diagnostic and require reset/new run.
- The status line and notification must say `paused in ARCHITECTURE` (or the
  actual phase) and include the run ID, rather than saying only “continue.”

#### `--resume` and `--continue`

- `--resume <session-id>` restores the session transcript and then resolves a
  workflow run within that session using the coordinator.
- `--continue` selects the latest eligible session, then performs the same
  workflow-run resolution. Selecting a session is not sufficient.
- An explicit workflow name may not override a recoverable workflow run with a
  different workflow name. The user must reset or explicitly start a new run.

#### Headless execution

The headless workflow executor must accept an optional resume disposition. When
the selected session contains a valid recoverable run, it must rehydrate and
call `runner.resume(context)`. It must not unconditionally generate a UUID and
call `runner.run(intent)`. New headless runs continue to use `run(intent)` only
when no recoverable run was selected and the invocation is explicitly a new
run.

### 7.9 Evidence and manifest identity

Every workflow-owned evidence store must persist its association to:

```text
session_id, run_id, workflow_name, workspace_root, manifest_id
```

The association may be metadata in the manifest or a small run pointer; it
must not duplicate artifact contents. On resume:

- the expected manifest is loaded from the checkpoint/run association;
- a different active manifest is reported as an identity conflict;
- a stale manifest is not silently replaced;
- a new manifest is created only for a user-selected new run; and
- phase receipts are filtered by exact run/manifest association before they can
  advance the cursor.

This directly addresses cases where a previous manifest ID and an active
manifest ID appear in the same recovery explanation.

### 7.10 Generated workflows

`create_workflow` must generate workflows using the same contract automatically:

- bootstrap creates the durable run identity and typed context before the first
  provider/tool operation;
- the framework owns failure finalization and the generated runner does not
  replace it with a fresh context;
- `resume(context)` is a real resume path and never delegates to
  `run(context.intent)`;
- every transition records the next state before entering the next phase;
- generated contexts contain a phase cursor, phase attempts, outputs, and
  resume provenance sufficient for exact restoration;
- generated phase prompts explain that a transient error means “continue from
  the saved phase,” not “restart the workflow”;
- generated tools are idempotent or receipt-backed; and
- generated validation rejects runners that initialize `INIT` on resume, ignore
  the supplied checkpoint context, or create a second workflow identity.

## 8. Proposed data model changes

Extend the versioned checkpoint/context contract as needed, preserving backward
compatibility:

```text
WorkflowRunIdentity
  session_id: string
  conversation_id: string
  run_id: string
  workflow_name: string
  plugin_fingerprint: string
  workspace_root: string
  manifest_id: string | null

WorkflowCursor
  phase: string | null
  phase_index: integer
  phase_iteration: integer
  phase_attempt: integer
  last_committed_phase: string | null
  last_safe_boundary: string | null
  cursor_source: enum
  cursor_reason: string

WorkflowFailure
  error_revision: integer
  failure_kind: string
  provider_status: integer | null
  retryable: boolean
  phase: string | null
  checkpoint_revision: integer

WorkflowResumeProvenance
  source: checkpoint | receipt | journal | reconciled
  source_revision: integer
  manifest_revision: integer
  receipt_prefix_digest: string
  rewind: boolean
  invalidation_ids: string[]
```

Do not persist raw exception reprs, authorization headers, provider response
bodies, prompt text, or unbounded conversation content in the checkpoint.

Schema migration requirements:

- old checkpoints without identity/provenance fields receive safe defaults;
- old valid typed contexts remain loadable;
- a checkpoint that cannot prove its workflow run identity is diagnostic-only,
  not silently treated as a new run; and
- schema version and migration outcome are visible in recovery diagnostics.

## 9. Functional requirements

### FR-1 — One source of resume truth

All workflow resume-capable entry points shall use the same coordinator and
return an explicit new-run, resume, unavailable, or ambiguous disposition.

### FR-2 — No implicit fresh run after recoverable error

If a valid recoverable run exists for the selected session, ordinary
continuation, `--continue`, `--resume`, and session selection shall not create a
new run or call `runner.run(intent)`.

### FR-3 — Stable identity

A recoverable error and its resume shall preserve the same session ID,
conversation ID, workflow name, run ID, evidence manifest ID, and workflow
intent.

### FR-4 — Exact active phase

The resume checkpoint shall contain the phase active at failure, its phase
index/iteration, and the typed context required to execute that phase.

### FR-5 — Monotonic cursor

Generic error handling shall never rewind a run to an earlier phase. Explicit
evidence invalidation is the only exception and must be auditable.

### FR-6 — Atomic transition boundary

A phase transition shall persist its evidence receipt and next-phase cursor
before the next phase begins. A failed transition shall leave the source phase
retryable.

### FR-7 — Idempotent failure finalization

Runner, TUI, headless, timeout, and cleanup paths may observe the same error,
but only one failure disposition may be committed for a checkpoint revision.
Later observers shall not overwrite a more precise phase/cursor.

### FR-8 — Recoverable provider errors

429 and equivalent transient errors shall pause a valid workflow run with a
bounded diagnostic and make it eligible for exact resume.

### FR-9 — Shared conversation preservation

Resume shall use the existing session-scoped conversation and journal. It shall
preserve valid committed provider/tool messages according to PRD-182 and add a
bounded resume marker only after the checkpoint is selected.

### FR-10 — Manifest/run consistency

The runner shall verify that the evidence manifest and receipts belong to the
selected run. A mismatched active manifest shall produce a structured conflict,
not a new `INIT` context.

### FR-11 — Reconciliation provenance

Every cursor adjustment shall record its source, revision, reason, and whether
it was a forward recovery or an explicit invalidation rewind.

### FR-12 — User-visible disposition

The TUI shall show the workflow name, run ID, active phase, failure category,
and resume command. It shall explicitly say when no safe resume is available.

### FR-13 — Headless parity

The headless workflow runner shall support the same recovery and resume
semantics as the TUI and shall not equate restored session selection with a new
workflow run.

### FR-14 — Generated-workflow enforcement

`create_workflow` shall teach and validate the identity, checkpoint, cursor,
failure, and true-resume contract for every generated workflow.

### FR-15 — Backward compatibility

Existing valid checkpoints and workflows shall continue to load. Unsupported
or ambiguous old records shall be diagnostic-only with an actionable migration
or reset message.

## 10. Acceptance criteria

### AC-1 — Reproduce the reported 429

Given a workflow that has durably completed `init`, research phases, and the
research gate and is actively running `architecture`, inject a deterministic
429 during the architecture provider turn. After the process returns to idle:

- the checkpoint is `paused` and classified as `provider_transient`;
- the checkpoint phase is `architecture` (or the exact active phase);
- the checkpoint revision is greater than the prior revision;
- no new run ID or manifest ID exists; and
- the UI identifies the same run and tells the user to continue/resume it.

### AC-2 — Continue in the same TUI

After AC-1, enter `continue`. The system shall:

- invoke the same path as `/workflow resume <run-id>`;
- call `runner.resume(context)` exactly once;
- not call `runner.run(intent)`;
- not inject `INIT` or any completed phase prompt;
- retain the same conversation ID, run ID, manifest ID, and intent; and
- start with the saved active phase.

### AC-3 — Restart and `--resume`

Stop the process after AC-1, then launch `agenthicc --resume <session-id>`.
The workflow shall be discovered as paused at the saved phase and resume
without creating a new run. A valid transcript summary that says
`ARCHITECTURE` must agree with the durable checkpoint; the summary alone must
not be used to make the decision.

### AC-4 — `--continue` parity

Run the same scenario through headless `--continue`. It shall select the
existing workflow run in the selected session and call `resume(context)`, not
create a new UUID and call `run(intent)`.

### AC-5 — Error during INIT

Inject a 429 before the first phase transition. Resume must return to `INIT`
because that is the actual active phase. This proves that `INIT` remains valid
when it is genuinely the saved cursor.

### AC-6 — Error after a committed transition

Commit a transition from `architecture` to `design_system`, then inject a 429
before the first provider call in `design_system`. Resume must start at
`design_system`, not `architecture` and not `INIT`.

### AC-7 — Error after partial provider progress

Complete at least one provider/tool step in a phase, then fail a later step.
Resume must preserve the committed conversation/tool messages and must not
repeat a committed side effect without the transaction/idempotency contract.

### AC-8 — Mismatched manifest

Make the checkpoint reference manifest A while the active output directory
contains manifest B. Recovery shall report `workflow_identity_conflict` or
equivalent, retain both diagnostic IDs, and refuse to start `INIT` implicitly.

### AC-9 — Conflicting phase sources

Create a checkpoint at `architecture`, a contiguous receipt prefix through
`design_system`, and an older kernel projection. The reconciler shall select a
deterministic safe result, record provenance, and never select `INIT`.

### AC-10 — Explicit invalidation

Invalidate a visual research artifact while the run is later in the workflow.
The run may rewind to the documented narrow recovery phase, but the checkpoint
must record the invalidation reason and affected artifact IDs. A generic 429
must not trigger that rewind.

### AC-11 — Multiple runs

Create two paused runs in one session. `continue` and `--continue` shall not
guess. The UI must select a run or require an explicit run ID.

### AC-12 — Invalid recovery record

Make a checkpoint unavailable because its codec, plugin fingerprint, workspace,
profile, or conversation cursor is invalid. The next ordinary message shall
not silently start a new workflow. The user must receive a diagnostic and an
explicit reset/new-run choice.

### AC-13 — Repeated errors

Resume the same run, inject another 429, and verify that the same run is paused
at the new active phase with a higher checkpoint/error revision. No duplicate
run, manifest, phase-one execution, or terminal overwrite may occur.

### AC-14 — Duplicate finalizers

Cause the runner and TUI wrapper to finalize the same error concurrently. One
durable failure disposition and one user-visible failure event shall result.
The more precise active phase must not be replaced by a generic or initial
phase.

### AC-15 — True reset remains available

An explicit `/workflow reset` followed by a new workflow request may create a
new run and start at `INIT`. The audit trail must distinguish this intentional
reset from automatic error recovery.

### AC-16 — Generated workflow

Generate a workflow with `create_workflow`, inject a transient error in each
generated phase, restart, and resume. Every phase must receive the restored
context and never restart through `run(intent)`.

### AC-17 — User-visible correctness

The TUI must display, at minimum:

```text
Workflow '<name>' paused after provider rate limit in phase 'architecture'.
Run <run-id> is saved. Use /workflow resume <run-id> or continue.
```

It must not display a successful completion, a generic fresh-run message, or a
claim that the workflow is at `INIT` unless the checkpoint actually says so.

## 11. Testing strategy

Tests must use temporary session/workflow stores, fake providers, deterministic
clocks, and controlled manifests. No test may depend on a real provider,
network rate limit, browser, MCP server, or pre-existing home directory.

### 11.1 Unit tests

- failure classification maps 429/rate-limit transport errors to
  `provider_transient`;
- execution snapshot captures the latest handle/context phase rather than the
  workflow's first phase;
- checkpoint revision writes are monotonic and same-payload idempotent;
- stale cursor writes are rejected;
- cursor reconciliation prefers the newest valid same-run source;
- reconciliation never selects `INIT` from a missing/default field;
- explicit invalidation is the only allowed backward move;
- manifest/run identity validation rejects mismatches;
- repeated finalization does not change the committed failure cursor;
- resume disposition distinguishes new, resume, unavailable, and ambiguous;
- a recoverable record with no attached in-memory handle is still selected;
- a diagnostic-only record cannot become an implicit new workflow; and
- generated runner validation rejects `resume()` implementations that call
  `run(intent)` or replace the restored context.

### 11.2 Integration tests

- TUI provider failure → checkpoint → `continue` → exact `resume(context)`;
- TUI process restart → recovery inspection → `/workflow resume`;
- TUI process restart → one candidate → ordinary `continue` parity;
- headless `--continue` workflow recovery;
- explicit `--resume` with session and workflow checkpoints;
- checkpoint/context/evidence/journal/kernel disagreement;
- manifest A/B conflict;
- atomic transition ordering and crash injection between each commit step;
- duplicate failure finalization and claim release;
- rate-limit pause with shared conversation/journal preservation;
- provider retry after a committed tool step;
- multiple paused runs and deterministic selection;
- invalid plugin/profile/workspace/context recovery diagnostics; and
- backward migration of pre-PRD-184 checkpoints.

### 11.3 End-to-end tests

Use a fake streaming provider whose failures are scheduled by `(run_id, phase,
provider_step)`:

1. start a reconstruct workflow;
2. complete research and enter architecture;
3. return a 429;
4. close the TUI/process;
5. reopen the same session with `--resume` or `--continue`;
6. submit continue;
7. assert the first resumed phase, all IDs, event ordering, transcript, and
   output continuity; and
8. complete the workflow after the provider becomes available.

Repeat the journey for `code_plan`, a generic declarative workflow, and a
workflow generated by `create_workflow`. Include explicit reset as a control
case proving that only user intent starts a fresh `INIT` run.

### 11.4 Regression assertions

Every regression test must assert both positive and negative behaviour:

- positive: exact saved phase is resumed;
- negative: `INIT` prompt count does not increase;
- positive: `run_id` and manifest ID remain stable;
- negative: no replacement UUID is created;
- positive: `resume(context)` is called;
- negative: `run(intent)` is not called; and
- positive: prior committed conversation/tool messages remain available.

## 12. Observability and diagnostics

Emit structured, redacted events for:

- `workflow_failure_observed`;
- `workflow_checkpoint_committed`;
- `workflow_resume_selected`;
- `workflow_cursor_reconciled`;
- `workflow_resume_dispatched`;
- `workflow_cursor_conflict`; and
- `workflow_new_run_explicitly_selected`.

Each event includes run ID, workflow, session ID hash or safe session ID as
allowed by existing policy, phase, phase index, checkpoint revision, manifest
ID/revision, source, and disposition. Provider status and failure kind are
allowed; credentials, authorization headers, prompt bodies, full model output,
and raw exception payloads are not.

The normal TUI should make the following distinctions visible:

```text
paused — resumable at architecture
interrupted — resumable at architecture
cursor conflict — manual recovery required
diagnostic only — no safe resume available
new run — explicitly selected by the user
```

## 13. Security, performance, and reliability

- Preserve claim ownership and never steal a live run to “fix” a cursor.
- Validate all IDs and paths before loading manifests or checkpoints.
- Keep diagnostics bounded and redacted.
- Do not increase the provider prompt with complete checkpoint/evidence
  contents; resume metadata should be compact and cache-stable.
- Reconciliation must be bounded in time and avoid repeatedly scanning large
  artifact trees on every provider turn.
- Atomic writes and directory fsync semantics remain in the checkpoint store.
- A transient error must not cause an unbounded automatic retry loop.
- The failure finalizer must be idempotent under cancellation and cleanup races.
- If durable state cannot be written, the system must say recovery is
  unavailable rather than pretending that a fresh run is a continuation.

## 14. Rollout and migration

1. Add tracing and a read-only reconciliation report behind tests to capture
   real cursor disagreements without changing execution.
2. Implement the shared resume disposition and headless parity.
3. Add snapshot-based failure finalization and monotonic cursor writes.
4. Add manifest/run identity and reconciliation provenance.
5. Migrate built-in runners and generated workflow validation.
6. Enable the TUI anti-fallback guard: recoverable/invalid records cannot turn
   an ordinary continuation into a new run without an explicit choice.
7. Enable the full E2E matrix and retain a compatibility switch only for
   checkpoint schema migration, not for silently reverting to fresh-run
   behaviour.

Existing terminal `failed` and diagnostic-only records remain non-resumable.
Existing valid paused/running checkpoints are migrated with a provenance value
of `legacy_checkpoint` and are reconciled conservatively. If their phase cannot
be proven safe, the user receives a diagnostic rather than an automatic rewind.

## 15. Assumptions and open questions

### Assumptions

- A 429 is recoverable by default when typed context and checkpoint storage are
  valid.
- The existing session conversation remains the only provider-facing history;
  workflow checkpoints store pointers and typed state, not a second transcript.
- Explicit reset is the intended escape hatch for a truly incompatible or
  corrupt run.
- Phase receipts and evidence manifests can be extended with run identity
  without duplicating artifact content.

### Open questions for implementation

1. Should a session with one invalid recovery record block a new workflow by
   default, or require a specific `--new-workflow`/`/workflow reset` command?
2. Which manifest association is canonical for workflows that intentionally
   reuse an existing output directory?
3. Should a failure after a successful transition but before the next provider
   request resume the next phase or expose a “boundary committed” confirmation
   in the TUI? The recommended behaviour is to resume the next phase because
   the transition protocol makes that boundary durable.
4. Should all recovery provenance be journaled, or is the versioned checkpoint
   plus structured session event sufficient? The default should be checkpoint
   plus event, with no duplicated provider message history.

## 16. Definition of done

- The investigation report identifies the reproduced restart path and
  distinguishes confirmed causes from ruled-out hypotheses.
- TUI, headless, `--continue`, `--resume`, and `/workflow resume` use the same
  workflow-run recovery coordinator.
- A recoverable 429 resumes the same run at the exact active phase.
- No generic failure path can silently call `run(intent)` for a recoverable run.
- Checkpoint, typed context, evidence, journal, manifest, and kernel state have
  deterministic reconciliation with provenance.
- Generic error handling cannot rewind a run to `INIT`.
- `reconstruct_site`, `code_plan`, generic workflows, and generated workflows
  pass the unit, integration, and E2E acceptance matrix.
- Existing valid checkpoints remain compatible and unsafe legacy records are
  diagnosed clearly.
- Documentation, event schemas, workflow guidance, and generated prompts
  describe the same resume contract.
- Relevant lint, type, unit, integration, and E2E checks pass, with unrelated
  repository blockers reported explicitly.

## 17. Implementation record

The recovery contract is implemented across the session owners and durable
workflow stores:

- `WorkflowRunHandle.sync_context_cursor()` captures the forward typed phase
  before failure finalization, so a stale bootstrap/`INIT` handle cannot replace
  a later active phase in the error checkpoint.
- `WorkflowRecoveryCoordinator.select_for_resume()` provides one shared,
  fail-closed selection path for TUI, headless, and background execution.
- `execute_workflow()` accepts an existing run ID, reloads and claims its latest
  checkpoint, preserves the original intent, and dispatches
  `runner.resume(context)` instead of creating a replacement UUID.
- TUI continuation now refreshes durable recovery state before it can fall
  through to a new workflow, and headless execution has the same behavior.
- `WorkflowCheckpointStore.save()` rejects stale revisions, treats identical
  same-revision writes as idempotent, and rejects conflicting same-revision
  payloads.

Verification includes the 429 regression in
`tests/unit/test_workflow_cli.py`, cursor/finalizer and checkpoint-store tests
in `tests/unit/test_checkpoint_lifecycle_edges.py`, recovery selection tests in
`tests/unit/test_workflow_recovery.py`, TUI anti-fallback coverage in
`tests/unit/test_tui_session_coverage.py`, and the existing process-restart
E2E recovery matrix. The final local run completed with 3,666 tests passed and
15 skipped; repository-wide format and mypy findings outside this change are
reported in the implementation handoff.
