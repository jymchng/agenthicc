---
title: "SUBAGENT-15: Parallel implementation in isolated git worktrees"
status: Implemented (2026-11-22)
version: 0.1.0
date: 2026-11-22
scope: "where parallel workers that can change files run: one git worktree per worker, based on a snapshot of the parent's working state, with each worker's result applied back to the parent's workspace atomically as each worker finishes, conflicts reported with their branch preserved, and the worktree diff used as the authoritative record of what changed"
area: A-10
related_prds:
  - SUBAGENT-11
  - SUBAGENT-12
  - SUBAGENT-13
  - SUBAGENT-14
  - PRD-203
  - PRD-124
tags:
  - subagents
  - worktrees
  - parallelism
  - git
  - isolation
---

# SUBAGENT-15: Parallel implementation in isolated git worktrees

## Implementation status (2026-11-22)

Implemented as specified, with real-git tests in
`tests/unit/test_subagent_isolation.py` (snapshot, worktrees, patch integration)
and `tests/unit/test_subagent_worktrees.py` (the pool and workers end to end).
Choices the implementation made where the design left room:

* The pool snapshots, creates and cleans up (`SubagentPool._prepare_isolation`,
  `_release_isolation`); the worker binds its own policy, harvests and integrates
  (`SubagentWorker.bind_workspace`, `_harvest`, `_integrate`).
* A worker whose type narrows `permissions.workspace_paths` runs shared, because
  a narrowed scope cannot be reproduced inside a worktree.
* Extra roots the session granted are kept in the worker's scope unless they lie
  inside the repository, since those would let a worker write past its worktree.
* Isolation binds the workspace policy, not a sandbox: a Yolo worker handed an
  absolute path inside the repository can still write there. The guide says so.

## 1. Executive summary

The point of a subagent pool is to hasten an implementation by running its parts
in parallel, as Claude Code's subagents do. SUBAGENT-12 makes a working
subagent behave like the main agent — every tool, the session's turn limit —
which is exactly what makes parallel **mutating** workers dangerous in one
shared directory. Measured against the current code (§3.1): two implementers
that edit the same file both report `✓`, and one worker's change is silently
lost.

This PRD runs each working worker in its **own git worktree**:

* the worktree starts from a **snapshot of the parent's current working state**
  (uncommitted and untracked work included), so a worker sees what the parent
  sees;
* the worker's tools — files, commands, git — all resolve their root to that
  worktree through the one existing mechanism (`current_workspace_access()`);
* when the pool finishes, each successful worker's changes are applied back to
  the parent's workspace **atomically, as it finishes** as a patch; a patch
  that does not apply is reported as a conflict and its branch is preserved;
* the worktree's git diff — not the tool hooks — is the authoritative list of
  what changed, so a change made through `run_bash` is counted too.

## 2. Problem statement

Parallel workers sharing one directory fail in three ways, all invisible to the
parent today:

1. **Lost updates.** Concurrent writes to one path race; the last wins; every
   worker reports success (§3.1).
2. **Torn reads.** A worker running `run_tests` sees another worker's
   half-finished edits and reports failures it did not cause, or passes
   against code that will be overwritten.
3. **Unattributed changes.** A worker that changes files through a command
   (`run_bash`, a formatter, a code generator) leaves no `changed_paths`;
   SUBAGENT-12's evidence hook only sees file-tool calls.

## 3. Current-source investigation

### 3.1 Measured: a lost update reported as success

Two `implementer` workers, `max_concurrent=2`, each told to make `f` return a
different value in `shared.py`, scripted so each issues a `write_file`:

```
ok: True  succeeded: 2  failed: 0
final shared.py:  def f(): |     return 'worker-B'
```

Worker A's change is gone; both workers' results say `✓`.

### 3.2 What exists and what does not

* `agenthicc.worktrees` already provides `WorktreeManager`
  (`create`, `inspect`, `complete`, `integrate`, `rebase`, `remove`),
  `ManifestStore` and `ParallelCoordinator`, used by the **durable**
  `spawn_worker_agents` tool, which starts whole background Agenthicc sessions.
  `spawn_subagents` pool workers are lightweight in-process turns and use none
  of it.
* `WorktreeManager.create` calls `ensure_clean()` and bases the worktree on
  `HEAD`. A parent session usually has uncommitted work, so (a) `create` would
  raise, and (b) a worktree based on `HEAD` would start without that work.
* `WorktreeManager.integrate` runs `git merge` in the repository and also
  requires a clean tree. It is the wrong merge-back for a parent that holds
  uncommitted changes.
