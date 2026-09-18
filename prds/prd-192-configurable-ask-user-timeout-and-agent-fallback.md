---
title: "PRD-192: Configurable Ask User timeout and best-effort agent fallback"
status: Implemented
version: 1.0.0
date: 2026-09-18
scope: "ask_user interaction waits, timeout configuration, TUI cleanup, and autonomous fallback decisions"
related_prds:
  - PRD-78   # Tool approval and human interaction service
  - PRD-100  # Code-plan architecture and question tool
  - PRD-144  # Resize-safe waiting modals and pause-aware display timing
  - PRD-156  # Resumable plan-mode interrupts and workflow continuation
  - PRD-166  # Terminal-safe active animation rendering
  - PRD-170  # Durable workflow recovery
  - PRD-173  # Recoverable workflow errors and failure checkpoints
  - PRD-179  # Generated workflow phase annotations and checkpoints
  - PRD-189  # Scrollable Ask User Questions
  - PRD-191  # Idempotent interrupted-tool recovery and MCP startup isolation
tags:
  - ask-user
  - questions
  - timeout
  - tui
  - workflows
  - resume
  - fallback
---

# PRD-192 — Configurable Ask User timeout and best-effort agent fallback

## 1. Executive summary

`ask_user` is the shared workflow and agent tool for presenting one or more
focused questions and waiting for answers. It is implemented through the
session-scoped `ApprovalService` and rendered by the TUI `QuestionsOverlay`.
Today, an interactive request waits indefinitely for `ApprovalService.respond()`.
If the user walks away, closes the terminal, loses the session, or misses the
prompt, the agent turn and workflow remain suspended forever.

This PRD adds a configurable question-wait timeout with a default of 60
seconds. When the deadline expires, the question request is closed exactly
once and the agent receives a structured timeout result. The agent, rather
than the UI or a generic tool handler, must then make the best decision it can
from the available context, state the assumption it made, and continue the
turn. A timeout is not an approval, a cancellation, or an arbitrary selection
of the first option.

The implementation must preserve the existing question contract, phase
transition rules, security policy, durable conversation, workflow checkpoints,
and resume semantics. It must also ensure that a late human answer cannot
revive an already timed-out request or be delivered to a later question.

## 2. Evidence and current behavior

The current interaction path is:

```text
agent calls ask_user(questions)
  -> make_questions_tool creates ApprovalRequest(kind="questions")
  -> ApprovalService.request_approval(req)
  -> AppState.pending_approval = req
  -> TUI shows QuestionsOverlay
  -> service awaits req.event.wait() indefinitely
  -> QuestionsOverlay calls service.respond(...)
  -> service returns ApprovalResponse
  -> ask_user returns answers to the agent
```

The relevant ownership boundaries are already established:

| Concern | Current owner | Required change |
| --- | --- | --- |
| Question validation and answer decoding | `make_questions_tool` | Preserve; add a distinct timeout result |
| Waiting and response arbitration | `ApprovalService` | Own the deadline, race handling, and cleanup |
| Question rendering and keyboard input | `QuestionsOverlay` | Display remaining time and close cleanly |
| Reactive pending state | `AppState.pending_approval` | Never retain an expired request |
| Workflow continuation | Agent turn / workflow runner | Deliver timeout result and let the model decide |
| Durable resume | Session journal/checkpoint layers | Preserve an in-progress wait and terminal outcome |
| Background sessions | Background input/approval adapter | Apply the same terminal timeout semantics |

The current service correctly clears `pending_approval` in its `finally`
block when a response or cancellation arrives, but there is no deadline path.
The current `ask_user` tool treats every non-allowed response as
`{"cancelled": True}`, which would hide the distinction between user
cancellation, service shutdown, and an unanswered question.

## 3. Problem statement

### 3.1 Indefinite suspension

An agentic turn can remain in the `WAITING_FOR_USER` state after the user has
left. The workflow outer loop cannot advance, phase checkpoints cannot settle,
and the process may retain the session lease indefinitely.

