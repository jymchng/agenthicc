---
title: "PRD-217: Forward Agents Session Input Without Interpretation"
status: Implemented
version: 1.1.0
date: 2026-10-02
repository: jymchng/agenthicc
related_prds:
  - PRD-150  # Client-neutral session service
  - PRD-170  # Workflow recovery
  - PRD-206  # Agents session manager
  - PRD-213  # Active workflow after explicit resume
  - PRD-215  # Targeted input from session details
  - PRD-216  # Input to completed background sessions
tags:
  - agents-manager
  - background-sessions
  - input
  - routing
  - workflows
---

# PRD-217 — Forward Agents Session Input Without Interpretation

## 1. Executive summary

Make input submitted from the agenthicc agents session-details page a strict,
target-addressed pass-through operation. Agenthicc must deliver the submitted
text to the selected session as though the user entered it in that session's
normal input panel. The manager and background-recovery machinery must not
parse, rewrite, replace, supplement, or infer the payload's meaning.

This applies to every input, not only /workflow commands. Plain text, slash
commands, /workflow subcommands, skills, mentions, multiline content, and
future input syntax all cross the manager/worker boundary unchanged. The
selected session's ordinary input and command router is the only component
that decides what the content means.

PRD-215 and PRD-216 already specify purpose-agnostic forwarding. This
corrective PRD addresses the observed contract breach and makes the
end-to-end invariant testable, including proving that no synthetic
“Workflow continuation” message is inserted ahead of the user's payload.

## 2. Problem statement

An operator submits input from a background session's details page and sees
activity such as “Workflow continuation ...” instead of seeing the supplied
input handled directly. Slash commands appear to trigger workflow-continuation
behavior or receive manager-generated semantics before the target session
handles them. This contradicts the operator's explicit requirement:
agenthicc agents forwards anything unchanged and lets the selected session
handle it.

The existing PRDs state that the manager is purpose-agnostic, but the observed
behavior shows that this must be verified across the complete path. Source
inspection found two distinct routes: attached foreground input can explicitly
start a paused workflow continuation, while background-manager input is
delivered by the background worker. The exact observed assistant activity
cannot be attributed to either route from a one-line projection alone; the
transcript event, source, and message ID must be checked. These paths must not
be conflated when diagnosing the report.

## 3. User story

> As an operator on a session's details page, I can send any text to that
> exact session without attaching to it, and Agenthicc delivers exactly that
> text as the next input. Agenthicc does not add a continuation instruction or
> decide that I meant to resume a workflow; the target session handles it
> normally.

## 4. Normative contract

### 4.1 Opaque payload

After the composer produces the submitted string, the payload is opaque to the
manager, supervisor, inbox, and lifecycle code. Those layers may validate
that it is non-empty and within existing size limits, and associate transport
metadata with it, but they must not:

- trim, normalize, tokenize, parse, or classify the payload;
- recognize /workflow, another slash command, or ordinary words such as
  “continue” as control instructions;
- replace user text with a synthetic continuation or recovery prompt;
- prepend or append user-visible text to the submitted content;
- create an additional user message to explain recovery, select a workflow, or
  replay a previous request;
- route the input to another session, workflow, or foreground TUI.

The accepted payload string must equal the submitted composer string exactly,
including non-blank leading/trailing whitespace, newlines, punctuation,
command spelling, and Unicode. A blank-only payload may be rejected by
existing composer validation; it must not be silently rewritten.

Transport fields such as session ID, message ID, target attempt, enqueue time,
and delivery status are metadata outside the message body. Adding metadata
must not change the text delivered to the target input path.

### 4.2 Target-owned interpretation

The target session receives input through its normal session-owned input and
command routing path. The manager must not execute commands in its own
process. The target may interpret a string as a command, skill, mention, or
ordinary user turn according to its existing behavior. A target-side warning
or refusal is still the result of the submitted input; it is not permission
for the manager to retry with altered text or inject a fallback continuation.

