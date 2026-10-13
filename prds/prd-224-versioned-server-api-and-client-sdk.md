---
title: "PRD-224: Versioned Server API Schema and Client SDK"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-138, PRD-150, PRD-220, PRD-222]
tags: [server, api, openapi, sdk]
---

# PRD-224 — Versioned Server API Schema and Client SDK

## 1. Summary

Publish a stable, versioned schema for Agenthicc's session and execution API,
generate reference documentation and a typed Python client from that schema,
and establish compatibility rules so external clients do not depend on private
TUI or runner internals.

## 2. Problem and research basis

The current service has typed Python models and HTTP routes, but route behavior
and schemas are not yet a complete external compatibility contract. OpenCode
uses an OpenAPI specification to generate its SDK; Codex app-server defines a
versioned bidirectional protocol for clients. Agenthicc should select a simple
HTTP/SSE v1 contract first, then add additional transports only when needed.

Sources: [OpenCode server and OpenAPI](https://docs.opencode.ai/docs/server/),
[Codex app-server protocol](https://learn.chatgpt.com/docs/app-server),
[PRD-150](prd-150-client-neutral-session-service-and-event-projection.md).

## 3. Goals and non-goals

Goals: machine-readable request/response/event schemas, generated API docs,
typed client methods, compatibility tests, and explicit deprecation policy.

Non-goals: exposing every internal dataclass, committing generated client
implementations for every language, or promising stability for experimental
fields.

## 4. Requirements

1. Define public schemas for session snapshots, events, commands, operation
   receipts, errors, pagination cursors, capabilities, and server metadata.
2. Generate OpenAPI 3.1 from route definitions or a single schema source; do
   not maintain an independent hand-written schema that can drift.
3. Generate and publish a typed Python client with sync/async support only if
   both can share the same protocol tests; otherwise deliver async first.
4. Document event sequence, replay retention, idempotency, retryability,
   timeout, cancellation, and error-code semantics.
5. Version additive-compatible schema changes; introduce a new API version
   for removed/renamed fields or changed semantics. Unknown optional fields
   must be tolerated by clients.
6. Add CI checks that compare generated artifacts with source and fail when
   checked-in docs/client output is stale.
7. Clearly mark auth-required operations and scopes in generated docs.

## 5. Acceptance criteria

- A clean build generates a valid OpenAPI document and API reference from the
  implementation.
- The generated Python client can create/read/stream/control a session using
  only public API types.
- Contract tests run the same fixtures against the in-process and HTTP clients
  and produce equivalent session behavior.
- A compatibility test proves an older client tolerates an additive event
  field; a breaking schema change is rejected without a version bump.
- Generated docs expose no secret-bearing examples or internal filesystem
  paths.

## 6. Testing, release, dependencies

Add schema snapshots, generator reproducibility, client conformance, event
replay, and version negotiation tests. Publish compatibility notes with each
release. Requires the public route set from PRD-220 and authentication/scopes
from PRD-222; this PRD refines PRD-138 P2.1 into a specific deliverable.
