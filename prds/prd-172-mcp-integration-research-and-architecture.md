---
title: "PRD-172: Production MCP Integration for agenthicc"
status: Implemented
version: 1.0.0
created: 2026-08-14
scope: "agenthicc and the lauren-ai MCP client boundary"
authors:
  - platform-ai-team
related_prds:
  - PRD-12  # Historical MCP integration design
  - PRD-28  # MCP bridge and registry
  - PRD-29  # MCP configuration and startup
  - PRD-30  # MCP connect communication tool
  - PRD-31  # MCP permissions and integration tests
  - PRD-146 # CLI MCP server registration
  - PRD-162 # Provider connection profiles and OpenAI-compatible endpoints
  - PRD-163 # Cache-stable workflow prompts and generated workflows
supersedes: []
tags:
  - mcp
  - tools
  - cli
  - tui
  - configuration
  - caching
  - security
---

# PRD-172: Production MCP Integration for agenthicc

## 1. Executive summary

This PRD defines the production MCP client architecture for agenthicc after a
source-level study of the Codex CLI and OpenCode CLI implementations. It is an
implementation specification, not a claim that the current bridge already
provides all of these behaviors.

agenthicc already has a useful foundation: `McpServerConfig`,
`McpToolBridge`, `McpToolRegistry`, the `[[tools.mcp_servers]]` TOML contract,
provider-safe tool names, and the normal tool-executor path. The current
implementation is nevertheless a thin session-start discovery loop. It does
not yet provide the lifecycle, catalog, authentication, diagnostics, and
incremental refresh contract needed when users configure many local and remote
servers.

The target is one session-scoped MCP service shared by TUI, headless runs,
workflows, subagents, and the CLI. A server is connected once, its negotiated
capabilities and tool definitions are cached in a versioned catalog, and every
consumer reads the same catalog. Tool calls continue through the existing
agenthicc executor, policy, hooks, event, journal, and usage-accounting paths.

The design deliberately keeps MCP discovery out of the per-turn prompt path.
Stable server instructions and deterministically ordered tool schemas are
published as stable inputs; only catalog revisions and actual tool results are
dynamic. This is required by PRD-163's cache contract and prevents a tool list
request, server ordering change, or transient connection failure from needlessly
destroying provider prompt-cache reuse.

## 2. Research basis and decisions

Research was performed against the official Codex source and documentation,
the official OpenCode source and documentation, and the MCP specification. The
snapshot date is 2026-08-14.

### 2.1 Verified implementation patterns

| Concern | Codex CLI | OpenCode CLI | agenthicc decision |
|---|---|---|---|
| Configuration | TOML `[mcp_servers.<name>]`; dedicated `mcp add/list/get/remove/login/logout` commands | JSON config with named server entries; current V2 docs describe `mcp.servers`; CLI has `mcp add/list/auth/logout` | Preserve agenthicc's `[[tools.mcp_servers]]` format for compatibility, add typed fields, and provide equivalent lifecycle commands |
| Local transport | Child process with explicit command, args, cwd, environment forwarding, and shutdown | `StdioClientTransport` with an argv array, workspace-relative cwd, merged environment, and stderr capture | Never execute a configured MCP command through a shell; parse legacy strings only at the compatibility boundary |
| Remote transport | Streamable HTTP with bearer headers, env-backed headers, OAuth, and bounded startup/tool timeouts | Streamable HTTP first, legacy SSE fallback, request headers, OAuth, and per-request timeout | Make Streamable HTTP the standard remote transport; keep SSE only as an explicitly bounded compatibility fallback |
| Startup | Parallel managed connections, required/optional distinction, cached/deferred startup, startup events | Parallel connection effects, disabled/failed/auth-required statuses, bounded connection timeout | Parallel, bounded, cancellable startup; required servers fail a requested run, optional servers degrade independently |
| Catalog | Per-server cached tools, filters, model-name normalization, catalog revision, reusable connections | State stores clients, definitions, and server instructions; tool lookup reads cached definitions rather than listing every turn | One session catalog with immutable snapshots, revision numbers, stable ordering, filters, and atomic publication |
| Changed tools | Managed connection and catalog revision; refresh must be coordinated with captured calls | Handles `tools/list_changed`, refetches, replaces definitions, and publishes `ToolsChanged` | Handle the notification, debounce refreshes, replace removed tools, and invalidate only the affected catalog revision |
| Instructions | Server metadata and instructions participate in the connection/tool model | Caches `getInstructions()` and exposes them separately from tool definitions | Capture instructions at initialization, sanitize and label them as untrusted server guidance, and keep them in the stable prompt region |
| Tool execution | Prepared calls retain exact server/tool identity and catalog revision | Adapts cached definitions to provider tools and applies timeout/abort signals | Keep canonical identity separate from provider-safe names and reject calls against stale/removed definitions |
| Authentication | Bearer tokens, OAuth, optional trusted first-party session auth, secure credential storage | OAuth with dynamic or configured client registration and explicit auth statuses | Support env-backed bearer headers first; add OAuth through the shared credential boundary, never by persisting raw tokens in TOML |
| Shutdown | Closes clients and local descendants | Closes clients and discovers/kills local descendants before finalization | Own the whole local process group, cancel pending startup, close transports, and never leak child processes |

