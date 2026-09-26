---
title: "PRD-198: Bounded recovery retries for irrecoverable provider errors and extended ask_user timeout"
status: Implemented
version: 1.0.0
date: 2026-09-26
scope: "Provider-error recovery budgets, resumable workflow checkpoints, and interactive question waits"
related_prds:
  - PRD-117  # permanent provider-error early exit
  - PRD-126  # transport retry with memory rollback
  - PRD-169  # transaction-safe tool-call conversations
  - PRD-170  # durable workflow recovery
  - PRD-173  # recoverable workflow errors and failure checkpoints
  - PRD-184  # preserve the active workflow phase after errors
  - PRD-192  # configurable ask_user timeout and fallback
  - PRD-196  # resume workflows after permanent provider errors
tags:
  - provider-errors
  - retries
  - workflow-recovery
  - checkpoints
  - ask-user
  - configuration
---

# PRD-198 — Bounded recovery retries for irrecoverable provider errors and extended `ask_user` timeout

## 1. Executive summary

Two related recovery problems are visible to users:

1. A provider can reject a request with an irrecoverable client error, such as
   HTTP 400 for an unsupported model:

   ```text
   ERROR TransportError: Error code: 400 - {'model': 'deepseek-v4.1-flash'}
   | provider='openai' | status_code=400
   ```

   The request must not enter an unbounded retry loop, but the workflow should
   receive a configurable, bounded recovery budget before it is checkpointed
   and made resumable. The default budget is five retries after the initial
   request. Exhaustion must save the current workflow run and phase rather than
   starting a new run or returning to `INIT`.

2. `ask_user` currently defaults to a 60-second wait. That is too short for a
   user who is reviewing a long question, gathering information, or returning
   to an unattended terminal. The default must become 300 seconds (five
   minutes), while explicit configuration and structured timeout semantics from
   PRD-192 remain intact.

This PRD defines the implementation contract. It does not make a permanent
provider error magically recoverable: the same invalid request may continue to
fail on every attempt. Instead, it gives operators a deliberate, bounded
recovery window, preserves durable state after exhaustion, and makes the final
failure actionable. Transient provider errors retain the existing
`transport_max_retries` policy and are not double-counted against this new
budget.

## 2. Evidence-backed diagnosis

### 2.1 Current provider-error path

The current request path is:

```text
workflow phase / direct agent turn
        │
        ▼
AgentTurnRunner._stream()
        │
        ├── run_with_transport_retry(...)
        │       └── retries only errors classified as transient
        │
        └── HTTP 400–499 except 429 is classified permanent
                │
                ▼
        error is re-raised across the agent-turn boundary
                │
                ▼
        workflow owner finalizes an error-paused checkpoint
```

The classification is correct for a bad model, invalid request, bad
credentials, unsupported schema, or similar client error: retrying an identical
request does not repair the request. `transport_max_retries` is therefore the
wrong setting for this requirement. It is the turn-level memory-safe retry
budget for transient network and provider-stream failures and must keep its
existing meaning.

The word “non-retryable” applies only to the ordinary transient transport
classifier. It MUST NOT mean “do not propagate the final failure.” After the
separate bounded recovery budget has been consumed, the still-failing provider
error MUST cross the agent-turn/workflow boundary as a typed failure. The
workflow owner MUST then finalize the same run as a resumable paused
checkpoint. A phase MUST NOT swallow the error, convert it into an ordinary
`FAILED` return that bypasses finalization, or restart at `INIT`.

The product gap is that there is no explicit, separately named recovery budget
for an irrecoverable provider error. Operators cannot choose between failing
immediately and allowing a bounded number of re-attempts while they expect a
provider configuration, endpoint, or external routing condition to become
valid. When the error ultimately crosses the workflow boundary, the system also
needs an explicit guarantee that the already-attached typed context, active
phase, conversation, and checkpoint are preserved exactly once.

