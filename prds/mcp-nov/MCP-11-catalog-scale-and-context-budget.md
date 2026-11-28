---
title: "MCP-11: Many MCP tools without spending the whole context window on them"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P2
related_prds:
  - MCP-00
  - MCP-02
  - MCP-03
  - MCP-06
  - prd-172
  - TOOLR-06
---

# MCP-11 — Many MCP tools without spending the whole context window on them

Every tool of every ready MCP server is offered to the model on every request, in full.
A few servers with a few dozen tools each, or one server with hundreds, leave less room
for the work the session is for. Nothing measures the cost, limits it or lets the model
find a tool without carrying all of them.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | Each turn builds its registry from `ctx.mcp_registry.all_tools()`: every published tool of every ready server, with name, description and the provider-safe input schema. | `runners/agent_turn.py:1556,1565-1572` |
| 2 | The only bounds on a catalog are generous: 10,000 tools per server, 1,000,000 bytes per input schema, and 32,000 characters of server instructions. **A tool's description has no bound.** | `tools/mcp_manager.py:58-60` |
| 3 | Nothing reports what the catalog costs. `/mcp status` shows a tool count; there is no token figure per server or in total, and no warning. | `tools/mcp_manager.py:799-834`, `commands/builtins.py:699-760` |
| 4 | No tool search or deferred loading exists for MCP tools. The only filtering is the operator's `enabled_tools` / `disabled_tools`. | `tools/mcp_manager.py:913-919` |
| 5 | Tool-surface generations are the existing mechanism for changing the visible tools mid-session: an immutable `ToolSurface(generation, tools, sources)` published through a holder, read by turns at request time and pinned by subagent workers at spawn. | `tools/surface.py:17-45`; `runners/agent_turn.py:1552-1560`; `docs/guides/tools.md` |
| 6 | PRD-172 §7.2 made the catalog **cache-stable**: it changes only when a published catalog's hash changes (`catalog_hash`, `revision`). Any new design must keep that property. | `prds/prd-172-…md:407-442`; `tools/mcp_manager.py:947-964` |
| 7 | Other hosts ship deferred tool loading and size caps. lauren-mcp's research records, among others, that Claude Code defers tool descriptions by default, with a 16,384-character description cap and 10,000/25,000-token output thresholds. **These figures are upstream's research, not verified here.** | `lauren-mcp` `prds/2026-11-landscape/VERSIONS-MATRIX.md` §6, §6.1 (E-191) |
| 8 | Whether subagent workers are offered MCP tools has no test. They receive the parent turn's registry, which includes MCP tools, so they should; no test pins it. | `runners/agent_turn.py:1894-1915`; `grep -rn mcp tests/*/test_subagent*` finds nothing |

## Goals

1. The operator can see, per server and in total, what the MCP catalog costs in the
   context window.
2. A bound exists, set from a measured default, and exceeding it has a defined effect.
3. When the bound is exceeded, the model can still reach every tool, through search,
   without the catalog being carried in full.
4. Approvals, capabilities, names and result handling are identical for a deferred tool
   and a visible one.
5. The cache-stability property of PRD-172 holds, and subagents see the same behaviour as
   the main agent.

## Non-goals

- Reducing what the model sees for a small catalog. Under the budget nothing changes.
- Ranking tools by quality or learning from use.
- Compressing schemas beyond the bounds in D2.

## Design

### D1 — Measure first

`McpSessionManager` computes, per server and in total, the size of what would be sent:
tool names, descriptions and provider-safe input schemas, as tokens. The implementer
uses the project's existing context-usage measurement or the provider's token counter
when the transport has one, and otherwise a documented characters-per-token estimate
(none was found by search of `memory/` and `runners/`, which should be confirmed
before choosing). The figure appears in `/mcp status`, `mcp doctor --json` and, over a
warning level, in the status line (`mcp 18k tokens`). **Before any default is set,
the implementation PR records the measurement for at least five real servers**; the
defaults below are placeholders until then.

### D2 — Bounds

| Setting | Meaning | Proposed default |
|---|---|---|
| `max_description_chars` (per server) | each tool description cut at this length, `…` appended, `truncated` noted in `doctor` | 1,024 |
| `max_tools` (per server) | publish at most this many tools, in name order, with a warning naming the rest | 200 |
| `[tools] mcp_tool_budget_tokens` | the total across servers that stays visible | 10% of the smallest context window among the configured models |

