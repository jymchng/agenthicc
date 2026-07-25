---
title: "PRD-148: Unified Interrupt and Graceful Cancellation"
status: Proposed
version: 0.1.0
created: 2026-07-25
study_date: 2026-07-25
related_prds:
  - PRD-10
  - PRD-74
  - PRD-129
  - PRD-139
  - PRD-141
  - PRD-143
tags:
  - interrupt
  - cancellation
  - tui
  - workflows
  - lauren-ai
  - durability
  - background
---

# PRD-148 — Unified Interrupt and Graceful Cancellation

Study date: 2026-07-25. This PRD defines a first-class interrupt contract for
foreground TUI turns, workflows, headless execution, and background workers.
It is based on the current source tree and the installed lauren-ai runner
contract, not the historical prompt-toolkit implementation described by PRD-10.

## 1. Executive summary

agenthicc already exposes several ways to stop active work:

- Ctrl+C and Esc are handled by the streaming input capability;
- /cancel and its /interrupt alias are immediate control commands; and
- TUISession stores the active asyncio.Task and calls task.cancel().

The current behavior is a useful shortcut, but it is not yet a complete
interrupt feature. The cancellation request has no shared lifecycle object or
stable reason, the kernel's existing IntentCancelled reducer is not emitted by
the TUI path, cancelled conversation turns are closed like successful turns,
and the durable conversation journal records a cancelled direct turn as
turn_completed. General workflow runners similarly expose cancellation as a
generic failed run. A task cancellation can also leave a synchronous tool,
subprocess, retry sleep, approval wait, or provider request without a clear
cleanup contract.

PRD-148 introduces one local-first interrupt protocol. Every interrupt source
creates an idempotent request, publishes cancelling, propagates cancellation
through the active lauren-ai run and its workflow/tool boundaries, waits for
bounded cleanup, records a terminal cancelled outcome, and releases queued work
exactly once. Partial output and completed side effects remain durable; the
system never promises to roll back arbitrary filesystem, network, or external
service mutations.

The feature keeps Ctrl+C, Esc, /cancel, and /interrupt as compatible entry
points, but moves their behavior behind one cancellation owner. It also aligns
foreground and background cancellation terminology and preserves an explicit
path to inspect or resume work without silently replaying a user-cancelled turn.

## 2. Evidence-backed current-state study

### 2.1 Existing entry points and ownership

| Concern | Current implementation | Evidence | Gap |
|---|---|---|---|
| Keyboard interrupt | InterruptCapability handles Key.CTRL_C and Key.ESC in STREAMING, clears the composer, and dispatches InterruptAgentCommand | src/agenthicc/tui/input/capabilities.py | No reason, request ID, acknowledgement, or timeout state |
| Slash control | /cancel has /interrupt as an alias and calls CommandContext.cancel_active | src/agenthicc/commands/builtins.py and command.py | Boolean task-cancel callback cannot report a structured transition |
| Active task owner | _cancel_active_task() calls self._agent_task.cancel() | src/agenthicc/runners/tui_session.py | No protocol for child tasks, tools, retries, approvals, or process groups |
| Direct turn cleanup | agent_task_body() catches CancelledError, calls close_turn(), then advances the FIFO queue | src/agenthicc/runners/tui_session.py | Cancellation is rendered as normal completion and queue release is not acknowledged |
| Conversation state | ConversationStore.close_turn() marks a no-error turn COMPLETE and emits turn_complete | src/agenthicc/tui/conversation_store.py | No distinct interrupted turn state or renderer event |
| Kernel state | IntentStatus and NodeStatus have failed but no cancelled; _intent_cancelled() marks all active intents failed | src/agenthicc/kernel/state.py and reducer.py | Cancellation is not targeted and is indistinguishable from failure |
| Kernel event | An IntentCancelled handler already exists | src/agenthicc/kernel/reducer.py | Foreground interruption does not emit it with an intent ID and reason |
| Direct journal | AgentTurnRunner writes turn_completed in finally, including after cancellation | src/agenthicc/runners/agent_turn.py | Clean user cancellation is indistinguishable from completion to resume detection |
| Resume detection | fold_resume_state() resumes a turn_started lacking a later turn_completed | src/agenthicc/memory/journal.py and run_coordinator.py | No “cancelled and intentionally not auto-resumable” marker |
| Workflow cancellation | Default and code-plan runners set status to failed and re-raise CancelledError | src/agenthicc/workflows/default/runner.py and code_plan/runner.py | User action becomes a failure |
| Background cancellation | Background sessions already model cancelling and cancelled, with a cancellation reason | src/agenthicc/background/model.py and worker.py | Foreground and background use different control implementations |
| Agent runtime | Installed lauren-ai exposes AgentRunner.run_stream() and async transport/tool execution, but no public interrupt() method | .venv/lib/python3.13/site-packages/lauren_ai/_agents/_runner.py | agenthicc needs a cancellation adapter without a second agent loop |

