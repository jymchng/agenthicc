---
title: "MCP-00: MCP audit and programme"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P0
related_prds:
  - prd-12
  - prd-28
  - prd-29
  - prd-31
  - prd-146
  - prd-172
  - prd-191
  - TOOLR-06
  - COMM-05
---

# MCP-00 — MCP audit and programme

agenthicc can load tools from MCP servers and run them. This programme finds where that
falls short of what a user, an operator and the specification expect, and orders the fixes.
It was written by reading agenthicc and `lauren-mcp` side by side, running a few probes
against released `lauren-mcp` versions, and reading the MCP specification, because two
earlier PRDs (PRD-172, TOOLR-06) were marked *Implemented* while parts of what they
promise are not in the code.

## Why now

On 2026-11-28 an operator's goal flow ran four tools without the MCP server it depended on
(`opengame`) and had to be interrupted. The notice said only "unavailable; continuing
without its tools". `/mcp reload` showed the cause: `lauren_mcp is not installed. Install
it with: pip install lauren-mcp` (MCP-01). Looking for why that was possible turned up a
chain of findings below. The most consequential is not about installation: the
specification's **current revision, 2026-07-28, is not backward compatible with the
handshake-based client agenthicc ships** (MCP-13).

## How the evidence was gathered

Each PRD names the file and line that proves a claim. The sources, and how far each was
checked, are these.

| Source | What was done |
|---|---|
| agenthicc at `main` `7174c3a` | Read in source. Every cited line was re-read after writing, and several citations that pointed at the wrong lines were corrected. |
| `lauren-mcp` checkout at `v1.2.1-6-gf255038` (locked release is `1.2.1`) | Read in source. |
| **Observed by running** | `import lauren_mcp` fails in the project virtualenv. In throwaway environments with released `lauren-mcp` 1.2.1 and 1.1.0: the factory names, the stdio signature, and `TypeError` on `cwd` under 1.1.0. Against a real annotated stdio server on 1.2.1: the wire result carries `title`, `annotations` and prompt `arguments`, and the client returns them as `None`/`[]`; the client has no `instructions`, no `list_resource_templates`, and a public `stderr_tail`. In the project virtualenv, an `AgenthiccMcpTool` registered with `build_registry` has empty capabilities and the gate's classifier returns `{UNDECLARED}`. |
| MCP specification (modelcontextprotocol.io) | Read on 2026-11-28: for `2025-06-18` the lifecycle, tools, elicitation, sampling and authorization pages; for `2026-07-28` the versioning, changelog, deprecated-features and client-registration pages. |
| `lauren-mcp`'s own research (`prds/2026-11-landscape/`) | Used for: what upstream plans, other hosts' config shapes and context budgets. **Not independently verified** and labelled as such where used. |

**Not done.** No third-party MCP server was connected to agenthicc. The tasks extension's
own page and the `2026-07-28` authorization page were not read. The numbers MCP-11 asks for
(what a real catalog costs) were not measured. The incident was not reproduced: which
interpreter agenthicc ran under is unknown. One claim written during drafting (that the stdio
client's `stderr_tail` was private) was wrong, was caught by a probe, and was withdrawn.

## Audit: what is true today

Severity is the cost of leaving it. **P0**: a user hits it in ordinary use or it is a
security hole. **P1**: it breaks a promise or a spec requirement. **P2**: a missing capability.

| # | Finding | Sev | PRD |
|---|---|---|---|
| F1 | `lauren-mcp` is in the optional `mcp` extra and in neither `dependencies` nor `dev`. CI never imports it; MCP tests run on stand-ins; the one real-server test needs `npx` and an env var. | P0 | 01, 03 |
| F2 | `mypy` is told to ignore missing imports for `agenthicc.tools.mcp`, so the bridge is type-checked without the library it calls. | P1 | 01, 03 |
| F3 | A missing client is reported as "unavailable; continuing without its tools" with no cause; the in-product fix text (`pip install lauren-mcp`) is not the documented one and omits the transports. | P0 | 01 |
| F4 | Skills, agents and workflows cannot declare the MCP servers they need; a flow runs without them, including when a server is merely still starting. | P1 | 07 |
| F5 | The current spec revision (`2026-07-28`) removes the handshake and sessions; a legacy client against a modern-only server **fails with no fall-forward**. The library is legacy-only (`LATEST = 2025-11-25`, no `server/discover`). | P0 | 13, 01 |
| F6 | The stdio client is built with `cwd`/`env`/`startup_timeout`; on `TypeError` (released `lauren-mcp` 1.1.0, which the manifest allows) it retries **without them**, so the child inherits the full environment. | P0 | 02 |
| F7 | `transport = "sse"` is connected as Streamable HTTP: `McpServer` has no `sse` factory (observed). | P1 | 02 |
| F8 | The CLI and runtime accept different transport names, and `http` means legacy SSE in the library but Streamable HTTP in agenthicc. | P1 | 02, 12 |
| F9 | `call_tool(..., tool_call_id=...)` fails with `TypeError` on every real call and is retried without it; a genuine `TypeError` is indistinguishable. | P1 | 02 |
| F10 | The library discards server `instructions`, so agenthicc's provenance-labelled instruction block can never be filled. | P2 | 02 |
| F11 | `list_tools`/`list_prompts`/`list_resources` drop `title`, `annotations`, `outputSchema` and prompt `arguments` that the wire carries (observed), follow no `nextCursor`, and there is no resource-template listing. | P1 | 02, 06, 08 |
| F12 | A timeout or an operator interrupt abandons a call without telling the server, and leaves a pending request in the library. | P1 | 04 |
| F13 | One timeout for every tool of a server; no progress token is sent, so progress cannot flow; the TUI has no per-call progress. | P1 | 04 |
| F14 | No health check; `DEGRADED` is set only by a failed refresh; `AUTHENTICATING` is never set; no bound on concurrent calls. | P2 | 04 |
| F15 | A project's own config starts stdio servers and attaches named environment variables to any URL **with no trust decision**, though every other extension kind is gated. | P0 | 05 |
| F16 | The network allow-list reaches MCP only when non-empty, is checked once on the first URL, and the HTTP client follows 20 redirects. | P1 | 05 |
| F17 | stdio servers have no filesystem or network confinement; a fail-closed Bubblewrap launcher exists for worktree commands. | P2 | 05 |
| F18 | A server's changed tool descriptions are accepted silently. | P1 | 05 |
| F19 | MCP tools are `UNDECLARED`: always prompt in Safe, blocked in Plan, even a read-only search. | P1 | 06 |
| F20 | `default_approval_mode` and `tool_approval` are parsed and never read, though PRD-172 §9.3 specifies them and is marked *Implemented*. | P1 | 06 |
| F21 | Tool annotations and `outputSchema` are unused; `structuredContent` is never validated. | P2 | 06 |
| F22 | Resources and prompts are listed at startup, stored in the snapshot, and never used by the user or the model. | P2 | 08 |
| F23 | The client advertises no capabilities, so elicitation is unavailable. Sampling and roots are also unavailable, and the current spec **deprecates** them. | P2 | 09 |
| F24 | `oauth` is a stub: it parks a server in `needs_auth` forever; `mcp auth` checks that an env var is set; `mcp logout` prints a sentence; auth failures are found by searching error text for "401". | P1 | 10 |
| F25 | Every tool of every ready server is sent on every request, with unbounded descriptions, and nothing reports the cost. | P2 | 11 |
| F26 | `mcp add` can set a third of a definition, stores a stdio command as a string in `url`, and cannot import another host's config. | P2 | 12 |
| F27 | agenthicc cannot be used from another MCP host. PRD-172 excluded it deliberately. | P3 | 14 |

Smaller defects, fixed inside the PRD named: the guide documents `error_code:
"remote_error"` where the code returns `reason: "remote_error"` (12); `mcp add NAME
server.py` generates an `lmcp` command that needs `lauren-mcp[cli]`, which agenthicc does
not depend on (12); agenthicc defines `McpServerConfig` and `McpToolBridge`, the same names
the library exports (02 note); `McpServerState.AUTHENTICATING` is dead (04, 10).

## The programme

| PRD | Title | Pri | Needs | Upstream asks |
|---|---|---|---|---|
| [MCP-01](MCP-01-mcp-works-or-says-why-not.md) | MCP works out of the box, or says exactly why it does not | P0 | — | — |
| [MCP-02](MCP-02-typed-client-adapter-and-lauren-mcp-contract.md) | One typed client adapter, and the contract agenthicc needs from lauren-mcp | P0 | 03 | U1–U9 |
| [MCP-03](MCP-03-mcp-conformance-harness-and-ci.md) | A conformance harness — the real client against real servers, in CI | P1 | 01 | — |
| [MCP-13](MCP-13-the-current-protocol-revision.md) | Speak the current MCP revision (2026-07-28) as well as the legacy one | P1 | 02, 03 | U10–U14 |
| [MCP-04](MCP-04-in-flight-call-control-and-server-telemetry.md) | In-flight calls can be cancelled, bounded and observed | P1 | 02, 03 | U5, U6 |
| [MCP-05](MCP-05-trust-and-isolation-of-mcp-servers.md) | A repository cannot start or feed an MCP server on its own | P1 | 03 | U9 |
| [MCP-06](MCP-06-tool-metadata-classification-and-approval.md) | Tool metadata, classification and approval | P1 | 02, 05 | U2 |
| [MCP-07](MCP-07-declared-mcp-dependencies.md) | Skills, agents and workflows declare the servers they need | P2 | 01, 05 | — |
| [MCP-08](MCP-08-resources-and-prompts.md) | Resources and prompts, usable by the operator and the model | P2 | 02, 05 | U1, U2, U4 |
| [MCP-09](MCP-09-server-initiated-requests.md) | Servers can ask the operator, through one host-side input model | P2 | 02, 05 | — |
| [MCP-10](MCP-10-oauth-for-remote-servers.md) | OAuth for remote servers | P2 | 02, 03, 05 | (U7: optional) |
| [MCP-11](MCP-11-catalog-scale-and-context-budget.md) | Many tools without spending the whole context window | P2 | 02, 06 | U1, U3 |
| [MCP-12](MCP-12-configuration-ux-and-interop.md) | Configure servers completely from the CLI, and import from other hosts | P2 | 02, 05 | — |
| [MCP-14](MCP-14-agenthicc-as-an-mcp-server.md) | agenthicc as an MCP server (proposal, needs a decision) | P3 | 05, 13 | — |

MCP-13 is numbered last because it was written last; by priority it sits with the first wave.

```text
MCP-01 ─┬─ MCP-03 ─┬─ MCP-02 ─┬─ MCP-04
        │          │          ├─ MCP-06 ── MCP-11
        │          │          ├─ MCP-08
        │          │          ├─ MCP-09 ── (MCP-13 D6)
        │          │          ├─ MCP-10
        │          │          └─ MCP-12
        │          ├─ MCP-05 ──┬─ MCP-06, 07, 08, 09, 10, 12
        │          │           └─ MCP-14
        └─ MCP-07  └─ MCP-13 (era axis; library-gated)
