---
title: "PRD-141: Background Sessions and Session Manager TUI"
status: Implemented
version: 1.0.0
created: 2026-07-23
related_prds:
  - PRD-138  # Repository Improvement Roadmap
  - PRD-139  # OpenCode-Inspired Product Expansion and Privacy-First Advertisements
  - PRD-81   # Workflow Revamp
  - PRD-124  # Concurrent Subagents
  - PRD-126  # Transport Retry with Memory Rollback
  - PRD-129  # Conversation Durability & Retry Resilience
supersedes: []
tags:
  - sessions
  - background
  - tui
  - workflows
  - persistence
  - resume
---

# PRD-141 — Background Sessions and Session Manager TUI

Study date: 2026-07-23. This PRD defines a local-first way to detach agenthicc
work from the foreground terminal and a dedicated TUI for finding, observing,
controlling, and resuming those sessions. It is a product and architecture
contract; implementation details may be refined during the design phase as
long as the ownership and safety requirements remain intact.

## 1. Executive summary

agenthicc can already run workflows, construct interactive sessions, execute a
headless stdin/JSON-lines runner, persist conversation and kernel state, and
coordinate concurrent subagents. These capabilities are not yet presented as
one durable user-facing lifecycle. A user who starts substantial work must
keep the foreground session attached, and there is no manager view that answers
which sessions are running, what they are waiting for, or how to resume one
after a terminal or process interruption.

PRD-141 adds first-class background sessions. A background session is a local
job with a stable identifier, durable lifecycle state, a session journal, a
bounded worker, and the same workflow, tool, model, approval, and security
contracts as a foreground session. The user can start it and return to a
manager TUI that lists all visible background sessions, streams their persisted
activity, and exposes safe actions such as resume, cancel, retry, and archive.

The manager is a control plane over existing runtime capabilities. It must not
create a second model dispatcher, tool executor, workflow registry, event
reducer, or persistence format that can diverge from the foreground path.
lauren-ai remains the canonical callable-tool and integration convention.

## 2. Problem statement and evidence

The current repository has the ingredients for background execution but not a
complete feature:

| Current capability | Current limitation | Evidence |
|---|---|---|
| Interactive session orchestration | The TUI is centered on the attached terminal and current conversation | `src/agenthicc/runners/tui_session.py` and `src/agenthicc/tui/` |
| Headless execution | The runner is a stdin/JSON-lines interface, not a durable job manager or control plane | `src/agenthicc/runners/headless.py` |
| Durable state | Session logs, conversation journals, and kernel persistence exist, but there is no unified user-visible background-session registry | `src/agenthicc/tui/runtime/session_log.py`, `src/agenthicc/kernel/processor.py`, and the persistence modules |
| Workflow execution | Workflows can contain retries, parallel phases, approvals, and resume state | `src/agenthicc/workflows/` and the workflow guidance |
| Concurrent work | The subagent pool supports concurrency inside an execution | `src/agenthicc/agents/` |
| Roadmap scope | A bounded background job service with status, logs, cancellation, retry policy, and resource limits is explicitly identified as missing | PRD-138's sessions and jobs roadmap table |
| Product direction | Reliable background/resume behaviour is identified as a required OpenCode-inspired surface | PRD-139's product-expansion direction |

The missing layer causes practical failures:

1. Long-running workflow work occupies a terminal even when no input is
   required.
2. A closed terminal makes it difficult to distinguish completed work from
   interrupted work.
3. Existing journals are useful for recovery but are not a discoverable list
   of jobs with current status and actions.
4. A workflow waiting for approval or input has no dedicated manager surface
   that makes the wait visible.
5. Ad-hoc backgrounding risks duplicate tool side effects, unbounded workers,
   orphaned processes, and security-policy bypasses.

## 3. Goals

### 3.1 Primary goals

- Let a user start a workflow or agent turn in the background and receive a
  stable session/job identifier immediately.
- Provide a TUI that lists all background sessions visible to the current
  user, project, and security context.
- Show lifecycle status, current workflow phase, latest activity, errors,
  approval state, timestamps, and resource information without requiring the
  worker to remain attached to the manager terminal.
- Support safe cancellation, explicit retry, resume after interruption, and
  recoverable archiving.
- Persist enough state to rediscover sessions after the manager or worker
  process exits.
