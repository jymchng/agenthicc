---
title: "PRD-220: Server-Side Agent Execution API"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P0
related_prds: [PRD-138, PRD-150, PRD-141, PRD-204]
tags: [server, api, sessions, execution]
---

# PRD-220 — Server-Side Agent Execution API

## 1. Summary

Extend Agenthicc's client-neutral `SessionService` and loopback HTTP/SSE
transport into an explicitly managed server runtime that can start and drive
real Agenthicc turns and workflows. The existing session service creates,
projects, streams, and accepts commands for sessions; this PRD closes the
execution gap without creating a second runner or state store.

## 2. Problem and research basis

The current `LocalSessionServer` is an adapter over `SessionService`; it does
not itself construct a runner. Users cannot submit a normal prompt or select a
workflow through a supported remote execution API. OpenCode's documented
server is a headless HTTP service used by its TUI and programmatic clients;
Codex's app-server exposes conversation, approval, and streamed agent events.
Agenthicc should adopt the client/server boundary, while preserving its own
kernel, session ownership, mode, and workflow contracts.

Sources: [Agenthicc PRD-150](prd-150-client-neutral-session-service-and-event-projection.md),
[OpenCode server](https://docs.opencode.ai/docs/server/),
[Codex app-server](https://learn.chatgpt.com/docs/app-server).

## 3. Goals and non-goals

Goals: start a session, submit an exact user prompt, select an existing
workflow/profile, stream durable progress, and control/cancel the same run from
another client. Reuse the current `SessionService`, TUI/headless runner
construction, background supervisor, and checkpoint/resume behavior.

Non-goals: public multi-tenant hosting, a web UI, bypassing approval or mode
gates, or making an HTTP request itself execute arbitrary shell commands.

## 4. Functional requirements

1. Add a server runtime composition root that registers the production turn
   handler with `SessionService`; server creation must not fabricate a parallel
   conversation store or workflow runner.
2. Expose idempotent commands to create a session, submit a user turn, select
   or start a workflow, interrupt/cancel, and query the resulting attempt.
   Each mutation requires a client-generated idempotency key and returns a
   stable operation receipt.
3. Preserve submitted prompt bytes/text and command intent. A remote command
   must not be silently rewritten as a workflow continuation.
4. Stream typed events for accepted input, assistant output, tool lifecycle,
   approvals/questions, workflow phase changes, usage, errors, and terminal
   status. Event IDs must support replay after reconnect.
5. Apply the same `SessionOwner` lease, mode/capability policy, approval
   service, workspace scope, and checkpoint rules as local runs.
6. Enforce request/event size bounds, per-session one-active-turn semantics,
   server concurrency limits, and cancellation propagation.
7. Retain existing session-only endpoints and response shapes during the
   compatibility period; version new execution operations under `/v1`.

## 5. Reliability and security

Persist command acceptance before acknowledging it. A crash between acceptance
and worker startup must leave a recoverable operation, not a lost prompt or a
duplicate run. Reject stale expected-sequence writes and conflicting live
owners. Do not log prompts, tokens, provider headers, or tool arguments by
default. Server mode is loopback-only unless explicitly configured with the
separate PRD-222 security controls; network binding must never imply open
access.

## 6. Acceptance criteria

- An HTTP client can create a session, submit one prompt, observe its real
  assistant/tool/workflow events, and read the terminal result.
- Repeating a mutation with its idempotency key returns the original receipt
  and does not execute the prompt twice.
- The same workflow can be interrupted and resumed through existing durable
  checkpoint paths after a server restart.
- Concurrent writes to one session are serialized/rejected deterministically;
  independent sessions can run concurrently within configured limits.
- Safe/Plan/Yolo mode, approvals, workspace authorization, and session leases
  behave identically to the local runner.
- Existing `session serve` behavior remains compatible and can be tested
  without live model credentials.

## 7. Verification

Add unit tests for command validation, idempotency, sequence conflicts, and
event serialization; integration tests for the real session-service/runner
bridge with fake providers; and end-to-end tests for remote create→run→stream→
interrupt→resume. Include crash injection before and after acceptance, and
assert no duplicate side effects.

## 8. Delivery dependencies

Depends on the session projection and command contracts in PRD-150. API
authentication is specified separately in PRD-222; process supervision and
deployment lifecycle are specified in PRD-223. Deliver the in-process runtime
first, then expose it over the existing local transport.
