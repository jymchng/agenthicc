---
title: "PRD-149: Background Terminals and Responsive Wait Control"
status: Implemented
version: 0.1.0
created: 2026-07-25
related_prds:
  - PRD-138  # Repository Improvement Roadmap
  - PRD-139  # OpenCode-Inspired Product Expansion and Privacy-First Advertisements
  - PRD-141  # Background Sessions and Session Manager TUI
  - PRD-143  # Safe Commands During Active Runs
  - PRD-144  # Resize-Safe Waiting Modals and Pause-Aware Display Timing
  - PRD-148  # Unified Interrupt and Graceful Cancellation
supersedes: []
tags:
  - terminals
  - subprocesses
  - background
  - tui
  - workflows
  - cancellation
  - observability
---

# PRD-149 — Background Terminals and Responsive Wait Control

Study date: 2026-07-25. This PRD defines the user-facing and runtime contract
for long-running terminal commands that continue under agenthicc ownership
while the foreground TUI remains responsive. It is an extension of PRD-141's
durable background-session control plane, not a replacement for it.

## 1. Executive summary

Long-running commands such as `uv run pytest tests/unit -q`, builds, package
installs, and development servers currently behave like one-shot tool calls:
the execution tool creates a subprocess, waits for `communicate()`, and only
then returns the result to the agent. The TUI has no first-class handle for the
process while it runs. A user cannot reliably see how many terminal processes
are active, inspect their output, or stop one without interrupting the entire
agent turn.

PRD-149 adds owned background terminals. A terminal is an agenthicc-managed
process group with a stable handle, bounded output, lifecycle state, elapsed
time, and an explicit relationship to the originating session and tool call.
The event loop waits asynchronously, so the terminal may run for minutes
without freezing the composer, status line, or safe control commands.

The intended foreground experience is:

```text
Waiting for background terminal (6m 11s • Esc to interrupt) · 2 background terminals running · /ps to view · /stop to stop all
  └ uv run pytest tests/unit -q
```

`/ps` opens the live terminal list. `/stop` stops every owned background
terminal by default, or an explicitly named handle when an ID is supplied.
`Esc` is the keyboard equivalent of interrupting the terminal currently owning
the wait. Existing foreground execution remains the default; background
execution must be explicit or selected by a documented workflow/tool policy.

## 2. Evidence-backed problem statement

| Current capability | Limitation | Evidence |
|---|---|---|
| Async subprocess execution | `_run_proc()` waits for `proc.communicate()` and returns only after completion | `src/agenthicc/tools/exec/__init__.py` |
| Process-group safety | Execution already starts a new session and kills the process group on timeout | `src/agenthicc/tools/exec/__init__.py` |
| TUI waiting display | The status component understands approval/question waits, but not terminal waits | `src/agenthicc/tui/workspace/components.py` |
| Busy command policy | Read-only and control commands can run while an agent turn is active | `src/agenthicc/commands/builtins.py`, `src/agenthicc/commands/busy_policy.py`, PRD-143 |
| Background agent sessions | Durable workers, leases, lifecycle state, and a manager TUI already exist | PRD-141, `src/agenthicc/background/` |
| Unified interruption | Foreground and background cancellation are being aligned around one protocol | PRD-148 |

The missing terminal-level control causes four user problems:

1. A test or build can occupy the visible status line without exposing a
   stable object that the user can inspect or stop.
2. Multiple concurrent terminal calls are indistinguishable; the user cannot
   tell whether one, two, or ten processes are consuming resources.
3. Pressing `Esc` has no precise terminal target when a tool call is waiting.
4. A terminal process that outlives a cancelled turn can become orphaned or
   continue making side effects without a visible owner.

## 3. Terminology and scope

### 3.1 Background terminal

A background terminal is an agenthicc-owned subprocess or process group
started through the canonical execution tools. It is not a shell command with
an untracked trailing `&`, a user-owned process discovered by PID scanning, or
a second agent session.

Each terminal has:

- a stable `terminal_id` for its lifetime;
- one resolved workspace and originating session ID;
- one process group owned by the terminal manager;
- a bounded output stream and durable activity summary;
- one lifecycle record and one cancellation owner; and
- zero or more foreground waiters, with at most one active wait per tool call.

### 3.2 Waiting for a background terminal

Waiting is a non-blocking TUI state in which an agent turn or explicit wait
operation is awaiting a terminal's completion. The subprocess continues under
the terminal manager while the TUI event loop can process safe commands,
resize events, output refreshes, and the interrupt key.

Starting a terminal and waiting for it are separate concepts:

- starting returns a handle when background mode is requested;
- waiting consumes or follows the handle until completion, interruption, or
  a caller timeout; and
- observing with `/ps` never attaches a second waiter or changes process
  ownership.

### 3.3 In scope

- Background mode for `run_bash` and `run_command`, with the same capability,
  workspace, network, approval, and environment policy as foreground mode.
- A shared terminal manager and bounded terminal registry.
- Live wait status and elapsed-time rendering in the existing TUI.
- `/ps`, `/stop`, and `Esc` control semantics.
- Multiple concurrently running terminals with deterministic limits.
- Streaming/tail output, completion records, cancellation, timeout, and
  orphan recovery.
- Integration with PRD-141 background sessions and PRD-148 cancellation.

### 3.4 Out of scope

- A general-purpose terminal multiplexer or interactive PTY replacement.
- Attaching to arbitrary existing system processes or discovering processes
  by scanning the host process table.
- A web dashboard, remote terminal service, hosted logs, or telemetry.
- Automatic backgrounding of every command.
- Rolling back filesystem, Git, network, MCP, email, or other side effects
  already accepted by a process.
- A second shell, tool executor, workflow runner, or agent loop.

## 4. Goals and non-goals

### 4.1 Goals

- Make long-running terminal work observable and controllable without
  freezing the foreground TUI.
- Give every owned terminal an unambiguous handle, state, elapsed duration,
  command label, and originating session.
- Make `/ps` and `/stop` immediate commands while an agent or terminal wait is
  active, without forwarding them to the model.
- Make `Esc` interrupt the terminal currently owning the foreground wait,
  while preserving normal Esc behaviour in idle mode and unrelated overlays.
- Preserve the existing synchronous foreground command contract unless the
  caller explicitly requests background mode.
- Reuse the existing subprocess process-group and graceful-cancellation
  boundaries, including PRD-148's idempotent request semantics.
- Bound concurrent terminals, output, runtime, disk retention, and cleanup
  work.
- Recover stale terminal records after a process or manager restart without
  claiming that an unfinished command succeeded.

### 4.2 Non-goals

- Making arbitrary command side effects safe or reversible.
- Bypassing tool capability gates, approvals, `WorkspaceView`, `NetworkGuard`,
  plugin trust, or configured timeouts.
- Treating a background terminal as a detached agent session. Long-lived agent
  work continues to use PRD-141's background session worker and manager.
- Keeping a process alive forever after its owning session is deleted or its
  security policy is no longer available.

## 5. User-facing contract

### 5.1 Starting a terminal

The current `run_bash` and `run_command` tools retain foreground behaviour by
default. Their background-capable request contract should add only explicit,
backward-compatible fields:

```json
{
  "command": "uv run pytest tests/unit -q",
  "background": true,
  "label": "unit tests",
  "timeout": 1200
}
```

For `run_command`, the equivalent request uses `argv` instead of `command`.
The tool returns a structured handle rather than pretending the command has
completed:

```json
{
  "ok": true,
  "background": true,
  "terminal_id": "term-7f3a",
  "state": "running",
  "pid": 12345,
  "label": "unit tests"
}
```

The model or workflow may then issue the canonical wait/follow operation. A
wait returns the same normalized stdout/stderr/return-code shape as a
foreground call, plus `terminal_id`, elapsed time, truncation, and the final
terminal state. A caller may also leave a handle running and continue other
work, subject to the concurrency and session ownership rules.

Background mode must never be inferred solely from an untrusted command string
containing `&`, `nohup`, `disown`, or shell-specific detachment syntax. Such
syntax remains part of the command's own semantics and does not transfer
ownership to the terminal manager.