For example, /workflow goal_flow must arrive as the literal input
/workflow goal_flow. If the target router handles it as a workflow-selection
command, that is target behavior. Agenthicc must not append a separate
“Workflow continuation” message or automatically resume a checkpoint because
of this input. An explicit /workflow resume command is also forwarded
literally and handled by the target router.

### 4.3 Delivery versus execution

The transport reports whether the exact payload was durably accepted, claimed,
delivered to the target input path, or rejected. It must not claim that the
LLM completed the requested work merely because delivery succeeded.
Conversely, a target-side command result is not a transport failure.

Retries preserve the same message ID and exact body. The target processes an
accepted message at most once under the existing inbox idempotency contract.
A retry after an uncertain result must not create a second, altered, or
synthetic message.

## 5. Session lifecycle behavior

| Target state | Required behavior |
|---|---|
| Starting or running | Enqueue/deliver the exact payload to the current target session owner. |
| Waiting for user input | Preserve the existing ask-user response contract while keeping submitted text unchanged. |
| Waiting for approval | Do not convert text into approval; preserve approval controls and existing delivery policy. |
| Stale/recoverable | Persist the exact payload, recover the same session in the background, then deliver it as the next input. Do not attach to foreground. |
| Completed | Use PRD-216's input-driven attempt for the same conversation; the exact payload is the first new user input. Do not replay the old launch intent. |
| Deleted or purged | Reject without recreating the session or redirecting the text. |
| Target changes while composer is open | Reject as stale-target input and retain the draft; never silently retarget. |

Starting or recovering a worker is an execution-mechanics concern. It must
not cause a second user turn to run before the queued payload, and must not
manufacture a user message such as “Workflow continuation” to initialize
workflow state. Internal checkpoint restoration may restore durable runtime
state, but must remain in session/checkpoint state or transport metadata,
outside the input transcript.

If delivery cannot safely complete, preserve the accepted payload and show a
recoverable pending/error receipt. Do not ask the user to resend modified
wording and do not fall back silently to the original launch prompt.

## 6. Goals and non-goals

### Goals

1. Enforce one invariant from composer submission through target handling:
   accepted input text is unchanged.
2. Ensure only the target session's normal input router interprets the text.
3. Remove implicit continuation/resume behavior caused by manager-side input
   classification or background reactivation.
4. Preserve session identity, ordering, idempotency, ownership fencing,
   transcript behavior, and background-only recovery.
5. Distinguish pending, delivered, rejected, and target-handled outcomes.
6. Add regression coverage for ordinary text, commands, recovery, retries,
   and completed-session input.

### Non-goals

- Changing what commands mean inside the target session's normal router.
- Removing explicit workflow checkpoint-resume commands.
- Making the manager execute target commands locally.
- Changing the regular foreground composer or its registered-trigger system.
- Adding broadcast input or a new remote-control service.
- Treating text as approval or an ask-user answer unless the existing target
  contract explicitly identifies it as such.

## 7. Required implementation investigation

Trace and document one live-session and one recoverable-session submission:

~~~
details composer
  -> submit callback
  -> manager/service request
  -> durable inbox record
  -> supervisor enqueue/recovery decision
  -> worker start/claim
  -> inbox delivery
  -> target input/command router
  -> transcript and activity projection
~~~

At each boundary, record the actual body, message ID, target session ID,
owner attempt, and whether a synthetic event or turn is created. Identify the
origin of “Workflow continuation” activity and determine whether it is a user
message, system prompt, activity-only event, or projection label. Remove
unintended injection without breaking legitimate checkpoint recovery requested
explicitly by the target.

Do not assume a visible activity label proves the LLM received that text.
Tests must inspect the durable inbox, target dispatch, and transcript/event
history.

## 8. Functional requirements

### FR-1 — Exact payload persistence

The inbox stores the submitted string without normalization. On readback, the
target receives an equal string. Transport metadata is stored separately.

### FR-2 — No manager-side command parsing

The detail manager and supervisor do not branch on payload contents.
Lifecycle decisions may depend on session state and delivery receipt, but not
on whether the body starts with /, contains “workflow,” or resembles prose.

### FR-3 — No synthetic continuation turn

