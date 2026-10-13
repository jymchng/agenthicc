---
title: "PRD-228: Repository-Scoped Scheduled Workflow Automations"
status: Proposed
version: 0.1.0
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

## 6. Tests, rollout, dependencies

Use fake clocks and time zones, crash-after-claim tests, duplicate dispatch
tests, recurrence property tests, worktree integration, and end-to-end service
restart coverage. Begin with read-only workflows and opt-in schedules. Requires
PRD-223 scheduler ownership, PRD-225 queue policy, and PRD-229 for isolated
mutable runs.
