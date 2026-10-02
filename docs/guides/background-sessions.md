# Background sessions

Background sessions let a workflow continue after the foreground terminal is
released. The registry and worker are local to the current user; no transcript
or lifecycle telemetry is sent to a hosted service.

## Start and detach

Start a direct turn or an existing workflow from a shell:

```bash
uv run agenthicc run --background --intent "Review the authentication flow"
uv run agenthicc run --background --workflow code_plan --intent "Plan the next release"
```

The command returns the stable session ID as soon as the worker is accepted.
Inside an active TUI session, `/bg`, `/background`, and `/detach` are
equivalent. They preserve the current session ID and durable journal, then
return to the shell after starting a background worker. Both commands appear
in the slash-command picker.

### What `/detach` does end to end

The command hands the current session to a separate local worker process; it
does not merely hide the TUI while the foreground model request continues.
The path through the runtime is:

1. **The command is routed immediately.** CLI command discovery installs
   `/detach` and the `/bg`/`/background` aliases. They use an immediate-control
   busy policy, so the command can be handled while an agent turn is running
   instead of waiting in the ordinary message queue. The integration bridge
   intercepts the command before it is treated as a new user request.
2. **The handoff chooses the work to continue.** It uses the most recently
   accepted user text as the worker intent—not the literal `/detach` command.
   It also carries the active or configured workflow name. If there is no
   prior user request, or background execution is disabled, the handoff is
   rejected and the TUI stays open.
3. **The request is persisted before launch.** The bridge asks the background
   supervisor to prepare a request containing the existing session ID, intent,
   workflow, workspace, and relevant run/configuration metadata. This reuses
   the session; it does not copy the transcript or create a second workflow
   run.
4. **Foreground execution is asked to stop.** If the TUI owns an active agent
   task, `/detach` calls `task.cancel()`. This is an asynchronous cancellation
   request, not proof that the task has already finished unwinding.
5. **Ownership is handed over and the worker is launched.** The prepared path
   releases the foreground session-owner lease, then starts the supervisor.
   The supervisor launches `python -m agenthicc.background.worker` as a
   separate process, with its own background log and the persisted request.
6. **The worker claims and reconstructs the session.** It claims the background
   record, acquires session ownership, rebuilds session context using the same
   session ID, and starts the event processor. If a recoverable workflow is
   present, it resumes that workflow; otherwise it runs the saved intent as a
   direct turn. Progress and terminal status are written to the background
   store.
7. **The foreground input loop exits.** The TUI shows a transient
   `Backgrounded session …` notice and signals its input loop to exit. During
   TUI shutdown, it awaits the foreground agent task while closing its other
   resources; after cleanup, control returns to the shell. The background
   worker is independent of that terminal shutdown.

There is an important ordering detail in the current implementation: step 4
requests cancellation, but does not await the agent task before steps 5–6
release ownership and launch the worker. TUI teardown later awaits that task,
but that later wait is not a quiescence barrier before worker startup. Thus the
handoff is durable, but there can be a short interval in which foreground
cancellation is still unwinding as the worker starts. This is a potential
race around session/workflow state, not a guarantee that every detach loses
state.

When the cancelled foreground task belongs to a running workflow, its
cancellation handler currently records a workflow failure with reason
`cancelled` and closes the foreground turn. The scroll appender can therefore
show `ERROR cancelled` even though the background worker has been launched.
The subsequent `Total wall clock time since last IDLE` line is emitted when
the foreground activity ends; it describes that activity's elapsed duration,
not necessarily additional work after cancellation. A plain non-workflow turn
can take the cancellation path without emitting the same workflow failure.

For a goal-backed session, `/detach` is the explicit foreground-to-background
handoff. It does not copy the conversation or create a second workflow run.
Inspect the product-level run with:

```bash
agenthicc runs
agenthicc agents --run RUN_ID
agenthicc attach SESSION_ID
```

The lower-level `jobs`/`agents` manager remains available for ordinary
background sessions that do not belong to a goal run.

`agenthicc attach SESSION_ID` is deliberately session-only. `SESSION_ID` is an
exact durable background-session ID, not a workspace-relative lookup and not a
goal-run ID. A workspace may contain many sessions, so attach never guesses the
latest record or resubmits its intent. Use `agenthicc agents --run RUN_ID` for
goal-run discovery, or the explicit `agenthicc attach --run RUN_ID` form when
attaching by goal-run identity is required.

