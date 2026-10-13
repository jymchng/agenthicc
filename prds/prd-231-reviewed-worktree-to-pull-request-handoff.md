---
title: "PRD-231: Reviewed Worktree-to-Pull-Request Handoff"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-203, PRD-219, PRD-222, PRD-229, PRD-230]
tags: [git, review, pull-request, integration]
---

# PRD-231 — Reviewed Worktree-to-Pull-Request Handoff

## 1. Summary

Extend parallel/background task completion into a reviewable Git handoff:
inspect the worker diff and validation, request changes or approve, then
optionally push a branch and create a draft pull request. No agent task may
silently push, merge, or publish changes.

## 2. Problem and research basis

Worktree isolation provides safe execution, but users still need a consistent
path from a worker result to code review. Claude Code web creates pull
requests from remote tasks; Codex exposes Git diff review and PR actions in its
client. Agenthicc should preserve a human-owned integration boundary and make
publication an explicit operation.

Sources: [Claude Code web](https://support.claude.com/en/articles/12618689-claude-code-on-the-web),
[Codex local environment Git tools](https://learn.chatgpt.com/docs/environments/local-environment),
[Agenthicc worktree PRD](prd-203-parallel-coding-git-worktree-isolation.md).

## 3. Goals and non-goals

Goals: one worker review screen, verification evidence, request-changes loop,
branch publication, draft PR creation, and traceable cleanup.

Non-goals: automatic merge, mandatory GitHub dependency, autonomous public
posting, or treating a passing test command as proof of semantic correctness.

## 4. Requirements

1. Present immutable base/head commits, commit list, changed files, bounded
   unified diff, worker summary, test/lint/build evidence, and unresolved
   warnings before integration/publication.
2. Support reviewer decisions `approve`, `request_changes`, and `decline`.
   Request-changes creates a new worker instruction on the same task/worktree
   with an auditable revision; it must not overwrite the prior result record.
3. Before push, verify the source repository, branch, remote, base commit,
   clean worktree state, and credential availability. Show the exact remote
   and branch; require explicit user approval.
4. Create a draft PR only after an approved push. Use a provider adapter with
   GitHub as the first implementation; use existing credential managers/`gh`
   auth without reading or logging tokens.
5. PR title/body include task summary, test evidence, base/head, and Agenthicc
   run link/ID where available. Redact secrets and private local paths.
6. A failed push/PR operation is resumable and idempotent. It must not delete
   the worker worktree or branch. Close/merge PR actions remain explicit and
   separately authorized.

## 5. Acceptance criteria

- A user can inspect a worker result, request changes, approve, and create a
  draft PR without manually locating its worktree.
- No network write occurs before the specific push/PR confirmation.
- A rejected or conflicting worker remains recoverable with its branch intact.
- Repeating a create-PR action returns the existing matching PR rather than
  creating duplicates.
- Non-GitHub repositories retain the local review/integration path and report
  provider integration as unavailable rather than failing the task.

## 6. Testing and delivery

Use fake Git remotes and a mock provider API/CLI; test dirty worktree, changed
remote, permission denial, network failure, duplicate PR creation, and
redaction. The integration view should be available in `agenthicc agents` and
headless/service APIs with the same authorization. Requires PRD-203 and
PRD-222; PRD-230 enables remote workers but is not required for local use.
