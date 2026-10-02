---
title: "PRD-215: Send Input to a Selected Background Session from its Details View"
status: Implemented
version: 1.4.0
date: 2026-10-02
repository: jymchng/agenthicc
related_prds:
  - PRD-141  # Background sessions and manager TUI
  - PRD-143  # Busy-session message policy
  - PRD-150  # Client-neutral session service
  - PRD-201  # Context-aware mentions
  - PRD-206  # agents manager identity and detail view
  - PRD-209  # agents manager responsiveness
tags:
  - agents-manager
  - input
  - background-sessions
  - tui
  - ipc
---

# PRD-215 — Send Input to a Selected Background Session from its Details View

## 1. Executive summary

Add an input action to the detailed page of `agenthicc agents`. While inspecting
a selected session, pressing `i` opens an editable composer. The composer must
provide the same text-editing and registered-trigger capabilities as the
regular session input: ordinary and multiline text, bracketed/condensed paste,
paste expansion and deletion, cursor editing, history where applicable,
`@`-mentions, slash commands, skills, and other registered input triggers.
Submitting sends the composed input to the exact selected session. For a live
worker it queues input to that owner. For a stale but recoverable session, the
input triggers recovery of that same session in the background; it does not
attach the session to the foreground.

This is not merely a second text box and not an attach shortcut. The `agents`
manager is a separate process from the selected background worker. Input must
cross that process boundary, reach the live session owner, and enter the same
session-side message/command path used for ordinary attached input. The
manager must not execute a selected session's slash command in its own process,
resolve a mention against its own workspace, create a second session, or
silently cancel/attach the worker.

## 2. Current-source findings

### 2.1 The details page has no general composer action

`BackgroundManager._render_details()` currently presents `Esc`, `Enter`, and
`[`/`]` detail-navigation controls. The detail key path handles attach,
scroll, and leaving details; it does not open a general text composer. The
manager's help text mentions `i input`, but the corresponding current key
handler is narrower than that hint suggests.

### 2.2 Existing `i` is only an ask-user response hook

In `BackgroundManager.handle_key()`, `i` is handled only when the selected
session is `WAITING_INPUT`. It invokes an optional `input_provider` and then
calls `BackgroundManagerService.provide_input_async()`. The normal
`agenthicc agents` entry point does not provide a general interactive composer
callback. The supervisor's `provide_input()` accepts a value only while the
session is waiting for a pending input request; it cannot submit a new user
turn while a worker is running.

Thus, the current `i` action is an answer-to-pending-question mechanism, not
general conversation input. Replacing it outright would risk regressing the
existing ask-user response contract.

### 2.3 The regular input panel is session-owned

The regular TUI constructs `UnifiedInputSession` with the selected session's
`AppState`, `CommandBus`, trigger registry, `ModeManager`, overlay host,
workspace, configuration, and history. Its capability pipeline implements
paste handling, cursor and multiline editing, trigger pickers, history,
submission, and cleanup. Submission emits a `SendMessageCommand`; the owning
`TUISession.handle_send()` then applies the target session's busy policy,
command routing, explicit workflow recovery commands, and turn queue.

The `agents` manager has none of those target-session-owned objects. Copying a
small subset of the editor or resolving target-specific triggers in the
manager would create a second, behaviorally divergent input implementation.

### 2.4 Current session-service submission is not sufficient by itself

`SessionService.submit_message` appends a `turn_queued` event and can start work
only when that service instance has a turn handler registered. Current source
does not register the live background worker's normal session handler through
this API. A separate manager-side `SessionService` can therefore record a
command without causing the independent worker process to execute it. Likewise,
calling `BackgroundSupervisor.provide_input()` only fills an outstanding
ask-user request. The implementation needs an explicit, owner-process-bound
delivery mechanism and a worker-side consumer.

### 2.5 Attach changes ownership; this feature must not

The existing detail-page `Enter` action returns an attach result. The CLI then
uses `attach_background_session()` and `BackgroundSupervisor.attach_foreground()`
to hand off from the background worker and open the normal foreground TUI.
That remains a valid existing action, but it is not equivalent to submitting
input while leaving the background session running. Pressing `i` must not
perform that handoff.

## 3. User problem and primary journey

An operator opens `agenthicc agents`, selects a live session, and presses
`Enter` to inspect its details. The session is progressing in another worker.
The operator wants to supply a correction, answer, or follow-up without
stopping the worker or taking over its terminal.