### 3.2 Ambiguous terminal outcomes

The current boolean `ApprovalResponse.allowed` is insufficient for a question
tool. These outcomes have different meanings:

- the user answered the questions;
- the user explicitly cancelled the prompt;
- the request timed out;
- the session was interrupted or shut down;
- the approval/input adapter failed.

Collapsing timeout into cancellation prevents the agent from making the
requested best-effort decision and makes workflow recovery indistinguishable
from deliberate user denial.

### 3.3 Incorrect ownership of the fallback decision

The TUI must not guess the user's answer, select the first option, or silently
approve a side effect. The question tool must report that no answer arrived;
the LLM agent must decide how to continue using the original question,
available evidence, workflow policy, and normal tool permissions.

### 3.4 Races and stale answers

A user may press Enter at the deadline, an overlay may be replaced while the
request is pending, or a response callback may execute after timeout cleanup.
Without request identity and terminal-state arbitration, a late response can
resolve a later request or leave a stale overlay visible.

### 3.5 Resume and generated workflows

An interruption or process restart while a question is pending must not reset a
workflow to its first phase or repeat a completed question. Workflows generated
by `create_workflow` must understand the timeout result automatically through
the shared prompt/tool contract and must record assumptions when they continue
without an answer.

## 4. Goals

1. Add a finite, configurable `ask_user` wait deadline with a default of 60
   seconds.
2. Close expired question requests exactly once and always clear their pending
   TUI/background state.
3. Return a structured, machine-readable timeout result distinct from answer,
   cancellation, denial, and infrastructure failure.
4. Resume the same agent turn after timeout so the agent chooses the best
   available option rather than having the UI guess.
5. Instruct the agent to state its assumption, use the safest reasonable choice,
   and continue without repeatedly asking the identical unanswered question.
6. Preserve normal Safe-mode capability approval and workspace authorization;
   timing out a question must never grant permission to perform a side effect.
7. Make timeout behavior consistent across direct turns, `code_plan`,
   `create_workflow`, generated workflows, headless adapters, background
   sessions, and workflow resume.
8. Make the waiting overlay visibly communicate the deadline without causing
   redundant redraws or breaking resize-safe rendering.
9. Persist enough bounded identity and outcome metadata to recover a pending or
   expired request after restart without persisting secrets or unnecessary
   question content.
10. Add unit, integration, and E2E coverage for timeout, response races,
    cancellation, resume, and fallback continuation.

## 5. Non-goals

- Automatically selecting the first, last, or otherwise arbitrary question
  option in the TUI.
- Automatically approving a tool, changing Safe/Yolo mode, or bypassing
  workspace/network/browser policies after timeout.
- Changing the timeout for ordinary tool approvals, plan review, or provider
  transport requests unless a separate configuration explicitly requests it.
- Retrying the same `ask_user` call indefinitely after a timeout.
- Replacing the existing `ApprovalService`, `QuestionsOverlay`, session
  journal, checkpoint, or workflow runner with a parallel interaction system.
- Persisting full question text, answer contents, tool arguments, API keys, or
  provider prompts in timeout diagnostics.
- Treating absence of a TUI as permission to wait forever.
- Making a workflow transition directly from the timeout callback. Phase
  transitions remain controlled by the workflow's transition tools.

## 6. Definitions and state model

### 6.1 Question request

A question request is one `ApprovalRequest` with `kind="questions"`. It has a
stable `request_id`/`tool_use_id`, a bounded question identity summary, a
creation time, a deadline, and one terminal outcome.

### 6.2 Terminal outcomes

The interaction contract must distinguish:

```text
answered       user submitted an answer object
cancelled      user explicitly cancelled, or the active session was cancelled
timed_out      deadline expired before an answer won the race
failed         the interaction service could not deliver a valid response
```

Only `answered` contains user answers. `timed_out` contains no invented
answers. `cancelled` must not be interpreted as permission to guess. `failed`
must remain actionable and must not silently become `timed_out`.

### 6.3 Best-effort decision

