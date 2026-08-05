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
- [PRD-149 — Background Terminals and Responsive Wait Control](prd-149-background-terminals-and-responsive-wait-control.md)
- [PRD-150 — Client-Neutral Session Service and Multi-Client Event Projection](prd-150-client-neutral-session-service-and-event-projection.md)
- [PRD-151 — Reliable Command Execution and Build/Development-Server Lifecycle](prd-151-reliable-command-execution-and-build-server-lifecycle.md)
- [PRD-152 — Agent-Executable create_* Authoring](prd-152-self-contained-workflow-phase-authoring.md)
- [PRD-153 — Reliable Agent-Owned Workflow Authoring](prd-153-reliable-agent-owned-workflow-authoring.md)
- [PRD-155 — Consolidated Safe, Plan, and Yolo Modes](prd-155-three-mode-operational-model.md)
- [PRD-156 — Resumable Plan-Mode Interrupts and Workflow Continuation](prd-156-resumable-plan-interrupts.md)
- [PRD-157 — Canonical Usage Accounting and TUI Token Observability](prd-157-usage-accounting-and-tui-token-observability.md)
- [PRD-158 — Display Resumed TUI Transcript](prd-158-resumed-tui-transcript.md)
- [PRD-159 — Specialized CloakBrowser Agent Tools](prd-159-cloakbrowser-agent-tools.md)
- [PRD-160 — Playwright Browser Agent Tools](prd-160-playwright-browser-agent-tools.md)
- [PRD-161 — Exploratory Tool-Call Consolidation in the TUI](prd-161-exploratory-tool-call-consolidation.md)
- [PRD-162 — Provider Connection Profiles and OpenAI-Compatible Endpoints](prd-162-provider-connection-profiles-and-modal-endpoints.md)
- [PRD-163 — Cache-Stable Workflow Prompts and Generated Workflows](prd-163-cache-stable-workflow-prompts-and-generated-workflows.md)
- [PRD-164 — Suppress Repeated Idle TUI Status Frames](prd-164-repeated-idle-status-frames.md)
- [PRD-165 — Suppress Approval-Wait Redraw Loops](prd-165-approval-wait-redraw-suppression.md)
- [PRD-166 — Terminal-Safe Active Animation Rendering](prd-166-terminal-safe-active-animation.md)
- [PRD-167 — Workspace-Scoped @Mentions and Cross-Repository Target Consistency](prd-167-workspace-scoped-mentions.md)
- [PRD-168 — Mode-Aware Parent-Workspace Access](prd-168-mode-aware-parent-workspace-access.md)
- [PRD-169 — Transaction-Safe Tool-Call Conversations Across agenthicc and lauren-ai](prd-169-tool-call-transaction-integrity.md)

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

PRD-149 extends the background-session control plane to owned terminal
subprocesses. It defines responsive foreground waiting, live `/ps` inspection,
`/stop` control, Esc interruption, bounded output, process-group cleanup, and
recovery semantics for long-running `run_bash` and `run_command` calls.

PRD-150 defines and implements the client-neutral session service and versioned
event projection needed for the TUI, headless runner, CLI, web, and IDE clients
to observe and control one session without creating parallel state or execution
paths. It selects an explicit loopback-first HTTP/SSE adapter, depends on the
state/API boundary decisions in PRD-138, and aligns the HTTP direction in
PRD-121 with the existing kernel and session ownership boundaries. See the
implementation evidence at the end of the PRD.

PRD-151 implements the reliable command outcome and lifecycle contract behind
long-running builds and development servers. It adds canonical result states,
seconds-based deadlines, cancellation cleanup, explicit service/readiness mode,
and workflow completion gates so `next build` failures are visible and
`npm run dev` is manageable through PRD-149's owned terminals.

PRD-152 implements a direct-source authoring contract for `create_workflow`,
`create_tools`, and `create_commands`. Generated workflow phase prompts contain
runtime implementation instructions, tool and command modules carry explicit
artifact metadata, the inherited generic runner is valid without wrapper
boilerplate, and custom runner `super()` delegation is conditional on
intentional composition.

PRD-153 addresses the remaining reliability gap in agent-owned
`create_workflow` authoring by separating the read-only design handoff from the
write-capable execute phase, with capability filtering, a successful-write
receipt, and bounded failure recovery.