Today the detail view cannot do this. The operator must attach, switch
execution ownership, or use a restricted pending-question input path. This is
especially inconvenient when the desired input contains pasted text, a file
mention, a slash command, or several lines.

### User story

> As an operator inspecting a background session, I can press `i`, compose
> input using the regular session input features, and submit it to that exact
> session without attaching to the foreground. Live workers receive it in
> place; stale recoverable sessions resume in the background before processing
> it. The manager forwards the exact text without inferring its purpose; the
> selected session's own router decides how to handle it.

## 4. Goals

1. Enable general input from the `agents` session-details view.
2. Preserve parity with the canonical input editor and all currently registered
   input triggers.
3. Forward the submitted text unchanged to the selected session's live owner,
   or persist it before resuming that same stale/recoverable session in the
   background. The manager must not infer the user's purpose or reinterpret
   commands; the target session alone routes and handles the input.
4. Preserve message ordering, idempotency, transcript/journal behavior, workflow
   state, and the selected session's identity across the manager/worker boundary.
5. Keep the manager responsive while input is composed, submitted, accepted, or
   rejected.
6. Preserve the existing attach action and pending ask-user answer behavior.
7. Make delivery state visible so users can distinguish accepted, queued,
   delivered, rejected, and still-pending input.

## 5. Non-goals

- Replacing or redesigning the normal TUI input panel.
- Automatically attaching to or taking ownership of a background session.
- Broadcasting one input to multiple selected or marked sessions.
- Allowing typed input to approve/reject a pending approval implicitly.
- Executing a target session's slash command in the `agents` manager process.
- Attaching or transferring a recovered session to the foreground TUI.
- Exposing the session's secrets, provider credentials, or private state to the
  manager in order to render a composer.
- Adding a general remote-control/network service; the initial feature remains
  local and uses Agenthicc's existing session ownership and IPC boundaries.

## 6. Interaction and keyboard contract

### 6.1 Opening the composer

- On a session-details page, lowercase or uppercase `i` opens the input panel
  bound to the detail page's immutable `session_id`.
- Opening the composer is available only when the session state can accept or
  durably queue input. Unsupported states show a concise reason and retain the
  details view.
- The session ID is captured when the composer opens. Refresh, sorting,
  pagination, selection changes, or row reuse must never retarget a draft.
- The table/list `i` behavior, if any, remains unchanged. This PRD adds the
  action to the details page and does not overload `Enter`.

### 6.2 Editing and modal controls

The composer reuses the canonical input capability/editor implementation and
supports all currently registered input triggers rather than a hard-coded
subset. At minimum, that includes:

- ordinary character entry, cursor movement, Home/End, deletion, clear, and
  multiline insertion;
- bracketed paste and Ctrl+V paste expansion, including condensed-paste
  editing and cancellation behavior;
- trigger-picker navigation and insertion for file mentions, commands, skills,
  and any other registered trigger;
- command/mention completion data and validation derived from the target
  session's workspace/configuration/registries, not the manager's unrelated
  context;
- history behavior consistent with the regular input mode, with any history
  additions written to the selected session's history only after accepted
  submission.

The composer is a modal subview. `Esc` returns to details without attaching,
cancelling the worker, or silently submitting. An unsubmitted draft remains
available if the user reopens the composer during the same manager visit; a
visible clear/cancel action can discard it. The target worker is never
interrupted by an editor key. Explicit manager actions such as `c` remain
outside the modal and retain their existing behavior.

### 6.3 Submission and feedback

- `Enter` submits through the normal submission semantics; `Ctrl+Enter`/`Ctrl+J`
  inserts a newline as it does in the regular composer.
- Empty/whitespace-only input is not sent.
- The composer clears only after the target accepts the input. On a definite
  rejection it keeps the draft and displays an actionable, bounded error.
- After acceptance, show a target-specific receipt/status such as `Queued for
  <session-id>` or `Delivered to <session-id>`. Do not claim delivery merely
  because a manager-side event was appended.
- The user can return to the detail page immediately; delivery and worker
  processing continue asynchronously.

## 7. Session-state behavior

The exact enum mapping is an implementation detail, but externally visible
behavior must be explicit and consistent:

| Target condition | Required behavior |
|---|---|
| Live worker starting or running | Accept a bounded, durable message into that session's ordered inbox; wake the owner when possible. |
| Worker waiting for an explicit `ask_user` response | Preserve the existing answer contract. A normal text submission may satisfy the outstanding request only when it is explicitly identified as its answer; it must not create a duplicate turn. |
| Worker waiting for approval | Do not treat composer text as approval/rejection. Keep it queued for normal target-session dispatch or explain why it cannot yet be consumed. Existing approval controls remain authoritative. |
| Recoverable session without a live input consumer (`orphaned`, `failed`, `cancelled`, or `archived`) | Durably queue the input against the displayed session revision, then resume that same session in the background. Show the message as pending recovery until a new worker claims it. Never attach or transfer it to the foreground. |
| Completed session | PRD-216 supersedes this PRD's original rejection rule: persist input, then start a new input-driven attempt for the same conversation without replaying the old intent. |
| Deleted session | Reject input and retain the draft; never recreate or launch the deleted session. |
| Session attempt/owner changes while composer is open | Reject with a stale-target notice, retain the draft, and require reopening details. Never redirect to a newer attempt or another session. |

Deferred recovery input is persisted before the resume operation begins. The
new worker binds it to its own attempt and lease, then forwards the exact text
through the target session's normal input-routing semantics before considering
the old launch intent. The manager and inbox are purpose-agnostic: ordinary
text, slash commands, skills, and other registered triggers use the same
session-owned routing path as attached input. A target-side command warning or
workflow-recovery refusal is still a delivered/handled input, not a manager
transport failure. Ordinary text is always a normal target-session turn. It
does not inspect workflow checkpoints, resume a workflow, start a new workflow,
or replay the old launch intent. Only an explicit target-side workflow command
such as `/workflow resume [run-id]` requests checkpoint recovery and fails
closed when no valid checkpoint exists. If worker startup fails, the inbox
entry remains durable and visibly pending/recoverable so the user is not
encouraged to resend and accidentally duplicate it.

If the implementation supports queueing before the worker is ready, a message
accepted in `starting` state must remain durable and be processed once the
worker has claimed the session. If the worker exits before consuming an
accepted message, the message remains visibly pending/recoverable; it must not
be dropped or silently replayed twice.

## 8. Functional requirements

### FR-1 — Details-page `i` action

In a valid session-details view, `i` opens a composer overlay/subview without
changing the selected session, attaching it, or changing worker ownership. The
details view shows a clear key hint. Existing Enter-to-attach, scroll, exit,
approval, cancellation, and list navigation behavior remains intact.

### FR-2 — Canonical editor parity

The new view MUST reuse `UnifiedInputSession`'s editor/capability pipeline, or
extract a shared controller used by both that session and `agents`. It MUST NOT
implement a second buffer, paste model, trigger dispatcher, or keyboard
capability list. Session-specific submit and rendering adapters may differ.
Parity tests must cover every registered capability and trigger relevant to
the active editor mode.

### FR-3 — Target-bound input context

The composer is bound to the exact target session ID and uses the target
session's effective workspace, mode/policy, configuration, command/skill
registry, and mention context for completion and execution. A target context
that cannot be obtained or is stale must fail closed; the manager's own cwd or
session registries must not be substituted silently.

### FR-4 — Owner-process delivery

Submission crosses the process boundary through an explicit background-session
input contract. The background worker that owns the live session is the
authority that accepts and dispatches the command. A manager-side durable event
append by itself is not delivery. Use existing local ownership and storage
primitives, adding a bounded per-session inbox/IPC mechanism if required. Do
not introduce a second agent runtime.

### FR-5 — Opaque forwarding and canonical target routing

The manager submits the exact composed text and stable message ID without
parsing its intent, classifying it as a resume request, or interpreting slash
commands. After receipt, every input is routed by the target session through
the same session-owned command/skill/turn semantics as normal
`TUISession.handle_send()`. The target runtime, not the manager, handles:

- busy/streaming queue policy;
- slash-command and skill routing;
- explicitly submitted workflow selection, reset, and recovery commands;
- mention resolution and tool execution;
- appending the user message to the correct conversation journal;
- session-service/event projections and resulting turn activity.