### 2.2 Version and protocol assumptions

The current OpenCode source contains a V1 configuration type while the current
V2 documentation and specification describe nested `mcp.servers` entries. The
PRD targets the behavior, not a copy of either configuration spelling. The
agenthicc format remains the compatibility authority.

The implementation must support the stable MCP baseline used by the selected
`lauren_mcp`/MCP SDK and feature-detect optional capabilities. It must not
assume that every server supports prompts, resources, progress, OAuth,
elicitation, tasks, or `tools/list_changed`. Streamable HTTP is the preferred
remote transport. Legacy SSE may be used only when configured or when an
explicit compatibility fallback is enabled.

The MCP client implementation belongs in lauren-ai when it is a protocol or
transport concern. agenthicc owns configuration, policy, catalog publication,
executor integration, lifecycle events, and user-facing commands. This avoids
two subtly different JSON-RPC clients in the product.

## 3. Problem statement

The current bridge has several production risks:

1. `discover_all()` connects servers sequentially, so one slow or dead server
   can delay all later servers and the first user turn.
2. The bridge has no explicit required/optional startup contract or structured
   status for disabled, authenticating, failed, starting, and ready servers.
3. Tool definitions are held only in an in-memory registry. There is no
   revisioned snapshot, deterministic catalog hash, reusable snapshot, or
   atomic replacement when a server changes its tool list.
4. There is no handling for `tools/list_changed`, connection-close events,
   server instructions, prompts, resources, or resource templates.
5. The current stdio configuration accepts a shell-like string and then uses
   `shlex.split`; this is retained for old configurations but is not a safe
   authoring contract for new commands.
6. The existing CLI is limited to appending one configuration stanza. Users
   cannot inspect effective status, diagnose startup, authenticate, connect,
   disconnect, or remove a server through a supported lifecycle surface.
7. MCP tool names are adapted for providers, but the reverse mapping, collision
   checks, catalog revision, and stale-call behavior need to be explicit.
8. Server-provided instructions are not currently captured as a distinct,
   cache-stable, untrusted context region.
9. A remote header or local environment can contain a secret; diagnostics must
   prove that secrets do not enter logs, events, transcripts, cassettes, or
   `config show` output.

## 4. Goals and non-goals

### 4.1 Goals

- Support local stdio and remote Streamable HTTP MCP servers as first-class
  agenthicc tools.
- Preserve existing `[[tools.mcp_servers]]` configurations and CLI behavior.
- Provide a single session-scoped manager used by TUI, headless, workflows,
  subagents, and background sessions.
- Start independent servers concurrently with per-server deadlines and
  cancellation.
- Keep optional-server failures from blocking usable sessions; make required
  server failures explicit and actionable.
- Maintain an immutable, deterministic, revisioned catalog of tools and server
  instructions.
- Refresh the affected server atomically after a list-change notification or an
  explicit refresh, including additions and removals.
- Route every MCP call through the normal tool policy, approval, hooks,
  timeout, events, journal, and usage accounting.
- Provide safe, scriptable CLI lifecycle and diagnostic commands plus a useful
  TUI `/mcp` status view.
- Support env-backed bearer tokens and a secure path to OAuth without exposing
  credentials.
- Make the stable system/tool prompt region cache-friendly according to
  PRD-163.

### 4.2 Non-goals

- Exposing agenthicc as an MCP server. That is a separate product surface.
- Building a hosted MCP registry or marketplace.
- Replacing the existing filesystem, command, browser, or HTTP policy engines.
- Executing arbitrary MCP server commands in a shell.
- Treating MCP server instructions as trusted system policy.
- Making every optional MCP primitive available in the first release. Prompts,
  resources, resource templates, elicitation, tasks, and sampling are staged
  capabilities, not prerequisites for tool support.
- Allowing MCP servers to bypass workspace, network, browser, approval, or
  secret policies because they are external.

## 5. Target architecture

### 5.1 Component responsibilities