- Preserve workflow phase outputs, history, approval state, summaries, and
  retry/idempotency information across resume.
- Enforce bounded concurrency, worker leases, output limits, and process-tree
  cleanup.
- Reuse the kernel event/reducer/processor, session construction, headless
  runner, workflow engine, existing tools, and security boundaries.
- Make the feature usable both interactively and from scripts through a
  stable non-interactive status/control surface.

### 3.2 Secondary goals

- Make background sessions composable with the existing user-defined workflow
  convention under `.agenthicc/workflows`.
- Make “waiting for a human” a first-class visible state rather than a hidden
  stalled process.
- Provide local observability useful for diagnosing a failed or slow session
  without introducing telemetry or sending session content off the machine.

## 4. Non-goals

- A web dashboard, hosted service, remote worker fleet, or multi-user server.
- Replacing `EventProcessor`, the kernel reducer, `SessionMemoryLayer`, the
  workflow registry/loader, or the lauren-ai integration convention.
- A second implementation of model calls, tool dispatch, approvals, or
  workflow transitions.
- Automatically approving tools, weakening sandbox/network policy, or making
  background execution more privileged than foreground execution.
- Arbitrary daemonisation of shell commands outside an agenthicc-owned worker
  lifecycle.
- Automatic or unprompted deletion of sessions or their transcripts. Explicit
  user-requested deletion is in scope and must have a visible confirmation and
  recoverability policy.
- Email, push notifications, public sharing, or cross-machine synchronization.
  These may be separate future product work.

## 5. Terminology and lifecycle contract

“Background session” is the user-facing term. “Job” is the supervisor's
internal control record. Each background session has one stable `session_id`
and one job record; they must not become two independently addressable sources
of truth.

### 5.1 Required states

The state vocabulary is intentionally explicit so the TUI, CLI, persistence,
and tests have one contract:

| State | Meaning | User action allowed |
|---|---|---|
| `queued` | Accepted but waiting for a worker slot | cancel |
| `starting` | Worker is acquiring the session and runtime resources | cancel |
| `running` | Agent/workflow work is active | cancel, inspect |
| `waiting_approval` | A tool or workflow step requires existing approval handling | approve/reject through the existing approval path, cancel |
| `waiting_input` | Work cannot continue without user-provided input | provide input, cancel |
| `retrying` | A policy-approved retry is being scheduled | cancel |
| `cancelling` | Cancellation was requested and cleanup is in progress | inspect |
| `completed` | Work reached a successful terminal result | inspect, archive, resume as a new branch only if explicitly supported |
| `failed` | Work stopped with a recoverable or terminal error | inspect, retry, resume where valid, archive |
| `cancelled` | User cancellation completed | inspect, resume, archive |
| `orphaned` | The worker disappeared or its lease expired before a terminal result | inspect, resume, cancel, archive |
| `archived` | Hidden from the default active list but recoverable through history | inspect, restore |

Legal transitions must be enforced by the domain event/reducer path, not by
TUI conditionals alone. A stale worker cannot publish `completed` after a new
worker has claimed the same session.

### 5.2 Identity and metadata

Each job record must retain:

- stable `session_id` and human-readable title;
- project root, creation command/source, workflow/profile name, and model
  provider identifier without credentials;
- creation, start, last-heartbeat, state-change, and completion timestamps;
- current phase/turn and a compact latest-activity summary;
- worker identity/lease metadata and an explicit resume marker;
- approval or input request metadata, with sensitive values redacted;
- retry count, cancellation reason, failure category, and exit reason;
- references to the canonical session journal and persisted kernel state.

The manager must not display API keys, OAuth tokens, plugin secrets, or raw
environment variables. Transcript and tool output display must use the same
redaction and truncation rules as the existing runtime logs.

## 6. User experience

### 6.1 Starting work

The first release should expose a scriptable command with a clear equivalent
interactive action. Proposed surfaces are:

```text
agenthicc run --background <request-or-workflow-input>
agenthicc jobs
agenthicc agents
agenthicc jobs list --json
agenthicc jobs status <session-id> --json
agenthicc jobs cancel <session-id>
agenthicc jobs resume <session-id>
agenthicc jobs retry <session-id>
agenthicc jobs archive <session-id>
agenthicc jobs delete <session-id>
agenthicc jobs restore <session-id>
```