Inputs must not create a new conversation/session ID or bypass mode,
capability, workspace, approval, or subagent policy. Ordinary text is submitted
verbatim as a normal target-session turn, even when a workflow is selected or
saved checkpoints exist. This path must not inspect workflow checkpoints,
resume a workflow, start a new workflow, or replay the previous launch intent.
An explicit target-side workflow command is routed by the target's normal
command path and may perform its documented workflow operation. A command
warning is handled target output, not a manager delivery failure.

### FR-6 — Queueing, ordering, and idempotency

Accepted messages have stable message/command IDs and are processed in FIFO
order for the target session. Repeated delivery of the same command ID is
idempotent. Enforce bounded queue depth and payload size; report backpressure
without losing the draft. The worker must not execute concurrent user turns
unless the existing session runtime explicitly supports that behavior.

For messages received while the worker is in an LLM/tool turn, follow the
target session's established busy policy. At minimum, ordinary follow-up text
must queue and run at a safe turn boundary; control/interrupt behavior is only
available through the existing explicit control keys/commands. Slash commands
must be evaluated using their declared busy policy by the target runtime.

### FR-7 — Pending question and approval semantics

The current ask-user answer mechanism remains supported. The implementation
must distinguish answering a pending question from queuing a new user turn and
must not deliver one submission to both paths. A pending approval cannot be
approved or rejected by arbitrary composer text. Preserve existing `y`/`n` or
approval-overlay semantics.

### FR-8 — Honest operation status

Represent at least accepted/queued, delivered-to-owner, processing, completed,
and rejected/failed states or equivalent user-visible receipts. Errors are
redacted and bounded. The UI must distinguish durable acceptance from actual
turn start/completion and show a stable message ID for diagnosis without
printing the submitted text into manager logs.

### FR-9 — Responsiveness and cancellation

Input typing and modal navigation remain on the UI loop and do not block on
filesystem scans, process operations, session-store locks, or worker response.
Enqueue/delivery work uses the existing bounded async service boundary or an
equivalent bounded adapter. Closing the modal cancels only its local draft UI;
it does not retract an already accepted message or terminate the worker.

### FR-10 — Transcript and persistence integrity

An accepted message is added to the selected session's conversation/journal
exactly once with the correct source/client and turn identity. Do not duplicate
the message by both importing an inbox record and appending a separate
`turn_queued` path. Persist enough inbox/receipt state to recover accepted but
unconsumed inputs after manager or worker restart.

### FR-11 — Stable target identity and ownership

Every command is addressed by session ID plus current owner/attempt identity,
not row number, page offset, title, or mutable selection index. A stale worker
attempt must not consume a newer attempt's message. A no-longer-owned or
non-recoverable terminal session returns an explicit failure. Recoverable
terminal sessions use deferred-input recovery; the feature cannot steal the
session lease or alter the single-owner invariant.

### FR-12 — Non-interactive compatibility

The existing non-interactive `agenthicc agents` listing and JSON projections
remain unchanged. The composer is available only in the interactive manager.
The existing `agenthicc jobs input`/question-answering behavior and public
`agenthicc attach <session-id>` behavior remain backward compatible.

## 9. Proposed architecture and data flow

The manager owns the editor surface; the selected live worker owns message
interpretation and execution. A shared input controller prevents the editor
from forking, and a target-bound inbox bridges processes.

```text
agents manager (foreground process)
  │
  ├─ detail page captures target session_id + owner attempt
  ├─ i opens shared input controller in target context
  ├─ paste / @mention / slash / skill picker uses target workspace registries
  └─ Enter → SendMessageCommand(text, command_id, target_session_id)
             │
             ▼
     bounded BackgroundManagerService adapter
             │
             ▼
     supervisor-owned local command inbox / IPC
       (durable acceptance, dedupe, FIFO, owner fence)
             │
             ▼
     target background worker drains inbox
             │
             ├─ resolve ask_user answer when one is pending
             └─ otherwise use shared session message router
                    │
                    ├─ busy policy / queue
                    ├─ slash command / skill route
                    ├─ mention and workspace policy
                    └─ session transcript + workflow + agent turn
             │
             ▼
     receipt/event projection → manager status and session details
```

### 9.1 Shared input surface

Refactor `UnifiedInputSession` only as needed to allow an alternate
session-bound submit adapter and a manager-owned renderer/overlay host. The
normal TUI remains the canonical capability owner. The manager must not create
a dummy `TUISession` or `CommandBus` whose handlers belong to the wrong
conversation. Inputs retain the target mode/policy; cycling mode from the
manager input, if exposed, must either update the target session through its
normal mode command or be unavailable with an explanatory hint.

