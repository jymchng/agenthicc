---
title: "PRD-193: Session-scoped recurring /loop prompts and idle-safe scheduling"
status: Implemented
version: 1.1.0
date: 2026-09-20
scope: "TUI recurring prompts, session persistence, safe dispatch, and loop lifecycle controls"
related_prds:
  - PRD-141  # Background sessions and session manager TUI
  - PRD-143  # Safe commands during active runs
  - PRD-148  # Unified interrupt and graceful cancellation
  - PRD-149  # Background terminals and responsive wait control
  - PRD-150  # Client-neutral session service and event projection
  - PRD-156  # Resumable plan-mode interrupts and workflow continuation
  - PRD-170  # Durable workflow recovery
  - PRD-171  # Single live owner for resumed sessions
  - PRD-188  # Resume the latest recoverable workflow run
  - PRD-192  # Configurable ask_user timeout and best-effort fallback
tags:
  - loop
  - scheduler
  - recurring-prompt
  - tui
  - sessions
  - persistence
---

# PRD-193 — Session-scoped recurring `/loop` prompts and idle-safe scheduling

## 1. Executive summary

Add a local, session-scoped `/loop` command that repeatedly submits a prompt
or an existing slash command at a fixed interval. The first iteration runs
immediately when the session is safe to accept it; later iterations are
scheduled at the requested cadence. A loop never interrupts an active agent
turn, tool call, workflow transition, approval, question, or plan review. If
the interval expires while the session is busy, the scheduler records one due
iteration and submits it at the next safe boundary rather than building an
unbounded queue or running turns concurrently.

The feature must use agenthicc's existing session, command, runner, workflow,
checkpoint, event-log, ownership, and security contracts. It must not create a
second model executor, a second conversation, or a detached path that bypasses
permissions. A loop is a trigger and lifecycle policy; it is not a new agent
type and it is not a replacement for `goal_flow`.

The initial release supports one active loop per session. Reissuing `/loop`
atomically replaces the existing loop, and `/loop stop` removes it. The loop
record is durable, but automatic execution after a process restart requires an
explicit session resume (or an explicitly configured PRD-141 background
worker). This prevents a stale terminal process from unexpectedly resuming
mutating work. The interactive `/loops` manager provides a single table over
all valid persisted session records, with keyboard navigation, immediate
run-now, and confirmed deletion.

## 2. Research and product comparison

This PRD studies the requested behavior in OpenCode, Claude Code, and Codex as
of 2026-09-20. The comparison separates documented first-party behavior from
upstream proposals and community plugins; community behavior must not be
treated as a compatibility contract.

### 2.1 Claude Code

