---
title: "MCP-13: Speak the current MCP revision (2026-07-28) as well as the legacy one"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P1
related_prds:
  - MCP-00
  - MCP-01
  - MCP-02
  - MCP-03
  - MCP-04
  - MCP-09
  - COMM-01
  - COMM-02
---

# MCP-13 — Speak the current MCP revision (2026-07-28) as well as the legacy one

The MCP specification site names **`2026-07-28`** as the current revision. It is not an
increment: it removes the `initialize` handshake and sessions, and puts version and
capabilities on every request. The specification's own compatibility matrix says a
legacy client talking to a modern-only server **fails, with no fall-forward**. agenthicc's
client library speaks only the legacy era. Any server that stops serving the legacy era
becomes unreachable, and the only symptom today would be a raw connection error.

## Evidence

Specification statements were read from the specification site on 2026-11-28 (versioning
page, changelog, deprecated-features registry, client-registration page of the
`2026-07-28` revision). Statements about `lauren-mcp` were read in its source.

| # | What is true | Source |
|---|---|---|
| 1 | The current protocol version is `2026-07-28`. "Modern" versions carry version, identity and capabilities as per-request `_meta`; "legacy" versions (`2025-11-25` and earlier) use an `initialize` handshake. A server MAY serve both ("dual-era"). | spec: Versioning |
| 2 | Compatibility matrix: **Legacy client × Modern server: Fails** ("Legacy clients have no fall-forward mechanism"); Dual-era client × Legacy, Modern or Dual-era server: works. A modern-only server **SHOULD** name its supported versions in the error it returns to `initialize`. | spec: Versioning and Compatibility |
| 3 | A dual-era client detects the server's era with transport-specific probes: on stdio, `server/discover` and fall back on any error that is not a recognised modern error; on Streamable HTTP, attempt a modern request and inspect the body of a `400` before falling back. The result is cached per server. | spec: Versioning and Compatibility |
| 4 | `lauren-mcp` `LATEST = "2025-11-25"`, `SUPPORTED` holds four legacy versions, and `grep -rl "server/discover\|resultType\|subscriptions/listen\|input_required\|ttlMs" src/` finds nothing. It is a legacy-only client. | `lauren-mcp` `_mcp_version.py`; grep |
| 5 | agenthicc passes no `protocol_version` or era when it builds a client, and has no handling for a version error: it reports the library's text as a generic failure. | `tools/mcp.py:309-362`; `tools/mcp_manager.py:519-540` |
| 6 | Upstream tracks this in drafts: dual-era core (P1), `server/discover` and per-request `_meta` (P1), result envelope and caching (P1), MRTR (P1), cancellation conformance (P1), client parity with an `era` selector (P2), `subscriptions/listen` (P2), tasks extension (P2). None is released. | `lauren-mcp` `prds/2026-11-landscape/README.md` §2 (dated 2026-11-26) |
| 7 | Changes in `2026-07-28` that touch agenthicc's MCP features: sessions and `Mcp-Session-Id` removed; `ping`, `logging/setLevel` and `notifications/roots/list_changed` removed; SSE resumability removed (a broken stream loses the request, and clients **MUST** re-issue it as a new request); `resources/subscribe` replaced by `subscriptions/listen`; server-initiated `roots/list`, `sampling/createMessage` and `elicitation/create` replaced by Multi Round-Trip Requests (`InputRequiredResult`, retried with `inputResponses`); every result carries `resultType`; list and read results carry `ttlMs` and `cacheScope`; tasks move to the extension `io.modelcontextprotocol/tasks` (polling with `tasks/get`, `tasks/update`). | spec: Changelog (Major and Minor changes) |
| 8 | Deprecated in `2026-07-28`, "new implementations should not adopt them": Roots, Sampling, Logging, Dynamic Client Registration (replaced by Client ID Metadata Documents) and the HTTP+SSE transport. The earliest removal for the first four is the first revision released on or after 2027-07-28. | spec: Deprecated Features |
| 9 | agenthicc does not retry a failed tool call: a transport failure reconnects before the **next** call and raises for the current one. Row 7's re-issue rule makes that a requirement, not a habit. | `tools/mcp.py:515-570` |

## Goals

1. agenthicc connects to legacy, dual-era and modern servers, chooses the era per server
   by the specification's probes, and the operator sees no difference in tools.
