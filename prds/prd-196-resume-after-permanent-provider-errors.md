---
title: "PRD-196: Resume workflows after permanent provider errors"
status: Implemented
version: 1.1.0
date: 2026-09-25
scope: "provider 4xx failures, workflow failure finalization, durable pause checkpoints, and resume after configuration correction"
related_prds:
  - PRD-117  # permanent provider-error early exit
  - PRD-169  # transaction-safe provider conversations
  - PRD-170  # durable workflow recovery
  - PRD-173  # recoverable workflow errors and failure checkpoints
  - PRD-184  # preserve the active workflow phase after errors
  - PRD-188  # resume the latest recoverable workflow run
  - PRD-195  # lauren-ai reasoning compatibility and recovery
tags:
  - workflows
  - provider-errors
  - model-configuration
  - checkpoints
  - resume
  - permanent-errors
---

# PRD-196 — Resume workflows after permanent provider errors

## 1. Executive summary

An agenthicc workflow can become non-resumable after a provider returns a
permanent HTTP 400 error, such as an unsupported model request:

```text
ERROR TransportError: Error code: 400 - {'model': 'deepseek-v4.1-flash'}
| provider='openai' | status_code=400 |
caused by: BadRequestError(...)
```

The request is correctly classified as non-retryable: sending the same model
and request again cannot repair it. However, **non-retryable is not the same
as non-resumable**. The workflow has already completed useful work and its
typed context can still be checkpointed. The user should be able to correct
the provider/model configuration and resume the same workflow run at the
same phase.

The current failure path can lose that distinction. `AgentTurnRunner`
re-raises the 400, a specialized phase catches it and returns a `FAILED` state,
and the outer workflow loop treats that state as an ordinary terminal result.
That path can bypass `WorkflowRunHandle.finalize_failure()`, which is the
operation that saves a recoverable typed context as a `paused` checkpoint.
Consequently, `--resume`, `--continue`, or `/workflow resume` may find no
recoverable run, even though the user only needs to fix the model or endpoint.

This PRD defines a two-axis error contract:

```text
retryable by provider request?   no
workflow resumable after repair? yes, when typed context/checkpointing work
```

It applies to built-in workflows, generated workflows, direct workflow
execution, TUI continuation, headless execution, process restart, and explicit
resume. It does not retry invalid provider requests or fabricate a response.

## 2. Evidence-backed diagnosis

### 2.1 Current request path

The observed error travels through the following path:

```text
provider rejects model/request with HTTP 400
        │
        ▼
lauren-ai raises TransportError/BadRequestError
        │
        ▼
AgentTurnRunner._is_permanent_error() = True
        │
        ├── request retry is correctly skipped
        └── error is re-raised across the agent-turn boundary
                │
                ▼
phase method catches the exception
  ctx.fail_reason = "...400..."
  return State.FAILED
                │
                ▼
outer workflow loop treats FAILED as normal terminal completion
  emits a failed workflow result
  does not necessarily call finalize_failure()
  does not necessarily save a paused checkpoint
                │
                ▼
recovery coordinator excludes terminal failed records
        │
        ▼
user cannot resume the existing run
```

### 2.2 Confirmed implementation facts

The current source establishes these facts:

1. `src/agenthicc/runners/agent_turn.py` treats HTTP 400–499 errors other
   than 429 as permanent and therefore not eligible for transport retry.
2. That classification is appropriate for an invalid model, credentials,
   endpoint, schema, or request. Retrying the identical request is wasteful.
3. `CodePlanRunner` phase methods catch turn exceptions, store `fail_reason`,
   and return `CodePlanState.FAILED`.
4. `CodePlanRunner.run()` maps `FAILED` to a terminal workflow result and
   emits completion state rather than routing the failure through the durable
   failure finalizer.
5. `WorkflowRunHandle.finalize_failure()` already has the desired capability:
   when a typed context and checkpoint store are valid, it sets the lifecycle
   to `paused`, synchronizes the active cursor, and saves a checkpoint.
6. `WorkflowRecoveryCoordinator` only offers records with a recoverable
   lifecycle and valid context to resume; terminal `failed` records are not
   ordinary resume candidates.

The defect is therefore not that 400 errors are classified as permanent. The
defect is that permanent provider errors can be converted into a normal
terminal phase result before the workflow failure finalizer receives them.

