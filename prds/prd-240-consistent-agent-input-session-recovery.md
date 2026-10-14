---
title: "PRD-240: Reliable Agents-Page Input — Provider Identity and Turn Recovery"
status: Implemented
version: 1.0.0
date: 2026-10-14
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-129, PRD-150, PRD-169, PRD-170, PRD-182, PRD-187, PRD-215, PRD-216, PRD-217]
tags: [agents-manager, background-sessions, input, provider-errors, recovery]
---

# PRD-240 — Reliable Agents-Page Input: Provider Identity and Turn Recovery

## 1. Executive summary

An operator reports that sending input to a session from `agenthicc agents`
produces this provider response:

```text
Provider request rejected retry 5/5 in 18.6s
HTTP 400
Request is missing x-opencode-session and cannot be routed efficiently.
ERROR Provider session identity is missing
Configure [execution].session_header = "x-opencode-session" or
providers.<profile>.session_header and restart the session.
```

The TUI then reports that the provider requires `x-opencode-session` and
recommends configuring `[execution].session_header` or
`providers.<profile>.session_header`. This is direct evidence that the
effective OpenCode Go request lacks its required stable session-identity
header. The input submission triggers a provider request; it does not, by
itself, show that the submitted text corrupted the conversation or that an
assistant/tool exchange was malformed. OpenCode Go documents the session
header requirement in its [client guidance](https://opencode.ai/docs/go/#where-can-i-use-it).

There is a separate, source-verified recovery mismatch worth fixing in the same
input path: session construction can create a `ResumePlan` for an interrupted
direct turn, but the background worker currently drains deferred input before
using that plan. This is an independent correctness risk and may cause other
provider failures, but it is **not established as the cause of the specific
`MissingSessionID` response above**. The implementation must address both
issues separately and preserve that distinction in diagnostics and tests.

The observed `Provider request rejected retry 5/5` line is also significant.
The transport retry policy distinguishes transient network retries from
irrecoverable-provider retries, but the latter currently have a separate
configurable budget (`irrecoverable_error_max_retries`, default 5). A known,
deterministic missing-header configuration error should not make five
identical provider requests before displaying the already-actionable
diagnostic.

## 2. User story and desired behavior

> As an operator, I can send input to a recoverable session from `agenthicc
> agents` and have it continue reliably, without attaching to the session.
> The background owner repairs or resumes any interrupted direct turn first,
> then delivers my exact input once through that same conversation.

When there is no interrupted direct turn, existing input-first recovery
behavior remains: the accepted message is handled before the old launch intent
is considered. Input text remains opaque and target-owned as required by
PRD-217; this feature changes execution ordering only when durable recovery
evidence says a direct turn was interrupted.

## 3. Current-source investigation

### 3.1 Shared session reconstruction

Both entry points call `src/agenthicc/runners/tui_session.py::_build_session_context`
with the selected session ID. It opens the same session conversation and
reconstructs the provider transport from the effective configuration. The
provider's dynamic session header is built from that stable session ID when a
session-header name is configured. For the official OpenCode Go endpoint, the
runtime automatically selects `x-opencode-session`; custom gateways remain
opt-in. The context builder also calls `RunCoordinator.detect_incomplete_turn()` and
`RunCoordinator.build_resume_plan()` and returns the result as
`SessionContext.pending_resume`.

### 3.2 `--continue` recovery path

The interactive TUI starts the processor, then calls
`TUISession._maybe_resume_interrupted_turn()`. If a direct turn is pending and
there is no workflow recovery that should take precedence, it starts
`agent_task_body()` with the interrupted turn's original user message and
`ResumePlan`. `run_turn()` forwards `resume_turn_id` and `resume_ledger` into
`_run_agent_turn()`. The stable turn ID aligns with journal records, and the
ledger replays completed tools instead of repeating their side effects.
Newly submitted text is queued while this recovery runs and is offered to the
agent at a safe tool boundary or when the resumed turn has finished.

### 3.3 `agenthicc agents` recovery path

For a stale/recoverable session, `BackgroundSupervisor.enqueue_input_or_recover()`
first writes the exact text to the durable input inbox, then starts a worker
attempt for the same session. `run_worker()` builds a headless session context,
so `pending_resume` may be present. However, the worker's deferred-input branch
calls `_drain_idle_inputs()` before ordinary workflow/direct execution. That
drain claims the deferred item, records it as a user message, and calls
`_run_direct_turn()` with the new input as `request.intent`. `_run_direct_turn()`
does not accept or forward `resume_turn_id` or `resume_ledger`.

That omission changes more than the text order: without a resume plan,
`AgentTurnRunner` assigns a new turn ID and creates the normal ledger for that
new turn. With a plan, it reuses the interrupted turn ID and seeds the ledger
from already completed tool records. Therefore a new background input can be
serialized against the rehydrated partial history as an unrelated turn instead
of closing the original provider/tool exchange with its recorded idempotency
state.

Thus the two entry points share session construction and provider identity but
do not share interrupted-turn execution:

```text
agenthicc --continue
  -> pending ResumePlan
  -> original interrupted turn + turn ID + idempotency ledger
  -> queued new input at a safe boundary / after recovery

agenthicc agents, recoverable session
  -> durable deferred input
  -> new input is claimed first
  -> fresh direct turn, with no ResumePlan arguments
```

This mismatch means the background path does not close the interrupted logical
turn with its original turn ID and durable idempotency ledger before handling
new input. That creates a continuity and side-effect-replay risk, even though
the journaled-memory constructor repairs some dangling tool-call tails before
the provider sees them. PRD-169 documents the separate transaction invariant
for assistant/tool exchanges. Nothing in the supplied error establishes that
the specific OpenCode Go request contained malformed tool history; its
structured cause names the missing session header.

### 3.4 Confirmed cause and separate recovery risk

For this reproduction, the provider supplied a structured cause:
`MissingSessionID`, specifically the absence of `x-opencode-session`. Agenthicc
already owns the stable conversation/session ID, but its generic dynamic
session-header feature was opt-in and the official OpenCode Go endpoint had no
provider-specific default. The correct fix is to automatically bind
`x-opencode-session` to that existing ID for the official endpoint—not ask the
user to restate an ID Agenthicc already knows, rewrite conversation history,
or retry the same request.

The original diagnostic was actionable but arrived only after the provider
rejection path ran. The reported `retry 5/5` is not the transient transport
retry loop: `AgentTurnRunner` also enables bounded retries for selected
irrecoverable provider failures. That second policy is useful only when a new
attempt can plausibly change the outcome. A missing required header is
deterministic, and retrying identical requests cannot fix it. The runtime must
recognize this structured error, stop immediately, and explain that the
official endpoint should already receive the Agenthicc session ID; if it does
not, the operator should check endpoint and proxy header preservation.

Separately, source inspection confirms that `run_worker()` ignores a pending
direct-turn `ResumePlan` and processes deferred input first, whereas the TUI
resumes that turn with its original ID and durable ledger. This remains a
separate defect to fix: it can expose an interrupted conversation to an
unrelated next request, but the evidence in this report does not attribute the
OpenCode Go `MissingSessionID` response to that sequencing mismatch. Generic
HTTP 400 responses must be diagnosed from their structured provider error
type/code rather than assumed to be either a header issue or a conversation
protocol issue.

### 3.5 Coverage gap

Existing tests cover durable input inbox behavior, stale-session worker
recovery, exact payload preservation, and foreground mid-turn recovery
separately. Existing mid-turn E2E coverage does not combine an interrupted
turn journal, an `agents` deferred input, background worker rehydration, and a
provider fixture that verifies original-turn identity and input ordering. There
is no parity test proving that the two entry points consume the same recovery
plan before a new message reaches the provider.

## 4. Goals

1. Ensure OpenCode Go requests from foreground, `--continue`, and background
   worker entry points automatically include `x-opencode-session` bound to the
   stable Agenthicc conversation/session ID, without requiring duplicate user
   configuration.
2. Stop immediately on a structured `MissingSessionID` configuration failure
   instead of spending the irrecoverable-provider retry budget on identical
   requests.
3. Make the background worker honor a pending direct-turn `ResumePlan` before
   it begins a new provider turn from deferred user input.
4. Reuse the same turn identity, committed-step state, and durable tool
   idempotency ledger used by the interactive `--continue` path.
5. Preserve `agenthicc agents` as a background-only operation; do not attach,
   transfer ownership to the manager, or start a second session.
6. Preserve exact inbox text, stable message IDs, FIFO ordering, and at-most-once
   delivery while deferring new input until recovery reaches a safe boundary.
7. Keep the existing input-first behavior for recoverable sessions with no
   pending interrupted direct turn; never replay the original launch intent
   just because no resume plan exists.
8. Make provider failures diagnosable without logging prompt bodies,
   credentials, or full serialized requests.
9. Keep explicit workflow recovery separate from direct-turn recovery. A
   pending workflow checkpoint must not be auto-started by this change.

## 5. Non-goals

- Retrying a structurally invalid HTTP 400 request with identical bytes.
- Changing provider retry eligibility for HTTP status codes.
- Reinterpreting `agenthicc agents` input, including ordinary text or slash
  commands; PRD-217 remains authoritative for the opaque-input contract.
- Replaying the original background launch intent when a new input was
  accepted.
- Automatically resuming a workflow checkpoint just because a direct-turn
  recovery plan exists.
- Hiding, rewriting, or downgrading genuine provider errors.
- Adding a second conversation, event journal, input inbox, or ownership store.

## 6. Functional requirements

### FR-1 — Shared recovery decision

The background worker must inspect the canonical `SessionContext.pending_resume`
or invoke the same `RunCoordinator` abstraction used by the TUI. It must not
independently infer incomplete turns from session status, provider error text,
or message-body content.

### FR-2 — Resume the interrupted turn before new direct input

When a direct-turn recovery plan exists and the session is eligible for direct
turn recovery, the worker must run the plan's original user message using its
original `turn_id` and `DurableIdempotencyLedger`. It must not claim or append
the newly deferred user message as a provider-facing user turn before the
recovery plan reaches a valid boundary.

The accepted inbox item remains durable and pending during recovery. Once the
interrupted turn is resumed successfully, the exact user input is delivered
once at the agent's supported safe message boundary or as the next normal
direct turn. The worker must not change the item text or message ID.

### FR-3 — Preserve recovery semantics on failure

If recovery fails before the queued message is delivered, the inbox entry must
remain recoverable and must not be marked delivered or completed. A later
attempt must reuse the same recovery turn ID/ledger state and input message ID.
Completed tool effects must not run twice. If the provider returns another
400, the attempt records the bounded error diagnostics and keeps the session
recoverable where the existing lifecycle policy permits.

### FR-4 — No-pending-resume behavior

If there is no interrupted direct-turn plan, existing PRD-216/217 behavior is
preserved: accepted deferred input is processed before the original launch
intent, and that original intent is not replayed first or substituted for the
input. This path must remain unchanged for completed sessions that are starting
a genuinely new input-driven turn.

### FR-5 — Workflow recovery separation

An in-progress workflow checkpoint is not a direct-turn resume plan. The
worker must preserve existing explicit workflow-selection/resume rules and
must not auto-run a workflow as part of fixing direct-turn recovery. If a
workflow-specific recovery record and a direct-turn journal marker conflict,
the system must follow the existing workflow recovery ownership rule or stop
with an actionable recoverable diagnostic; it must not run both recovery paths
concurrently.

### FR-6 — Stable OpenCode Go session identity

When the official OpenCode Go endpoint is selected, every provider request for
that session must include the automatically bound `x-opencode-session` header
bound to the stable `conversation_id`/session ID. The value must not be the
background run ID, worker attempt, or inbox message ID. The foreground TUI,
`--continue`, background recovery, and auxiliary agent requests must use the
same identity.

This is the narrow provider-specific exception to PRD-187's generic opt-in
contract: the well-known official OpenCode Go endpoint automatically selects
`x-opencode-session`, because Agenthicc already has the stable ID required as
its value. An explicitly configured matching name is accepted; a conflicting
dynamic header or static `x-opencode-session` value fails preflight rather
than sending an incorrect identity. Custom gateways retain the existing
opt-in `session_header` behavior, and unrelated provider profiles receive no
new header. The background worker continues to use its persisted launch
configuration, and diagnostics identify the effective profile and model
without secrets. If OpenCode Go still returns `MissingSessionID`, the error
points to endpoint/proxy header preservation rather than asking the user to
repeat configuration that should be automatic.

### FR-7 — Safe provider error projection

Persist/display bounded diagnostic metadata sufficient to distinguish at
least:

- provider and model identifiers;
- HTTP status and provider error type/code;
- provider request/correlation ID when present;
- session ID, background attempt, interrupted turn ID, and input message ID;
- whether the failure occurred while recovering a prior turn or processing
  the new input; and
- effective profile/config revision or a safe fingerprint.

Do not persist or render API keys, authorization values, full request bodies,
system prompts, or unrelated conversation content in generic status surfaces.
Use the existing redaction policy. Full provider detail remains available only
through existing protected diagnostics where that contract permits it.

### FR-8 — Correct 400 retry behavior

The structured `MissingSessionID` error is a deterministic configuration
failure: stop immediately, emit the existing bounded actionable diagnostic,
and do not consume `irrecoverable_error_max_retries`. Do not submit the same
request again unless the request has been corrected with the required header.
Transient statuses such as 429 continue to use the transient retry policy.
Other provider errors retain their existing bounded classification policy
unless their structured type/code is known to be deterministic. Resuming an
interrupted turn may change the conversation request correctly, but it must
not be used to disguise or retry a request that is missing required provider
identity.

## 7. Proposed implementation

1. Keep `RunCoordinator` and the conversation journal as the authority for
   interrupted-turn detection and resume-plan construction.
2. Extend PRD-187's dynamic session-header binding with one narrow default:
   the official OpenCode Go endpoint automatically selects
   `x-opencode-session` and binds it to the stable conversation ID. Reject a
   conflicting explicit header or static value before dispatch. Do not attach
   this header to custom gateways or unrelated endpoints unless their existing
   opt-in `session_header` setting requests it. Use the canonical
   provider-transport builder so TUI, headless, worker, and auxiliary calls
   cannot diverge.
3. Add a structured classifier for `MissingSessionID` and ensure it bypasses
   the irrecoverable-provider retry loop while preserving the current redacted
   user diagnostic. Do not classify every HTTP 400 as this error.
4. Introduce a small shared session-execution helper, or extend the existing
   direct-turn helper, to execute either a normal input or a `ResumePlan`
   through `_run_agent_turn`. The resume path must pass
   `resume_turn_id=plan.turn_id` and `resume_ledger=plan.ledger`, matching the
   TUI's `run_turn()` call. Avoid duplicating journal-folding rules in
   `background/worker.py`.
5. In `run_worker`, inspect `session.pending_resume` before the branch that
   drains deferred inbox messages. When eligible, resume the interrupted
   direct turn first under the current worker's normal session/attempt lease.
6. Bind the existing `QueuedMessageSource` during resumed execution so queued
   ordinary input can be claimed at the same safe boundary as in the TUI. If
   no safe boundary consumes it, drain it after the resumed turn completes.
   Existing slash-command routing and PRD-217 target-side semantics remain
   unchanged.
7. Ensure the order of durable operations is crash-safe: the pending input
   remains accepted/unconsumed until its actual delivery boundary; replayed
   tool results are tied to the original turn ID; turn recovery markers are
   written only when the resumed execution reaches the existing completion
   boundary.
8. Add redacted provider diagnostics that preserve upstream 400 type/code and
   correlation ID while correlating provider failure to recovery phase and
   target input message. Do not log serialized messages or credentials.
9. Leave all other non-recoverable errors surfaced as errors. Add a targeted
   remediation only when the structured error category proves a known local
   cause, rather than inferring from status 400 alone.

## 8. Persistence, ordering, and ownership invariants

- The selected session ID and its existing conversation journal remain the
  single conversation identity across `agents` recovery and `--continue`.
- A new input is not a replacement for the interrupted turn's original user
  message. The interrupted turn keeps its original turn ID.
- An accepted input message has one stable message ID, remains FIFO, and is
  delivered at most once.
- Provider-facing conversation order is valid at every request boundary; an
  unfinished assistant/tool exchange is resumed or repaired by the canonical
  recovery logic before another unrelated user turn is appended.
- The background worker remains the sole owner while running. `agenthicc
  agents` never attaches to the foreground.
- If recovery cannot establish a valid provider conversation, do not consume or
  discard the pending input. Surface a recoverable failure rather than
  submitting a knowingly malformed history.
- Existing idempotency-ledger records remain authoritative for prior tool side
  effects. New input must not seed or overwrite the old turn's ledger.

## 9. Acceptance criteria

1. With a journal containing an interrupted direct turn and at least one
   committed provider/tool step, sending input from `agenthicc agents` first
   resumes the original turn using its existing turn ID and durable ledger.
2. The background worker does not send the new input as a fresh provider turn
   before that recovery plan reaches a valid boundary.
3. With an OpenCode Go profile that omits `session_header`, the fake provider
   receives the stable Agenthicc session ID in `x-opencode-session` and accepts
   the request. An explicitly conflicting dynamic or static header fails
   preflight before provider dispatch. If a provider still returns structured `MissingSessionID`, no
   irrecoverable-provider retry is scheduled (one original request, zero
   retries).
4. A separate fake provider records the interrupted turn and subsequent input.
   It verifies that background recovery re-drives the original turn before
   delivering the new input, with no duplicate tool effect. This verifies the
   independent recovery-ordering fix, not the cause of the reported
   `MissingSessionID` response.
5. Sending the same session through `agenthicc --continue` and through
   `agenthicc agents` uses the same `RunCoordinator` recovery plan semantics.
6. The exact accepted input and stable message ID are delivered once, after
   recovery, and the final transcript/inbox receipt can be correlated.
7. A crash after recovery starts but before queued input delivery preserves
   both the recoverable turn and the accepted message; a restart resumes them
   without duplicate user turns or tool effects.
8. With no interrupted direct turn, a deferred input remains the first new
   turn and the original launch intent is not replayed.
9. Workflow checkpoints are not auto-resumed by this behavior, and
   `/workflow ...` remains target-session input governed by PRD-217.
10. The effective OpenCode Go profile/model and header name are observable in
    redacted diagnostics for both entry points; no credential or header value
    is persisted or displayed. The header value itself is the stable session
    identity and is covered by transport-level tests, not exposed in generic
    status projections.
11. A generic provider 400 is not misclassified as `MissingSessionID`; its
    bounded, redacted status/type/code/request ID and failing phase are
    surfaced under the existing provider retry policy.
12. Existing live-worker inbox delivery, ask-user responses, completed-session
    input-first behavior, deleted-session rejection, and owner-attempt fencing
    remain unchanged.

## 10. Test plan

### Unit tests

- Build a resume plan from a journal containing committed and interrupted
  provider steps; assert stable turn ID, last committed step, and seeded
  idempotency ledger.
- Verify background execution passes the exact plan into `_run_agent_turn`.
- Verify a pending inbox message is not claimed or marked delivered before the
  prior turn's safe boundary.
- Verify no-pending-resume input retains current input-first behavior.
- Verify workflow checkpoint state does not trigger automatic workflow resume.
- Verify the official OpenCode Go endpoint automatically binds the stable
  conversation ID to `x-opencode-session` when the setting is omitted; reject
  conflicting dynamic/static configuration before dispatch.
- Verify structured `MissingSessionID` stops before any irrecoverable-provider
  retry and emits one actionable, redacted error.
- Verify 400 diagnostics redact credentials and prompt/message content while
  retaining status, safe error code/type, request ID, profile/model, and phase.
- Verify unrelated provider 400 errors are not labeled `MissingSessionID` and
  retain the configured provider-error policy.

### Integration tests

- Rehydrate a real temporary `SessionConversation` and `BackgroundStore`,
  submit deferred input through the supervisor, run the background worker with
  a deterministic agent/provider double, and inspect provider-visible message
  order, journal records, inbox receipts, and tool ledger.
- Assert that `RunCoordinator` plan, input message ID, session ID, and
  conversation ID stay stable across a worker restart.
- Run a control fixture through the `--continue` lifecycle and assert parity
  with the background worker's recovery plan.
- Configure a local fake OpenAI-compatible endpoint that requires
  `x-opencode-session`. Verify the official endpoint binding sends the stable
  Agenthicc session ID without an explicit setting. Also exercise a structured
  `MissingSessionID` response and assert it gets zero irrecoverable retries.
- Separately configure a fake endpoint that records the original interrupted
  turn and subsequent input. Assert the original turn is re-driven first with
  its stable turn ID, and the exact new input follows only after recovery; no
  duplicate tool effect occurs. This verifies recovery ordering without
  assuming that the reported missing-header response came from bad message
  history. No paid provider calls are allowed.
- Add a separate generic 400 fixture to prove that an unrelated invalid model
  option is reported as a provider/config error, not falsely attributed to
  interrupted-turn recovery.
- Exercise explicit profile-level and execution-level custom header settings,
  automatic official OpenCode Go binding, conflicting OpenCode Go config, and a
  non-OpenCode provider with no header to prove the compatibility boundary.

### End-to-end tests

- Seed a durable session with an interrupted direct turn and committed tool
  result, mark its background attempt recoverable, open the same record through
  the `agents` manager, and send a new message. Verify recovery completes and
  input is then delivered exactly once, without foreground attachment.
- Start a second fresh process to simulate restart between accepted input and
  recovery. Verify the same turn and input message IDs are recovered.
- Compare against `agenthicc --continue` using the same fake provider fixture.
- Verify completed/no-incomplete-turn input remains input-first and does not
  replay the original launch intent.
- Verify the provider receives the Agenthicc session ID without an explicit
  header setting, and a structured `MissingSessionID` response results in zero
  irrecoverable retries with an endpoint/proxy-preservation diagnostic.
- Verify a different provider-shaped HTTP 400 remains visible with bounded
  diagnostics and is not incorrectly presented as a session-header failure.

## 11. Rollout and compatibility

The change is additive to recovery orchestration and requires no durable-store
migration. Existing inbox rows remain valid. On upgrade, a background worker
must derive pending recovery from the existing conversation journal and inbox
records rather than require a new field. If recovery metadata is inconsistent,
fail safely with the input preserved and a diagnostic directing the operator
to `--continue` or the existing recovery controls.

PRD-215, PRD-216, and PRD-217 remain authoritative for targeted input,
completed-session input, exact payload forwarding, and no implicit workflow
interpretation. PRD-129/182 and `RunCoordinator` remain authoritative for
mid-turn replay and tool idempotency. PRD-169 remains authoritative for valid
assistant/tool exchanges. This PRD connects those contracts across the
background-worker entry point; it does not replace them.

## 12. Definition of done

- The exact reported `MissingSessionID` category is reproduced locally; the
  official OpenCode Go endpoint automatically sends the stable Agenthicc
  session ID in `x-opencode-session`, while custom gateways remain opt-in. The
  failure is not attributed to malformed conversation history.
- A separate fake-provider test verifies the interrupted-turn ordering issue
  and proves that its correction does not mask missing-header or other
  configuration failures.
- The deterministic missing-header error stops without consuming the
  irrecoverable-provider retry budget and provides a bounded, actionable
  diagnostic.
- Background input recovery consumes the same pending direct-turn recovery
  plan as `--continue` before dispatching the next user message.
- Provider-visible request ordering is valid, tool effects are idempotent, and
  accepted inbox inputs remain durable until delivered.
- The input-only path without interrupted work remains unchanged.
- Unit, integration, and E2E acceptance tests pass without live provider
  credentials.
- Documentation and the PRD index accurately describe behavior and the
  remaining limitations of provider-specific diagnostics.

## 13. Implementation and verification record

- `config.build_llm_config()` automatically binds the official OpenCode Go
  endpoint's `x-opencode-session` header to the stable conversation ID before
  constructing a transport. Custom endpoints retain PRD-187's opt-in behavior;
  conflicting explicit values fail preflight.
- `AgentTurnRunner` classifies the structured missing-session response as
  non-retryable and emits bounded provider/session/turn/phase metadata without
  exception text or request content. The worker's durable failure projection
  applies the same payload-free policy to HTTP provider errors.
- Background sessions retain the session-construction `ResumePlan`; the worker
  checks for workflow-recovery conflicts, re-drives the interrupted direct
  turn with its original ID and ledger, and only then delivers pending inbox
  input. A failed re-drive leaves a non-terminal recovery marker so another
  attempt can build the same plan.
- Verification: the full suite passed immediately before the automatic
  OpenCode Go header-binding correction (4,290 passed, 16 skipped). Against
  the final correction, the focused provider/retry/resume unit, background-
  session integration, and recovery/OpenCode identity E2E tests passed (94
  passed). A subsequent full-suite rerun was interrupted after 461 passed and
  2 skipped while it was executing lengthy nested tests; no failure was
  observed before interruption, so the full suite has not been verified
  against the exact final diff. `uv run ruff check src/ tests/ scripts/`, the
  changed Python files' Ruff format check, `uv run python scripts/type_audit.py
  --check docs/reference/type-safety-baseline.json`, and
  `uv run mkdocs build --strict` pass.
- `uv run mypy src/agenthicc` remains blocked by existing findings in other
  modules (244 errors across 11 files); no diagnostics are reported for the
  changed source modules. The repository-wide `ruff format --check src/ tests/
  scripts/` also identifies eight untouched files that already need
  formatting; this change does not modify them.