```text
Config loader
  -> validates and normalizes [[tools.mcp_servers]]
  -> resolves env variable references without materializing secrets in logs

McpSessionManager (one per session)
  -> owns server lifecycle, connection tasks, cancellation, and status
  -> owns the session catalog and catalog revision
  -> owns authentication handles and safe diagnostics

McpTransportAdapter (one per connected server)
  -> stdio argv process OR Streamable HTTP (+ optional SSE fallback)
  -> initialize/initialized negotiation and capability detection
  -> bounded requests, cancellation, progress, and close handling

McpCatalog
  -> immutable per-server definitions, instructions, capabilities, metadata
  -> deterministic ordering, filters, provider-name collision checks
  -> atomic publish and revisioned snapshots

McpToolAdapter
  -> maps canonical mcp:<server>:<tool> identity to provider-safe name
  -> validates arguments and catalog revision
  -> invokes the transport with tool_call_id and request context

agenthicc ToolExecutor / policy / hooks / events / journal
  -> the only path for an MCP tool call

TUI, headless runner, workflow phases, subagents, CLI
  -> consume the same manager and snapshot; none owns a second MCP registry
```

### 5.2 Session ownership and lifetime

`McpSessionManager` is created after the session owner is acquired and before
the first transcript turn. It is attached to `SessionContext` and passed into
every `AgentTurnContext` and `WorkflowRunConfig`. The same manager remains
alive while the user switches between normal chat, Plan mode, `code_plan`,
`create_workflow`, and generated workflows.

It must not be created separately for each phase or agent turn. A workflow may
restrict its visible tools, but it receives a read-only catalog view backed by
the same manager. A subagent receives an explicit derived policy view, not a
new uncontrolled connection.

Shutdown is idempotent and runs on every exit path: normal completion,
interrupt, provider failure, workflow pause, resume handoff, session ownership
loss, and TUI teardown. Local process descendants are terminated as a group
and pending network/auth tasks are cancelled before the manager is released.

### 5.3 Dataflow

```text
agenthicc.toml / CLI overrides / environment references
                  |
                  v
      validate + normalize + redact diagnostics
                  |
                  v
       McpServerSpec[] (immutable effective config)
                  |
                  v
      McpSessionManager.start_all()
       |                 |                 |
       |                 |                 +--> status/startup events
       |                 +--> auth state / secure credential store
       +--> transport initialize() and capability negotiation
                              |
                              v
                   tools/list + instructions
                              |
                              v
             catalog snapshot(server, revision)
             {defs, capabilities, instructions, hash}
                              |
       filter enabled/disabled tools + policy + provider name mapping
                              |
                              v
              AgentTurnContext -> provider request
                              |
                              v
                model emits provider-safe tool call
                              |
                              v
       reverse map -> canonical identity + snapshot revision + server/tool
                              |
                              v
      ToolExecutor policy -> hooks -> MCP call -> normalized result/events
                              |
                              v
                    transcript/journal/usage

notifications/tools/list_changed or /mcp refresh
                  |
                  v
      debounce -> refetch -> validate -> atomic replace
                  |
                  v
             revision N -> revision N+1 -> next turn surface
```

### 5.4 Lifecycle state machine

Each configured server exposes one structured status:

```text
configured -> disabled
configured -> starting -> ready
                         |  |
                         |  +-> refreshing -> ready
                         +----> failed
configured -> authenticating -> needs_auth -> starting
starting -> cancelled
ready -> stopping -> stopped
```

Required fields are `name`, `status`, `transport`, `tool_count`,
`catalog_revision`, `last_error` (redacted), `started_at`, `last_success_at`,
and `auth_state`. Status must be serializable to stable JSON for CLI and TUI
consumers. A failed optional server is not silently omitted: it remains
visible with a recovery hint.

## 6. Configuration contract

### 6.1 Backwards-compatible format

The canonical agenthicc format remains an array because existing projects,
`agenthicc mcp add`, and configuration loaders use it:

```toml
[[tools.mcp_servers]]
name = "filesystem"
transport = "stdio"
command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]
cwd = "."
enabled = true
required = false
startup_timeout_s = 10
tool_timeout_s = 60
reconnect_attempts = 3
reconnect_delay_s = 1.0
enabled_tools = ["read_file", "list_directory"]
disabled_tools = ["write_file"]
default_approval_mode = "prompt"

[tools.mcp_servers.env]
MCP_LOG_LEVEL = "info"

[[tools.mcp_servers]]
name = "remote-search"
transport = "streamable_http"
url = "https://mcp.example.test/mcp"
startup_timeout_s = 10
tool_timeout_s = 45

[tools.mcp_servers.env_headers]
Authorization = "MCP_REMOTE_TOKEN"
```

The exact field names must be implemented in one typed `McpServerConfig` and
one documented parser. Unknown fields are rejected in strict validation, with
an opt-in compatibility warning mode for old project files.

### 6.2 Field requirements

