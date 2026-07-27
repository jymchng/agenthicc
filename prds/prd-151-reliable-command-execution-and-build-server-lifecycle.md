---
title: "PRD-151: Reliable Command Execution and Build/Development-Server Lifecycle"
status: Implemented
version: 1.0.0
created: 2026-07-27
related_prds:
  - PRD-138  # Repository Improvement Roadmap
  - PRD-141  # Background Sessions and Session Manager TUI
  - PRD-143  # Safe Commands During Active Runs
  - PRD-148  # Unified Interrupt and Graceful Cancellation
  - PRD-149  # Background Terminals and Responsive Wait Control
supersedes: []
tags:
  - command-execution
  - subprocesses
  - npm
  - nextjs
  - workflows
  - background
  - reliability
  - observability
---

# PRD-151 — Reliable Command Execution and Build/Development-Server Lifecycle

Study date: 2026-07-27. This PRD records the investigation into commands such
as `npm run dev`, `next build`, and other long-running package-manager tasks.
It extends PRD-149's owned-terminal implementation; it does not introduce a
second shell runner, terminal registry, workflow engine, or process manager.

## 1. Executive summary

Agenthicc can launch `run_bash` and `run_command`, but the current contract is
not strong enough for real project development commands. A command can fail,
be killed by a timeout, or be cancelled while the UI still reports a generic
successful completion. A development server can also be correctly kept alive
only by backgrounding it, while the current foreground contract waits for a
process that is intentionally never going to exit.

The corrective feature is a canonical command-execution lifecycle with four
properties:

1. Every command result has an authoritative outcome. A non-zero exit,
   timeout, cancellation, spawn failure, and successful exit cannot be
   confused by the TUI, the model, or a workflow.
2. Every deadline has an explicit owner and unit. The tool-level timeout is in
   seconds, including fractional seconds; outer tool, turn, session, and
   background deadlines are visible and cannot silently override a larger
   command timeout without reporting which deadline fired.
3. One-shot commands and services have different lifecycles. `next build`
   remains a finite command whose success means exit code zero. `npm run dev`
   is started as an owned background terminal and reaches a separate
   readiness milestone; it is stopped explicitly or during owner cleanup.
4. Workflows cannot advance or claim completion after an unsuccessful command.
   A workflow can retain a background handle, wait for a terminal result, or
   wait for a readiness probe, with all state linked to the existing phase and
   session journal.

## 2. Investigation and evidence

### 2.1 Incident evidence

The supplied incident transcript contains:

- a displayed call resembling `Run(command='cd website && npx next b,
  timeout=300)`, with the command text visibly truncated or malformed;
- a generic `Completed` result after approximately 6.3 seconds;
- a follow-up listing of `website/.next` showing only a small, partial build
  tree; and
- a later workflow status still at `build_pages`.

The transcript does not include the command's actual `returncode`, `stderr`,
`timed_out`, process signal, exact command string, or final workflow result.
Therefore it is not possible to prove from the transcript alone whether the
specific invocation was malformed (`next b` rather than `next build`), failed
inside Next.js, was cancelled externally, or was still running elsewhere.
The absence of those fields is itself a product defect: the UI should make the
cause unambiguous.

The transcript also shows a process inspection and cgroup memory inspection.
Those checks do not establish an out-of-memory kill: the recorded cgroup output
says there was no cgroup limit, and the process listing does not include a
kernel exit reason. The implementation must collect a structured termination
reason instead of inferring one from a partial artifact directory.

### 2.2 Current implementation findings

