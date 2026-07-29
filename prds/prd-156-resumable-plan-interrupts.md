---
title: "PRD-156: Resumable Plan-Mode Interrupts and Workflow Continuation"
status: Implemented
version: 0.4.0
created: 2026-07-29
study_date: 2026-07-29
scope: Preserve and resume workflow state when Esc interrupts Plan-mode thinking
related_prds:
  - PRD-74   # input capability pipeline
  - PRD-86   # plan approval overlay
  - PRD-89   # workflow guards
  - PRD-91   # Plan-mode enforcement
  - PRD-100  # code_plan architecture
  - PRD-129  # conversation durability and retry resilience
  - PRD-148  # unified interrupt and graceful cancellation
  - PRD-154  # create_workflow architecture
  - PRD-155  # Safe, Plan, and Yolo modes
tags:
  - workflows
  - plan-mode
  - interrupt
  - resume
  - durability
  - tui
---

# PRD-156 — Resumable Plan-Mode Interrupts and Workflow Continuation

Study date: 2026-07-29. This PRD addresses a current interactive TUI failure:
when the user presses Esc while a Plan-mode workflow is thinking and then sends
another message, the queued message starts the workflow from its first phase.
The new agent has no access to the cancelled runner's in-memory phase context,
so it appears to forget the original request and all progress made before Esc.

The new behaviour is deliberately narrower than a generic retry. Esc in a
workflow-bound Plan turn becomes **pause and preserve**: it stops the current
LLM turn, checkpoints the workflow at its current phase, and makes the next
ordinary user message a continuation of that same run. It must never silently
create a new run as a fallback. An explicit reset remains available when the
user wants to discard the paused run.

“Same conversation” here means one session-scoped LLM conversation reused by
direct turns and every workflow phase in that session. Workflow context remains
workflow-scoped; provider message history is session-scoped. The existing reactive
[`tui/conversation_store.py`](../src/agenthicc/tui/conversation_store.py) is a
UI transcript and event projection; it is not the provider-ready message
history consumed by lauren-ai. The session conversation should use the
existing journaled memory boundary and feed events into the UI store, rather
than making the UI projection a second agent-memory implementation.

## 1. Executive summary

The current path is:

```text
Esc
  → STREAMING InterruptCapability
  → InterruptAgentCommand
  → TUISession._cancel_active_task()
  → cancel the entire agent/workflow task
  → discard local runner/context
  → queue drains through advance()
  → a new run_turn() creates a fresh runner
  → the mode's workflow starts at PLAN/DESIGN again
```

`WorkflowRunner` and `CodePlanRunner` already have typed contexts and a
`resume(context)` API, but the interactive owner does not retain or serialize
the context created by `run()`. Their resume methods are therefore useful for
explicit historical resume paths, not for the same-session Esc → follow-up
race. `create_workflow` is even more explicit: its current `resume()` starts a
new state machine from DESIGN because it writes its artefact directly to disk.

This PRD introduces:

1. a session-owned conversation and resumable workflow handle containing one
   workflow context plus a reference to that session conversation;
2. runner injection so direct turns and every workflow phase reuse the same
   conversation instead of constructing a new memory object;
3. a versioned checkpoint that persists the conversation journal position and
   serializes only the workflow state needed to rehydrate it;
4. a distinct pause path for Esc on workflow-bound Plan turns;
5. queue routing that attaches follow-up input to the paused run; and
6. explicit reset and fail-closed behaviour when the conversation or checkpoint
   cannot be restored.

The design preserves completed side effects and tool results. It does not claim
to undo filesystem, Git, network, MCP, or other external effects that occurred
before the interrupt.

## 2. Evidence-backed current-state study

The study is based on the current source tree, not historical workflow PRDs.

### 2.1 Reproduction

1. Select `Plan` and submit a change request.
2. While the workflow is in an LLM thinking/streaming turn, press `Esc`.
3. Before cancellation cleanup has fully completed, submit a follow-up such as
   `continue with the README changes`.
4. Observe that the queued message eventually starts a new workflow run. The
   plan phase begins again, and the new runner does not receive the prior
   runner's phase context or its association with the session conversation.

The same failure can occur when the follow-up is queued after cancellation has
returned the input session to IDLE: the queue contains text, but no association
with the interrupted workflow run.

### 2.2 Runtime ownership and gaps

| Concern | Current implementation | Resulting gap |
|---|---|---|
| Esc handling | `tui/input/capabilities.py:InterruptCapability` treats Esc and Ctrl+C as the same streaming interrupt | There is no pause/resume disposition or workflow identity on the command |
| Active task ownership | `TUISession._agent_task` is cancelled by `_cancel_active_task()` in `runners/tui_session.py` | The task owns a local runner and local workflow context; cancellation destroys both |
| Follow-up queue | `handle_send()` appends ordinary input to `_msg_queue`; `advance()` starts `agent_task_body()` after cancellation | A queued message is not linked to the interrupted `run_id` |
| Generic workflow context | `WorkflowRunner.run()` creates a local `WorkflowContext` and one `_shared_memory` object reused by its live phases; `resume()` accepts a context object but creates fresh memory | No TUI checkpoint is produced or restored for the active session, so the live shared memory disappears on cancellation |
| `code_plan` context | `CodePlanRunner.run()` creates a local `CodePlanContext` with one `ctx.shared_memory` reused by its live phases | Plan, execute summaries, command outcomes, and current state disappear with the task; `resume()` creates a fresh memory instead of restoring the interrupted one |
| Reactive conversation store | `tui/conversation_store.py` lives for the TUI session and renders `ConversationTurn`/`ConversationEvent` values | It is not the provider-ready LLM memory and cannot restore workflow reasoning by itself |
| Cancellation result | `WorkflowRunner` and `CodePlanRunner` set `WorkflowRun.status` to `failed` on `CancelledError` | A user pause is reported as a failure and has no continuation handle |
| Durable projection | Kernel reducers record workflow start and completed phases | There is no generic serialized checkpoint for an in-progress phase or its context |
| Existing resume command | `/workflow resume` currently explains that `create_workflow` has no staged run | It cannot resume a paused interactive Plan run |
| Authoring workflow | `CreateWorkflowRunner.resume()` intentionally calls `run(context.intent)` | A generic resume path silently restarts DESIGN, which is unsafe for this use case |

