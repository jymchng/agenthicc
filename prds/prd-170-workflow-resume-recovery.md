# PRD-170 — Reliable `/workflow resume` and Durable Workflow Recovery

**Status:** Implemented
**Date:** 2026-08-06
**Scope:** Interactive TUI workflow selection, workflow checkpoints, session
conversation rehydration, runner resume contracts, generated workflows, and
workflow-related interruption/recovery tests.
**Related PRDs:** PRD-98, PRD-129, PRD-148, PRD-154, PRD-156, PRD-158,
PRD-163, PRD-169

## Summary

Make `/workflow resume` a reliable continuation command for a workflow that was
paused, interrupted, or left recoverable by a process crash. It must continue
the existing run from its durable phase state and the same session-scoped
provider conversation. It must never silently create a new run from the first
phase.

The current command is only partially wired. It can resume an in-memory
`WorkflowRunHandle` or a checkpoint whose status is already `paused`, but it
does not recover a checkpoint left in `running` state by a process restart. The
command picker also advertises workflow names but not the `resume` subcommand.
The result is that a user can see that a session had an in-progress workflow,
run `/workflow resume`, and receive “no paused workflow” even though a durable
checkpoint exists.

This PRD defines the implementation required to make resume correct across:

- Esc pause and explicit `/workflow resume` in the same TUI process;
- `agenthicc --resume <session-id>` followed by `/workflow resume`;
- process termination during a workflow phase or agent/tool turn;
- the generic `WorkflowRunner`, `code_plan`, `create_workflow`, and downstream
  custom runners;
- the journaled session conversation and PRD-169 tool-call transaction repair;
- plugin reloads, provider-profile changes, corrupt checkpoints, and stale
  terminal records.

## 1. Evidence-backed current-state study

### 1.1 Current command path

`/workflow` is intercepted by `TUISession.route()` and dispatched to
`_handle_workflow_command()` in
[`src/agenthicc/runners/tui_session.py`](../src/agenthicc/runners/tui_session.py).
The `resume` branch calls `_handle_workflow_resume(run_id)`, which currently:

1. rejects the command if another agent task is active;
2. accepts an optional run ID only when it matches the in-memory
   `_workflow_handle`;
3. requires `_workflow_handle.lifecycle` to be `paused` or `pausing`;
4. builds a new runner and calls `runner.resume(handle.context)`; and
5. adds a synthetic `[WORKFLOW RESUME]` user message to the existing
   `session_memory`.

This path is valid only when the current `TUISession` still owns the handle.

### 1.2 Current persistence path

`WorkflowRunHandle` persists an atomic JSON checkpoint at:

```text
~/.agenthicc/sessions/<session-id>/workflows/<run-id>/checkpoint.json
```

The checkpoint is bounded, hashed, and linked to:

- `conversation_id`;
- workflow and plugin fingerprint;
- current phase/index/iteration;
- typed workflow context;
- session conversation cursor;
- non-secret provider profile identity; and
- redacted prompt-cache metadata.

`TUISession._restore_paused_workflow()` scans this store during construction,
but it only considers checkpoint statuses `paused` and `pausing`. A fresh run
writes a `running` checkpoint at startup. A process crash or force-kill can
therefore leave the only durable workflow record in `running` state, which is
ignored on the next process. The startup notification is derived separately
from the kernel event projection and says to start a new run; it does not
attach the durable workflow handle.

There is a second durability gap: the initial checkpoint is written before the
first phase is entered, while current phase/context updates are held in memory
until pause or terminal completion. A crash inside a later phase can therefore
restore stale phase state even if the `running` checkpoint is made eligible.

### 1.3 Current runner contracts

- `WorkflowRunner.resume()` accepts a generic `WorkflowContext`, finds the
  first incomplete phase, and reuses `WorkflowConfig.session_memory` when it is
  supplied.
- `CodePlanRunner.resume()` accepts `CodePlanContext` and resumes its typed
  `CodePlanState`, but its generic-context fallback can start a fresh run.
- `CreateWorkflowRunner.resume()` resumes a typed `CreateWorkflowContext`, but
  intentionally falls back to `run(context.intent)` for a legacy generic
  context.
- Custom plugins must provide checkpoint codec hooks to persist custom context;
  unsupported contexts fail closed when a checkpoint is attempted.
