---
title: "PRD-226: Background Run Notifications and Webhook Delivery"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P2
related_prds: [PRD-141, PRD-150, PRD-222, PRD-223]
tags: [background, notifications, webhooks, events]
---

# PRD-226 — Background Run Notifications and Webhook Delivery

## 1. Summary

Notify users and approved automation endpoints when background sessions reach
meaningful state changes—completion, failure, waiting for approval/input, or
recovery required—without polling or exposing transcript content.

## 2. Problem and research basis

An unattended task is useful only if the user can learn when it needs
attention. Claude Code hooks include notifications for input-needed and
completed background agents; Codex exposes streamed events to clients. Agenthicc
already records durable background state and session events but needs a
delivery layer for operators who are not watching the TUI.

Sources: [Claude Code hook notifications](https://code.claude.com/docs/en/hooks-guide),
[Codex app-server events](https://learn.chatgpt.com/docs/app-server),
[Agenthicc session service](prd-150-client-neutral-session-service-and-event-projection.md).

## 3. Goals and non-goals

Goals: opt-in local notifications and generic signed webhooks, durable
delivery/retry status, event filtering, and duplicate suppression.

Non-goals: built-in Slack/email accounts, sending full prompts or transcripts,
or allowing notification delivery to alter a run's state.

## 4. Requirements

1. Define a minimal notification event schema: event ID, session/run IDs,
   workspace display name, state, event time, and a redacted short summary.
   Default payload excludes prompts, tool inputs/outputs, provider data, and
   paths outside the workspace.
2. Support configurable local desktop/terminal notifications when available;
   headless systems must degrade quietly and not fail the worker.
3. Support optional HTTPS webhook targets with per-destination secret
   references, HMAC signatures, timestamp/replay protection, and configurable
   event filters.
4. Persist an outbox record before delivery. Retry transient errors with
   bounded exponential backoff and jitter; never retry permanent 4xx responses
   except explicitly configured rate-limit responses.
5. Use an idempotent event ID for delivery and expose delivery attempts,
   last-result category, and next retry time without recording secrets.
6. Prevent SSRF: allow only configured destinations, resolve and reject
   loopback/private/link-local targets unless an explicit administrator policy
   allows them, revalidate redirects and DNS on every delivery.
7. Notification failure must not change the job status or hold session
   completion open after the bounded enqueue step.

## 5. Acceptance criteria

- A user can subscribe to completion/failure/waiting events and receive one
  verifiable signed payload for each event.
- Restarting after enqueue but before send results in eventual delivery, with
  duplicate event IDs that receivers can safely ignore.
- Secret values, prompts, raw transcripts, arbitrary headers, and internal
  paths are absent from logs and notification payloads.
- Invalid schemes, redirects, DNS rebinding, and disallowed private addresses
  are rejected before sending.
- Notification outage does not block or convert a successful agent run into a
  failure.

## 6. Verification and rollout

Use a local mock HTTP server, fake clock, forced process restart, malformed
responses, rate limits, redirect chains, DNS-policy tests, and payload-redaction
goldens. Ship disabled by default. Server deployments require PRD-222 auth and
PRD-223 lifecycle; use a separate webhook secret scope from API client tokens.