### 2.3 Failure timeline

```text
T0  Plan request starts run R1; CodePlanRunner creates context C1 and memory M1.
T1  C1 reaches phase EXECUTE; tools and summaries are recorded in C1/M1.
T2  The LLM is thinking inside the current phase; no transition tool has fired.
T3  Esc cancels TUISession._agent_task. C1, M1, and the phase-local closure die.
T4  The runner marks the workflow failed and TUISession returns to IDLE.
T5  Follow-up F is released from _msg_queue.
T6  run_turn(F) creates runner R2, context C2, and memory M2.
T7  R2 begins PLAN, despite R1 having already reached EXECUTE.
```

The bug is not primarily a model-context-window problem. It is an ownership
and durability problem: the only object that knows the active phase is scoped
to the cancelled task, while the queue knows only the text of the next user
message.

### 2.4 ConversationStore is not lauren-ai memory

The name `ConversationStore` refers to two different abstractions:

- `agenthicc.tui.conversation_store.ConversationStore` is the reactive TUI
  projection. `_run_agent_turn()` places it in `AgentTurnContext` as
  `conv_store`; the turn runner uses it for `begin_turn()`, visible text and
  system events, token totals, tool status, and `close_turn()`. It does retain
  `ConversationTurn` and `ConversationEvent` values for the session UI, but it
  has no provider-message API and is not passed to lauren-ai as memory.
- lauren-ai's `ConversationStore` is an asynchronous persistence protocol. Its
  `AgentRunner.run_stream()` integration loads and saves provider-ready memory
  only when a stable session `conversation_id` and a configured store are
  supplied.
  lauren-ai does not contain an `_run_agent_turn()` function; its corresponding
  entry point is `AgentRunner.run_stream()`.

In agenthicc, the authoritative LLM history is the `session_memory` argument.
`AgentTurnRunner._stream()` passes it as `memory=ctx.session_memory` to
lauren-ai. Lauren-ai appends the user message, assistant completion, and
consolidated tool results to that object; agenthicc's direct session memory is
also backed by `JournaledShortTermMemory`. The same TUI `conv_store` instance
can and should continue to receive the resulting display events, but it must
not be treated as a second provider-memory implementation.

The implementation consequence is precise: retain one session-scoped
`ShortTermMemory`/journal object, keyed by the session's stable
`conversation_id`, and inject that same object into direct turns and every
workflow phase. Rehydrate it once when the session starts or recovers. Do not
create parallel authority by simultaneously reconstructing LLM history from
the TUI event list. If a lauren-ai `ConversationStore` is selected instead,
use the same session `conversation_id` for every call and make that store the
sole durable memory boundary; do not combine it with independent workflow
stores. In either design, context-window trimming and compaction remain
intentional limits; “same conversation” does not mean an unbounded provider
request.

## 3. Problem statement and root cause

Users reasonably interpret Esc during generation as “stop this response so I
can redirect or continue,” not “forget the workflow and create a new plan.”
The current implementation violates that expectation in four ways:

1. **The interrupt boundary is too high.** Esc cancels the entire workflow task,
   not only the current agent turn.
2. **The workflow's conversation binding is ephemeral.** Runner state and the
   workflow's phase-shared memory object are local to `run()` and are not bound
   to the session conversation owned by `TUISession` after cancellation.
3. **The UI store is the wrong recovery source.** It retains rendered event
   summaries, not the exact assistant/tool message sequence required by the
   provider.
4. **The queue is context-free.** A follow-up is released as a new intent,
   without a run ID, phase, or continuation disposition.
5. **Cancellation is misclassified.** A user interruption becomes `failed`,
   which makes a future continuation look like an unrelated fresh workflow.

The one-sentence root cause is:

> Esc destroys the only in-memory owner of the workflow context and its single
> phase-shared LLM conversation before that state has been retained by the
> session owner, and the subsequent FIFO message has no run identity with which
> to continue it.

## 4. Goals and non-goals

### Goals

- Make Esc on an active workflow-bound Plan turn pause and preserve the current
  run instead of silently restarting it.
- Use exactly one session-scoped LLM conversation, identified by the stable
  session `conversation_id`, for direct turns and every workflow phase,
  retry, continuation, and resume.
- Resume from the interrupted phase and preserve the original user intent,
  completed phase outputs, artefacts, tool results, approval decisions, mode
  choices, and phase iteration counters.
- Attach a follow-up submitted during cancellation to the paused run exactly
  once, preserving FIFO order for additional messages.
- Prevent completed tools and side effects from being re-executed merely because
  an LLM turn was interrupted.
- Make the paused state visible in the TUI and durable across a process restart;
  persistence rehydrates the same session conversation instead of creating a
  parallel context copy.
- Provide an explicit `/workflow resume` and `/workflow reset` contract.
- Fail closed when a checkpoint is missing, corrupt, incompatible, or belongs
  to a different workflow/plugin version; never silently start from phase one.
- Give generic `WorkflowRunner`, `CodePlanRunner`, `CreateWorkflowRunner`, and
  downstream custom runners a clear single-conversation and checkpoint
  contract.
- Preserve Plan's hard capability boundary and restore temporary phase modes
  without granting a checkpoint more authority than the current session mode.

### Non-goals

- Undoing arbitrary external side effects that completed before Esc.
- Replacing lauren-ai's agent loop, transport, memory implementation, or tool
  executor with a second agent runtime.
- Treating a partial, uncommitted provider stream as a completed assistant
  message. The next continuation may receive a bounded interruption note instead.
- Automatically resuming a paused workflow on application startup without user
  intent.
- Inferring that every new message is a new workflow. In the paused-workflow
  state, ordinary text is a continuation; `/workflow reset` is the explicit
  escape hatch.
- Making direct non-workflow turns behave as workflow continuations. Their
  existing interrupt and resume rules remain governed by PRD-129 and PRD-148.
- Using the reactive UI `ConversationStore` as a substitute for the provider's
  typed LLM memory.
- Persisting arbitrary Python objects or pickled plugin state.

## 5. Resolved product contract

### 5.1 Esc disposition