- `WorkflowRunHandle.request_pause()` accepts only the `running` lifecycle.
  A second Esc during a resumed run whose lifecycle is `resuming` cannot turn
  that run back into a pause. The task is then cancelled as a failure.

These behaviours are individually understandable compatibility choices, but
they do not form a reliable end-to-end `/workflow resume` contract.

### 1.4 Current command-discovery gap

The built-in command metadata describes `/workflow <name> | reset`, and
`SlashCommandTrigger` completes only registered workflow names. `resume` and an
optional run ID are not discoverable through the picker. Existing coverage
tests also assert the old “create_workflow writes directly” message when no
handle is present, which preserves the broken contract instead of testing
durable resume.

### 1.5 Current data flow and failure flow

The intended current-process flow is:

```text
Esc
  -> InterruptAgentCommand(disposition="pause")
  -> WorkflowRunHandle.request_pause()
  -> cancel TUISession agent task
  -> runner unwinds and retains typed context
  -> _finalize_workflow_pause()
  -> checkpoint status=paused
  -> /workflow resume
  -> runner.resume(context)
```

The process-restart failure is:

```text
workflow starts
  -> checkpoint status=running, context at initial state
  -> phase/context changes remain in memory
  -> process is killed
  -> next --resume rebuilds session conversation and kernel events
  -> _restore_paused_workflow() ignores status=running
  -> startup says “send a message to start a new run”
  -> /workflow resume finds no _workflow_handle
  -> user cannot continue the original run
```

The required recovery flow is:

```text
process restart
  -> load session journal and workflow recovery records
  -> classify running/resuming record as interrupted, not complete
  -> validate checkpoint, plugin fingerprint, conversation cursor, profile,
     workspace and tool-transaction state
  -> attach one handle to the session's existing conversation_id
  -> show “workflow can be resumed” without auto-starting it
  -> /workflow resume [run-id]
  -> atomically claim the handle and mark resuming
  -> restore typed context and exact phase/state
  -> repair/replay the interrupted tool-call tail according to PRD-169
  -> call runner.resume(context)
  -> checkpoint each durable transition
  -> terminal completion or another recoverable pause
```

## 2. Problem statement and root cause

The root cause is a split recovery contract:

> `/workflow resume` is implemented as an in-memory paused-handle action,
> while workflow checkpoints and session journals imply a process-independent
> recovery model. The startup path does not convert an interrupted durable run
> into a resumable handle, and the command path has no durable run-selection or
> stale-state policy.

This is not primarily a model-context-window problem. The session conversation
already has a durable journal and a stable `conversation_id`. The missing work
is to make workflow identity, phase state, checkpoint lifecycle, in-flight
turns, and command routing share one recovery coordinator.

## 3. Goals

### G-1 — Resume the exact workflow run

`/workflow resume` must continue the selected run from its last durable phase
and typed context. It must preserve the original intent, completed artifacts,
phase outputs, branch/retry counters, approval decisions, browser metadata,
cache contract metadata, and workflow-specific state.

### G-2 — Recover after process interruption

A workflow interrupted by a crash, SIGTERM, terminal close, or force-kill must
be discoverable and resumable after `agenthicc --resume <session-id>` without
requiring a new user message and without silently restarting phase one.

### G-3 — Keep one session conversation

Direct turns, workflow phases, retries, continuation messages, and resumed
runs must use the existing session-scoped conversation and its stable
`conversation_id`. Rehydration must attach workflow context to that memory;
it must not reconstruct provider history from the reactive TUI transcript or
create a second conversation.

### G-4 — Preserve tool-call transaction integrity

An interrupted agent/tool turn must use PRD-169's canonical tool-batch
validation and repair. Completed tools must not be executed twice merely
because workflow resume was requested. Unresolved calls must be repaired or
rejected before another provider request.

### G-5 — Make the command discoverable and deterministic

Users must be able to discover `/workflow resume`, inspect available run IDs,
select a run when more than one is recoverable, and receive actionable errors
for missing, stale, incompatible, or corrupt state.

### G-6 — Make generated workflows resumable by construction

`create_workflow` must generate either a declarative workflow handled by the
framework codec or a custom runner with a bounded checkpoint codec and a true
resume dispatch path. It must instruct the authoring agent that a workflow may
be interrupted at any phase and must never implement resume as
`return await self.run(intent)` for a typed or recoverable context.

## 4. Non-goals

- Undoing filesystem, Git, network, browser, MCP, or other external effects
  that completed before interruption.