2. When it cannot, it says why: which versions the server supports, which the installed
   client offers, and what to upgrade.
3. The modern features that matter to a host are adopted where the client library
   provides them: cached list results, change notifications over `subscriptions/listen`,
   user input through multi round-trip requests, and long-running work through tasks.
4. Nothing deprecated is newly adopted.

## Non-goals

- Implementing the protocol in agenthicc. The client library owns the wire (MCP-02); this
  PRD is what agenthicc does with it and what it asks of it.
- Sampling, roots and logging (deprecated; MCP-09 records the decision).
- Dropping legacy support. Both eras stay until the registry removes the legacy
  features, which is no earlier than 2027-07-28.

## Design

### D1 — The client library is the gate

The modern era needs a `lauren-mcp` client that does `server/discover`, per-request
`_meta`, the result envelope, `subscriptions/listen`, MRTR and the tasks extension (U10
below). agenthicc raises its floor to the first release that has them and meanwhile does
only D2, D3 and D8, which need nothing new from upstream.

| ID | Request to `lauren-mcp` | Upstream draft |
|---|---|---|
| U10 | a client `era` (`legacy` / `modern` / `auto`), `negotiated_era`, `server/discover` probing with fallback | client parity §4.3; dual-era core |
| U11 | `resultType` handling and a result cache honouring `ttlMs` / `cacheScope` | result envelope and caching |
| U12 | `subscriptions/listen` behind the existing `on_list_changed` / `on_resource_updated` callbacks | subscriptions listen |
| U13 | MRTR: surface `InputRequiredResult` to a handler and retry with `inputResponses` / `requestState` | MRTR |
| U14 | tasks extension client: task handle, `tasks/get`, `tasks/update`, cancel | tasks extension |

### D2 — Per-server era

`era = "auto"` (default) | `"legacy"` | `"modern"` on a server. `auto` probes as the
specification describes and caches the result for the server's configuration, re-probing
when a cached assumption fails. `mcp doctor` and `/mcp status` print `era`, the negotiated
`protocol_version` and the library's supported range.

### D3 — An honest failure (ships first)

A version error from a server is mapped to
`McpUnavailable(reason="protocol_era_mismatch", supported=[…], offered=[…])` with a
message of the form `server "x" supports MCP 2026-07-28 only; the installed client offers up
to 2025-11-25. Upgrade lauren-mcp (needs the dual-era client).` A modern-only server's
error to `initialize` is expected to list its versions (row 2), so the text is read from
it. This is an addition to MCP-01 and needs no library change.

### D4 — Cached lists

Honour `ttlMs`: within it, `tools/list`, `prompts/list` and `resources/list` are served
from the adapter's cache; `cacheScope: "private"` results are cached per server
configuration only. A catalog refresh still happens on a change notification and on
`/mcp refresh`. The catalog hash and revision (PRD-172 §7) are unchanged.

### D5 — Change notifications

The adapter keeps one callback, `on_list_changed(kind)`, and fulfils it from
`notifications/*/list_changed` in the legacy era and from `subscriptions/listen` in the
modern one, opting in to `toolsListChanged`, `promptsListChanged` and
`resourcesListChanged` (and `resourceSubscriptions` only when MCP-08 D5 is enabled).

### D6 — User input through MRTR

An `InputRequiredResult` carries `inputRequests`. The adapter turns each into the
host-side `McpInputRequest` of MCP-09 D6, the operator answers through the existing
question overlay (or the operator inbox in a background session), and the adapter retries
the original request with `inputResponses` and the server's `requestState`. Bounds: at
most 5 rounds per call and the call's overall timeout (MCP-04). Roots and sampling
requests inside `inputRequests` are answered `decline` (deprecated; MCP-09).

### D7 — Long-running calls: task handles

When a server advertises `io.modelcontextprotocol/tasks` and a call returns a task
handle, the call becomes an **MCP task** in agenthicc:

- foreground: the adapter polls `tasks/get` with back-off, feeds progress to the running
  row (MCP-04 D3), and cancelling the turn cancels the task;
- **Ctrl+B** moves it to the background exactly as a shell command (COMM-01): the tool
  returns a handle (`mcp-task-…`), and a finished task is announced to the agent through
  the runtime-notice route COMM-02 built (`RUNTIME_NOTICE_SOURCES`), bounded like a terminal
  notice;
