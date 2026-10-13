---
title: "PRD-225: Durable Background Queue and Fair Scheduling"
status: Implemented
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-141, PRD-205, PRD-219, PRD-223]
tags: [background, queue, scheduling, fairness]
---

# PRD-225 — Durable Background Queue and Fair Scheduling

## 1. Summary

Change background admission from “start if a worker slot is free, otherwise
fail” into a durable, bounded queue with explicit priority, per-project
fairness, and user-visible dispatch state. A queued request is not a running
session and must never be described as accepted for execution until its durable
queue record is committed.

## 2. Problem and research basis

Agenthicc has detached/background workers, configurable worker limits, and a
manager, but users can hit a background worker limit instead of having work
queued. Claude Code and Codex present unattended tasks as work that can be
started, left running, and revisited; Agenthicc needs predictable local
admission and scheduling before it grows those surfaces.

Sources: [Agenthicc background sessions](prd-141-background-sessions-and-session-manager-tui.md),
[Claude Code web tasks](https://support.claude.com/en/articles/12618689-claude-code-on-the-web),
[Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments).

## 3. Goals and non-goals

Goals: durable pending state, bounded queue size, stable ordering, fairness,
priority aging, cancellation before start, and clear queue position/status.

Non-goals: distributed multi-host scheduling, speculative execution, or
automatic retries of non-retryable provider/tool failures.

## 4. Requirements

1. Persist queue records in the existing background state root, including
   request/session IDs, project identity, requested mode/profile, creation
   time, priority, idempotency key, and scheduler state.
2. Support a small explicit priority set (`urgent`, `normal`, `low`) with
   aging so low-priority work cannot starve indefinitely. Default to `normal`.
3. Enforce global and per-project concurrency limits, maximum queue length,
   and optional per-user limits. Config validation must reject zero/negative
   and unreasonable bounds.
4. Dispatch only when a slot becomes available; use a single scheduler owner
   and compare-and-swap/lease semantics to avoid duplicate worker starts.
5. Display queued/running/waiting/terminal as distinct states in `jobs` and
   `agenthicc agents`; expose queue position as advisory, not a promise.
6. Allow cancellation of a queued job without spawning a worker. Moving a
   running job in priority requires an explicit command and audit event.
7. Keep queue acceptance distinct from prompt delivery and worker startup in
   command receipts and logs.

## 5. Acceptance criteria

- When the worker limit is reached, a valid request is either durably queued
  with a receipt or rejected with a specific capacity error; it is never lost.
- Freed slots dispatch queued work exactly once across rapid completion and
  service restart.
- Per-project traffic cannot monopolize all slots while another eligible
  project is queued; priority aging eventually advances low-priority work.
- Queue bounds survive restart, and queue overflow is deterministic and
  user-visible.
- Cancelling queued work produces no child process, model call, or session
  lease.

## 6. Tests and rollout

Add deterministic scheduler unit tests with a fake clock, concurrency race
tests, crash/replay tests, and multi-project fairness simulations. Roll out
behind `background.queue_enabled = false` initially; migration must preserve
existing accepted/running records and never auto-resubmit terminal sessions.

## 7. Dependencies

Builds on PRD-141 and the finite-attempt semantics in PRD-205/219. The server
process that owns queue dispatch is defined in PRD-223.