The exact argument parser can follow the repository's current CLI conventions,
but the semantics are required: starting background work returns before the
agent completes, prints the stable identifier, and returns a non-success exit
code only when the job could not be accepted. The command must not leak a
second command registry.

`agenthicc agents` is a direct, memorable alias for opening the background
sessions manager. With no manager-specific arguments it must enter the same
manager mode as `agenthicc jobs`; it must not open a separate agent-registry
screen. Agent definitions under `.agenthicc/agents/` and `~/.agenthicc/agents/`
remain runtime discovery inputs, not a competing CLI destination. If future
agent-registry commands are needed, they must use an explicit subcommand that
does not change this entry point.

If the foreground TUI starts background work, it should use the canonical
command registry and trigger infrastructure. While an active session is
running, `/bg` and `/background` are equivalent built-in commands that hand
the current session to the background supervisor. The handoff must preserve
the existing `session_id`, journal, workflow phase, approvals, outputs, and
attempt history; it must not clone the session or start a duplicate worker.
The TUI should flush the current event before detaching, warn about unsent
composer text, display the resulting session ID, and leave the foreground
session view only after the worker lease is established. Both spellings must
be discoverable through the existing trigger picker.

### 6.2 Manager TUI

`agenthicc jobs` opens a dedicated manager mode using the existing Rich
workspace, reactive conversation store, input capabilities, trigger handling,
and terminal backend. The initial layout should contain:

1. An active-session table with state, title, project, phase, last activity,
   age, and a compact failure/wait indicator.
2. A details pane for the selected session with metadata, current phase,
   approval/input state, retry information, and resource limits/usage.
3. A scrollable activity pane showing persisted transcript events, workflow
   phase transitions, tool-call summaries, and structured errors.
4. A footer showing available actions, connection/refresh state, and whether
   the selected record is live, stale, or terminal.

The table must support filtering by state/project/workflow and text search by
title or session ID. Refreshing the list must be incremental and must not
restart a worker or duplicate transcript events.

Minimum actions:

| Action | Behaviour |
|---|---|
| Open/inspect | Select a session and follow new persisted activity when it is live |
| Attach/follow | Return to the normal conversation view for a live session without starting a second worker |
| Cancel | Confirm, emit a cancellation request, and show `cancelling` until cleanup is acknowledged |
| Resume | Validate the journal/resume marker and continue the same session where safe |
| Retry | Create an explicit retry attempt using the existing retry/idempotency contract; do not blindly repeat completed side effects |
| Approve/reject | Route through existing approval handling only when the selected session is waiting for approval |
| Provide input | Route through the session's existing input path only when requested |
| Archive/restore | Hide or restore the record without deleting its journal |
| Delete (`Ctrl+X`) | Confirm cancellation/cleanup when needed, then move the exact session artifacts to recoverable trash |
| Quit | Leave the manager without stopping workers |

The manager must remain useful when no job is running, when a selected job is
stale, and when a session's transcript is partially unavailable. It should
show a structured diagnostic and retain list navigation instead of crashing.

### 6.3 Manager ergonomics

The manager should feel like a control surface rather than a periodically
refreshed log viewer:

- Use `?` for a context-sensitive shortcut/help overlay and show the most
  important actions in the footer. Arrow keys and `j`/`k` should navigate;
  `Enter` should inspect or attach; `r` should refresh; and `/` should focus
  filtering without losing the selected session.
- Preserve selection, scroll position, active filters, sort order, and detail
  pane state across refreshes and terminal reconnects.
- Group sessions into active, waiting, attention-needed, completed, and
  archived sections, while allowing a flat list and deterministic sorting by
  recent activity, creation time, or state.
- Add pin/favorite, rename, and lightweight user labels so a user can find
  important sessions without changing the canonical title or execution data.
- Show unread/new-activity markers and local completion, failure, or approval
  notifications. Notifications are local UI state and must not transmit
  transcript content or session metadata externally.
- Provide an attach/follow action that returns to a live session's normal
  conversation view when safe. Attaching must transfer terminal ownership
  without starting another worker; a second manager can remain read-only.
- Provide copyable session IDs, project roots, workflow names, and failure
  summaries, with redaction applied before clipboard or JSON output.
- Support multi-select for safe bulk actions such as archive and cancel. Bulk
  delete must show the exact count and titles, require one explicit
  confirmation, and never broaden its path scope implicitly.
