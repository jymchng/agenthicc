---
title: "MCP-06: MCP tool metadata, classification and approval"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P1
related_prds:
  - MCP-00
  - MCP-02
  - MCP-05
  - prd-31
  - prd-76
  - prd-172
  - TOOLR-06
---

# MCP-06 — MCP tool metadata, classification and approval

Every MCP tool is treated as an unknown. Two configuration keys that promise to
change that do nothing, the metadata that could inform the operator is thrown away
before it arrives, and a read-only tool is unusable in Plan mode.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | `AgenthiccMcpTool` is a legacy `Tool` and declares no capabilities (`capabilities` defaults to an empty set). The capability gate classifies a tool with none as `UNDECLARED`: **approval in Safe, blocked in Plan, unrestricted in Yolo**. | `tools/base.py` (`Tool.capabilities`); `tools/capability_gate.py:1-9`; `tui/runtime/mode_manager.py:197-209` |
| 2 | So an MCP tool that only searches a database cannot be used in Plan mode, and in Safe mode every call prompts, whatever the server or tool. **Observed 2026-11-28:** an `AgenthiccMcpTool` registered with `build_registry` appears as `mcp_db_search`, `get_tool_capabilities` returns the empty set, and `classify_tool_capabilities` returns `{UNDECLARED}`. | row 1 |
| 3 | `default_approval_mode` and `tool_approval` are parsed, validated as strings and stored, and **nothing reads them**: no reference outside `mcp_server_config.py`. PRD-172 §9.3 specifies both, and its status is *Implemented*. | `tools/mcp_server_config.py:56-57,139-142`; `grep -rn "default_approval_mode\|tool_approval" src/` finds only that file |
| 4 | `McpToolSchema` keeps `name`, `description` and `input_schema`. The server's `title`, `annotations` and `outputSchema` never reach the model, the approval overlay or the gate. (The library also drops them: MCP-02 L2.) | `tools/mcp.py:92-97` |
| 5 | The approval overlay shows MCP arguments as a generic key/value list; the transcript row reads `server · tool`. Neither shows what the server says about the tool. | `tui/workspace/overlays/approval_body.py:19`; `tui/tool_summary.py:224-227` |
| 6 | `structuredContent` is passed through whole; it is never validated against `outputSchema`. | `tools/mcp_content.py:6,111` |
| 7 | The specification says clients **MUST** treat tool annotations as untrusted unless they come from trusted servers, **SHOULD** validate structured results against `outputSchema`, and **SHOULD** show tool inputs before a call (MCP specification 2025-06-18, Tools, read 2026-11-28). PRD-172 §9.3 already decided: annotations "may reduce friction but never grant permission". | spec; `prds/prd-172-…md:547-558` |
| 8 | A new tool name from a server is `mcp:<server>:<tool>`, then rewritten to a provider-safe name. Two tools whose names collide after rewriting fail the whole publish. | `tools/mcp.py:36-50`; `tools/mcp_manager.py:971-996` |

## Goals

1. An operator can say, per server and per tool, what a tool is allowed to do, and the
   gate honours it.
2. A read-only MCP tool is usable in Plan mode **only** when the operator has said
   it is read-only.
3. The approval prompt shows what the server claims, marked as unverified.
4. `default_approval_mode` and `tool_approval` either work or are removed.
5. A structured result that breaks its server's own `outputSchema` is reported, not
   passed on silently.

## Non-goals

- Deciding that a server is trustworthy (MCP-05).
- Inferring capabilities from tool names or descriptions.
- Changing how Safe, Plan and Yolo treat the capabilities themselves.

## Design

### D1 — Capabilities are declared by the operator

Per server, in `[[tools.mcp_servers]]`:

```toml
[[tools.mcp_servers]]
name = "db"
trust_annotations = false          # default
default_capabilities = ["execute"] # for any tool not listed; omit for UNDECLARED
[tools.mcp_servers.tool_capabilities]
search_rows = ["read", "network"]
delete_row  = ["write", "network"]
```

`AgenthiccMcpTool.capabilities` is set per instance from the first match: the
tool's entry, then `default_capabilities`, then (D2), else empty (`UNDECLARED`,
today's behaviour). Values are the existing `ToolCapability` names, validated at
load; an unknown name is a configuration error, listed by `mcp doctor`.

### D2 — Annotations: advisory, and opt-in

`trust_annotations = true` is the operator saying "I trust this server's hints".
Only then are they used, and only to add information the operator did not
declare:

| Server hint | With `trust_annotations = true` |
|---|---|
| `readOnlyHint: true` | capabilities `{READ}` plus `NETWORK` when `openWorldHint` is not `false` |
| `destructiveHint: true` | capabilities `{WRITE}`; the approval prompt is marked destructive |
| none of the above present | `UNDECLARED` (unchanged) |

Plan mode blocks `NETWORK` (`mode_manager.py:197-209`), so a read-only hint opens a
tool to Plan only when the server also says it is closed-world
(`openWorldHint: false`); otherwise the tool is `{READ, NETWORK}` and Plan keeps it
closed, as it does for any network tool. An operator who wants the tool in Plan
declares `tool_capabilities = ["read"]` themselves.

