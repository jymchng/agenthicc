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

The command returns the stable session ID as soon as the request is durably
accepted. With queueing enabled, acceptance may mean `queued`, not that a
worker process has started.
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
   direct turn. For an idle handoff, the request is resume-only: the worker
   resumes a valid checkpoint if present and otherwise exits without replaying
   the historical intent or making a model request. Progress and terminal
   status are written to the background store.
7. **The foreground input loop exits.** The TUI shows a transient
   `Backgrounded session …` notice and signals its input loop to exit. During
   TUI shutdown, it awaits the foreground agent task while closing its other
   resources; after cleanup, control returns to the shell. The background
   worker is independent of that terminal shutdown.

If the process that owned a session is killed abruptly, its lifecycle record
may still say `running` after `agenthicc --continue` has safely reclaimed the
session-owner lease. A subsequent `/detach` now reconciles that exact record:
it verifies the recorded worker process identity, verifies the current TUI
still owns the same session lease, and compare-and-sets the dead attempt to
`orphaned` before preparing another attempt for the same session ID. It does
not use heartbeat age alone to decide that a worker is dead. A verified live
worker is left untouched; a dispatch claim or uninspectable process is
reported as an actionable recovery conflict rather than starting a duplicate.

The handoff also carries the resumed TUI's config path, CLI overrides,
requested provider profile, mode, workflow, and run ID into its private worker
request. Thus a config/model selection made for the resumed invocation is not
silently replaced with the default config when the worker starts. Secrets stay
in the protected request envelope and are not printed in status or error
messages. If worker startup fails after the foreground lease is released, the
TUI reacquires that lease only after the store confirms the worker attempt is
terminal; otherwise it exits safely so the session can be reopened with
`agenthicc --continue`.

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
`completed` describes the previous background attempt, not permanent closure
of the session. Detaching a session with a completed prior attempt reuses the
same session ID and starts a new attempt when recovery work exists. If the
foreground agent is idle, the worker only resumes an existing recoverable
workflow checkpoint; it does not replay the last user message or restart a
completed `goal_flow` from its first phase. If there is no recoverable work,
the short-lived worker exits without making a model request.
Inspect the product-level run with:

```bash
agenthicc runs
agenthicc agents --run RUN_ID
agenthicc attach SESSION_ID
```

## Background interaction inbox

Pending background tool approvals, `ask_user` questions, and explicit input
requests are listed in the agents hub's Needs you tab (`/inbox` in the TUI)
and through `agenthicc agents inbox` in scripts. These are typed responses to an
exact pending operation; they are separate from arbitrary text submitted to an
agent from the hub's composer. That input is still forwarded
unchanged to the owning session, whose normal command/router path may handle
commands such as `/workflow resume` or `/workflow reset`.

### Input waiting on interrupted workflow recovery

A background session can have both an unfinished journal turn and a saved
workflow checkpoint. Agenthicc records whether a new turn belongs to a direct
agent turn or to a workflow run, then uses that provenance to choose one safe
recovery owner. A proven direct turn is replayed with its original turn ID and
tool-result ledger before ordinary queued input is delivered. A turn owned by
a workflow is never replayed as an independent direct turn.

Older journals may not contain this ownership metadata. Agenthicc can still
recover automatically when exactly one recoverable checkpoint matches the
session's configured workflow, conversation ID, and original intent; it
restores that run first, then delivers the queued input unchanged. If the
identity does not match exactly or more than one run qualifies, the worker
does not guess or reject accepted input. It leaves the session recoverable and
the inbox receipt as `waiting for recovery`, with a bounded reason and relevant
workflow run ID in the agent's detail view. Send an explicit target-side
command from that detail view with `i`, for example:

```text
/workflow resume <run-id>
/workflow reset <run-id>
```

These are ordinary inputs forwarded to the target session. They do not attach
the session to the foreground. A recovery control may be routed ahead of an
earlier blocked ordinary message so the queue cannot deadlock; the ordinary
message remains durably pending and retains its relative order when it is later
delivered. New journal turns persist `execution_kind` and, for workflow turns,
`workflow_run_id`; legacy entries remain readable. Merely having a checkpoint
does not implicitly resume a workflow.