- Offer a pause-live-refresh action while inspecting older output, plus a
  visible “new activity available” indicator so the user's scroll position is
  not unexpectedly moved.

### 6.4 Deleting a managed session

With a background session selected, `Ctrl+X` invokes the explicit Delete action.
The action must be available for every managed session, including active ones,
but it must follow a safe two-stage contract:

1. For a live, queued, waiting, or retrying session, the manager first shows
   the session title, ID, project root, current state, and the consequence
   that its worker will be cancelled. The session is not removed until the
   worker lease is released or the bounded cleanup policy records a safe
   termination.
2. After confirmation, the service removes the session from the normal manager
   view and deletes only its exact job metadata, derived index entry, journal,
   transcript, and persisted state references. It must not recursively target
   a project root or any path outside the resolved session directory.

Deletion should move data to a local recoverable trash area for the configured
retention window by default, so an accidental `Ctrl+X` can be undone with
`Restore`/`u`. A future or explicitly configured permanent-purge mode may erase
the exact session artifacts, but it must require a stronger confirmation and
must be clearly labelled irreversible. Delete is distinct from Archive: an
archived session remains in history, while a deleted session is hidden from
normal history and visible only through trash/recovery until purged.

### 6.5 Non-interactive control

The same lifecycle operations must be available without a TTY. `list` and
`status` should support stable JSON output for automation. Mutating commands
must print the affected session ID and resulting state, use non-zero exit
codes for invalid transitions or policy denials, and never require terminal
escape sequences.

## 7. Functional requirements

### 7.1 Durable job service

Implement a bounded local supervisor behind a new, clearly owned background
runtime package (proposed location: `src/agenthicc/background/`). It must:

- accept a validated session request and atomically create the job record;
- enforce a configurable maximum number of workers and per-job limits;
- launch workers with an explicit project root and inherited security policy;
- maintain a heartbeat/lease so stale workers can be identified;
- serialize control requests such as cancel, resume, retry, approval, and
  input;
- publish lifecycle changes through domain events and the existing processor;
- preserve the foreground session path when the feature is disabled or unused;
- shut down cleanly without losing acknowledged lifecycle events;
- prevent two live workers from owning one session at the same time.

The first implementation should use local worker processes or an equivalent
isolated supervisor boundary, rather than relying on a shell `&` process that
cannot be tracked or cancelled. The process model must remain an implementation
choice until measured, but orphan detection and process-tree cleanup are
acceptance requirements.

### 7.2 Runtime reuse

The worker must construct sessions through `runners/session_context.py`, run
headless work through the existing runner or a shared extracted orchestration
path, and use the existing workflow, agent, tool, approval, memory, and
security services. It must not fork a “background-only” implementation of
those concerns.

The worker must support the existing user-defined workflow shape, including
phase outputs, rejection loops, retries, parallel phases, approval state, and
resume state. A workflow that is valid in the foreground is not silently
converted into an untracked one-shot background command.

### 7.3 Persistence and recovery

The canonical session journal and kernel persistence remain authoritative for
execution state. The background service may maintain a derived index optimized
for listing and filtering, but that index must be rebuildable and must not
become a competing transcript or lifecycle authority.

Persistence must provide:

- atomic creation and state transitions;
- durable event ordering or an equivalent monotonic sequence per session;
- restart discovery for queued, running, waiting, retrying, and terminal jobs;
- explicit marking of a missing or expired worker as `orphaned`;
- recovery from a partially written final record without silently claiming
  success;
- journal validation before resume;
- retention, archive, and recoverable-trash policy that never permanently
  deletes user work without an explicit user action.
- deletion markers or tombstones sufficient to prevent a stale worker or
  rebuilt index from resurrecting a deleted session.

After a process restart, jobs must not automatically relaunch unless an
explicit, documented restart policy enables that behaviour. The default is to
show the job as `orphaned` and require an explicit resume or retry action.

### 7.4 Cancellation, retry, and resume safety

- Cancellation is cooperative first, followed by bounded worker/process-group
  cleanup when the worker does not respond.
- A cancellation request is idempotent and remains visible while cleanup is
  pending.
- A delete request is idempotent, cannot race a live worker, and leaves an
  auditable tombstone or trash record until the configured recovery window
  expires.