### 2.2 Why the observed 400 may appear twice

The current implementation records a provider-turn error and the workflow/TUI
owner can also report the propagated failure. These are two projections of one
failure, not evidence that the provider successfully executed the request
twice. PRD-198 must give the error a stable failure/retry identity and render a
single user-facing retry stream plus one final error summary. Diagnostic logs
may retain the exception chain, but the scroll transcript must not become an
unbounded duplicate-error list.

### 2.3 Current `ask_user` path

The question path is:

```text
agent calls ask_user(questions)
        │
        ▼
make_questions_tool → ApprovalService → QuestionsOverlay
        │
        ├── configured question_timeout_s (currently 60 seconds by default)
        └── structured timeout result from PRD-192
```

`question_timeout_s` is already a distinct setting from provider, agent-turn,
HTTP, browser, and plugin timeouts. PRD-198 changes its default from 60.0 to
300.0 seconds in every construction and configuration fallback path. It does
not make the wait unbounded and does not change explicit overrides.

## 3. Problem statement

The current recovery contract leaves three operational ambiguities:

- A request known to be irrecoverable has no dedicated bounded retry setting,
  so retry behavior is either immediate failure or an accidental re-drive by a
  higher-level loop.
- A retrying or failing provider turn can produce repeated messages without
  clearly communicating the attempt count, the terminal condition, or whether
  the workflow is still resumable.
- A 60-second question deadline is insufficient for normal human review, yet
  changing it in one code path could leave TUI, background, headless, resumed,
  or generated-workflow paths with inconsistent defaults.

The implementation must make all three outcomes explicit:

```text
transient error       → existing transport retry budget
irrecoverable error   → new bounded recovery retry budget
budget exhausted      → one durable paused checkpoint at the same phase
ask_user unanswered   → timeout result after 300 seconds by default
```

## 4. Goals

The implementation MUST:

1. Add a clearly named integer configuration setting for retries after an
   irrecoverable provider error, defaulting to `5` retries after the initial
   attempt.
2. Keep `transport_max_retries` exclusively responsible for transient
   network/provider-stream retries and keep `llm_sdk_max_retries` as the SDK
   layer setting.
3. Apply the irrecoverable-error budget at the single agent-turn boundary so
   direct turns, workflow phases, generated workflows, subagents, TUI, and
   headless execution cannot silently invent different retry behavior.
4. Retry only the current provider request/turn boundary. Do not re-run a
   completed workflow phase, re-execute completed tool side effects, or create
   a new workflow run.
5. Preserve conversation and tool-call transaction integrity between attempts,
   using the existing journal, committed-step receipts, idempotency ledger, and
   checkpoint mechanisms from PRDs 126 and 169.
6. Emit bounded, readable progress with attempt number, configured budget,
   error category, and next action. Do not append the same full SDK exception
   representation on every attempt.
7. After the budget is exhausted, save a resumable paused checkpoint containing
   the exact run, conversation, typed context, active phase, phase index,
   committed artefacts, and failure metadata.
8. Ensure `--continue`, `--resume`, workflow resume, and session restart resume
   the same run at the saved phase instead of invoking `run(intent)` from
   `INIT`.
9. Change the default `tools.question_timeout_s` from 60 seconds to 300
   seconds in config models, parsing defaults, services, background adapters,
   generated templates, documentation, and user-visible result examples.
10. Preserve PRD-192's distinction between answered, cancelled, timed out, and
    failed question requests. A timeout must never approve a tool or fabricate
    an answer.
11. Provide deterministic unit, integration, and end-to-end coverage for retry
    classification, memory safety, checkpoint persistence, resume, duplicate
    suppression, configuration validation, and question timeout propagation.

## 5. Non-goals

This PRD does not:

- make an invalid model name, invalid API key, malformed request, or unsupported
  schema valid;
- replace or rename the existing transient transport retry settings;
- retry cancellation, keyboard interrupts, programming errors, or tool-side
  effects that are not idempotently recoverable;