| Situation | Esc result |
|---|---|
| Active workflow-bound Plan phase | Pause the workflow at its current phase and preserve a checkpoint |
| Active direct turn | Keep the direct-turn interrupt contract from PRD-148 |
| Approval/question overlay owns the terminal | The overlay consumes Esc; it is not forwarded to the workflow interrupt path |
| No active run | Keep normal idle input behaviour |
| Cleanup is already in progress | Coalesce the request; do not cancel twice or advance the queue twice |

The command must carry its source/disposition, for example
`InterruptAgentCommand(source="escape", disposition="pause")`. The exact type
may be refined during implementation, but the distinction must not be inferred
from timing or from whether `_agent_task` happens to be done.

Ctrl+C, `/cancel`, and `/interrupt` retain the stronger cancellation semantics
defined by PRD-148 unless the user explicitly chooses a resume-capable workflow
pause command. They must not accidentally use the Esc pause path merely because
the same task owner is shared.

### 5.2 Paused workflow state

When Esc is accepted:

1. The active run enters `pausing` and stops accepting new phase transitions.
2. The current agent turn is cancelled cooperatively.
3. The latest safe checkpoint is written and acknowledged.
4. The workflow is projected as `paused`, with its `run_id`, workflow name,
   current phase, and checkpoint age visible to the TUI.
5. The input session returns to IDLE with an editable composer.
6. The TUI displays an actionable message such as:

   ```text
   ⏸ Plan paused in execute. Progress is preserved; your next message will continue this run.
   ```

No `WorkflowRunStarted` event is emitted for the continuation. The same
`run_id` remains authoritative.

### 5.3 Continuation routing

- Ordinary text submitted while a workflow is paused becomes a continuation
  instruction on the existing run. It does not replace the immutable original
  intent.
- A continuation prompt includes the original intent, current phase, completed
  context, a bounded interruption note, and the new user message.
- If the message arrived while cancellation was unwinding, it remains in the
  FIFO queue until the checkpoint is acknowledged; then it is claimed by the
  paused run rather than passed to `run_turn()` as a new intent.
- Additional ordinary messages remain FIFO. The first resumes the run; later
  messages are handled at the existing safe phase/tool boundary or remain
  queued according to the normal busy policy.
- `/workflow resume` resumes the paused checkpoint without adding a new user
  instruction.
- `/workflow reset` discards the paused continuation handle after writing a
  terminal `discarded` record. The next ordinary message starts a new run.
- Selecting a different `/workflow <name>` while paused does not mutate the
  current checkpoint. The command reports that a paused run exists and asks the
  user to resume or reset it, preventing accidental cross-workflow reuse.

### 5.4 Phase and transition semantics

- If Esc occurs before a phase transition tool call, the phase remains current.
  The continuation re-enters that phase's inner loop with its previous attempt
  count and context.
- If a transition tool call was durably committed before Esc, the checkpoint
  advances exactly once and continuation starts at the next phase.
- A cancelled in-flight model response never counts as a completed phase and
  cannot trigger a transition by cleanup timing.
- Completed tool calls and results are retained in the run ledger. A resumed
  phase may inspect them but must not invoke the same idempotency key twice.
- A phase-local approval or question wait is restored as `pending` only when it
  has a durable request record. Otherwise the resumed phase asks again with a
  new request ID and explains why.

### 5.5 Session-wide conversation identity

The session owns one stable `conversation_id`, normally the TUI `session_id`.
That identifier and its provider-message history survive mode changes and
workflow changes:

```text
conversation_id = session_id

direct chat ───────┐
Plan mode ─────────┤
code_plan ─────────┤──► one SessionConversation / one JournaledShortTermMemory
create_workflow ──┘
```

This means a user may talk directly to the assistant, enter Plan mode, run
`code_plan`, then run `create_workflow`; each later agent turn can see the
earlier session history subject to the configured context window and
compaction summary. The history is semantically continuous, but the workflow
contexts are not merged: each workflow still owns its phase state, artefacts,
transition tools, approvals, and checkpoint.

`conversation_id` is an identity, not magic state sharing. The runtime must
also pass the same session memory object to every `run_stream()` invocation.
Passing the same ID while constructing a new memory object per workflow is not
sufficient. The canonical implementation uses the existing
`JournaledShortTermMemory` and session journal as the sole memory authority.
The lauren-ai `ConversationStore` load/save path may replace that journal only
as a deliberate architectural choice; it must not run as a second independent
store.

Only one agent turn may mutate a session conversation at a time. A workflow
switch is accepted at an idle boundary, never while two models/tools can append
concurrently. If `code_plan` is paused, selecting `create_workflow` is rejected
until the user resumes or resets the paused run; after a safe completion or
reset, a new `create_workflow` handle references the same session conversation.
This preserves the session-wide history without cross-wiring two active
workflow contexts.

## 6. Proposed solution

### 6.1 One session conversation as the source of truth

Create one `SessionConversation` when the TUI session starts. Its stable
`conversation_id` is the session ID, and its `JournaledShortTermMemory` is
reused by direct turns and every workflow run in that session:

```text
SessionConversation                         WorkflowRunHandle
  conversation_id = session_id                run_id
  journal                                        workflow_name / version
  memory ───────────────────────────────────►   original_intent
  cursor                                         lifecycle
  turn_lock                                      current_phase / iteration
  active_owner_run_id                            workflow_context
                                                  conversation ──┐
                                                  checkpoint      │
                                                                  │ reference
                                                                  ▼
                                                        SessionConversation.memory
```

The session conversation is separate from the reactive UI store, but it is not
separate from direct chat, Plan mode, `code_plan`, or `create_workflow`. The
important invariant is one conversation object/journal per session, not one
memory object per workflow or phase. Workflow handles own orchestration state;
they reference the session conversation rather than owning a second one.

`TUISession` creates and folds the session conversation before accepting the
first turn. `WorkflowConfig` passes the same object into every runner.
`WorkflowRunner` and `CodePlanRunner` use it as their shared memory;
`CodePlanContext.shared_memory` is a reference to the session memory, not a new
allocation. Direct `_run_agent_turn()` calls receive the same object. The outer
workflow loop, all inner phase loops, and later workflows therefore see the
same provider-ready assistant, tool-call, tool-result, and direct-chat history.

