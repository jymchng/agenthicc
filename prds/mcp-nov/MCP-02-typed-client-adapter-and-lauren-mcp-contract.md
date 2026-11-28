---
title: "MCP-02: One typed client adapter, and the contract agenthicc needs from lauren-mcp"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P0
related_prds:
  - MCP-00
  - MCP-01
  - MCP-03
  - MCP-04
  - MCP-06
  - prd-172
  - TOOLR-06
---

# MCP-02 — One typed client adapter, and the contract agenthicc needs from lauren-mcp

agenthicc talks to `lauren-mcp` through a bridge that guesses what the library
offers: it probes attributes, retries calls on `TypeError`, and falls back to
other factories. Several of those guesses are wrong for the version the project
locks, and some features agenthicc reports it has can never work with the real
client. This PRD replaces the guessing with one typed adapter over the library's
public API, raises the version floor to what the code needs, and lists the changes
agenthicc needs `lauren-mcp` to make.

## Evidence

agenthicc at `7174c3a`; `lauren-mcp` at `v1.2.1-6-gf255038` (the locked release is
`1.2.1`). "Verified" means read in source at the line shown.

### What agenthicc does today

| # | Behaviour | Where |
|---|---|---|
| 1 | Imports **private** modules: `lauren_mcp._client._stdio.McpCallError`, `lauren_mcp._types`. `McpCallError` is exported publicly. | `tools/mcp.py:67-69`; `lauren_mcp/__init__.py:10,92` |
| 2 | Builds stdio clients with `cwd=`, `env=` and `startup_timeout=`, and on `TypeError` retries **without them**. Those keywords reached the factory in `lauren-mcp` **1.2.0**; the manifest allows `>=1.1.0`. On 1.1.x the retry silently drops the scrubbed environment and working directory, so the child is launched with no `env` argument, which means it inherits the parent's full environment. **Observed 2026-11-28 in throwaway environments:** released `lauren-mcp` 1.1.0 raises `TypeError: McpStdioClient.__init__() got an unexpected keyword argument 'cwd'`; 1.2.1 accepts `cwd`, `env` and `startup_timeout`. | `tools/mcp.py:309-331`; `pyproject.toml:32`; `_factory.py` stdio signature (added in `v1.2.0`, commit `896ef64`) |
| 3 | `transport = "sse"` resolves `getattr(_McpServer, "sse", None) or _McpServer.streamable_http`. The library has no `sse` factory; its legacy SSE factory is `McpServer.http`. **A configured SSE server is connected as Streamable HTTP.** Observed on released 1.2.1 and 1.1.0: `dir(McpServer)` lists `http, stdio, streamable_http, ws` and `hasattr(McpServer, "sse")` is `False`. | `tools/mcp.py:340`; `_client/_factory.py` (stdio, ws, http, streamable_http) |
| 4 | The CLI accepts `{stdio, ws, websocket, streamable, http}`; the runtime accepts `{stdio, streamable_http, sse, ws}` with `http` → Streamable HTTP. In `lauren-mcp`, `http` means **legacy SSE**. The same word names two protocols. | `cli/mcp_config.py:27`; `tools/mcp_server_config.py:23-30` |
| 5 | Calls `call_tool(name, args, tool_call_id=...)` and on `TypeError` retries without it. The library's `call_tool` has no such parameter, so the first attempt always fails and a genuine `TypeError` inside a tool is indistinguishable from it. | `tools/mcp.py:498-506`; `_client/_base_remote.py:228-235` |
| 6 | Reads server `instructions` through `get_instructions` / `server_instructions` / `instructions` attributes. The client keeps `capabilities` and `serverInfo` from `initialize` and **discards `instructions`**, so none of those attributes exists and the provenance-labelled instruction block (`prompt_instructions`) is always empty. | `tools/mcp.py:392-404`; `_client/_base_remote.py:116-150` |
| 7 | Installs only `on_list_changed`. Roots, sampling, elicitation, progress and log handlers are never passed, so the client advertises **no capabilities** and every server request of those kinds is answered `METHOD_NOT_FOUND`. | `tools/mcp.py:332-362`, `:472-496`; `_client/_features.py:157-170,197-240` |
| 8 | `McpToolSchema` keeps `name`, `description` and `input_schema` only, so tool `title`, `annotations` and `outputSchema` never reach the model, the approval UI or the capability gate. | `tools/mcp.py:92-97`, `:370-392` |
| 9 | A call past `tool_timeout_s` is abandoned with `asyncio.wait_for`; the server is never told. (Cancellation is MCP-04.) | `tools/mcp.py:540-545` |

### What `lauren-mcp` does today (the cause behind rows 6–9)