### 9.2 Worker-owned command inbox

Add a narrow typed input envelope, conceptually:

```text
message_id
session_id
owner_attempt
client_id = "agents-manager"
text
accepted_at
delivery_state
```

The concrete storage may be an append-only inbox in the existing background
store or another local IPC primitive, but it must provide atomic enqueue,
bounded payloads, idempotent acceptance, FIFO consumption, owner/attempt
fencing, and restart recovery. Keep it separate from ask-user `input_value`;
the two protocols have different semantics.

The worker must monitor its inbox while it owns the session, including while a
workflow is running. An implementation that checks only before or after the
whole `execute_workflow()` call does not satisfy live input. It may dispatch
messages at safe turn/phase boundaries according to the session queue contract,
but must expose the pending/queued state and must not reset the workflow.

### 9.3 Shared target-side message routing

Extract or expose the smallest session-owned dispatch operation needed by
both `TUISession.handle_send()` and the background worker. Preserve behavior
for natural-language messages, registered commands/skills, workflow
continuations, and current busy policies. Avoid duplicating parsing logic in
the manager or maintaining separate TUI and headless command registries.

## 10. Failure handling and security

- If the target becomes terminal, loses its owner, or changes attempt between
  composer opening and submission, reject the send and retain the draft.
- If enqueue succeeds but the worker is temporarily unavailable, report
  `queued/pending`, not `delivered`; retain the item for recovery.
- If the command inbox is full, reject before clearing the editor.
- If the worker acknowledges a command but crashes before recording dispatch,
  recovery uses the stable ID to avoid duplicate execution.
- If the owner lease changes, the old owner cannot consume or acknowledge
  messages for the new attempt.
- Error and diagnostic output is redacted. The submitted message is stored in
  the target session's normal journal as expected, but must not be copied into
  generic manager logs or operation telemetry.
- The worker validates session ID, attempt, payload size, text type, and
  capability/mode policy before dispatch. User input does not grant additional
  tools, permissions, network access, or workspace paths.
- Mention completions and resolution are scoped to the target workspace and
  existing `WorkspaceScope`/`WorkspaceAccessPolicy`.
- Slash commands execute under the target's command registry, session owner,
  and policy—not under manager process privileges.

## 11. Acceptance criteria

1. Opening a session's details and pressing `i` displays an input panel with a
   visible target identity; it does not attach or stop a live background worker.
2. `Esc` returns to details, retains an unsubmitted draft for the manager visit,
   and has no cancellation effect on the target worker.
3. The composer supports all normal registered input capabilities, including
   multiline text, ordinary typing, cursor editing, paste/expand/delete, and
   trigger pickers; tests demonstrate the same core controller is used by both
   the regular TUI and this view.
4. `@` completion searches the selected session workspace even when the
   manager process cwd differs. A submitted mention is resolved by the target
   session's policy.
5. Slash commands, skills, and other registered triggers are executed by the
   target session's normal router. The manager does not inspect or interpret
   the submitted text, and its own command handlers are never invoked.
6. Sending while a worker is running reaches that worker without changing its
   PID, session ID, owner lease, workflow run ID, or current checkpoint. The
   message is processed once at a safe session boundary under existing busy
   policy.
7. Sending to an orphaned, failed, cancelled, or archived recoverable session
   durably queues the input and resumes the same session in the background;
   the receipt remains pending until a worker claims the input. It never
   attaches or switches to the foreground TUI.
8. A recovered session processes ordinary submitted text as a normal turn in
   its existing conversation, without replaying the original launch request,
   selecting a workflow checkpoint, or starting/restarting a workflow. The
   input is forwarded unchanged; only an explicit target-side command such as
   `/workflow resume [run-id]` may request workflow recovery.
9. If background recovery cannot start, accepted input remains visibly
   pending/recoverable and is not falsely reported as delivered.
10. Messages sent in quick succession preserve FIFO ordering and are not
   duplicated by retries, repeated Enter events, or worker recovery.
11. A pending ask-user response is resolved once by the existing response path;
   the same text is not also queued as a new turn. Pending approval remains
   unresolved unless the user uses the established approval action.