### 5.2 Foreground wait status

While the active turn is waiting for one or more background terminals, the
status component shows a compact, width-safe message in the existing Live
region:

```text
Waiting for background terminal (6m 11s • Esc to interrupt) · 2 background terminals running · /ps to view · /stop to stop all
  └ uv run pytest tests/unit -q
```

Requirements for the display:

- elapsed time is monotonic and pauses only when the terminal is not running;
- the running count includes only owned terminals visible to the current
  session/user and excludes completed records;
- the displayed command is a redacted, bounded label, never a raw secret or
  unbounded argument string;
- narrow terminals use the existing `fit()`/rendering contract and retain the
  action hints in a shortened form;
- a terminal completion replaces the wait state with a bounded result summary;
- multiple waited terminals show the selected/current command and a count,
  while `/ps` exposes the full list.

The exact punctuation may be localized or adjusted for width, but the meaning
of “waiting”, elapsed time, interrupt action, running count, `/ps`, and `/stop`
is stable.

### 5.3 `/ps` — process list and terminal manager

`/ps` is a local, immediate command. With no arguments it opens an overlay for
owned terminals in the current session and project. It must also be usable
while an agent turn is active or a foreground wait is displayed.

The overlay contains:

| Field | Requirement |
|---|---|
| Handle | Short `terminal_id`, copyable and stable |
| State | `starting`, `running`, `stopping`, `exited`, `failed`, `stopped`, or `orphaned` |
| Age/duration | Creation time and active elapsed duration |
| Command | Redacted bounded label/command preview |
| Directory | Workspace-relative path where possible |
| Exit | Exit code, signal, timeout, or cancellation reason when terminal |
| Output | Latest bounded output line and unread-output marker |

`/ps <terminal-id>` opens the selected terminal's details and output tail.
`/ps --all` may include terminal records from other non-archived sessions only
when the current user and project policy permits it. The default must remain
scoped to prevent accidental disclosure of another project's command output.
`/ps --json` provides redacted, stable machine-readable records without ANSI
control sequences.

The overlay must preserve selection, filters, scroll position, and output
position across refreshes. Refreshing never restarts, duplicates, or attaches
another process to a terminal.

### 5.4 `/stop` — stop and close a terminal

`/stop` is an immediate control command:

```text
/stop                 # stop every visible owned terminal; no ID required
/stop term-7f3a       # stop one exact terminal
/stop all             # request stopping all visible terminals, with confirm
/stop --force term-7f3a
```

Without arguments, `/stop` always requests stopping every visible owned
terminal, regardless of whether a wait is active or a terminal is selected.
`/stop all` is the explicit spelling and retains its confirmation requirement;
`/stop <terminal-id>` remains the single-terminal form. It must not guess
ownership based on command text.

Stopping is a two-stage operation:

1. publish one idempotent cancellation request and attempt graceful
   interruption (`SIGINT`/`CTRL_BREAK` where supported); and
2. after the configured grace period, terminate only the owned process group
   if it has not exited. `--force` skips the graceful interval but still
   targets only the validated owned group.

The UI shows `stopping` until process cleanup and record persistence are
acknowledged. `/stop` never reports success merely because a cancellation
request was queued. Repeated requests are coalesced.

### 5.5 Esc and terminal ownership

When no overlay owns Esc and the foreground is waiting for a terminal, Esc
means “interrupt the current terminal wait and stop its terminal.” It follows
the PRD-148 cancellation owner and returns the foreground turn to a usable
state after bounded cleanup.

When an approval, question, command picker, `/ps` overlay, or other modal owns
Esc, that overlay keeps its existing close/back behaviour. When the session is
idle and no terminal is selected, Esc retains its current input behaviour.

If the current wait has multiple terminals, Esc targets the terminal shown in
the wait detail. The status line must make the target clear through the command
label and `/ps` details.

## 6. Terminal lifecycle and data contract

### 6.1 States and transitions

The terminal registry uses one explicit lifecycle vocabulary:

| State | Meaning | Legal actions |
|---|---|---|
| `starting` | Request accepted; process group is being created | stop, inspect |
| `running` | Process group is alive and output may arrive | wait, inspect, stop |
| `stopping` | Interrupt or termination requested; cleanup pending | inspect |
| `exited` | Process exited with a known return code | inspect, collect output |
| `failed` | Spawn, stream, or registry failure prevented a normal result | inspect, retry explicitly |
| `stopped` | User cancellation or timeout completed cleanup | inspect, retry explicitly |
| `orphaned` | Owner/lease disappeared and process status cannot be trusted | inspect, recover/stop explicitly |

Legal transitions are enforced by the terminal service and durable events,
not by TUI conditionals:

```text
starting → running → exited
starting → failed
running → stopping → stopped
running → failed
running → orphaned
starting/running → stopping → failed   (cleanup failure)
```

An exited process is never changed to successful merely because its wait task
was cancelled. A wait cancellation and a process stop are recorded separately
so a user can distinguish “I stopped waiting” from “the command was stopped”.

### 6.2 Required record

Each terminal record contains:

- `terminal_id`, `session_id`, `tool_call_id`, and parent background-job ID
  when the originating session is managed by PRD-141;
- normalized command kind (`bash` or `exec`), redacted label, and a bounded
  command preview;
- exact resolved workspace path, project identity, and security-policy
  version/decision without secrets;
- PID/process-group identity, worker lease, host/platform, and creation/start
  timestamps;
- monotonic active duration, last output timestamp, and last heartbeat;
- lifecycle state, exit code/signal, timeout, stop reason, and cleanup result;
- bounded stdout/stderr tail, total byte counters, truncation marker, and a
  durable output reference when configured; and
- waiter count, current waiter, and whether the handle is still attached to a
  foreground turn.

The registry is a derived control index. The canonical session journal remains
the source of truth for agent/workflow events, while terminal activity is
linked into that journal by terminal and tool-call IDs.

### 6.3 Multiple terminals and limits

The manager enforces configurable limits before spawning a process:

- maximum running background terminals per session;
- maximum running terminals per project and per user;
- maximum total terminal records retained in the live index;
- maximum output bytes per terminal and aggregate output bytes per session;
- per-terminal wall-clock and idle-output timeout; and
- graceful-stop interval.

Defaults must be conservative and documented. A rejected spawn returns a
structured, actionable error and does not leave a phantom `starting` record.

## 7. Architecture and ownership boundaries

The following boundaries are normative:

| Concern | Owner |
|---|---|
| Command validation, `WorkspaceView`, capability, and approval checks | Existing `tools/exec/`, `tools/sandbox.py`, `security.py`, and tool hooks |
| Process-group spawn, output pumps, handles, leases, and cleanup | One canonical terminal manager under `background/`, consumed by `tools/exec/` |
| Background-session lifecycle and agent workers | Existing `background/BackgroundSupervisor` and PRD-141; terminal records are child resources, not agent sessions |
| Wait/cancel coordination | PRD-148 interrupt owner and the existing runner/task orchestration |
| Domain events and durable ordering | Existing kernel event/processor boundary or the terminal manager's versioned local event adapter; no direct frozen-state mutation |
| Reactive wait fields and counters | `tui/conversation_store.py` |
| Status/footer rendering | `tui/workspace/components.py` and existing width-safe rendering helpers |
| `/ps` and `/stop` commands | `commands/builtins.py`, canonical command registry, busy-policy classifier, and existing overlays |
| Terminal keys and platform differences | `tui/input/capabilities.py`, `tui/triggers/`, `tui/terminal/`, and `cbreak_reader.py` |
| Durable terminal output and retention | Existing session/background storage policy, documented in `docs/reference/storage.md` |

The implementation must not create another model loop, tool executor, shell
runner, TUI input loop, or process-table scanner. The existing
`asyncio.create_subprocess_*` boundary and process-group cleanup should be
extracted or adapted rather than duplicated.

The event loop must never call blocking `communicate()`, `wait()`, or file I/O
on the UI thread. Output pumps must apply backpressure and preserve line/order
metadata sufficiently for the tail view without retaining unbounded output.