| # | Behaviour | Where in `lauren-mcp` |
|---|---|---|
| L1 | `list_tools`, `list_resources` and `list_prompts` send no `cursor` and never read `nextCursor`. A server that pages returns its first page only, with no error. Upstream records this as "not implemented today". | `_client/_base_remote.py:217-262`; `prds/2026-11-landscape/prd-result-envelope-caching.md:100` |
| L2 | The result parsers build `ToolSchema(name, description, inputSchema)`, `PromptSchema(name, description)` and `ResourceSchema(uri, name, description, mimeType)`. The **types already model** `outputSchema`, `annotations`, `title`, prompt `arguments` and resource `annotations`; the parsers drop them. | `_base_remote.py:217-262` vs `_types.py` (`ToolSchema`, `PromptSchema`, `ResourceSchema`) |
| L3 | `initialize`'s `instructions` is dropped. | `_base_remote.py:130-145` |
| L4 | There is no `resources/templates/list`. | `_client/_protocol.py` (no such method) |
| L5 | `call_tool` takes no progress token, metadata or timeout, and returns an untyped dict. | `_client/_protocol.py:57-79` |
| L6 | `_request` awaits a future with no timeout; task cancellation leaves the pending entry behind and sends nothing. Upstream plans `cancel()`, `notifications/cancelled` and an era-aware client in its draft client-parity PRD (P2). | `_base_remote.py:154-169`; `prds/2026-11-landscape/prd-client-parity.md` |
| L7 | OAuth is client-credentials only, with an in-memory token store and one 401 retry. No authorization-code flow, discovery or registration. | `_client/_oauth.py` |

## Goals

1. agenthicc has **one** adapter module that is the only code importing
   `lauren_mcp`. Everything else sees agenthicc's own typed models.
2. No attribute probing, no `TypeError` retries, no private imports, no fallback
   factories: the adapter targets one pinned minimum version.
3. `sse` means legacy SSE, and the CLI and runtime accept one vocabulary.
4. agenthicc states, as a versioned contract, what it needs from `lauren-mcp`, and
   features that need an upstream change are gated on the negotiated capability,
   not on guesswork.

## Non-goals

- Reimplementing the protocol or a second client.
- Forking `lauren-mcp`. Gaps in the library are requests (section U), not patches
  in agenthicc.
- The current protocol revision's features (MCP-13); this adapter only has to leave room
  for them.

## Design

### A1 — The adapter

A new `agenthicc/tools/mcp_client.py` defines frozen dataclasses and one
`McpClient` protocol, and an implementation over `lauren_mcp.McpServer`:

```python
@dataclass(frozen=True, slots=True)
class McpToolInfo:           # what the catalog, approvals and the model need
    name: str; title: str; description: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object] | None
    annotations: McpToolAnnotations      # read_only/destructive/idempotent/open_world hints, all optional
    meta: Mapping[str, object]

class McpClient(Protocol):
    server_info: McpServerInfo           # name, version, instructions (may be "")
    capabilities: McpCapabilities        # typed: tools/resources/prompts/logging/completions/...
    protocol_version: str
    async def list_tools(self) -> tuple[McpToolInfo, ...]: ...
    async def call_tool(self, name: str, args: Mapping[str, object]) -> McpCallResult: ...
    # resources, prompts, templates: added by MCP-08 behind the same protocol
```

`McpToolBridge` and the manager depend on `McpClient`, never on `lauren_mcp`.
`AgenthiccMcpTool` keeps its `mcp:<server>:<tool>` identity.

### A2 — Pin the floor to what the code uses

Set `lauren-mcp>=1.2.1,<2` (the locked release, which has `cwd`/`env` on the stdio
factory). Delete the stdio `TypeError` fallback, the `tool_call_id` retry, the
`getattr` probes and the three alternative change-callback spellings. A test reads
the installed version and fails if it is below the floor.

### A3 — One transport vocabulary

Define the names once, in `McpServerConfig`, and have the CLI import them:

| Config value | Protocol | `lauren-mcp` factory |
|---|---|---|
| `stdio` | subprocess | `McpServer.stdio` |
| `streamable_http` (aliases `streamable`, `http`) | MCP 2025-03-26+ | `McpServer.streamable_http` |
| `sse` | legacy HTTP+SSE (2024-11-05) — **Deprecated** in the current MCP revision; config and `mcp add` warn | `McpServer.http` |
| `ws` (alias `websocket`) | WebSocket | `McpServer.ws` |

`mcp add` accepts all four names. `http` keeps meaning Streamable HTTP in
agenthicc (documented, since it differs from the library) because existing configs
use it; `mcp doctor` prints the resolved protocol so the difference is visible.

### A4 — Capability-gated features

`McpCapabilities` is parsed from the `initialize` result. Code that needs a
capability (`resources`, `prompts`, `completions`, `logging`, `tools.listChanged`)
checks it and returns a structured `McpCapabilityUnavailable` (PRD-172 §8.3), never
an `AttributeError`.