- Retry is allowed only for states and failure categories permitted by policy.
- Resume continues from persisted state and does not replay acknowledged
  side-effecting tools without the existing idempotency or confirmation
  contract.
- Every attempt has a sequence/attempt identifier, while the user-facing
  session ID remains stable.
- Failed cancellation or resume validation produces an actionable structured
  error and leaves the previous journal intact.

### 7.5 Resource and concurrency limits

Configuration must include, at minimum:

- maximum background workers;
- maximum concurrent workers per project;
- per-job wall-clock and idle timeouts;
- output/transcript retention or display limits;
- optional CPU/memory/process limits where the host supports them;
- cancellation grace period;
- archive/retention policy.

Defaults must be conservative and documented. A background worker cannot
silently consume unlimited processes, disk, network requests, or transcript
output. Existing `WorkspaceView`, `NetworkGuard`, sandbox, approval, and
capability checks remain mandatory.

## 8. Architecture and ownership boundaries

The following ownership table is normative for implementation:

| Concern | Owner |
|---|---|
| Session/job domain state and legal lifecycle transitions | `kernel/state.py`, `kernel/events.py`, and `kernel/reducer.py` |
| Event loop, effect dispatch, and durable processing | `kernel/processor.py` plus the background supervisor adapter |
| Session construction and provider selection | `runners/session_context.py` |
| Foreground orchestration | `runners/tui_session.py` |
| Headless worker execution | `runners/headless.py` or a shared path extracted from it |
| Background worker lifecycle, leases, limits, and control requests | New `background/` package with one canonical service |
| Reactive manager state and mutations | `tui/conversation_store.py` |
| Manager rendering and scrollable activity | `tui/workspace/` and `ScrollBufferAppender` |
| Keys, commands, and triggers | `tui/input/`, `tui/trigger.py`, and `tui/triggers/` |
| Terminal portability | `tui/terminal/` and `cbreak_reader.py` |
| Workflow execution and resume semantics | `workflows/` |
| Session journal and durable logs | `tui/runtime/session_log.py` and the existing memory/persistence layers |
| Tool, network, filesystem, plugin, and approval policy | Existing `tools/`, `security.py`, `config.py`, and plugin trust boundaries |

The implementation must not introduce historical paths that do not exist in
the current tree, including `tui/app.py`, `tui/transcript.py`, `tui/events.py`,
`tools/hooks.py`, or `tools/executor.py`.

## 9. Security and privacy requirements

- Background execution inherits the same provider, capability, workspace,
  sandbox, network, plugin-trust, and approval policy as the originating
  session.
- A manager action cannot grant a capability that the session did not already
  have. Approval prompts remain prompts when the manager is closed.
- Job metadata, status output, and logs redact secrets and limit untrusted
  output before rendering or JSON serialization.
- Control requests validate session ownership, project root, state, and
  expected sequence/lease. Malformed or replayed requests are rejected.
- Worker startup uses an exact resolved project path and does not accept a
  broad or ambiguous destructive target.
- Cancellation terminates the owned process group without killing unrelated
  user processes.
- The default transport and registry are local-only. No session content or
  lifecycle telemetry is sent to an external service by this feature.
- If a privacy-first advertisement surface is later added, advertisements
  must not be able to observe transcript contents, tool arguments, secrets,
  or background-session identifiers without a separate explicit product and
  consent contract.

## 10. Acceptance criteria

The feature is ready for implementation-complete status when all of the
following are true:

### P0 user and lifecycle criteria

1. A user can start a supported request or workflow in the background, receive
   a stable session ID promptly, and continue using the original shell/TUI.
2. While a session is active, `/bg` and `/background` both hand off that same
   session to the background supervisor, preserve its session ID and journal,
   and do not create a duplicate worker.
3. `agenthicc agents` opens the same background sessions manager as
   `agenthicc jobs` without entering a separate agent-registry screen.
4. `agenthicc jobs` lists active and terminal background sessions after the
   manager is restarted, with deterministic state and timestamp ordering.
5. The manager shows a selected session's metadata and persisted activity,
   including phase transitions, tool-call summaries, approvals, and errors.
6. A user can attach to or follow a live session without starting a second
   worker, and can return to the manager without interrupting it.
