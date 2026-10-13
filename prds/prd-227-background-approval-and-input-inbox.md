---
title: "PRD-227: Unified Background Approval and Input Inbox"
status: Implemented
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-141, PRD-150, PRD-215, PRD-216, PRD-217, PRD-222]
tags: [background, approvals, questions, input]
---

# PRD-227 — Unified Background Approval and Input Inbox

## 1. Summary

Create one durable inbox for pending approvals, `ask_user` questions, and
operator-visible input requests across background sessions, accessible from
the TUI, CLI, and authenticated session service. Every response is delivered
to the exact owning session operation.

## 2. Problem and research basis

Agenthicc has background approval/input commands and an agents manager, but
operators must find the right session and infer which action is pending.
Claude Code surfaces background agents that need input and uses notification
hooks; Codex clients expose approvals and streamed agent events. Agenthicc can
unify the operator surface while retaining separate typed decisions.

Sources: [Claude Code subagents](https://code.claude.com/docs/en/sub-agents),
[Claude Code hooks](https://code.claude.com/docs/en/hooks-guide),
[Codex app-server](https://learn.chatgpt.com/docs/app-server).

## 3. Goals and non-goals

Goals: one sortable inbox, exact-session routing, durable receipts, expiry and
staleness visibility, remote client support, and recovery-safe answers.

Non-goals: synthesizing workflow continuation, interpreting slash commands,
altering user text, or approving a tool because a notification was clicked.

## 4. Requirements

1. Represent each pending item with stable ID, session/attempt ID, item type,
   creation/expiry time, exact choices or approval target, current status, and
   required scope. Persist before showing it to clients.
2. Provide TUI `/inbox`, a CLI listing/answer path, and equivalent session
   service operations. Filter by project, item type, and age; sort oldest first
   by default to reduce starvation.
3. Approval responses must reference the exact tool-call/approval ID and
   preserve the existing capability/approval policy. `ask_user` answers must
   preserve answer text exactly and use the existing question result contract.
4. Arbitrary user input remains an opaque input message delivered unchanged to
   the target session. It must not be converted into a synthetic resume,
   workflow action, or answer unless the user selects the corresponding inbox
   item and answer operation.
5. Use expected version / idempotency keys so simultaneous clients cannot
   answer the same item twice. A stale item returns its current state and does
   not inject the response into another attempt.
6. Provide explicit expire/reject behavior, configurable timeout, and
   recovery of undelivered accepted answers after restart.

## 5. Acceptance criteria

- A user can see and answer a pending question or approval without attaching
  the TUI to the session; the result reaches that session once.
- An ordinary input submitted from session details remains byte-for-byte
  unchanged and is not treated as workflow control.
- Deleted sessions and stale attempts never receive late responses; the UI
  explains why and offers a safe alternative.
- Concurrent answers produce one winner and deterministic conflict feedback.
- Recovered inbox state matches durable session state after restart.

## 6. Testing and dependencies

Add service/CLI/TUI parity tests, exact-input regression tests, answer race and
restart tests, timeout tests, and cross-scope authorization tests. Builds on
PRD-141/150 and PRD-217; remote access must follow PRD-222.

## 7. Implementation and verification

Implemented in `agenthicc.background.inbox` with a private SQLite inbox, exact
session-attempt/request identity, versioned compare-and-set responses,
idempotency receipts, and reconciliation against the authoritative background
session store. The TUI `/inbox`, `agenthicc jobs inbox` CLI commands, and
authenticated `/v1/inbox` session-service endpoints use that same store.
Ordinary session-detail input continues through the separate opaque input
inbox and is not parsed or converted by this feature.

Verification includes unit tests for ordering, target/answer shape validation,
privacy, expiry, stale/deleted attempts, concurrent responses, restart reads,
and worker delivery/timeouts; integration tests cover service capabilities,
redaction, HTTP bearer authentication, and conflict responses. The target suite
is `tests/unit/test_background_interaction_inbox_prd227.py` and
`tests/integration/test_background_inbox_prd227.py`.