12. Sending to completed/deleted sessions produces a bounded actionable
   error, keeps the draft, and never silently launches a worker.
13. A status/attempt change after opening the composer cannot redirect the
    message to another list row or session.
14. Enqueue/delivery does not freeze navigation or rendering under a slow
    worker, full queue, or session-store contention.
15. Accepted-but-unconsumed messages survive a manager restart and are either
    consumed once by the valid owner or explicitly shown as pending/rejected;
    they are never silently lost.
16. Existing Enter-to-attach, list view, ask-user response, `jobs input`,
    cancellation/approval keys, JSON listing, and non-interactive output remain
    compatible.
17. The interactive session table displays no more than ten sessions per page
    and never includes deleted sessions. Recovery from trash remains available
    through the existing jobs/store recovery interface rather than a deleted
    row in this table.
18. No message is accepted merely because a durable projection event exists;
    the manager shows a delivery claim only when the target owner acknowledges
    it.
19. Every `/workflow` subcommand works from the selected background session's
    input composer: workflow-name selection, default reset, checkpoint resume
    with optional run ID, and checkpoint discard via `reset <run-id>`. All
    mutations are performed by the owning worker with attempt/lease fencing and
    the canonical workflow recovery/checkpoint APIs. The exact command text is
    forwarded to the target unchanged; the target's native workflow command
    handler processes it rather than the manager rewriting it or sending it as
    ordinary LLM prose.
20. A selection-only `/workflow <name>` or reset command records its result,
    leaves the session recoverable for a later manager input, and does not
    replay the old launch intent. Any following queued input is still delivered
    unchanged and is not rewritten into a workflow start or resume. Invalid
    selectors and unsafe checkpoint operations produce target-side warning
    output and a completed input receipt, not a false `not delivered` result.
21. An ordinary message sent to a workflow-backed stale/recoverable session is
    forwarded verbatim as a normal user turn in the existing session. It does
    not inspect checkpoints, resume a workflow, start a new workflow, or replay
    the session's original intent. A checkpoint validation error cannot be
    generated by this ordinary-input path.
22. The manager/inbox is purpose-agnostic for all accepted text: it does not
    classify a message as "resume", "continue", or "command". The target
    session receives the exact text and its existing input/command path owns
    any interpretation. In particular, workflow continuation is never
    inferred from ordinary text. A target-side command warning is observable
    in the session transcript and does not become a manager-side delivery
    failure.
23. The composed payload is preserved exactly from editor submission through
    inbox persistence and target dispatch, including leading/trailing
    whitespace, tabs, and newlines. Validation may reject blank or oversized
    input, but accepted input is never silently trimmed, truncated, or rewritten;
    this applies equally to slash commands and ordinary text.

## 12. Verification plan

### Unit tests

- Detail-view key handling opens input only for the captured selected session;
  modal keys do not fall through to manager cancel/archive/attach actions.
- Shared editor tests verify exact parity for every capability and registered
  trigger, including condensed paste cursor boundaries, backspace, Esc, and
  Ctrl+J/Enter behavior.
- Target context tests assert mention cwd, mode/policy, and command registry
  are sourced from the target, not manager cwd/session.
- Inbox envelope validation covers empty/oversized text, malformed IDs,
  unknown session, stale owner/attempt, and queue capacity.
- FIFO, idempotency-key replay, acknowledge/consume transitions, and stale
  attempt fencing are tested without a real provider.
- Exact-payload tests cover editor submission, live/deferred inbox persistence,
  ask-user answers, command delivery, and worker dispatch with surrounding
  whitespace and multiline text.
- State-specific tests cover live, waiting-input, waiting-approval, recoverable
  stale, completed, and deleted sessions.
- Deferred-input tests verify enqueue-before-resume, attempt/lease rebinding,
  background-only recovery, and pending receipts when launch fails.
- Workflow-input tests prove ordinary text reaches the existing session turn
  unchanged and does not inspect, resume, or start a workflow regardless of
  whether a compatible checkpoint exists. Explicit `/workflow resume` tests
  separately verify checkpoint selection and target-side warnings.
- Pagination tests verify the interactive table is capped at ten sessions per
  page and excludes deleted sessions, including after refresh.
- Failure tests verify a failed submission retains the draft and does not
  clear it until acceptance.

### Integration tests

- Start a real background worker with a deterministic fake provider; submit
  from a separate manager-side service instance and verify the owning worker
  receives the message through its shared session dispatcher.