The runtime must pass `conversation_id=session_id` into lauren-ai's agent
context on every turn. With the canonical journal-backed implementation, the
explicit `memory=session_conversation.memory` object is authoritative and the
lauren-ai `ConversationStore` load/save path is disabled or adapted to that
same journal. Passing the same ID to independent memory objects would not
provide the required continuity.

The reactive `ConversationStore` remains the UI projection. Every rendered
workflow event should carry `run_id` and phase metadata, but the UI store does
not become the provider-memory source. It can be rebuilt from the journal and
does not need to contain provider-specific message objects.

### 6.2 Session-owned handle and live continuation

The handle is owned by `TUISession` or a small runner-boundary service and
survives replacement of the asyncio task. During a same-process Esc pause it
retains the workflow context and a reference to the session conversation; no
serialization round trip is needed before the user continues. A workflow
switch does not create another conversation: it releases and reacquires the
session conversation's turn lock at a safe boundary.

The live handle must not retain an event loop, `asyncio.Event`, phase-local
callback closure, transport, or arbitrary agent instance. Transition events and
approval/question gates are recreated for the resumed phase and are resolved
from durable evidence where available. The typed workflow context and the
session conversation reference are the live progress state that crosses the
task boundary; the session journal is the durable provider-memory state.

Each runner type supplies a context adapter:

- `WorkflowRunner` restores `WorkflowContext` and its `PhaseOutput` values while
  attaching the existing session conversation.
- `CodePlanRunner` restores `CodePlanContext` and `CodePlanState`, including
  `plan`, execution/review summaries, rejection reason, command outcomes, and
  the same session shared memory object.
- `CreateWorkflowRunner` must stop claiming that resume always restarts DESIGN.
  It either uses the same conversation/context contract for DESIGN/GENERATE/
  VALIDATE/SUMMARIZE or declares itself non-resumable. A non-resumable workflow
  must reject Esc pause with an explicit message rather than silently restarting.
- Downstream custom runners must declare checkpoint support and provide a
  serializable context adapter. The generic runner may provide a phase-boundary
  fallback, but it must not serialize arbitrary runner internals.

`BaseWorkflowRunner` should expose the narrow lifecycle hooks needed by the
owner, tentatively:

```python
attach_conversation(conversation: SessionConversation) -> None
export_context() -> WorkflowContextSnapshot
restore_context(snapshot: WorkflowContextSnapshot) -> None
supports_checkpoint_resume: ClassVar[bool]
```

The final API may use a separate context/codec protocol, but every supported
runner must be able to continue with the same session `conversation_id` and
the session memory object.

### 6.3 Checkpoint as persistence for the conversation

The checkpoint is not a second source of truth and must not duplicate the full
LLM message history. It persists workflow state and a position in the one
session conversation:

| Field | Requirement |
|---|---|
| `schema_version` | Reject unknown incompatible versions explicitly |
| `run_id`, `workflow_name` | Stable identity and ownership |
| `intent` | Immutable original request |
| `state`, `current_phase`, `phase_index` | Exact resume location |
| `phase_iteration` | Prevent retry-budget reset |
| `phase_outputs` / artefacts | Structured, bounded, serializable output from completed phases |
| `phase_metadata` | Approval, execute mode, model, transition, and validation evidence |
| `conversation_id`, `conversation_cursor` | Reopen the same session-journaled LLM conversation; the cursor is the position observed by this workflow, not an exclusive lock on later session messages |
| `command_outcomes` / tool-ledger cursor | Preserve completed tool results and idempotency evidence |
| `mode_identity` | Mode selected by the user and temporary phase override |
| `status`, `revision`, timestamps | Lifecycle and monotonic checkpoint ordering |
| `plugin_fingerprint` | Detect a changed workflow definition before restore |

The checkpoint format uses JSON-compatible values with explicit size limits.
Conversation messages and large artefacts stay in the journal/artifact
boundary and are referenced by sequence or content hash, rather than copied
into every checkpoint or kernel event. No pickle or executable code is allowed.

On a same-process continuation, the handle uses its existing context and the
session conversation directly. On process restart, the session loader opens
and folds the one session journal once, then restores the typed workflow
context from the checkpoint and attaches it to the shared session conversation.
If the journal was compacted after the workflow checkpoint, the loader uses the
current summary/window plus the cursor metadata; it does not reject merely
because later direct or other-workflow messages were appended. If the journal
or context cannot be restored, recovery fails closed.

### 6.4 Durable storage and events

Use a session-scoped `WorkflowCheckpointStore` under the existing session
storage boundary. It must:

1. write a new revision to a temporary file;
2. fsync the file and atomically replace the previous revision;
3. fsync the containing directory where supported;
4. retain the last known-good revision until the new revision is valid;
5. enforce session ownership, file permissions, and size limits; and
6. validate schema, hash, run ID, workflow name, conversation key, and plugin
   fingerprint on load.

The kernel/session event stream records metadata, not duplicate large payloads:

```text
WorkflowCheckpointSaved
  run_id, workflow_name, checkpoint_revision, state, current_phase,
  status, store_key, content_hash, created_at

WorkflowRunPaused
  run_id, workflow_name, current_phase, checkpoint_revision, reason="escape"

WorkflowRunResumed
  run_id, workflow_name, current_phase, checkpoint_revision,
  continuation_message_id

WorkflowRunDiscarded
  run_id, workflow_name, checkpoint_revision, reason
```

The reducer stores the latest checkpoint pointer and lifecycle status in the
kernel workflow projection. It must target the exact `run_id`; one paused Plan
run must never mark unrelated workflows as paused. Existing
`WorkflowPhaseCompleted` and `WorkflowRunCompleted` events remain compatible.

Checkpoint writes happen at these boundaries:

- run creation;
- phase start;
- after each committed tool/result boundary that changes resumable state;
- approval/question request and resolution;
- transition-tool completion, before the outer loop advances;
- accepted Esc pause; and
- terminal completion, failure, cancellation, or discard.

The write after Esc is the acknowledgement that permits the queued
continuation to run. The checkpoint records the journal cursor; it does not
become a competing copy of the conversation.

### 6.5 TUISession and input integration

`InterruptAgentCommand` gains explicit source and disposition fields. The input
capability only translates a key into a command; it does not inspect workflow
state or cancel tasks directly.