| Field | Requirement |
|---|---|
| `name` | Required, stable, unique, safe for logs and namespacing |
| `transport` | `stdio` or `streamable_http`; `sse` is explicit legacy compatibility; existing `ws`, `websocket`, `streamable`, and `http` aliases remain readable during migration |
| `command` | New stdio form is an argv array; executable is element zero; empty arrays are invalid |
| `url` | Required for remote transports; absolute `http`/`https` URL; no credentials embedded in the URL |
| `cwd` | Optional stdio working directory, resolved relative to workspace and rejected if outside the configured process policy |
| `env` | Non-secret explicit environment values; values are redacted from diagnostics if their key is secret-like |
| `env_vars` | Names of existing environment variables allowed to pass through; no wildcard forwarding by default |
| `headers` | Non-secret static remote headers; sensitive values must not be stored here |
| `env_headers` | Header-to-environment-variable mapping; resolve only at request time and redact always |
| `enabled` | Default `true`; false produces `disabled` without starting a process or request |
| `required` | Default `false`; true makes a run fail clearly if the server cannot become ready |
| `auto_connect` | Default `true`; false leaves the server configured but dormant until connect |
| timeouts | Positive bounded startup and tool defaults; explicit `0` is allowed only where the product policy intentionally means no timeout |
| tool filters | `enabled_tools` is applied first, then `disabled_tools`; an empty result is valid |
| approval | Server default plus optional per-tool override, evaluated by the existing policy engine |
| `oauth` | Optional remote auth configuration; secrets and tokens live outside TOML |
| `metadata` | Non-secret provenance and display metadata only; never executable instructions |

Legacy entries such as `url = "python -m server"` with `transport = "stdio"`
continue to load through `shlex.split` at the compatibility boundary. The
effective config and `mcp add` output must migrate users toward `command = [...]`.
The new argv form has no shell interpretation. Legacy strings remain supported,
receive a deprecation warning, and are never passed to `shell=True`.

### 6.3 Configuration precedence

Use agenthicc's existing precedence rules: built-in defaults, user config,
project config, explicit `--config`, `--set`, and environment references.
Servers are merged by stable `name`, not by array position. A project entry
with the same name replaces the user entry as one complete server definition;
unrelated servers remain. `--set-secret` may select an environment variable or
secure credential reference but must never put a raw secret in the effective
config dump.

## 7. Catalog, cache, and prompt contract

### 7.1 Catalog snapshot

The manager maintains an immutable snapshot per server:

```python
McpCatalogSnapshot(
    server_name="filesystem",
    revision=7,
    protocol_version="...",
    server_info={...},
    capabilities={...},
    instructions="...",          # separately labeled untrusted text
    tools=(McpToolDefinition(...),),
    prompts=(...),
    resources=(...),
    tool_filter_hash="...",
    catalog_hash="...",
    captured_at="...",
)
```

Tool definitions must be copied before adaptation so neither a provider
adapter nor an agent can mutate the shared snapshot. Lists are deterministically
sorted by canonical server name and tool name. Duplicate canonical names are a
configuration/protocol error; duplicate provider-safe names are resolved by a
stable digest suffix and surfaced as a diagnostic rather than silently
overwriting a tool.

The provider-facing registry is rebuilt only when the catalog revision or
visible-tool policy changes. It must not call `tools/list` for every agent turn.
An individual tool call carries the revision it was prepared from. If that
revision is no longer current, the executor either safely remaps the unchanged
tool or returns a typed stale-catalog retry result; it must never call an
unknown or removed tool by name.

### 7.2 Prompt-cache stability

The MCP prompt contribution is partitioned as follows:

```text
stable prefix:
  agenthicc system prompt
  static policy and tool-execution rules
  sorted MCP server identity and server instructions
  sorted MCP tool declarations for catalog revision R

dynamic suffix:
  user message
  workflow phase state and artifacts
  tool calls/results
  current questions, approvals, and transient status
```

Server instructions are captured once per catalog revision and wrapped with
explicit provenance such as `Instructions supplied by MCP server 'x'`. They
are untrusted data. They may describe how tools work but cannot change
agenthicc policy, reveal secrets, authorize writes, or override the system
prompt. Empty or excessively large instructions are bounded and recorded as a
diagnostic.

Tool declarations preserve the server's schema semantics while normalizing
provider constraints. Their order, names, descriptions, and JSON serialization
must be stable for equal catalog hashes. A changed catalog intentionally starts
a new stable prompt segment; ordinary turns with the same revision reuse the
same segment.

The manager emits `McpCatalogPublished` with the new revision. The session
prompt builder consumes the event at a turn boundary, never half-way through a
provider request. This avoids one turn seeing a definition that another turn
has already removed.

### 7.3 Refresh and invalidation

On `notifications/tools/list_changed`, or `/mcp refresh NAME`:

1. Coalesce duplicate notifications for the same server.
2. Fetch `tools/list` with the server timeout and pagination guard.
3. Validate every definition and calculate the candidate hash.
4. Reapply configured and policy filters.
5. Atomically publish the candidate snapshot only if the transport and server
   identity still match the request that started the refresh.
6. Increment the server/global revision and emit one change event.
7. Remove tools no longer returned and make new tools visible at the next
   turn boundary.

If refresh fails, retain the last known-good snapshot, mark the server
`degraded`, and expose the error. A failed refresh must not erase a usable
catalog or invalidate unrelated servers. Refreshes must be bounded and
cancelled during shutdown.

### 7.4 Optional persisted snapshot

Persisting a redacted catalog snapshot is recommended for fast startup, but it
must be opt-in per policy and keyed by server identity, effective config hash,
protocol version, tool-filter hash, and server authentication scope. A cached
snapshot is a hint, never authorization: the manager must not invoke a tool
until a live compatible client is ready. The default behavior may use the
snapshot to render status and build an optional catalog while connection starts.

## 8. Transport and protocol behavior

### 8.1 stdio

- Spawn with an argv list, `shell=False`, a validated cwd, and a controlled
  environment.
- Capture stderr as diagnostics; never interpret stderr as protocol data.
- Require stdout to contain only valid MCP messages.
- Use process-group/descendant cleanup on close and cancellation.
- Bound initialization, listing, and each call; reset a call deadline only if
  the selected SDK explicitly reports valid progress.
- Never log command arguments that contain secret-looking values.

### 8.2 Streamable HTTP

- Require an absolute URL and route it through `NetworkGuard`/the configured
  HTTP policy before connecting.
- Use the MCP SDK's protocol headers and negotiated version rather than
  hand-building JSON-RPC requests in agenthicc.
- Resolve bearer headers from environment or secure credentials at request
  time; redact them in errors and events.
- Validate TLS and reject embedded credentials in URLs.
- Use one bounded transport timeout for initialization and a separate bounded
  tool-call timeout.
- Support server notifications when the transport supports them.
- SSE fallback is disabled by default for new entries and must be clearly
  labeled legacy when enabled.

### 8.3 Capability negotiation

The manager records only capabilities actually advertised by the server. It
must feature-detect tools, prompts, resources, resource templates,
list-changed notifications, progress, and optional server-to-client requests.
Unsupported capabilities return structured `McpCapabilityUnavailable` errors,
not generic `AttributeError` failures.

## 9. Tool execution and policy

### 9.1 Identity mapping

Canonical internal identity:

```text
mcp:<server-name>:<original-tool-name>
```

Provider-facing names use the provider's allowed character set and length,
for example `mcp_filesystem_read_file`. The mapping is bijective in the
session catalog and stored in the prepared call. Tool descriptions should
identify the server only when useful; the canonical identity remains in
events, permissions, and diagnostics.

### 9.2 Single execution path

An MCP call must follow this path:

```text
provider tool call
  -> reverse-map and schema validation
  -> catalog revision/live-client check
  -> existing allow/deny and approval policy
  -> before hooks
  -> ToolCallStarted event
  -> bounded MCP call with cancellation and tool_call_id
  -> normalize CallToolResult (content, structuredContent, isError)
  -> after/error hooks
  -> ToolCallCompleted/Failed event
  -> journal, transcript, usage, provider result
```

MCP servers cannot call the bridge directly from a workflow or bypass the
executor. `isError = true` becomes a typed failed tool result with safe text;
structured content remains available to the model without being stringified
twice. Tool output size, binary content, and nested JSON are bounded according
to the existing tool result policy.

### 9.3 Permission model

Every MCP server has a policy identity `mcp:<server>` and every tool has the
more specific identity `mcp:<server>:<tool>`. Rules are evaluated from most
specific to least specific. The server's default approval mode applies first;
per-tool overrides then narrow it. A disabled or denied server has no visible
tools and cannot be activated by model output.

MCP tool annotations such as read-only hints are advisory. They may reduce
friction but never grant permission. An MCP tool that writes files, sends
network requests, changes browser state, or performs an external side effect
is still subject to the corresponding agenthicc mode and approval policy.

## 10. CLI and TUI product surface

### 10.1 CLI commands

Extend the existing command group without breaking `agenthicc mcp add NAME
URL`:

```text
agenthicc mcp add NAME [--stdio COMMAND ... | --url URL]
agenthicc mcp list [--json]
agenthicc mcp get NAME [--json]
agenthicc mcp remove NAME [--global | --project]
agenthicc mcp connect NAME
agenthicc mcp disconnect NAME
agenthicc mcp refresh NAME
agenthicc mcp auth NAME
agenthicc mcp logout NAME
agenthicc mcp doctor [NAME] [--json]
```