### 2.2 Implementation constraints

1. TUISession is the interactive orchestration owner. Do not create a second
   TUI task owner or input loop.
2. The kernel remains immutable and event-driven. Interrupt state must use
   events and reducer transitions, never direct mutation of frozen records.
3. lauren-ai remains the canonical agent runner, transport, tool executor,
   signal bus, and idempotency boundary. agenthicc may add an adapter or
   optional cancellation hook at that boundary, but must not fork the loop.
4. Python task cancellation is cooperative. It does not stop arbitrary code
   already running in asyncio.to_thread() or a synchronous plugin.
5. External side effects are not generally reversible. Cleanup must prevent new
   work and preserve idempotency evidence; it must not claim to undo a write,
   Git operation, HTTP request, MCP call, or email already accepted elsewhere.
6. Existing background status transitions are the reference vocabulary:
   cancelling is in progress and cancelled is terminal.

## 3. Goals and non-goals

### Goals

- Make Ctrl+C, Esc, /cancel, and /interrupt reliable aliases for one interrupt
  operation.
- Stop direct turns and built-in workflows without starting another phase or
  retry after the request is accepted.
- Return the foreground TUI to IDLE with an actionable status and usable
  composer after cleanup.
- Preserve partial output, token usage, tool records, phase history, and other
  durable evidence of work already performed.
- Distinguish cancellation from successful completion and provider failure.
- Make repeated interrupt requests safe and idempotent.
- Keep queued messages FIFO and release them only after cancellation is
  terminally acknowledged.
- Cancel pending approval and ask_user() waits without allowing a new tool call.
- Give owned subprocesses a bounded termination path and report
  non-cooperative synchronous work honestly.
- Align foreground, headless, and background semantics enough to inspect and
  explicitly resume work without silently replaying a cancelled turn.

### Non-goals

- Rolling back arbitrary filesystem, Git, network, MCP, Outlook, email, or
  external API side effects that were already accepted.
- Replacing lauren-ai's agentic loop, transports, tool executor, or signal bus.
- Killing the entire agenthicc process as the normal response to one interrupt.
- Automatically retrying or automatically resuming a user-cancelled turn.
- Adding a general queue editor or /queue clear; queued messages remain FIFO.
- Defining provider-specific remote cancellation APIs when a provider has none.
- Changing idle-mode exit behavior: with no active run, existing double Ctrl+C
  and Ctrl+D behavior remains intact.

## 4. User-facing contract

### 4.1 Active foreground run

While a direct turn, workflow phase, retry, tool call, approval wait, or
ask_user() interaction is active:

1. Ctrl+C requests interruption immediately.
2. Esc remains a streaming keyboard alias when no overlay owns the key. An
   active overlay continues to receive Esc and is not interrupted.
3. /cancel and /interrupt execute in the immediate-control lane and use the
   same owner and transition as the keyboard path.