| Finding | Current evidence | User impact |
|---|---|---|
| Timeout unit is seconds | `src/agenthicc/tools/exec/__init__.py:61-117` passes `timeout` directly to `asyncio.wait_for`; tool docs describe “Maximum seconds” | `timeout=300` means 300 seconds, not milliseconds or minutes. `duration_ms` is separate telemetry. |
| Foreground command failures are not authoritative failures | `_run_proc()` returns a plain mapping with `returncode` and `timed_out`, but no `ok` field | A non-zero `next build` can be rendered as a successful tool completion. |
| Normalization accepts failed process mappings as success | `src/agenthicc/tools/executor.py:94-135` only turns a mapping into failure when it contains `ok: false` | The adapter reproduction returned `ok=True` for a process with `returncode=7`. |
| Timeout can be reported as success on the same path | A timed-out foreground mapping has `returncode=-1` and `timed_out=true`, still without `ok:false` | The model may continue as if a build completed. |
| Tool-level timeout and executor-level timeout are separate | `AgenthiccToolExecutor` defaults to 30 seconds (`executor.py:140-154`) and wraps calls with `asyncio.wait_for` (`executor.py:483-490`) | A caller-requested `timeout=300` can still be cancelled by a 30-second adapter deadline when that execution path is used. |
| Outer cancellation has no guaranteed subprocess cleanup | `_run_proc()` handles `asyncio.TimeoutError` but has no cancellation cleanup branch around `proc.communicate()` | A turn or executor cancellation can leave a child process group alive or discard its partial output. |
| Timeout cleanup is abrupt and loses output | On timeout, the current path sends signal 9 and replaces captured output with an empty stdout plus a generic stderr line | Build diagnostics needed to fix the failure can be lost. |
| Foreground and background timeout semantics differ | Foreground always calls `wait_for(..., timeout=timeout)`; background treats `timeout=0` as no wall timeout (`terminals.py:652-657`) | The same value has inconsistent meaning depending on execution mode. |
| Background terminals are opt-in | PRD-149 and `PhaseSpec.terminal_wait_policy` support `background`, and `TerminalManager` returns a handle | A server invoked as an ordinary foreground call waits forever or reaches a deadline; the tool contract does not guide the model strongly enough about service lifecycle. |
| Background wait timeout is destructive | `TerminalManager.wait(..., timeout=...)` calls `stop()` when the wait timeout expires | A caller that only wanted to stop observing can unintentionally kill a dev server. |
| Agent-facing wrappers omit supported execution fields | The low-level `Tool` accepts `cwd` and `env`, but `tools/exec/agent_tools.py` wrappers expose only command/argv, timeout, background, and label | Agents resort to `cd ... && ...`, making exact cwd and environment diagnostics harder. |
| Shell naming is ambiguous | `run_bash` uses `asyncio.create_subprocess_shell`, whose default shell is platform-dependent; it does not explicitly request Bash | Bash-specific scripts can behave differently from the tool name users read. |
| Workflow policy changes launch mode but not completion semantics | `PhaseSpec.terminal_wait_policy` is applied in `workflows/default/runner.py:375-394`; no shared command outcome gate is defined | A workflow can launch a background command without a durable, typed requirement to collect its result or readiness state before phase completion. |

### 2.3 Reproduction performed during investigation

The low-level tool returned this shape for a command that exited with code 7:

```text
{'stdout': 'bad\n', 'stderr': '', 'returncode': 7,
 'duration_ms': 380.8, 'timed_out': False}
```

When that result was dispatched through `AgenthiccToolExecutor`, the
completion envelope was:

```text
ok=True, value={..., 'returncode': 7, 'timed_out': False}
```

The same executor, configured with a 50 ms outer deadline, cancelled a command
that requested a 2 second tool timeout. This proves the deadline layering is
real even though the exact default path used by every agent surface must be
measured separately during implementation.

The current repository has Node, npm, and npx installed, but it has no
`package.json` or Next.js website fixture. A genuine `next build` smoke test
therefore belongs in a dedicated fixture or an integration repository; this
PRD does not claim to have reproduced the supplied website build locally.

## 3. Problem statement

### 3.1 One-shot commands

Builds, tests, package installs, migrations, and generators are finite
commands. A successful one-shot command must mean:

```text
process spawned
  → process group remained owned
  → process exited normally
  → returncode == 0
  → no command/turn/session deadline fired
  → result and output were captured
```

The current implementation treats “the Python coroutine returned a mapping”
as success, even when the mapping says that the child returned a non-zero code
or was killed. This is the primary explanation for a generic “Completed” label
next to incomplete build artifacts.

### 3.2 Long-lived development servers

`npm run dev`, `next dev`, Vite, Storybook, and similar commands are services,
not one-shot tasks. Their correct successful startup state is not process exit;
it is an owned process with a verified readiness signal. Waiting for the
process to exit is therefore the wrong default interaction:

```text
start service → observe readiness → return handle/URL → keep ownership
                                      ↓
                         inspect, follow logs, stop, or restart
```

The service must not be hidden behind an untracked shell `&`, because that
loses process ownership, output, cleanup, and workflow linkage.

### 3.3 Workflows

A workflow phase must not advance because a tool call was syntactically
accepted. It must receive a typed outcome and apply the phase's declared
completion rule:

- a finite build requires `exited` and `returncode == 0`;
- a service phase requires `ready` and retains the terminal handle; and
- a failed, timed-out, cancelled, rejected, or orphaned command routes to the
  existing error/retry/approval path rather than silently continuing.

## 4. Goals and non-goals

### 4.1 Goals

- Make success/failure semantics correct at the low-level tool, Lauren tool,
  Agenthicc executor, TUI, headless, and workflow boundaries.
- Document and enforce timeout units and deadline precedence.
- Clean up the exact process group on timeout, cancellation, turn failure, and
  session shutdown, including descendants where the platform guarantees it.
- Preserve useful stdout/stderr tails and structured diagnostics after failure.
- Make `npm run dev` and equivalent services first-class owned background
  terminals with optional readiness probes.
- Make `next build` and equivalent finite commands reliable without requiring
  a server-style background workaround.
- Expose `cwd` and controlled environment fields consistently through the
  agent-facing wrappers.
- Ensure workflow phases cannot claim completion after a failed command.
- Reuse PRD-149's `TerminalManager`, `/ps`, `/stop`, session ownership, and
  storage boundaries.
- Provide deterministic tests for non-zero exits, timeouts, cancellation,
  partial output, process trees, readiness, and workflow transitions.

### 4.2 Non-goals

- Automatically making arbitrary shell side effects reversible.
- Running arbitrary existing host PIDs or adopting processes that agenthicc
  did not create.
- Replacing `npm`, `npx`, Next.js, package managers, or project build tools.
- Inferring success from the existence of `.next`, `dist`, or another output
  directory without a successful process outcome.
- Silently detaching every command. Explicit background/service intent remains
  required unless a future, separately approved policy enables safe inference.
- Creating a second workflow runner, tool executor, TUI event loop, or durable
  session store.

## 5. Product contract

### 5.1 Timeout units and semantics

The public `timeout` parameter for `run_bash`, `run_command`, `shell`, and
`wait_terminal` is measured in seconds. Decimal values are allowed, so
`0.5` means 500 milliseconds. `duration_ms` is output telemetry only and is
never an input timeout unit.

The implementation must make these rules explicit:

| Value | Meaning |
|---|---|
| omitted | Use the configured command default, currently 30 seconds for `run_bash`/`run_command` |
| positive number | Maximum wall-clock seconds for the named operation |
| `0` | No deadline for that operation, subject to the enclosing turn/session shutdown policy; this meaning must be identical in foreground and background modes |
| negative, NaN, or infinity | Reject before spawn with a structured validation error |

`wait_terminal(timeout=...)` must distinguish “stop waiting after N seconds”
from “stop the process after N seconds.” The preferred contract is:

- `wait_terminal` waits for process exit and, on an observer timeout, returns a
  non-terminal `waiting` snapshot without killing the process; and
- an explicit stop operation or `/stop` requests process cancellation.

If backward compatibility requires retaining destructive wait timeouts for one
release, the response must say `stop_requested=true` and the migration must
be documented. New callers must not rely on an ambiguous timeout.

### 5.2 Canonical foreground result

All foreground execution paths return a versioned, JSON-safe mapping with
backward-compatible output fields:

```json
{
  "ok": true,
  "state": "exited",
  "command_kind": "shell",
  "returncode": 0,
  "stdout": "...",
  "stderr": "",
  "duration_ms": 8123.4,
  "timed_out": false,
  "cancelled": false,
  "truncated": false,
  "termination_reason": null,
  "deadline": null
}
```

Required failure states include:

| State | `ok` | Meaning |
|---|---:|---|
| `exited` | `returncode == 0` | Process exited successfully |
| `failed` | false | Process exited non-zero or output/registry handling failed |
| `timed_out` | false | A command deadline fired and cleanup completed |
| `cancelled` | false | User/turn/session cancellation stopped the command |
| `spawn_failed` | false | Executable, cwd, permission, or environment setup failed |
| `orphaned` | false | Ownership or cleanup could not be proven after interruption |
| `rejected` | false | Capability, workspace, limit, or policy rejected spawn |
| `running` | false/handle-only | A background service or terminal is still owned |

`ok` is derived by the execution service, never by whether a coroutine
returned. The invariant is:

```text
ok == (state == "exited" and returncode == 0 and not timed_out and not cancelled)
```

`normalize_result()` must recognize legacy process-shaped mappings so existing
tools cannot accidentally turn a non-zero return code into a successful
`ToolResult`. New code should return a typed internal outcome and serialize it
only at the boundary.

### 5.3 Deadline hierarchy

Every execution record reports the deadline that ended it:

```text
command timeout (requested by tool)
  ≤ configured tool ceiling
  ≤ enclosing agent-turn deadline, when present
  ≤ background-session wall timeout, when present
  ≤ process/session shutdown deadline
```

The exact precedence must be represented as a computed effective deadline,
not as nested silent `wait_for()` calls. If a caller requests 300 seconds and
the configured ceiling is 30 seconds, the tool result must explicitly say
`deadline="tool_ceiling"`, include both requested and effective values, and
show the configured policy. It must never claim that the requested 300-second
operation was allowed to run for 300 seconds.

The implementation must decide and document whether a command timeout can
extend an enclosing turn timeout. The safe default is no: a process cannot
outlive its owner unless it was explicitly transferred to an owned background
terminal. In that case, the turn returns a handle and the terminal's lifecycle
continues under PRD-149.

### 5.4 Explicit command modes

`run_bash` and `run_command` retain foreground mode as the default for
backward compatibility. They support these explicit modes:

```json
{
  "background": false,
  "lifecycle": "oneshot"
}
```

```json
{
  "background": true,
  "lifecycle": "service",
  "label": "website dev server",
  "ready": {
    "url": "http://127.0.0.1:3000",
    "timeout": 30
  }
}
```

`lifecycle=oneshot` requires a terminal result before a workflow can use the
command as completed. `lifecycle=service` returns an owned terminal handle;
the optional readiness operation can report `ready` while the process remains
running. A service that has not become ready before the readiness deadline is
`failed` or `starting_timeout` according to the explicit policy, while the
user may choose whether to keep or stop the process. A readiness timeout must
not silently mean process termination.

The exact JSON field names may be adjusted during implementation, but the
distinction between process exit and service readiness is mandatory.

### 5.5 Agent-facing execution fields

The public wrappers must expose the same supported fields as the low-level
tools:

- `cwd`, resolved and workspace-validated before spawn;
- `env`, a string-to-string overlay with secret-safe display and persistence;
- `timeout`, in seconds;
- `background`, `lifecycle`, and bounded `label`; and
- an optional readiness specification for service mode.

The shell tool must either explicitly invoke Bash or be renamed/documented as
platform shell. The chosen behavior must be tested on supported platforms.
Commands and argv must be preserved exactly in structured diagnostics; UI
preview truncation must never mutate the command that was executed.

## 6. Service lifecycle for `npm run dev` and similar commands

### 6.1 Start and readiness

The intended journey is:

1. The model or workflow declares service intent and calls
   `run_bash(command="npm run dev", background=true, lifecycle="service")`.
2. Agenthicc starts the command in the existing owned `TerminalManager` and
   returns a stable handle immediately.
3. The tool optionally follows output until a configured readiness condition
   succeeds: HTTP status, TCP connect, or an explicit output marker. Network
   probes remain subject to `NetworkGuard`; probes default to loopback only.
4. The result says `running` plus `ready=true` and includes the redacted URL or
   readiness evidence. It does not say the process exited.
5. `/ps`, terminal inspection, and workflow state retain the handle. `/stop`
   or session cleanup terminates the exact owned process group.

No readiness heuristic may treat any output line containing “started” as
authoritative without recording the matched rule. When no readiness probe is
provided, the result is `running` with `ready=unknown`, not `ready=true`.

### 6.2 Service control

