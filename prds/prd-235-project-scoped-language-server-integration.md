---
title: "PRD-235: Project-Scoped Language Server Integration"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P2
related_prds: [PRD-138, PRD-139, PRD-229, PRD-233]
tags: [lsp, code-intelligence, tools, language-servers]
---

# PRD-235 — Project-Scoped Language Server Integration

## 1. Summary

Provide reusable, lifecycle-managed LSP clients so agents can query diagnostics,
definitions, references, symbols, hover information, and implementations
through typed tools rather than repeatedly approximating semantic navigation
with grep and file reads.

## 2. Problem and research basis

Agenthicc has strong filesystem and search tools but no canonical language
server process owner. OpenCode exposes LSP code intelligence operations;
Claude Code also uses language-aware navigation in supported environments.
Agenthicc should make LSP an optional, project-scoped capability with explicit
server trust and resource limits.

Sources: [OpenCode LSP tools](https://docs.opencode.ai/docs/tools/),
[OpenCode LSP configuration](https://docs.opencode.ai/docs/lsp/),
[PRD-139 code-intelligence comparison](prd-139-opencode-inspired-features-and-privacy-first-ads.md).

## 3. Goals and non-goals

Goals: standard LSP client, selected language-server configurations,
diagnostic/navigation tools, lifecycle reuse, and graceful fallback.

Non-goals: bundling language servers, installing packages automatically,
editing files through the LSP server, or treating server output as trusted.

## 4. Requirements

1. Add a per-project language-server registry with command/args, file
   extensions, initialization options, environment allowlist, startup timeout,
   request timeout, and resource limits. Loading a project command requires
   explicit trust.
2. Start a server lazily on first relevant file and share it safely within the
   owning project runtime. Shut down on project/session service close; restart
   after crash with bounded backoff and one initialization per root.
3. Expose typed read-only tools for diagnostics, definition, references,
   document/workspace symbols, hover, implementation, and call hierarchy.
   Every URI/path must resolve through `WorkspaceView` and current authorization.
4. Bound result counts and text bytes, normalize URI paths, sanitize malformed
   responses, and keep server stderr/output out of prompts except bounded
   diagnostics.
5. Report unavailable server, missing executable, initialization error, and
   timeout as recoverable tool results; standard file/search tools remain
   available.

## 5. Acceptance criteria

- A configured Python or TypeScript language server can return a definition
  and diagnostics for a workspace file through an agent tool call.
- Untrusted project config does not execute a server command; a server cannot
  read paths outside the authorized workspace through tool arguments.
- A crashed server is restarted without hanging the TUI; requests have strict
  time/result bounds.
- Unsupported projects and missing servers degrade to current file tools with
  a clear explanation and no startup failure.
- Server process cleanup is verified after normal exit, cancellation, timeout,
  and Agenthicc shutdown.

## 6. Testing and dependencies

Use a deterministic fake LSP subprocess for protocol tests plus optional
marked tests against installed servers. Cover URI/path attacks, large
diagnostic sets, malformed JSON-RPC, initialization races, timeout, crash, and
cleanup. Depends on PRD-229 project environment/trust policy and PRD-233 tool
profile filtering.