### 2.3 Why this is different from a transient 429

A 429 should normally consume the transient retry policy and, after retry
exhaustion, be checkpointed as a paused provider failure. A 400 model error
should not retry the same request at all. Both cases still share the same
workflow-level rule:

> If a typed context and durable checkpoint boundary exist, preserve the same
> run and phase for user-directed recovery.

The provider retry axis and workflow recovery axis MUST remain independent.

## 3. Problem statement

The product currently conflates “the provider will not accept this request”
with “the workflow can never resume.” This causes:

- completed artifacts to be detached from the active workflow run;
- a model/configuration mistake to require restarting a long workflow;
- `--continue` to start a new turn or report no recoverable workflow;
- users to lose the precise phase cursor and run identity; and
- repeated manual attempts to risk duplicate work or a fresh `INIT` run.

The system needs a durable, idempotent boundary between provider failure and
workflow lifecycle. A permanent provider error must stop the current request,
save the current typed state, release ownership, and wait for an explicit
configuration correction and resume.

## 4. Goals

The implementation MUST:

1. Keep permanent provider errors non-retryable at the transport/request
   layer.
2. Preserve a valid workflow run as `paused` when its typed context and
   checkpoint store are available.
3. Route every phase-local turn failure through one idempotent workflow failure
   finalizer, regardless of whether the phase method catches and returns a
   state or propagates an exception.
4. Save the exact `run_id`, `conversation_id`, active phase, phase index,
   phase iteration, context, artifacts, approvals, cache metadata, and safe
   failure classification.
5. Let the user correct the model/provider/profile configuration and resume
   the same run through `resume(context)`, never through a new `run(intent)`.
6. Make ordinary `continue`, `/workflow resume`, `--continue`, and
   `--resume` use the same durable recovery decision.
7. Ensure the behavior is inherited by workflows generated by
   `create_workflow` without requiring generated code to duplicate error
   handling.
8. Prevent an infinite retry loop or accidental fresh `INIT` run.
9. Preserve provider history and tool-call transaction integrity.
10. Add deterministic unit, integration, end-to-end, and regression tests.

## 5. Non-goals

This PRD does not:

- retry a 400 model/request error;
- guess a valid replacement model;
- silently change provider, model, profile, or endpoint;
- roll back filesystem, subprocess, browser, or network side effects that
  completed before the failure;
- mark a corrupt or context-less checkpoint resumable;
- overwrite or delete the failed run;
- replace PRD-117's permanent-error classification;
- replace PRD-184's phase-cursor/topology reconciliation; or
- expose API keys, authorization headers, full request bodies, or hidden
  reasoning in the recovery record.

## 6. Product requirements

### 6.1 Two independent dispositions

Every provider error MUST have two explicit decisions:

| Axis | Meaning | Example for HTTP 400 model error |
|---|---|---|
| Request retryability | Whether the identical provider request may be sent again automatically | `false` |
| Workflow resumability | Whether the same workflow run may continue after an explicit repair | `true` when context/checkpoint are valid |

The current boolean/exception paths may be adapted for compatibility, but no
caller may infer workflow resumability solely from `retryable == false`.

### 6.2 Permanent provider failure classification

The failure record MUST contain bounded, JSON-safe metadata:

```text
failure_kind: provider_configuration | provider_request | provider_history
retryable: false
resumable: true | false
provider: openai | anthropic | ollama | litellm | ...
model: redacted-safe model identifier
status_code: integer | null
phase: active phase name
run_id: stable run identity
```

The model identifier may be retained because it is configuration metadata, but
credentials, headers, prompts, tool arguments, raw bodies, and hidden
reasoning MUST NOT be persisted in the failure record.

An invalid model/profile/endpoint or authentication configuration is
`provider_configuration`. A malformed conversation that cannot safely be
replayed is `provider_history` and follows PRD-195's recovery rules. Other
non-retryable request failures are `provider_request`. Classification must
not depend on the exact wording of one provider's error string.

### 6.3 Unified workflow failure finalization

The workflow engine MUST have one idempotent boundary with behavior equivalent
to:

```python
finalize_workflow_failure(
    handle,
    error,
    retryable=False,
    failure_kind="provider_configuration",
)
```

The boundary MUST:

1. synchronize the handle cursor from the attached typed context;
2. preserve the current active phase rather than the bootstrap phase;
3. preserve all committed conversation/tool/artifact state;
4. set the lifecycle to `paused` when the context and checkpoint are valid;
5. atomically save the checkpoint and increment its revision;
6. emit one bounded failure event;
7. release the live workflow claim after durable save; and
8. be safe when called again by a phase loop, runner wrapper, TUI cleanup
   callback, or task-finalization callback.

If checkpoint serialization/storage fails, the run becomes diagnostic-only and
the user receives an explicit recovery-unavailable message. The framework
MUST NOT pretend that a new run is a continuation.

### 6.4 Phase-loop contract

Specialized and generated phase loops MUST follow one of these equivalent
contracts:

- propagate a turn/provider exception to the workflow owner; or
- return a typed failure result that carries the original error and causes the
  owner to invoke the unified finalizer before publishing terminal state.

Returning a bare `FAILED` enum after writing only `ctx.fail_reason` is
insufficient. A normal workflow completion event MUST NOT be the only durable
record of an error that can be repaired by changing configuration.

The success path remains strict:

- `COMPLETE`/equivalent means successful terminal completion;
- `EXITED`/equivalent means explicit user exit; and
- `FAILED` means the failure finalizer has already committed a terminal or
  paused disposition.

### 6.5 Resume after configuration correction

After the user fixes the model, provider profile, endpoint, credentials, or
request configuration, resume MUST:

1. select the same durable `run_id` and `conversation_id`;
2. re-read the current effective configuration and secrets without writing
   them into the checkpoint;
3. validate the workflow fingerprint, topology, workspace, conversation
   cursor, and context codec;
4. rehydrate the saved context and memory;
5. invoke the workflow's `resume(context)` entry point;
6. begin at the saved active phase/boundary; and
7. clear or supersede the prior failure metadata only after a successful
   resumed boundary is durably saved.

It MUST NOT call `run(original_intent)` for a recoverable checkpoint, create a
new workflow or manifest ID, or inject the first-phase prompt solely because
the previous error was a 400.

If the saved profile no longer exists or the user requests a different
profile/model, the change must be explicit and validated before resume. The
old checkpoint remains available if the replacement configuration is also
invalid.

### 6.6 User-facing behavior

After the error, the TUI/headless surface MUST show a message equivalent to:

```text
Workflow 'code_plan' paused in phase 'execute' after a non-retryable
provider error for model 'deepseek-v4.1-flash'. Fix the provider/model
configuration, then use /workflow resume <run-id> or continue.
```

The message MUST identify the workflow, phase, run ID, and recovery action. It
MUST NOT claim successful completion, silently restart at `INIT`, or recommend
raising the retry count for a permanent 400.

If multiple recoverable runs exist, selection must be explicit or use the
existing deterministic latest-run contract. If a run exists but is invalid,
the UI must show the diagnostic and require an explicit reset/new-run action.

## 7. Data flow

### 7.1 Failure and pause

```text
active workflow run (run R, phase P, context C)
        │
        ▼
provider rejects request with permanent HTTP 400
        │
        ├── transport: retryable = false; stop this request
        │
        ▼
workflow failure coordinator
  sync cursor from C
  retain run R, conversation, artifacts, and phase P
  classify provider_configuration/request/history
        │
        ▼
atomic paused checkpoint revision N+1
  {run_id=R, phase=P, context=C, retryable=false, resumable=true}
        │
        ▼
release claim and publish one paused notification
```

### 7.2 Corrected resume

```text
user fixes model/provider configuration
        │
        ▼
--resume R / --continue / workflow resume
        │
        ▼
recovery coordinator validates checkpoint R
        │
        ▼
current configuration is resolved
        │
        ▼
runner.resume(saved_context)
        │
        ▼
provider request uses corrected model/profile
  same conversation_id, same workflow run_id, phase P
        │
        ▼
next safe boundary checkpoint supersedes failure metadata
```

## 8. Implementation design

### 8.1 Shared failure result

Introduce or extend a shared typed result/exception boundary between
`AgentTurnRunner`, `BaseWorkflowRunner`, specialized runners, and
`WorkflowRunHandle`. It must carry the original exception for logging while
exposing only sanitized fields to persistence/UI.

The implementation must preserve compatibility with existing runner methods.
Adapters may translate an existing `FAILED` state into a failure finalization,
but new workflow code must use the typed contract directly.