- Auto-starting a workflow merely because a recoverable checkpoint exists.
- Reconstructing provider memory from rendered `ConversationStore` events.
- Serializing live locks, asyncio events, browser objects, provider clients,
  credentials, raw tool handles, or arbitrary Python objects.
- Replacing lauren-ai's conversation/tool executor or creating a second agent
  runtime in agenthicc.
- Treating a provider or plugin incompatibility as safe to bypass by starting
  from the first phase.
- Guaranteeing rollback of a partially completed external command. Such work
  must use the existing command lifecycle, terminal ownership, and idempotency
  contracts.

## 5. Product contract

### 5.1 Command grammar

The accepted forms are:

```text
/workflow resume
/workflow resume <run-id>
/workflow reset
/workflow <workflow-name>
```

`/workflow resume` without an ID resumes the only recoverable run. If several
runs are recoverable, it opens a small selector or presents a deterministic
run list and requires a run ID; it must not choose an arbitrary run. An
explicit ID must be validated as a safe identifier and must resolve to the
current session's checkpoint only.

The command picker and `/help` must show `resume [run-id]` as an argument path.
Workflow-name completion must remain available and must not treat `resume` as
a plugin workflow name.

### 5.2 Resume is explicit, not implicit

Opening a session or running `--resume` may load the transcript and prepare a
recoverable workflow handle, but it must not invoke the LLM automatically.
The TUI must show a notification containing the workflow name, run ID (or a
safe short form), current phase, and the command to continue. The next
ordinary user message must not silently start a new workflow while a
recoverable workflow exists. Product behaviour must be one of:

- treat the message as an explicit continuation of the selected run; or
- require `/workflow resume` first and keep the message queued without losing
  it.

The chosen behaviour must be consistent for same-process pause and process
restart. `/workflow reset` remains the explicit discard path.

### 5.3 Recoverable lifecycle states

The resume coordinator must distinguish:

| State | Meaning | Resume action |
|---|---|---|
| `running` | Checkpoint was active when the process disappeared | Recover as interrupted, validate, then offer resume |
| `pausing` | Pause was requested but finalization may not have completed | Complete/repair pause finalization, then offer resume |
| `paused` | User-approved pause is durable | Offer resume |
| `resuming` | Previous process disappeared while resuming | Recover as interrupted and offer resume after validation |
| `complete`, `exited`, `discarded` | Terminal | Never resume |
| `failed` | Terminal failure | Do not resume automatically; expose failure and reset/new-run options |

Recovery must record why an active status was converted to a resumable state
(for example `process_interrupted`) without pretending that the prior turn
completed.

### 5.4 One authoritative recovery record

Introduce a session-owned recovery coordinator/repository around
`WorkflowCheckpointStore`. Its public result should include at least:

- session/conversation ID;
- run ID and workflow name;
- lifecycle/recovery disposition;
- original intent summary or a bounded redacted label;
- current phase and phase index;
- checkpoint revision and journal cursor;
- plugin fingerprint and compatibility result;
- whether the provider/tool tail needs repair;
- provider profile compatibility;
- workspace compatibility;
- recoverability/error code; and
- the exact checkpoint path for diagnostics, without exposing secrets.

The coordinator must atomically claim a run before resume so two commands,
two clients, or a queued command cannot execute the same workflow concurrently.
It must release or transition the claim on success, pause, failure, and process
termination.

### 5.5 Checkpoint durability points

The implementation must persist a checkpoint at minimum:

1. before the first phase starts, with the first phase and state recorded;
2. when entering every phase or state, before invoking its agent/tools;
3. after a phase transition and its artifacts/context are durably recorded;
4. before/after a pause finalization;
5. after a recoverable tool/turn boundary where PRD-169 requires a durable
   repair marker; and
6. at terminal completion, failure, exit, or explicit discard.

Checkpoint writes remain atomic, bounded, hashed, and secret-free. The
implementation may coalesce safe writes, but a crash must never leave a
checkpoint claiming a later phase than the context and journal can prove.

The checkpoint must carry a consistency relation such as:

```text
checkpoint.conversation_cursor <= current journal cursor
checkpoint.context state == checkpoint.current_phase
checkpoint.run_id == every related workflow event/run marker
```

If the relation cannot be proven, resume must fail closed with a repair/reset
diagnostic rather than restart from phase one.

### 5.6 Shared conversation and rehydration

The data flow for a successful resume is:

```text
checkpoint.conversation_id
  -> SessionConversation.open(conversation_id)
  -> fold conversation-journal.jsonl into one JournaledShortTermMemory
  -> validate/repair the provider tool-call tail
  -> context_from_payload(checkpoint.context, memory=session.memory)
  -> WorkflowRunHandle.from_checkpoint(..., conversation=session)
  -> WorkflowConfig(session_memory=session.memory,
                    conversation_id=session.conversation_id,
                    workflow_handle=handle)
  -> runner.resume(typed_context)
  -> _run_agent_turn(..., session_memory=session.memory,
                     conversation_id=session.conversation_id)
```

The TUI `ConversationStore` continues to render events and transcript replay,
but is not a recovery source for provider messages. A resumed workflow must
see prior direct turns, prior workflow phase turns, and the synthetic resume
instruction in the same session memory, subject to the normal bounded memory
and compaction policy.

### 5.7 Runner resume protocol

`BaseWorkflowRunner` and `WorkflowPlugin` must define one explicit protocol:

- `run(intent)` creates a new run and initial typed context;
- `resume(context)` accepts only a valid context for the same run and continues
  the state machine from its persisted state;
- the runner updates the handle before each phase and after each transition;
- a typed context must not fall back to `run(intent)`;
- a legacy generic context may be rejected with a migration diagnostic, but it
  must not be silently restarted when a durable checkpoint claims resumability;
- cancellation while a pause is requested leaves the context attached and
  recoverable; cancellation without a pause request is classified separately;
- a resumed runner may be paused again, including while its handle lifecycle is
  `resuming` or `running` after the claim is established; and
- a terminal runner writes a terminal checkpoint exactly once and removes the
  active in-memory handle only after persistence succeeds.

The generic `WorkflowRunner` must persist enough graph position to avoid
replaying completed phases, including branch/rejection/parallel-phase
decisions and iteration counts. A phase output alone is not sufficient when
the graph can branch or loop.

### 5.8 Tool calls, retries, and side effects

Resume must integrate PRD-169 at the workflow boundary:

- validate the session conversation before the resumed provider request;
- identify the interrupted turn and its run/phase;
- preserve valid completed tool results and durable idempotency records;
- synthesize or reject unresolved tool calls according to the canonical
  transaction policy;
- do not rerun a completed side-effecting tool merely because a phase was
  interrupted;
- persist the repair before sending another provider request; and
- show only a safe diagnostic containing counts/IDs or hashes, not prompts,
  file contents, credentials, or full tool arguments.

If a workflow phase performed an external side effect but no idempotency
receipt exists, resume must report the ambiguity and require a user decision,
workflow reset, or an explicit retry policy. It must not claim that the side
effect is known to be absent.

### 5.9 Modes, approvals, and phase capabilities

Resume restores the workflow's declared phase and model configuration but must
re-evaluate the current session's Safe/Plan/Yolo policy. A checkpoint cannot
grant capabilities that the current mode blocks. Approval requests must be
fresh UI requests; approval objects and pending overlays are not serialized.
If the checkpoint's required mode/profile/capabilities are incompatible, the
run is offered a fail-closed repair/reset path rather than resumed under a
broader policy.

### 5.10 Multiple runs and stale state

The coordinator must list all recoverable runs for the session in deterministic
order, preferably newest checkpoint revision/time first, and support explicit
run selection. It must:

- ignore terminal checkpoints for the default selector;
- retain terminal records for inspection and reset history;
- detect duplicate or conflicting active checkpoints for one run;
- detect a checkpoint whose plugin is removed or whose fingerprint changed;
- not let a workflow reload silently replace the plugin while a run is active;
- preserve a stale checkpoint for diagnosis; and
- offer `/workflow reset <run-id>` or an equivalent explicit cleanup path.

### 5.11 `create_workflow` and downstream custom workflows

`create_workflow` must be enhanced as an authoring contract, not merely patched
as a special case. Its design prompts, inspection tools, example templates,
and validation must tell the author to:

- use `PhaseSpec` and the inherited generic runner when possible;
- use a typed outer-loop state machine and one method per phase when custom
  behaviour is required;
- attach the session-provided `shared_memory`/`session_memory` on every run and
  restore path;
- persist all state needed to choose the next phase, including branch,
  iteration, artifacts, approvals, and external receipts;
- implement `resume(context)` by re-entering the same dispatch loop at the
  saved state, never by calling `run(context.intent)` for a checkpointed run;