Existing hard caps stay. Whitespace is normalised; a description that is only markup
or an image link is replaced by the tool name.

### D3 — Deferral by search

When the visible total would exceed the budget, tools are admitted whole servers at a
time in configured order until the next would not fit; the remaining tools are
**deferred**: registered with the manager but absent from the surface. Two tools exist
while anything is deferred:

| Tool | Class | Behaviour |
|---|---|---|
| `search_mcp_tools(query, server="")` | read | ranks deferred tools by name, title and description (the `_rank` approach the skill search already uses, `skills/tools.py`), returns up to 8 with one-line descriptions and **activates** them |
| *(activation)* | — | publishes a new `ToolSurface` generation that includes the activated tools; the model sees them in its next request |

Activation is per session and sticky for the rest of the session; at most 24 activated
tools stay visible, the oldest deactivated first. A deferred tool is a normal
`AgenthiccMcpTool`: the gate, approvals (MCP-06), timeouts (MCP-04), result handling and
the `mcp:<server>:<tool>` identity are unchanged, because only its **visibility** was
deferred. A tool the model calls by name before activating it is refused with
`not_activated` and the search hint.

### D4 — Cache stability

The surface changes at three moments only: a catalog hash changes (as today), a server
becomes ready or fails, and an activation. Each bumps the surface generation once; no
request reorders tools. Tools stay name-sorted within the surface so equal sets produce
equal prompts.

### D5 — Subagents

A worker receives the parent's surface at spawn (as today) plus `search_mcp_tools` when
anything is deferred; activations inside a worker affect that worker's surface only.
A test pins that MCP tools are offered to workers (row 8).

## Failure handling

| Case | Result |
|---|---|
| Search matches nothing | `[]` with the server names that have deferred tools |
| Activation would exceed the 24 cap | the oldest activated tool is deactivated and named in the result |
| Budget set below the cost of one server | that server is wholly deferred, nothing else changes |
| Catalog hash changes for an activated tool | it stays activated if its name survives, and the MCP-05 diff notice applies |

## Security

Tool descriptions are server-supplied and enter the model's context. Bounding them
(D2) reduces the room for injected instructions, and search results show a server's own
words only as short descriptions of tools the operator already enabled. Deferral never
exposes a tool the operator disabled (`disabled_tools` and MCP-06 `deny` are applied
before deferral). `search_mcp_tools` cannot activate a tool of a server that is not
`ready` or not trusted (MCP-05).

## Rollout and migration

1. D1 first, alone: it changes no behaviour and produces the numbers the rest needs.
2. D2 with defaults chosen from those numbers, then D3–D5.
3. Under the budget (the common case) behaviour is unchanged, so existing sessions need
   no migration.
4. Update `docs/guides/mcp.md` ("Restrict or troubleshoot tools"), `docs/guides/tools.md`,
   `llms-full.txt`, `CHANGELOG.md`.

## Acceptance criteria

Against the MCP-03 `many_tools` scenario (hundreds of tools, long descriptions).

1. `mcp doctor --json` reports a token figure per server and a total; `/mcp status` shows
   the total.
2. With the budget unset or large, the request's tool list equals today's.
3. With a small budget, the request contains the admitted servers' tools plus
   `search_mcp_tools`, and no deferred tool.
4. `search_mcp_tools("create issue")` returns matching tools and the next request includes
   them; calling a deferred tool before activation returns `not_activated`.
5. An activated tool goes through the same approval and capability gate as a visible tool
   (asserted with the real gate).
6. A description over `max_description_chars` is cut and flagged in `doctor`.
7. Two identical sessions produce byte-identical tool lists (cache stability).
8. A subagent worker is offered the parent's MCP tools and `search_mcp_tools`.

## Verification

Request-capture on the provider transport (the repository's recording transport); a
mutation that sends deferred tools anyway, skips the gate for activated tools, or
reorders tools must fail a test.

## Risks

- A model may not think to search. The `search_mcp_tools` description says what is
  deferred (server names and counts), and `/mcp status` shows the same.
- Placeholders in D2 could be wrong; D1 exists so they are measured, not guessed.
- Every activation bumps the generation and costs one prompt-cache miss. It is bounded
  by the 24-tool cap and happens only after a search.