For `agenthicc --goal GOAL --detach`, the accepted session is owned by a
detached child worker. Startup reports that worker's PID and the background
record retains it after exit. The worker finalizes terminal workflow results,
irrecoverable errors, cancellation, and confirmed idle outcomes before
returning; it records an exit reason and never signals the launcher or a PID
read from stale storage. An attached `--goal` TUI does not use this finalizer
and remains available for another user message after an agent turn is idle.

Global `--mode MODE` is written into the durable background-session record
before the child process is launched. The session projection keeps the
invocation value (`requested_mode_name`) separate from the effective canonical
value (`mode_name`): before startup succeeds, the mode status is `pending` and
the effective value is empty. After the production session builder resolves
the request through `ModeManager`, the worker writes the canonical value and
`applied` status, tagged with the owning worker attempt. A mode cannot be
reported as effective merely because it appeared in the launch request.

The worker must durably attest that mode before dispatching a provider turn,
workflow phase, tool, or subagent. If resolution or the durable attestation
fails, the attempt is marked failed and agent work is not started. Application
updates are checked against the current session lease and attempt so that a
late worker cannot replace newer state. `jobs status SESSION_ID --json`, the
interactive `agents` manager, `agenthicc runs show RUN_ID --json`, and
`agenthicc agents --run RUN_ID --json` expose the requested/effective/status
fields. The manager shows `pending: YOLO`, for example, instead of presenting
the unvalidated value as an active mode. A no-explicit-mode launch reports
pending default/persisted resolution until normal session construction
completes.

Mode application does not enable `dangerously_skip_permissions`; that flag
continues to be controlled independently. On resume/retry, an explicit mode
on that invocation takes precedence. Without one, an unconsumed request is
preserved until applied; after successful initialization, the canonical mode
persisted with the session is used.

For example:

```bash
agenthicc --mode Yolo --goal "Implement OAuth" --detach
agenthicc --mode Plan run --background --workflow code_plan --intent "Plan the migration"
```

Workflow phase-specific mode overrides remain in effect for their declared
phases; the CLI mode selects the session's initial/default runtime mode.

Recovery does not trust numeric PID liveness alone. Where procfs is available,
the recorded PID must match the Agenthicc worker module and the exact request
file/store identity for the session; an unmatchable or reused PID is treated as
orphaned and surfaced for reconciliation.

## Open and inspect the manager

`agenthicc agents` and `agenthicc jobs` open the same manager TUI. The manager
is viewport-aware and paginates the session table, keeping the selected
summary and controls visible even on a short terminal. The header reports the
current page and visible range; selection is stored by session ID, so refreshes
cannot retarget an action to a different row. The selected row is marked with
`▶` and the details panel is labelled `Details · selected`.
The table's `WS` value and the selected-session details' `Dir:` value contain
only the workspace directory name, not its full path. Press Enter on a row to
open a dedicated details page; it shows the workspace directory name, lifecycle
timestamps, workflow and phase history, provider/model, recent activity, and
failure information. Press Enter again there to attach that exact session.
Press Esc to return to the table. The detail frame stays sized to its visible
fields so its navigation/action hints remain on-screen. Use `↑`/`↓` or `j`/`k`,
`PageUp`/`PageDown`, `Home`/`End`, or `[`/`]` to scroll without changing the
selected session; the view does not dump the session's complete tool history.

Press `i` on a live session's detail page to open a target-pinned composer.
It uses the normal `UnifiedInputSession` editing and trigger pipeline, with
mentions rooted at the target workspace and command/skill suggestions built
from that session's launch configuration and project. Submitting ordinary text
does not attach, restart, or change the worker lease. The manager durably queues
it for the exact owner attempt; the worker consumes it at an agent-safe
boundary, or runs it as a follow-up turn if it arrived after the previous turn's
last tool boundary. A worker that finishes first leaves a visible rejection
receipt with a resend instruction rather than silently dropping the text.
The detail page's `Latest input` field shows the newest receipt (`queued`,
`delivered`, `processed`, or `not delivered`) and a short message ID, never the
message body. The same message ID fences retries and duplicate Enter events.

Target-side non-interactive commands and skills use the worker's command and
skill registries; a command is never evaluated by the manager's own session.
Commands that require a foreground overlay, and `/workflow` recovery controls,
must be run after attaching so their session-owned UI/checkpoint semantics are
preserved. A text entry sent while the worker is running is not an approval:
use the existing `y`/`n` controls for approval requests.