- implement bounded JSON checkpoint codecs for custom contexts, excluding live
  resources; and
- add a system-prompt instruction that every phase may ask the user focused
  questions with `ask_user` before making an irreversible or materially
  ambiguous decision.

Validation must reject generated custom workflows that advertise resumability
without both codec hooks, mutate or replace session memory, omit a valid
resume dispatch path, or contain an unconditional `resume -> run` restart.
Generated declarative workflows must be covered by the framework's generic
checkpoint tests. A manually installed legacy workflow may remain runnable,
but it must be marked non-resumable and `/workflow resume` must fail closed
with an explanation.

### 5.12 Headless and background compatibility

The first user-facing implementation is the TUI command, but the durable
record must be usable by the existing headless and background session owners.
The PRD does not require a new command syntax for every client. It does
require:

- no duplicate execution if a background owner and the TUI observe the same
  run;
- a shared claim/lease and lifecycle record;
- headless resume to use the same checkpoint/context/memory contract when it is
  exposed; and
- client-neutral session events for `workflow_recovery_available`,
  `workflow_resume_started`, `workflow_resume_paused`, `workflow_resume_failed`,
  and `workflow_resume_completed`.

## 6. Error and security contract

Errors must be typed or mapped to stable codes, including:

- `no_recoverable_workflow`;
- `run_not_found`;
- `run_already_claimed`;
- `checkpoint_corrupt`;
- `checkpoint_schema_unsupported`;
- `conversation_cursor_mismatch`;
- `conversation_tool_tail_invalid`;
- `plugin_not_loaded`;
- `plugin_fingerprint_mismatch`;
- `custom_context_codec_missing`;
- `provider_profile_mismatch`;
- `workspace_mismatch`; and
- `resume_transition_failed`.

The user message should say what can be done next (`/workflow reset`, reload
the workflow, restore the provider profile, or inspect the session) without
printing secret or unbounded state. Checkpoints, recovery indexes, and
diagnostics must not store API keys, authorization headers, prompts, file
contents, tool arguments, browser cookies, or raw provider payloads.

Run and session identifiers must be validated before path construction. Atomic
writes, file permissions, content hashes, bounded payloads, and crash-safe
journal semantics remain mandatory.

## 7. Implementation map

The exact module split may follow current ownership, but the implementation
must address these boundaries:

| Area | Required change |
|---|---|
| `tui/runners/tui_session.py` | Replace paused-handle-only lookup with recovery coordinator use; expose selector/errors; claim runs; preserve queued input and notifications; support re-pausing a resumed run. |
| `runners/workflow_checkpoint_store.py` | Add deterministic recovery listing, status transitions/claims, conflict detection, and safe cleanup without weakening atomic persistence. |
| `workflows/checkpoint.py` | Version any new recovery/turn metadata and validate consistency without serializing live resources. |
| `runners/workflow_handle.py` | Serialize/rehydrate the full lifecycle needed for recovery, expose a safe claim/pause transition, and retain the session conversation binding. |
| `runners/session_conversation.py` / journal | Reconcile conversation cursor, interrupted turn, tool-tail repair, and one stable `conversation_id`. |
| `workflows/base_runner.py`, `workflows/default/runner.py` | Define and enforce the no-silent-restart resume contract; checkpoint graph position at durable boundaries. |
| `workflows/code_plan/runner.py` | Resume exact `CodePlanState`, phase iteration, approvals, command outcomes, and repeated pause/resume cycles. |
| `workflows/create_workflow/runner.py` and `validation.py` | Make generated/custom workflow resume contracts explicit, validated, and prompt-guided. |
| `commands/builtins.py`, `tui/triggers/slash_command.py` | Advertise and complete `resume [run-id]` and reset/run selection syntax. |
| session service projection | Publish recovery and resume lifecycle events without duplicating ownership. |
| docs | Update `docs/guides/workflows.md`, `docs/reference/cli.md`, storage docs, and generated API/reference inventories after implementation. |

## 8. Acceptance criteria

### A-1 — No recoverable run

In a clean session, `/workflow resume` returns a concise
`no_recoverable_workflow` message, does not call the LLM, does not create a
checkpoint, and does not alter the active workflow selection.

### A-2 — Same-process Esc pause and resume