## 8. Security, privacy, and reliability

- Background execution inherits the originating tool's capability decision;
  moving a command into the background cannot bypass approval or make a denied
  network/filesystem/execute operation allowed.
- `cwd` is resolved through `WorkspaceView`; broad or ambiguous destructive
  paths are rejected before spawn.
- Environment inheritance follows the existing execution policy. API keys,
  OAuth tokens, plugin secrets, and secret-shaped arguments are redacted from
  labels, status lines, JSON, and persisted activity.
- Shell commands remain subject to the same shell/tool trust boundary as
  foreground commands. Background mode is not a sandbox escape.
- Stop and cleanup operate only on a process group recorded by agenthicc and
  protected by a lease/ownership check; arbitrary PIDs are never accepted.
- A process that survives the grace period is force-terminated only within its
  owned group. If the platform cannot guarantee this, the record becomes
  `orphaned` and the user receives an actionable diagnostic.
- Output is bounded, truncated with an explicit marker, and treated as
  untrusted text before Rich rendering or JSON serialization.
- Crash/restart recovery marks uncertain processes `orphaned`; it never claims
  success from a missing final event and never silently relaunches a command.
- Deleting the parent agent session requests terminal cleanup first. Terminal
  records and output follow the existing recoverable-trash and retention
  policy; no broad recursive path is inferred from a project root.
- No command, output, lifecycle event, or terminal metadata is sent to an
  external service by this feature.

## 9. Rollout and migration

### Phase 1 — Terminal resource contract

- Define typed terminal request/record/events and a single manager.
- Extract the existing subprocess process-group and timeout logic behind it.
- Keep `background=false` as the default and preserve the current foreground
  result envelope.
- Add focused unit tests for spawn, output bounds, exit, timeout, stop, and
  process-group cleanup.

### Phase 2 — Explicit background tools and wait coordination

- Add the opt-in `background` request field and structured terminal handles.
- Add the wait/follow operation and connect it to PRD-148 cancellation.
- Ensure concurrent waits and duplicate stop requests are deterministic.
- Add integration tests with real short-lived subprocesses and a fake clock.

### Phase 3 — TUI status, `/ps`, `/stop`, and persistence

- Add reactive terminal state and the exact responsive wait status contract.
- Add immediate `/ps` and `/stop` command records, completion discovery, and
  output-tail inspection.
- Add resize/non-interactive rendering coverage and platform backend tests.
- Persist and recover terminal records using the PRD-141 local registry and
  document retention/configuration.

### Phase 4 — Workflow and background-session integration

- Permit workflows to declare a terminal wait policy without hard-coding a
  shell command.
- Link child terminals to PRD-141 background sessions and ensure cancelling,
  retrying, resuming, archiving, or deleting a session follows terminal
  ownership rules.
- Enable the feature by default only after the process-tree and restart tests
  pass on every supported terminal backend.

Existing foreground callers require no migration. Existing command schemas
remain valid because background mode is opt-in and new response fields are
additive.

## 10. Acceptance criteria

The PRD is complete only when all of the following are true:

1. A foreground `run_bash`/`run_command` call behaves exactly as it does today
   unless background mode is explicitly requested.
2. An explicit background request returns a stable terminal handle, state,
   label, and process identity without claiming completion.
3. A canonical wait/follow operation returns the existing normalized result
   shape plus terminal state, elapsed time, terminal ID, and truncation data.
4. The TUI remains responsive while a terminal runs for at least ten minutes;
   resize, safe input, `/ps`, and `/stop` are processed without blocking on
   subprocess I/O.
5. The wait status renders the required meaning and includes elapsed time,
   current command, running-terminal count, `/ps`, `/stop`, and Esc guidance.
6. Two or more concurrent terminals are independently listed, followed,
   stopped, and completed without output or state cross-contamination.
7. `/ps` is discoverable, immediate during an active run, scoped by default,
   supports detail/tail inspection, and has stable redacted JSON output.
