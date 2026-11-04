---
title: "TOOLR-06: MCP results, schemas and reconnection"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "how remote MCP tools are presented to providers and how their results come back: content-block handling, size bounds, schema compatibility, and recovery from a crashed server"
related_prds: [TOOLR-00, TOOLR-04, PRD-191]
tags: [tools, mcp, robustness]
---

# TOOLR-06: MCP results, schemas and reconnection

## 1. Problem

TOOLR-00 M1–M3. The MCP session manager is thorough about lifecycle,
catalogs and redaction. Its edges toward the model are not.

- **Results are unbounded and lossy.** `_extract_tool_content`
  (`tools/mcp.py:161`):
  - uses `structuredContent` only when it has a `result` key;
  - joins multi-block content into a list of strings;
  - turns an image block into its raw base64 `data` string, which the model
    reads as thousands of tokens of noise;
  - renders embedded resources and resource links as `str(block)`.

  Nothing bounds the size of the result.
- **Schemas pass through verbatim.** `AgenthiccMcpTool.parameters` returns the
  server's `inputSchema` unchanged. Providers reject some constructs — or the
  whole request — and one bad tool from one server fails every provider call
  of the session. These include:
  - a non-object root;
  - top-level `oneOf`/`anyOf`;
  - `$ref`/`$defs` for some providers;
  - very deep nesting.
- **A crashed server stays down.** `call_tool` raises "not connected" until
  the user runs a reload. A stdio server killed by the OS, or an HTTP server
  that restarted, is never retried.
- **Timeouts are local.** `asyncio.wait_for` gives up on the call, but the
  server is not sent `notifications/cancelled`, so it keeps working on a
  request nobody will read.

## 2. Goals

1. **Content-aware results.** An MCP result becomes a TOOLR-04 envelope:
   - `structuredContent` is preferred whenever present, used whole and
     validated against the tool's `outputSchema` when one is declared;
   - text blocks are concatenated;
   - image and audio blocks are written to the session's artifact directory
     and returned as `{type, mime_type, path, bytes}`, or passed to providers
     as native image content when the provider supports image tool results;
   - embedded resources return their text, or an artifact path for binary;
   - resource links return `{uri, name, mime_type}`;
   - `isError` results return `ok: false, error_code: "remote_error"` with the
     server's text, instead of raising.
2. **Bounded.** Text is capped at `mcp.max_result_chars` (default 50,000)
   with `truncated: true` and the original size. Artifacts are capped at
   `mcp.max_artifact_bytes`.
3. **Provider-safe schemas.** A sanitizer, applied once per catalog revision:
   - inlines local `$ref`s up to a depth bound;
   - wraps a non-object root as `{value: …}` and unwraps it on call;
   - flattens a top-level `oneOf` into an object with the union of
     properties;
   - drops unsupported keywords per provider;
   - caps the depth.

   A tool whose schema still cannot be made safe is withheld from the catalog,
   with a diagnostic in `/mcp status`, instead of breaking the session.
4. **Reconnect on use.** A call to a server in `failed` or `disconnected` state
   triggers at most one reconnect attempt, with the configured backoff, and
   the call then proceeds once. Repeated failures open a circuit for
   `mcp.circuit_open_s`, during which calls fail fast with
   `error_code: "unavailable"`.
5. **Cancellation reaches the server.** A timed-out or cancelled call sends
   `notifications/cancelled` with its request id, where the transport supports
   it.

## 3. Non-goals

- MCP sampling, elicitation or resource subscription.
- Changing catalog lifecycle, trust or the generation model.

## 4. Acceptance criteria

1. A fake server returning one text block and one 200 KB PNG produces a result
   with the text and an artifact path. No base64 reaches the provider request.
2. A 1 MB text result returns 50,000 characters, `truncated: true` and
   `original_chars`.
3. Fake tools with `$ref`, a top-level `oneOf`, and a string root each appear
   in a provider request that a schema validator accepts. An unrepairable
   schema is withheld and reported.
4. Killing a stdio fake server between calls: the next call reconnects and
   succeeds. Five consecutive failures open the circuit, and calls fail fast.
5. An `isError` result returns `ok: false, error_code: "remote_error"` and
   raises nothing.
6. A call that times out causes the fake server to receive
   `notifications/cancelled`.