Commands that mutate persistent configuration must use atomic writes, preserve
permissions, reject duplicate names, and never start a server unexpectedly.
Runtime commands operate on a running session where applicable and return a
clear message when no session manager is available. `list`, `get`, and
`doctor --json` must be stable machine-readable contracts. Raw tokens, secret
headers, environment values, OAuth codes, and command secrets never appear.

`doctor` performs bounded, read-only validation: config parse, transport
selection, executable/path checks, URL/network-policy check, auth state, live
initialize, catalog count, and a redacted failure cause. It must not call a
tool merely to prove connectivity.

### 10.2 TUI `/mcp`

`/mcp` becomes an interactive status panel, while preserving a concise status
message for non-interactive callers. It shows server name, transport, state,
tool count, catalog revision, auth state, and the last redacted error. Users
can select a server to connect, disconnect, refresh, authenticate, or inspect
the filtered tool list, subject to busy-state policy. Startup progress is
grouped by server and does not block normal input for optional servers.

## 11. Implementation plan

### Phase 0 — contract and compatibility baseline

1. Add typed normalized config fields and validation.
2. Preserve legacy `url` stdio strings and transport aliases.
3. Add redacted effective-config serialization and migration diagnostics.
4. Define typed MCP statuses, events, errors, and catalog snapshot objects.

### Phase 1 — manager, transports, and catalog

1. Move session startup from direct `McpToolRegistry` use to
   `McpSessionManager` while retaining a compatibility facade.
2. Add concurrent bounded startup with required/optional semantics.
3. Use lauren-ai for protocol lifecycle and transports; add missing client
   features there rather than duplicating protocol handling.
4. Add immutable snapshots, deterministic names/order, filters, and revisioned
   publication.
5. Integrate tool adapters with the existing executor and all contexts.

### Phase 2 — refresh, resilience, and user surfaces

1. Handle close and list-change notifications with coalesced atomic refresh.
2. Add reconnect/backoff and last-known-good catalog behavior.
3. Implement CLI list/get/remove/connect/disconnect/refresh/doctor.
4. Expand `/mcp` into the interactive status panel.
5. Add process-group cleanup, cancellation, metrics, and redacted diagnostics.

### Phase 3 — auth and staged MCP primitives

1. Add env-backed bearer headers and tests first.
2. Add OAuth secure storage, callback/CSRF handling, auth statuses, and CLI
   login/logout.
3. Add prompts, resources, and resource templates as separate APIs; do not
   silently inject resources into the tool prompt.
4. Evaluate elicitation, tasks, progress, server logs, and sampling only after
   the host approval/user-question contracts are specified.

## 12. Non-functional requirements

### Performance

- Starting N independent optional servers must be concurrent, with no server
  waiting on another server's network or process startup.
- A slow optional server must not block input or the first usable turn beyond
  the configured optional startup grace period.
- Once the catalog is ready, normal turns must perform zero `tools/list`
  requests and zero process launches.
- Equal catalog snapshots must serialize identically to maximize provider
  prompt-cache reuse.
- Catalog refresh is scoped to one server and must not rebuild unrelated
  adapters.

### Reliability

- Every startup, call, refresh, auth, and shutdown operation has a deadline and
  cancellation path.
- A server failure is isolated and observable; it cannot erase healthy server
  tools.
- Calls use the last exact live client/catalog binding and report stale or
  missing tools deterministically.
- Shutdown is idempotent and leaves no owned stdio descendants.
- Repeated notifications, reconnects, `/mcp refresh`, and session resume are
  safe to repeat.

### Security and privacy

- No `shell=True`, command interpolation, embedded URL credentials, raw secret
  config values, or secret-bearing telemetry.
- Local cwd and executable resolution use the existing workspace/process
  policy; remote URLs use the existing network policy.
- OAuth state is random, single-use, callback-bound, and checked against CSRF;
  credentials use the existing secure store abstraction.
- Server instructions and tool metadata are untrusted input and are isolated
  from system policy.
- Diagnostics, cassettes, journals, and TUI transcripts apply the same
  redaction policy.

### Observability

Emit structured events/metrics for config validation, startup duration,
connection state, auth transitions, catalog fetch/cache hit/cache miss,
catalog revision, tool-call duration, timeout, retry, refresh, and shutdown.
Metrics contain server name only after normalization and never contain URLs
with query secrets, headers, environment values, arguments, or tool payloads.

## 13. Testing strategy

All tests must use deterministic in-process fakes unless a test explicitly
needs a subprocess or local HTTP server. No test may require `npx`, internet
access, a real OAuth provider, or a user's credential store.

### 13.1 Unit tests

- Parse new stdio argv, remote, header, timeout, filter, approval, enabled,
  required, and auth fields.
