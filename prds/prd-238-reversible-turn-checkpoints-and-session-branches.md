---
title: "PRD-238: Reversible Turn Checkpoints and Session Branches"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-138, PRD-139, PRD-150, PRD-169, PRD-182, PRD-203]
tags: [sessions, checkpoints, undo, fork, diff]
---

# PRD-238 — Reversible Turn Checkpoints and Session Branches

## 1. Summary

Capture a durable relationship between a user turn, the tools/file changes it
caused, and the resulting session state. Let users inspect a turn diff, revert
supported file changes, redo them, or fork a new session from an earlier
conversation point without destroying the original history.

## 2. Problem and research basis

Agenthicc has durable journals, event logs, workflow checkpoints, and Git
worktrees, but these do not yet provide one user-facing “what changed in this
turn?” and safe undo/fork contract. Claude Code documents rewind/checkpoint
actions; OpenCode documents session branching and revert; Codex worktrees
provide isolation for parallel alternatives. Agenthicc should prioritize
non-destructive branching and explicit file recovery.

Sources: [Claude Code checkpointing](https://code.claude.com/docs/en/checkpointing),
[OpenCode session/server docs](https://docs.opencode.ai/docs/server/),
[Codex worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees).

## 3. Goals and non-goals

Goals: per-turn change manifest, diff view, file-tool undo/redo, transcript
fork-at-event, and safe recovery under concurrent edits.

Non-goals: claiming that arbitrary shell/database/network side effects can be
undone, rewriting append-only audit history, or deleting the source session
when a user forks it.

## 4. Requirements

1. Record a turn boundary ID and before/after fingerprints for files changed by
   Agenthicc-controlled filesystem tools. Record command execution as
   potentially external/non-reversible unless it uses an explicitly supported
   transactional adapter.
2. Provide `/undo`, `/redo`, `/fork`, and `session fork --at EVENT_ID` over the
   canonical session service. Preview affected paths/diff and detect conflicts
   against edits made after the checkpoint.
3. Undo only when current file fingerprints match the expected post-turn
   state; otherwise refuse destructive overwrite and offer export/recovery
   comparison. New user edits after undo invalidate redo for conflicting
   paths.
4. Preserve append-only transcript/event history and write a compensating
   event describing undo/redo; never rewrite history to imply a tool did not
   happen.
5. A fork gets a new session ID and conversation identity, links to parent
   session/event, copies the appropriate transcript prefix and workflow
   artifacts/checkpoint only when topology-compatible, and does not inherit
   live ownership or pending approvals.
6. Store snapshots in a bounded, project-relative, permission-aware store with
   retention/size controls. Secrets and files outside authorized roots are
   excluded by default.

## 5. Acceptance criteria

- A simple file edit can be diffed, undone, redone, and resumed with correct
  transcript annotations.
- A post-turn manual edit causes a conflict warning and is never overwritten
  by undo.
- Forking an earlier turn creates an independent session while preserving the
  original and correctly marking incompatible workflow checkpoints.
- Arbitrary shell effects are labeled non-reversible; the UI does not promise
  rollback where no snapshot exists.
- Crash between file change and manifest write is detected/reconciled or
  surfaced as an incomplete transaction, not silently declared undoable.

## 6. Testing and dependencies

Add crash-injection filesystem transaction tests, fingerprint conflict tests,
undo/redo state-machine tests, fork/replay tests, checkpoint-topology tests,
and storage retention tests. Reuse PRD-169 transaction receipts and PRD-182
mid-turn preservation; this PRD defines the user operation layer.