The TUI owner changes as follows:

1. `run_turn()` registers a `WorkflowRunHandle` before starting the runner.
2. `handle_send()` checks for a paused handle before creating a new workflow
   task.
3. `_cancel_active_task()` is split into pause and terminal-cancel paths; it
   never clears the workflow handle for an Esc pause.
4. `agent_task_body()` catches the pause cancellation, persists the checkpoint,
   publishes the paused lifecycle event, and does not call `advance()` in a way
   that starts a fresh run.
5. `advance()` drains local slash commands normally, but routes the first
   ordinary message to `_resume_workflow_task(handle, message)` when a paused
   handle exists.
6. The resume task reuses the live runner context and conversation/tool ledger
   when still in process; after restart it rehydrates those same objects from
   the journal and checkpoint, enters the saved phase, and uses the same
   `run_id`.
7. A successful continuation clears the paused marker only after the next
   checkpoint is durable; a failure keeps the last good checkpoint available.

The composer remains usable after pause. The user must not have to race the
task finalizer or retype a message because a queue transition occurred between
Esc and Enter.

### 6.6 Memory and tool idempotency

The one session conversation is the memory used by direct turns and every
workflow phase. On restore:

- all committed user/assistant/tool messages needed to continue are present;
- an interrupted provider stream is represented as an incomplete attempt, not
  as a fabricated completed assistant message;
- the next prompt includes a bounded “interrupted while thinking” marker; and
- tool calls are looked up by stable run/phase/turn/tool-call IDs before
  execution.

If a tool was accepted and its result was durably recorded before Esc, the
resume path consumes that result. If execution status is unknown because the
process died before commit, the tool's declared idempotency policy determines
whether to retry, ask for approval again, or fail closed. This PRD does not
make non-idempotent external operations magically safe.

### 6.7 Mode and security boundaries

- A checkpoint may restore workflow context, but it cannot elevate the current
  session above Plan's hard capability restrictions.
- Temporary Safe/Yolo phase overrides are restored from the checkpoint only
  through the same validated `ModeManager` path used by a fresh run.
- Checkpoints are session-scoped, permission-restricted, and excluded from
  user-visible diagnostics unless explicitly requested.
- Kernel events contain metadata and hashes, not raw prompts, secrets, or full
  tool outputs.
- A plugin fingerprint mismatch pauses restoration and reports an actionable
  error. The user must reset or deliberately migrate the checkpoint; the system
  must not execute a different workflow under an old checkpoint.

### 6.8 Concrete implementation map

The implementation must make the ownership boundary visible in code. The
following modules are the expected homes; an equivalent decomposition is
acceptable only if it preserves the same responsibilities.

| Component | Responsibility | Required behaviour |
|---|---|---|
| `runners/session_conversation.py` | `SessionConversation` and turn ownership | Own the session `conversation_id`, one journaled memory object, journal cursor, and non-persisted turn lock. It is created/folded once and shared by direct turns and workflows. |
| `runners/workflow_handle.py` | `WorkflowRunHandle` and lifecycle transitions | Own the run ID, typed workflow context, reference to the session conversation, active turn identity, pause request, queued continuations, and checkpoint revision. It survives replacement of the asyncio task. |
| `workflows/checkpoint.py` | Serializable snapshots and runner codec protocol | Define JSON-compatible `WorkflowCheckpoint`, `WorkflowContextSnapshot`, schema validation, plugin fingerprint validation, and the adapter interface for generic, `code_plan`, `create_workflow`, and custom runners. |
| `memory/journal.py` / `memory/journaled.py` | Durable provider-message history | Reuse the session-scoped journal and expose an observable journal cursor. Preserve append/reset semantics and represent an interrupted turn as aborted, never naturally completed. |
| `runners/workflow_checkpoint_store.py` | Atomic checkpoint persistence | Write and validate revisions using temp-file + flush/fsync + atomic replace. Keep the previous valid revision until the new revision is accepted. |
| `workflows/plugin.py` | Workflow checkpoint hooks | Expose optional context codecs; the framework fingerprint covers the declared phase topology and fail-closed support is explicit when no codec exists. |
| `runners/tui_session.py` | Session ownership and input routing | Create the handle before starting a workflow, route Esc as pause, retain the handle after pause, and route the next ordinary message to continuation instead of `run_turn()`. |
| `runners/agent_turn.py` | One-turn abort and memory hygiene | Accept the pause boundary, stop the current stream, remove or reconcile incomplete provider messages, and leave the shared memory valid for the next phase/continuation. |
| `workflows/default/runner.py` | Generic phase restoration | Consume injected session memory; never replace it in `run()`, `resume()`, or phase execution. Export/import declarative phase state. |
| `workflows/code_plan/runner.py` | Typed code-plan restoration | Attach the injected session memory to `CodePlanContext.shared_memory`; restore plan, execute/review summaries, command outcomes, approval state, and phase counters. |
| `workflows/create_workflow/runner.py` | Authoring workflow capability | Implement a codec for supported phases or explicitly reject unsafe pause. It must never silently restart DESIGN for a continuation. |
| `kernel/events.py` / `kernel/reducer.py` | Lifecycle projection | Add typed pause/resume/checkpoint events and reducers keyed by exact `run_id`; store metadata and pointers, not provider messages. |

The key constructor rule is:

```python
session_conversation = SessionConversation.open(
    conversation_id=session_id,
    journal=ConversationJournal(journal_path_for(session_id)),
    max_tokens=budget,
)
handle = WorkflowRunHandle(
    run_id=run_id,
    workflow_name=plugin.name,
    context=context,
    conversation=session_conversation,
)
config = dataclasses.replace(workflow_config, workflow_handle=handle)
```

`WorkflowRunner`, `CodePlanRunner`, direct turns, and supported downstream
runners receive `session_conversation.memory` by injection. They may create
phase-local tools and closures, but they may not create another
`ShortTermMemory`, another conversation journal, or a second session
`conversation_id`. A phase-local closure is recreated after pause; the session
conversation, workflow context snapshot, and tool ledger are not.

The session conversation journal is intentionally shared with direct-turn
history. Workflow checkpoints remain distinct from the journaled provider
messages. A concrete storage layout is:

```text
~/.agenthicc/sessions/<session_id>/
  conversation-journal.jsonl       # shared by direct turns and workflows
  workflows/<run_id>/checkpoint.json
```

The path is an implementation detail, but the session journal must be opened
once per session and the workflow checkpoint must include the stable
`conversation_id` so a checkpoint cannot attach to another session's history.

### 6.9 Pause, checkpoint, and rehydration algorithms

#### Starting a workflow

1. `TUISession` creates/folds `SessionConversation` once, using
   `conversation_id=session_id`, before accepting direct or workflow turns.
2. When a workflow starts, it allocates `run_id` and computes the
   workflow/plugin fingerprint; it does not create another conversation.
3. It constructs the typed workflow context and `WorkflowRunHandle`, stores the
   handle as the session's active workflow, and writes an initial checkpoint
   before the first phase begins.
4. The runner receives the handle's context and the session memory reference.
   Every call to `_run_agent_turn()` receives
   `session_memory=session_conversation.memory`,
   `conversation_id=session_conversation.conversation_id`, and the same TUI
   `conv_store` projection.

#### Pausing on Esc

1. The input layer emits `InterruptAgentCommand(source="escape",
   disposition="pause")`; it does not cancel a task directly.
2. The session asks the active handle to pause. The handle atomically changes
   `running → pausing`, records the active phase/turn, and rejects transition
   requests until the pause is acknowledged.
3. The active turn is cancelled cooperatively. `AgentTurnRunner` must not
   convert this cancellation into a normal turn completion. It closes the UI
   turn, marks the provider attempt aborted, and restores memory to the last
   valid boundary. A partial assistant stream is never appended as a completed
   assistant message.
4. If a tool result was durably recorded, it remains in the journal and ledger.
   If a tool was in flight without a durable result, the checkpoint records
   `unknown`; resume applies that tool's idempotency policy rather than guessing
   success or silently running it twice.
5. The runner exports a bounded, typed context snapshot. The handle captures
   the journal cursor, message base count, phase iteration, tool-ledger cursor,
   and queued-message IDs.
6. `WorkflowCheckpointStore` validates and atomically writes the next revision.
   Only after the write succeeds does the handle change `pausing → paused` and
   release the queued continuation. If the write fails, the run remains
   recoverable in `pausing`/`failed-to-pause` and no queued message starts a new
   workflow.

The journal lifecycle must distinguish these outcomes:

```text
turn_started → turn_completed       # natural completed LLM turn
turn_started → turn_aborted          # Esc/cancellation before completion
turn_started → turn_recovered        # crash recovery after replay/rollback
```

`turn_aborted` replaces the current unsafe behaviour where cancellation can be
followed by a `turn_completed` marker. `fold_resume_state()` must treat only a
natural completion or explicit abort/recovery record as closed, and must return
the pre-turn base count plus durable tool records for an unclosed crash turn.

#### Rehydrating in the same process

1. The first queued ordinary message is claimed by the paused handle and given
   a stable continuation message ID.
2. The existing typed context and `JournaledShortTermMemory` object remain in
   use; no journal load or new run ID is needed.
3. The runner reconstructs the phase-local transition tools and approval gates,
   enters the checkpointed phase/iteration, and calls `_run_agent_turn()` with
   the same `session_memory` object.
4. The continuation prompt contains the immutable original intent, current
   phase/context, a bounded interruption marker, and the new user message. The
   new user message is appended once to the same provider history.
5. A transition can occur only when the phase's transition tool is called and
   its result is committed. The outer loop then checkpoints the new phase
   before entering it.

#### Rehydrating after restart

1. The session loader finds the latest valid checkpoint for the session and
   verifies `run_id`, workflow name, schema version, plugin fingerprint, file
   ownership, and revision hash.
2. It opens the checkpoint's `conversation_id` (which must equal the current
   session ID), folds the one session journal, and verifies that the journal
   cursor has not moved backwards from the checkpoint cursor. Later direct or
   other-workflow messages are valid appended history; a cursor that points
   into a compacted-away prefix is reconciled through the journal summary or
   reported as an actionable recovery error, not permission to start over.
3. It restores the typed context through the runner's codec, rebuilds the
   handle, and validates that the checkpoint's phase/context and memory base
   count agree.
4. `/workflow resume` or the first ordinary continuation message starts a new
   asyncio task with the same `run_id`, phase, context, journal, and tool-ledger
   evidence. It does not emit `WorkflowRunStarted`.
5. The first successful continuation writes a newer checkpoint and only then
   clears the paused marker.

### 6.10 End-to-end data flow

The following flow shows both authorities: the session journal/memory carries
provider context across direct turns and workflows, while the reactive
conversation store only mirrors events for the TUI.

```text
User types request
      │
      ▼
InputSession ──► TUISession.run_turn()
      │                 │
      │                 ├─ allocate run_id
      │                 ├─ open/fold session conversation once
      │                 ├─ use session JournaledShortTermMemory
      │                 ├─ create WorkflowRunHandle + typed context
      │                 └─ save initial checkpoint
      │
      ▼
Workflow outer phase loop
      │  handle.context + session_conversation.memory
      ▼
Phase inner agent loop
      │
      ├─ _run_agent_turn(session_memory=session_conversation.memory,
      │                   conversation_id=session_id,
      │                   conv_store=AppState.conversation)
      │       │
      │       ├─ lauren-ai run_stream(
      │       │      memory=session_conversation.memory,
      │       │      conversation_id=session_id)
      │       │       ├─ append user message ───────┐
      │       │       ├─ append assistant response  │
      │       │       └─ append tool results         │
      │       │                                     ▼
      │       └─ JournaledShortTermMemory ──► session conversation-journal.jsonl
      │
      ├─ UI/system/tool/token events ─────────────► TUI ConversationStore
      │                                             (render-only projection)
      │
      └─ transition tool committed
              │
              ├─ mutate typed workflow context
              ├─ update phase/run metadata
              └─ checkpoint(context snapshot + journal cursor)
                              │
                              └─ next phase uses same session_conversation.memory

Esc during the inner loop
      │
      ▼
InterruptAgentCommand(disposition="pause")
      │
      ├─ handle: RUNNING → PAUSING
      ├─ abort current turn; reconcile memory/tool ledger
      ├─ export typed context snapshot
      ├─ checkpoint context + phase + journal cursor atomically
      └─ handle: PAUSING → PAUSED
                              │
                              ▼
                    queued follow-up remains attached to run_id
                              │
                              ▼
      follow-up text or /workflow resume
                              │
                              ├─ same-process: reuse handle + session objects
                              └─ restart: reopen checkpoint + session journal
                              │
                              ▼
      RESUMING ──► same phase/iteration ──► same session memory ──► RUNNING
                              │
                              └─ no new WorkflowRunStarted, no PLAN restart
```