After a `timed_out` tool result, the agent receives one continuation turn with
the original conversation context and a stable instruction equivalent to:

> The user did not answer before the configured deadline. Decide the safest
> reasonable option from the available context. State the assumption you are
> making, continue with that assumption, and do not ask the same question
> again solely because it timed out.

The exact wording belongs to the stable prompt/tool policy contract; the
question text and answers remain dynamic content. If no safe decision is
possible, the agent must explain the blocking uncertainty and use the existing
workflow rejection/failure path rather than fabricate a material fact.

### 6.4 Timeout scope

The timeout applies to one question request, not to the entire agent turn or
workflow run. LLM generation, tool execution, phase transitions, and ordinary
approval requests retain their existing timeout and retry policies.

## 7. Configuration

### 7.1 Canonical setting

Add the following typed setting to `ToolSettings`:

```toml
[tools]
question_timeout_s = 60.0
```

The setting is intentionally named `question_timeout_s` so it cannot be
confused with:

- `execution.timeout_s` (provider request timeout);
- `execution.turn_timeout_s` (agent-turn watchdog);
- `tools.http_timeout_s` (HTTP tool timeout);
- browser navigation/action timeouts; or
- plugin/tool execution deadlines.

The setting must participate in the existing global/project/environment/CLI
configuration precedence. For example:

```bash
agenthicc --set tools.question_timeout_s=180
```

The default is `60.0` seconds when omitted. Values must be finite and greater
than zero. A deliberately long wait is configured by choosing a larger finite
value; an unbounded question wait is not supported because it recreates the
indefinite-suspension defect.

### 7.2 Configuration diagnostics

Invalid values must fail during configuration loading with a path-specific
message such as `tools.question_timeout_s must be a finite number greater than
zero`. The value must be included in redacted configuration diagnostics and
must not be hidden as a provider or tool execution timeout.

### 7.3 Per-request override

The initial implementation must not allow model-generated question payloads to
override the global timeout. If a future trusted integration needs a per-call
override, it must be supplied outside the LLM-controlled `questions` payload,
validated against the configured policy, and recorded in the structured event.

## 8. Functional requirements

### FR-1 — Shared timeout ownership

`ApprovalService` (or a typed interaction service owned by the same boundary)
must own the timeout for `ApprovalRequest(kind="questions")`. Do not implement
independent `asyncio.wait_for` calls in `make_questions_tool`, the overlay, or
individual workflow runners.

The service must:

1. compute a request deadline from the configured timeout;
2. publish the pending request before waiting;
3. wait for either the user response or the deadline;
4. atomically claim the first terminal outcome;
5. clear the pending request in every terminal path; and
6. return a typed response to the tool.

### FR-2 — Structured timeout response

Extend the response contract additively, for example:

```python
ApprovalResponse(
    allowed=False,
    timed_out=True,
    message="No answer was received before the question timeout.",
    request_id="...",
)
```

The exact field names may follow the current dataclass conventions, but the
serialized tool result must include bounded fields equivalent to:

```json
{
  "timed_out": true,
  "decision_required": true,
  "request_id": "stable-id",
  "timeout_s": 60.0,
  "message": "The user did not answer before the configured deadline; choose the best safe option and state your assumption."
}
```

It must not include a fabricated answer or silently use `cancelled: true` as
the only indicator.

### FR-3 — Agent continuation and bounded fallback

When `make_questions_tool` receives a timed-out response, it must return the
structured timeout result to the agentic loop. The runner must remain in the
same turn/phase and allow the model to produce its best-effort continuation.

The continuation must:

- preserve the original question call and all prior conversation context;
- tell the model that the timeout is terminal for that request;
- require an explicit assumption in the assistant response or workflow
  artifact when the decision affects output;
- prohibit pretending that the user selected an option;
- preserve normal phase-transition-tool requirements; and
- stop repeated identical question calls from creating an unbounded loop.