```text
/inbox
/inbox type=question older-than=60
/inbox project=/path/to/project status=pending
```

The TUI lists the oldest pending requests first. Select a question/input item
and press Enter to type an answer; one-question answers are sent as exact text,
including whitespace and pasted newlines. For multiple questions, submit a JSON
object keyed by the displayed question IDs. For approvals, `a` approves with
the least broad available scope, `n` rejects, and `x` expires the request. Plan
reviews can be approved with the displayed mode choices. A version conflict or
stale/deleted session is shown without delivering the response elsewhere.

First list CLI items to get the current version, then use it and a
caller-generated idempotency key for each write:

```bash
agenthicc agents inbox list --json
agenthicc agents inbox answer ITEM_ID 'answer text' VERSION request-uuid
agenthicc agents inbox answer ITEM_ID '{"runtime":"Python"}' VERSION request-uuid
agenthicc agents inbox approve ITEM_ID VERSION request-uuid
agenthicc agents inbox reject ITEM_ID VERSION request-uuid --message 'Not approved'
agenthicc agents inbox expire ITEM_ID VERSION request-uuid
```

The JSON answer form is required only for multiple question IDs. Approval
commands accept optional `--message`, `--mode`, `--remember`, `--remember-all`,
and `--scope-grant`; workspace access defaults to `target_once`. Accepted
answers are stored privately and omitted from item projections. Compare-and-set
versions ensure only one concurrent operator wins. Question expiry uses the
existing `[tools].question_timeout_s` setting; approvals can be explicitly
rejected or expired by an operator.

The authenticated session service exposes `GET /v1/inbox`,
`POST /v1/inbox/{item_id}/respond`, and `POST /v1/inbox/{item_id}/expire`.
Read capability is required to list; control capability is required to
respond/expire. Writes require `expected_version` and `idempotency_key` (or an
`Idempotency-Key` header); a stale version returns HTTP 409 with the current
redacted item projection.

## Durable queue and priorities

Queueing is opt-in during rollout. Set `background.queue_enabled = true` to
accept work while all configured worker slots are occupied. Accepted requests
are recorded in the existing `events.jsonl` background log before the CLI
reports success. The scheduler applies global, per-project, and optional
per-user limits, and rotates fairly among eligible projects. Priorities are
`urgent`, `normal` (the default), and `low`; aging lets older lower-priority
requests overtake a stream of newer high-priority work. The reported queue
position is advisory and can change as capacity or priority changes.

```bash
agenthicc run --background --priority urgent --intent "Fix the release blocker"
agenthicc --priority low --goal "Update generated API docs" --detach
agenthicc agents priority SESSION_ID normal
agenthicc agents list --json
agenthicc agents status SESSION_ID
```

An optional `--idempotency-key KEY` on `agenthicc run --background` makes a
repeat submission by the same local user return the existing request. Reusing
the key for different intent, workspace, workflow, mode, or profile is rejected.
Changing priority with `agents priority` (or `P` in the hub) writes a bounded
audit event; it never
restarts a running worker.

Cancelling a request whose dispatch state is still `pending` changes its
durable status without creating a child process or acquiring a session lease.
Once dispatch is reserved, cancellation uses the normal worker lifecycle.
When a worker finalizes, it asks the scheduler to fill the freed slot. On
restart, pending records are recovered and dispatched under a cross-process
scheduler lock; terminal records are never resubmitted. A crash after the
scheduler reservation is fenced by a durable dispatch token and the worker's
existing session claim, so duplicate child launches cannot both execute the
accepted request.

The agents hub lists ordinary background agents and goal runs together; a
goal run's workers are nested under its main agent.

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
late worker cannot replace newer state. `agents status SESSION_ID --json`, the
agents hub's detail view, `agenthicc runs show RUN_ID --json`, and
`agenthicc agents --run RUN_ID --json` expose the requested/effective/status
fields. The hub shows `pending (requested YOLO)`, for example, instead of presenting
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

## The agents hub

