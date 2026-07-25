# Product requirements index

This directory contains the project's product requirements and implementation
notes. Many documents are historical design records; verify their status
against the current source before implementing them.

## Current repository roadmap

- [PRD-138 — Repository Improvement Roadmap](prd-138-repository-improvement-roadmap.md)
- [PRD-139 — OpenCode-Inspired Product Expansion and Privacy-First Advertisements](prd-139-opencode-inspired-features-and-privacy-first-ads.md)
- [PRD-140 — Type-Safety and Static Contract Hardening](prd-140-type-safety-and-static-contract-hardening.md)
- [PRD-141 — Background Sessions and Session Manager TUI](prd-141-background-sessions-and-session-manager-tui.md)
- [PRD-142 — Dollar-Prefixed Skill Triggers](prd-142-dollar-prefixed-skill-triggers.md)
- [PRD-143 — Safe Commands During Active LPM Runs](prd-143-safe-commands-during-active-runs.md)
- [PRD-144 — Resize-Safe Waiting Modals and Pause-Aware Display Timing](prd-144-resize-safe-waiting-modals.md)
- [PRD-145 — CLI Skill Installation](prd-145-cli-skill-installation.md)
- [PRD-146 — CLI MCP Server Registration](prd-146-cli-mcp-server-registration.md)
- [PRD-147 — Workflow-Native Extension Authoring](prd-147-workflow-native-extension-authoring.md)
- [PRD-148 — Unified Interrupt and Graceful Cancellation](prd-148-unified-interrupt-and-graceful-cancellation.md)

PRD-138 is the current cross-cutting roadmap for documentation truth,
packaging, state boundaries, security, workflow correctness, persistence,
observability, extension APIs, and release gates.

PRD-139 is the product-expansion roadmap layered on top of PRD-138. It compares
the current repository with OpenCode-inspired product surfaces and defines the
privacy, lifecycle, and rendering contract for advertisements.

PRD-140 is the typing-focused companion to PRD-138. It records the measured
static-analysis debt and defines the phased contract, toolchain, and CI ratchet
for stricter type checking without changing runtime ownership boundaries.

PRD-141 defines the local-first background-session lifecycle and the TUI/CLI
control plane for observing, cancelling, retrying, and resuming durable agent
work without creating a second execution or persistence architecture.

PRD-142 implements the source-aware input cutover that uses `$` for explicit
skill invocation while keeping `/` for commands. Legacy `/skill-name` input is
not an executable compatibility path.

PRD-143 evaluates a typed busy-state policy so safe read-only and run-control
commands can remain responsive while the LPM is responding, while mutating and
agent-starting commands remain queued.

PRD-144 defines a pause-aware display clock and resize-safe Live rendering for
approval, plan-review, and question modals. It preserves wall-clock turn
telemetry while preventing a resize from changing otherwise static waiting UI.

PRD-145 adds `agenthicc skills add` for validated project- or user-global
`SKILL.md` installation without overwriting existing skills.

PRD-146 adds `agenthicc mcp add` for validated, persistent MCP server
registration using the existing `[[tools.mcp_servers]]` configuration format.

PRD-147 proposes converting the default `$create-*` authoring skills into
first-class workflows that stage, validate, approve, and publish workflows,
tools, and commands through their existing extension contracts.

PRD-148 defines the missing unified interrupt contract across the foreground
TUI, `lauren-ai` agent turns, workflows, durable journals, headless execution,
and the PRD-141 background-session lifecycle. It distinguishes user
cancellation from failure and successful completion while preserving partial
work and idempotency evidence.

## Existing PRDs

The numbered PRDs in this directory record individual feature decisions and
acceptance criteria. Use `rg '^# PRD|^#' prds -g '*.md'` to search them by title.
When a PRD is implemented, update its status and link the implementation and
verification evidence. When superseded, keep the file and add a superseded-by
link rather than deleting the design history.

## Status convention

New or revised PRDs should include:

- status (`Proposed`, `In progress`, `Implemented`, `Superseded`, or
  `Historical`);
- date and scope;
- evidence-backed problem statement;
- goals and non-goals;
- acceptance criteria;
- rollout/migration and security considerations;
- verification commands or test references;
- links to superseding or related PRDs.