7. A user can cancel a live session and observe `cancelling` followed by a
   terminal `cancelled` state; the worker and owned process group are cleaned
   up within the configured grace period.
8. A user can resume an orphaned, interrupted, or otherwise resumable session
   and the workflow continues from persisted state with no duplicate
   acknowledged side effects.
9. A user can retry an eligible failure, and the attempt history distinguishes
   retry from the original execution.
10. A workflow waiting for approval or input is visible as such and can be
   completed through the existing approval/input path without implicit
   approval.
11. With a session selected, `Ctrl+X` invokes Delete, requires explicit
    confirmation, safely cancels active work when necessary, and moves only
    that session's artifacts to recoverable trash. `u` restores a deleted
    session during the configured recovery window.
12. Two workers cannot concurrently own one session, including across manager
   restarts or stale-heartbeat recovery.
13. Configured worker, project, timeout, output, trash, and cleanup limits are
   enforced, and invalid configuration fails closed with a useful error.
14. Foreground sessions and existing headless invocations retain their current
    behaviour when no background option is selected.

### P0 correctness and security criteria

15. Kernel/reducer tests cover every legal lifecycle transition and reject
    illegal or stale-owner transitions.
16. Restart tests cover queued, running, waiting, retrying, terminal, stale,
    partially written, and corrupt-record scenarios.
17. Retry/resume tests prove that completed side-effecting tools are not
    blindly repeated and that failed cancellation is recoverable.
18. Permission, workspace, network, sandbox, plugin-trust, secret-redaction,
    malformed-input, and process-cleanup tests pass for both interactive and
    non-interactive control paths.
19. The job index can be deleted and rebuilt from canonical persisted session
    data without losing execution state.
20. JSON status/list/delete output is stable, redacted, machine-readable, and uses
    documented exit codes.

### P1 usability and documentation criteria

21. The manager supports state/project/workflow filtering, text search, live
    refresh, transcript scrolling, shortcut help, attach/follow, pinning,
    labels, unread markers, and graceful display of unavailable data.
22. A working user-defined workflow under `.agenthicc/workflows` can be run in
    the background, inspected, interrupted, and resumed with phase outputs and
    history preserved.
23. Non-interactive terminal tests verify that command output does not contain
    terminal control sequences and that manager startup fails gracefully when
    no TTY is available.
24. User documentation explains `/bg`, `/background`, `agenthicc agents`,
    inspecting, attaching, cancelling, resuming, retrying, archiving, deleting,
    restoring, retention, limits, and privacy guarantees.
25. Architecture and storage documentation identify the canonical journal,
    derived index, lifecycle events, restart policy, and retention behaviour.

## 11. Rollout and migration

### Phase 1 — Domain and persistence contract

- Define typed job metadata, lifecycle events, legal transitions, leases, and
  control-request envelopes.
- Add reducer and processor tests before connecting a worker.
- Define the canonical journal/index relationship and corruption handling.

### Phase 2 — Local supervisor and scriptable control

- Add bounded worker startup, heartbeat, cancellation, retry, resume, and
  orphan detection.
- Add `run --background`, `/bg`, `/background`, `agenthicc agents`, and
  non-interactive `jobs` operations.
- Exercise a built-in workflow and the repository's user-defined workflow
  convention in integration tests.

### Phase 3 — Manager TUI

- Add reactive manager state and workspace rendering within the current TUI
  boundaries.
- Add selection, filtering, scrolling, action confirmation, approval/input
  routing, attach/follow, notifications, labels, deletion-to-trash, restore,
  and incremental refresh.
- Test interactive and non-interactive terminal paths, including terminal
  loss while workers continue.

### Phase 4 — Hardening and enablement

- Add resource-limit and process-tree tests on supported platforms.
- Run corruption/restart, security, retry-idempotency, and full workflow
  matrices.
- Enable the feature by default with conservative limits after the local
  opt-in period; preserve a configuration switch for emergency disablement.
- Update README, a user guide, architecture/storage references, `llms.txt`,
  `llms-full.txt`, and the PRD status with implementation evidence.

Existing foreground sessions require no migration. Existing persisted sessions
may be shown only when they have enough metadata to be identified safely; the
implementation must not guess ownership or fabricate a resume marker. Any
record discovered without a live worker is shown as `orphaned` or historical,
not relaunched automatically.

## 12. Observability and success measures