The critical invariant is that the downward provider path and the upward
checkpoint path share one `run_id` and one conversation cursor:

```text
phase state ───────────────┐
typed context ─────────────┼──► WorkflowCheckpointStore
tool ledger cursor ────────┤              │
conversation cursor ───────┘              ▼
                              rehydrate same run + same journal
```

The TUI `ConversationStore` receives a parallel display projection. It is not
read during rehydration, so missing or truncated display events cannot cause a
workflow to restart or lose provider context.

The sequential workflow-switch flow is:

```text
Direct user conversation
      │  conversation_id=session_id, memory=S
      ▼
Switch to Plan mode
      │  mode changes capabilities only; S is unchanged
      ▼
code_plan workflow
      │  context=C_plan, handle=H_plan, memory=S
      │  PLAN/EXECUTE/REVIEW messages append to S
      ▼
Safe boundary or explicit pause
      │  release session turn lock; checkpoint H_plan with cursor k
      ▼
Switch to create_workflow
      │  context=C_author, handle=H_author, memory=S
      │  new authoring prompt is appended after cursor k
      │  lauren-ai receives S's retained history/summary plus authoring context
      ▼
create_workflow continues
      │  its phase transitions mutate C_author only
      │  provider messages continue appending to S
      ▼
Resume code_plan, if requested
         restore C_plan from H_plan/checkpoint
         append an explicit "resume code_plan" marker to S
         continue at the saved phase; do not roll S back or create S2
```

The shared session history gives the model continuity, but workflow prompts
must delimit ownership explicitly. A `create_workflow` system/context prefix
must identify the active workflow and distinguish prior Plan/code-plan output
from current authoring instructions. A paused workflow must never resume by
rewinding the shared session history, because doing so would erase later direct
or other-workflow messages. It resumes by appending a bounded workflow-resume
marker to the same conversation.

## 7. State machine

```text
                     Esc accepted
RUNNING ─────────────────────────────────► PAUSING
   │                                         │
   │ transition tool + checkpoint             │ checkpoint acknowledged
   ▼                                         ▼
 next phase RUNNING                         PAUSED
                                                │
                    /workflow resume or text  │
                                                ▼
                                           RESUMING
                                                │
                                                └──► RUNNING (same run_id)

PAUSED ── /workflow reset ──► DISCARDED
RUNNING ── terminal cancel/failure ──► CANCELLED/FAILED
RUNNING ── terminal transition ──► COMPLETE
```

`PAUSED` is not `FAILED`, and a paused workflow is not a completed workflow.
The phase index remains stable during pause. A transition is recorded only by
the existing phase transition tool contract; Esc or queue handling cannot
advance a phase.

## 8. Compatibility and migration

### Existing sessions

- Sessions with no PRD-156 checkpoint continue to load their existing journal
  and kernel state.
- The reactive `ConversationStore` remains a rebuildable UI projection; no
  migration should attempt to turn rendered events into provider messages.
- New workflow runs attach to the already-open session conversation. Existing
  direct-turn memory therefore remains available to them; no second workflow
  journal is created. Sessions that predate the shared-conversation schema must
  migrate or explicitly start a new session conversation rather than silently
  mixing incompatible journal formats.
- If an in-progress workflow has completed phase events but no checkpoint, the
  system may reconstruct completed phase outputs and a conversation cursor when
  the data is sufficient. It must label the result as a degraded checkpoint and
  start at the first provably incomplete phase, never assume the whole run is
  fresh silently.
- If reconstruction is ambiguous, show the paused/incomplete workflow notice
  and require `/workflow reset` or an explicit recovery decision.

### Existing plugins

- `WorkflowPlugin` exposes optional `checkpoint_context_to_payload()` and
  `checkpoint_context_from_payload()` hooks for custom typed contexts. The
  framework computes a fingerprint from the plugin name and phase topology.
- Generic declarative workflows use the built-in codec.
- Specialized runners without a codec remain runnable, but Esc reports that
  the current workflow cannot be paused safely and preserves the existing
  terminal-cancellation path.
- `create_workflow` must update its `/workflow resume` message to reflect its
  actual checkpoint capability once implemented.

### Existing commands and events

- `/workflow reset` remains compatible and becomes the explicit discard action.
- `/workflow resume` gains a real paused-run path while retaining a clear error
  for a missing or incompatible checkpoint.
- Old `WorkflowRun.status` values remain valid. Consumers must tolerate the new
  `paused`, `pausing`, and `discarded` values.
- Old conversation and kernel events remain readable; new events are additive.

## 9. Acceptance criteria

### 9.1 Resolution of the original Esc problem

Yes—once implemented, this design directly resolves the reported failure:
pressing Esc during Plan-mode thinking no longer destroys the workflow's only
owner. The follow-up message is routed to the paused `run_id`, the checkpoint
rehydrates the same typed context and provider conversation, and the outer
workflow loop resumes at the saved phase instead of constructing a new runner
at PLAN. The guarantee is conditional on successful checkpoint acknowledgement;
if persistence fails, the system must keep the run blocked and report the
failure rather than silently starting a fresh workflow.

### Core reproduction

1. A Plan-mode workflow reaches EXECUTE, Esc is sent during an LLM thinking
   turn, and a follow-up is submitted before cancellation cleanup completes.
2. Exactly one `run_id` exists for the original request and continuation.
3. No second `WorkflowRunStarted` event is emitted.
4. The continuation starts at EXECUTE, not PLAN.
5. The same session `conversation_id` and memory/journal identity are used by
   direct chat, PLAN, EXECUTE, REVIEW, SUMMARIZE, and the continuation.