Start `code_plan`, interrupt it with Esc during each of PLAN, EXECUTE, REVIEW,
and SUMMARIZE, then run `/workflow resume`. The run ID, original intent, typed
state, current phase, session `conversation_id`, prior phase artifacts, and
completed tool results are preserved. The next phase starts exactly once.

### A-3 — Repeated pause/resume

Resume a paused run, interrupt it again, and resume it a second time. The second
pause is durable, no cancellation is misclassified as failure, and no phase or
side-effecting tool is duplicated.

### A-4 — Process restart

Terminate the process during an active workflow phase without running normal
pause cleanup. Start `agenthicc --resume <session-id>`. The TUI reports one
recoverable workflow, attaches its handle, and `/workflow resume` continues
from the last checkpointed state. It does not start a new run or repeat a
completed phase.

### A-5 — Exact run selection

Create two recoverable runs in one session fixture. `/workflow resume` does not
choose arbitrarily; it shows the selector/error. `/workflow resume <run-id>`
resumes exactly the requested run and rejects another session's ID.

### A-6 — Durable cursor and memory

After direct turns, workflow phases, a mode switch, and a process restart, the
resumed provider request receives one session memory containing the prior
history. The TUI projection may be bounded for display, but it is not used as
the provider-memory source.

### A-7 — Tool transaction recovery

Interrupt a parallel tool batch after one tool completes. Resume the workflow.
The provider-valid conversation contains one result for every call; the
completed tool is not rerun; unresolved calls are repaired or rejected with a
safe diagnostic; and the journal remains foldable after a second restart.

### A-8 — Crash-safe phase boundaries

For every built-in and generic phase, kill the process before the first agent
turn, during an agent turn, immediately after a transition tool, and after a
phase artifact is produced. Resume either continues the saved phase or fails
closed with a specific consistency error. It never silently restarts from the
first phase.

### A-9 — Plugin compatibility

Resume succeeds when the exact workflow fingerprint is loaded and fails closed
with `plugin_fingerprint_mismatch` when the topology changes. A missing plugin,
missing custom codec, unsupported schema, corrupt hash, provider-profile
mismatch, or cursor mismatch yields an actionable diagnostic and preserves the
checkpoint for inspection/reset.

### A-10 — `create_workflow` output

Generate a declarative and a custom workflow. Both pass authoring validation,
share the session memory, expose a true checkpoint-aware resume path, include
the user-question system-prompt instruction, and survive a process-style
checkpoint/rehydration test. A generated custom workflow with a restart-style
`resume()` is rejected.

### A-11 — Command discovery and busy state

`/help`, the slash picker, and argument completion show `resume [run-id]` and
reset. Resume while another run is active is rejected or safely queued by a
documented policy; two simultaneous resume requests cannot claim one run.

### A-12 — Terminal and reset semantics

Complete, exited, failed, and discarded checkpoints are not resumed as active
runs. `/workflow reset` writes an auditable discarded record for a recoverable
run, clears the in-memory handle only after persistence, and cannot delete an
unrelated session's checkpoint.

### A-13 — UI and client-neutral events

The TUI shows recovery availability, selected run, phase, pause, failure, and
completion without spinner/redraw duplication. Session-service consumers see
the documented lifecycle events and never receive prompts, secrets, or raw
tool payloads.

## 9. Test plan

### Unit tests

- command grammar, picker completion, run-ID validation, and busy policy;
- recovery status classification and deterministic ordering;
- atomic claim/release and duplicate-claim rejection;
- checkpoint schema, content hash, bounded size, safe paths, and consistency
  checks;
- cursor and `conversation_id` validation;
- lifecycle transitions including running → interrupted → resuming → paused,
  repeated pause, terminal, and reset;
- generic graph resume with branch, rejection, loop, and parallel phase state;
- `CodePlanContext` and `CreateWorkflowContext` exact state restoration;
- custom codec success, missing codec, malformed payload, and live-resource
  exclusion;
- interrupted tool-tail repair and idempotency-ledger reuse;
- profile, mode, capability, workspace, and plugin fingerprint rejection;
- no-LLM/no-new-run guarantees for error paths.

### Integration tests

- TUI Esc → checkpoint → `/workflow resume` using a real temporary session
  directory and `SessionConversation`;
- process-style restart using the same session ID, journal, kernel event log,
  and checkpoint store;
- direct turns before workflow, workflow phase turns, and post-resume turns
  sharing one memory object and conversation ID;