- rerun an entire workflow phase or reset its context after a provider error;
- bypass capability, approval, workspace, network, browser, MCP, or plugin
  policy during recovery;
- remove the durable checkpoint requirement after retry exhaustion;
- make `ask_user` waits unbounded;
- select an answer, approve a tool, or alter workflow state from a timeout
  callback;
- change the timeout defaults for ordinary tool approvals, provider requests,
  HTTP tools, browsers, MCP, or subprocesses;
- persist API keys, question answers, full prompts, or unbounded exception
  payloads in retry diagnostics.

## 6. Functional requirements

### 6.1 Configuration contract

Add an execution setting with a stable public name. The recommended name is:

```toml
[execution]
irrecoverable_error_max_retries = 5
```

The setting means **retries after the first provider attempt**. Therefore a
value of `5` permits at most six provider attempts for one logical request.
The UI and diagnostics MUST use the same terminology and make this distinction
clear. A value of `0` means one initial attempt and no retry.

The setting MUST:

- default to `5` when omitted;
- be an integer, not a boolean or fractional number;
- reject negative values;
- have a documented finite safety ceiling (the implementation should choose a
  conservative ceiling such as 20 and make the validation error actionable);
- be available through TOML, the existing `--set` override path, resolved
  profile settings, and all runtime configuration objects;
- be included in redacted config display and effective-settings diagnostics;
- never accept secrets or provider payloads as part of its value.

Backwards compatibility requires that an existing
`execution.transport_max_retries` value keeps its current meaning and default.
An implementation MAY support a temporary alias, but the canonical setting
must be unambiguous and the alias must not cause both budgets to be applied to
the same error.

### 6.2 Error taxonomy and retry decision

The implementation MUST retain the existing classification boundary:

| Error | Transport retry budget | Irrecoverable budget | Final workflow behavior |
|---|---:|---:|---|
| HTTP 400–499 except 429 | no transport retry | yes | Cross the workflow boundary after the bounded budget, then pause/resume |
| HTTP 429 | yes | no | Use `transport_max_retries`, then pause/resume |
| HTTP 5xx | yes | no | Use `transport_max_retries`, then pause/resume |
| timeout/connection/transient transport error | yes | no | Use `transport_max_retries`, then pause/resume |
| cancellation/keyboard interrupt | no | no | Preserve cancellation semantics; do not retry |
| local programming/invariant error | no | no | Propagate and checkpoint according to existing failure policy |

An implementation MAY narrow the irrecoverable budget to a provider error
explicitly marked safe to re-attempt, but it MUST not silently route a 400
through `transport_max_retries`, and it MUST document the resulting category.
For the initial implementation, all provider-originated permanent 4xx errors
already recognized by `_is_permanent_error` are eligible for the new bounded
budget. This includes the example unsupported-model response.

Retrying an irrecoverable error is a product-level recovery attempt, not a
claim that the request is expected to succeed. The retry loop MUST:

- be bounded by the configured count;
- honor cancellation promptly;
- avoid a retry if the workflow owner has been stopped or the session lease is
  lost;
- avoid duplicating a user message, assistant tool call, tool result, or
  completed side effect;
- preserve the latest safe provider-step checkpoint before each retry;
- use a bounded delay policy and configurable/provider `retry_after` hints only
  when the exception supplies them;
- not multiply attempts unexpectedly through SDK retries, turn retries, and
  workflow continuation loops.

### 6.3 Attempt identity and diagnostics

Each logical provider failure recovery sequence MUST have a stable bounded
`recovery_id` or equivalent journal identity. Every retry event includes only:

- recovery ID;
- workflow run ID and phase name when available;
- error category (`irrecoverable_provider`);
- retry number and configured retry limit;
- HTTP status when available;
- bounded delay and whether a retry is scheduled.

The TUI should render a compact message such as:

```text
⚠ Provider rejected the request (HTTP 400); retrying 1/5 …
```

At exhaustion it should render one final actionable message such as:

```text
✗ Provider request still rejected after 5 retries.
  Workflow paused at ARCHITECTURE and can be resumed after fixing the model
  or provider configuration.
```

The full exception chain belongs in debug logging subject to existing secret
redaction. The scroll appender MUST not render the same nested SDK exception
once per layer.

### 6.4 Transaction safety and phase ownership

The retry boundary MUST be below the workflow phase transition boundary and
above the individual provider request. A retry MUST NOT:

- invoke the phase transition tool again merely because the provider failed;
- call a phase's `run()` entry point again;
- regenerate or delete committed research, source, screenshot, or build
  artefacts;
- repeat a completed `run_command`, `write_file`, or other mutating tool call;
- change the typed phase state or advance the phase cursor.

Before each retry, the agent turn must restore the latest committed provider
step, promote or consult existing idempotency receipts, and retain the active
conversation prefix. If the installed lauren-ai version does not expose
step-level recovery, the compatibility path must use the existing journal and
idempotency ledger rather than rolling back useful completed work.

### 6.5 Exhaustion and durable resumability

When the irrecoverable retry budget is exhausted:

1. The agent turn emits one terminal failure event and propagates a typed
   provider failure to the workflow owner. This propagation is required even
   though the provider error was classified as non-retryable by the transient
   transport policy; non-retryable does not mean swallowed or terminally
   discarded.
2. The owner calls the idempotent workflow failure finalizer exactly once.
3. The checkpoint is written with `paused`/recoverable lifecycle, not ordinary
   terminal `failed`, when a valid typed context and checkpoint store exist.
4. The checkpoint contains the same `run_id`, `conversation_id`, workflow
   topology/profile identity, active phase, phase index, phase iteration,
   context, phase history, artefact references, approvals, committed-step
   receipts, failure category, and last provider diagnostic.
5. Ownership is released after persistence, while a stale or live owner cannot
   create a second checkpoint.
6. `--continue`, `--resume`, `/workflow resume`, and session selection use the
   persisted recovery record and call `resume(context)`.
7. Resume begins at the saved phase and does not ask the agent to repeat an
   already completed phase transition.

If checkpoint persistence itself fails, the user must receive a distinct,
actionable diagnostic. The implementation must never report that the workflow
is resumable unless the checkpoint was durably accepted.

### 6.6 `ask_user` default and timeout propagation

Change the default in every authoritative path to:

```toml
[tools]
question_timeout_s = 300.0
```

The implementation MUST update:

- `ToolSettings` and validation defaults;
- TOML parsing and fallback values;
- `ApprovalService` constructor defaults;
- TUI/session construction;
- background and headless adapters;
- generated config templates and examples;
- `ask_user` result examples and user documentation;
- tests that assert the default.

Explicit values continue to override the default. Values remain finite and
greater than zero; an unbounded value is not introduced by this PRD. A timeout
result MUST report the effective configured value, for example:

```json
{
  "timed_out": true,
  "decision_required": true,
  "timeout_s": 300.0
}
```

The timeout is owned by `ApprovalService`, not implemented as a second timer
in a workflow phase or generated workflow. TUI, background, headless, and
resumed sessions use the same deadline and terminal-state arbitration from
PRD-192. Late answers cannot resolve a later request. Timeout never grants
approval and remains distinct from cancellation.

### 6.7 Generated and custom workflows

The shared runner and checkpoint owner must provide the retry and resume
contract automatically to workflows created by `create_workflow`. Generated
workflow prompts and validation guidance MUST tell agents:

- provider errors may be retried by the runner under the configured budget;
- phase code must not implement a second provider retry loop;
- phase transitions remain tool-call-only;
- artefacts and phase summaries must remain durable before transition;
- an error-paused checkpoint resumes the current phase rather than `INIT`;
- `ask_user` timeout results require a stated best-effort assumption and never
  authorize unsafe side effects.

