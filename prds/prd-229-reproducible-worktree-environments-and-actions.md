---
title: "PRD-229: Reproducible Worktree Environments and Actions"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-203, PRD-223, PRD-228]
tags: [worktrees, environments, setup, reproducibility]
---

# PRD-229 — Reproducible Worktree Environments and Actions

## 1. Summary

Define a project-scoped, reviewable environment manifest for preparing
Agenthicc-created worktrees and exposing common actions such as test, lint, and
dev-server commands. Each action runs in the exact worker workspace with
bounded execution and existing tool authorization.

## 2. Problem and research basis

Agenthicc can create isolated Git worktrees, but fresh worktrees may lack
dependencies, generated files, or project-specific setup. Codex local
environments document setup scripts run on worktree creation and reusable
actions for common commands. Agenthicc needs a safer explicit contract rather
than assuming every clone is immediately runnable.

Sources: [Codex local environments](https://learn.chatgpt.com/docs/environments/local-environment),
[Codex worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees),
[Agenthicc worktree isolation](prd-203-parallel-coding-git-worktree-isolation.md).

## 3. Goals and non-goals

Goals: deterministic setup, reusable named actions, environment provenance,
safe project trust, and understandable setup failures.

Non-goals: container orchestration, secret distribution to workers, or
automatically running untrusted project scripts on first discovery.

## 4. Requirements

1. Define a versioned manifest under `.agenthicc/` with named setup steps and
   actions, working directory, timeout, declared environment-variable names,
   expected outputs, and optional platform conditions.
2. Setup/action commands are data, not direct permission grants. On first use,
   display the exact command, workspace, network implication, and timeout;
   require project trust or per-run approval before execution.
3. Execute setup only after the worktree path is resolved and authorized.
   Resolve symlinks, prevent escaping the worktree, cap output, terminate
   process groups on timeout/cancel, and redact configured secrets.
4. Record manifest digest, command IDs, start/end, exit code, and setup result
   in the worker run, not full environment contents or secret values.
5. Support cache reuse only for explicitly declared immutable/shared caches;
   prohibit concurrent sharing of mutable build outputs unless the project
   manifest declares a safe isolation strategy.
6. Provide actions from `agenthicc agents`/worker details and CLI, but executing
   an action must not mutate another worktree or the coordinator checkout.

## 5. Acceptance criteria

- A clean worker worktree can install/build using a trusted setup manifest and
  produce the same declared readiness result on repeat runs.
- An untrusted or changed manifest does not execute automatically; digest
  changes require review.
- Timeout, cancellation, output truncation, missing dependency, platform
  mismatch, and path escape are surfaced with actionable diagnostics.
- Two concurrent workers cannot corrupt one another through shared mutable
  artifacts under the default policy.
- Commands and secrets are not leaked into session titles, status projections,
  or notification payloads.

## 6. Testing and dependencies

Use temporary Git repositories, fake setup commands, path/symlink attacks,
timeouts, secrets, and parallel workers. Requires PRD-203 worktrees; scheduled
mutating runs in PRD-228 depend on this manifest for reproducible setup.
