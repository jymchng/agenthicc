# Parallel coding workers

The parallel_code_plan workflow lets one coordinator split an implementation
request into independent tasks. Each task runs in a separate Git worktree and
branch, then the coordinator reviews and integrates the result.

## Available to every workflow

`spawn_worker_agents` is a session-bound tool injected by the shared
agent-turn runner. It is not exclusive to `parallel_code_plan`: built-in and
custom workflows receive the same tool whenever they execute in a normal
workspace-scoped Agenthicc session. A workflow can therefore delegate an
independent coding task without importing or reimplementing the parallel
workflow runner.

The tool accepts tasks with `task_id`, `description`, and a `dependencies` list
(use `[]` for an independent task), plus an optional concurrency limit. It
returns the durable orchestration ID, task IDs, base commit, and dispatch
state. The parent workflow remains responsible for inspecting, integrating,
verifying, and cleaning up the workers. Worker sessions receive only their
task description; the parent conversation is not copied into each worker.

Workers use the normal approval and capability policy by default. A workflow
must explicitly pass `dangerously_skip_permissions=true` when it has obtained
the appropriate approval for autonomous worker tool execution; the default is
deliberately safe.

## Start and inspect

Select the workflow in the TUI or run it headlessly:

    uv run agenthicc --workflow parallel_code_plan
    uv run agenthicc workflows run parallel_code_plan --intent 'implement the feature'

Inside the TUI, /workers displays the durable orchestration and task status.
The CLI surfaces the same manifest:

    agenthicc worktrees list
    agenthicc worktrees show ORCHESTRATION_ID
    agenthicc worktrees recover ORCHESTRATION_ID --repository .

## Lifecycle

The workflow advances only through its control tools:

1. PLAN obtains approval and records the decomposition strategy.
2. DECOMPOSE adds one task per independent unit and records dependencies.
3. DISPATCH creates agenthicc/<parent>/<task>-<id> branches and launches
   workers through the existing background-session supervisor.
4. COLLECT reads each worktree's Git base, head, commits, changed files, and
   clean state. Worker prose is not completion evidence.
5. INTEGRATE merges each completed worker into the coordinator branch.
6. VERIFY runs normal project checks in the coordinator and records the final
   summary.

One task owns exactly one worker session and one worktree. A dependency is
ready only after its prerequisite has completion evidence or has been
integrated. The coordinator's initial commit is immutable in the manifest; if
the coordinator branch changes unexpectedly, dispatch stops and asks for
reconciliation.

## Conflict and recovery

Integration uses a regular Git merge. When it conflicts, Agenthicc aborts the
coordinator merge and records the conflict paths. The worker branch and
worktree are intentionally preserved so the worker can be inspected or
rebased:

    worker completed
      → coordinator merge
      → conflict
      → coordinator merge --abort
      → worker remains available

worktrees recover compares the manifest with Git's authoritative worktree
list. Missing paths become orphaned; unexpected Git worktrees are reported
for review. Recovery never removes a worktree merely because its process is
gone. Only an explicitly integrated, clean task may be cleaned up by the
coordinator.

Worktrees provide repository isolation, not a security sandbox. Worker
sessions still use Agenthicc's capability, approval, workspace, network, and
provider controls.