Custom workflows may add domain-specific error text, but they must not bypass
the shared turn runner, checkpoint finalizer, or policy gates.

## 7. Data flow

### 7.1 Irrecoverable provider failure

```text
user/session input
    │
    ▼
WorkflowRunHandle + typed workflow context
    │
    ▼
AgentTurnRunner._stream()
    │
    ├─ provider request ── HTTP 400 / permanent provider error
    │                         │
    │                         ├─ classify as irrecoverable_provider
    │                         ├─ journal failure + committed-step cursor
    │                         ├─ restore latest safe request boundary
    │                         └─ retry while recovery_attempt <= configured 5
    │
    ├─ success → continue same agent turn and phase
    │
    └─ exhaustion
          │
          ▼
    typed provider failure
          │
          ▼
    WorkflowRunHandle.finalize_failure() exactly once
          │
          ├─ durable checkpoint: paused, same run/phase/context/conversation
          └─ release owner + emit one resumable diagnostic
          │
          ▼
    --resume / --continue / workflow resume
          │
          ▼
    rehydrate checkpoint → resume(context) at saved phase
```

The retry sequence never routes through `run(intent)` and never creates a new
manifest solely because a provider rejected one turn. The exhausted error then
crosses the workflow boundary exactly once, where the workflow owner persists
the paused checkpoint. This remains true for both errors that were never
eligible for transient transport retry and errors that exhausted the transient
retry budget.

### 7.2 `ask_user` question

```text
agent calls ask_user
    │
    ▼
ApprovalService creates request + deadline = now + effective timeout
    │
    ├─ user answers before deadline → answered result
    ├─ user cancels/session stops → cancelled result
    └─ 300-second default deadline expires → timed_out result
            │
            ├─ clear pending request exactly once
            ├─ close overlay/adapter
            └─ return result to same agent turn
                    │
                    ▼
            agent states assumption and continues or rejects safely
```

## 8. Acceptance criteria

### Provider recovery and resume

| ID | Acceptance criterion |
|---|---|
| 198.1 | Omitting the new setting resolves to `irrecoverable_error_max_retries = 5`. |
| 198.2 | A configured value counts retries after the initial attempt; value `0` performs exactly one provider attempt. |
| 198.3 | Negative, boolean, fractional, and over-ceiling values are rejected with a path-specific configuration error. |
| 198.4 | An HTTP 400 unsupported-model error is classified as irrecoverable and uses the new budget, not `transport_max_retries`. |
| 198.5 | A sequence of five irrecoverable retries produces at most six total provider attempts and then one terminal exhaustion outcome. |
| 198.6 | A transient 429/5xx/timeout continues to use the existing transport retry budget and is not multiplied by the irrecoverable budget. |
| 198.7 | Cancellation during irrecoverable backoff stops promptly and does not schedule another attempt. |
| 198.8 | A retry never duplicates committed assistant messages, tool results, or successful mutating tool effects. |
| 198.9 | Retry diagnostics contain bounded attempt metadata and the TUI does not show duplicate full nested SDK errors. |
| 198.10 | After exhaustion, a typed workflow with checkpoint support is saved as one paused, resumable run at the same phase and phase index. |
| 198.11 | Resume after correcting the provider/model uses the same run, conversation, typed context, artefacts, and phase cursor; it does not rerun `INIT`. |
| 198.12 | Checkpoint failure is reported distinctly and the UI never claims the run is resumable when persistence failed. |
| 198.13 | Direct turns, code-plan, default workflows, generated workflows, subagents, TUI, headless mode, and process restart share the same policy or explicitly document an equivalent adapter. |
| 198.14 | Repeated workflow finalization, duplicate error events, and stale owner attempts are idempotently suppressed. |
| 198.14a | A provider error classified as non-retryable still propagates after its bounded recovery attempts; no phase loop swallows it or turns it into a fresh `INIT` run before checkpoint finalization. |