* Every tool family derives its root from one task-local contextvar:
  `fs/agent_tools.py`, `exec/agent_tools.py` and `git/agent_tools.py` each build
  `workspace_root = current_workspace_access().scope.primary_root`. The pool
  creates each worker as its own asyncio task, so a worker can bind its own
  policy without affecting its siblings or the parent. **No tool needs to
  change.**
* Worker `ApprovalGate` takes a `WorkspaceAccessPolicy`; the worker already
  builds a child policy (`_make_child_workspace_access`) that must be rebuilt
  over the worktree scope.

## 4. Goals

* **G1.** Working workers that run in parallel cannot overwrite or observe one
  another's in-progress changes.
* **G2.** A worker starts from the parent's working state, uncommitted work
  included.
* **G3.** A worker's result reaches the parent's workspace without a merge
  commit on the parent's branch and without disturbing the parent's own
  uncommitted changes.
* **G4.** A change that cannot be applied is reported, not dropped, and remains
  recoverable.
* **G5.** What a worker changed is read from git, so command-made changes count
  as evidence and appear in `changed_paths`.
* **G6.** The parent is told how to use this: give each worker disjoint files
  and a self-contained task.

## 5. Design

### 5.1 When isolation applies

`subagents.isolation` (operator setting, never a model argument — PRD-197):

| Value | Behaviour |
|---|---|
| `auto` (default) | isolate every **working** worker when the pool has ≥ 2 of them and the workspace is inside a git work tree; otherwise run shared |
| `worktree` | isolate every working worker, even one; refuse the pool at admission when the workspace is not a git repository |
| `shared` | today's behaviour |

A worker is *working* when its effective tool surface (SUBAGENT-13
`type_surface`) holds a usable **file-changing** tool: a workspace-mutating tool
the session policy does not block. Investigative workers never get a worktree.
Command-only roles (`reviewer`, `verifier`, which hold `run_python_expr` /
`run_tests`) are deliberately *not* working: they check the parent's real tree,
and a worktree lacks the git-ignored files (`.venv`, build output) their checks
depend on, so isolating them would manufacture failures. A custom type that can
only run commands is likewise shared. A type that narrows
`permissions.workspace_paths` is shared too, since a narrowed scope cannot be
reproduced inside a worktree. When `auto` leaves ≥ 2 working
workers shared because there is no git repository, the digest says so in one
line, so the parent knows collisions are possible.

### 5.2 The base snapshot

Once per pool, `WorktreeManager.snapshot_commit()` builds a commit of the
parent's current working state **without touching the parent's index or tree**:
a temporary `GIT_INDEX_FILE`, `read-tree HEAD`, `add -A` (untracked,
non-ignored files included), `write-tree`, `commit-tree -p HEAD`, with a fixed
`agenthicc` author/committer identity. A clean tree yields `HEAD` itself. Every
worker of the pool starts from this one immutable base.

### 5.3 Creating the worktrees

`WorktreeManager.create` gains `require_clean: bool = True`; the subagent path
passes `False` (its base is a snapshot, not `HEAD`). Existing callers
(`ParallelCoordinator`) keep the default, unchanged. Creation is sequential,
runs in `asyncio.to_thread` (git is blocking and the TUI loop must not stall),
and a failure creates none of the pool's workers and is reported as an
admission failure naming the git error.

If the workspace root is a subdirectory of the git top level, the worker's scope
is `<worktree>/<same relative subdirectory>`.

### 5.4 Binding a worker to its worktree

Inside the worker's own task, before its first tool call, the worker builds
`WorkspaceScope.create(<worktree>[/subdir])`, a child `WorkspaceAccessPolicy`
over it (same mode provider and approval service as today), assigns it to the
worker's `ApprovalGate`, and calls `set_current_workspace_access(policy)`. The
system prompt's policy section is unchanged; a one-line `## WORKSPACE` section
tells the worker its working directory is an isolated copy and that its changes
are applied back when it finishes.

### 5.5 Harvest

After a worker's run, in a thread: `git add -A` and, if anything is staged, one
commit on the worker's branch. `git diff --name-status <base> HEAD` is the
worker's **change set**:

* `SubagentResult.changed_paths` is that set (repository-relative);
* a non-empty change set is *success evidence* for SUBAGENT-12's rule, whatever
  tool made it;
* an empty change set with a required evidence rule and no observed call is the
  same `missing_mutation` / `missing_action` failure as today.

### 5.6 Applying results

Each worker is integrated **inside its own `run()`**, before its result is
returned, so the panel, the `worker_done` event, the journal and the digest all
carry the final outcome (a deferred "apply everything at the end" step would
publish `ok=True` and contradict it afterwards). Integration of different
workers is serialized by one lock:

```
git diff --binary <base> <worker-HEAD>   |   git apply --whitespace=nowarn   (in the parent repo root)
```

