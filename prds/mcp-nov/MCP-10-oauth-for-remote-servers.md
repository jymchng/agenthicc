---
title: "MCP-10: OAuth for remote MCP servers"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P2
related_prds:
  - MCP-00
  - MCP-02
  - MCP-03
  - MCP-05
  - MCP-09
  - prd-146
  - prd-172
---

# MCP-10 — OAuth for remote MCP servers

Hosted MCP servers authorise with OAuth. agenthicc can send a static bearer token
from an environment variable and nothing else: the `oauth` setting is parsed and then
only puts the server in `needs_auth` for good, `mcp auth` prints whether a variable
is set, and `mcp logout` prints a sentence.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | `oauth` is accepted as `true`, `false` or a table and stored. Its only reader returns `needs_auth` when it is set. No flow starts, ever, so a server configured with `oauth` can never become `ready`. | `tools/mcp_server_config.py:58,~215`; `tools/mcp_manager.py:1172-1182` |
| 2 | `mcp auth NAME` reports `configured` or `not_configured` from the presence of `env_headers` or a `${…}` token; it authenticates nothing. `mcp logout` says no credential was removed. | `cli/commands/mcp.py:235-259` |
| 3 | Authentication failure is detected by searching the error text for `401`, `403`, `unauthorized`, `authentication` or `auth required`. Any failure whose message contains "401" (a port, an id) is reported `needs_auth`. | `tools/mcp_manager.py:1213-1218` |
| 4 | `AUTHENTICATING` exists as a state and is never set. | `tools/mcp_manager.py:101` |
| 5 | The library's client accepts an `httpx` `auth=` object on the Streamable HTTP and SSE factories (not on WebSocket) and ships a client-credentials provider with an in-memory token store and one 401 retry. It has no authorization-code flow, discovery or registration. | `lauren-mcp` `_client/_factory.py:143-162`, `_oauth.py`; upstream PRD `prd-oauth-resource-indicators-cimd.md` (draft) |
| 6 | agenthicc already has a working PKCE login for its own service: a loopback `aiohttp` callback, code exchange, refresh and revoke, and a `TokenStore` that uses the OS keyring with an owner-only file fallback. It is tied to fixed endpoints and one token shape. | `auth.py:117-235` (`TokenStore`, `AuthClient.login`, `_exchange_code`, `_refresh`, `_revoke`) |
| 7 | The specification, 2025-06-18: for HTTP transports, clients MUST parse `WWW-Authenticate` on a 401, use RFC 9728 protected-resource metadata and RFC 8414 server metadata for discovery, implement PKCE, send the RFC 8707 `resource` parameter in both authorization and token requests, send `Authorization: Bearer` on **every** request, never put tokens in the query, never send a token to the server other than one its authorization server issued, and store tokens securely; dynamic client registration (RFC 7591) is SHOULD; redirect URIs MUST be localhost or HTTPS. A stdio server SHOULD NOT use this flow and takes credentials from the environment. | MCP specification, Authorization, read 2026-11-28 |
| 8 | Static-token servers work today via `token = "${VAR}"` or `env_headers`, and the config validator refuses literal secret headers. | `tools/mcp_server_config.py:validate` |
| 9 | The current revision (2026-07-28) **deprecates Dynamic Client Registration** in favour of Client ID Metadata Documents (CIMD): a client's `client_id` is an HTTPS URL, with a path, of a JSON document the client hosts (`client_id`, `client_name`, `redirect_uris` required). Clients that support all options SHOULD try, in order: pre-registered credentials; CIMD when the authorization server advertises `client_id_metadata_document_supported`; DCR as a fallback when it advertises a `registration_endpoint`; otherwise prompt the user. A client that uses DCR MUST send an `application_type` (`native` for a CLI). | spec 2026-07-28, Client Registration; Changelog Deprecated item 4 and minor item 8 |
| 10 | Also new in 2026-07-28: when an authorization response carries `iss` (RFC 9207) the client MUST validate it against the recorded issuer before redeeming the code; persisted client credentials MUST be keyed by the authorization server's `issuer`, never reused with another, and re-registered when the server changes. | spec 2026-07-28, Changelog minor items 7 and 9; Client Registration → Authorization Server Binding |
| 11 | agenthicc's own login already uses a fixed domain for OAuth endpoints (`https://agenthicc.ai/oauth/…`). Whether the project can host a CIMD document there is not something the repository shows. | `auth.py:31-33` |

## Goals

