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
equivalent. They preserve the current session ID and journal, then return to the shell after the
worker lease is established. Both commands appear in the slash-command picker.

For a goal-backed session, `/detach` is the explicit foreground-to-background
handoff. Agenthicc durably records the request and session first, cancels
foreground input, releases the foreground owner, and only then lets the
background worker claim the same session. It does not copy the conversation or
create a second workflow run. Inspect the product-level run with:

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
only the workspace directory name, not its full path.

The manager shows state, workflow, workspace, phase, failure information, and
approval waits. Its default activity field contains only the newest meaningful
redacted text event; repeated tool/lifecycle events are omitted. It uses a
cached durable session projection and a cached bounded journal tail, so idle
repaints do not fold the complete registry or reread the transcript. Stale
worker recovery runs on a separate maintenance cadence. It can also be
rendered safely without a TTY, which is useful for scripts and diagnostics.

Useful keys:

| Key | Action |
|---|---|
| `↑`/`k`, `↓`/`j` | Move selection; moving across a boundary changes page |
| `Home`/`End` | Select the first/last session |
| `PageUp`/`PageDown` | Move one session page |
| `Enter` | Attach the exact selected session in the normal foreground TUI, loading its transcript |
| `r` | Refresh |
| `c` | Cancel the selected worker |
| `a` | Archive a terminal session |
| `p` | Pin or unpin a session |
| `y`/`n` | Approve or reject a visible approval request |
| `Ctrl+X` | Immediately delete the selected/marked sessions to recoverable trash |
| `t` | Include recoverable trash in the list |
| `u` | Restore a selected deleted session |
| `?` | Show help |
| `q`/`Esc` | Leave the manager without stopping workers |

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

## Configuration

The optional `[background]` section may be placed in the project or global
`agenthicc.toml`:

```toml
[background]
enabled = true
max_workers = 2
max_workers_per_project = 2
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
```

Defaults are conservative. Invalid values fail closed before a worker is
created. `--set background.max_workers=1` is supported for one invocation, and
`AGENTHICC_DISABLE_BACKGROUND=1` is an emergency local disable switch.

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