8. `/stop`, `/stop <terminal-id>`, and Esc target only the intended owned
   terminal; repeated requests are idempotent and cleanup reaches a terminal
   state or an explicit `orphaned` diagnostic.
9. A graceful interrupt is attempted before force termination, and only the
   recorded process group is terminated.
10. Timeout, spawn failure, non-zero exit, signal exit, output truncation,
    manager restart, and orphan recovery have distinct tested outcomes.
11. Terminal output and metadata obey workspace, capability, approval,
    redaction, retention, and resource limits in both interactive and
    non-interactive paths.
12. Cancelling a foreground wait does not mark an already completed command as
    stopped, does not duplicate tool side effects, and leaves the agent turn
    and queued-message state consistent with PRD-148.
13. Deleting or cancelling a PRD-141 background session cannot leave an owned
    child terminal running without an auditable owner.
14. The default TUI and headless paths remain usable when background terminals
    are disabled, unavailable on a platform, or have no active records.
15. Documentation covers request schemas, `/ps`, `/stop`, Esc, status output,
    limits, security, retention, and troubleshooting.

## 11. Verification plan

Required focused checks include:

```bash
uv run pytest tests/unit/test_exec_tools.py -q
uv run pytest tests/unit/test_background*.py tests/unit/test_*terminal* -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
```

The implementation must add coverage for:

- typed lifecycle transitions and duplicate control requests;
- short-lived, long-lived, failing, timed-out, and output-heavy subprocesses;
- process-group cleanup including child processes;
- multiple concurrent terminals and per-session/project limits;
- wait rendering at narrow widths and elapsed-time updates;
- `/ps` and `/stop` classification while an agent turn is active;
- Esc ownership with and without overlays;
- redaction, workspace validation, disabled-feature behaviour, and
  non-interactive JSON output; and
- restart/orphan recovery and PRD-141 session deletion/cancellation.

## 12. Risks and decisions

| Risk | Mitigation |
|---|---|
| A cancelled wait leaves a process running | Make terminal ownership explicit; stop through the PRD-148 cancellation owner and show `stopping`/`orphaned` until resolved |
| A process group contains unrelated descendants | Spawn in a dedicated group, validate ownership, and refuse unsafe force cleanup |
| Output overwhelms the TUI or disk | Ring-buffer/tail limits, aggregate quotas, truncation markers, and retention policy |
| `/ps` leaks another project's command or secrets | Default project/session scope, capability-aware access, redaction before display and JSON |
| Background mode changes existing tool behaviour | Keep it opt-in, version schemas additively, and test foreground parity |
| Multiple systems become competing process registries | Make one terminal manager authoritative and link it to, rather than duplicate, PRD-141 session records |
| Windows and POSIX process semantics diverge | Isolate process-group operations behind the existing terminal portability boundary and test both supported paths |

Open implementation choices such as the exact on-disk terminal event format,
short-handle encoding, and whether the terminal manager lives as a child
resource in `BackgroundStore` or an adjacent local store may be resolved during
design. They must not change the lifecycle, ownership, security, or user-facing
contracts in this PRD.

## 13. Implementation evidence

Implemented in the current runtime with an adjacent, namespaced terminal
registry under `background/terminals.py`. The execution tools expose explicit
background handles and `wait_terminal`; `TUISession` binds the manager and
workflow terminal policies; `/ps`, `/stop`, and Esc use the existing immediate
command/input lanes; detached-session cancellation calls the exact child-group
cleanup bridge. User-facing behavior and storage are documented in
`README.md`, `docs/guides/background-sessions.md`,
`docs/guides/architecture.md`, and `docs/reference/storage.md`.

Verification evidence:

- `tests/unit/test_background_terminals.py` — lifecycle, output, limits,
  persistence, restart, policies, and concurrent waits;
- `tests/integration/test_background_terminals_integration.py` — command and
  overlay controls plus wait rendering;
- `tests/e2e/test_background_terminals_e2e.py` — real subprocess and process
  group scenarios;
- full unit suite: 2,191 passed, 14 skipped;
- integration suite: 108 passed; E2E suite: 69 passed, 1 skipped;
- Ruff, mypy, and the type-safety audit pass.