### 8.2 Specialized runner fixes

Audit `code_plan`, `goal_flow`, `create_workflow`, `reconstruct_site`,
`make_book`, `site_imitate`, and every registered workflow for phase methods
that catch provider/tool exceptions and return a bare failed state.

For each affected runner:

- keep the phase's user-facing failure summary;
- retain the original error classification;
- attach/synchronize the typed context before returning or raising;
- route the failure through the shared finalizer; and
- ensure the outer loop does not overwrite the paused checkpoint with a
  terminal `failed` result.

Generated workflows must inherit this behavior from the generic runner and
must not be asked to reproduce provider-error persistence manually.

### 8.3 Checkpoint and event ordering

The durable ordering MUST be:

```text
typed context cursor
  → failure metadata
  → atomic paused checkpoint
  → failure projection/event
  → claim release
```

The error event may be emitted before the checkpoint only if the event clearly
states that persistence is pending and a failure-safe fallback guarantees a
durable result. A successful workflow completion event must never precede the
failure decision for the same turn.

### 8.4 Configuration refresh

Resume must resolve credentials and non-secret provider settings at resume
time, as existing profile semantics require. Checkpoints retain profile
identity and safe model metadata for diagnostics, not secrets. A corrected
configuration must not mutate historical checkpoint provenance.

### 8.5 No provider-specific workflow branches

The `deepseek-v4.1-flash` value is reproduction data, not a special case.
The implementation must work for any provider/model returning a permanent
request error, including incorrect model IDs, invalid profiles, unsupported
parameters, authentication failures, and endpoint policy errors.

## 9. Acceptance criteria

| ID | Criterion |
|---|---|
| 196.1 | A 400 model error is not automatically retried. |
| 196.2 | The same 400 error with a valid typed context creates a paused checkpoint rather than silently discarding the workflow run. |
| 196.3 | The checkpoint preserves the exact run ID, conversation ID, active phase, phase index, context, and prior artifacts. |
| 196.4 | The recovery record is discoverable by `/workflow resume`, `--resume`, `--continue`, and the session UI. |
| 196.5 | After the model/provider configuration is corrected, resume invokes `resume(context)` rather than `run(intent)`. |
| 196.6 | Resume uses the same workflow run and conversation identity and does not create a new manifest/run ID. |
| 196.7 | The resumed provider request uses the corrected effective configuration. |
| 196.8 | Successful resumed progress clears or supersedes the prior error only after a new durable boundary. |
| 196.9 | Repeated finalization calls are idempotent and do not overwrite the first valid paused checkpoint. |
| 196.10 | A checkpoint serialization/storage failure is reported as diagnostic-only and never presented as a successful recoverable pause. |
| 196.11 | `code_plan` and at least one generated workflow pass the same permanent-provider-error journey. |
| 196.12 | No affected path injects the first-phase prompt or creates a fresh workflow after the error unless the user explicitly resets/starts new. |
| 196.13 | The UI identifies the workflow, phase, run ID, non-retryable status, and recovery action without exposing secrets or raw request bodies. |
| 196.14 | Transient 429/5xx/timeout retry behavior remains unchanged and still ends in a paused checkpoint after retry exhaustion. |
| 196.15 | Existing successful, cancelled, terminal-invariant, and invalid-checkpoint behavior remains unchanged. |

## 10. Test plan

### 10.1 Unit tests

Add tests for:

- the independent `retryable` and `resumable` decisions;
- permanent 400 classification without transport retry;
- phase-local failure conversion into the unified finalizer;
- active-context cursor synchronization before checkpointing;
- idempotent duplicate finalization;
- safe model/profile metadata redaction; and
- terminal behavior when context or checkpoint storage is invalid.

### 10.2 Integration tests

Using a temporary session and fake provider:

1. Start a multi-phase workflow and complete at least one earlier phase.
2. Make the provider reject the active model with HTTP 400.
3. Assert exactly one provider attempt and a paused checkpoint at the active
   phase.
4. Inspect recovery and verify the same run is selectable.
5. Change the effective model/profile configuration.
6. Resume and assert the fake provider sees the corrected model, same
   conversation ID, same run ID, and no first-phase prompt.
7. Inject the same failure twice and verify monotonic, idempotent recovery.
8. Fail checkpoint storage and verify a diagnostic-only result with no false
   resumability.

