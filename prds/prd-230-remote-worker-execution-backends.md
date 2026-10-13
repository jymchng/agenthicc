---
title: "PRD-230: Pluggable Remote Worker Execution Backends"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P2
related_prds: [PRD-203, PRD-220, PRD-222, PRD-223, PRD-229]
tags: [remote-workers, ssh, execution, worktrees]
---

# PRD-230 — Pluggable Remote Worker Execution Backends

## 1. Summary

Allow a coordinator to dispatch a task to a registered Agenthicc worker host,
where the repository worktree and agent process live, then receive durable
status and reviewable results. Deliver a local backend and one remote backend
through a single `WorkerExecutor` contract; do not let the coordinator assume
the remote filesystem is locally mounted.

## 2. Problem and research basis

Local worktrees isolate concurrent workers but do not provide more CPU, a
long-lived machine, or execution near a private repository. Claude Code web
and Codex Cloud run tasks in isolated remote environments; Codex also lets a
remote client control a machine-hosted runtime. Agenthicc's server/session
boundary provides a path to remote worker hosts without tying the core to one
vendor cloud.

Sources: [Claude Code web](https://code.claude.com/docs/en/claude-code-on-the-web),
[Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments),
[Codex app-server](https://learn.chatgpt.com/docs/app-server).

## 3. Goals and non-goals

Goals: backend-neutral task dispatch, remote worktree ownership, streaming
progress, cancellation, reconnect, artifact/diff return, and explicit remote
trust boundaries.

Non-goals: Agenthicc-hosted multi-tenant cloud, arbitrary shell-over-SSH,
automatic credential forwarding, or cross-host shared filesystem assumptions.

## 4. Requirements

1. Define a typed `WorkerExecutor` protocol with submit, status, events,
   cancel, recover, inspect-result, and cleanup operations. Persist backend ID,
   remote task ID, host identity, and idempotency key per worker task.
2. Preserve the local executor as the default. MVP remote execution targets a
   separately installed Agenthicc server over authenticated HTTPS; SSH may be
   used only as an explicitly configured tunnel, not as an unbounded shell API.
3. Register remote workers with capability metadata: repository allowlist,
   supported runtimes, available concurrency, policy ceiling, and health.
   Reject dispatch if task requirements exceed declared capabilities.
4. Pin each remote task to an immutable base commit and isolated worktree.
   Return commit IDs, changed-file list, bounded diff/artifact bundle, test
   results, and a signed/validated completion receipt.
5. Apply server authentication scopes, workspace authorization, mode ceilings,
   setup trust, and per-worker resource limits. Never forward parent process
   environment wholesale; send only allowlisted variable references.
6. On network loss, retain the remote task and reconcile by stable task ID.
   Do not resubmit unless the remote idempotency lookup proves no task exists.
7. Remote completion does not merge changes or authorize publication; only the
   coordinator's integration workflow may do so.

## 5. Acceptance criteria

- A task dispatched to a second Agenthicc host runs in a remote worktree and
  its progress is visible to the coordinator.
- Coordinator restart and temporary network loss reconcile the same task,
  without duplicate remote sessions or loss of worker artifacts.
- Remote results are inspectable without mounting the remote filesystem.
- A remote worker cannot access an unlisted repository or inherit an
  unapproved credential.
- Local-only operation remains unchanged when no remote workers are
  registered.

## 6. Testing and dependencies

Use an in-process fake remote server for protocol tests and two isolated local
processes for E2E. Cover expired credentials, replay, wrong host identity,
network partition, cancellation race, result tampering, and remote cleanup.
Requires PRD-220/222/223 plus PRD-229 setup semantics; do not ship remote
dispatch before those are operational.