If the agent calls `ask_user` again with the same request fingerprint during
the fallback continuation, the tool must return a bounded `repeated_timeout`
or `question_already_timed_out` result, not open a second indefinitely waiting
overlay. A materially different question is a new request with a new ID.

### FR-4 — Response wins a clean race

If a valid user response is committed before the deadline, return
`answered` exactly once, even if the timeout task wakes in the same event-loop
iteration. If the deadline terminal state is committed first, a later response
must be ignored and the caller must receive `timed_out`.

The service must use request identity and a terminal-state guard so a late
overlay callback cannot resolve a different request.

### FR-5 — TUI countdown and cleanup

The `QuestionsOverlay` must expose the remaining wait in a compact, readable
status line, for example `Waiting for your answer · 38s remaining`. The display
must use the existing shared tick/pause-aware rendering contract and must not
append a new transcript line on every countdown tick.

When the timeout fires:

- the overlay closes or transitions to a non-interactive expired state;
- input is no longer accepted for the expired request;
- `pending_approval` becomes `None`;
- the display pause is released;
- one concise timeout event/notice may be rendered; and
- the agent continuation is allowed to resume.

Resize, scroll, redraw, and cancellation must not reset or extend the
deadline.

### FR-6 — Explicit cancellation remains distinct

Esc, `/stop`, Ctrl-C, session shutdown, and workflow interruption continue to
produce cancellation semantics. They must not be reported as timeout and must
not trigger the best-effort agent decision. The existing interruption and
resume contracts remain authoritative.

### FR-7 — Persistence and resume

Record a bounded question-wait lifecycle in the existing session/workflow
journal or checkpoint projection:

- request ID and workflow/turn association;
- question count and non-sensitive question-ID fingerprints;
- configured timeout and absolute deadline;
- `pending`, `answered`, `timed_out`, `cancelled`, or `failed` outcome;
- timestamp/revision for ordering; and
- whether the fallback continuation was delivered.

On process restart or `--resume`:

- an unexpired pending request may be rehydrated with its remaining time;
- an expired pending request must be reconciled to `timed_out` before the next
  phase prompt or workflow dispatch;
- an already-delivered timeout must not create a second timeout result; and
- the workflow resumes at its durable phase/cursor, never at `INIT` solely
  because the question was unanswered.

The persisted deadline must use a restart-safe wall-clock representation plus
the existing monotonic timing for in-process countdowns. Clock skew must be
handled conservatively: a clearly expired deadline times out, while an invalid
or ambiguous record fails closed with an actionable recovery state.

### FR-8 — Workflow and generated-workflow contract

Update the shared workflow prompt contract and `create_workflow` authoring
instructions so generated workflows know that:

- `ask_user` can return `timed_out` instead of answers;
- the agent must make a best-effort decision and state assumptions;
- a timeout is not a phase transition and does not bypass a phase gate;
- generated workflow artifacts should record fallback assumptions in their
  existing dynamic context or phase artifacts; and
- generated workflows must not implement their own question timers.

The stable policy text must remain cacheable. Actual timeout values, question
text, and request state remain dynamic context.

### FR-9 — Headless and background behavior

Headless mode must never wait forever for a UI that does not exist. Its
interaction adapter must return a structured non-answer outcome according to
the existing headless policy, with `timed_out` used only when a configured
headless input source actually waits until the deadline. An immediate
unsupported/cancelled result must remain distinguishable from a real timeout.

Background sessions and non-TUI clients must use the same request identity,
deadline, terminal-state, and event schema. A background session may be
answered through its existing control surface before the deadline; after the
deadline its input endpoint must reject late answers with a stable expired
request error.

### FR-10 — Structured observability

Emit versioned structured events for:

- `question_wait_started`;
- `question_answered`;
- `question_timed_out`;
- `question_cancelled`; and
- `question_wait_failed`.

Each event includes request ID, session/turn/workflow IDs where available,
timeout/deadline, outcome, and bounded counts. It must not include full
question text, answer contents, tool arguments, secrets, or provider output.