4. The first request publishes “Interrupt requested — stopping the active
   run…” and enters cancelling.
5. Repeated requests while cancelling are coalesced; they do not duplicate
   cleanup, terminal events, or queue advancement.
6. The composer is empty and editable after acknowledgement. Input during
   cleanup follows the existing busy policy and is not lost.

The final display uses a distinct cancellation result, for example:

    ⏹ Interrupted after 18 seconds (phase: execute; tool: Run)
       Partial output and completed tool actions were preserved.
       2 queued message(s) remain in FIFO order.

The exact copy may change, but a cancelled turn must not render only the
successful-turn “Worked for …” message.

### 4.2 No active run

- /cancel and /interrupt report “No active run to interrupt.” locally.
- Ctrl+C retains the idle double-press exit sequence.
- Esc retains normal idle input behavior.

### 4.3 Queue, approval, and question behavior

Interrupting the active run does not silently discard queued messages. The
queue remains ordered and drains once after the cancellation event is durable
and the input session returns to IDLE. A queued slash command is reclassified
against the current registry before execution.

An interrupt while waiting for approval or user input closes the pending prompt
through the existing owner, resolves the wait as cancelled rather than
approved, prevents the tool or continuation from starting, and records the
pending operation.

### 4.4 Background and headless behavior

- agenthicc jobs cancel, background-manager c, and foreground /bg and
  /background use the same reason vocabulary and terminal cancelled semantics.
- A background worker displays cancelling until cleanup acknowledgement, then
  cancelled; it never reports completed merely because a coroutine was cancelled.
- Headless SIGINT maps to the same cancellation result and exit contract where
  the process can handle it. JSON output contains a redacted cancellation
  record rather than an unstructured traceback.

## 5. Lifecycle and state contract

### 5.1 Typed interrupt state

Introduce a session-owned InterruptRequest/InterruptState record:

| Field | Meaning |
|---|---|
| request_id | Stable ID for one interrupt request |
| source | keyboard, slash_command, background, headless_signal, shutdown, or timeout |
| reason | Stable reason, defaulting to user_requested for keyboard/command input |
| intent_id / turn_id | Exact active operation targeted |
| phase | requested, cancelling, cancelled, timed_out, or failed |
| requested_at / completed_at | Monotonic and wall-clock telemetry |
| active_tool / active_phase | Best-known operation at request time |
| queued_count | FIFO depth at acknowledgement |
| cleanup_error | Redacted structured error if cleanup is incomplete |

cancelled is the persisted terminal outcome. interrupt describes the action,
not a second status. Timeouts and shutdown may share mechanics but retain
distinct source/reason values.

### 5.2 Domain status and events

Add cancellation deliberately rather than overloading failed:

- add cancelled to IntentStatus and NodeStatus where an active intent/workflow
  node can be terminated by a control request;
- support cancelled in WorkflowRun.status;
- add an INTERRUPTED/CANCELLED conversation turn state, or an equivalent
  explicit event-backed projection; and
- preserve old failed and turn_completed records when folding old data.

The reducer targets the exact event ID. It must never mark every active intent
cancelled because one foreground task was interrupted.

Proposed versionable events:

    InterruptRequested
      request_id, intent_id, turn_id, source, reason,
      active_phase, active_tool, queued_count, requested_at

    InterruptCompleted
      request_id, intent_id, turn_id, outcome, source, reason,
      active_phase, active_tool, cleanup_error, completed_at, queued_count

    IntentCancelled
      intent_id, request_id, source, reason

    WorkflowRunCompleted
      ... existing fields ..., status="cancelled", request_id, reason

The existing IntentCancelled compatibility event must carry an exact target
and be emitted by the new owner. Duplicate events are harmless.

## 6. Technical design

### 6.1 One cancellation owner

Add a small component in the runner boundary, tentatively
src/agenthicc/runners/interrupt.py. It should:

1. expose request(source, reason) and return a structured result;
2. coalesce duplicate requests;
3. publish InterruptRequested before cancelling the active task;
4. set a cooperative cancellation event/token for child operations;
5. cancel and await the active asyncio.Task through one cleanup path;
6. cancel approval, question, retry, and child-task waits;
7. enforce the configured grace period;
8. publish InterruptCompleted and release the queue exactly once; and
9. leave the owner clean even when a cleanup callback raises.

TUISession._cancel_active_task() becomes a compatibility adapter.
CommandContext.cancel_active and InterruptAgentCommand must not contain a
second implementation.

### 6.2 Input and lauren-ai integration

InterruptCapability dispatches InterruptAgentCommand carrying source keyboard
or escape; it does not own run state. /cancel and /interrupt use source
slash_command. The existing IMMEDIATE_CONTROL busy policy remains.

AgentTurnRunner remains the adapter around lauren-ai's
AgentRunner.run_stream():

- pass cancellation context through existing agenthicc turn/workflow context;
- stop consuming the async stream when cancellation is requested;
- let CancelledError propagate through run_stream() and workflow runners;
- never schedule transport retries after the cancellation flag is set;
- capability-detect a future lauren-ai request-cancellation handle if one is
  available; and
- otherwise use the current local async task boundary and report limitations.

The PRD does not fork the lauren-ai observe-think-act loop or require
provider-specific code in agenthicc.

### 6.3 Tools, subprocesses, workflows, and side effects

The shared cancellation context reaches the existing lauren-ai/agenthicc tool
boundary:

- owned tool tasks are cancelled and awaited;
- approval and ask_user() futures resolve as cancelled;
- wait_for() and retry sleeps stop and do not restart;
- asyncio.to_thread() work is marked non-cooperative when the underlying
  synchronous function cannot stop; no next phase starts while it is owned;
- agenthicc-owned subprocess tools send SIGTERM to the exact process group,
  wait the grace period, and use a narrowly scoped SIGKILL fallback;
- HTTP/MCP request scopes close where the existing client supports it; and
- the idempotency ledger records completed side effects before cancellation.

Default, code-plan, authoring, parallel, and custom workflow runners must:

- stop the current phase and parallel children;
- prevent transition to a next phase;
- mark the workflow cancelled, not failed;
- preserve completed phase history and output;
- cancel review/plan/execute continuation loops; and
- avoid retry logic for CancelledError.

Custom plugins that ignore cooperative cancellation remain cancellable at the
outer task boundary, but their cleanup limitation must be visible.

### 6.4 Durable journal and resume

Extend ConversationJournal with a terminal turn_cancelled marker containing
request ID, source, reason, and active operation. fold_resume_state() treats
turn_completed and turn_cancelled as terminal markers:

- clean user cancellation is not automatically resumed;
- a crash or hard process death remains eligible for the existing PRD-129
  explicit resume path;
- explicit resume/retry reuses durable tool evidence and is a visible new
  attempt; and
- journal writes remain append-only and fsync-protected.

### 6.5 Queue race prevention

The active task, cancellation owner, and queue need one completion gate:

- a request racing with normal completion observes one terminal outcome;
- advance() runs once per terminal task;
- queued work cannot start before cancellation acknowledgement;
- queued commands are reclassified against current policy/registry; and
- a stale request cannot cancel a newly started queued task.

An owner identity or run generation may guard these races; authoritative state
remains the session owner and kernel events.

### 6.6 Configuration

Add conservative settings to the existing validated configuration:

    [execution]
    interrupt_grace_s = 5.0
    interrupt_force_cleanup = true

    [background]
    cancel_grace_s = 5.0

Exact names/defaults are finalized in Phase 0 so foreground and background do
not maintain conflicting policies. Invalid or negative values fail closed.
Force cleanup applies only to owned tasks/processes and never claims to undo
external side effects.