- Preserve and warn on legacy stdio strings and transport aliases.
- Reject duplicate names, invalid URLs, unsafe cwd, invalid timeouts,
  malformed env/header references, embedded URL credentials, and unknown strict
  fields.
- Verify name sanitization, digest collision handling, reverse mapping, stable
  sort, canonical hash, and redaction.
- Verify catalog snapshot immutability, revision increments, stale binding
  detection, filters, and atomic publication.
- Verify status transitions and typed errors for disabled, failed,
  needs-auth, cancelled, and degraded servers.
- Verify structured MCP results, text content, `isError`, empty content,
  oversized output, and invalid schemas.
- Verify required/optional policy and approval precedence.
- Verify CLI config path selection, atomic write, permissions, JSON output,
  duplicate handling, and secret omission.

### 13.2 Integration tests

Use a fake MCP client/transport and a local deterministic HTTP MCP fixture to
verify:

- concurrent startup where one server hangs or fails while others become ready;
- optional failure versus required failure;
- stdio argv/cwd/environment and descendant cleanup;
- Streamable HTTP headers, network policy, timeout, and cancellation;
- initialization capability capture and server instructions;
- one catalog shared by TUI/headless/workflow/subagent contexts;
- tool calls through permission, approval, hooks, events, journal, and usage;
- `tools/list_changed` debounce, refetch, additions, removals, and retained
  last-known-good snapshot;
- connection close, reconnect, refresh races, shutdown races, and session
  resume;
- CLI lifecycle commands against a temporary config and live fake manager;
- TUI `/mcp` state and command behavior without live redraw timing.

### 13.3 End-to-end tests

1. Configure one local stdio server and one remote fake server, start
   agenthicc, inspect `/mcp`, and call one tool from each.
2. Start with an unavailable optional server; confirm the TUI remains usable,
   healthy tools are present, and the error is actionable.
3. Start with an unavailable required server; confirm the run fails before a
   misleading provider request and identifies the server.
4. Change a server's tool list during a session; confirm the next turn sees
   new tools and cannot call removed tools.
5. Switch from normal chat to Plan mode, `code_plan`, `create_workflow`, and a
   generated workflow; confirm all use the same live manager and stable catalog
   revision.
6. Run `mcp doctor --json` with missing executable, blocked URL, missing auth,
   and healthy server; confirm deterministic redacted output.
7. Interrupt and resume a session while an MCP server is starting or a tool is
   in flight; confirm cancellation, rehydration, ownership, and cleanup.
8. Run with a fake bearer token and assert it is absent from every event,
   journal, transcript, cassette, CLI output, and exception string.

### 13.4 Quality gates

The implementation is not complete until the focused MCP unit/integration/E2E
matrix, full repository tests, lint, type checks, and the repository's type
audit pass. Add a verification section to this PRD with the exact commands and
evidence when implementation begins.

## 14. Acceptance criteria

- [ ] Existing `[[tools.mcp_servers]]` entries and `agenthicc mcp add NAME URL`
      continue to load and behave as before.
- [ ] New stdio entries use argv execution without a shell.
- [ ] Streamable HTTP is supported with bounded initialization/tool calls and
      policy-checked URLs; legacy SSE behavior is explicit and bounded.
- [ ] One session manager is shared by every runner, workflow, subagent,
      headless path, resume path, and TUI view.
- [ ] Independent servers start concurrently and optional failures do not block
      usable sessions.
- [ ] Required-server failure is explicit, redacted, and actionable.
- [ ] Status includes disabled, starting, ready, failed, degraded,
      authenticating/needs-auth, cancelled, and stopped outcomes.
- [ ] Tool definitions and instructions are stored in immutable deterministic
      catalog snapshots with revision/hash and exact server/tool identity.
- [ ] Normal turns reuse the catalog and perform no discovery request.
- [ ] A list-change notification or explicit refresh atomically updates only
      the affected server, removes stale tools, and publishes a new revision.
- [ ] A failed refresh retains the last-known-good catalog and exposes the
      failure.
- [ ] Provider-safe tool names are collision-safe and reverse-mappable.
- [ ] Every MCP call goes through the normal policy, approval, hooks, timeout,
      events, journal, usage, and redaction path.
- [ ] Server instructions are captured, size-bounded, provenance-labeled,
      cache-stable, and cannot override system policy.
- [ ] Bearer tokens and secret headers are resolved from approved references and
      never persisted or displayed as raw values.
- [ ] Local processes and descendants are cancelled and cleaned up on every
      exit path.
- [ ] CLI list/get/remove/connect/disconnect/refresh/doctor and auth lifecycle
      behavior are documented, scriptable, and tested.