Without the flag, hints appear in the prompt (D3) and in `mcp doctor`, but change
nothing. A hint can only ever **narrow** friction for a server the operator named; it
cannot grant what the operator's own `tool_capabilities` forbid, and an explicit
`tool_capabilities` entry always wins over a hint.

### D3 — Approval shows the claim and the inputs

The approval overlay for an MCP call shows: server and tool, the tool's `title`, the
arguments (as now), and a line `server says: read-only · open-world (unverified)`
built from the annotations (or `no metadata`). In Safe mode, "always allow this tool"
records `(server, tool, catalog hash)` so a changed tool asks again (MCP-05 D7).

### D4 — `default_approval_mode` and `tool_approval` get a meaning, or go

| Key | Values | Effect |
|---|---|---|
| `default_approval_mode` | `prompt` (default), `allow`, `deny` | the mode's approval requirement, per server: `allow` skips the prompt in Safe; `deny` hides every tool of the server |
| `tool_approval = { name = mode }` | the same | per tool, overriding the server default |

`allow` never overrides Plan mode's block and never applies to an `UNDECLARED` tool
in a mode that blocks it; it replaces *asking*, not *blocking*. If maintainers prefer
not to carry two ways to say this, delete both keys and keep D1 (a decision for the
reviewer, recorded here). The current state, where they are accepted and ignored, is
the one outcome this PRD rules out.

### D5 — Titles and names

`title` is shown in the transcript row and the approval prompt (`db · Search rows`),
and never replaces the canonical identity `mcp:<server>:<tool>`. A name collision
after provider-safe rewriting withholds only the colliding tool, with a named reason
in `mcp doctor`, instead of failing the publish for the whole server.

### D6 — Output validation

When a tool declares `outputSchema` and returns `structuredContent`, the adapter
validates it with `jsonschema` (already a base dependency). A mismatch returns the
result flagged `schema_violation: true` with the first error path, and the model is
told the result did not match its schema. The raw content is still returned (bounded)
so a slightly wrong server stays useful. Schemas the validator cannot compile are
skipped with a `doctor` warning. In the current protocol revision `outputSchema` may use
any JSON Schema 2020-12 keyword and `structuredContent` may be any JSON value (not only an
object), so validation is by the declared schema and not by an assumed object shape.

## Failure handling

| Case | Result |
|---|---|
| Unknown capability name in config | server `FAILED: configuration`, the key named |
| `deny` on a server | its tools are not offered; `/mcp status` says why |
| Annotations missing | `UNDECLARED`, unchanged |
| Output schema invalid or non-compilable | result passed through, warning recorded |
| Tool changes after "always allow" | asks again (hash differs) |

## Security

Annotations are server-supplied and untrusted (specification, MUST). They are never
used without the operator's per-server opt-in, they can only reduce prompts for a
server the operator chose, and Plan mode stays closed to anything the operator has
not declared read-only. The prompt labels the claim as unverified so a user does not
read a server's words as the host's. `tool_capabilities` is user configuration and
is trusted only from trusted config layers (MCP-05 D1): a project file cannot declare
its own server's tool `read`.

## Rollout and migration

1. Needs MCP-02 (annotations in the adapter, U2 for the library) and MCP-05 (so a
   project cannot self-declare capabilities).
2. D1 and D3 first, then D4, D2, D5, D6.
3. Existing configs behave as today (all `UNDECLARED`). The two inert keys now either
   work or fail validation if the reviewer chooses removal; a CHANGELOG entry says
   which.
4. Update `docs/guides/mcp.md` ("Restrict or troubleshoot tools"), the `/mcp` help,
   `llms-full.txt`, `CHANGELOG.md`; mark PRD-172 §9.3 as delivered by this PRD.

## Acceptance criteria

1. With `tool_capabilities = { search = ["read"] }`, the `search` tool runs in Plan
   mode and `delete` (unlisted) is blocked, asserted through the real gate.
2. A peer tool advertising `readOnlyHint: true` stays `UNDECLARED` (blocked in Plan)
   until `trust_annotations = true` is set for that server.
3. `default_approval_mode = "deny"` removes the server's tools from the registry;
   `tool_approval = { search = "allow" }` skips the Safe prompt for `search` only and
   does not unblock it in Plan.
4. The approval overlay for a peer tool shows the title and the unverified claim line.
5. A peer result violating its `outputSchema` returns `schema_violation: true` and the
   path of the first error.
6. A project-layer `tool_capabilities` entry has no effect until the server is
   trusted (MCP-05).
7. `grep -rn "default_approval_mode\|tool_approval" src/` finds a reader outside the
   config file (or finds nothing, if the keys were removed).

## Verification

Gate tests with the real `ToolCapabilityGate` and the `annotated` peer scenario
(MCP-03); mutation checks: hints granting without the flag, `allow` overriding Plan,
output validation skipped, project layer honoured.

## Risks

- Operators may over-grant `read`. The prompt and `mcp doctor` print the effective
  capabilities per tool so the choice is visible.
- Annotation semantics are the server's. The opt-in puts the decision where the
  specification puts it.
