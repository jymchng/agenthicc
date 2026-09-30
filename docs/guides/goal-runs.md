# Goal runs and detached orchestration

PRD-204 adds a durable product-level identity around Agenthicc work. A goal
run is not a second agent loop: it links one normal Agenthicc main session to
its workflow checkpoint, optional PRD-203 worker sessions, Git worktrees, and
verification state.

## Start a goal

Run a goal in the normal TUI:

```bash
uv run agenthicc --goal "Add rate limiting to the public API"
```

The goal is persisted before the first provider call and submitted once as the
initial user message. `--goal DESCRIPTION` is deliberately the same operation
as selecting `/workflow goal_flow` and submitting `DESCRIPTION`. It cannot be
combined with `--workflow`; use the ordinary `--workflow` option when starting
a non-goal workflow without the goal-run lifecycle.

## Detach a goal

Use `--detach` to return as soon as the existing background supervisor has
accepted the main session:

```bash
uv run agenthicc --goal "Implement OAuth and its tests" --detach
uv run agenthicc --mode Yolo --goal "Implement OAuth and its tests" --detach
```

The command's success means that durable execution was accepted; it does not
mean the provider, workflow, or workers have completed. Use `--json` for a
bounded machine-readable startup response:

```bash
uv run agenthicc --json --goal "Implement OAuth" --detach
```

An explicit `--mode MODE` is preserved in the durable worker request and
applied through normal session initialization. The background session detail
view reports the resulting canonical mode. On resume, the stored mode remains
authoritative unless that resume invocation supplies a new explicit `--mode`.

Detached startup prints the PID of the child worker, not the short-lived CLI
launcher:

```text
Run ID:  run_...
Session ID: <session-id>
PID:     <worker-pid>
```

The JSON response contains both `pid` and `worker_pid` for compatibility. The
same PID is retained in `runs show`, the `agents --run` projection, and the
background-session record after the worker exits. It is correlation metadata;
the run/session status and lease remain authoritative, so a PID must never be
used by a client to kill an unrelated process.

Recovery is fail-closed against PID reuse: on platforms with procfs, the
recorded PID must still expose the Agenthicc worker module together with the
exact session request file and background-store root. A merely live numeric
PID is reported for reconciliation rather than accepted as proof of ownership.

The detached worker owns a bounded finalizer. After `goal_flow` completes or
reaches an irrecoverable failure, it persists the result, phase history, exit
code, exit reason, and worker-exit event, closes its owned resources, releases
the session lease, and returns from the worker entry point. Typical reasons
are `workflow_complete`, `recoverable_error`, `irrecoverable_error`, `cancelled`, and
`idle_after_thinking`, or `cleanup_timeout` when resource closure reaches its
bounded deadline. Finalization is idempotent and does not delete journals,
checkpoints, logs, or worktrees.

The `idle_after_thinking` reason is only available to a detached worker after
a canonical active-turn-to-`IDLE` boundary with no pending question/approval,
tool, retry, compaction, continuation, child worker, or active workflow phase.
An attached TUI never installs this finalizer: `agenthicc --goal "..."`
without `--detach` remains interactive and may accept another message after a
turn returns to idle.

The run ID is independent of the main session ID, workflow run ID, worker IDs,
and Git branch names.

## Inspect and control runs

```bash
uv run agenthicc runs
uv run agenthicc runs list --status running --json
uv run agenthicc runs show RUN_ID --json
uv run agenthicc agents --run RUN_ID
uv run agenthicc agents --run RUN_ID --json
uv run agenthicc runs cancel RUN_ID
uv run agenthicc runs resume RUN_ID
uv run agenthicc attach --run RUN_ID
```

`runs` is the goal-level projection. `jobs` remains the lower-level
background-session manager, and `agents` without `--run` continues to open the
existing interactive background manager.

Run inspection reconciles the main background session before rendering it.
Missing processes, stale sessions, waiting approvals/input, and failures are
shown as attention states rather than being reported indefinitely as healthy
running work.

If a detached worker disappears before its terminal metadata is durable, the
background recovery path marks the session lost/orphaned for explicit resume
or inspection. It never starts a second worker solely because a persisted PID
is stale or reused.

## Foreground handoff

Inside an active run, `/detach` (also `/background` and `/bg`) hands the same
session to the existing `BackgroundSupervisor`. The handoff persists the
journal/checkpoint boundary and releases the foreground owner before the
background worker becomes the sole owner. It does not create a second
conversation or session.

`agenthicc attach --run RUN_ID` performs the reverse operation: it stops the
background worker, claims the existing session lease, and opens the normal TUI
with the same transcript and workflow checkpoint. A live owner conflict is
reported rather than starting a second writer.

## Worker agents

Every `spawn_worker_agents` invocation remains governed by PRD-203. Workers
have separate sessions, branches, worktrees, immutable base commits, and
background leases. The parent run projection links workers through their
`parent_session_id` and exposes their task/worktree/Git metadata under
`agents --run` and `runs show`.

Workers never write the coordinator worktree directly. Integration remains a
serialized coordinator operation; conflicts preserve the worker worktree for
inspection and recovery.

## Storage and privacy

Goal events are stored in an append-only, fsync'd JSONL registry below:

```text
~/.agenthicc/runs/events.jsonl
```

The registry is local and crash-tolerant. It stores IDs, bounded summaries,
statuses, timestamps, and Git metadata. It never stores resolved API keys,
secret headers, complete environment maps, or provider request bodies. The
existing background, session, workflow, and worktree stores remain the
authorities for their own state.

The durable boundaries are intentionally distinct:

```text
run state       ≠ process state
session state   ≠ run state
TUI state       ≠ durable orchestration state
Git evidence    ≠ agent prose
```