`agenthicc agents` opens the agents hub full screen; inside a session,
`/agents`, Ctrl+G or ← on an empty prompt opens the same hub over the
conversation, and Esc returns to it exactly as it was (AGX-05). One component
shows every agent:

| Tab | Shows | Order |
|---|---|---|
| **Running** | Active background agents, queued ones, pinned ones, and those that finished in the last 30 minutes; a goal run's workers nest under its main agent; the next scheduled occurrences | Grouped: Pinned, Needs you, Ready for review, Running, Queued, Recently finished |
| **Needs you** | Every pending approval, question and input request (the operator inbox), plus waiting or lost agents without one | Oldest first |
| **History** | Finished, failed, cancelled and archived agents, and saved foreground sessions | Most recent first |
| **Library** | Agent definitions by source: project, user, built-in | Source, then name |

The tab bar carries live counts and highlights **Needs you** when it is not
empty. Open a tab directly with `agenthicc agents --tab needs-you` (or
`running`, `history`, `library`), one agent's detail view with
`agenthicc agents SESSION_ID`, and one goal run's agents with
`agenthicc agents --run RUN_ID`. `/inbox` opens the Needs you tab and
`/workers` the Running tab. `agenthicc sessions list` on a terminal opens the
History tab; piped, it prints the saved sessions as before.

### The list

Each row is the agent's status, title, report line, project, agent or workflow,
age and cost, in that priority: columns are dropped from the right as the
terminal narrows (activity from 90 columns, project from 100, agent from 110,
cost from 120) and a shown column is never narrower than its minimum, so
statuses stay readable at any width. The report line is the first sentence of
the agent's latest message, redacted, which the worker records as it works; a
completed agent keeps it as its summary, and a failed one shows its error.
One vocabulary is used by the hub, `agenthicc agents list` and these docs:

| Status | Glyph | Label | Group |
|---|---|---|---|
| `waiting_approval` | `◆` | needs approval | Needs you |
| `waiting_input` | `◆` | needs input | Needs you |
| `orphaned` | `⚠` | worker lost | Needs you |
| `running` | `●` | running | Running |
| `starting` | `◐` | starting | Running |
| `retrying` | `↻` | retrying | Running |
| `cancelling` | `⊘` | stopping | Running |
| `queued` | `◌` | queued #N | Queued |
| `completed` | `✓` | done | Ready for review (with a worktree branch) or Recently finished |
| `failed` | `✗` | failed | Recently finished |
| `cancelled` | `⊘` | cancelled | Recently finished |
| `archived` | `▫` | archived | History |

The list fills the terminal and scrolls; the selection is marked by `▸` and
stays on the same agent when an update moves it to another group. A `•` marks
an agent whose state or report changed since you last opened it (remembered in
`~/.agenthicc/state/agents-hub.json`). `s` cycles the order within the Running
tab's groups (attention, recent activity, created, title), `g` groups it by
project instead, Shift+↑/↓ reorders pinned agents and →/← expand and collapse a
goal run's workers. History reads 200 agents at a time and reads more as you
scroll.

`/` filters the tab as you type: every word must match, in any order, across
the title, intent, labels, report, ID and project. Tokens narrow further:
`status:failed` (or a label such as `status:needs-approval`), `project:app-0`,
`agent:reviewer`, `label:release` and `run:RUN_ID`. Esc clears the search and
restores the previous selection.

`+` opens the dispatch line at the bottom of the Running tab. Enter starts a
background agent as `agenthicc run --background` would, in the selected
agent's project (or the current directory); `@DIR` targets another project,
`/workflow NAME` picks a workflow and `/mode NAME` or `/model ID` set them for
this agent — alone on the line they set the default for the next ones. A
project whose extensions are not trusted asks for the `agenthicc trust`
confirmation first.

### Peek, detail and input

Space opens a peek panel under the selected agent: the exact pending request,
or its latest report line and current tool, with the keys that answer it. Enter
opens the detail view (a right-hand pane at 140 columns and wider). Its
sections answer four questions:

| Section | Shown when | Content |
|---|---|---|
| Needs you | The agent waits or its worker was lost | The inbox request's canonical target — tool, input, workspace paths, mode — redacted; `y`/`n` resolve that exact item |
| Now | The agent is active | Phase progress, queue position, the latest activity |
| Transcript | Always | The last 50 journal events, redacted with the export redactor, one line each; `f` toggles following new events, `t` the whole transcript |
| Changes | A worktree agent | `git diff --stat` from its base commit including uncommitted work; `d` shows the full diff |
| Attempts | More than one attempt | Each attempt's outcome and error |
| Details | Always | Only fields that have a value |

The title and the Needs you card stay at the top while the rest scrolls.
The header shows the agent's tokens and cost across all its attempts. The
worker records them from the session's usage ledger (PRD-157) once per
completed provider call — never streamed, provisional values — and writes them
with its next heartbeat as a delta update, so they appear within a second or
two; `agents status --json` and `agents list --json` include `tokens_in`,
`tokens_out` and `cost_usd`.

Press `i` on a live, stale-but-recoverable, or completed agent (in the list, the peek
panel or the detail view) to open a target-pinned composer.
It uses the normal `UnifiedInputSession` editing and trigger pipeline, with
mentions rooted at the target workspace and command/skill suggestions built
from that session's launch configuration and project. Submitting ordinary text
does not attach to the foreground. For a live worker, the hub durably queues
it for the exact owner attempt; the worker consumes it at an agent-safe
boundary, or runs it as a follow-up turn if it arrived after the previous turn's
last tool boundary. For an orphaned, failed, cancelled, or archived recoverable
session, the hub persists the exact input first, then resumes that same
session in the background. The hub does not infer whether the text is a
continuation, a new task, or a command. The target session routes it as though
it arrived through that session's own input path. Ordinary text always becomes
a normal turn in the existing target conversation. Even when the session has
a selected workflow or saved workflow checkpoints, the worker does not inspect
or resume them, start a new workflow, or replay the old launch intent on behalf
of ordinary input. Send an explicit target-side command such as
`/workflow resume [run-id]` when workflow recovery is intended. If worker
startup fails, the accepted message remains visibly pending/recoverable.
A live worker that finishes before consuming an input leaves a visible
rejection receipt rather than silently dropping the text.

For a completed session, the accepted message starts a new background attempt
for the same session and conversation. The worker first restores any unfinished
transaction or uniquely identified workflow that owns the saved turn, then
processes the accepted message; it does not replay the completed attempt's
original launch intent or detached goal-run bookkeeping. The session's
effective configuration and security settings are retained, and the new worker
is subject to the normal background concurrency limits. If the hub closes
after persisting the message but before launch, background maintenance finds
the still-pending receipt and starts recovery when capacity is available.
Slash commands, including `/workflow ...`, are still forwarded unchanged and
handled by the target's normal command router.

There is one direct-turn recovery case that precedes new input: if the journal
contains an interrupted direct turn, the worker first re-drives that original
message with its original turn ID and durable tool-idempotency ledger. The new
inbox message remains pending until the recovered turn reaches a safe input
boundary; if recovery fails first, the message stays recoverable for a later
attempt. A workflow checkpoint is automatically resumed only when durable turn
provenance identifies it, or when exactly one legacy checkpoint matches the
session workflow, conversation ID, and original intent. Ambiguous cases remain
recoverable for explicit resolution; the worker never runs direct-turn and
workflow recovery concurrently.

The detail view's `Latest input` line shows the newest receipt (`queued`,
`waiting for recovery`, `delivered`, `processed`, or `not delivered`) and a
short message ID, never the message body. The same message ID fences retries
and duplicate Enter events.

The hub treats `/workflow` like any other submitted text: it forwards the
unchanged input to the target. The target session's command router applies the
same workflow controls as the foreground TUI. Other commands and skills also
use the target's registries; no submitted command is evaluated by the
hub's own session. Accepted composer payloads, including whitespace and
newlines, are preserved exactly; blank or oversized input is rejected rather
than silently modified.