### U — Requests to `lauren-mcp` (the contract)

Each is small, additive and independently useful. File each as an upstream issue
with the row number; agenthicc features that depend on one are marked in the PRD
that needs it.

| ID | Request | Needed by | Upstream plans it? |
|---|---|---|---|
| U1 | `list_tools/resources/prompts` follow `nextCursor` (`cursor` parameter and an auto-follow default, with a hard page cap) | MCP-06, MCP-08, MCP-11 | **No** (recorded as not implemented) |
| U2 | Fill the fields the types already have: tool `title`/`annotations`/`outputSchema`/`_meta`; prompt `arguments`/`title`; resource `title`/`annotations` | MCP-06, MCP-08 | No |
| U3 | Expose `instructions` from `initialize` (`client.instructions`) | MCP-02 A1, MCP-11 | No |
| U4 | `list_resource_templates()` (`resources/templates/list`) | MCP-08 | Partly (mentioned, not scheduled) |
| U5 | `call_tool(name, args, *, progress_token=None, meta=None)` and a typed `CallToolResult(content, isError, structuredContent, meta)` | MCP-04, MCP-06 | `progressToken` appears in 7 upstream PRDs; the client side is not scheduled |
| U6 | `cancel(request_id)` and cancellation on task cancel (`notifications/cancelled`) | MCP-04 | **Yes** (client-parity PRD, P2) |
| U7 | Authorization-code + PKCE flow, discovery, and a persistent `TokenStorage` hook | MCP-10 | Partly (resource indicators and client-ID metadata PRD) |
| U8 | Public typed error: `McpCallError.data` and `.code` documented as API | MCP-04 | Exported already |
| U9 | An HTTP policy hook on the Streamable HTTP and SSE clients: accept a `follow_redirects` setting and a request/redirect hook (or an `httpx` client or transport), so a host can enforce its network allow-list on every hop | MCP-05 | No (`follow_redirects=True` is hard-coded) |

The protocol-era requests (U10–U14: dual-era client, result cache, `subscriptions/listen`,
multi round-trip requests, tasks) are listed in MCP-13 D1, because they depend on the
current protocol revision and not on this adapter work.

Until U1–U3 land, the adapter reports what it can see and `mcp doctor` says which
fields the installed library does not provide (see MCP-06).

## Failure handling

- A library older than the floor: `McpUnavailable(reason="dependency_incompatible")`
  with the installed and required versions (MCP-01).
- A server answering a capability it did not advertise: `McpCapabilityUnavailable`.
- A paginated catalog while U1 is missing: `mcp doctor` warns that the catalog may
  be truncated whenever a list result carries a `nextCursor` the client dropped
  (visible only once U1 exposes it; until then the warning is documentation).

## Security

The adapter is the single place where wire data becomes agenthicc data, so it is
where size bounds (`_MAX_TOOL_COUNT`, `_MAX_SCHEMA_BYTES`, the 50,000-character
result cut) and the `${ENV}` expansion rules stay enforced. Nothing in this PRD
loosens them.

## Rollout and migration

1. Land MCP-03's real-client fixtures first.
2. Introduce the adapter behind the existing bridge, move the manager onto
   `McpClient`, then delete the probes and fallbacks in the same change.
3. Update `docs/guides/mcp.md` (transport table, the "transport-alias trap"
   section is replaced by A3), `llms-full.txt` and `CHANGELOG.md`.
4. `grep -rn "lauren_mcp" src/` lists only the adapter and `tools/mcp_client.py`'s
   import guard.

## Acceptance criteria

1. `grep -rn "import lauren_mcp\|from lauren_mcp" src/` matches one file.
2. `grep -rn "getattr(.*client\|except TypeError" src/agenthicc/tools/mcp*.py`
   finds none of the removed shims.
3. A configured `transport = "sse"` server connects through `McpServer.http`
   (asserted against a real legacy-SSE test server, MCP-03).
4. `mcp add --transport sse` and `--transport streamable_http` are accepted, and
   the CLI and runtime read the same constant.
5. A stdio server receives exactly the configured `env` and `cwd` (asserted by a
   test server that reports both).
6. `mcp doctor --json` reports `protocol_version`, `capabilities` and which of U1–U5
   the installed `lauren-mcp` provides.
7. A stdio server that exits during startup produces a failure message that ends
   with its last stderr lines, read from the client's public `stderr_tail`. (This
   already works with the real client; the harness test pins it.)

## Verification

Real-client tests from MCP-03 for the transport rows; unit tests for the adapter's
mapping; a mutation check for each removed shim (restore it and a test must fail).

## Risks

- Raising the floor excludes `lauren-mcp` 1.1.x. That version silently loses the
  child environment today (row 2), so excluding it is the safe direction.
- If upstream declines U1–U3, agenthicc ships with documented limits. The adapter
  isolates the workaround to one file.