## 7. Acceptance criteria

### User-visible and lifecycle behavior

1. Ctrl+C, streaming Esc, /cancel, and /interrupt share one tested owner.
2. An active direct turn returns to an editable idle composer after cleanup.
3. Esc in an active overlay does not interrupt the run.
4. Repeated requests are idempotent and do not duplicate cleanup, events, or
   queue advancement.
5. Idle double-Ctrl+C and Ctrl+D behavior remains unchanged.
6. Partial output, tokens, completed tools, and completed phase history remain
   visible and durable.
7. The final display identifies cancellation and does not present success only.
8. Queued messages remain FIFO and do not start before acknowledgement.
9. Approval/question interruption cancels the wait and prevents the operation.
10. Foreground/background show cancelling → cancelled with reason/source.

### Runtime and persistence correctness

11. Only the exact active intent/turn is targeted; unrelated state stays.
12. Typed cancellation events reduce correctly and duplicate events are harmless.
13. Direct/workflow cancellation is distinct from failed.
14. No new phase, parallel branch, retry, continuation, or subagent starts after
    cancellation is accepted.
15. CancelledError is never converted into a transport retry.
16. A journal cancellation marker prevents automatic resume; a crashed
    incomplete turn remains eligible for existing explicit resume.
17. Child tasks, approvals, owned subprocesses, and cleanup are bounded or
    explicitly reported as non-cooperative.
18. Notifications and JSON output contain no prompt, tool argument, token, or
    unredacted provider error.
19. Headless SIGINT yields a structured cancelled result and documented exit.
20. A custom workflow that ignores cooperative cancellation cannot report
    completed without surfacing its cleanup limitation.

### Required verification

21. Unit tests cover the state machine, source normalization, idempotency,
    duplicate events, no-active-run, and queue release.
22. Reducer/processor tests cover targeted cancellation from every active state.
23. TUI tests cover synthetic keyboard input, active overlays, commands,
    approvals/questions, and idle double-Ctrl+C.
24. lauren-ai cassette tests prove stream cancellation preserves partial output
    and does not retry.
25. Tool tests cover async, retry sleep, to_thread(), subprocess, MCP/HTTP,
    and completed-side-effect cases.
26. Workflow tests cover default, code-plan, authoring, parallel, review,
    approval, and custom-plugin paths.
27. Journal restart tests distinguish clean cancellation from crash interruption.
28. Background tests prove cancelling → cancelled, reason persistence, lease
    cleanup, and duplicate-cancel safety.
29. Headless signal tests cover JSON output, exit status, and terminal cleanup.
30. Relevant unit/integration/E2E, lint, format, typing, type-audit, and
    documentation checks pass under repository commands.

## 8. Rollout plan

### Phase 0 — Contract and instrumentation

Confirm event/status names, old-journal compatibility, grace-period policy, and
redacted metrics. Add state-machine, reducer, and journal tests first.

### Phase 1 — Foreground direct turns

Add the runner-owned controller, route keyboard/slash controls through it, add
distinct conversation rendering, and prove provider, retry, and queue races.

### Phase 2 — Workflow, approval, and tool propagation

Thread the context through default, code-plan, authoring, and parallel runners.
Update approvals, questions, subagents, retries, subprocesses, and tool
contracts for cooperative versus non-cooperative cleanup.

### Phase 3 — Durability and explicit recovery

Add journal cancellation markers, targeted kernel events, state compatibility,
and crash/restart/partial-side-effect tests. Ensure deliberate cancellation is
not auto-resumed.

### Phase 4 — Headless and background convergence

Map headless SIGINT to the structured result, align background reasons and
acknowledgement, and verify /bg and /background cannot race with interrupt.

### Phase 5 — Hardening and documentation

Exercise provider cassettes, Windows/POSIX terminals, MCP, HTTP, subprocess,
sync-plugin, non-TTY, and cleanup-failure paths. Update README, TUI,
background-session, storage, architecture, and contributor documentation.