Recovery changes only how the target worker is started; it does not add a
second user turn. For a stale or completed session, the exact accepted inbox
payload is delivered before the old launch intent is considered, and ordinary
text is sent as a direct target-session turn. Recovery does not synthesize a
“Workflow continuation” message, select a checkpoint, or resume a workflow.
Only an explicit command submitted by the user—such as `/workflow resume`—is
handled as a workflow command by the target session. Inbox retries retain the
same message ID and body, so an uncertain hub response does not require
resending or create a duplicate turn.

This is intentionally distinct from the attached foreground TUI's ordinary
text continuation behavior for a paused workflow. Input sent from `agenthicc
agents` does not invoke that implicit continuation path; send an explicit
`/workflow resume` command when checkpoint recovery is intended. The model may
still answer in its own words, so diagnose a displayed “Workflow continuation”
assistant message by inspecting its transcript event/source rather than
assuming it was inserted by the input transport.

- `/workflow <name>` stores the selected workflow durably. If sent alone to a
  recoverable session, the session remains recoverable for the next input.
  Ordinary text submitted afterward is still forwarded as a normal target
  turn; it does not implicitly launch or resume that workflow.
- `/workflow` and `/workflow reset` restore the active mode's default workflow.
- `/workflow resume [run-id]` validates and resumes a saved checkpoint through
  the normal recovery coordinator and ownership claim path.
- `/workflow reset <run-id>` marks that checkpoint discarded through the
  checkpoint store's guarded discard path, then restores the mode default.

Target-side command feedback (including an invalid name, unknown subcommand,
missing checkpoint, or unsafe resume) is the result of delivered input. It must
not fail the background session merely because the target router displayed a
warning. The hub forwards every input unchanged and does not infer intent;
the target's normal input/command path decides what it means. In particular,
ordinary text never implicitly resumes or starts a workflow. Only
transport/owner-fencing failures mean the target did not receive the input.
Commands that need an interactive overlay may explain that the target has no
interactive surface; the hub still forwards them unchanged. A text entry
sent while the worker is running is not an approval: use the existing `y`/`n`
controls for approval requests. `/stop` as the whole reply stops the agent
instead of sending text.

### Keys

Keys come from the key map (`agents`, `agents.list`, `agents.peek` and
`agents.detail` contexts, rebindable in `~/.agenthicc/keybindings.toml`); the
footer shows only the keys valid for the selected agent and `?` lists them all.

| Key | Action | Valid when |
|---|---|---|
| `↑`/`k`, `↓`/`j`, `PageUp`/`PageDown`, `Home`/`End` | Move the selection (scroll the detail view) | |
| `Alt+↑`/`Alt+↓` | Jump between groups | |
| `Enter` | Open the agent; in the detail view, attach it | |
| `Space` | Peek (Space or Esc closes it) | An agent row |
| `y` / `n` | Approve / reject the exact pending request | It needs approval |
| `i` | Reply: answer the question, or send input through the target-pinned composer | It accepts input |
| `r` / `R` | Resume / retry, as `agenthicc agents resume`/`retry` | Orphaned, failed, cancelled or archived / failed or cancelled |
| `c` | Stop (every marked agent, if any are marked) | Active |
| `A` | Archive (every marked agent, if any are marked) | Finished |
| `p`, `e`, `l`, `P` | Pin, rename, labels, queue priority | |
| `v` / `m` | Mark for stop, archive or delete | |
| `Ctrl+X` | Delete to recoverable trash (every marked agent, if any are marked) | |
| `x` | Expire the request (Needs you); worktree environment actions (detail of a worker) | |
| `/`, `s`, `g` | Search, sort, group | |
| `Tab`/`Shift+Tab`, `1`–`4` | Switch tab | |
| `+` | Start a new agent | Running tab |
| `Ctrl+R` / `F5` | Reload and check workers | |
| `z` | Pause live updates | |
| `o`, `d`, `t`, `f` | Show the request in the transcript, the full diff, the full transcript, follow | Detail view |
| `?` | Every key | |
| `q`, `Esc` | Leave the hub (Esc first clears a search, marks, a peek or the detail view) | |

With `[ui] mouse = "on"` (or `"auto"`, which applies to the full-screen hub
only) a click selects a row or tab, a second click opens it, the wheel scrolls
and a click on a footer hint runs it.