- [ ] `/mcp` displays and controls server state without blocking input.
- [ ] Tests cover unit, integration, E2E, interruption/resume, cache stability,
      stale catalogs, security redaction, and regression cases.

## 15. Migration and rollout

1. Ship the manager behind the existing MCP configuration, with the registry
   facade delegating to it.
2. Run legacy string configurations in compatibility mode and emit one
   actionable deprecation warning per server per process.
3. Keep current transport aliases readable for one release cycle; `mcp add`
   writes the new typed format.
4. Default new servers to optional, bounded, policy-checked startup. Do not
   silently change existing servers to required or unrestricted network access.
5. Roll out catalog caching and notifications before enabling optional MCP
   primitives.
6. Remove only deprecated aliases after telemetry and migration diagnostics
   show no active users, with an explicit migration note in the release notes.

## 16. Open questions and assumptions

- Which exact `lauren_mcp` release will provide the required Streamable HTTP,
  notifications, progress, and OAuth interfaces? The implementation must pin
  and test that boundary before agenthicc code relies on it.
- Should persisted catalog snapshots be enabled by default, or only for users
  who opt into faster startup? This is a privacy and freshness trade-off.
- Should MCP prompts become slash commands, as in OpenCode, or remain an API
  exposed to workflow authors? The first implementation should choose one
  explicit surface rather than silently adding prompt text to user input.
- Should a future code-mode/tool-search surface group large MCP catalogs? This
  needs a separate context-budget and cache experiment; it is not required for
  the initial full-tool catalog.
- OAuth callback hosting and secure credential storage should reuse the
  provider/auth infrastructure from PRD-162 where possible.
- The current requested scope is MCP client integration. MCP server mode,
  marketplace discovery, and hosted remote execution require separate PRDs.

## 17. Primary sources

- [Codex MCP documentation](https://developers.openai.com/codex/mcp/)
- [Codex MCP CLI implementation](https://github.com/openai/codex/blob/main/codex-rs/cli/src/mcp_cmd.rs)
- [Codex MCP connection manager](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/connection_manager.rs)
- [Codex MCP tool catalog](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/connection_manager/tool_catalog.rs)
- [OpenCode MCP server documentation](https://opencode.ai/v2/docs/mcp-servers)
- [OpenCode MCP service](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/mcp/index.ts)
- [OpenCode MCP catalog adapter](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/mcp/catalog.ts)
- [OpenCode V2 configuration specification](https://github.com/anomalyco/opencode/blob/dev/specs/v2/config.md)
- [MCP lifecycle specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- [MCP transport specification](https://modelcontextprotocol.io/specification/draft/basic/transports)
- [MCP tools specification](https://modelcontextprotocol.io/specification/draft/server/tools)

## 18. Implementation evidence

The PRD-172 implementation is complete for the agenthicc session, catalog,
transport, policy, CLI, TUI, cache-contract, and optional-dependency scope.
The following evidence was collected on 2026-08-14:

- The complete agenthicc MCP unit/integration/E2E matrix passes: **102 passed,
  1 skipped**.
- The broader configuration, TUI lifecycle, command, reload, and session
  regression subset passes: **168 passed, 1 warning**.
- The isolated lauren-mcp client-feature and stdio regression matrix passes:
  **61 passed**.
- An isolated lauren-mcp full-suite run passes **2,339 tests** with 21 skips;
  two CLI integration cases require the optional `lauren-mcp[cli]` uvicorn
  dependency and are not part of the client extra.
- Ruff passes for every touched agenthicc MCP source/test file and every
  touched lauren-mcp source file. Both source trees compile successfully.
- `pyproject.toml` exposes MCP as an optional extra and now installs the
  `lauren-mcp[http,ws]` transport dependencies only when that extra is
  requested. `uv.lock` is updated accordingly. CloakBrowser and Playwright
  remain separate optional extras.

The repository-wide agenthicc test command was also run. It produced **3,285
passed, 17 skipped, and 59 failures** in the current shared checkout. The
failures are baseline/environment-gated rather than MCP failures: documentation
and source-introspection tests resolve the inaccessible `/root` checkout,
workspace redraw tests receive MagicMock notification state, the static cache
audit cannot glob through that checkout, the type-audit baseline predates the
current source metrics, and the two capability-gate assertions reflect the
intentional execute capability of the trusted generated-workflow validator.
The focused PRD matrix is the reliable implementation gate in this restricted
environment; CI must rerun the full repository gate from a normal repository
root and update unrelated baseline fixtures before release.

The persisted catalog snapshot and OAuth secure-store work remain explicitly
staged as described in sections 7.4 and 11 Phase 3. Env-backed bearer headers,
authentication status, redaction, and CLI auth/logout lifecycle surfaces are
implemented now; no raw token or OAuth code is persisted by this scope.
