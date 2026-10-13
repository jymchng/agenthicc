---
title: "PRD-233: Declarative Agent Profiles and Per-Agent Policy"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P1
related_prds: [PRD-138, PRD-139, PRD-155, PRD-197, PRD-232]
tags: [agents, profiles, configuration, policy]
---

# PRD-233 — Declarative Agent Profiles and Per-Agent Policy

## 1. Summary

Provide a validated, discoverable profile format for primary and specialized
agents. Profiles select a prompt, provider/model, tools, skill set, resource
budgets, and a policy ceiling while remaining subordinate to global security
policy and the selected session mode.

## 2. Problem and research basis

Agenthicc currently has modes, agent registry entries, subagent types, provider
profiles, and workflow-specific overrides, but these settings do not yet form
one user-facing profile contract. OpenCode agents define primary/subagent
roles, models, prompts, and permissions; Claude Code supports project/user
subagent definitions. Agenthicc should unify the *configuration view* without
collapsing existing runtime ownership boundaries.

Sources: [OpenCode agents and permissions](https://docs.opencode.ai/docs/agents/),
[Claude Code subagents](https://code.claude.com/docs/en/sub-agents),
[PRD-139](prd-139-opencode-inspired-features-and-privacy-first-ads.md).

## 3. Goals and non-goals

Goals: project/user/built-in profile discovery, validation, explainable
effective configuration, TUI/CLI selection, and policy-safe worker creation.

Non-goals: allowing a profile to elevate Yolo mode, disable sandboxing, grant
network access, or load arbitrary Python without plugin trust.

## 4. Requirements

1. Define a versioned profile schema with stable ID, display name, description,
   role (`primary`, `subagent`, `reviewer`), system prompt source, provider
   profile/model override, tool/skill allowlists, mode ceiling, approvals,
   turn/token/time budgets, and optional output contract.
2. Resolve profile precedence explicitly: managed policy > project profile >
   user profile > built-in defaults, while restrictive security settings
   compose by intersection/minimum rather than last-writer-wins escalation.
3. Validate all fields before any provider request. Unknown tools, skills,
   workflows, provider profiles, or policy values produce source-located
   diagnostics; never silently fall back to a more privileged profile.
4. Expose `agenthicc agents profiles`, `--profile`, and an interactive picker;
   show the effective profile and source in session details and run metadata.
5. Recompute the profile for every direct turn, subagent, workflow, and resume
   from durable session selection plus current restrictive policy. Persist the
   resolved version/hash so checkpoint compatibility is explicit.
6. Permit users to clone/edit profiles through a generated template, not by
   modifying bundled source. Project-defined code remains subject to plugin
   trust rules.

## 5. Acceptance criteria

- A profile can select a model and narrower tool/skill set consistently in
  TUI, headless, workflow, subagent, and background runs.
- A profile cannot expand capabilities beyond Safe/Plan/security policy.
- Invalid or removed profile references fail before the LLM call with a clear
  source and remediation; no silent use of default Yolo/build profile occurs.
- Resume displays and validates profile-version drift without restarting a
  workflow at the wrong phase.
- Secrets and full prompt bodies are excluded from config diagnostics and
  effective-config output.

## 6. Testing and dependencies

Test precedence, restrictive composition, schema migration, missing references,
checkpoint drift, and parity across runner entry points. Reuse `agents/`,
provider profiles, `SubagentExecutionPolicy`, and mode/capability gates; do not
create a second permission engine.