### 10.3 End-to-end tests

Headless and TUI tests MUST cover:

- the exact `deepseek-v4.1-flash` 400-shaped failure;
- visible paused status and actionable resume instruction;
- process restart followed by `--resume`;
- ordinary `continue` selecting the existing run;
- corrected provider configuration continuing the later phase;
- no duplicate artifacts or new workflow ID; and
- explicit reset/new-run behavior remaining the only way to start at `INIT`.

### 10.4 Workflow coverage

At minimum, test `code_plan`, `goal_flow`, `reconstruct_site`, and one
workflow produced through `create_workflow`. Add parametrized coverage for
every registered workflow whose phase runner catches provider errors locally.

## 11. Rollout and migration

1. Land the shared failure-finalization contract.
2. Migrate specialized and generic runners to it.
3. Add compatibility decoding for existing checkpoints that lack the new
   retryability/resumability fields.
4. Treat old terminal failed records conservatively: they remain inspectable,
   but are not retroactively made resumable without a valid context.
5. Deploy the UI diagnostics and recovery path.
6. Monitor bounded counters for paused permanent provider errors, successful
   corrected resumes, diagnostic-only failures, and accidental fresh-run
   fallbacks.

## 12. Security, privacy, and performance

- Never persist API keys, authorization headers, raw request bodies, prompts,
  tool arguments, or hidden reasoning in the failure envelope.
- Reuse existing checkpoint permissions, ownership claims, and atomic writes.
- Keep error messages bounded and redacted.
- Do not issue a live probe request solely to decide resumability.
- The failure finalizer must be O(1) in workflow history aside from the
  existing checkpoint serialization and journal operations.
- A permanent provider error must not consume the transient retry budget or
  create a busy loop.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A malformed request is actually unsafe to resume | Validate context and checkpoint capability; classify history/invariant failures separately. |
| A user resumes without fixing the model | Make the next failure bounded and actionable; do not retry automatically. |
| A phase catches and hides the original error | Require the typed failure result or propagation contract and test every registered runner. |
| Duplicate TUI and runner finalizers race | Use idempotent revision/owner checks and preserve the first committed disposition. |
| Resume uses the old invalid provider settings | Re-resolve current configuration and support explicit profile/model correction. |
| Old checkpoints lack new fields | Apply conservative defaults and preserve existing validation semantics. |

## 14. Definition of done

The PRD is complete when a permanent provider 400 stops the invalid request
without retrying, saves a paused checkpoint for every valid workflow context,
exposes the same run to all resume entry points, resumes through
`resume(context)` after configuration correction, never silently restarts at
`INIT`, preserves existing transient/terminal behavior, and passes the full
unit, integration, E2E, and registered-workflow regression matrix.

## 15. Implementation evidence

Implemented in the shared workflow failure boundary and session owners:

- `WorkflowFailureDisposition` and `classify_workflow_failure()` now separate
  provider request retryability from workflow resumability and classify
  permanent model/configuration errors without provider-specific branches.
- `WorkflowCheckpoint` and recovery diagnostics persist bounded,
  backward-compatible provider/model/status metadata while retaining the
  existing schema-v1 decoder defaults.
- `WorkflowRunHandle.finalize_failure()` records the two-axis disposition,
  synchronizes the typed phase cursor, and remains idempotent. Successful
  completion clears stale failure metadata only after its durable checkpoint.
- TUI and headless owners inspect typed `FAILED` results before their normal
  return safety net, preventing specialized runners from being mistaken for
  successful completion. They use the same paused checkpoint path as thrown
  exceptions and never fall back to a new first-phase run.
- `code_plan` and `create_workflow` invoke the same finalizer for local phase
  failures; generated workflows inherit the owner-level protection.

Verification evidence:

- `tests/unit/test_permanent_error_exit.py` covers the independent classifier,
  400 non-retry behavior, 429 transient classification, and agent-turn retry
  boundaries.
- `tests/unit/test_workflow_error_recovery.py` verifies a 400 model failure
  preserves run identity, conversation identity, active phase, checkpoint,
  safe provider metadata, and resumability.
- Existing workflow recovery, checkpoint, topology, and resume suites remain
  regression coverage for idempotency, storage failure, cancellation, and
  process restart behavior.