The manager paints a loading viewport immediately, then loads a page through a
bounded background service. Navigation and lifecycle operations do not wait
for registry reads, process checks, artifact moves, or journal parsing. Its
projection uses an atomic versioned snapshot and incrementally replays newer
JSONL events; the event log remains authoritative and malformed or incompatible
snapshots are rebuilt. The selected activity tail is read asynchronously and
redacted before it reaches the details panel. Long activity is shortened to one
visible line so `Updated` and identity details remain visible. Refreshes and
terminal frames are coalesced, and stale worker recovery runs independently of
the repaint cadence. Projection folding is serialized per store so refresh and
maintenance threads cannot race while rebuilding shared indexes. Startup
maintenance waits until the first session page is ready. If an index read
fails, the manager shows the error and retry hint; automatic retries back off
instead of repeatedly hammering the store. The non-TTY listing and JSON output
remain complete.

Useful keys:

| Key | Action |
|---|---|
| `↑`/`k`, `↓`/`j` | Move selection; moving across a boundary changes page |
| `Home`/`End` | Select the first/last session |
| `PageUp`/`PageDown` | Move one session page |
| `Enter` | Open the selected session's detailed page |
| `Enter` (details page) | Attach that exact session in the normal foreground TUI, loading its transcript |
| `i` (details page) | Open the composer for that exact live session; accepted input continues asynchronously |
| `Esc` (details page) | Return to the session table |
| `↑`/`↓`, `j`/`k` (details page) | Scroll one detail row without changing the selected session |
| `PageUp`/`PageDown` (details page) | Scroll one detail viewport |
| `Home`/`End` (details page) | Scroll to the first/last detail row |
| `[`/`]` (details page) | Scroll six detail rows |
| `r` | Refresh |
| `c` | Cancel the selected worker |
| `a` | Archive a terminal session |
| `p` | Pin or unpin a session |
| `y`/`n` | Approve or reject a visible approval request |
| `Ctrl+X` | Immediately delete the selected/marked sessions to recoverable trash |
| `t` | Include recoverable trash in the list |
| `u` | Restore a selected deleted session |
| `?` | Show help |
| `q` | Leave the manager without stopping workers |
| `Esc` (session table) | Leave the manager without stopping workers |

If a session is deleted while its details page is open, the second Enter never
attaches the row that shifted into its place. The manager validates the exact
session ID against the durable store before handoff; deleted or missing
sessions are rejected in place, and a validation read never stops or
foregrounds a worker. Only after validation does the CLI perform the normal
single-owner foreground handoff.

Foregrounding is an ownership handoff, not a second observer. For an active
session the manager stops that session's worker and owned terminals, verifies
the worker lease has ended, and then opens the normal TUI with the same
`session_id`, project directory, and durable journals. This lets the user send
new directions without concurrent writers or duplicated tool calls. Completed,
failed, cancelled, and orphaned sessions open directly through the same resume
path; leaving that TUI does not relaunch a background worker.

Delete is asynchronous and recoverable. Pressing `Ctrl+X` immediately captures
the selected/marked session IDs and starts one durable operation; no second
confirmation key is required. Active work is cancelled first, then only
the exact session directory and its sibling kernel journal are moved to
`~/.agenthicc/background/trash/`. The tombstone remains in the append-only
registry, so a stale worker cannot resurrect it. `u` restores the artifacts
when they are still in recoverable trash. In the interactive manager, deletion
runs off the TUI event loop; the table remains responsive
while active-worker cancellation and terminal cleanup finish, then refreshes
automatically. A repeated `Ctrl+X` is ignored while the current operation is
active, and failures remain visible with durable retry/recovery metadata.

## Scriptable control

```bash
uv run agenthicc agents --json
uv run agenthicc jobs list --json
uv run agenthicc jobs status SESSION_ID --json
uv run agenthicc jobs cancel SESSION_ID
uv run agenthicc jobs resume SESSION_ID
uv run agenthicc jobs retry SESSION_ID
uv run agenthicc jobs approve SESSION_ID
uv run agenthicc jobs archive SESSION_ID
uv run agenthicc jobs delete SESSION_ID
uv run agenthicc jobs restore SESSION_ID
```