## 9. Security, safety, and failure handling

- Cancellation is not an authorization bypass. Existing mode, capability,
  approval, workspace, network, and plugin trust checks remain active.
- Never kill a broad process tree. Terminate only an exact process group created
  and owned by the specific tool invocation.
- Do not delete session artifacts during cleanup; durability is needed for
  inspection and explicit recovery.
- A failed cleanup acknowledgement is an operational failure, not successful
  cancellation. Show redacted remaining owner/process information.
- A write interrupted after acceptance may be partial or complete. State what is
  known and never claim atomicity without a tool contract.
- Cancellation racing with approval defaults to deny/cancel, never allow.
- No event or metric includes raw prompts, assistant output, tool arguments,
  API keys, OAuth tokens, or session transcripts.

## 10. Open decisions

| ID | Question | Recommendation |
|---|---|---|
| OQ-1 | Should Esc remain a streaming alias? | Yes, only when no overlay owns it, with visible documentation. |
| OQ-2 | Should repeated Ctrl+C force-kill? | No implicit force action; use bounded owned-process cleanup. |
| OQ-3 | Add cancelled to kernel enums now or metadata first? | Add the typed terminal state so failure and cancellation remain queryable. |
| OQ-4 | Auto-drain queued input after cancellation? | Yes, preserve current FIFO behavior; queue discard is separate scope. |
| OQ-5 | Should lauren-ai gain a public cancellation handle? | Prefer an optional upstream hook, but Phase 1 works with local task cancellation. |
| OQ-6 | What is the sync-plugin policy? | Keep the session recoverable and truthful; never claim to_thread() stopped. |

## 11. Measurement plan

Measure locally:

- requests reaching terminal acknowledgement within the grace period;
- cleanup duration by active operation category;
- duplicate-request and duplicate-queue-release counts;
- cancellations misclassified as failed or complete;
- cancelled turns incorrectly detected for resume;
- owned subprocesses alive after cleanup; and
- foreground/background/headless parity for status and reason.

## 12. Definition of done

This PRD is complete when:

1. The four foreground entry points share one tested interrupt owner.
2. Direct turns, workflows, approvals, tools, retries, and owned subprocesses
   have documented behavior and regression coverage.
3. Kernel, conversation, workflow, and journal state distinguish cancellation
   from success, failure, and crash interruption.
4. Queue release, partial output, idempotency, and explicit recovery are correct
   under cancellation races.
5. Headless and background lifecycle behavior is convergent and redacted.
6. Relevant repository validation commands pass and the PRD index links this
   document with its implementation status.

## 13. Verification command set

At implementation time, run focused tests first, then repository gates:

    uv run pytest tests/unit/test_tui_coverage_edges.py \
      tests/unit/test_tui_session_coverage.py \
      tests/unit/test_busy_commands_tui.py \
      tests/unit/test_run_resume.py -q
    uv run pytest tests/unit tests/integration tests/e2e -q
    uv run ruff check src/ tests/ scripts/
    uv run ruff format --check src/ tests/ scripts/
    uv run mypy src/agenthicc
    uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
    uv run nox -s llms_check

## 14. Related documents

- [PRD-10 — Enhanced Input Bar](prd-10-input-bar.md) — historical interrupt
  intent; its prompt-toolkit architecture is not authoritative.
- [PRD-129 — Conversation Durability and Retry Resilience](prd-129-conversation-durability-and-retry-resilience.md)
- [PRD-141 — Background Sessions and Session Manager TUI](prd-141-background-sessions-and-session-manager-tui.md)
- [PRD-143 — Safe Commands During Active LPM Runs](prd-143-safe-commands-during-active-runs.md)
- [TUI guide](../docs/guides/tui.md)
- [Background sessions guide](../docs/guides/background-sessions.md)