### Live updates and maintenance

The hub paints its frame immediately and loads rows through a bounded
background service; navigation and operations never wait for registry reads,
process checks, artifact moves or journal parsing. Rows, counts and search are
queries against the derived SQLite index (`index.sqlite3`): a reader applies
only the events appended since the index's recorded position, and the event
log remains authoritative. When the index has to be built from the log — its
first use, or after corruption — a progress line shows and the Running tab
lists recent agents read from the end of the log; leaving the hub cancels the
rebuild. If a read fails the hub says so, with Ctrl+R to retry; automatic
retries back off. Without a terminal, `agenthicc agents` prints the tab as
plain lines (ID, status, title, project, age).

The first rows come first. Everything else the hub shows is read just after
them: the next scheduled runs, saved sessions (which complete the History
count) and, in a session, agent definitions; the shell reads definitions when
the Library tab is first shown, and its tab label has no count until then. The
first maintenance pass starts at the same point, so a store busy with process
checks never delays the list. Inside a session the hub's store and worker
threads are kept from its first opening until the session ends, and its
modules are imported in the background once the agent runtime is ready, so
`/agents`, Ctrl+G and ← open it without setup work.
`scripts/benchmark_agents_manager.py` measures an opening as `overlay_open`.

The hub does not poll. It sleeps until a key arrives or something it watches
changes: the registry log `events.jsonl`, the `input-inbox/` directory, the
opened or peeked agent's `conversation.jsonl`, or a worker process exiting. A
change made by any process reaches the open hub within a fraction of a second,
and an idle hub uses effectively no CPU. On Linux it uses inotify and
`pidfd_open`; on macOS and BSD, kqueue; elsewhere it stats the watched files
every `refresh_interval_s`. A slower stat sweep runs alongside native
notification as a safety net for network filesystems. A worktree agent's
change summary is re-read at most every five seconds while it is open.

Maintenance (stale workers, deferred input, queue dispatch) also runs only
when something is due:

| Concern | When it runs |
|---|---|
| A worker process exits without recording its result | At once: the session is marked orphaned |
| A heartbeat is older than `stale_after_s` | When the deadline passes: the worker is probed once and flagged stale, or orphaned if it is gone |
| A new or restarted worker | Probed once when first seen, then watched for exit |
| A worker whose exit cannot be watched (no pidfd or kqueue) | Probed every `maintenance_interval_s` |
| Accepted deferred input is waiting | When its inbox file changes or a worker slot frees; a failed start retries with exponential backoff |
| Queued requests | When the registry changes, and when a claimed reservation's lease expires |

The first pass, when the first rows are ready, and Ctrl+R reconcile every
active session. With nothing due, maintenance probes no process and writes
nothing.

### Attach, switch and delete

Enter in the detail view attaches the agent in the foreground. The hub
validates the exact session ID against the durable store first; a deleted or
missing agent is rejected in place, and a validation read never stops or
foregrounds a worker. Foregrounding is an ownership handoff, not a second
observer: for an active agent the worker and its owned terminals are stopped,
the worker lease is verified to have ended, and the normal TUI opens with the
same `session_id`, project directory and durable journals. Completed, failed,
cancelled and orphaned agents open directly through the same resume path.

Inside a session the hub asks first — `Switch to … ? This session reopens when
you leave it.` — then closes the current session, opens the chosen agent or
saved session, and reopens the original session when that one ends. Switching
waits for a running turn to finish or be stopped. After `/background` the
session hands over to its worker and the hub opens with the new background
agent selected.

Delete is asynchronous and recoverable. `Ctrl+X` immediately captures the
selected or marked agents and starts one durable operation; no confirmation is
asked. Active work is cancelled first, then only the exact session directory
and its sibling kernel journal are moved to `~/.agenthicc/background/trash/`.
The tombstone remains in the append-only registry, so a stale worker cannot
resurrect it. Deleted agents leave the hub; `agenthicc agents restore
SESSION_ID` brings one back. Deletion runs off the event loop, so the hub stays
responsive; a repeated `Ctrl+X` is ignored while an operation runs, and
failures stay visible with durable retry metadata.

