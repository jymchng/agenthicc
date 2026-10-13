---
title: "PRD-232: Durable Agent-Team Task Graph and Communication"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-03, PRD-203, PRD-204, PRD-214, PRD-219]
tags: [agents, teams, task-graph, communication]
---

# PRD-232 — Durable Agent-Team Task Graph and Communication

## 1. Summary

Promote one-shot worker spawning into a durable team coordinator: maintain a
dependency-aware task graph, route clarifying questions and answers between
coordinator and workers, optionally permit peer messages, and restore pending
work after a restart. Agent tasks remain subordinate to a single parent
session and its policy boundary.

## 2. Problem and research basis

Agenthicc has a subagent pool and a communication broker, but task execution,
worker state, and messages must remain coherent across workflow/session
recovery; in-memory messages alone are not a durable team contract. Claude Code
documents background/foreground subagents, task creation/completion events,
and concurrent limits; Codex app-server exposes multi-agent events to clients.
Agenthicc should add durable coordination without creating a second agent
runtime.

Sources: [Claude Code subagents](https://code.claude.com/docs/en/sub-agents),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Agenthicc subagent policy](prd-197-subagent-runtime-policy-inheritance.md).

## 3. Goals and non-goals

Goals: task DAG, durable worker assignments/messages, clarification escalation,
team progress view, partial completion, and safe recovery.

Non-goals: open-ended autonomous agent swarms, unbounded nested spawning, or
letting workers edit parent conversation or bypass parent capability policy.

## 4. Requirements

1. Persist task ID, parent session, worker session, dependencies, status,
   revision, base commit/worktree, result receipt, and timestamps as
   event-sourced or transactionally durable team state.
2. Validate the task DAG for missing dependencies and cycles before dispatch.
   Dispatch only ready tasks and enforce configured worker/depth/token/time
   budgets.
3. Persist messages with sender, recipient, task, message type, correlation ID,
   and status. Support `info`, `question`, `answer`, `blocker`, and
   `handoff`; deliver to an exact agent/task, not a guessed display name.
4. A worker question can pause only that worker, surface to the parent inbox,
   and resume the same worker after an answer. Peer messaging is opt-in and
   scoped to workers in the same parent run.
5. Parent and workers receive explicit context bundles, not mutable shared
   conversation objects. Worker reports/artefacts are linked by durable IDs
   and can be reloaded after process restart.
6. Partial failures do not erase successful worker results. Retry creates a
   new attempt record; completed tasks are not repeated unless explicitly
   invalidated by dependency changes.

## 5. Acceptance criteria

- A coordinator can dispatch two independent tasks and hold a dependent task
  until both complete.
- Worker question → parent answer survives parent/service restart and resumes
  the same worker task exactly once.
- Messages from one parent run cannot be read or delivered across another
  session, project, or principal.
- A failed worker can be retried without losing other workers' results or
  duplicating successful downstream work.
- The TUI and session service project the same task graph and message status.

## 6. Verification and dependencies

Test DAG validation/scheduling, message delivery and replay, worker races,
policy inheritance, checkpoint recovery, and multi-project isolation. Reuse
`subagents.communication`, `SubagentPool`, event/session persistence, and
worktree orchestration. This PRD extends PRD-03/203 rather than introducing a
new process or memory runtime.