The JSON manager listing is complete and unpaginated. Pagination applies only
to the interactive viewport; it never hides or removes a durable session.

JSON status removes the original intent and lease token and applies the same
secret-pattern redaction used by session inspection. A missing or invalid
transition is reported as a failed control operation; workers are never
implicitly relaunched after a process restart.

Target input receipts live at `~/.agenthicc/background/input-inbox/<id>.jsonl`,
separate from background lifecycle logs. The inbox stores the submitted text
because the live worker needs to deliver it, so those files are sensitive just
like the target transcript and must not be copied into generic logs or shared
support bundles. Receipts contain the message ID, owner attempt, state, and a
bounded error; raw lease tokens are not persisted.

## Configuration

The optional `[background]` section may be placed in the project or global
`agenthicc.toml`:

```toml
[background]
enabled = true
max_workers = 4
max_workers_per_project = 4
cancel_grace_s = 5.0
stale_after_s = 30.0
wall_timeout_s = 0.0       # 0 means no wall-clock timeout
max_activity_bytes = 64000
trash_retention_days = 30
terminals_enabled = true
max_terminals = 4
max_terminals_per_project = 8
terminal_max_output_bytes = 64000
terminal_wall_timeout_s = 0.0
terminal_cancel_grace_s = 5.0
terminal_retention_days = 30

[background.manager]
refresh_interval_s = 0.25
maintenance_interval_s = 5.0
projection_batch_size = 256
activity_tail_bytes = 64000
frame_debounce_ms = 16
max_in_flight_operations = 4
metrics = false
```

Defaults are conservative. Invalid values fail closed before a worker is
created. `--set background.max_workers=1` is supported for one invocation, and
`AGENTHICC_DISABLE_BACKGROUND=1` is an emergency local disable switch.
Manager settings are independently validated and bounded; they control only
refresh, display, and concurrency cadence, not locking, ownership, or durable
event recording. Set `metrics = true` to collect redacted timing aggregates
while diagnosing latency.

The background registry is an append-only, fsync'd JSONL event stream. It is a
derived lifecycle index; the canonical conversation, workflow, kernel, and
approval journals remain owned by their existing runtime components. A missing
worker lease becomes `orphaned` and requires an explicit resume or retry.

## Owned background terminals

`run_bash` and `run_command` keep their existing foreground result by default.
An explicit request such as:

```json
{
  "command": "uv run pytest tests/unit -q",
  "background": true,
  "label": "unit tests",
  "timeout": 1200
}
```

returns a `term-...` handle immediately. `wait_terminal` follows the handle
and returns bounded stdout/stderr, exit status, elapsed time, and truncation
metadata. Its `timeout` is an observer timeout in seconds and does not stop
the process; use `/stop` or `stop_terminal` to terminate it. A workflow phase can opt into this default with
`PhaseSpec(..., terminal_wait_policy="background")`; command text alone never
backgrounds a process.

Use `lifecycle="service"` for development servers and an explicit loopback
`readiness` probe. The service remains `running` after readiness; it is not a
successful finite command. See the [reliable command execution guide](./command-execution.md)
for build, environment, timeout, readiness, and workflow-gate examples.

While the TUI awaits a handle, its status line shows elapsed time, the running
terminal count, the command label, and `/ps`, `/stop`, and `Esc` controls.
`/ps` opens the live terminal list (use `/ps --json` for redacted metadata),
`/stop` and `/stop all` request graceful process-group shutdown for every
owned background terminal; no terminal ID or active wait is required.
`/stop <terminal-id>` targets one handle. `/stop all` asks for an explicit
`--confirm`; `--force` both confirms and skips the graceful signal. A terminal
is stopped only
through the manager entry that created its process group; arbitrary PIDs are
never discovered or attached. Parent background-session cancellation also
cleans up child terminal records linked to that session.

The interactive `/ps` view is paginated. Its header shows the current page,
visible range, and total record count, for example `page 1/3 · showing 1–6 of
15`. Use `↑`/`↓` or `j`/`k` to move through records, `PageUp`/`PageDown` to
move by a page, and `Home`/`End` to jump to the first or last record. The
selected terminal ID remains stable when a process completes or the terminal
is resized. The table and selected-terminal details are bounded to the
available Live-region height, so the close and stop controls remain visible;
long labels and output are shortened for narrow terminals. `/ps --json` is
not paginated and continues to return the complete owned-record projection.