The TUI human projection must be concise and redacted. Structured consumers,
session export, workflow checkpoints, and diagnostics must use the event fields
instead of parsing display text.

### FR-11 — Security and policy invariants

Timeout fallback must not:

- grant or remember an approval;
- switch Safe to Yolo;
- bypass `workspace_access`, network, browser, MCP, or plugin policy;
- execute a side-effecting tool without its normal capability gate; or
- expose question/answer content in logs or event IDs.

If the best-effort decision requires a side-effecting action, the normal
approval service still runs. If the agent cannot make a safe decision, it must
report the ambiguity and use the existing workflow rejection/failure contract.

### FR-12 — Compatibility

Existing `ApprovalResponse` consumers that only inspect `allowed`, `message`,
or `mode` must continue to work. New timeout fields are additive with safe
defaults. Older approval adapters that do not expose timeout support must be
feature-detected; agenthicc must either wrap them with a deadline-aware adapter
or return an actionable compatibility diagnostic rather than silently waiting
forever.

## 9. Data flow

### 9.1 Normal answer

```text
agent -> ask_user(questions)
     -> ApprovalRequest(kind=questions, request_id, deadline)
     -> journal wait_started + pending_approval signal
     -> QuestionsOverlay renders countdown
user -> answer submission before deadline
     -> service atomically claims answered
     -> journal question_answered
     -> overlay closes; pending signal clears
     -> ask_user returns {question_id: answer}
     -> agent/workflow continues normally
```

### 9.2 Timeout fallback

```text
agent -> ask_user(questions)
     -> service starts one deadline
     -> TUI waits and renders countdown
deadline -> service atomically claims timed_out
         -> journal question_timed_out
         -> expired overlay closes; late input is rejected
         -> ask_user returns timed_out=true, decision_required=true
         -> same agent turn receives timeout policy instruction
         -> agent chooses safest available option
         -> agent states assumption and continues
         -> normal tools/phase transitions remain gated
```

### 9.3 Restart while waiting

```text
process interruption
  -> durable wait_started(deadline, request_id)
restart / --resume
  -> load conversation + workflow checkpoint + wait record
  -> reconcile deadline before phase prompt
      ├─ still pending: rehydrate with remaining time
      └─ expired: commit timed_out exactly once
  -> deliver timeout result if not already delivered
  -> resume saved phase/cursor, never INIT by default
```

## 10. Acceptance criteria

### AC-1 — Default timeout

With no configuration, `ask_user` has a 60-second deadline. A test using a
controlled clock or short injected duration proves that the wait does not
remain pending after the deadline.

### AC-2 — Configurable timeout

`[tools].question_timeout_s` is loaded through all supported configuration
layers, `--set tools.question_timeout_s=N` overrides it, and invalid/non-finite
values fail with a path-specific diagnostic.

### AC-3 — Agent makes the decision

When the user does not answer, the UI does not choose an option. The agent
receives `timed_out=true` and produces a continuation that states a best-effort
assumption. The workflow does not receive a fabricated user answer.

### AC-4 — No indefinite wait

After timeout, the `ask_user` coroutine returns, `pending_approval` is cleared,
the waiting overlay is no longer interactive, and the agent turn is no longer
blocked on the original request.

### AC-5 — Answer race

A response submitted before the deadline returns `answered`. A response after
the timeout returns an expired-request error and cannot affect the next
question. Concurrent response/timeout tests prove exactly one terminal outcome.

### AC-6 — Explicit cancellation

Esc, Ctrl-C, `/stop`, and session shutdown return cancellation semantics and do
not trigger best-effort fallback. Their existing recovery/checkpoint behavior is
unchanged.

### AC-7 — TUI behavior

The TUI displays a countdown or equivalent deadline indicator, remains resize
safe, emits no per-second transcript spam, closes the expired overlay, and
returns to the normal status/input state after timeout.

### AC-8 — Workflow phase continuity

A timed-out question inside `code_plan`, `create_workflow`, and one generated
custom workflow resumes the same phase/inner turn. It does not restart at INIT,
repeat the same question indefinitely, or bypass a transition tool.