- generic runner, `code_plan`, `create_workflow`, and a generated custom runner;
- PRD-169 tool transaction repair across cancellation and resume;
- workflow reload/fingerprint changes, provider-profile changes, and corrupt
  checkpoint handling;
- session-service lifecycle projection and no-secret payload assertions;
- foreground/background ownership and duplicate resume claims.

### End-to-end tests

- launch a TTY session, start a Plan workflow, Esc during an LLM turn, exit,
  relaunch with `--resume`, select `/workflow resume`, and observe completion;
- repeat the journey with `create_workflow` while validating a generated file;
- resume a custom generated workflow after a forced process termination;
- run two recoverable workflows and select one by ID;
- verify `/workflow reset` and a fresh workflow after discard;
- verify malformed/incompatible state produces a safe recovery screen rather
  than an implicit fresh run.

Tests must use temporary directories, deterministic fake transports/tools,
controlled cancellation barriers, and fixed clocks where timestamps affect
ordering. No test may require a live provider, browser runtime, network
endpoint, API key, or user home directory.

## 10. Rollout and compatibility

The checkpoint schema must be versioned with an explicit migration or a
fail-closed unsupported-schema error. Existing terminal checkpoints remain
inspectable. Existing clean sessions and direct-turn `--resume` behaviour must
remain unchanged.

Legacy custom workflows that lack a codec are not silently upgraded: they keep
their existing execution path, but an interrupted run is reported as
non-resumable and requires reset or a workflow update. Once a new checkpoint
has been written, the owning runner must obey the new resume contract.

The implementation should first ship the recovery coordinator and tests behind
the existing TUI command, then update picker/docs and generated workflow
validation in the same release. No provider-specific configuration or secret
format is required.

## 11. Definition of done

- All requirements in Sections 5–8 are implemented in the current ownership
  boundaries.
- Every acceptance criterion has a deterministic test with evidence of the
  exact run ID, phase, conversation ID, and lifecycle result.
- Unit, integration, and E2E resume suites pass; relevant Ruff, mypy, type-audit,
  and documentation checks pass.
- `docs/guides/workflows.md`, `docs/reference/cli.md`, and storage/reference
  documentation describe the implemented command and recovery states.
- The PRD's implementation record links the changed modules and verification
  commands after delivery.

## 12. Implementation record

Implemented in the current runtime boundaries:

- `runners/workflow_checkpoint_store.py` — atomic owner claims, stale-claim
  recovery, fail-closed malformed-claim handling, and safe release.
- `runners/workflow_recovery.py` — session-scoped recovery inspection,
  compatibility checks, tool-tail repair, typed rehydration, and stable
  diagnostics.
- `runners/workflow_handle.py` — phase-entry/transition persistence, lifecycle
  recovery, workspace identity, pause during `resuming`, and claim ownership.
- `runners/tui_session.py` and `runners/headless.py` — durable discovery,
  explicit selection, claim lifecycle, resume/reset events, and owner cleanup.
- `workflows/default/runner.py`, `code_plan`, `create_workflow`, and the
  state-machine built-ins — exact graph/typed-state persistence at phase and
  transition boundaries; legacy restart-style resume is rejected.
- `workflows/create_workflow/validation.py` and authoring inspection/prompt
  surfaces — codec, shared-memory, question-tool, cache-contract, and
  `resume → run` restart validation.
- `commands/builtins.py` and `tui/triggers/slash_command.py` — discoverable
  `resume [run-id]` and `reset [run-id]` command paths.
- `docs/guides/workflows.md`, `docs/reference/cli.md`,
  `docs/reference/storage.md`, `llms.txt`, and `llms-full.txt` — recovery data
  flow, lifecycle, security, and extension guidance.
- `background/worker.py` and `docs/reference/type-safety-baseline.json` — keep
  optional background-session adapters compatible and ratchet the repository's
  existing typing-hygiene baseline.

Final verification:

```text
uv run pytest tests/ -q                         # 3217 passed, 15 skipped
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
focused changed-file mypy                         # clean
direct llms public-symbol check                   # clean
```

The full-repository mypy command remains blocked by the pre-existing typing
backlog in unrelated workflow/tool modules (152 diagnostics); the changed
recovery core and its adapters pass focused mypy. The Nox `llms_check` wrapper
could not run because the shared `.venv` contains root-owned package metadata
that prevents its internal `uv sync`; the same check passed directly with
`PYTHONPATH=src`.