No recovery or completed-session startup path injects an additional user
message or agent turn before the accepted payload. Internal workflow
restoration must not be represented as user-authored input.

### FR-4 — Target-owned command behavior

All payloads, including /workflow and its subcommands, reach the same
target-side input router used for attached input. Target-side feedback is
reported without rewriting or retrying the payload.

### FR-5 — Same session and background ownership

Recovery retains the selected session ID and runs in the background. It must
not attach, transfer ownership to the invoking TUI, or create a replacement
session.

### FR-6 — Delivery and retry integrity

Idempotent retries use the original message ID and body. Duplicate user turns
must not result if the manager times out after acceptance or the worker
restarts between claim and delivery. Uncertain delivery remains visible until
reconciled.

### FR-7 — User-visible status

Receipts distinguish at least queued/pending, delivered, rejected, and
failed/recoverable. A receipt may include target-side command feedback, but
must not present a synthetic continuation as the user's input.

### FR-8 — Preserve composer features

Existing editing, paste, mentions, skills, and trigger behavior remains
intact. The guarantee applies to the final string submitted by the composer;
intentional editing before submission, such as expanding a mention token, is
not changed by this PRD.

## 9. Non-functional requirements

- Reliability: restart and retry do not lose, duplicate, or mutate accepted
  payloads.
- Isolation: input is scoped to its immutable target; owner-attempt fencing
  remains in force.
- Privacy: message bodies remain out of generic lifecycle logs and are exposed
  only through existing protected inbox/transcript surfaces.
- Responsiveness: submission does not wait synchronously for an LLM turn to
  finish.
- Compatibility: ordinary live input, ask-user responses, approval controls,
  explicit workflow resume, completed-session input, and deleted-session
  protections retain their documented behavior.

## 10. Acceptance criteria

1. Ordinary text produces exactly one target user message with the submitted
   text and no manager-generated continuation message.
2. /workflow goal_flow produces exactly one target input with that exact body;
   the manager does not parse or replace it.
3. Every /workflow subcommand is forwarded exactly. Valid commands are handled
   by the target router; invalid commands produce target-side feedback and
   remain delivered input.
4. /workflow resume <id> is not executed by the manager; the target router
   decides checkpoint validity and recovery.
5. Ordinary text such as “continue” does not implicitly resume a workflow or
   replay the original goal.
6. A stale session recovers in the background and processes the exact input
   before any old launch intent, without foreground attachment.
7. A completed session starts an input-driven attempt for the same conversation
   and receives the exact payload once.
8. Restart after durable acceptance preserves exact text and does not duplicate
   it.
9. Retrying with the same message ID returns the original receipt and creates
   no second turn or continuation message.
10. Target-side warnings/refusals do not trigger manager-side rewriting,
    automatic recovery, or altered resubmission.
11. Deleted sessions remain rejected; input is not redirected or used to
    recreate a session.
12. Inbox, transcript, and activity records can be correlated by message ID
    to prove which text was delivered to which session.

## 11. Test plan

### Unit tests

- Exact string round-trip for whitespace, newlines, Unicode, slash commands,
  and punctuation.
- Lifecycle decisions depend on session state and receipt, not body content.
- Recovery creates no generated continuation or fallback user message.
- Target-side command errors are handled results, not reasons to alter input.
- Message ID and body remain stable across retry and duplicate submission.

### Integration tests

- Exercise details-service -> inbox -> supervisor -> real worker dispatch with
  a captured target handler; assert exact body and one dispatch.
- Cover live, starting, recoverable, completed, and deleted states.
- Restart between acceptance and delivery; verify exact once-only consumption
  and no original launch-intent replay.
- Send /workflow, ordinary “continue,” and explicit resume input; prove only
  the target router handles them.
- Verify transcript/activity distinguish user text from internal recovery
  metadata.

### End-to-end tests

- From agenthicc agents, open details and send ordinary text; assert the
  selected session receives it without attaching.
- Repeat for /workflow goal_flow and /workflow resume <run-id>; assert exact
  input and absence of generated continuation in the transcript.
- Recover a stale session and verify the exact input is its first new turn.
- Simulate a delivery timeout and retry; assert one target turn.