```

### Suggested order

1. **Now, small and independent.** MCP-01 D2–D5 (honest errors, the real client in CI);
   MCP-13 D3 and D8 (say plainly when a server is modern-only; never replay a tool call).
   MCP-05 D1–D4 is the security fix and should follow immediately.
2. **Foundation.** MCP-03 (harness, with the era axis) and MCP-02 (adapter, version floor,
   one transport vocabulary).
3. **Core behaviour.** MCP-04, MCP-06, MCP-07.
4. **Capabilities.** MCP-08, MCP-09, MCP-10, MCP-11, MCP-12.
5. **Gated or optional.** MCP-13's era support follows the library's dual-era client;
   MCP-14 follows the decision.

## What the library has to provide

One list, so upstream work can be filed once. "Planned" means a draft in `lauren-mcp`'s own
`prds/2026-11-landscape/` (not released).

| ID | Request | Needed by | Planned upstream |
|---|---|---|---|
| U1 | follow `nextCursor` on all list calls | 02, 08, 11 | no |
| U2 | return `title`, `annotations`, `outputSchema`, `_meta`, prompt `arguments` that the types already model | 02, 06, 08 | no |
| U3 | expose `instructions` from `initialize` | 02, 11 | no |
| U4 | `list_resource_templates()` | 08 | mentioned |
| U5 | `call_tool` with progress token / meta and a typed result | 04 | partly |
| U6 | `cancel()` and `notifications/cancelled` on task cancel | 04 | yes (client parity, P2) |
| U7 | authorization-code + PKCE, discovery, persistent token storage hook | 10 | partly |
| U8 | public typed error data | 04 | exported |
| U9 | HTTP policy hook (redirects, per-hop checks) | 05 | no |
| U10–U14 | dual-era client; result cache; `subscriptions/listen`; MRTR; tasks extension | 13 | yes (several P1/P2 drafts) |

## Decisions for the maintainer

These are recorded where they arise; collected here so none is lost.

1. **MCP-01.** Make `lauren-mcp[http,ws]` a base dependency (recommended) or keep the
   extra and rely on the diagnostics.
2. **MCP-05.** Existing project-defined servers become untrusted on upgrade. Accept that
   behaviour change?
3. **MCP-06.** Give `default_approval_mode` / `tool_approval` a meaning (D4) or delete them.
4. **MCP-09.** Not adopting sampling and roots, because the current spec deprecates them.
5. **MCP-10.** Who hosts agenthicc's Client ID Metadata Document, and at what URL?
6. **MCP-11 and MCP-04.** The proposed defaults are placeholders until measured.
7. **MCP-14.** Should agenthicc become an MCP server at all? (Reverses a PRD-172 non-goal.)

## Relationship to earlier PRDs

PRD-172 and TOOLR-06 stay as history. This series delivers what PRD-172 staged and did not
build (Phase 3: OAuth, prompts and resources, elicitation; §9.3's approval model), and
records that sampling, roots and logging are not carried forward. PRD-146 (CLI
registration) is extended by MCP-12; PRD-191 (startup isolation) is extended by MCP-07 and
MCP-13. `prds/README.md` links each PRD here when it is implemented.

## How to read a PRD in this series

Each one has the same shape: evidence with file and line, goals and non-goals, a design a
reviewer can disagree with item by item, failure handling, security, rollout, numbered
acceptance criteria and the verification that proves them. Acceptance criteria that need
a peer server name the MCP-03 scenario.
