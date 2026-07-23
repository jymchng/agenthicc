---
title: "PRD-139: OpenCode-Inspired Product Expansion and Privacy-First Advertisements"
status: In progress
version: 0.1.0
created: 2026-07-22
related_prds:
  - PRD-138  # Repository Improvement Roadmap
  - PRD-11   # OAuth Authentication and TUI Text Advertisements
  - PRD-12   # MCP Integration
  - PRD-13   # Plugin System
  - PRD-46   # Command Plugins
  - PRD-81   # Workflow Revamp
  - PRD-114  # Composite Workflows
supersedes: []
tags:
  - product-expansion
  - opencode
  - sessions
  - agents
  - workflows
  - integrations
  - advertisements
  - privacy
---

# PRD-139 — OpenCode-Inspired Product Expansion and Privacy-First Advertisements

Implementation status: P0 project bootstrap (`agenthicc init` and `/init`) is
implemented in `src/agenthicc/project_bootstrap.py`, with CLI/TUI integration,
unit coverage in `tests/unit/test_project_bootstrap.py`, and end-to-end command
coverage in `tests/integration/test_project_bootstrap.py`. The remaining
features in this PRD remain proposed.

## 1. Executive summary

This PRD defines the next product expansion for agenthicc after a source-level
comparison with the current `dev` branch of
[anomalyco/opencode](https://github.com/anomalyco/opencode). It is an
inspiration and prioritisation document, not a request to copy OpenCode's
runtime, language, package layout, or product branding.

agenthicc already has substantial foundations that should remain authoritative:
the immutable kernel and reducer, the event processor, the Rich workspace,
headless JSON-lines runner, workflow registry and runners, subagent pool,
memory tiers, capability-aware tools, MCP bridge, plugin/skill/command
extension points, OAuth client, and lauren-ai's model/tool execution contract.
The opportunity is to connect those foundations into a more complete product
surface.

The recommended product direction is:

1. Make sessions reversible and branchable: fork, undo/redo, diffs, snapshots,
   named sessions, and reliable background/resume behaviour.
2. Make agents and workflows first-class user-configurable products: primary
   agents, subagents, per-agent permissions/models, durable todos, and
   background work.
3. Add code intelligence around the existing tools: project rules/bootstrap,
   LSP, formatters, structured output, richer patch review, and file/context
   references.
4. Provide a protocol boundary only after PRD-138's API decision: one backend
   that can serve the TUI, headless clients, IDEs, and future web clients.
5. Add GitHub/GitLab and CI workflows as controlled, auditable workflow
   adapters rather than as a second agent runtime.
6. Finish the existing advertisement design as an optional, first-party,
   privacy-first free-tier feature. Ads must never enter model context, tool
   selection, workflow state, session exports, or headless output.

The highest-confidence first delivery is reversible sessions plus configurable
agent profiles and project bootstrap. The highest-risk items are remote
clients, public sharing, enterprise identity, and monetisation; they require
the API/security work in PRD-138 first.

## 2. Evidence and comparison scope

### 2.1 Repository evidence

The comparison used the current source tree and tests, not historical PRD
examples. The relevant agenthicc ownership boundaries are:

| Surface | Current implementation | Assessment |
|---|---|---|
| Domain state and events | `src/agenthicc/kernel/state.py`, `kernel/events.py`, `kernel/reducer.py` | Strong foundation; new durable behaviour must use kernel events and pure reducers. |
| Event loop and persistence | `src/agenthicc/kernel/processor.py` and session journals/logs | Strong foundation; lifecycle, failure, and migration work remains in PRD-138. |
| Session construction | `src/agenthicc/runners/session_context.py` and `_build_session_context()` in `runners/tui_session.py` | Correct ownership boundary for session-scoped services. |
| Interactive client | `src/agenthicc/runners/tui_session.py`, `tui/conversation_store.py`, `tui/workspace/` | Rich and reactive, but lacks several session-control and code-intelligence views. |
| Headless client | `src/agenthicc/runners/headless.py` and the workflow CLI | Useful JSON-lines and workflow automation path; no network API or remote attach protocol. |
| Workflows | `src/agenthicc/workflows/`, workflow registry/plugins, `workflows` CLI, and headless workflow execution | A differentiating strength; the next step is user-facing composition, background execution, and durable control. |
| Agents and subagents | `src/agenthicc/agents/`, `src/agenthicc/subagents/` | Registry and concurrent pool exist; profiles, lifecycle controls, and UI need consolidation. |
| Tools and security | `src/agenthicc/tools/`, `agent_tools.py`, `security.py`, sandbox, capability metadata | Strong safety boundary; new tools must continue through it. |
| Extensions | `plugins/`, `skills/`, `commands/`, MCP registry, command/trigger registry | Broad but needs stable SDK contracts, discovery diagnostics, and managed lifecycle. |
| Memory and durability | `memory/`, session journal, file cache, session export/replay | Strong local durability; session lineage and reversible filesystem state are incomplete. |
| Identity and ads | `src/agenthicc/auth.py`, `src/agenthicc/ads.py`, auth commands | OAuth is implemented. Ads have a cache/fetch/rotation task and tests, but no current consumer renders `UIAdUpdate` in the workspace. |

### 2.2 OpenCode surfaces studied

The study covered the OpenCode monorepo, its current source package layout, and
the following official documentation pages:

- [Agents](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/agents.mdx)
  and [commands](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/commands.mdx)
  for primary agents, subagents, custom agents, mentions, and reusable prompts.
- [Server](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/server.mdx),
  [SDK](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/sdk.mdx),
  [web](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/web.mdx),
  and [ACP](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/acp.mdx)
  for the multi-client/backend architecture.
- [Plugins](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/plugins.mdx),
  [MCP servers](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/mcp-servers.mdx),
  and [skills](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/skills.mdx)
  for extension and external-tool patterns.
- [Permissions](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/permissions.mdx),
  [configuration](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/config.mdx),
  [rules](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/rules.mdx),
  [LSP](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/lsp.mdx),
  and [formatters](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/formatters.mdx)
  for project intelligence, policy, and code quality.
- [Sharing](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/share.mdx),
  [GitHub](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/github.mdx),
  [GitLab](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/gitlab.mdx),
  [IDE integration](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/ide.mdx),
  and [enterprise deployment](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/enterprise.mdx)
  for collaboration and operational deployment.
- [Models](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/models.mdx),
  [providers](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/providers.mdx),
  [network](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/network.mdx),
  [TUI](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/tui.mdx),
  [themes](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/themes.mdx),
  [keybinds](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/keybinds.mdx),
  [Zen](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/zen.mdx),
  and [custom tools](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/custom-tools.mdx)
  for model variants, proxy/certificate support, terminal customisation,
  managed gateways, and tool authoring.

OpenCode's repository also contains source surfaces for session forking,
revert/undo, snapshots, worktrees, background jobs, todo state, LSP, MCP,
providers, plugins, skills, GitHub/GitLab automation, sharing, and server
transport. These are treated as product patterns to evaluate, not as a
prescription to reproduce every implementation detail.

### 2.3 Decision principles

- `lauren-ai` remains the canonical model, agent, and callable-tool contract.
  OpenCode-inspired features must adapt to it; they must not introduce a
  second model dispatch or tool execution pipeline.
- The current ownership table in `AGENTS.md` and `CLAUDE.md` is normative.
  Historical paths such as `tui/app.py`, `tui/transcript.py`, and
  `tools/executor.py` are not valid destinations for new work.
- Existing registries remain single sources of truth. A new agent, command,
  skill, workflow, or tool catalog must not create a parallel built-in list.
- Local-first and fail-closed security remain defaults. Remote features are
  opt-in, explicit, inspectable, and disableable at project or organization
  scope.
- User data must not be sent to a service merely because a UI feature exists.
  Sharing, telemetry, ads, and remote execution each need separate consent and
  policy gates.
- A feature is not complete when its backend exists. It needs the relevant TUI,
  headless, persistence, security, tests, documentation, and failure-mode
  integration.

## 3. Feature status legend and comparison

`Current` means the capability is already usable in the current source and is
not proposed again. `Partial` means the foundation exists but the product
surface or contract is incomplete. `Gap` means the capability is a new
candidate. `Defer` means it is intentionally postponed or rejected.

Priority is relative to this PRD: `P0` is the next implementation wave, `P1`
requires the P0 contracts, and `P2` is later reach/scale work.

### 3.1 Sessions, clients, and reversible work

| OpenCode-inspired capability | agenthicc today | Proposed agenthicc feature | Status / priority |
|---|---|---|---|
| One session model usable by TUI, CLI, web, and IDE clients | TUI, headless JSON-lines, and CLI inspect/export paths are separate client surfaces | Define a client-neutral session service and event projection; keep the TUI and headless runner as adapters | Partial / P0-P1 |
| Continue, select, fork, and navigate child sessions | Resume and session listing/show/inspect exist; no first-class branch/fork UX | Add `/session`, `session fork`, child lineage, fork-at-message, and a session picker with search | Partial / P0 |
| Undo/redo/revert of conversation and filesystem changes | Durable logs and replay exist, but there is no safe user-facing reversible edit operation | Add message revert, unrevert, redo semantics, file-change checkpoints, and explicit confirmation for destructive rollback | Gap / P0 |
| Snapshots and session diffs | Git/status and session artifacts exist; no unified pre/post-turn snapshot contract | Capture a project-relative snapshot before mutating runs, show diff by turn/workflow phase, and restore through `WorkspaceView` | Partial / P0 |
| Named sessions, automatic titles, summaries, and search | Session IDs and logs exist; title/search/summaries are limited | Add editable titles, local title generation, full-text metadata search, summary regeneration, and child-session navigation | Partial / P1 |
| Abort, queue, retry, and resume interrupted work | Interrupt and resume paths exist; workflow and subagent lifecycle have separate control paths | Unify run coordinator controls across direct turns, workflows, and background subagents | Partial / P0 |
| Background jobs and concurrent sessions | Concurrent subagent pool exists, but no durable user-visible job manager | Add a bounded background job service with status, logs, cancellation, retry policy, and resource limits | Partial / P0-P1 |
| Headless `run` with model/agent/continue/fork options | Headless stdin and workflow CLI exist | Extend the headless contract with explicit session IDs, agent/profile selection, fork, structured events, and stable exit codes | Partial / P1 |
| HTTP server, attach, health, event stream, and generated client SDK | PRD-138 records the API boundary as undecided; no current server | Decide the API boundary, then expose a typed local server and attach protocol with auth, origin, and lifecycle controls | Gap / P1 |
| Browser web client and desktop application | No web or desktop client | Consider a web client after the local server/SDK is stable; desktop packaging is a later distribution project | Gap / P2 / defer desktop |
| Public conversation sharing and unsharing | Local session export exists; no public share service | Add manual, redacted, expiring share links only after privacy review; allow project/enterprise disablement | Gap / P1 / gated |

### 3.2 Agents, workflows, and task execution

| OpenCode-inspired capability | agenthicc today | Proposed agenthicc feature | Status / priority |
|---|---|---|---|
| Primary agent profiles such as build and plan | Modes provide capability ceilings and plan/auto-style behaviour | Make profiles explicit records with prompt, model, tools, mode ceiling, approval policy, and UI description | Partial / P0 |
| Read-only planning agent | Plan mode and workflow planning exist | Publish a first-class `plan` profile that is read-only by construction and produces a resumable execution plan | Partial / P0 |
| Specialized subagents such as explore, review, test, and general | Agent registry and concurrent subagent pool exist | Add built-in profile templates and typed task contracts; keep delegation through the existing pool and tool policy | Partial / P0 |
| Custom agents from project/user files | Agent plugins/registry exist but authoring/discovery is not a single documented contract | Support validated `agents/` definitions with frontmatter/TOML/Python adapter, precedence, trust prompt, and diagnostics | Partial / P0-P1 |
| Per-agent model, prompt, tools, permissions, and temperature/config | Modes and workflow phase overrides cover parts of this | Add a typed `AgentProfile` projection used by TUI, workflows, headless, and future API clients | Partial / P0 |
| `@agent` delegation and mention-aware context | Mentions and subagent pool exist | Make mention targets discoverable in the trigger picker, show delegation status, and require explicit scope for external side effects | Partial / P0 |
| Durable todo/task list | Workflow phase state and TUI progress exist; no general todo contract | Add session-scoped hierarchical todos with event-sourced status, ownership, dependencies, and resume behaviour | Partial / P0 |
| Background multi-agent orchestration | Pool supports concurrent workers | Add queue limits, per-agent budgets, aggregation, partial failure policy, and a live TUI job panel | Partial / P0-P1 |
| Workflow templates, custom commands, and reusable prompts | Workflow plugins, command registry, skills, and default creation skills exist | Add a unified workflow/profile/command authoring guide and discovery diagnostics without merging their ownership boundaries | Current foundation / P0 |
| Structured output and schema-validated phase results | Workflow outputs are typed in places, but not a universal provider contract | Add validated JSON-schema output for agent turns and workflow phases with bounded repair/retry | Gap / P1 |
| Automatic compaction/title/summary agents | Memory compaction exists | Make compaction, title, and summary policies explicit services with model/tool permissions and durable failure states | Partial / P1 |

### 3.3 Code intelligence and developer tools

| OpenCode-inspired capability | agenthicc today | Proposed agenthicc feature | Status / priority |
|---|---|---|---|
| Read, write, edit, patch, grep, glob, shell, tests, and web tools | Broad tool catalog already exists and is capability/sandbox aware | Consolidate user-facing descriptions and add consistent preview, timeout, retry, and result-size metadata | Current foundation / P0 |
| Unified patch/diff review before applying changes | File tools and Git/status exist | Add a patch planning result, per-file diff preview, approve/reject hunks, and rollback linkage to the session snapshot | Partial / P0 |
| Language Server Protocol | No canonical LSP ownership is present in the current source map | Add a process-lifecycle-managed LSP service with diagnostics, hover, definition, references, symbols, and permission-aware project paths | Gap / P1 |
| Formatter and linter integration | Test and shell tools can invoke project commands; no formatter registry | Add a formatter/linter registry with project detection, explicit commands, safe environment, timeouts, and post-edit hooks | Partial / P1 |
| Project file references and selected-line context | Mention/cache infrastructure exists | Add canonical `@file`, line-range, symbol, and selection references that resolve through `WorkspaceView` and are visible in transcripts | Partial / P0-P1 |
| Image and media attachments | No documented attachment contract | Add bounded local image attachment support through the provider capability contract; reject unsupported or oversized media before network calls | Gap / P1 |
| Persistent PTY/terminal sessions | Shell/exec tooling exists | Add an explicitly permissioned PTY tool for long-running interactive commands, with output truncation and kill-on-session-close | Gap / P1 |
| Repository map, ignore rules, symbol search, and file finder | File tools and memory/index surfaces exist | Add a cached project index respecting ignore files, workspace boundaries, symlinks, and redaction rules | Partial / P1 |
| Project bootstrap command | `AGENTS.md` and `CLAUDE.md` guidance exists, but no canonical initializer | Add `/init` and `agenthicc init` to inspect the repository and create/update concise project rules with a review step | Gap / P0 |
| Configurable instruction files and precedence | Config loader exists; repository guidance is external | Add documented project/global instruction discovery with explicit precedence, size limits, trust handling, and no remote URLs by default | Partial / P0 |

### 3.4 Extensions and external tools

| OpenCode-inspired capability | agenthicc today | Proposed agenthicc feature | Status / priority |
|---|---|---|---|
| Custom tools with typed schemas | Plugin tools, lauren-ai callable tools, legacy `Tool`, and executor adapters exist | Publish one stable authoring contract and compatibility adapter; validate schemas at registration and invocation | Partial / P0 |
| Plugin lifecycle hooks around tools, messages, permissions, sessions, and TUI | Plugin discovery and several hook/policy surfaces exist; hook docs still describe future work | Define versioned hook points with ordering, isolation, error policy, and redaction; do not let plugins bypass approval/sandbox | Partial / P1 |
| Project/global plugin discovery and dependency management | Project plugin discovery and trust exist | Add lockable plugin manifests, dependency conflict diagnostics, safe installation, and unload/disable operations | Partial / P1 |
| Skills with metadata, discovery, compatibility, and permissions | Skill loader/bootstrap and slash skill commands exist | Add frontmatter validation, canonical name rules, discovery diagnostics, per-agent skill permissions, and compatibility aliases | Partial / P0 |
| Local MCP servers | MCP bridge/registry exists | Document and harden stdio configuration, startup/shutdown, tool filtering, reconnect, and server health | Partial / P0 |
| Remote MCP servers and OAuth | Remote and auth coverage is incomplete or uneven | Add explicit HTTPS/streamable transport, OAuth credential storage, allowlists, timeout, reconnect, and per-agent tool scopes | Partial / P1 |
| Dynamic MCP management and catalog | Registry exists; runtime management needs a stable user contract | Add `mcp list/add/remove/auth`, status events, schema inspection, and approval for newly introduced capabilities | Partial / P1 |
| Tool list filtering per agent/model | Capability metadata and mode ceilings exist | Project an effective tool catalog per profile and log why a tool is allowed, hidden, or denied | Partial / P0 |
| Extension marketplace/catalog | No trusted marketplace | Defer until plugin trust, signing, dependency isolation, and review policy are complete | Gap / P2 / defer |

### 3.5 Providers, models, and response control

| OpenCode-inspired capability | agenthicc today | Proposed agenthicc feature | Status / priority |
|---|---|---|---|
| Multiple provider configuration | Anthropic/OpenAI/Ollama configuration exists | Keep provider selection in lauren-ai and add validated effective-config/model diagnostics | Current foundation / P0 |
| Provider auth/connect flows and credential status | agenthicc OAuth is for agenthicc.ai; provider keys are configuration-driven | Add provider connection checks and secure status reporting without exposing credentials | Partial / P1 |
| Model picker and per-turn model override | Model/config metadata and phase overrides exist | Add a TUI/CLI model picker backed by the effective provider registry, with profile and workflow override visibility | Partial / P0-P1 |
| Model aliases, fallback, latency/cost metadata, and budgets | Tokens/cost are visible in the TUI; routing is limited | Add explicit model profiles, fallback policy, max-cost/token budgets, and provider failure classification | Partial / P1 |
| Reasoning/model variants and effort controls | Provider/model configuration exists, but no common variant contract | Add provider-capability-aware effort variants with visible effective settings and bounded token/cost impact | Gap / P1 |
| Proxy, custom certificates, and provider network diagnostics | `NetworkGuard` controls destinations; no provider proxy/certificate UX | Add opt-in proxy/certificate configuration and a redacted connectivity diagnostic without weakening destination policy | Partial / P1 |
| Image input capability negotiation | No public contract | Add provider capability discovery and preflight validation | Gap / P1 |
| Structured JSON-schema output | No universal contract | Add schema validation and bounded retries to lauren-ai adapter and workflow result envelope | Gap / P1 |
| Curated managed model gateway, analogous to OpenCode Zen | No managed gateway | Consider an optional agenthicc.ai gateway only after provider transparency, billing, outage, data-handling, and local-provider rules are documented | Gap / P2 / gated |

### 3.6 Git, CI, and collaboration integrations

| OpenCode-inspired capability | agenthicc today | Proposed agenthicc feature | Status / priority |
|---|---|---|---|
| Git status/diff/commit support | Git/status tooling and session diffs exist | Add reversible commit preparation, branch/worktree awareness, and explicit commit approval | Partial / P0-P1 |
| Isolated worktree per task/session | No canonical task worktree lifecycle | Add safe worktree creation, naming, cleanup, path registration, and session/workflow association | Gap / P1 |
| GitHub issue triage and PR creation | No first-party GitHub workflow adapter | Add a GitHub integration that runs inside a checked-out/worktree boundary, requires scoped OAuth/token permissions, and produces reviewable patches/PR metadata | Gap / P1 |
| GitLab issue/MR automation | No first-party GitLab workflow adapter | Add a parallel provider-neutral integration interface after GitHub proves the policy and audit model | Gap / P2 |
| CI event triggers and scheduled agent runs | Workflow CLI/headless path exists | Add signed, idempotent CI triggers for issue/PR/review/schedule events with non-interactive approval policy | Gap / P1-P2 |
| Review comments and line context | No integrated review conversation | Add imported review context as a read-only event source; never allow untrusted comments to change policy | Gap / P1 |
| Slack/team chat control surface | OpenCode contains a Slack product package; agenthicc has no such client | Defer until the API, identity, audit, and approval protocol are stable | Gap / P2 / defer |
| Shareable session links | Session export exists | Add explicit redaction, expiration, unshare, access audit, and organization policy before any public service | Gap / P1 / gated |

### 3.7 TUI, configuration, operations, and enterprise

| OpenCode-inspired capability | agenthicc today | Proposed agenthicc feature | Status / priority |
|---|---|---|---|
| Themes and keybind customisation | Rich workspace and input capability layers exist | Publish validated theme/keymap configuration with conflict diagnostics and accessible defaults | Partial / P1 |
| Help, command discovery, session/model/theme pickers | Trigger picker and command registry exist | Add contextual help and searchable pickers driven by canonical registries | Partial / P0-P1 |
| External editor/export flow | Session export/inspect exists | Add `/editor`, `/export`, and copy-safe Markdown/JSON/patch exports with redaction controls | Partial / P1 |
| Rich TUI controls: mouse, scroll, attention notifications, and sound | Terminal portability and reactive workspace exist; these controls are not a unified product contract | Add opt-in terminal attention settings, mouse/scroll configuration, and capability-aware fallbacks for non-interactive terminals | Partial / P1 |
| Effective config and schema validation | Configuration loading exists; PRD-138 tracks improvements | Add `config check`, `config show --effective`, source provenance, and schema/version migration | Partial / P0 |
| Local server auth, CORS, mDNS, and attach | No server | Add only as part of the API boundary; bind localhost by default, require auth for non-local binds, and use exact CORS origins | Gap / P1 |
| Usage, cost, health, and audit views | TUI token/cost metrics and session inspection exist | Add local usage reports and optional structured audit export; no sensitive content in default telemetry | Partial / P1 |
| OpenTelemetry/metrics integration | No canonical external observability contract | Add opt-in redacted metrics/traces with provider/tool/session identifiers that are non-content and configurable | Gap / P2 |
| Central configuration, SSO, internal AI gateway | No enterprise control plane | Add organization policy only after local security baseline and public API are stable | Gap / P2 |
| Desktop packaging | No desktop app | Defer; first make TUI/server/SDK reliable and package installation reproducible | Gap / P2 / defer |
| Automatic updates | Packaging/install paths exist but no product-wide updater contract | Add signed, opt-in update checks; never silently replace a running or trusted binary | Gap / P2 |

## 4. Recommended product slices

The matrix is intentionally broad. The following slices turn it into shippable
features with visible user value.

### Slice A — Reversible sessions and workspaces (`P0`)

User experience:

- `/session` opens a searchable local session picker.
- `/fork [message-id]` creates a child session without mutating the parent.
- `/undo` reverts the most recent approved filesystem/message change;
  `/redo` restores it when safe.
- `/diff` shows the current turn/workflow diff and lets the user inspect files.
- A crashed direct turn or workflow can resume through one run coordinator.

Implementation boundary:

- Extend `kernel/events.py`, `kernel/reducer.py`, and persistence only for
  durable domain facts such as session lineage, checkpoints, and lifecycle.
- Keep filesystem snapshots behind `WorkspaceView` and the existing security
  policy. Do not implement rollback by writing arbitrary paths.
- Use `tui/conversation_store.py` and `tui/workspace/` for display state.
- Expose equivalent structured records in headless mode; never make headless
  output depend on Rich rendering.

Acceptance criteria:

- A forked session has a parent ID, independent append-only history, and the
  same project/security policy.
- Undo/revert refuses ambiguous or untracked destructive operations and asks
  for approval when a restore would overwrite user changes.
- A killed run can resume without duplicating completed tool side effects.
- TUI, headless, workflow, and session export tests cover the same lifecycle
  semantics.

### Slice B — Agent profiles and background work (`P0`)

Introduce a typed `AgentProfile` projection that can be consumed by the current
mode manager, workflow runner, subagent pool, CLI, and future API. A profile
contains:

- stable name and description;
- primary or subagent role;
- lauren-ai model/provider selection or inherited selection;
- system prompt/instruction sources;
- allowed tools and capability ceiling;
- approval policy and external-side-effect policy;
- concurrency, token, turn, and cost budgets;
- optional workflow bindings.

Built-in profiles should include read-only planning, full-access build,
exploration, review, test, and general delegation. They may map to existing
modes/agents internally, but the profile projection must be the one user-facing
description. A profile must never bypass `PermissionChecker`, `WorkspaceView`,
`NetworkGuard`, or the lauren-ai tool contract.

The background job panel should show queued/running/completed/failed/cancelled
states, the owning session/workflow/agent, elapsed time, bounded output, and a
retry/cancel action. It must not leak hidden tool arguments or credentials.

### Slice C — Project bootstrap and code intelligence (`P0-P1`)

Add `agenthicc init` and `/init` to inspect the current repository and produce a
small, reviewable `AGENTS.md` or project instruction file. It should capture
commands, architecture boundaries, testing commands, operational traps, and
references to existing instruction sources. It must not upload the repository
or overwrite an existing file without a diff and confirmation.

Then add LSP, formatters, symbol/file references, and patch review as separate
capabilities. Each must have an explicit capability declaration, path/network
policy, timeout, output limit, and non-interactive failure result.

### Slice D — Local protocol and clients (`P1`)

Only after PRD-138 decides the headless/API boundary, define a local server with:

- typed session, message, workflow, agent, tool, approval, file, diff, and event
  resources;
- a generated client contract and a small Python SDK;
- local-only binding by default;
- authentication for all non-local binds;
- exact CORS allowlists, no wildcard default;
- server health, graceful shutdown, connection ownership, and backpressure;
- an attach command for a TUI or other client to an existing session;
- an ACP adapter only if the protocol preserves agenthicc's approval, tool,
  workflow, and session semantics.

The web UI is a client of this boundary, not a new runtime. Desktop packaging
is not a dependency of the server slice.

### Slice E — Git provider automation (`P1-P2`)

Build GitHub first, then generalise the policy for GitLab. A provider adapter
must:

- run in an explicit repository/worktree and record the exact revision;
- accept only the minimum token scopes needed for the requested action;
- treat issue/PR text, comments, and labels as untrusted input;
- use the regular agent/profile/workflow/tool/approval path;
- create reviewable commits/patches and require approval before pushing or
  opening a PR/MR unless a separately configured CI policy allows it;
- be idempotent across retries and expose structured audit events.

## 5. Advertisement serving design

### 5.1 Current implementation and gap

OAuth and the advertisement skeleton already exist:

- [`src/agenthicc/auth.py`](../src/agenthicc/auth.py) stores OAuth tokens and
  exposes the account plan.
- [`src/agenthicc/ads.py`](../src/agenthicc/ads.py) defines `AdRecord`,
  `AdCache`, and `AdRotator`; it fetches a first-party catalog with a bearer
  token, caches it, rotates every 60 seconds, and fails open when the network
  fails.
- `TUISession.run()` starts the rotator for an authenticated non-Pro account.
- [`PRD-11`](prd-11-oauth-and-ads.md) already rules out images, tracking
  pixels, personalised targeting, multiple simultaneous ads, and headless
  stdout ads.

The remaining product gap is material: the rotator emits `UIAdUpdate`, but the
current reducer does not register an ad handler and the current workspace does
not consume that event into a reactive signal or render an advertisement. The
feature is therefore partially wired, not a finished user-visible capability.
The old PRD-11 architecture references stale `tui/events.py` and
`tui/transcript.py` paths; this PRD supersedes those implementation paths with
the current session/store/workspace boundaries.

### 5.2 Eligibility and policy

Ads are a presentation-only monetisation feature. The default eligibility rule
is:

| Condition | Serve an ad? |
|---|---:|
| Interactive TUI, authenticated token valid, account plan is `free`, local ads setting enabled | Yes |
| Pro or enterprise plan | No |
| Not logged in or plan cannot be determined | No |
| Headless stdin, workflow CLI, CI, replay, export, or test mode | No |
| Organization/project policy disables ads | No |
| Network policy does not allow the first-party ads origin | No |

The client must never send repository paths, file contents, prompts, model
messages, tool arguments, workflow state, session IDs, or semantic-memory
values to the ad service. Eligibility is based only on local account plan and
explicit settings. Locale and app version may be sent only if separately
approved as non-sensitive request metadata.

Ads must not:

- be inserted into a system prompt, user prompt, tool description, memory,
  workflow phase, agent profile, or model response;
- influence tool ranking, model selection, workflow transitions, or approvals;
- be written to the durable kernel log, conversation journal, session export,
  cassette, or headless JSON-lines stream by default;
- contain Rich markup, ANSI escapes, HTML, images, executable content, or an
  arbitrary command;
- auto-open a URL or create a network request on behalf of a user action;
- use impression/click telemetry by default.

### 5.3 Catalog and server contract

The first-party endpoint may remain
`https://api.agenthicc.ai/v1/ads`, but it must be treated as a versioned,
allowlisted external integration. A catalog response should be equivalent to:

```json
{
  "schema_version": 1,
  "catalog_id": "catalog-2026-07-22",
  "expires_at": "2026-07-23T00:00:00Z",
  "ads": [
    {
      "id": "ad-123",
      "sponsor": "Example sponsor",
      "text": "Sponsored: a short plain-text message.",
      "cta_url": "https://example.com/learn",
      "plans": ["free"],
      "locale": "*",
      "starts_at": "2026-07-22T00:00:00Z",
      "expires_at": "2026-07-31T00:00:00Z",
      "priority": 10
    }
  ]
}
```

Required server rules:

- Serve only HTTPS and return a strict schema version. Unknown fields are
  ignored; unknown required fields or invalid records invalidate that record,
  not the entire client.
- Enforce campaign start/end, plan, locale, frequency, and content review on
  the server. Do not require a client impression callback to rotate ads.
- Require `text` to be plain UTF-8 with a hard client-side maximum of 120
  characters. The client truncates defensively and escapes before Rich render.
- Require `sponsor` and display a visible `Sponsored` label. A missing or
  invalid sponsor makes the record ineligible.
- Permit `cta_url` only as display-only text. Validate its scheme (`https`)
  and, preferably, an approved sponsor-domain policy. Do not follow it
  automatically.
- Return no user or repository data in the response. Do not use ad copy to
  carry prompt injection, executable instructions, or tool recommendations.
- Consider signing the catalog with a published Ed25519 verification key so a
  cached catalog can be authenticated independently of transport. A failed
  signature must result in no ad, never in a trust downgrade.

The client request uses the existing `AuthClient` and
`agenthicc_http_client()` path, with `NetworkGuard`/first-party allowlisting,
short timeout, no redirect to an unapproved origin, and no token in logs or
cache files.

### 5.4 Client lifecycle and rendering

Use the current ownership boundary:

1. `AdRotator` becomes an `AdService` with explicit eligibility, catalog
   validation, cache, rotation, dismissal, and stop semantics.
2. `TUISession.run()` owns its lifetime and stops it in the same `finally` path
   as the workspace, processor, and tick task. A cancellation must not leave a
   background task or refresh loop behind.
3. The service updates a presentation-only signal in
   `tui/conversation_store.py`, or invokes a typed callback owned by the
   session. It must not invent a durable kernel state field merely to repaint a
   status panel.
4. `tui/workspace/` renders one small, bounded sponsored panel/status line. The
   renderer uses Rich escaping and never treats ad text as markup.
5. The existing `UIAdUpdate` emission is either removed in favour of the typed
   presentation callback or formally classified as an ephemeral UI event. It
   must not be persisted/replayed as domain state unless a separate product
   decision requires an auditable display record.
6. Dismissal is local and time-bounded. Preserve the existing five-minute
   intent, but do not steal the global Escape action used to interrupt an agent;
   use the ad's focused action or a canonical command such as `/ad dismiss`.
7. Rotation is based on active display time, not a wall-clock network timer
   that blocks a turn. Refresh and rotation failures leave the panel empty.

The visible panel should contain `Sponsored`, sponsor, short text, and an
optional display-only URL. It must not appear in the transcript or change the
height/layout contract when absent.

### 5.5 Cache, privacy, and failure rules

- Cache only validated catalog records and expiry metadata. Use a versioned
  schema and atomic replacement; a corrupt or incompatible cache is discarded.
- Store the cache in the user/project metadata location selected by the
  existing storage policy, not in a repository-tracked file. Ads contain no
  secrets, but file permissions should still avoid exposing account metadata.
- Never log bearer tokens, account email, user ID, raw response bodies, or ad
  request headers. Error logs contain only a stable category such as
  `ads_timeout` or `ads_invalid_catalog`.
- No network call is made when ads are disabled, the account is paid, the
  session is headless, or no valid token is available.
- A timeout, 4xx/5xx response, invalid record, bad signature, denied network,
  or cache failure is a silent no-ad state. Ads can never prevent startup,
  input, agent execution, shutdown, or resume.
- Default telemetry is zero. If campaign measurement is later required, it
  needs a separate consent and privacy review, aggregate reporting, no content
  fields, no click tracking, and a random non-account installation identifier.

### 5.6 Advertisement acceptance criteria

- Free authenticated TUI users can see one validated, clearly labelled,
  plain-text sponsored panel without blocking the event loop.
- Pro, enterprise, unauthenticated, headless, CI, replay, export, and disabled
  sessions make no ad request and render no ad.
- An ad update reaches the reactive workspace exactly once per displayed
  record; it does not enter the transcript, kernel persistence, model context,
  or headless stdout.
- Dismissal hides the current panel for five minutes without breaking Escape
  interrupt behaviour or affecting another session.
- Invalid JSON, invalid UTF-8, overlong text, markup, unsupported URL, expired
  campaign, invalid signature, timeout, and network denial are all tested and
  produce no ad.
- Tests prove no prompt, path, tool argument, token, account email, or session
  contents are sent to the endpoint or written to logs/cache.
- Documentation explains eligibility, disablement, data handling, and the
  distinction between sponsored copy and agent/tool behaviour.

## 6. Delivery roadmap

### P0 — High-confidence local product value

1. Complete PRD-138's truthful baseline, state-boundary, security, config, and
   persistence work that the new session features depend on.
2. Implement session lineage, fork, checkpoint, diff, safe revert, and unified
   run coordination for direct turns, workflows, and subagent jobs.
3. Introduce the `AgentProfile` projection, publish plan/build/explore/review/
   test/general profiles, and add profile-aware tool/approval diagnostics.
4. Add durable session todos and a bounded background-job panel.
5. Add project bootstrap (`init`/`/init`) and validated local instruction-file
   discovery.
6. Finish advertisements behind an explicit local feature gate using the
   privacy and lifecycle rules in section 5.

### P1 — Code intelligence and controlled remote surfaces

1. Add patch review, file/symbol references, LSP, formatters, and structured
   output as individually permissioned capabilities.
2. Harden MCP remote/OAuth/dynamic management and the stable plugin/skill SDK.
3. Decide and implement the local server/SDK/attach contract from PRD-138;
   then evaluate ACP and a web client as adapters.
4. Add local session sharing with redaction/expiry, followed by explicit
   GitHub automation and isolated worktrees.
5. Add model/provider health, model picker, fallback, and budget controls.

### P2 — Reach, teams, and monetisation scale

1. Add GitLab and CI event adapters after GitHub policy is proven.
2. Add opt-in remote observability, organization policy, central config, SSO,
   and internal AI gateway support.
3. Re-evaluate a managed model gateway, web/desktop clients, Slack/team
   surfaces, plugin catalog, and signed update service.
4. Consider richer sponsored extension recommendations only if they remain
   clearly labelled, opt-in where required, non-personalised, and completely
   separate from tool/model selection.

## 7. Security, privacy, and migration

- Existing projects continue to run with local TUI/headless behaviour when new
  features are not configured.
- New remote capabilities default to disabled or localhost-only. Configuration
  migration must be versioned and report effective values and their source.
- Existing `AdRotator` cache files are either migrated through a versioned
  loader or safely ignored; no raw token or response body migration is allowed.
- Existing `UIAdUpdate` tests should be retained while adding integration tests
  for the new presentation path. If the event is removed, update tests and
  docs together rather than leaving a dead public event.
- Plugin, MCP, Git provider, LSP, PTY, server, and sharing features must all
  pass through the existing `WorkspaceView`, `NetworkGuard`, capability,
  approval, and trust contracts.
- Treat external repository text, advertisements, issue/PR comments, MCP
  descriptions, remote instructions, and plugin metadata as untrusted input.
- Never weaken a security default to make a demo, ad, remote client, or CI
  integration work.

## 8. Open decisions

| ID | Decision | Owner / dependency |
|---|---|---|
| OD-01 | Is the public API a local HTTP server, a library boundary, or both? | PRD-138 P0.2 |
| OD-02 | Which ACP subset can preserve agenthicc approvals, workflows, and tool policy? | API decision plus protocol review |
| OD-03 | Should filesystem checkpoints use Git-native snapshots, a separate content-addressed store, or both? | Storage/security owners |
| OD-04 | Is `/init` allowed to update an existing `AGENTS.md`, or must it always create a patch for review? | Product/security review |
| OD-05 | What provider capabilities are guaranteed by the lauren-ai adapter for image input and structured output? | Runtime/provider owners |
| OD-06 | Is the first-party ad catalog signed in addition to HTTPS, and which sponsor-domain policy is used? | Identity/ads/security owners |
| OD-07 | Which free-tier jurisdictions and organization policies allow sponsored UI, and what consent/notice is required? | Legal/product review |
| OD-08 | Is session sharing hosted by agenthicc.ai, self-hosted, or both? | Privacy/API/enterprise owners |
| OD-09 | Which GitHub/GitLab token scopes and CI approval defaults are acceptable? | Integrations/security owners |
| OD-10 | Are desktop, Slack, plugin marketplace, and managed gateway investments justified after P1 usage evidence? | Product review |

## 9. Measurement plan

Measure local product value without collecting message or repository content:

- session fork/revert success rate and duplicate-side-effect rate;
- percentage of interrupted runs that resume successfully;
- profile selection, background-job completion, and workflow retry outcomes;
- time from project start to a reviewed patch;
- LSP/formatter invocation success and timeout rates;
- plugin/MCP registration failures and approval denials;
- server/SDK reconnect and backpressure behaviour once shipped;
- advertisement fetch success, no-ad fallback, dismissal, and paid-tier
  suppression as local counters only by default.

Any remote metrics require opt-in, a documented payload schema, retention
limits, and a separate privacy review. Do not use ad impressions or feature
telemetry to infer repository content, prompts, or user intent.

## 10. Verification and definition of done

This PRD is complete when the roadmap is accepted, ownership and dependencies
are recorded, and each implemented slice has evidence in the current source
tree. For code slices, run the relevant checks from `AGENTS.md`, including:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

Run `uv run nox -s llms_check` when public symbols or exported contracts
change. For the advertisement slice, the minimum evidence is unit coverage of
catalog validation/cache/eligibility and integration coverage of session
lifecycle, reactive rendering, no-headless-output, token/log redaction, and
network-failure behaviour.

Required documentation updates for implementation are:

- `README.md` and a guide under `docs/guides/` for user-visible features;
- `docs/guides/architecture.md` or `docs/reference/storage.md` for server,
  session, snapshot, or retention changes;
- `docs/guides/security.md` for remote tools, sharing, provider, Git, or ad
  trust boundaries;
- `llms-full.txt` and appropriate `llms.txt` entries for public Python symbols;
- this PRD and the relevant PRD status/evidence links as slices ship.
