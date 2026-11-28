---
title: "MCP-08: MCP resources and prompts, usable by the operator and the model"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P2
related_prds:
  - MCP-00
  - MCP-02
  - MCP-03
  - MCP-05
  - COMM-02
  - prd-172
---

# MCP-08 — MCP resources and prompts, usable by the operator and the model

agenthicc asks every server for its resources and prompts, stores the answers in the
catalog snapshot, and then nothing reads them. Of the three MCP server primitives,
only tools reach a user or a model.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | The manager calls `list_prompts` and `list_resources` at startup and keeps the results in `McpCatalogSnapshot.prompts` / `.resources`, and hashes them into the catalog revision. | `tools/mcp_manager.py:469-484`, `:936-937,959`; `tools/mcp.py:431-465` |
| 2 | **No code reads them.** `grep -rn "\.prompts\b\|\.resources\b\|read_resource\|get_prompt" src/` outside the bridge and manager finds nothing. `/mcp` shows tool counts only. | `commands/builtins.py:699-760`; `tools/mcp_manager.py:799-834` |
| 3 | PRD-172 staged them deliberately: "Add prompts, resources, and resource templates as separate APIs; do not silently inject resources into the tool prompt." That was Phase 3 and was not built. | `prds/prd-172-…md:629-638` |
| 4 | The client library returns prompts without their `arguments` and resources without `title`/`annotations`, has no `resources/templates/list`, and follows no `nextCursor`. (MCP-02 L1, L2, L4.) | `lauren-mcp` `_client/_base_remote.py:238-262` |
| 5 | The client library does implement `read_resource`, `get_prompt`, `complete`, `subscribe_resource`, `unsubscribe_resource` and `on_resource_updated`. | `lauren-mcp` `_client/_protocol.py`, `_features.py` |
| 6 | `@mention` parsing already has a typed-prefix precedent (`@agent:NAME`), and `build_context_prefix` is the async, token-budgeted injector for file, URL and agent mentions. | `mentions/parser.py` (`AGENT_PREFIX`); `mentions/injector.py:529` |
| 7 | The command registry reserves a source namespace for this and nothing uses it: `source_id` is `"builtin" \| "skill:<slug>" \| "plugin:<stem>" \| "mcp:<alias>"`. | `commands/command.py:227` |
| 8 | A server name may not contain `:` or whitespace, so `server:rest` parses unambiguously. | `tools/mcp_server_config.py:validate` |
| 9 | COMM-02 gave the agent a durable, deduplicated route for "something changed" notices (`RUNTIME_NOTICE_SOURCES`). | `subagents/delivery.py` |

## Goals

1. An operator can attach an MCP resource to a message with an `@` mention and run a
   server's prompt as a command.
2. The model can list and read resources through two read-class tools, bounded the
   same way tool results are.
3. Nothing from a server enters the model's context except by a model tool call or an
   explicit operator action, and it is labelled untrusted.
4. Everything is capability-gated: a server without `resources` or `prompts` simply
   shows none.

## Non-goals

- Injecting resources automatically (PRD-172 forbids it).
- Resource subscriptions pushed into the transcript (phase 2, D5).
- Serving agenthicc's own resources (MCP-14).

## Design

### D1 — Adapter surface (MCP-02)

`McpClient` gains, each capability-gated and bounded:

```python
async def list_resources(self) -> tuple[McpResourceInfo, ...]
async def list_resource_templates(self) -> tuple[McpResourceTemplate, ...]   # needs U4
async def read_resource(self, uri: str) -> McpResourceContents                # text | blob (to artifacts)
async def list_prompts(self) -> tuple[McpPromptInfo, ...]                     # with arguments, needs U2
async def get_prompt(self, name: str, arguments: Mapping[str, str]) -> McpPromptResult
async def complete(self, ref: McpCompletionRef, argument: str, value: str) -> tuple[str, ...]
```

Pagination (U1) is followed up to a page cap. Binary contents are written to the
session's `mcp-artifacts/` directory exactly as tool results already are
(`tools/mcp_content.py`), never returned as base64 text. Text is cut at the existing
50,000-character bound with `truncated` and `original_chars`.

### D2 — Two model tools

Registered only while some ready server advertises `resources`:

| Tool | Class | Behaviour |
|---|---|---|
| `list_mcp_resources(server="")` | read | resource and template names, titles, MIME types and URIs, for one server or all, capped at 200 entries with `more: true` |
| `read_mcp_resource(server, uri)` | read | reads one resource; `uri` must be a listed resource or match a listed template, else `not_a_known_resource` |