All default observability is local and redacted. The service should expose
structured diagnostics for:

- accepted, completed, failed, cancelled, orphaned, and archived counts;
- queue wait, active duration, and recovery duration;
- retry and cancellation outcomes;
- worker-limit rejections;
- resume validation failures;
- manager refresh and rendering errors.

Success is measured by reliable user outcomes, not by backgrounding processes
alone: a user can detach a workflow, rediscover it after a restart, understand
what it is waiting for, safely intervene, and recover without duplicate tool
side effects. No external analytics are required for this measure.

## 14. Implementation evidence

The core PRD-141 phases are implemented in the current source tree:

| Phase | Evidence |
|---|---|
| Domain and persistence | `src/agenthicc/background/model.py` and `store.py` provide typed lifecycle states, legal transitions, leases, append-only fsync'd events, corruption tolerance, tombstones, archive, and recoverable trash. |
| Supervisor and scriptable control | `supervisor.py`, `worker.py`, and `cli/commands/background.py` provide bounded detached workers, per-project limits, wall-clock limits, cancellation, orphan recovery, approval control, resume/retry, JSON status, and non-zero mutation failures. |
| Foreground handoff | `integration.py` registers `/bg` and `/background` in the canonical trigger path and transfers the existing session identifier to one background worker. |
| Manager TUI | `tui/workspace/background_manager.py` provides selection, search/filter hooks, pinning, approval actions, live refresh/pause, redacted activity summaries, attach/follow, and confirmed `Ctrl+X` deletion with `u` restore. `agenthicc agents` and `agenthicc jobs` share this entry point. |
| Hardening and enablement | `[background]` settings, secret-redacted status output, workflow-result integration, failure-path tests, process E2E tests, `docs/guides/background-sessions.md`, and the maintained-surface 90% coverage gate are present. |

Verification commands:

```bash
uv run ruff check src/ tests/
uv run mypy src/agenthicc
uv run python -m agenthicc.background.coverage_gate
```

The PRD-141 gate runs the full test suite and enforces at least 90% coverage
over the new background package, its CLI commands, and its manager workspace.
The repository's broader legacy/platform coverage report remains a separate
diagnostic because it includes pre-existing optional adapters and interactive
surfaces outside this feature's ownership boundary.

## 13. Risks and decisions to resolve during design

| Risk or decision | Required treatment |
|---|---|
| In-process tasks die with the parent or cannot be force-stopped safely | Prefer an owned worker boundary with leases and process-group cleanup |
| A derived job index diverges from the session journal | Make the journal authoritative and test index rebuild |
| Resume replays a side effect | Reuse PRD-129 idempotency/attempt records and require confirmation where needed |
| Approval is requested while no TUI is open | Persist `waiting_approval`; never auto-approve; surface it on next manager open |
| Multiple projects use the same user-level manager | Scope records by exact project root and show project context in every action |
| Large transcripts make the manager slow or unsafe | Use bounded, incremental, redacted activity loading |
| Terminal disappears during an action | Make control requests durable/idempotent and leave the worker independent of the UI |
| Platform process semantics differ | Isolate process handling behind the terminal/runtime portability boundary and test supported platforms |

The design review must explicitly decide the initial local registry format and
worker IPC mechanism. Neither decision may introduce a second authoritative
execution or transcript store.

## 14. Verification plan

The implementation must run the checks relevant to each changed surface and
report environment blockers:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

Additional required coverage includes focused reducer/processor tests,
temporary-directory persistence and restart tests, workflow resume tests,
security and redaction tests, worker lease/process cleanup tests, CLI JSON
contract tests, `/bg` and `/background` trigger-picker tests,
`agenthicc agents` alias tests, `Ctrl+X` deletion/restore tests, and
interactive/non-interactive TUI tests. If public exports change, also run
`uv run nox -s llms_check` and update the generated API documentation
artifacts.

## 15. Related documentation

- [PRD-138 — Repository Improvement Roadmap](prd-138-repository-improvement-roadmap.md)
- [PRD-139 — OpenCode-Inspired Product Expansion and Privacy-First Advertisements](prd-139-opencode-inspired-features-and-privacy-first-ads.md)
- [Workflow guide](../docs/guides/workflows.md)
- [Storage reference](../docs/reference/storage.md)
- [Architecture guide](../docs/guides/architecture.md)