1. `agenthicc mcp auth NAME` (and `/mcp auth NAME`) completes an OAuth authorization-code
   flow with PKCE against a server that requires it, and the server becomes `ready`.
2. Tokens are stored securely, refreshed, bound to the server they were issued for, and
   revoked on logout.
3. `needs_auth` is determined from the server's actual challenge, not from text
   matching.
4. Background and headless sessions never open a browser and say what to do.

## Non-goals

- OAuth for stdio servers (specification: credentials come from the environment).
- Making Dynamic Client Registration the primary path: it is deprecated for new
  implementations, and is kept as a fallback only.
- Acting as an authorization server.
- OAuth over WebSocket (the library offers no hook; headers remain the only option).
- URL-mode elicitation flows (MCP-09 non-goal).

## Design

### D1 — Configuration

```toml
[[tools.mcp_servers]]
name = "docs"
transport = "streamable_http"
url = "https://mcp.example.com/mcp"
oauth = true                     # discovery + dynamic registration

# or explicit, for servers without dynamic registration:
[tools.mcp_servers.oauth]
client_id = "agenthicc-cli"
client_secret_env = "DOCS_CLIENT_SECRET"   # confidential clients only
scopes = ["read"]
redirect_port = 8765                       # registered redirect: http://127.0.0.1:8765/callback
grant = "authorization_code"               # or "client_credentials" for service accounts
```

A `[tools.mcp_servers.oauth]` table from a project config takes effect only after
the server is trusted (MCP-05).

### D2 — Discovery from the challenge

A 401 produces a typed `McpAuthChallenge(resource_metadata_url, scope, error)` parsed
from `WWW-Authenticate` (not a string search). The adapter then:

1. fetches the protected-resource metadata (RFC 9728) and picks an authorization server
   (the first listed, or the one named in `oauth.authorization_server`);
2. fetches its metadata (RFC 8414; OpenID discovery as a fallback) and checks that the
   `issuer` matches, every endpoint is HTTPS, and `S256` PKCE is offered;
3. caches both per resource for the session.

Failure at any step leaves the server `needs_auth` with the step and reason named.

### D3 — The flow

`mcp auth NAME` runs, in this order: **choose a client identity** by the
specification's priority (pre-registered `oauth.client_id`; else CIMD when the
authorization server advertises it and agenthicc has a hosted metadata URL (see Decisions
required); else Dynamic Client Registration, deprecated, with `application_type:
"native"`, the name `agenthicc` and the loopback redirect URI; else ask the operator for a
client id); build the authorization URL with
`code_challenge` (S256), a random `state` and
`resource=<canonical server URI>`; **show the authorization host to the operator and
ask before opening the browser** (print the URL when no browser is available); receive
the code on the loopback listener (the same `aiohttp` pattern as `auth.py`, bound to
`127.0.0.1`, closed after one use); verify `state` and, when present, `iss` against the issuer recorded at discovery; exchange the code with
`code_verifier` and the same `resource`; store the result; reconnect the server under a
new generation. The canonical URI follows RFC 8707: lowercase scheme and host, no
fragment, no trailing slash unless significant.

### D4 — Storage

Generalise `auth.TokenStore` into a `CredentialStore` that keeps, per
`(server name, resource, issuer)`: the access and refresh tokens, expiry, scope and the
registered client (id and secret). Backend: the OS keyring under service `agenthicc-mcp`
with an owner-only (`0600`) file fallback in `~/.agenthicc/`, exactly the existing
policy. Never written to a project directory, config file, session log or transcript.
Refresh-token rotation is stored atomically. `mcp logout NAME` revokes at the
`revocation_endpoint` when one exists (RFC 7009), then deletes.

### D5 — Using the token

The adapter supplies an `httpx.Auth` object to the Streamable HTTP and SSE clients (the
library's existing `auth=` hook). It adds `Authorization: Bearer` to **every** request,
refreshes shortly before expiry and once after a 401, and **refuses to attach the token
to any request whose host is not the resource's host** (including after a redirect;
MCP-05 D5). A 403 `insufficient_scope` does not trigger an automatic browser prompt: it
sets `needs_auth` with the missing scope and tells the operator to run `mcp auth NAME
--scope …`. `grant = "client_credentials"` uses the library's provider with a
`TokenStorage` implemented over D4.

### D6 — State

`NEEDS_AUTH` comes from the typed challenge (or a missing credential), `AUTHENTICATING`
is set while a flow runs, and `_is_auth_error` is deleted. `mcp list --json` and `/mcp
status` show `auth_state` (`not_required`, `needs_auth`, `authenticating`,
`authenticated`, `expired`) and the expiry time, never a token.