Results use the existing envelope (`ok`, `content`, bounded), and each content block
is wrapped `<mcp-resource server=… uri=… untrusted="true">`. Capability class is
`READ` plus `NETWORK` for remote servers, so Plan mode treats them like other network
reads. They are unavailable for a server in a state other than `ready`.

### D3 — `@mcp:SERVER:NAME-OR-URI`

A new `MentionKind.MCP_RESOURCE`, parsed like `@agent:`. The picker (`tui/triggers/`)
completes server names, then resource names, then template parameters through
`complete` when the server supports it. `build_context_prefix` resolves it through the
manager, applies the same token budget and ordering as file mentions, and labels the
block as in D2. An unresolved mention reports `unresolved` as files do. A mention of a
server in `untrusted` or `failed` state is reported with that cause (MCP-01).

### D4 — Prompts as commands

Each prompt becomes a slash command `/<server>:<prompt>` with
`source_id = "mcp:<server>"`, an argument hint built from `arguments` (required ones
marked), and completions from `complete`. Running it calls `prompts/get` and **places
the result in the composer for the operator to review and send**; it is never sent
automatically. Message roles map as: `user` text becomes the draft; an `assistant`
message is included as a quoted block; images and embedded resources become
mentions-style attachments (artifacts). The draft's first line records
`from MCP server "<server>", prompt "<prompt>"`. Commands appear and disappear with
`notifications/prompts/list_changed`, and are listed under their source in `/help`.

### D5 — Phase 2: update notices

For a server that advertises `resources.subscribe` and has `subscribe_resources =
true`, the host subscribes to a URI the session has read in the last N turns and
reports `notifications/resources/updated` as a runtime notice through COMM-02's route
(`source: "mcp_resource_updated"`): `MCP resource <uri> from <server> changed since you
read it.` At most one notice per URI per turn, never the new content. The adapter uses
`resources/subscribe` on a legacy-era connection and `subscriptions/listen`
(`resourceSubscriptions`) on a modern one (MCP-13 D5); the notice is the same.

## Failure handling

| Case | Result |
|---|---|
| Server lacks `resources` / `prompts` | no tools, no commands, no mention completion for it |
| `read_mcp_resource` for an unlisted URI | `not_a_known_resource`, listing the nearest template |
| Resource larger than the bound | cut with `truncated`; blobs go to artifacts |
| Prompt argument missing | the command reports the argument name and hint; nothing is sent |
| Server restarts and drops a prompt | its command is removed and the composer draft is left alone |

## Security

Resource contents and prompt text are server-authored and untrusted. They enter
context only through a model tool call or an operator action, are labelled as such, and
never gain the authority of the operator's own message: a prompt result is a **draft**
the operator sees first. `file://` resources are whatever the server returns; the host
does not widen workspace access for them. Template expansion percent-encodes
parameters (RFC 6570 simple expansion only) so a model-chosen value cannot add path
segments or query parameters.

## Rollout and migration

1. Needs MCP-02 (adapter), U1, U2 and U4 upstream; D2 and D3 degrade to names without
   titles and without templates until then.
2. Ship D1 and D2, then D3, then D4; D5 after.
3. Update `docs/guides/mcp.md` (replace "How results and schemas reach the model" with
   a resources and prompts section), `docs/guides/commands.md`, `llms-full.txt`,
   `CHANGELOG.md`; mark PRD-172 Phase 3 item 3 delivered.

## Acceptance criteria

All use MCP-03 scenarios (`annotated` plus a `resources` scenario with a template).

1. `list_mcp_resources` returns the peer's resources and templates; for a peer without
   the capability the tool is absent from the tool list.
2. `read_mcp_resource` returns text content, wraps it as untrusted, and refuses a URI
   that is neither listed nor matched by a template.
3. A 200 KB resource is cut at the bound; a PNG blob is written to `mcp-artifacts/` and
   described by path, never inlined.
4. `@mcp:peer:notes` injects the resource into the message prefix within the mention
   token budget; an unknown name reports `unresolved`.
5. `/peer:summarize topic=x` puts the rendered prompt in the composer with the
   `from MCP server` line and sends nothing; a missing required argument sends nothing.
6. After the peer sends `prompts/list_changed`, the command set updates without
   restarting the session.
7. A template parameter containing `/../x?y=1` is percent-encoded in the request the
   peer receives.

## Verification

Peer frame logs show the exact `resources/read` and `prompts/get` requests; a mutation
that inlines blobs, skips the URI check, or auto-sends the prompt must fail a test.

## Risks

- Two new model tools raise the default tool count; they register only when a ready
  server advertises resources (MCP-11 covers catalog size).
- Mapping prompt roles to a single draft loses structure for multi-message prompts; the
  quoted-block form keeps the content visible to the operator.