The existing `/ps` and `/stop` commands remain the user-facing controls. The
agent tool surface should add or standardize structured equivalents for:

- inspecting one terminal's state and bounded output tail;
- waiting for exit without killing on observer timeout;
- waiting for readiness without changing process ownership; and
- stopping one exact owned terminal.

Restart is an explicit new lifecycle request. It must not start a duplicate
server while the old handle is still `running`, `stopping`, or `orphaned`.

### 6.3 Build lifecycle for `next build`

The intended journey is different:

1. The model calls a finite command with the exact working directory and a
   timeout large enough for the project, for example
   `run_bash(command="npx next build", cwd="website", timeout=300)`.
2. Agenthicc streams or tails bounded stdout/stderr while preserving the
   process group.
3. A return code of zero produces `state=exited`, `ok=true`.
4. Any non-zero code, signal, timeout, cancellation, or spawn error produces
   `ok=false` and a diagnostic containing the exact reason and output tail.
5. The workflow advances only on the successful outcome. The existence of
   `website/.next` is an optional postcondition, never a substitute for the
   process result.

If the command is expected to exceed the enclosing turn deadline, the model
must either request a supported longer policy or use an owned background
terminal and wait/follow it. The tool must report a policy conflict instead of
silently killing the process and returning “Completed.”

## 7. Workflow integration

### 7.1 Phase declarations

Reuse `PhaseSpec.terminal_wait_policy` as the phase-level default, adding only
the minimum typed lifecycle metadata needed for correctness. A phase may
declare:

```python
PhaseSpec(
    name="build_pages",
    terminal_wait_policy="foreground",
    command_lifecycle="oneshot",
    require_successful_commands=True,
)
```

or a service phase:

```python
PhaseSpec(
    name="start_preview",
    terminal_wait_policy="background",
    command_lifecycle="service",
    require_readiness=True,
)
```

Defaults must preserve existing foreground behavior. User-defined workflows
under `.agenthicc/workflows` must validate lifecycle values and reject invalid
combinations before activation.

### 7.2 Completion gate

The workflow runner must collect command outcomes linked to the current
phase/tool call. Before it persists phase output or follows `next`:

- all required one-shot commands must be `ok=true`;
- required services must have a live owned handle and the declared readiness
  result;
- failed/timed-out/cancelled/orphaned commands must route through the phase's
  existing error/retry/approval semantics; and
- a background handle must be persisted in phase metadata without being
  mistaken for a completed command.

The gate must not parse human-readable stdout to decide whether the process
succeeded. It consumes the canonical result fields.

### 7.3 Resume and idempotency

On workflow resume, a previously successful finite command must not be blindly
re-run when its tool-call outcome and idempotency record are durable. A running
service must be reattached only when its ownership lease and process identity
are still valid; otherwise it is `orphaned` and requires explicit recovery.

Retrying a failed build is explicit. Retrying a service start first checks for
an existing owned handle and does not create duplicate servers.

## 8. Architecture and ownership

| Concern | Canonical owner |
|---|---|
| Request validation and tool schemas | `src/agenthicc/tools/exec/` and `tools/exec/agent_tools.py` |
| Typed command outcome and deadline calculation | One execution service shared by foreground and background paths |
| Process groups, output pumps, handles, readiness, and cleanup | Existing `src/agenthicc/background/terminals.py` / `TerminalManager`, extended rather than duplicated |
| Capability, workspace, network, and approval checks | Existing `tools/sandbox.py`, security, capability gate, and tool hooks |
| Agent-facing tool dispatch | Lauren-ai callable convention and existing `AgenthiccToolExecutor` adapter |
| Workflow completion and phase persistence | Existing workflow runner and `WorkflowContext` |
| Session/turn cancellation | PRD-148 cancellation owner and current runner/task orchestration |
| TUI state and operation result rendering | `tui/conversation_store.py`, `tui/workspace/`, and existing appender |
| Durable agent/workflow lifecycle | PRD-141 background session store; terminal records remain child resources |
| Terminal portability | `tui/terminal/`, `cbreak_reader.py`, and a process-control portability helper |

The execution service must be the only owner of process cleanup. The same
cleanup path is used for:

- command deadline;
- outer task cancellation;
- `/stop` and Esc;
- background-session cancellation;
- TUI shutdown; and
- orphan recovery diagnostics.

On POSIX, process groups are created with a fresh session and terminated with a
graceful signal followed by bounded escalation. On Windows, use the supported
job/process-group primitive and document limitations. If ownership cannot be
proven, mark the record `orphaned` and do not kill an arbitrary PID.

Foreground output should be drained incrementally through the shared bounded
output mechanism. It may retain the current final stdout/stderr fields, but
must not lose the diagnostic tail when timeout or cancellation occurs.

## 9. Observability and user experience

### 9.1 Correct result rendering

The operation renderer must map canonical state to explicit labels:

```text
● Run('npx next build')
└─ Completed  8.1s  exit 0

● Run('npx next build')
└─ Failed  8.1s  exit 1
     error: Module not found ...

● Run('npm run dev')
└─ Running  2.4s  term-ab12  ready http://127.0.0.1:3000
```

“Completed” is reserved for `ok=true`. “Running” is never a success claim.
The footer and workflow status must show the operation's terminal ID when a
background handle exists.

### 9.2 Diagnostic record

Every command completion records, subject to redaction and bounded size:

- exact command/argv identity and display-safe preview;
- resolved cwd and project identity;
- effective environment policy, not secret values;
- requested timeout, effective timeout, and deadline owner;
- PID/process-group or platform job identity;
- start/end monotonic and wall timestamps;
- state, return code, signal, cancellation source, and cleanup result;
- bounded stdout/stderr tails and truncation flags; and
- terminal/session/workflow/phase/tool-call IDs.

The user can answer “what happened?” from one result without reconstructing it
from `ps`, `.next`, and a guessed timeout. Redaction applies before Rich
rendering, journaling, persistence, JSON output, and diagnostics.

### 9.3 Command display fidelity

The TUI may shorten long commands for width, but tool-call history and
structured diagnostics must preserve the exact input. A preview must indicate
that it is shortened; it must not turn `next build` into an apparently
different `next b` command. Tests must cover operation rendering at narrow
widths.

## 10. Security and resource policy

- Background/service mode cannot bypass capability gates, approval, workspace
  boundaries, network policy, or plugin trust.
- `cwd` is resolved and validated through the existing workspace boundary.
- Environment overlays are string-only, bounded, and secret-safe. Inherited
  API keys and tokens are never copied into labels or output diagnostics.
- Shell commands remain subject to the existing execute capability and shell
  trust policy. `run_command` remains the preferred no-shell path for exact
  argv.
- Readiness URLs are validated against the network policy and default to local
  addresses; a user must explicitly authorize non-local probes.
- Output has per-command, per-session, and retained-storage bounds.
- A command with no timeout is still stopped when its owning session is closed,
  unless it has been explicitly transferred to a durable background session.
- Process termination targets only a recorded, validated process group/job.
- Resource limits and concurrency limits reject before spawn and leave no
  phantom `starting` records.
- No command, output, environment, or readiness data is sent to an external
  analytics or advertising service.

## 11. Implementation phases

### Phase 1 — Correct result semantics (P0)

- Add a typed internal command outcome and serialize `ok`, `state`, exit code,
  timeout, cancellation, and termination reason consistently.
- Fix `normalize_result()` to classify legacy process mappings by return code,
  timeout, cancellation, and state.
- Make tool completion events and operation rendering use canonical outcome,
  not coroutine return alone.
- Add regression tests proving a non-zero and timed-out command are failures in
  direct, Lauren, and `AgenthiccToolExecutor` paths.

### Phase 2 — Deadline and cancellation correctness (P0)

- Validate timeout values and unify `0` semantics across foreground/background.
- Calculate and record effective deadlines and the owner that fired.
- Make outer cancellation perform bounded process-group cleanup before the
  tool returns or the turn is finalized.
- Reuse graceful interrupt/escalation from PRD-148 and preserve output tails.
- Add process-tree leak tests and cancellation tests on supported platforms.

### Phase 3 — Build and environment diagnostics (P1)

- Expose `cwd` and safe `env` in the agent-facing wrappers.
- Resolve and record executable/shell identity, PATH diagnostics, and exact
  command identity without exposing secrets.