Terminal records are persisted below `~/.agenthicc/background/terminals/` (or
the configured `store_path`), with mode-600 JSONL events. Commands, labels,
and captured output are bounded and common credential-shaped values are
redacted. Active records found after a manager restart become `orphaned`
diagnostics rather than being reported as successful or silently relaunched.

## Verification

Run the full repository tests plus the PRD-141 maintained-surface gate with:

```bash
uv run python -m agenthicc.background.coverage_gate
```

The gate enforces at least 90% coverage across the background package, the
background CLI commands, and the background manager workspace. The ordinary
full-package coverage report remains useful for tracking unrelated legacy and
platform-specific surfaces.

## Try it

Background sessions are inspectable without opening the manager TUI:

```bash
PYTHONPATH=src python -m agenthicc jobs list --json
```

```text
[
  {
    "approval_decision": null,
    "approval_request": "Workflow Design Review",
    "artifact_dir": "/root/.agenthicc/sessions/e8bc3147-02e5-4648-a16d-a8ad43e8e708",
    "attempt": 1,
    "cancellation_reason": "foreground handoff requested",
    ...
  }
]
```

The field names are the contract: `artifact_dir` names the durable location to
inspect, `attempt` distinguishes a retry from a first run, and
`cancellation_reason` tells you *why* a job stopped rather than only that it
did. `approval_request`/`approval_decision` are the pairing you need to
understand a job parked on an approval.

To see the entry points the runtime exposes:

```bash
PYTHONPATH=src python -m agenthicc jobs --help
```

```text
usage: agenthicc jobs [-h] <subcommand> ...
```

Accepted subcommands are `list`, `status`, `cancel`, `resume`, `retry`,
`approve`, `reject`, `input`, `rename`, `labels`, `purge`, `archive`, `delete`,
and `restore`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A job is stuck and no prompt is visible | It is parked on an approval | `agenthicc jobs status JOB --json` shows the pending approval; then `jobs approve` or `jobs reject` |
| A job needs an answer, not an approval | It is waiting on input | Use `agenthicc jobs input JOB ...` to supply it |
| A cancelled job cannot be found | Cancelled work may have moved to the trash | `agenthicc jobs list --trash` lists recoverable sessions; `jobs restore` brings one back |
| A job list is cluttered with old entries | They have not been archived or purged | `jobs archive JOB` to keep it out of the default list, `jobs purge` to remove it deliberately |
| `agenthicc agents` behaves like `agenthicc jobs` | They are the same manager | Expected: both are registered entry points to the background-session manager, and `jobs <subcommand>` is the scriptable form |
| `/bg` is missing from the command picker | It is injected outside `BUILTIN_COMMANDS` | `/background` and `/bg` come from the background integration; check that the integration loaded |
| A background job wrote to an unexpected place | `cwd` was relative at submit time | Pass an explicit working directory to `run --background`; inspect `artifact_dir` in the job record |
| A job shows `attempt: 2` unexpectedly | It was retried | Compare with `jobs status --json`; a retry after a cancellation may be intentional, so confirm before assuming a crash |
| A stale job is still listed as running | Stale detection is lease-based | Check the worker lease; a dead worker is reclaimed rather than silently reported as healthy |
| Killing the parent session left child processes alive | Detached terminals are owned by the session's terminal manager | Cancelling a detached parent asks the terminal registry to stop its exact child groups. Inspect with `/ps` and stop with `/stop <terminal-id>` |
| Background state disappeared after a restart | The store holds only the rebuildable lifecycle index | The index is rebuilt from durable owners (kernel events, conversation events, workflow phase state), so a lost index is recoverable |
| Archiving lost the audit trail | Archive moves the listing entry | Workflow phase state, approvals, and memory keep their own owners; the index was never the audit trail |

### A job is a control-plane record, not a runtime

`BackgroundStore` owns only the rebuildable lifecycle index. `BackgroundSupervisor`
owns worker leases, bounded process creation, cancellation, stale detection, and
control requests; the worker itself builds the normal session and delegates to
the canonical agent-turn runner or the headless workflow runner. So "the job is
wedged" is usually one of three distinct things: a lease problem, a control
request that was never answered, or a turn that is genuinely running.

### Manage the index, not the state

`purge` and `delete` remove listing entries. If you need the history for
forensics, export or inspect before purging — the lifecycle index is explicitly
rebuildable and is not the durable record of what happened.