### `ask_user` timeout

| ID | Acceptance criterion |
|---|---|
| 198.15 | `ToolSettings().question_timeout_s`, config parsing, `ApprovalService`, and all adapters default to `300.0`. |
| 198.16 | An explicit TOML or `--set tools.question_timeout_s=...` value overrides 300 seconds everywhere. |
| 198.17 | The effective timeout result reports the configured value and remains distinct from cancellation and approval. |
| 198.18 | The default wait expires after approximately 300 seconds using a deterministic clock in tests; no second workflow-specific timer exists. |
| 198.19 | A late answer cannot resolve an expired request or a later request, and pending state/overlay cleanup happens exactly once. |
| 198.20 | Timeout never approves a tool, changes runtime mode, bypasses policy, or performs a phase transition. |
| 198.21 | Generated workflows receive the shared timeout result and are instructed to state a safe assumption rather than repeat the same question indefinitely. |
| 198.22 | Documentation, generated templates, examples, and user-visible result text no longer describe 60 seconds as the default. |

## 9. Testing requirements

### Unit tests

Add isolated tests for:

- configuration default, parsing, `--set`, validation, redaction, and upper
  bound of `irrecoverable_error_max_retries`;
- retry-count semantics (`0`, `1`, and default `5`);
- permanent 400 classification versus transient 429/5xx/timeout classification;
- cancellation and backoff interruption;
- stable recovery ID and bounded diagnostic payload;
- duplicate event suppression and finalization idempotency;
- journal/checkpoint metadata and exact active-phase preservation;
- all 300-second `ask_user` default/fallback constructors;
- explicit timeout overrides and timeout-result serialization;
- timeout/answer/cancellation races and stale response rejection.

### Integration tests

Use temporary durable stores and fake provider transports to verify:

- a workflow completes artefacts, receives a permanent 400, retries according
  to configuration, and persists one paused checkpoint after exhaustion;
- correcting the model/profile and resuming rehydrates the same context and
  phase without re-running earlier transitions or tools;
- transient errors remain governed by `transport_max_retries`;
- journal recovery after process restart retains the same run and conversation;
- TUI, background, and headless question adapters share the 300-second
  effective default and cleanup contract;
- generated/custom workflows inherit the shared runner policy.

### End-to-end tests

Cover these user journeys:

1. Start a multi-phase workflow, force an unsupported model response, observe
   bounded retry messages, terminate at exhaustion, correct configuration, and
   resume at the interrupted phase.
2. Repeat the same journey with `--continue` and session selection, proving no
   fresh `INIT` run is created.
3. Configure retry count `0` and verify immediate checkpointing after the first
   permanent error.
4. Leave `ask_user` unanswered under a shortened test clock, verify the
   structured timeout result, and confirm the agent continues only with an
   explicit assumption.
5. Answer just before the deadline and verify the answer wins; submit a late
   answer and verify it is rejected without affecting a later request.

Tests must use fake clocks/transports, temporary workspaces, and deterministic
delays. No real provider calls or five-minute sleeps belong in CI.

## 10. Observability, security, and performance

- Retry and checkpoint events use bounded IDs and redacted metadata. Never
  include API keys, authorization headers, question answers, full prompts, or
  arbitrary provider response bodies in scroll events.
- Error category, status, retry count, final disposition, workflow name, phase,
  and run ID are sufficient for metrics and logs; exception chains remain in
  debug logs under existing redaction rules.
- The maximum additional wait is finite: configured retries plus bounded
  backoff and provider request timeout. A configuration ceiling prevents an
  accidental denial-of-service loop.
- Retry state is session/run scoped and cannot be reused by another owner or
  conversation.
- A resumed run reuses its existing policy snapshot unless the documented
  configuration-reload path explicitly changes it. Changing configuration
  must not invalidate checkpoint topology or erase artefacts.