## Scriptable control

```bash
uv run agenthicc agents --json
uv run agenthicc agents list --json
uv run agenthicc agents status SESSION_ID --json
uv run agenthicc agents cancel SESSION_ID
uv run agenthicc agents resume SESSION_ID
uv run agenthicc agents retry SESSION_ID
uv run agenthicc agents approve SESSION_ID
uv run agenthicc agents archive SESSION_ID
uv run agenthicc agents delete SESSION_ID
uv run agenthicc agents restore SESSION_ID
```

`agenthicc agents list` prints statuses with the same glyphs and labels as the
hub; its JSON output, and `agenthicc agents --json`, are complete and keep the
raw `status` values.

JSON status removes the original intent and lease token and applies the same
secret-pattern redaction used by session inspection. A missing or invalid
transition is reported as a failed control operation. On restart, durable
pending queue records are eligible for dispatch; an interrupted running attempt
is reconciled as orphaned and requires explicit resume/retry. Terminal records
are never resubmitted automatically.

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
queue_enabled = false       # opt in to durable queued admission
max_queue_length = 100
max_workers_per_user = 4    # optional; omit for no per-user cap
priority_aging_interval_s = 900.0
dispatch_lease_s = 30.0
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
activity_tail_bytes = 64000
frame_debounce_ms = 16
max_in_flight_operations = 4
metrics = false
```

Queueing remains disabled by default until explicitly enabled. Other defaults
are conservative. Invalid or out-of-range values fail closed before a worker
is created. `--set background.max_workers=1` is supported for one invocation, and
`AGENTHICC_DISABLE_BACKGROUND=1` is an emergency local disable switch.
`[background.manager]` configures the agents hub (`AgentsHubSettings`). Its
settings are independently validated and bounded; they control only display
and concurrency cadence, not locking, ownership, or durable event recording.
`refresh_interval_s` is how often the hub stats the files it
watches where native change notification is unavailable; `maintenance_interval_s`
is how often it probes a worker whose exit it cannot watch (and, without
inotify, rescans the input inbox). `stale_after_s` is the heartbeat age after
which a worker is probed and flagged stale. Set `metrics = true` to collect
redacted timing aggregates while diagnosing latency.

### Background notifications and webhooks

Notifications are disabled by default. Configure operator-owned webhook
destinations in `~/.agenthicc/agenthicc.toml` (or pass an explicitly selected
trusted file with `--config`); project configuration may enable local notices,
but cannot register webhook targets or grant access to private network
addresses. Webhook secrets must use a dedicated environment-variable name
beginning with `AGENTHICC_WEBHOOK_`, separate from provider and session-service
credentials:

```toml
[background.notifications]
local_enabled = true
local_backend = "auto" # auto | desktop | terminal
request_timeout_s = 5.0
max_attempts = 5
retry_base_delay_s = 1.0
retry_max_delay_s = 300.0
retry_rate_limits = false

[background.notifications.quiet_hours]
start = "22:00"
end = "07:00"

