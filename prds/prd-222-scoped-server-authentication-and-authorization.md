---
title: "PRD-222: Scoped Server Authentication and Authorization"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P0
related_prds: [PRD-138, PRD-150, PRD-220, PRD-221]
tags: [server, security, authentication, authorization]
---

# PRD-222 — Scoped Server Authentication and Authorization

## 1. Summary

Replace the current single shared bearer-token/all-capabilities server model
with explicit authenticated principals and least-privilege capabilities for
Agenthicc's remotely reachable session service.

## 2. Problem and research basis

`LocalSessionServer` requires a token off loopback, but a valid token currently
maps to `read`, `control`, and `workspace` together. That is insufficient for
read-only dashboards, worker controllers, and user-input clients. OpenCode's
server documents a local-first binding and explicit HTTP authentication;
Codex app-server warns that remote WebSockets require authentication and TLS.
Agenthicc needs a stronger authorization boundary before remote operation is
treated as supported.

Sources: [OpenCode server](https://docs.opencode.ai/docs/server/),
[Codex app-server security](https://learn.chatgpt.com/docs/app-server),
[Agenthicc security baseline](prd-138-repository-improvement-roadmap.md).

## 3. Goals and non-goals

Goals: support named principals, scoped bearer credentials, token rotation,
revocation, audit-safe identity, and endpoint-specific capability checks.

Non-goals: Internet-wide public hosting, account federation, or implementing a
full enterprise identity provider in the first release. TLS termination may be
provided by a documented reverse proxy; plain remote HTTP must remain rejected.

## 4. Requirements

1. Define a typed principal with stable ID, display name, scopes, allowed
   projects/workspaces, issue/expiry times, and revocation identity.
2. Provide at least `session:read`, `session:input`, `session:approve`,
   `session:control`, and `workspace:read` scopes. Map each API command to the
   minimum scope; no wildcard scope is implicit.
3. Load credentials from protected local files or environment-backed
   references. Do not accept secret literals in project TOML or command-line
   flags. Provide a CLI to create, inspect-redacted, rotate, revoke, and remove
   credentials.
4. Keep loopback-only unauthenticated development behavior configurable only
   for loopback; reject wildcard bind or non-loopback bind without auth and a
   verified secure transport policy.
5. Check project/session authorization on every request, including replay,
   SSE reconnect, cancellation, input, approval, and workflow commands.
6. Apply exact CORS origin allowlists; CORS is not authentication. Reject
   `Origin: null`, wildcard credentialed CORS, and malformed origins.
7. Record principal ID, operation, session ID, decision, timestamp, and result
   code in a redacted audit log. Never record bearer tokens or request bodies.

## 5. Acceptance criteria

- A read-only token can list/read/stream only authorized sessions and cannot
  submit input, approve, cancel, or access workspace data.
- Revocation takes effect without restarting the server and terminates or
  denies existing subscriptions within a documented bound.
- Expired, malformed, revoked, wrong-scope, and cross-project requests return
  stable machine-readable errors with no data leakage.
- Non-loopback serving fails closed when credentials, TLS-proxy declaration,
  or origin policy are missing.
- Secret rotation and audit-log tests prove tokens never appear in output,
  exception strings, logs, cassettes, or persisted session events.

## 6. Testing and rollout

Add policy unit tests, API authorization matrix tests, CORS/transport tests,
revocation concurrency tests, and a threat-model review. Ship migration notices
for existing `auth_token` users; retain the old setting only through a
time-bounded compatibility adapter that translates it to one explicitly
scoped principal. Never silently grant broader scopes during migration.

## 7. Dependencies

Required before PRD-221 remote clients or non-loopback deployment. Builds on
PRD-150's `SessionService` capability contract and PRD-220's execution API.