Claude's official power-user guidance documents `/loop` as a local recurring
task that can run for up to three days at a time. Its examples use an
interval followed by a prompt or slash command, such as `/loop 5m /babysit`.
The same guidance distinguishes local `/loop` from cloud `/schedule`; a local
loop stops when the local computer is unavailable. Anthropic's loop article
also describes an interval-based prompt that is repeatedly run on the local
computer. See the [Claude Code power-user tips](https://support.claude.com/en/articles/14554000-claude-code-power-user-tips)
and [Introducing loops in Claude Code](https://claude.com/blog/getting-started-with-loops).

Implications adopted here:

- The syntax accepts a duration and a prompt or registered command.
- The operation is local to the active client/session, not silently promoted
  to a cloud job.
- A finite lifetime is a safety default.
- A loop is a repeated user instruction, not a background model with a new
  conversation.

### 2.2 OpenCode

The current first-party [OpenCode CLI reference](https://opencode.ai/v2/docs/cli/)
documents session, model, permission, export, and other slash commands but
does not document a native `/loop` command. OpenCode's upstream feature
request [Add a `/loop` recurring prompt command](https://github.com/anomalyco/opencode/issues/23578)
describes the desired semantics: an immediate first iteration, an optional
interval, explicit stop, session scoping, and server-backed lifecycle. The
request is closed as not planned, so it is evidence for the requested design,
not evidence that current OpenCode ships the command.

Community loop plugins further demonstrate useful design considerations such
as idle-safe dispatch, persistence, one task per session, status/cancel
controls, bounded retries, and adaptive backoff. Those plugins are not
normative dependencies for this PRD. agenthicc should reuse its own durable
session and background-session contracts instead of installing a separate
scheduler daemon.

### 2.3 Codex

The official [Codex CLI documentation](https://developers.openai.com/codex/cli)
currently documents interactive commands such as `/init`, `/status`,
`/permissions`, `/model`, and `/review`, but does not expose a native
recurring `/loop` command. An upstream [Codex `/loop` proposal](https://github.com/openai/codex/issues/15679)
requested a minute-based interval, queueing while a turn is running,
replacement when a loop is re-created, and removal after completion; it is
closed as not planned. OpenAI's [scheduled tasks documentation](https://developers.openai.com/es-419/docs/automations)
describes a separate ChatGPT/Codex managed scheduling surface, not an
interactive local CLI loop.

The Codex evidence reinforces two requirements: queued work must not interrupt
the current turn, and scheduling must be represented as explicit state rather
than inferred from transcript text. The internal Codex “agent loop” is an
execution loop and is not the user-facing recurring `/loop` feature specified
here.

### 2.4 Portable contract

The common, useful contract is therefore:

| Capability | Claude Code | OpenCode evidence | Codex evidence | agenthicc decision |
| --- | --- | --- | --- | --- |
| Duration + prompt syntax | Documented | Proposed | Proposed | Required |
| Immediate first iteration | Implied/documented examples | Proposed | Proposed | Required |
| Do not interrupt an active turn | Required for a usable local agent | Proposed | Explicit request | Required |
| Explicit stop/status | Local task lifecycle | Proposed | Explicit request | Required |
| Session-bound conversation | Local session behavior | Proposed | TUI request | Required |
| Durable restart behavior | Local process bounded | Plugin-dependent | Not documented | Explicit resume only |
| Cloud scheduling | Separate `/schedule` | Not native | Separate managed tasks | Out of scope |
| Multiple concurrent loops | Not used as the MVP contract | Plugin-dependent | Replacement requested | One active loop |
| Model-created scheduling | Not required | Plugin-dependent | Not required | Out of scope |

## 3. Problem statement

agenthicc currently has commands that start, stop, inspect, and resume agent
work, but no first-class way to ask the same session to check or continue a
task periodically. Users consequently keep terminals open and manually resend
the same prompt, use external cron jobs that cannot safely target a live
session, or ask the model to simulate a scheduler inside one turn. Each option
can lose conversation context, create overlapping turns, bypass the TUI's
busy policy, or start a second workflow owner.

The existing repository already contains the ownership boundaries needed for a
safe implementation: a canonical command registry, a session-scoped runner,
durable session/event logging, background-session leases, workflow checkpoints,
and unified cancellation. The missing product contract is how a scheduled
trigger enters those boundaries and how its state survives a transient error,
restart, or workflow wait.

## 4. Goals

1. Provide `/loop [interval] <prompt-or-command>` in the interactive TUI.
2. Submit the first iteration immediately when the session is idle, otherwise
   retain one pending iteration until the next safe dispatch boundary.
3. Repeat the same prompt or registered slash command at a fixed interval while
   preserving the session's existing `conversation_id`, memory, workflow run,
   approvals, checkpoints, and transcript.
4. Make loop state observable and controllable with `/loop status`,
   `/loop stop`, `/loop pause`, and `/loop resume`.
5. Rehydrate a loop when a user explicitly resumes its session, without
   replaying every missed interval or starting an unowned process.
6. Enforce the same permissions, workspace boundaries, network/browser/MCP
   policies, approval gates, cancellation, and single-owner lease used by a
   manually submitted turn.
7. Bound local automation with a finite default lifetime, interval limits,
   prompt-size limits, failure backoff, and an explicit stop path.
8. Make the lifecycle testable through deterministic clocks, fake runners, and
   isolated durable stores.

## 5. Non-goals

- Cloud scheduling, hosted cron, notifications, or a remote control plane.
- A new model executor, conversation store, workflow engine, or persistence
  format unrelated to the existing session contracts.
- Arbitrary shell commands, Python snippets, or URLs as scheduler payloads.
- Model- or tool-created loops in the first release. A user must explicitly
  create or change a loop through the command surface.
- Natural-language interval parsing. `5m`, `1h`, and `2d` are accepted;
  phrases such as “every few minutes” are rejected with help text.
- Prose-based automatic completion detection. A model saying “done” is not a
  reliable lifecycle signal; users stop a loop or configure a bounded run/age
  limit.
- Multiple active loops per session. A future PRD may add named loops after
  the single-loop lifecycle is proven.
- A detached process that continues work after the owning session is closed.
  Explicit PRD-141 background execution may be added as a later integration,
  but it is not silently enabled by this feature.

## 6. User experience and command contract

### 6.1 Create or replace a loop

```text
/loop [<interval>] <prompt or registered slash command>
```

Examples:

```text
/loop 5m Check the deployment and fix a failing health check.
/loop 30m /status
/loop 1h Review the current workflow for unresolved failures.
```

The interval is optional. When omitted, the configured
`loops.default_interval_s` is used and the confirmation must display the
resolved interval. The default prompt is required unless a future, explicitly
specified `.agenthicc/loop.md` feature is added; this prevents an accidental
bare `/loop` from scheduling an opaque maintenance instruction.

The command creates a new loop or atomically replaces the current session's
loop. Replacement must not cancel an already-running agent turn. It updates
the loop after the turn reaches a safe boundary and reports the new schedule.
The first iteration is due immediately. If the session is busy, it is marked
`waiting_for_idle` and is coalesced to one dispatch.

The prompt is either ordinary user text or one existing slash command. A
payload beginning with `/loop` is rejected to prevent recursive scheduling.
Unknown slash commands are rejected at creation time. A scheduled slash
command is routed through the canonical command registry at execution time;
it does not call a handler directly and does not bypass its busy policy.

### 6.2 Lifecycle commands

```text
/loop status
/loop pause
/loop resume
/loop stop
```

These are control commands and must remain responsive according to the
existing safe-command policy. They do not create an agent turn. `/loop stop`
is idempotent and clears a pending dispatch without interrupting a currently
running iteration; the active turn finishes under normal cancellation/error
rules, and no future iteration is scheduled. `/loop pause` retains the loop
definition but prevents dispatch. `/loop resume` reactivates it and makes one
iteration due immediately. `/loop status` reports state without exposing the
full stored prompt by default.

When no loop exists, status and stop return a concise, non-error message.
When creation is rejected, the command explains the invalid interval, payload,
limit, or unavailable session without changing existing loop state.

### 6.3 TUI presentation

Creation, replacement, pause, resume, stop, expiry, and terminal failure emit
one structured event and one concise human-readable status line. The footer or
session status area shows only the active state and next due time, for example:

```text
↻ Loop active · every 5m · next in 4m 12s
```

Each actual iteration remains a normal visible user/assistant/tool exchange in
the same transcript. Timer ticks and repeated status redraws must not flood
the scroll appender. The status view includes:

- lifecycle state;
- interval and next due time;
- last dispatch and last completion;
- successful, skipped/coalesced, and failed run counts;
- consecutive failure count;
- expiry or max-run deadline; and
- a redacted, bounded prompt preview.

### 6.4 Manage persisted schedule jobs

```text
/loops
```

`/loops` opens a table containing every valid `loop.json` record below the
configured session store, not just the loop attached to the foreground
session. The table displays a short job ID, lifecycle state, interval, next
due time, owning session, and a bounded redacted payload preview. It is a
projection of the existing session records; it is not a second scheduler
registry.

The selected row is controlled with the arrow keys (also `j`/`k`), page keys,
`Home`, and `End`. `Enter` requests an immediate run through the owning
session's normal scheduler. For the current session this wakes the scheduler
immediately. A foreign live owner is never raced or stolen; the UI explains
that the user must resume that session. An unowned foreign record is marked
due and runs when that session is explicitly attached. This owner-safe
behavior is required even though the UI action is called “run now”.

Pressing `d` opens a confirmation prompt and `Enter` (or `y`) deletes the
selected record using its loop ID as a compare-and-swap precondition. `Esc` or
`n` cancels deletion. Deleting a current-session job stops future scheduling
without interrupting a turn already in progress. Missing, replaced, corrupt,
or concurrently changed records produce an inline diagnostic and never delete
another job.

## 7. Functional requirements

### FR-1 — Typed loop state

Define a versioned, typed loop record containing at least:

- `loop_id` and `session_id`;
- owning project/workspace identity;
- the existing `conversation_id`;
- payload kind (`prompt` or `command`) and payload;
- interval in seconds;
- `created_at`, `updated_at`, `next_due_at`, and optional `expires_at`;
- lifecycle state (`scheduled`, `waiting_for_idle`, `running`, `paused`,
  `stopped`, `expired`, or `failed`);
- run counters and the last run result summary;
- consecutive failure count and backoff deadline; and
- schema version and last owner metadata.

The domain type must reject impossible combinations, negative durations,
oversized payloads, timestamps that move backwards, and transitions from a
terminal state unless an explicit replacement creates a new record.

### FR-2 — Deterministic interval parsing

Accept a strict, case-insensitive duration grammar of an integer followed by
`s`, `m`, `h`, or `d`. Convert it to integer seconds, reject overflow and
zero, and enforce configurable minimum and maximum intervals. Error messages
must include accepted examples. The parser must be independent of wall-clock
time and easy to unit test.

### FR-3 — Safe first dispatch

Creating or resuming a loop marks one iteration due immediately. Dispatch is
performed by the existing session orchestration path only when:

- the session has the live owner lease;
- no agent turn, tool execution, workflow transition, approval, question,
  plan-review modal, or cancellation cleanup is active;
- the session is not closed or terminally failed; and
- the normal mode/security policy permits the operation.

The scheduler must never call the provider or workflow runner directly from a
timer callback.

### FR-4 — Fixed cadence and coalescing

After a dispatch, the next due time is computed from the configured interval.
If one or more intervals elapse while busy, the scheduler records the missed
time and retains exactly one pending iteration. It must not enqueue one user
message per missed interval. Once idle, it dispatches at most one iteration and
advances the schedule. The implementation must define and test whether the
next deadline is anchored to the scheduled deadline or the actual dispatch;
the chosen v1 behavior is to anchor it to the actual successful dispatch to
avoid an immediate burst after a long turn.

### FR-5 — No overlap

At most one loop iteration may be in `running` state for a session. A second
timer tick, manual user message, resume event, or duplicate scheduler callback
must observe the active run and coalesce rather than start another model turn.
This invariant must hold across async tasks and after a process restart through
the existing session owner/lease mechanism.

### FR-6 — Existing execution path

An iteration must enter the same path as a user-submitted message, including:

- the same `conversation_id` and `ConversationStore`;
- the same prompt contract and context assembly;
- the same tools, MCP servers, capabilities, and workflow registry;
- the same checkpoint and journal writes;
- the same retry and transient-error handling;
- the same approvals, questions, cancellation, and interruption recovery; and
- the same usage accounting and event projection.

The source must be identifiable as `loop` in structured metadata, while the
model-visible content remains the requested prompt/command plus a concise
stable marker when needed for auditability. Loop metadata must not become a
growing system-prompt suffix that invalidates provider caches on every tick.

### FR-7 — Prompt and command payloads

Ordinary prompt payloads are submitted as a new user turn. Command payloads
are parsed and dispatched through the canonical command registry after the
loop reaches idle. A command that requires interactive user input must follow
the existing question/approval behavior; the scheduler must wait rather than
auto-answer, bypass, or repeatedly reinject the command. A command that is
not legal in the current mode returns a structured failed iteration and uses
the normal failure policy.

### FR-8 — Persistence and explicit rehydration

Persist loop state atomically with the existing session durability boundary.
The durable representation must be recoverable from a partially written or
older versioned record without corrupting the session transcript. On clean
session resume, load and validate the loop record using the same session and
workflow identity checks as the transcript/checkpoint.

Automatic restart behavior is deliberately conservative:

- a process shutdown stops execution but does not silently delete the loop;
- launching agenthicc without resuming that session does not run the loop;
- `--resume`, `--continue`, or the interactive sessions picker may rehydrate
  it after the live owner is acquired;
- missed intervals are coalesced into at most one due iteration; and
- an expired, stopped, invalid, or already-owned record cannot be reactivated
  without the normal recovery/ownership path.

### FR-9 — Lifecycle transitions and idempotency

All lifecycle mutations are serialized and idempotent. Repeating stop, pause,
or resume after the same state is reached returns the current state without
duplicating events. Replacement uses compare-and-swap/version checks so a
stale TUI cannot overwrite a newer loop. Every transition records the actor
(`user`, `scheduler`, `resume`, or `recovery`) and reason.

### FR-10 — Failure and retry policy

An iteration's provider, tool, workflow, or command failure is recorded without
losing the loop definition or preceding transcript. Reuse the existing
transport retry policy for the turn itself; the loop scheduler must not
duplicate a turn merely because it saw a transient error. After a terminal
iteration result, schedule the next run from the actual completed dispatch.

Consecutive scheduler-level failures use bounded exponential backoff with
jitter, capped below the configured interval. After the configured maximum
consecutive failures, transition to `failed`, stop scheduling, and display an
actionable status message. A successful run resets the consecutive-failure
counter. Error records must preserve the exception category and redacted
message, never credentials or full provider headers.

### FR-11 — Expiry and bounds

Default settings must include a finite maximum loop lifetime matching the
local, bounded nature of Claude's documented feature. The initial default is
72 hours. Enforce configurable minimum/maximum interval, maximum prompt bytes,
maximum retained run history, maximum consecutive failures, and optional
maximum run count. A user may stop earlier; there is no unbounded “forever”
default. Expiry is a normal terminal state and is visible in status.

### FR-12 — Configuration

Add a typed `loops` configuration section with documented defaults and
validation. The minimum contract is:

```toml
[loops]
enabled = true
default_interval_s = 600
min_interval_s = 60
max_interval_s = 86400
max_age_s = 259200
max_prompt_bytes = 16384
max_consecutive_failures = 3
busy_poll_s = 1
persist = true
allow_slash_commands = true
```

The exact configuration owner and naming must follow current config conventions.
Environment/CLI overrides must use the existing precedence rules. Invalid
configuration fails validation with a field-specific message; it must not
silently disable security controls or convert a finite bound to infinity.

### FR-13 — Ownership and concurrency

The loop scheduler is session-owned. It may run only after the same live owner
claim used by resumed sessions succeeds. A second process sees the loop as
owned and reports that the original session must be resumed or stopped; it
must not steal or duplicate the schedule. Lease loss stops new dispatches and
leaves a recoverable durable state.

### FR-14 — Background integration boundary

The first release is foreground/session based. If PRD-141 background execution
is enabled for loops in a later slice, it must use the existing background
session record, worker lease, cancellation, and event projection. It must not
introduce a second scheduler store or a daemon with weaker security. A loop
created without an explicit background option must never escape the owning TUI
process.

### FR-15 — Headless and API behavior

`--headless` must not silently start a wall-clock loop without an active input
and lifecycle owner. It should either expose the same loop commands through
the existing command protocol or return a clear “interactive session required”
result. If a future client-neutral session service adopts the scheduler, the
same state machine and event schema must be used; no TUI-only duplicate is
permitted.

### FR-16 — Observability and redaction

Emit structured lifecycle events for creation, replacement, due/coalesced,
dispatch, completion, failure, pause, resume, stop, expiry, lease loss, and
rehydration. Events include IDs, timestamps, state, counters, and reason, but
not API keys, authorization headers, full prompts, tool arguments, or full
provider errors. Status uses a bounded redacted preview and should permit a
user to inspect the full prompt only through the existing local session access
controls.

### FR-17 — Persisted job manager

The interactive command registry must expose `/loops` as an immediate
read-only command. It must load all valid persisted loop records from the
session store, sort them deterministically, and present them in a Rich table
overlay with paging/navigation. The overlay must:

- invoke the scheduler's run-now operation when `Enter` is pressed on a row;
- require explicit confirmation before deleting a row;
- use an ID precondition so a stale table cannot delete a replacement;
- preserve the live-owner lease and never steal a foreign active session; and
- keep payload display bounded and redacted.

The command must remain safe for command-dispatch embedders without an overlay
host by rendering a bounded table fallback. Headless mode must return the
existing structured interactive-required result rather than creating a second
management protocol.

## 8. Proposed architecture and data flow

### 8.1 Ownership

Use a small scheduling domain under the existing runner/session boundary (for
example, `src/agenthicc/loop/`) with the following responsibilities. Names are
proposals; final names must follow repository conventions.

| Component | Responsibility | Must not own |
| --- | --- | --- |
| `LoopSpec` / `LoopState` | Immutable validation and transitions | Provider calls |
| `LoopStore` | Versioned atomic persistence and recovery | TUI rendering |
| `LoopController` | Parse commands and request state changes | Timer callbacks |
| `LoopScheduler` | Clock, due detection, coalescing, backoff | Direct model execution |
| `TUISession` / session service | Safe-boundary dispatch through existing send path | Duplicate schedule state |
| `ScrollBufferAppender` / workspace | Concise projection of lifecycle events | Scheduling decisions |

The canonical command registry owns `/loop` discovery and help. The runner
owns the lifecycle of the scheduler task. The session log owns durable event
projection. The workflow engine remains responsible for workflow phases and
checkpoints.

### 8.2 Data flow

```text
User enters `/loop 5m inspect deployment`
        │
        ▼
Canonical command registry / LoopController
  parse duration + payload; validate config/mode/session
        │
        ▼
LoopStore atomic replace (loop_id, session_id, conversation_id, next_due=now)
  append loop.created/replaced event; show confirmation
        │
        ▼
LoopScheduler clock tick
  acquire/verify session owner
  if paused/stopped/expired/failed: no-op
  if busy: state=waiting_for_idle; retain one due iteration
  if idle: atomically claim iteration and enqueue a session send
        │
        ▼
Existing TUISession/session service send path
  same ConversationStore + conversation_id
  same prompt contract, tools, workflows, approvals, checkpoints, retries
        │
        ├── question/approval/workflow wait → ordinary existing wait state
        ├── transient/terminal error → ordinary result + loop failure update
        └── completion → loop run event; next_due=completed_dispatch+interval
        │
        ▼
LoopStore + SessionEventLog + TUI projection
  status counters, next due, redacted reason, durable recovery state
```

### 8.3 Restart and recovery flow

```text
Process exits or loses lease
        │
        ▼
Persisted loop remains non-running with owner/recovery metadata
        │
User explicitly opens `--resume`, `--continue`, or sessions picker
        │
Acquire session lease → validate session/conversation/workflow identity
        │
Rehydrate loop → coalesce missed intervals → safe-idle dispatch at most once
        │
Invalid topology/checkpoint/owner → preserve evidence and report recovery error;
never reset to INIT and never create a duplicate loop
```

### 8.4 Cache and context contract

The loop must preserve the existing stable system prompt and workflow prompt
prefix. Per-iteration data belongs in the user-turn or structured event
metadata, not in a mutated system prompt. The same conversation store is
reused so the model can see the prior transcript, but the scheduler must not
append timer diagnostics, duplicate status frames, or unbounded run history to
every request. Compaction and context limits continue to use the established
conversation/checkpoint policy.

## 9. Acceptance criteria

### Parsing and creation

- **AC-01:** `/loop 5m check status` creates a typed loop with a five-minute
  interval and reports its ID, resolved interval, and next action.
- **AC-02:** `/loop check status` uses the configured default interval.
- **AC-03:** `s`, `m`, `h`, and `d` values are parsed deterministically;
  malformed, zero, negative, overflow, below-minimum, and above-maximum
  values are rejected without changing an existing loop.
- **AC-04:** Creating a second loop replaces the first atomically, leaves no
  duplicate scheduler task, and does not interrupt an active iteration.
- **AC-05:** Nested `/loop`, unknown commands, oversized prompts, and payloads
  disallowed by configuration are rejected with actionable help.

### Dispatch and execution

- **AC-06:** A newly created loop dispatches one first iteration immediately
  when idle, through the same send/runner path as a manual user message.
- **AC-07:** When the session is busy, the loop waits for a safe boundary and
  coalesces any number of elapsed intervals into one pending iteration.
- **AC-08:** No timer tick can produce concurrent agent turns, duplicate tool
  calls, duplicate workflow transitions, or duplicate user messages.
- **AC-09:** The iteration retains the same `conversation_id`, conversation
  store, session memory, active workflow run, checkpoints, tools, policies,
  approvals, and usage accounting as the manually submitted session.
- **AC-10:** A loop command is routed through the canonical command registry;
  it cannot bypass command policy or invoke arbitrary shell code.
- **AC-11:** A loop that reaches a question, approval, plan review, or workflow
  wait follows the existing interaction state and does not auto-answer or
  reinject the prompt.

### Lifecycle, errors, and persistence

- **AC-12:** `/loop status`, `/loop pause`, `/loop resume`, and `/loop stop`
  are idempotent and work while an agent turn is active according to the safe
  command policy.
- **AC-13:** Stop prevents all future dispatches, clears a coalesced pending
  iteration, and does not corrupt or retroactively remove the transcript.
- **AC-14:** A transient provider error does not duplicate the iteration or
  reset the workflow to its first phase; the loop records the result and
  applies the bounded failure policy.
- **AC-15:** Consecutive failures use bounded backoff and transition to
  `failed` after the configured threshold; success resets the failure count.
- **AC-16:** The default finite lifetime expires the loop and prevents further
  work; expiry is persisted and shown in status.
- **AC-17:** A process restart does not run loops from unrelated sessions.
  Explicit resume rehydrates a valid loop, coalesces missed work to at most
  one iteration, and cannot bypass the live-owner claim.
- **AC-18:** A partial/corrupt/old loop record produces a recoverable diagnostic
  and leaves the session transcript/checkpoint intact.
- **AC-19:** Lease loss prevents new dispatches and makes the loop recoverable
  only through the normal owner/resume path.

### UI, security, and quality

- **AC-20:** TUI output gives one concise lifecycle message per transition,
  shows next due state without redraw flooding, and displays a bounded redacted
  prompt preview.
- **AC-21:** Loop iterations inherit all existing workspace, network,
  browser, MCP, tool, approval, mode, and cancellation policies. A loop cannot
  widen permissions compared with a manual turn.
- **AC-22:** Logs and status never expose credentials, authorization headers,
  full provider errors, or the full stored prompt by default.
- **AC-23:** Configuration validation enforces finite bounds and existing
  precedence rules; changing configuration cannot silently reactivate a stopped
  or expired loop.
- **AC-24:** Unit, integration, and E2E tests cover all behavior above with a
  fake clock and deterministic runner; the relevant lint, type, coverage, and
  documentation gates pass.
- **AC-25:** `/loops` opens a table containing all valid persisted loop records
  across session directories, with deterministic ordering, pagination, and a
  bounded redacted payload preview.
- **AC-26:** Arrow/j/k/page/Home/End navigation changes the selected row, and
  pressing `Enter` requests run-now for exactly that record through the
  scheduler rather than invoking a provider directly.
- **AC-27:** Pressing `d` requires a visible confirmation; confirming deletes
  only the selected record, while canceling leaves it unchanged.
- **AC-28:** A current-session run-now wakes its scheduler; a foreign live
  owner is protected from races; an unowned foreign job is marked due for its
  next explicit resume.
- **AC-29:** A stale or concurrently replaced row cannot delete the new job,
  and a corrupt/unreadable record cannot make the jobs table crash or expose
  its raw contents.

## 10. Testing strategy

### Unit tests

- duration parser grammar, conversion, and bounds;
- loop-record validation and every legal/illegal state transition;
- coalescing and actual-dispatch cadence using a fake monotonic clock;
- no-overlap claim under concurrent ticks;
- replacement, pause/resume, stop, expiry, and idempotency;
- failure backoff, jitter cap, reset after success, and terminal failure;
- bounded prompt preview and redaction;
- configuration defaults, precedence, invalid values, and finite-limit checks;
- command parsing, nested-loop rejection, command registry lookup, and help;
- serialization migration and corruption handling.
- persisted job listing, deterministic ordering, run-now selection, delete
  confirmation, stale-row protection, and bounded table rendering.

### Integration tests

- controller → store → scheduler wiring in a temporary session directory;
- scheduler → existing session send path with a fake provider;
- preservation of `conversation_id`, `ConversationStore`, event journal,
  workflow phase/checkpoint, usage, and retry state;
- busy TUI, tool execution, approval/question, plan-review, cancellation, and
  workflow-wait behavior;
- owner lease acquisition/loss and two-process duplicate-dispatch prevention;
- atomic persistence across restart and missed-interval coalescing;
- structured event projection and scroll-appender output suppression;
- security-policy inheritance for filesystem, network, browser, MCP, and mode
  checks;
- headless behavior and explicit unsupported/background boundaries.

### End-to-end tests

Run a real local TUI/session harness with a deterministic fake model and
temporary project:

1. create `/loop 1m inspect the fixture`, observe the immediate turn, advance
   the fake clock, and observe exactly one subsequent turn;
2. keep a long tool call active while advancing several intervals, then verify
   one queued iteration runs after idle;
3. replace, pause, resume, and stop the loop while a turn is active;
4. interrupt/restart, resume the session, and verify the same transcript,
   workflow phase, loop state, owner lease, and coalesced due work;
5. inject a transient transport error, retry it, and verify no phase reset or
   duplicate loop turn;
6. exercise a scheduled slash command, an approval/question wait, and a
   denied tool operation;
7. exceed lifetime/failure limits and verify no further provider calls; and
8. verify status/log output is concise and contains no secret or full prompt.

Tests must not sleep for real intervals. A fake clock and manually triggered
 scheduler tick are mandatory for deterministic CI execution.

## 11. Configuration and migration

The feature is enabled by default as a command capability, but it only becomes
active after an explicit user `/loop` command. Existing sessions have no loop
record and therefore behave exactly as before. The loop record is versioned
from its first release; future migrations must be additive and must preserve
the terminal state of stopped/expired loops.

No existing transcript or workflow checkpoint should be rewritten merely to
add loop metadata. If a session store cannot persist the loop atomically, loop
creation must fail clearly rather than creating an in-memory schedule that
cannot be recovered or stopped reliably.

## 12. Security, privacy, and reliability

- Treat the scheduled payload as user data, not trusted scheduler code.
- Re-run all normal authorization, capability, workspace, network, browser,
  MCP, and approval checks on every iteration; do not cache an approval forever.
- Store loop data with the same owner-only permissions and local trust boundary
  as session state. Do not put secrets in lifecycle events or status output.
- Use the session lease and compare-and-swap revision to prevent two TUI
  processes from dispatching one iteration.
- Bound prompt size, loop age, interval frequency, run history, retry delay,
  and failure count to prevent local resource exhaustion.
- Make stop/cancel precedence explicit: a stop acknowledged before dispatch
  wins over a due timer; a currently running turn follows the existing graceful
  cancellation contract.
- Preserve evidence after errors. Never recover a loop by resetting a workflow
  manifest or checkpoint to INIT, and never discard a completed prior turn.
- Do not send loop payloads to telemetry or remote services beyond the normal
  provider request required to execute the user's instruction.

## 13. Implementation plan

1. Confirm the current command, session, event-log, background lease, and
   persistence APIs and select the canonical ownership boundary.
2. Introduce typed loop state, strict duration parsing, configuration, and a
   versioned store with atomic writes and corruption diagnostics.
3. Add the controller and canonical `/loop` command handlers with status,
   pause, resume, stop, replacement, and help output.
4. Add a scheduler driven by an injectable clock. Integrate it with the
   existing safe session-dispatch boundary and owner lease; do not call model
   providers from the scheduler.
5. Add loop event schemas/projections and concise TUI status rendering without
   repeated scroll frames.
6. Add failure/backoff, expiry, restart rehydration, missed-work coalescing,
   and lease-loss recovery.
7. Add unit, integration, and E2E tests from Section 10 before enabling the
   command in the default registry.
8. Add the `/loops` job-management overlay and owner-safe run/delete
   operations over the existing session records.
9. Update user documentation, configuration reference, command help,
   architecture/storage references, `llms-full.txt` if public symbols are
   exported, and this PRD's status/evidence after implementation.

## 14. Rollout and observability

Roll out behind a configuration flag in the first development slice while
the fake-clock and session-restart tests stabilize. Enable by default only
after the no-overlap, owner-lease, persistence, and security integration tests
pass. During rollout, count loop creations, replacements, dispatches,
coalesced ticks, failures, expiries, lease conflicts, and recovery failures;
record only IDs and categories, not prompt content.

If a severe scheduler defect is found, disabling `loops.enabled` must prevent
new dispatches while retaining enough state for a later explicit recovery.
Stopping or deleting loop records is not a rollback mechanism unless the user
explicitly requests it.

## 15. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| A timer starts a second live turn | Single session-dispatch gate, atomic iteration claim, lease checks, no-overlap tests |
| Restart causes an unexpected burst | Explicit resume only and missed-interval coalescing |
| Loop bypasses a newly changed permission | Re-evaluate policy on each iteration |
| Prompt persistence leaks sensitive text | Owner-only storage, bounded/redacted status, no prompt telemetry |
| Long workflow waits make loops look stuck | Explicit `waiting_for_idle`/running state and status timestamps |
| Provider errors reset workflow progress | Reuse durable turn/checkpoint recovery and record loop result separately |
| Repeated failures create request storms | Backoff, maximum failures, maximum age, and one pending iteration |
| Multiple processes race after resume | Existing live-owner lease and compare-and-swap revisions |
| Scheduler implementation becomes a second runtime | Timer only enqueues through the canonical session/command path |

## 16. Open decisions resolved for v1

The following choices remove ambiguity for implementation:

- **First run:** due immediately, but dispatched only at a safe idle boundary.
- **Cadence:** fixed interval; next due is anchored to the actual successful
  dispatch so a long turn cannot cause a burst.
- **Concurrency:** one active loop per session; replacement is atomic.
- **Restart:** durable record, explicit session resume required for execution.
- **Missed intervals:** coalesce to one pending iteration.
- **Completion:** explicit stop or finite bound; no prose-based auto-stop.
- **Payload:** ordinary prompt or registered slash command; no arbitrary shell.
- **Persistence:** reuse the session durability boundary and event log.
- **Background execution:** not implicit; later work must reuse PRD-141.
- **Default lifetime:** 72 hours, with validated finite configuration bounds.
- **Job management:** `/loops` is a table projection over one persisted record
  per session; it does not introduce multiple active loops in one session or a
  second scheduler store.

These decisions are product assumptions, not claims that all studied tools use
identical behavior. Any change should update this PRD and its acceptance tests
before implementation.

## 17. Definition of done

- Every functional requirement and acceptance criterion is implemented and
  linked to tests.
- The existing manual-message path remains the only model/workflow execution
  path.
- Unit, integration, E2E, lint, formatting, type, type-audit, and relevant
  documentation checks pass.
- Resume/restart tests demonstrate no duplicate turns, no loss of transcript,
  no workflow reset, and no unauthorized dispatch.
- User documentation explains syntax, finite lifetime, status/stop behavior,
  restart semantics, and security boundaries.
- The PRD status is `Implemented` after the implementation and verification
  evidence are recorded below.

## 18. Implementation and verification evidence

The v1 implementation is complete in the current source tree:

- `src/agenthicc/runners/loop_scheduler.py` contains the typed state machine,
  strict duration parser, atomic `LoopStore`, idle/coalescing scheduler,
  bounded failure backoff, expiry, redacted status, and lifecycle controls.
- `src/agenthicc/commands/builtins.py` registers `/loop` and keeps lifecycle
  controls in the immediate-control lane while creation queues safely.
- `src/agenthicc/runners/tui_session.py` dispatches loop payloads through the
  normal `handle_send()` path with the same session/conversation/workflow
  ownership, and projects lifecycle status into the TUI footer.
- `src/agenthicc/config.py` and `src/agenthicc/config_template.py` provide the
  validated `[loops]` settings and finite defaults.
- `src/agenthicc/runners/headless.py` returns a structured
  `loop_interactive_required` result rather than silently scheduling from
  stdin.
- `src/agenthicc/tui/workspace/overlays/loops.py` renders the paginated
  persisted-job table, handles keyboard selection, run-now, and confirmed
  deletion; `LoopStore.list_all()` and the manager operations preserve
  session-owner and compare-and-swap boundaries.
- `tests/unit/test_loop_scheduler.py`,
  `tests/unit/test_loop_jobs_overlay.py`,
  `tests/integration/test_loop_integration.py`, and
  `tests/e2e/test_loop_e2e.py` cover the scheduler, persistence, command
  wiring, rehydration, coalescing, failure limits, stop behavior, table
  navigation, run-now, and confirmed deletion.
- User-facing documentation is synchronized in the README, the TUI command
  and configuration chapters, the session/resume and troubleshooting/FAQ
  chapters, the CLI reference, and the storage reference.
- The current TUI architecture pointer also documents the `/loops` overlay
  ownership and run/delete safety boundary. Two older tracked references,
  `docs/guides/commands.md` and `docs/reference/fact-base.md`, still contain
  the pre-feature command count because they are `root:root` mode `0644` in
  this checkout and cannot be updated by the current non-root workspace user.
  Their stale count is an environment permission issue, not a runtime source
  of truth; the writable canonical command and CLI pages contain the current
  `/loop` and `/loops` contract.

Verification completed for the implementation surface:

```text
17 loop unit/integration/E2E tests: passed
89 loop plus command/TUI/session regression tests: passed
relevant Ruff checks: passed
relevant mypy checks: passed
type-audit baseline: passed
```

The full repository suites were also executed. Remaining failures are
pre-existing/environmental and unrelated to PRD-193: reasoning-content tests
expect a newer lauren-ai `Completion` contract than the installed package,
reconstruct-site tests target a root-owned unreadable fixture, and one
make-book integration test requires the optional Pillow dependency. The
repository-wide mypy/format gates likewise report existing debt in unrelated
workflow, MCP, screenshot, and process-lease modules. No loop test or touched
loop integration failed.