[[background.notifications.webhooks]]
name = "build-alerts"
url = "https://hooks.example.test/agenthicc" # HTTPS; no query or credentials
secret_env = "AGENTHICC_WEBHOOK_BUILD_ALERTS"
events = ["completed", "failed", "waiting_approval", "waiting_input", "recovery_required"]
```

Set the secret outside the TOML file, for example
`export AGENTHICC_WEBHOOK_BUILD_ALERTS='…'`. The signature is HMAC-SHA256 over
`<timestamp>.<exact compact JSON body>` and is sent in
`X-Agenthicc-Signature: sha256=…`; the timestamp and event ID are separate
headers. Receivers should reject stale timestamps and deduplicate event IDs.
Payloads contain only the session/run IDs, workspace directory name, resulting
state, event time, and a fixed redacted summary—not the prompt, title, provider,
tool data, exception text, or absolute workspace path.

Private, loopback, and link-local webhook addresses are rejected by default.
To allow them, an operator must set `allow_private_network = true` in the
user-global notification configuration (or in the explicitly selected trusted
`--config` file). A project-local file or `--set` cannot grant this permission.
DNS is resolved and checked on every delivery, and redirects are never followed.

The append-only background journal records the redacted event descriptor
before the SQLite outbox is updated; startup reconciliation repairs a crash
between those writes. The sender runs separately from the job worker, retries
only bounded transient failures, and cannot change the job's lifecycle state.
Use `agenthicc agents notifications [--status pending|sending|delivered|failed]
[--session-id ID] [--json]` to inspect receipts. The command omits webhook
URLs, secret references, and payloads. Headless environments without a local
notification service quietly record `local_notifier_unavailable`.

During an interactive TUI session, local lifecycle notices are routed through
the foreground renderer rather than written as visible text to stdout or
stderr. The TUI shows coalesced badges and toasts; `/dnd` suppresses those
notices without hiding counts. Local system notifications are sent only while
the terminal is unfocused. `local_backend = "terminal"` uses OSC 9 on supported
terminals, and `auto` falls back to OSC 9 when no platform notifier is
available. `[background.notifications.quiet_hours]` suppresses local attention
notices during the configured recurring local-time window.

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
redacted. Active worker records found after a manager restart become
`orphaned` diagnostics rather than being reported as successful or silently
relaunched. Queue-pending session records are the exception: their accepted
request is recovered by the background scheduler, not treated as a dead worker.

## Verification

Run the full repository tests plus the PRD-141 maintained-surface gate with:

```bash
uv run python -m agenthicc.background.coverage_gate
```

The gate enforces at least 90% coverage across the background package, the
`agenthicc agents` commands, and the agents hub. The ordinary
full-package coverage report remains useful for tracking unrelated legacy and
platform-specific surfaces.

## Try it

Background agents are inspectable without opening the hub:

```bash
PYTHONPATH=src python -m agenthicc agents list --json
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
PYTHONPATH=src python -m agenthicc agents --help
```

```text
usage: agenthicc agents [-h] [--tab TAB] [--run RUN] [--json] [SESSION_ID]
```

Its subcommands are `list`, `status`, `cancel`, `resume`, `retry`, `priority`,
`approve`, `reject`, `input`, `inbox`, `notifications`, `rename`, `labels`,
`purge`, `archive`, `delete`, `restore`, `actions` and `action`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| An agent is stuck and no prompt is visible | It is parked on a typed approval/question | Open the hub's Needs you tab (`/inbox`, `agenthicc agents --tab needs-you`) or run `agenthicc agents inbox list`; answer by exact item ID/version |
| An agent needs an answer, not an approval | It is waiting on an input request | Press `i` on it in the hub, or `agenthicc agents inbox answer`; `agents input` answers a legacy request |
| A cancelled agent cannot be found | Cancelled work may have moved to the trash | `agenthicc agents list --trash` lists recoverable agents; `agents restore` brings one back |
| The History tab is cluttered with old entries | They have not been archived or purged | `A` (or `agents archive SESSION_ID`) archives one; `agents purge` removes expired trash deliberately |
| `agenthicc jobs` is not a command | It moved under `agenthicc agents` (AGX-05) | Use `agenthicc agents <subcommand>`; `agenthicc agents` alone opens the hub |
| `/bg` is missing from the command picker | It is injected outside `BUILTIN_COMMANDS` | `/background` and `/bg` come from the background integration; check that the integration loaded |
| A background job wrote to an unexpected place | `cwd` was relative at submit time | Pass an explicit working directory to `run --background`; inspect `artifact_dir` in the job record |
| An agent shows `attempt: 2` unexpectedly | It was retried | Compare with `agents status --json`; a retry after a cancellation may be intentional, so confirm before assuming a crash |
| A stale job is still listed as running | Stale detection is lease-based | Check the worker lease; a dead worker is reclaimed rather than silently reported as healthy |
| `/detach` says a session is already managed after `--continue` reclaimed it | The background lifecycle record can outlive the killed worker process | Retry `/detach`; it verifies process identity and the current foreground owner lease, then reconciles only the exact dead attempt. If the worker is live or its identity cannot be verified, inspect it in `agenthicc agents` and do not start a second worker |
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