Tests use deterministic fake providers/handlers; CI requires no real model API.

## 12. Rollout and observability

Retain privacy-safe metadata needed to diagnose delivery: session ID, message
ID, attempt/lease, inbox state, and delivery timestamps. Do not log payloads in
generic lifecycle logs. The details view reports pending/delivered/handled
state; the session transcript remains authoritative for actual user messages.

Older pending inbox entries must retain their exact stored body during
reconciliation. Migration must not replace them with a generated continuation.

## 13. Compatibility and authority

PRD-215 remains authoritative for the target-bound composer and supported
session states. PRD-216 remains authoritative for new input-driven attempts
on completed sessions. PRD-217 strengthens their shared delivery contract:
the final submitted payload is opaque until it reaches the target session's
normal input router. Where earlier wording could permit manager-side workflow
inference, this PRD takes precedence. It does not change command semantics
inside the target.

## 14. Definition of done

- The complete path and source of continuation activity are documented from
  code, not inferred from a UI label.
- No manager/supervisor/inbox path parses or rewrites payload content.
- No implicit continuation or old-intent replay occurs before accepted input.
- Unit, integration, and end-to-end acceptance tests pass.
- Composer, ask-user, approval, explicit workflow-resume, and
  completed-session behavior have regression coverage.
- User documentation explains pass-through and delivery semantics.

## 15. Implementation and verification record

The current background-session path satisfies the opaque-input contract
without adding a second manager-side command or workflow implementation:

- `BackgroundManager._submit_target_input` submits the editor's exact text and
  stable command ID to the selected session's service boundary.
- `BackgroundInputInbox.enqueue` and `enqueue_deferred` reject blank/oversized
  input but persist every accepted character unchanged. The supervisor
  chooses delivery or recovery by session lifecycle and ownership, not by
  inspecting the input body.
- `run_worker` records accepted text as a target `user_message` using the same
  message ID. Deferred ordinary text is passed to `_run_direct_turn`, which
  passes it unchanged to the canonical `_run_agent_turn`; deferred input is
  consumed before the old launch intent. This path does not inspect workflow
  checkpoints or synthesize a workflow-resume prompt.
- Slash commands are interpreted inside the owning background worker's target
  command router. The original input remains in the transcript with its exact
  body and message ID; command feedback is a separate target response. The
  agents manager never dispatches those commands in its own context.
- `docs/guides/background-sessions.md` documents that recovery changes only
  worker startup mechanics and creates no extra user turn.

Regression coverage was strengthened to capture input at the canonical
agent-turn boundary, compare exact transcript bodies and IDs, reject implicit
workflow execution for ordinary text, and verify that invalid workflow input
is still delivered and answered by the target router.

Source tracing found the foreground continuation mechanism in
`TUISession._start_workflow_continuation()` and
`TUISession._resume_workflow_task()`: ordinary text submitted to an attached
paused workflow can create a `[WORKFLOW RESUME]` context entry with a
`User continuation` line. The background agents-input recovery path does not
call that handler. It drains the durable inbox first, routes ordinary input
through `_run_direct_turn`/`_run_agent_turn`, and routes slash commands within
the target worker. Therefore the continuation mechanism is not a permissible
substitute for the manager payload. An assistant response that happens to use
similar wording must be diagnosed from its transcript source rather than
assumed to be a transport-generated message.

Verification in the implementation checkout:

- `uv run pytest tests/ -q` — **4,106 passed, 16 skipped**.
- Focused inbox, worker, supervisor, and agents-details tests — **61 passed**
  after the PRD-217 regression assertions were added.
- `uv run ruff check src/ tests/ scripts/` and formatting check for touched
  implementation/test files — passed.
- `uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json`
  — passed.
- Targeted mypy on the five background input/session modules — passed.
- `uv run mkdocs build --strict` — passed; MkDocs reported an existing guide
  omitted from navigation and its upstream Material warning.
- Repository-wide `uv run mypy src/agenthicc` still reports 250 errors across
  14 modules; the targeted background input/session modules have no mypy
  errors.
