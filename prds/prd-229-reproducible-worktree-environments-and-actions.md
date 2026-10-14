---
title: "PRD-229: Reproducible Worktree Environments and Actions"
status: Implemented
version: 1.0.0
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

## 7. Implementation decisions and security boundaries

- The v1 manifest is `.agenthicc/environment.toml`; unknown schema keys are
  rejected so a misspelled security field cannot silently change behavior.
- Persistent trust is user-local and binds the exact SHA-256 manifest digest
  to the Git common directory. A per-dispatch/action `--approve` approval is
  available without persisting trust. A digest change always requires review.
- Setup is preflighted before allocating a worktree, then the worker checkout
  is checked for the same manifest digest before execution. Missing or
  divergent worktree manifests leave the task pending and preserve the
  worktree; setup is never silently skipped.
- Run receipts are bounded and stored on the durable background worker
  record. They include digest, command IDs, timestamps, result, and exit code;
  captured output and environment values are not persisted. Failed setup
  receipts are also retained by the orchestration worktree record when no
  worker session could be started.
- The environment runner uses bounded output capture and process-group
  termination on timeout, cancellation, and normal command exit (to prevent a
  shell from leaving unmanaged background children). Output redaction covers
  configured secret variables and credential-looking names.
- Setup and action execution are pinned to the manifest digest reviewed during
  preflight/preview, including an expected-absent manifest. The TUI preview
  uses the verified durable worktree path rather than trusting the session's
  mutable `cwd` projection.
- Cache behavior is opt-in: isolated per-worktree by default, explicit shared
  immutable caches, or explicitly serialized mutable sharing using an
  exclusive lock.
- Linux setup/actions run in Bubblewrap: the host filesystem is read-only,
  only the selected Git worktree and declared cache/temp paths are writable,
  and `network = "none_declared"` uses a network namespace without external
  interfaces. The runner fails closed if Bubblewrap is unavailable or cannot
  establish isolation. Other operating systems currently return
  `sandbox_unavailable`; no unrestricted fallback is used. This is not a
  container/privilege boundary against kernel vulnerabilities, and trusted
  commands may still read files accessible to the invoking user.
- Named actions are finite commands with the same trust, path, timeout,
  output, and provenance rules as setup. Persistent long-running services are
  not managed by this interface; use Agenthicc's background terminal/service
  mechanisms for that lifecycle.
- Before an action is offered, the durable worktree path and branch are checked
  against Git's registered worktree list, preventing tampered session metadata
  from selecting the coordinator checkout or an arbitrary sibling path.

## 8. Verification

Implementation coverage includes unit tests for parsing, invalid schemas,
manifest and worktree symlinks, digest invalidation, output redaction and
bounds, filesystem/network isolation, timeouts/cancellation, cache sharing,
and the `agents` review-before-run interaction; integration tests for dispatch
setup, preflight denial, manifest mismatch, action execution, and durable
receipt recovery; and CLI E2E tests for trust preview and digest-bound
confirmation. See
[`docs/guides/worktree-environments.md`](../docs/guides/worktree-environments.md)
for user-facing setup and operations.