- three small tools exist while a task is outstanding: `wait_mcp_task`, `inspect_mcp_task`
  and `cancel_mcp_task` (read-class except cancel), mirroring `wait_terminal`,
  `inspect_terminal` and `stop_terminal`.

The exact method set (`tasks/get`, `tasks/update`, the cancel method) is read from the
extension's own page when this is implemented; **this PRD did not read that page** and
fixes only the host-side behaviour above.

### D8 — Re-issue is never automatic

After a broken stream the specification requires a new request with a new id. For a tool
call that could have had side effects, agenthicc **does not** re-issue it: the result is
`error_code: "interrupted"`, `retryable: false`, with the text `the connection broke after
the call was sent; its outcome is unknown`, and the model decides. Idempotent reads
(`resources/read`, `tools/list`) may be re-issued once.

### D9 — Deprecated features

No handler for sampling, roots or logging is added (MCP-09 records the decision). The HTTP+SSE
transport keeps working for legacy servers and produces a configuration-time warning that
it is deprecated. Dynamic Client Registration is a fallback only (MCP-10).

## Failure handling

| Case | Result |
|---|---|
| Era probe times out | treated as legacy; `era` shown as `legacy (probe timed out)`; re-probed on the next failure |
| Server answers `UnsupportedProtocolVersionError` | retry with the highest mutually supported version, else D3 |
| `ttlMs` of 0 or absent | no caching |
| MRTR round limit reached | the call fails `input_loop` and names the server |
| Task poll fails repeatedly | the task is reported `lost`; the notice says so |

## Security

The probes send no secrets beyond what the connection already would. MRTR answers go only to
the server that asked, with the MCP-09 guards (secret-looking fields declined, rate limit).
`requestState` is opaque and is echoed back unchanged and never shown to the model. Cached
results are keyed by server and credentials so one account's results cannot serve
another's. D8 prevents a replayed side effect.

## Rollout and migration

1. **Now, without upstream:** D3, D8 and the harness's `era` axis (MCP-03). These make the
   cliff visible and safe.
2. Track U10–U14 upstream; the adapter (MCP-02) already hides the library, so the switch
   is internal.
3. When U10 lands: D2, then D4, D5; then D6 (with MCP-09), then D7.
4. Update `docs/guides/mcp.md` (a "Protocol versions" section), `llms-full.txt`,
   `CHANGELOG.md`.

## Acceptance criteria

Using the MCP-03 peer with an `era` of `legacy`, `modern` and `dual`.

1. Against a modern-only peer with the legacy-only library, the failure message names
   the peer's supported versions and the library's, and `mcp doctor` reports
   `protocol_era_mismatch` (D3).
2. With a dual-era client library: connects to all three peers; `era=auto` records the
   era per peer; the legacy peer's behaviour is unchanged from today (regression).
3. A modern peer's broken stream during `tools/call` is not re-issued, and the result is
   `interrupted` (D8). A `tools/list` is re-issued once.
4. Two `tools/list` calls inside `ttlMs` produce one request on the wire (D4).
5. A `toolsListChanged` event on `subscriptions/listen` refreshes the catalog once and bumps
   the catalog revision (D5).
6. An MRTR tool that needs two inputs asks the operator twice, retries with
   `inputResponses` carrying the server's `requestState`, and succeeds; a decline
   returns a defined error; the sixth round fails `input_loop` (D6).
7. A task-returning tool is polled in the foreground with progress shown; with Ctrl+B it
   returns a handle, a completion notice reaches the agent, and `cancel_mcp_task` stops it
   (D7).
8. `grep -rn "sampling_handler\|roots=" src/agenthicc` finds nothing added by this work.

## Verification

The MCP-03 peer implements both eras and the spec's compatibility matrix is the test table
(client era × server era). Mutation: re-issue a `tools/call` after a broken stream; ignore
`ttlMs`; answer an MRTR request without the `requestState`; each must fail a test.

## Risks

- The date: servers moving to modern-only is outside agenthicc's control. D3 and D8 cost
  little and do not wait for it.
- Upstream drafts may change. The adapter boundary (MCP-02) keeps the change to one file.
- D7 is the largest piece and rests on an extension page not yet read; it is last for that
  reason.
