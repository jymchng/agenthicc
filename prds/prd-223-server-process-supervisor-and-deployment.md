---
title: "PRD-223: Agenthicc Server Process Supervisor and Deployment"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-141, PRD-150, PRD-205, PRD-220]
tags: [server, daemon, deployment, recovery]
---

# PRD-223 — Agenthicc Server Process Supervisor and Deployment

## 1. Summary

Provide a supported long-running `agenthicc serve` process that owns the
session service, execution runtime, background supervisor, and event transport
for a configured workspace set. The service must recover durably accepted
work after process restarts and shut down without losing accepted input.

## 2. Problem and research basis

Detached Agenthicc runs already have a worker/supervisor lifecycle, and
`session serve` exposes a local session adapter, but users do not yet have one
supported always-on service lifecycle that owns both remote execution and
background-session recovery. OpenCode documents standalone headless serving;
Claude Code and Codex show why unattended work needs to continue independently
of a foreground terminal or laptop.

Sources: [OpenCode server](https://docs.opencode.ai/docs/server/),
[Claude Code web sessions](https://code.claude.com/docs/en/claude-code-on-the-web),
[Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments).

## 3. Goals and non-goals

Goals: supervised start/stop, readiness/liveness, orderly drain, restart
recovery, process lock, structured logs, and deployment examples for systemd
and containers.

Non-goals: embedding a privileged container runtime, orchestrating a cluster,
or running as a public cloud SaaS control plane.

## 4. Functional requirements

1. Add an explicit server entry point that validates configuration before
   binding, reports the effective bind address without secrets, and supports
   foreground operation suitable for systemd/container supervision.
2. Provide separate liveness and readiness endpoints. Readiness is false until
   stores, event replay, execution handlers, and required configuration have
   initialized; liveness does not perform model calls.
3. On SIGINT/SIGTERM, stop accepting new work, persist accepted commands,
   signal active workers according to configured drain policy, flush event
   stores, close SSE subscriptions, release session leases, then exit.
4. On startup, reconcile durable background requests, operation receipts,
   session owners, and orphaned worker PIDs. Never infer that a stale PID owns a
   session solely from its numeric value.
5. Enforce one server owner per state root using an OS lock with safe stale-lock
   recovery. Report actionable errors when another live service owns the root.
6. Expose metrics for queue depth, active sessions, wait states, worker
   outcomes, event lag, and resource use; labels must avoid prompts, tokens,
   project secrets, and unbounded session IDs.
7. Document Linux service setup, log rotation, state-root backup, upgrades,
   permissions, and graceful restart behavior.

## 5. Acceptance criteria

- SIGTERM during idle, active model call, tool execution, pending approval, and
  checkpoint boundary produces the documented state and can be recovered.
- Repeated service restart does not duplicate accepted input or corrupt the
  event store; an unaccepted request is clearly reported as not accepted.
- Readiness fails on corrupt/incompatible state with a repair message, while
  liveness remains cheap and bounded.
- Two servers cannot concurrently claim the same state root.
- Service examples use least-privilege OS users, restrictive file modes, and
  loopback/reverse-proxy guidance consistent with PRD-222.

## 6. Verification and dependencies

Use subprocess E2E tests with controlled termination points, lock contention,
restart/replay, and no network/model dependency. Requires PRD-220 execution
runtime; remote exposure requires PRD-222. It complements rather than replaces
the finite detached worker semantics in PRD-205.