- Verify a message arriving during a busy turn queues, then appears exactly
  once in target conversation history and starts only after the configured
  safe boundary.
- Verify ask-user answer and generic turn delivery are mutually exclusive.
- Verify registered `/` command and `@` mention are interpreted in the target
  workspace/config context.
- Crash/restart the manager and worker at acceptance/dispatch/ack boundaries;
  verify pending receipts, idempotent recovery, and owner lease safety.
- Inject slow IPC/store operations and assert manager service calls remain
  bounded and asynchronous.

### End-to-end tests

- With a scripted terminal, navigate list → details → `i`, type ordinary and
  multiline input, submit, and remain in `agents` details while the session
  records and processes the message.
- Paste a condensed large block, edit/delete it with standard paste keys, and
  verify the exact submitted payload reaches the selected session.
- Use `@` and slash/skill pickers while manager cwd differs from the target
  workspace; assert target-side routing and resolution.
- Send to two sessions in succession while projections reorder; each command
  must remain pinned to the session whose details opened its composer.
- Exercise a running and stale workflow session with ordinary text; verify the
  exact text reaches a normal target turn and does not inspect, resume, start,
  or mutate a workflow checkpoint. Separately exercise explicit
  `/workflow resume` and verify its checkpoint semantics.
- Exercise pending question, pending approval, worker exit, full queue, stale
  owner states, and a stale recoverable session; verify correct feedback and
  that recovery never attaches to the foreground.
- Keep existing detail attach E2E coverage passing unchanged.

## 13. Implementation sequence

The implementation sequence below was completed against the existing session,
workflow, and TUI ownership boundaries.

1. **Input contract audit:** map every `UnifiedInputSession` capability,
   registered trigger, render dependency, and `TUISession.handle_send()` side
   effect. Define a typed target input/receipt protocol.
2. **Shared editor adapter:** extract only the UI/input pieces needed to bind
   the existing editor to an alternate target-bound submit function; retain
   normal TUI behavior and tests.
3. **Worker inbox:** implement bounded durable enqueue, idempotency, owner and
   attempt fences, wake-up/consumption, and recovery receipts.
4. **Target dispatcher:** share message routing between TUI and background
   execution. Add safe-boundary handling for live workflow/agent turns and
   preserve ask-user/approval semantics.
5. **Manager composer:** add `i` detail mode, target context, async submit,
   status rendering, draft preservation, and responsive modal key handling.
6. **Compatibility and lifecycle:** verify process restart, cancellation,
   worker completion, retry, and attach coexist without ownership races.
7. **Documentation and release:** document key behavior and supported session
   states; update the PRD index and tests/gates for touched surfaces.

## 14. Operational requirements

- The manager remains responsive with a slow or unreachable worker.
- Inbox storage and user-visible receipt state are bounded and observable.
- The worker uses an event-driven wakeup where available; any polling fallback
  has bounded cadence and shutdown behavior.
- No new listener is bound to a public network interface. Local IPC must retain
  session ownership and authorization checks.
- Accepted text is retained according to the selected session's existing
  transcript/journal retention policy; generic manager logs contain IDs and
  states, not text contents.
- Existing session/workflow durable checkpoints remain the source of workflow
  recovery. Inbox processing must not create a parallel checkpoint format.

## 15. Definition of done

The feature is complete when a user can open a live or stale-recoverable
session's details, compose input with the same editor features used by the
regular TUI, and submit to that exact session without attaching it to the
foreground. Live workers process input once through their normal routing and
policy path. Stale recoverable sessions persist input before background
recovery and deliver ordinary text as a normal turn without implicit workflow
resume/restart; explicit workflow commands retain their target-side behavior.
Pending input is retained if startup fails. The interactive table shows at most ten
rows per page and omits deleted sessions. Delivery is durable across process
boundaries, recoverable at crash boundaries, and compatible with existing
attach, question, approval, and workflow-resume behavior. Unit, integration,
and end-to-end evidence must cover those claims.

## 16. Supersession note

[PRD-216](prd-216-input-to-completed-background-sessions.md) supersedes this
PRD's completed-session rejection rule only. Completed sessions may accept an
explicit new target-bound input and start a new input-driven attempt; deleted
sessions remain unavailable. All other PRD-215 requirements continue to
apply.