6. The original intent, approved plan, prior phase artefacts, and completed tool
   results are visible to the resumed agent.
7. The workflow does not report `failed` solely because Esc paused it.
8. After `code_plan` reaches a safe boundary, `create_workflow` can start with
   the same `conversation_id` and receives the retained prior session history;
   no second session memory object is created.
9. Resuming `code_plan` after `create_workflow` has appended messages preserves
   those later messages and continues from the code-plan checkpoint without
   rewinding the shared session conversation.

### Phase and side-effect safety

10. Esc before a transition tool leaves the current phase unchanged.
11. Esc after a transition checkpoint but before the next phase starts resumes
   at the next phase exactly once.
12. A tool completed before Esc is not called again on continuation.
13. A tool with an unknown commit status follows its idempotency policy and is
   never duplicated silently.
14. Phase iteration and retry budgets do not reset on continuation.
15. Approval and question waits cannot approve or execute a stale operation
   after pause.

### Queue and commands

16. A follow-up queued during cancellation attaches to the paused run after
   checkpoint acknowledgement.
17. Multiple follow-ups preserve FIFO order and are not lost or duplicated.
18. `/workflow resume` continues without changing the original intent.
19. `/workflow reset` discards the checkpoint and the next ordinary message
   starts a fresh run.
20. Selecting another workflow while a run is paused does not cross-wire the
   checkpoint.
21. A second agent turn cannot mutate the shared session memory while the first
    turn owns the session conversation lock.
22. Esc with no workflow or no active run preserves existing idle/direct-turn
   behaviour.

### Durability and safety

23. A process restart after an acknowledged pause reopens the same session
    conversation journal, restores the same workflow checkpoint, and resumes
    the same run ID and `conversation_id`.
24. Torn, corrupt, stale, schema-incompatible, or plugin-mismatched
   checkpoints fail closed with an actionable notification.
25. Checkpoint files are atomically written, permission-restricted, bounded,
   and do not expose secrets through kernel events or ordinary logs.
26. Plan capability restrictions remain enforced after restore; a checkpoint
   cannot grant write, execute, Git-write, or network capabilities in Plan.

### Workflow coverage

27. Generic declarative workflows reuse one session conversation and resume
    from a phase checkpoint.
28. `code_plan` preserves its typed context and the same session memory object
   across all phases and pause/continuation.
29. `create_workflow` either resumes each supported phase correctly or clearly
   rejects unsafe pause; it never silently restarts DESIGN under a continuation
   request.
30. A downstream custom workflow receives documented checkpoint hooks and a
   validation error when it declares unsupported state.

## 10. Verification plan

### Unit tests

- command source/disposition mapping for Esc, Ctrl+C, and slash cancellation;
- handle lifecycle and duplicate-request coalescing;
- one session conversation object/journal key shared across direct turns and all
  workflow phases;
- same-object identity for every `session_memory` injection into a live run;
- stable `conversation_id=session_id` propagation through direct, Plan,
  `code_plan`, and `create_workflow` turns;
- session turn-lock rejection/coalescing when two workflows attempt concurrent
  mutation;
- separation between provider memory and the reactive UI `ConversationStore`;
- aborted-turn journal records never masquerade as completed turns;
- checkpoint serialization, schema validation, size limits, fingerprints, and
  atomic revision selection, including the conversation cursor;
- `WorkflowContext` and `CodePlanContext` round trips;
- phase resume selection, iteration preservation, and transition ordering;
- tool-ledger idempotency decisions;
- queue routing for paused, running, reset, and missing-checkpoint states;
- mode restoration and Plan capability enforcement.

### Integration tests

- TUISession → CommandBus → runner pause and continuation;
- `WorkflowConfig` injects the one session conversation into every phase runner
  and never creates a per-phase or per-workflow replacement;
- a phase turn writes provider messages to the session journal while only
  display events are written to the reactive TUI store;
- direct chat → Plan/`code_plan` → `create_workflow` uses one conversation and
  retains prior history across the workflow switch;
- checkpoint store plus kernel/session event projection;
- cancellation during LLM thinking, tool execution, approval, question, and
  terminal wait;
- restart/reload from an acknowledged checkpoint;
- FIFO follow-ups submitted during the cancellation race;
- corrupted checkpoint and plugin-version mismatch handling.

### End-to-end tests

- mock-transport Plan-mode scenario matching the reproduction above;
- plan → execute progress survives Esc and continues without a second plan;
- a completed write/read tool result is not replayed after continuation;
- `/workflow resume` and `/workflow reset` through the real command/input path;
- generic custom workflow and `code_plan` coverage;
- non-resumable `create_workflow` behaviour is explicit and safe;
- no-active-run and direct-turn regressions remain unchanged.

Required commands for the implementation change:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

## 11. Rollout sequence

1. Introduce one session-scoped journaled conversation with stable
   `conversation_id=session_id`; inject it into direct, generic, and
   specialized runners while keeping the reactive UI store as a projection.
2. Add the typed workflow handle and context contracts without changing
   user-facing input behaviour.
3. Implement atomic checkpoint storage containing workflow context metadata,
   the shared session `conversation_id`, and the workflow's observed
   conversation cursor, plus additive event/reducer projections.
4. Implement generic `WorkflowRunner` and `CodePlanRunner` context adapters;
   add phase-boundary and interrupted-turn checkpoints.
5. Split Esc pause from terminal cancellation and route queued input through
   the paused handle.
6. Add restart recovery and explicit `/workflow resume`/`reset` behaviour.
7. Opt `create_workflow` and downstream custom runners into the context contract;
   report unsupported workflows explicitly.
8. Run the full unit/integration/E2E matrix and document the final storage and
   migration format.

## 12. Open implementation choices

These choices do not change the product contract, but should be settled during
implementation:

- Whether checkpoint payloads live beside the conversation journal or behind a
  kernel-owned artifact store, provided atomicity and session ownership remain.
- Whether the public codec is methods on `BaseWorkflowRunner` or a separate
  `WorkflowCheckpointCodec` protocol.
- The exact notification wording and status-bar visual treatment for PAUSED.

The implementation must not use any of these open choices to weaken the two
non-negotiable guarantees: a continuation keeps the same workflow identity and
an unrecoverable checkpoint never falls back to an implicit fresh Plan run.
