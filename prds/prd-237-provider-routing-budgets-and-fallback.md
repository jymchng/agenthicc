---
title: "PRD-237: Provider Routing, Budgets, and Fallback Policies"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-138, PRD-157, PRD-162, PRD-187, PRD-198, PRD-233]
tags: [providers, model-routing, budgets, reliability]
---

# PRD-237 — Provider Routing, Budgets, and Fallback Policies

## 1. Summary

Add explicit, explainable provider routing with per-run token/cost/time budgets
and opt-in fallback chains. The router may retry or switch providers only
according to a user-defined error policy; it must not hide quota exhaustion,
change a model silently, or repeat a non-idempotent tool call.

## 2. Problem and research basis

Agenthicc supports multiple providers, named profiles, retries, session-aware
headers, and usage accounting, but model selection and failure policy are
currently configured in separate layers. OpenCode exposes provider/model
selection; Codex provides model selection and task context; all products make
provider behavior visible to the operator. Agenthicc can add routing without
making opaque choices that undermine reproducibility.

Sources: [OpenCode configuration](https://docs.opencode.ai/docs/config/),
[Agenthicc provider profiles](prd-162-provider-connection-profiles-and-modal-endpoints.md),
[Agenthicc usage accounting](prd-157-usage-accounting-and-tui-token-observability.md).

## 3. Goals and non-goals

Goals: explicit ordered routes, policy by error class, per-session budgets,
provider attribution, and reproducible resume behavior.

Non-goals: automatic provider signup, credential copying between providers,
or retrying every HTTP error indiscriminately.

## 4. Requirements

1. Define routes by task class (main turn, compaction, title, subagent, review)
   with primary model, ordered fallbacks, maximum attempts, and provider
   capability requirements. Default route remains the existing selected
   profile with no fallback.
2. Classify permanent authentication/configuration errors, quota/billing
   errors, transient rate limits, timeouts, invalid requests, and unsupported
   capabilities. Each class has an explicit retry/fallback policy; permanent
   failures stop unless an operator-selected fallback says otherwise.
3. Support maximum prompt/output tokens, total session tokens, estimated cost,
   wall-clock budget, and maximum provider spend. Explain estimates and stop or
   ask when a budget is exhausted; never raise a budget automatically.
4. Preserve conversation ID/session headers and provider-specific options when
   switching to a compatible endpoint. Reject fallback models that cannot
   preserve required features such as vision, tools, or reasoning content.
5. Emit redacted routing events with chosen route, model, fallback reason,
   attempt count, and estimate/actual usage. Keep provider errors actionable
   without dumping credentials or raw headers.
6. Persist route and model identity in attempt metadata; on resume, use the
   original route unless the user explicitly selects a new route.

## 5. Acceptance criteria

- A transient timeout follows configured bounded retries; an insufficient
  balance response stops by default and is never misreported as transient.
- An opted-in compatible fallback is selected once, recorded, and retains the
  session conversation identity; unsupported fallback is rejected before call.
- Cost/token/time limits stop before another provider call and are visible in
  TUI, background manager, CLI, and session projections.
- Retry/fallback does not duplicate a completed tool side effect or lose
  messages from a failed mid-turn transaction.
- Default behavior for existing provider profiles is unchanged.

## 6. Testing and dependencies

Add table-driven provider-error classification, fake transport routing,
budget-boundary, duplicate-side-effect, resume, and session-header tests.
Reuse the canonical usage ledger, lauren-ai retry boundary, and provider
profiles; avoid a parallel accounting store.