### AC-9 — Durable resume

Interrupting or restarting while a question is pending either rehydrates the
remaining deadline or reconciles an already-expired deadline before dispatch.
The timeout event and fallback continuation are each delivered at most once.

### AC-10 — Background/headless parity

Background sessions can answer before the deadline and reject late answers.
Headless sessions never wait indefinitely and distinguish unsupported/cancelled
input from a real configured timeout.

### AC-11 — Security invariants

Timeout fallback never grants permissions, changes operational mode, bypasses
workspace/network/browser policies, or runs a side-effecting tool outside its
normal gate. Tests cover Safe mode and an approval-required fallback action.

### AC-12 — Structured observability

Session export and event subscribers contain one versioned timeout event with
stable identity and bounded metadata. Human output is concise and contains no
full question text, answer content, secrets, or tool arguments.

### AC-13 — Compatibility

Existing approval adapters, direct turns, ordinary tool approvals, plan review,
workflow checkpoints, transcript replay, and old durable records continue to
work. Old records without timeout metadata remain readable.

## 11. Testing strategy

### 11.1 Unit tests

Add deterministic tests for:

- configuration default, override, precedence, and invalid values;
- request deadline and remaining-time calculation;
- timeout response serialization and backward-compatible defaults;
- response-before-deadline, timeout-before-response, and same-tick races;
- duplicate response and late-response rejection by request ID;
- cleanup when the waiter is cancelled or the approval service raises;
- `ask_user` answer, cancellation, timeout, repeated-timeout, and malformed
  response results;
- countdown rendering without transcript spam;
- overlay close, resize, and expired-input handling;
- stable event IDs, bounded metadata, and redaction;
- Safe-mode permission checks after a fallback decision.

### 11.2 Integration tests

Use a fake clock, fake approval service, temporary journal, and deterministic
workflow runner to cover:

1. TUI question timeout through `ApprovalService` and `QuestionsOverlay`;
2. a real `code_plan` question timing out and continuing in the same phase;
3. `create_workflow` prompt/tool contract handling `timed_out`;
4. generated workflow fallback assumptions and checkpoint persistence;
5. process-style restart with pending and expired wait records;
6. response/timeout races and stale overlay callbacks;
7. background input before and after the deadline;
8. headless behavior with and without an explicit input adapter; and
9. session event export and structured timeout observability.

### 11.3 End-to-end tests

Add deterministic local journeys:

- Start a workflow that asks one question, provide no input, observe the
  countdown expire, and verify that the agent states an assumption and
  continues.
- Start a workflow with multiple questions, answer some, allow the remaining
  request to time out, and verify that answered data is preserved while only
  unanswered data is handled as a fallback.
- Interrupt/restart during the wait and verify that the workflow resumes from
  its saved phase and the timeout is not duplicated.
- Submit input at the deadline boundary and verify that only one of answer or
  timeout is committed.
- Trigger a fallback that would write a file in Safe mode and verify that the
  ordinary capability/workspace approval still appears.

All tests must use local fake clocks and approval adapters; they must not wait
60 real seconds or depend on a live provider.

## 12. Non-functional requirements

### NFR-1 — Bounded liveness

No interactive question may suspend an agent turn indefinitely under the
default policy. Timeout cleanup must complete within one event-loop turn after
the deadline task wins.

### NFR-2 — Idempotency

Repeated timeout observation, resume reconciliation, redraw, late responses,
and duplicate lifecycle signals must not duplicate answers, timeout results,
events, overlays, or workflow transitions.

### NFR-3 — Responsiveness

The countdown must not block the event loop or cause one transcript write per
second. Question input, Esc, resize, and safe commands remain responsive while
the agent waits.

### NFR-4 — Durability

The wait lifecycle and terminal outcome use the existing journal/checkpoint
durability contract. A truncated final record must not corrupt prior
conversation or workflow state.

### NFR-5 — Provider neutrality

