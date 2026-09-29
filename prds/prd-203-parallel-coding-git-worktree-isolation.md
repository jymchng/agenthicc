---
title: "PRD-203: Parallel coding with isolated Git worktrees"
status: Implemented
---

# PRD-203 — Parallel coding with isolated Git worktrees

## Summary

Add a production-safe orchestration boundary for splitting one coding request
among multiple worker agents. Every worker receives one durable identity, one
Git branch, and one Git worktree created from the same immutable coordinator
commit. Workers run through the existing background-session supervisor. The
parent session reviews machine-derived Git evidence and is the only component
allowed to integrate changes into its worktree.

This implementation uses the repository's existing WorkflowPlugin,
WorkflowRunHandle, BackgroundStore, and BackgroundSupervisor boundaries. It
does not create a second agent loop, session journal, or process manager.

## Problem and goals

Running several agents in one checkout creates races, branch switching,
unreviewed changes, and ambiguous recovery after a crash. Conversational
claims such as “worker complete” are insufficient evidence for integration.

The feature must:

1. preserve parent/worker/task/worktree identity across process restarts;
2. keep worker filesystem and branch operations isolated from the coordinator;
3. derive completion evidence from Git rather than worker prose;
4. make merge and conflict outcomes explicit and recoverable;
5. reuse existing durable background jobs and workflow checkpoints;
6. expose enough state in the CLI and TUI for inspection;
7. preserve completed but unintegrated work until an explicit cleanup action.

## User experience

parallel_code_plan is a built-in Plan workflow:

    PLAN → DECOMPOSE → DISPATCH → COLLECT → INTEGRATE → VERIFY → SUMMARIZE

The agent uses phase control tools to create the task graph, dispatch ready
tasks, collect Git evidence, integrate each task, and mark completion. A
successful tool call is required for each phase boundary; prose never advances
the graph.

/workers shows the current session's durable orchestration/task table. The
CLI provides:

    agenthicc worktrees list
    agenthicc worktrees show <orchestration-id>
    agenthicc worktrees recover <orchestration-id> [--repository PATH]
    agenthicc worktrees integrate <orchestration-id> <task-id> [--repository PATH]

## Durable data model

ParallelManifest stores the coordinator session, repository, coordinator
branch and immutable base commit. Each ParallelTask stores its dependencies,
worker session, worktree, lifecycle status, and WorkerResult. Each
WorktreeRecord stores the exact path, branch, base commit, owner session,
dirty state, conflict paths, and status.

Manifests are JSON files under ~/.agenthicc/orchestrations/ (or an injected
test root). Writes are locked, written to mode-0600 temporary files, fsynced,
atomically replaced, and never silently deleted. The background session record
also carries worker metadata so existing job views remain the session
projection rather than a competing registry.

## Safety and invariants

- The coordinator must be clean before an orchestration or worker is created.
- A worker branch is named agenthicc/<parent>/<task>-<short-id>.
- The worker base commit is resolved and stored before its worktree is used.
- A worker prompt prohibits changing branches or accessing other worktrees.
- The coordinator worktree is modified only by integrate_task.
- Integration uses a normal merge first. A failed merge is aborted in the
  coordinator, while the worker worktree and branch remain available.
- Rebase is explicit and also preserves the worker on conflict.
- Removal is allowed only for a clean, integrated worker unless force cleanup
  is explicitly requested by a future administrative surface.
- Recovery compares durable records with git worktree list --porcelain and
  marks missing/untracked worktrees orphaned; it never guesses or deletes them.
- Worktree isolation is not a security sandbox. Worker sessions inherit the
  existing workspace, network, capability, approval, and provider policy.

## Checkpoint and restart flow

The workflow checkpoint contains bounded intent, plan, orchestration ID, next
phase, and iteration. It does not copy session memory, live supervisors,
locks, or provider clients. On resume, the existing session journal is
rehydrated and the orchestration ID is loaded from the manifest:

    parent checkpoint
      → same conversation/session memory
      → same orchestration_id
      → manifest + Git reconciliation
      → resume at the saved next phase
      → inspect existing workers; never recreate completed branches

The next phase is persisted after each successful boundary. A worker that
completed but was not integrated remains visible and recoverable after a
coordinator crash.

## Acceptance criteria

- A clean temporary repository can create two independent workers from one
  base commit and keep their changes isolated.
- Dirty coordinator state is rejected without modifying user files.
- Task dependencies prevent premature dispatch.
- Completion evidence includes base/head commits, commit range, changed files,
  and clean state.
- A successful merge marks the task integrated; cleanup removes only that
  integrated worktree and its branch.
- A merge conflict returns conflict paths, aborts the coordinator merge, and
  preserves the worker worktree.
- A manifest survives a fresh ManifestStore instance and can be reconciled
  against Git after a simulated restart.
- The workflow is discoverable in the built-in registry and its checkpoint
  codec excludes live resources.
- Existing background-session and full repository tests remain compatible.

## Non-goals and assumptions

The first implementation intentionally does not make worktrees a kernel
security boundary, does not infer arbitrary shell verification commands, and
does not delete stale worktrees automatically. Merge is the default strategy;
cherry-pick, squash, and rebase policies can be added without changing the
manifest identity contract.

