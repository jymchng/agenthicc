---
title: "PRD-228: Repository-Scoped Scheduled Workflow Automations"
status: Implemented
version: 1.0.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-141, PRD-193, PRD-223, PRD-225, PRD-229]
tags: [schedules, workflows, background, automation]
---

# PRD-228 — Repository-Scoped Scheduled Workflow Automations

## 1. Summary

Add durable repository/user-scoped schedules that start an explicitly selected
workflow with an explicit prompt, profile, and policy. This is distinct from
PRD-193's session-scoped recurring `/loop`: schedules must run without an
already-open foreground session and must have bounded, inspectable job history.

## 2. Problem and research basis

Users need repeatable maintenance runs—dependency review, test health, docs
drift, or issue triage—without keeping a terminal session alive. Claude Code
documents scheduled tasks and Codex documents scheduled work in dedicated
background worktrees. Agenthicc already has workflows, background workers,
and a server/service direction but no repository-owned schedule contract.

Sources: [Claude scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks),
[Codex Git worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees),
[Agenthicc session loops](prd-193-session-scoped-recurring-loops.md).

## 3. Goals and non-goals

Goals: create/list/pause/resume/run-now/delete schedules, safe recurrence,
missed-run policy, audit history, and execution in a fresh session/worktree.

Non-goals: arbitrary always-running prompt loops, hidden schedule creation by
the model, or automatic publishing/merging of changes.

## 4. Requirements

1. A schedule definition includes stable ID, repository root, workflow name,
   exact prompt, provider profile, permission ceiling, recurrence/time zone,
   concurrency policy, next-run time, enabled flag, and creator.
2. Support one-shot, fixed interval, and cron-like schedules with explicit
   time zone and daylight-saving semantics. Validate recurrence and display
   the computed next execution before enabling.
3. Provide `agenthicc schedules` TUI/CLI operations to create, inspect, pause,
   resume, run now, and delete. Destructive update confirmation is required;
   every revision is recorded.
4. Default to one in-flight run per schedule and a configurable coalescing
   policy (`skip`, `queue_one`, or `allow_bounded`). Never replay every missed
   tick after downtime by default.
5. Each run gets a new session ID and, for mutating work, a dedicated worktree
   from a recorded base commit. Schedule prompts cannot elevate mode or
   permissions beyond their saved policy ceiling.
6. Persist scheduler cursor, occurrence ID, run receipt, and last outcome before
   dispatch. Use a unique key per schedule occurrence to prevent duplicates.
7. Keep schedule execution disabled unless the service scheduler is running;
   show whether next run is armed, paused, blocked, or awaiting service.

## 5. Acceptance criteria

- A schedule survives process restart and runs one due occurrence exactly once.
- Clock skew, DST transitions, downtime, and concurrent scheduler processes
  follow documented deterministic rules.
- `run now` creates an auditable occurrence without shifting the recurrence
  unless explicitly requested.
- Every run links to its schedule revision and session; failed runs can be
  inspected/retried as a new attempt without silently altering schedule state.
- Schedule deletion stops future occurrences but preserves historical run
  records and artifacts.

## 6. Implementation scope and rollout

The implementation is opt-in at the operator level: definitions start
paused, and execution occurs only while `agenthicc session serve` owns the
schedule-service lease. Read-only `Plan` schedules may use the repository
checkout. Every other permission ceiling uses an isolated worktree at a
recorded base commit; merges are never automatic. Schedules use the current
provider profile/configuration at dispatch time and never persist credential
values.

## 7. Implementation decisions and deterministic semantics

- Definitions, immutable revision snapshots, audit entries, occurrence
  receipts, and outcomes are stored in owner-only
  `~/.agenthicc/schedules/schedules.sqlite3`. Schedule IDs and occurrence IDs
  are stable; scheduled occurrence keys include schedule ID, definition
  revision, and the exact due cursor. Manual runs and retries receive separate
  occurrence keys.
- Before calling the background supervisor, the scheduler advances the due
  cursor and commits a `PREPARING` occurrence with its deterministic session
  identity. The dispatcher adopts that same identity after restart. A
  cross-process dispatch lock covers pending-state selection through durable
  supervisor admission; the service lease separately prevents two normal
  service owners.
- Manual `run-now` count/admission and occurrence insertion happen in one
  SQLite write transaction. Simultaneous invocations therefore cannot exceed
  `max_in_flight` or create multiple pending items under `queue_one`.
- A definition edit is explicit (`schedules update ... --confirm`), creates
  an audit revision, recomputes its next-run preview, and pauses the schedule.
  The operator must resume with the exact displayed ISO timestamp, including
  seconds and offset. This prevents an edit from silently re-arming changed
  work under an old confirmation.
- One-shot local times that do not exist during a DST spring-forward are
  rejected. Ambiguous one-shot times choose the earlier instant (`fold=0`).
  Cron wall times that do not exist are skipped; ambiguous cron wall times run
  once at the earlier instant. Cron uses five numeric fields, Sunday 0/7, and
  OR semantics when both day-of-month and weekday are restricted.
- Interval recurrences remain anchored to the previous scheduled occurrence.
  After downtime or a forward wall-clock jump, the cursor advances to the
  first future tick without replaying every missed interval. A backward clock
  correction does not move the persisted cursor backward or duplicate a
  committed occurrence.
- `skip` records a due tick as skipped while another run is active;
  `queue_one` coalesces additional missed ticks into one waiting occurrence;
  `allow_bounded` dispatches only up to its finite `max_in_flight` value and
  records excess ticks as skipped. The default is `skip` with one active run.
- Deletion is a tombstone. It stops future scheduled ticks but leaves
  occurrence history, background-session records, and worktree artifacts
  available for inspection. A retry is a new occurrence/session/attempt linked
  to the failed or blocked occurrence.

## 8. Verification

- Unit coverage: interval/cron parsing, timezone validation, DST folds/gaps,
  clock jumps, resume confirmation, edit pause/revision behavior, all
  coalescing modes, bounded concurrency, retry identity, storage validation,
  duplicate scheduler ticks, and crash-after-claim recovery.
- Integration coverage: Plan versus mutating dispatch, recorded base commit,
  isolated worktree, exact prompt/profile/mode propagation, deterministic
  background-session adoption, and durable occurrence receipts.
- E2E coverage: `tests/e2e/test_schedules_prd228.py` exercises the CLI lifecycle
  across separate processes and kills/restarts the scheduler service after a
  persisted claim to verify recovery uses the same occurrence and session.
- User and operator contracts are documented in
  [`docs/guides/scheduled-workflows.md`](../docs/guides/scheduled-workflows.md),
  [`docs/reference/cli.md`](../docs/reference/cli.md), and
  [`docs/reference/storage.md`](../docs/reference/storage.md).
