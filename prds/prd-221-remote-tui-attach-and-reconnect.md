---
title: "PRD-221: Remote TUI Attach and Reconnect"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P0
related_prds: [PRD-150, PRD-220, PRD-222]
tags: [tui, remote, sessions, streaming]
---

# PRD-221 — Remote TUI Attach and Reconnect

## 1. Summary

Allow the existing Agenthicc TUI to attach to a session hosted by a separate
Agenthicc server. Local TUI execution remains the default; remote mode becomes
an explicit client transport, not a second TUI implementation.

## 2. Problem and research basis

Agenthicc already has client-neutral session snapshots/events and HTTP client
adapters, but its rich workspace is constructed around local runtime objects.
OpenCode documents its TUI as a client of its server; Codex documents remote
terminal UI connections to app-server. These patterns enable a persistent
machine to run work while a laptop reconnects to inspect and steer it.

Sources: [OpenCode server](https://docs.opencode.ai/docs/server/),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Agenthicc session service](prd-150-client-neutral-session-service-and-event-projection.md).

## 3. Goals and non-goals

Goals: configure a server URL and credential reference, list/select sessions,
render replay plus live events, submit literal input, answer approvals and
questions, cancel, and recover after network loss.

Non-goals: browser UI, automatic exposure of local filesystem paths to the
server, or silently switching a local session into a remote one.

## 4. User experience requirements

1. Add an explicit `--remote`/server profile or `agenthicc attach --server`
   entry point. Existing `agenthicc`, `--continue`, and `--resume` semantics
   stay local unless the user selects a remote endpoint.
2. Render server-originated events through existing `ConversationStore`,
   `ScrollBufferAppender`, and workspace presentation contracts.
3. Support event replay from the last acknowledged sequence, reconnect with
   bounded exponential backoff, and a visible disconnected/reconnecting state.
4. Keep local input responsive while disconnected; do not claim delivery until
   the server returns a durable receipt. Offer retry only with the same
   idempotency key.
5. Show server identity, session ID, workspace label, mode, workflow phase,
   and connection state without exposing internal remote paths unnecessarily.
6. Provide a clear exit/detach action that closes only the client connection;
   it must not cancel the remote session unless the user explicitly requests
   cancellation.

## 5. Correctness and security

Remote sessions use the same opaque-input contract as local sessions. The TUI
must not parse, prepend, or transform remote user text. Credentials are read
from secret references, never included in URLs, logs, event payloads, or crash
reports. Server-provided text is rendered as untrusted terminal content; escape
Rich markup and control sequences.

## 6. Acceptance criteria

- A TUI client on another host can attach, display prior transcript events,
  observe new output, submit input, and disconnect without stopping the run.
- Reconnect replays events exactly once in sequence, including events produced
  during a disconnect; duplicate notifications do not duplicate transcript
  rows.
- A dropped submit response can be retried safely without running the prompt
  twice.
- Remote approval/question overlays have parity with the local TUI and return
  decisions to the owning server session.
- Local launch and transcript behavior remain unchanged when remote mode is not
  configured.

## 7. Testing

Use deterministic fake HTTP/SSE servers for unit and integration coverage of
disconnect, replay cursor, event gaps, malformed frames, expired credentials,
slow consumers, resize, terminal teardown, and duplicate submit responses.
Add a headless TUI E2E that runs server and client as separate processes on
loopback and verifies detach does not cancel the server worker.

## 8. Dependencies

Requires PRD-220 execution endpoints and PRD-222 server authentication. Keep
transport access behind `SessionClient`; do not add HTTP requests directly to
workspace renderers.