- Add finite-command guidance and a fixture that runs a real Node package
  script. Add an optional pinned Next.js fixture for `next build` when the
  environment has dependencies available; skip with an explicit reason when
  it does not.
- Ensure build failures leave enough output to identify the first actionable
  error and never infer success from an artifact directory.

### Phase 4 — Service lifecycle and readiness (P1)

- Extend PRD-149's `TerminalManager` with service lifecycle and non-destructive
  observation/readiness operations.
- Add loopback HTTP/TCP and explicit output-marker probes with bounded timeouts.
- Add structured inspect, follow, stop, and duplicate-start protection.
- Render `running`, `ready`, `failed`, `stopping`, and `orphaned` distinctly in
  the TUI and headless output.

### Phase 5 — Workflow gates and rollout (P1)

- Add typed phase lifecycle/completion requirements while preserving existing
  `PhaseSpec` defaults.
- Gate `next` transitions on successful finite commands or declared service
  readiness.
- Persist handles/outcomes in workflow phase metadata and preserve resume and
  idempotency rules.
- Update user guides, architecture/storage references, `README.md`,
  `llms.txt`, `llms-full.txt`, and the PRD status with measured evidence.

## 12. Acceptance criteria

The implementation is complete only when all criteria pass:

1. `timeout` is documented and tested as seconds for `run_bash`,
   `run_command`, and `wait_terminal`; fractional values work; invalid values
   fail before spawn; and `duration_ms` is not accepted as a timeout unit.
2. A process exiting with code 1 or 7 produces `ok=false`, `state=failed`, a
   non-zero `returncode`, and a failed completion event in every supported
   dispatch path.
3. A process killed by the command deadline produces `ok=false`,
   `state=timed_out`, `timed_out=true`, a deadline owner, cleanup result, and
   retained diagnostic output.
4. A user/turn/session cancellation produces `ok=false`, `state=cancelled`,
   identifies the cancellation source, and leaves no owned descendant running
   after the configured grace period.
5. A spawn failure identifies whether the executable, cwd, permission, shell,
   or environment setup failed and is never rendered as “Completed.”
6. A caller requesting a 300-second command timeout either receives that
   effective policy or receives a structured earlier-deadline explanation; it
   is never silently cut off by an unrelated 30-second wrapper deadline.
7. Foreground and background execution have identical timeout-zero semantics,
   result fields, redaction, and process-group ownership rules.
8. `run_bash` and `run_command` expose validated `cwd` and safe environment
   overlays, and operation history preserves the exact command/argv despite
   display truncation.
9. `npm run dev` or an equivalent fixture can be started as an owned service,
   returns a handle without waiting for process exit, reports readiness only
   after the configured probe succeeds, and is stopped through `/stop` without
   leaking descendants.
10. A readiness observer timeout does not kill the service unless an explicit
    stop policy was requested.
11. `next build` or an equivalent finite build fixture returns success only on
    exit code zero; a partial output directory never causes success.
12. The TUI labels failed, timed-out, cancelled, running, ready, and completed
    commands distinctly. “Completed” appears only when `ok=true`.
13. A workflow phase requiring a finite command cannot follow `next` after a
    failed, timed-out, cancelled, rejected, or orphaned command.
14. A service workflow can persist a live handle and readiness result, resume
    safely, and refuse to start a duplicate owned service.
15. Existing PRD-149 `/ps`, `/stop`, Esc, terminal limits, output bounds,
    retention, session ownership, and `agenthicc agents` behavior remain
    compatible.
16. Unit, integration, and E2E coverage includes short commands, non-zero
    commands, timeout, cancellation, output-heavy commands, process trees,
    shell/exec modes, cwd/env, build fixtures, service readiness, restart,
    orphaning, workflow gates, and non-interactive output.
17. Documentation explains the exact command journey for finite builds and
    development servers, including timeout units, background handles,
    readiness, `/ps`, `/stop`, troubleshooting, and security limits.

## 13. Verification plan

Focused checks should include:

```bash
uv run pytest tests/unit/test_exec_tools.py -q
uv run pytest tests/unit/test_tool_executor_contract.py -q
uv run pytest tests/unit/test_background_terminals.py -q
uv run pytest tests/integration/test_exec_tools_integration.py -q
uv run pytest tests/integration/test_background_terminals_integration.py -q
uv run pytest tests/e2e/test_background_terminals_e2e.py -q
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
```

The Node/Next fixture must report its prerequisites (`node`, `npm`, package
manager, installed dependencies, and platform) and skip explicitly when it
cannot run. A skipped optional fixture is not evidence that a real Next build
works. A passing fixture must record exit code, timeout, readiness, and cleanup
outcomes.

## 14. Implementation evidence

PRD-151 is implemented across the existing PRD-149 ownership boundary. The
implementation adds the shared `CommandOutcome`/`CommandState` contract,
seconds-based deadline validation, process-group cleanup and output draining,
cwd/environment propagation, service lifecycle and readiness probes,
non-destructive terminal observation, duplicate-service protection, workflow
command completion gates, headless phase metadata, and state-aware TUI
rendering. Existing terminal controls and storage remain compatible.

Verification completed on 2026-07-27:

```text
uv run pytest tests/ -q
2419 passed, 15 skipped

uv run pytest --run-cassette tests/integration/test_cassette_replay.py -q
2 passed

uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run nox -s llms_check
```

Dedicated PRD-151 coverage includes direct, Lauren-wrapper, and adapter
failures; fractional/invalid/zero timeouts; cancellation and output tails;
cwd/env and spawn diagnostics; non-destructive waits; process-group timeout;
service marker readiness and duplicate prevention; workflow gates; a finite
build fixture; and service lifecycle E2E tests. A real Next.js application is
not present in this repository, so the finite build fixture is the deterministic
equivalent; a pinned Next.js smoke fixture can be supplied by an integration
repository that provides its package lock and dependencies.

## 15. Risks and open decisions

| Risk/decision | Required treatment |
|---|---|
| An arbitrary command looks like a server | Do not silently infer service mode in the first release; make lifecycle intent explicit and provide model/tool guidance. |
| A service prints “ready” but is unusable | Prefer HTTP/TCP probes or require an explicit marker rule and record evidence. |
| An outer deadline cancels the coroutine during cleanup | Shield the cleanup task, bound it, and report `orphaned` when ownership cannot be proven. |
| Package managers spawn grandchildren | Use the owned process group/job and test descendant cleanup, never PID scanning. |
| Long builds produce huge logs | Stream into bounded tails with counters and truncation markers; retain actionable stderr. |
| `timeout=0` can allow an accidental infinite foreground command | Keep enclosing turn/session shutdown, show an explicit warning, and require service/background intent for known long-lived workflows. |
| Windows process semantics differ | Isolate job/process controls behind the terminal portability boundary and keep a platform-specific acceptance matrix. |
| Existing consumers expect the old mapping without `ok` | Preserve stdout/stderr/returncode fields while adding fields; fix only the success classification and document the contract change. |
| The incident command text is visually truncated | Preserve exact structured input and add a clear “preview shortened” marker; do not infer the exact original typo without raw tool-call data. |

The implementation review must decide whether service readiness belongs in the
existing terminal record or a linked child record. Either choice must keep one
authoritative terminal owner and one workflow/session link; it must not create a
parallel process registry.

## 16. Rollout and migration

The change should ship in compatibility-first stages:

1. Correct failure classification and add result fields without changing the
   default foreground launch mode.
2. Add cancellation/deadline cleanup and warnings for ambiguous timeout or
   service usage.
3. Enable explicit service/readiness mode and workflow completion gates.
4. Migrate built-in and documented user workflows to declare finite versus
   service lifecycle explicitly.
5. Existing sessions and terminal records must not be relaunched automatically;
   uncertain records remain `orphaned` or historical.

## 17. Related documents

- [PRD-148 — Unified Interrupt and Graceful Cancellation](prd-148-unified-interrupt-and-graceful-cancellation.md)
- [PRD-149 — Background Terminals and Responsive Wait Control](prd-149-background-terminals-and-responsive-wait-control.md)
- [Background sessions guide](../docs/guides/background-sessions.md)
- [Architecture guide](../docs/guides/architecture.md)
- [PRD index](README.md)