### D7 — Background and headless

No browser, no prompt. A server in `needs_auth` raises the MCP-01 notice with the fix
`agenthicc mcp auth NAME`; a background session waiting on it (MCP-07) reaches
`WAITING_INPUT` with that instruction. Because the store is per user, a token obtained
interactively is used by the user's background sessions without another login.

## Decisions required

1. **CIMD hosting.** CIMD needs a stable HTTPS URL, with a path, serving agenthicc's client
   metadata document, and its `redirect_uris` must match the loopback URIs the flow uses
   (the specification's example lists `http://127.0.0.1:<port>/callback`). Who hosts it,
   and where, is a project decision. Without one, the flow uses pre-registered credentials
   or DCR (while it lasts), and servers that support neither are reachable only with
   `oauth.client_id` set by the operator.
2. **Loopback port.** CIMD and exact-match redirect validation favour a fixed set of
   registered loopback ports over an ephemeral one.

## Failure handling

| Case | Result |
|---|---|
| Metadata unreachable or issuer mismatch | `needs_auth`, step named, no flow started |
| Callback with wrong `state` | rejected, listener closed, nothing exchanged |
| Operator declines the browser prompt | `needs_auth`, unchanged |
| Refresh fails | token discarded, `needs_auth` with `expired` |
| Registration refused | message naming `client_id` / `redirect_port` options |
| `iss` present and different from the recorded issuer | the response is discarded, nothing is exchanged |
| Authorization server changes between sessions | stored client credentials are not reused; the flow re-registers |
| Keyring unavailable | file fallback with a one-line notice |

## Security

Everything in the specification list in evidence row 7 is a requirement here. Additional
rules: the callback listener binds loopback only and accepts exactly one request; the
operator sees the authorization host before the browser opens, which is the phishing
check for a server-supplied authorization endpoint; tokens never enter tool results,
prompts, logs or `redacted()` output (the redaction list includes stored token values);
a token is bound to one resource and one issuer and is never sent elsewhere.

## Rollout and migration

1. Needs MCP-02 (typed challenge from the adapter) and MCP-03 (a peer with a fake
   authorization server).
2. Ship D6 and the discovery (D2) first so failures are honest, then D3–D5, then D7.
3. Existing `token = "${VAR}"` and `env_headers` servers are unchanged. Servers with only
   `oauth` set move from "permanently `needs_auth`" to working.
4. Update `docs/guides/mcp.md` (an "Authentication" section replacing the `mcp auth`
   description), `llms-full.txt`, `CHANGELOG.md`; mark PRD-172 Phase 3 item 2 delivered.

## Acceptance criteria

Against the MCP-03 `needs_auth` scenario with a fake authorization server.

1. A 401 with `WWW-Authenticate: Bearer resource_metadata="…"` yields `needs_auth` and a
   parsed challenge; an unrelated error containing "401" does not.
2. `mcp auth NAME` completes the flow; the authorization server's log shows
   `code_challenge_method=S256`, a verified `state`, and the **same canonical `resource`
   in the authorization and the token request**.
3. Every subsequent HTTP request to the peer carries `Authorization: Bearer`.
4. An expired token is refreshed once and the rotated refresh token is stored.
5. A peer that redirects to a second host receives no token.
6. A callback with a wrong `state` is rejected and no token is stored.
7. No token appears in the config file, the session log, the transcript or
   `mcp doctor --json`; with the keyring unavailable the fallback file is mode `0600`.
8. `mcp logout NAME` calls the revocation endpoint and a later call is `needs_auth`.
9. With a peer authorization server advertising `client_id_metadata_document_supported`
   the flow uses the metadata URL as `client_id`; with only a `registration_endpoint` it
   registers with `application_type: "native"`; a pre-registered `client_id` wins over both.
10. An authorization response whose `iss` differs from the recorded issuer is discarded.
11. Credentials stored for one issuer are not offered to another.
12. A background session with the server in `needs_auth` opens no browser and ends in
   `WAITING_INPUT` with the instruction.

## Verification

Fake authorization server and protected peer in the MCP-03 harness; mutation checks:
omit `resource` from the token request, skip the `state` check, attach the token after
a cross-host redirect, write a token to the log.

## Risks

- This is the largest security surface in the series; the acceptance tests encode the
  specification's MUSTs one by one.
- Upstream may later ship an authorization-code flow (U7). The `httpx.Auth` boundary lets
  agenthicc adopt it by replacing one class.
- Servers differ in registration support; D1 covers static client ids.