The contract works across Anthropic, OpenAI-compatible, Ollama, LiteLLM, MCP,
and generated workflow agents because the timeout is owned by the interaction
boundary, not a provider adapter.

### NFR-6 — Security and privacy

Timeout metadata is bounded, redacted, and free of credentials, prompt text,
answers, tool arguments, and file contents. The fallback cannot weaken any
existing capability or workspace policy.

### NFR-7 — Observability

Operators can distinguish answered, cancelled, timed out, repeated timeout, and
failed interaction outcomes from structured events and session inspection.

## 13. Rollout and migration

1. Add the typed configuration field and validation with the default enabled.
2. Add the additive response fields and shared service deadline handling.
3. Add the TUI countdown/expiration projection and structured events.
4. Add `make_questions_tool` timeout result and stable fallback instruction.
5. Update workflow and `create_workflow` contracts.
6. Add journal/checkpoint rehydration and resume reconciliation.
7. Enable the full unit/integration/E2E matrix using fake clocks.
8. Keep old records and old approval adapters readable through feature
   detection and additive defaults.

No migration may select an answer on behalf of the user or reset a workflow to
its first phase. If a durable wait record cannot be interpreted safely, report
an actionable recovery state and preserve the existing workflow checkpoint.

## 14. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Model treats timeout as a user answer | Use a distinct structured result and explicit stable policy text |
| Timeout races with a real answer | One request identity and atomic terminal-state guard; answer wins when committed first |
| Late callback resolves a new question | Check request ID/generation before accepting any response |
| Countdown floods the transcript | Render through the existing Live/status surface, not `ScrollBufferAppender` |
| Fallback bypasses permissions | Return to the ordinary agent loop; never auto-approve capabilities |
| Restart duplicates timeout | Durable terminal outcome and delivery receipt keyed by request ID |
| Generated workflow repeats the question forever | Shared prompt contract plus repeated-request fingerprint guard |
| Headless mode hangs | Explicit non-interactive adapter semantics and bounded input behavior |
| Clock changes during restart | Persist wall-clock deadline, use monotonic in-process timing, fail conservatively on ambiguity |

## 15. Resolved implementation decisions

The implementation records these decisions in code and tests:

1. `ApprovalResponse` uses additive `outcome`, `timed_out`, `decision_required`,
   `request_id`, `timeout_s`, and `repeated_timeout` fields. The tool exposes
   only the bounded timeout projection and never an invented answer.
2. Lifecycle events are emitted through `ConversationStore` and therefore the
   existing session event log; no second interaction journal is introduced.
3. The request fingerprint hashes only bounded question IDs and option counts.
4. The effective deadline is attached to the published `ApprovalRequest`; the
   TUI redraw loop renders its remaining seconds without transcript writes.
5. Headless mode returns an explicit non-interactive cancellation result. The
   background adapter is the only non-TUI adapter that waits for external input
   and applies the configured deadline.
6. Resume reads pending `question_wait_started` records from the existing
   conversation log. It reuses the remaining deadline or reconciles an expired
   record before the replayed question can open a new wait. Old records without
   lifecycle metadata remain readable.

These decisions must preserve the ownership boundaries in `CLAUDE.md` and must
not be resolved by adding a second approval service or by silently choosing an
answer.

## 16. Definition of done

- `tools.question_timeout_s` defaults to 60 seconds and is validated and
  configurable through existing precedence/CLI mechanisms.
- `ApprovalService` owns one deadline-aware, race-safe question lifecycle.
- `ask_user` returns a distinct structured timeout result and the agent makes
  the best-effort decision with an explicit assumption.
- TUI, headless, background, workflow, generated-workflow, and resume paths
  share the same semantics.
- Timeout never grants permission or bypasses a security policy.
- Pending/expired waits are durable, replay-safe, and do not restart workflows
  at INIT.
- Unit, integration, and E2E tests cover all acceptance criteria using fake
  time and local adapters.
- Relevant guides, public contracts, storage references, and the PRD index are
  updated when implementation begins.