`git apply` is atomic per patch: it applies all of it or none of it, and does
not need a clean tree, so the parent's uncommitted work is preserved. Because
each patch applies on top of whatever earlier workers already applied, edits to
*different parts* of one file compose and only genuine overlaps conflict. When
two workers do overlap, the one that **finishes first** is applied and the other
is reported as a conflict (completion order, not task order: the cost of
truthful live events).

| Outcome | `integration` | Worktree/branch |
|---|---|---|
| applied | `applied` | removed |
| patch rejected | `conflict` | **kept**; `branch` and `workspace` reported |
| worker failed, has changes | `kept` | **kept** |
| no changes | `none` | removed |

A conflicting worker's `ok` becomes `False` with
`error_code="integration_conflict"` and the message names the conflicting paths
and where the work is preserved, so the parent can re-run the task against the
new state or merge the branch by hand. A retry of a worker (SUBAGENT-08) runs
in the parent's workspace, not a fresh worktree.

### 5.7 Cleanup

The pool's `finally` removes every worktree and branch it created that was not
preserved above — including on cancellation, `stop`, timeout and parent turn
cancellation. A crash can orphan a worktree; `WorktreeManager.recover()` already
lists them and the guide documents `git worktree prune`.

### 5.8 What the parent is told

* `SubagentResult` gains `integration`, `branch`, `workspace`.
* SUBAGENT-14's footer adds `applied to your workspace`, or
  `NOT applied: conflict in a.py — kept on branch X at PATH`.
* The `spawn_subagents` description gains: *Delegate independent work in
  parallel. Workers cannot see this conversation: put file paths, interfaces and
  acceptance checks in `task`/`context`. Give each worker different files — when
  more than one can change files, each runs in its own git worktree and its
  changes are applied to your workspace when the pool finishes; overlapping
  edits are reported as conflicts.*

## 6. Non-goals

* No merge commit, rebase or checkout on the parent's branch.
* No per-call isolation argument for the model.
* No environment bootstrap (virtualenv, `node_modules`) inside worktrees:
  git-ignored directories are not in a worktree. A worker that runs tests
  there uses the project's normal on-demand tooling (`uv run`); a trusted
  `WorktreeEnvironment` manifest hook is future work and called out in the
  guide.
* No live view of a worker's edits in the parent's tree while it runs — that is
  the point of isolation.
* No isolation of investigative workers, nor change to `spawn_worker_agents`.

## 7. Acceptance criteria

All use real git repositories under `tmp_path`.

1. Two parallel implementers editing **different** files: both files exist in
   the parent workspace afterwards; both results `ok`, `integration="applied"`;
   no worktree or branch remains (`git worktree list`, `git branch`).
2. Two parallel implementers editing the **same lines** of one file: the first
   to finish is applied; the second is `ok=False`,
   `error_code="integration_conflict"`, `integration="conflict"`; its branch and
   worktree exist and contain its edit; the parent's file holds the first
   worker's edit only; the digest names the path and the branch.
3. Two implementers editing **different regions** of one file: both edits are in
   the parent's file.
4. The parent has an uncommitted modification and an untracked file; a worker
   reads both from its worktree; after the pool the parent's uncommitted
   modification and untracked file are intact.
5. A worker that creates a file only through `run_bash` has that file in
   `changed_paths` and satisfies `completion_evidence="action"`.
6. A failed worker with changes is `integration="kept"` and preserved; a failed
   worker without changes leaves nothing behind.
7. Cancelling the pool mid-run leaves no worktree or branch.
8. `auto` with one working worker and with a non-git workspace runs shared and
   creates no worktree; `worktree` with a non-git workspace is refused at
   admission.
9. Investigative workers never get a worktree.
10. The parent's index and `HEAD` are unchanged by a pool run (`git rev-parse
    HEAD`, `git diff --cached` identical before and after).
11. The `spawn_subagents` description contains the parallel-work guidance.
12. `WorktreeManager.create` with its default arguments still raises on a dirty
    repository (existing behaviour, existing tests).

## 8. Verification

`tests/unit/test_subagent_isolation.py` (real `git`, scripted `MockTransport`),
extended `tests/unit/test_worktree_manager*.py` for `snapshot_commit` and
`require_clean`. `ruff`, `mypy`, `type_audit`, the unit/integration/e2e
subagent suites.

## 9. Related PRDs

* PRD-203 provides `WorktreeManager`; this PRD reuses it and extends it only
  with `snapshot_commit` and `require_clean`.
* SUBAGENT-12 supplies "behave like the main agent" and the evidence rule this
  PRD feeds from git; SUBAGENT-14 reports the integration outcome.
