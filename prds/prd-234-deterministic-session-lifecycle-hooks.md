---
title: "PRD-234: Deterministic Session Lifecycle Hooks"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P2
related_prds: [PRD-04, PRD-138, PRD-150, PRD-223]
tags: [hooks, lifecycle, plugins, automation]
---

# PRD-234 — Deterministic Session Lifecycle Hooks

## 1. Summary

Add a small, versioned lifecycle-hook API for deterministic automation at
session/run/workflow/tool boundaries: notifications, project checks, audit
events, and safe output enrichment. Hooks must not replace the canonical
permission, approval, sandbox, or workflow transition systems.

## 2. Problem and research basis

Agenthicc has lauren-ai tool-hook adapters and configuration hooks, but no
complete documented lifecycle contract covering startup, user input,
workflow/phase boundaries, background wait/completion, and errors. Claude Code
uses explicit lifecycle events and structured hook input/output; its docs also
warn that command hooks execute with the user's permissions. Agenthicc should
adopt deterministic event contracts and strong trust/timeout limits.

Sources: [Claude Code hooks](https://code.claude.com/docs/en/hooks-guide),
[Agenthicc hook boundary](../docs/guides/hooks.md),
[PRD-138 extension SDK](prd-138-repository-improvement-roadmap.md).

## 3. Goals and non-goals

Goals: stable event schema, ordered execution, trusted command hooks,
read-only prompt hooks, bounded timeouts, diagnostics, and auditable results.

Non-goals: arbitrary hook code from auto-discovered untrusted projects,
LLM-driven hook decisions on security boundaries, or permission bypasses.

## 4. Requirements

1. Define versioned events for session start/resume, user input accepted,
   before/after tool, workflow phase transition, awaiting input/approval,
   run completed/failed/cancelled, and compaction completed.
2. Hooks receive a size-bounded JSON envelope with stable IDs, minimal
   metadata, and no secrets by default. Hook outputs use a typed schema; only
   explicitly designated events may add informational context or veto a
   non-security operation.
3. Support command hooks only after explicit trust, with per-hook executable,
   workspace scope, environment allowlist, timeout, output cap, process-group
   cancellation, and no implicit network grant.
4. Execute hooks with declared ordering and defined failure policy
   (`fail_closed` only for explicitly configured integrity checks;
   notification/telemetry errors are non-blocking). Prevent recursive hook
   invocation and cap nesting.
5. Provide a hooks inspector showing origin, event, command, trust state,
   last result, and redacted error; `/hooks reload` swaps config atomically.
6. Emit hook lifecycle records to the normal event stream, but exclude secrets,
   unrestricted environment, and raw hook stdout unless explicitly retained.

## 5. Acceptance criteria

- A trusted post-edit hook can run a formatter and its failure is reported
  without pretending the original edit was rolled back.
- An untrusted project hook never executes on startup or in Yolo mode without
  a separate trust grant.
- Timeouts/cancellation cannot leave child processes running; hook errors do
  not corrupt session/checkpoint state.
- Hooks cannot approve tools, increase mode, change workspace authorization,
  or cause a workflow transition without the normal transition tool.
- Config reload either atomically installs a valid registry or retains the
  previous working registry with actionable diagnostics.

## 6. Testing and dependencies

Add schema/version tests, trust tests, hook order/failure tests, process
cleanup, recursion, size limits, reload races, and security boundary tests.
Keep this separate from `tools/hooks.py`'s lauren-ai adapter and require the
versioned extension contracts in PRD-138 before public plugin use.