- `ask_user` has a five-minute default but still has a finite deadline, clears
  pending state on every terminal outcome, and cannot grant permissions by
  timing out.

## 11. Rollout and compatibility

1. Add the new setting with default `5` while leaving existing transient retry
   settings unchanged.
2. Deploy retry classification/diagnostics and checkpoint tests before enabling
   the default budget in production.
3. Change all `ask_user` default fallbacks atomically to `300.0`; preserve
   explicit existing values such as 60 seconds.
4. Existing checkpoints must remain readable. They have no irrecoverable retry
   counter, so a resumed run starts a new bounded recovery sequence for its
   current provider request without changing its phase cursor.
5. If a deployment rolls back, old runtimes may ignore the new setting but must
   continue to read existing checkpoint fields and preserve the run. The
   migration must not require rewriting historical checkpoints.

## 12. Implementation notes and assumptions

- “Retries” means attempts after the initial provider request. This PRD
  deliberately states that `5` means up to six total provider attempts.
- “Irrecoverable” describes the current request classification, not guaranteed
  permanent failure of the provider configuration. The bounded budget exists
  for operator-controlled recovery and must never become infinite.
- PRD-196 remains authoritative for resumability after a permanent provider
  error. This PRD adds the pre-checkpoint recovery budget and does not replace
  PRD-196's paused-checkpoint lifecycle.
- PRD-192 remains authoritative for question state, races, fallback decisions,
  and policy boundaries. This PRD only changes its default timeout from 60 to
  300 seconds and requires every construction path to agree.
- The canonical setting name is proposed as
  `execution.irrecoverable_error_max_retries`; implementation may choose a
  different name only if the migration, docs, and acceptance tests preserve
  the same semantics and avoid ambiguity with `transport_max_retries`.
- The initial scope treats provider-originated permanent 4xx responses as
  eligible for the bounded budget. Local programming errors remain outside the
  budget unless a later PRD explicitly classifies them.

## 13. Verification commands

The implementation is complete only when the relevant checks pass:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
uv run mkdocs build --strict
```

The implementation record must link the focused retry and question-timeout
tests, the full-suite result, and any environment limitation. The PRD remains
the design record; current user documentation must describe the implemented
configuration and defaults.

## 14. Implementation record

Implemented in the current repository:

- `ExecutionSettings.irrecoverable_error_max_retries` defaults to `5`, is
  strictly validated, is exposed through TOML/CLI configuration, and is
  threaded through foreground, workflow, and subagent turn retries.
- Provider-originated permanent HTTP 4xx errors use a separate bounded retry
  path. Exhaustion still propagates the original error to the workflow owner;
  existing checkpoint finalization therefore preserves the same paused run and
  active phase. Local context/integrity errors remain immediately propagated.
- Provider recovery notices use bounded structured scroll events and terminal
  error event IDs prevent one propagated failure from being rendered twice by
  the agent-turn and workflow/TUI boundaries.
- `DEFAULT_QUESTION_TIMEOUT_S` is now `300.0` and is used by the typed config,
  `ApprovalService`, and background approval adapter. Explicit overrides remain
  unchanged.
- README, configuration/troubleshooting guides, fact-base, generated config
  surfaces, and `llms-full.txt` document the new contract.

Verification evidence:

```text
3851 passed, 15 skipped — uv run pytest tests/ -q
243 passed — uv run pytest tests/integration -q
130 passed, 1 skipped — uv run pytest tests/e2e -q
Type audit OK — uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
llms_check OK — uv run nox -s llms_check
mkdocs build --strict completed successfully
```

The repository-wide mypy command still reports pre-existing errors in
`runners/process_lease.py`, `workflows/name_that_ui.py`, and
`tools/tui_screenshot.py`; no new errors were reported in the files changed by
this PRD. The lint session's Ruff check passes; its format check still reports
nine unrelated pre-existing files requiring formatting.