PRD-154 is the current reference for `create_workflow`. It records the clean-slate
rebuild that models the meta-workflow directly on `code_plan`: an outer loop
evolving typed phase state, one bounded async method per phase as the inner loop,
transitions only via tool calls, a typed context capturing each phase's artefact,
and a `design → generate → validate → summarize` graph whose validate phase
imports the generated file deterministically and overrides an agent approval that
contradicts the result. It supersedes the `create_workflow` portions of PRD-147,
PRD-152, and PRD-153.

PRD-155 is the implemented specification for consolidating the user-facing
mode catalogue into Safe, Plan, and Yolo. It treats Yolo as the current Auto
mode, keeps Plan hard-blocked, moves approval semantics into Safe, and makes
Safe the default for new sessions while defining compatibility for legacy Auto,
Guard, and other mode names. Its implementation record and verification plan
are authoritative for the current source tree.

PRD-156 proposes the workflow-specific continuation contract for Esc during
Plan-mode thinking. It preserves the active run in a durable checkpoint,
attaches queued follow-up input to the same run, and makes reset explicit so a
paused workflow can never silently restart from its first phase.

PRD-157 implements a canonical, session-scoped usage ledger so `/usage`, the
TUI token/cost display, direct turns, workflows, subagents, compaction, session
restore, and session inspection all use one idempotent durable accounting
source. It revises the live-token assumptions recorded in PRD-82 and PRD-83;
verification evidence is the PRD-157 test matrix and the repository quality
gates.

PRD-159 proposes an opt-in, policy-gated CloakBrowser integration with bounded
browser tools, an optional `cloakbrowser` packaging extra, session-owned
lifecycle management, safe artifacts, workflow capability/approval integration,
and checkpoint-aware rehydration. It keeps raw Playwright/CDP access,
model-controlled stealth settings, and persistent profiles outside the initial
trust boundary.

PRD-161 implements consolidation of contiguous exploratory tool calls into a
derived `Explored` TUI block while preserving every individual tool event,
result, journal record, checkpoint, and replay path. The presentation marker
is separate from `ToolCapability`, because the current capability enum controls
permission and phase filtering as well as metadata.

PRD-163 proposes a shared structured prompt/cache contract for every workflow
runner. It keeps stable system and tool regions reusable while phase state,
artifacts, summaries, questions, and transition details remain dynamic. It
also makes `CreateWorkflowRunner` teach, template, inspect, and validate
cache-stable generated workflows so downstream workflows inherit the common
runner behavior instead of implementing provider-specific caching.

PRD-164 specifies the repeated-idle-status-frame defect reproduced in the TUI.
It separates the animation frame clock from idle state, prevents redundant
idle Live redraws, and preserves active animation, input responsiveness,
transcript replay, and workflow notifications.

PRD-165 specifies the separate approval-wait redraw loop. It keeps the
wall-clock activity duration available for telemetry while preventing paused
`activity_elapsed_s` updates from repainting an unchanged Plan Review, tool
approval, or Questions overlay every session tick.

PRD-166 specifies the active-animation output policy. It preserves flower and
Thinking animation for replacement-capable terminals while using append-safe
snapshot behavior for captured or ANSI-insensitive clients, preventing active
status frames from appearing as duplicate transcript history.

PRD-167 specifies the cross-workspace @mention defect in which mention
resolution can inspect a sibling repository while filesystem tools remain
restricted to the current project, allowing the agent to substitute a local
same-named file. It defines one canonical workspace resolver, fail-closed
single-root behavior, explicitly configured multi-root access, exact-target
prompting, and TUI/headless/tool parity.

PRD-168 defines the implemented mode-aware policy for paths above or outside the current
workspace. It revises PRD-167's default out-of-scope decision so Safe mode
requests explicit approval before any outside-target I/O, while Yolo mode uses
an explicit unrestricted workspace policy without an agenthicc boundary prompt.
Plan remains read-only and blocked, OS/container permissions remain in force,
and mentions, filesystem tools, commands, workflows, subagents, headless runs,
resume, and replay all share the same resolver and policy decision.

PRD-169 specifies the cross-layer tool-call transaction invariant exposed by the
parallel `Read` regression. It makes lauren-ai validate and atomically commit
every assistant tool-call batch, makes provider adapters fail before sending
malformed history, and integrates durable repair, interruption, queued input,
workflow resume, and safe TUI/headless diagnostics in agenthicc.

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
