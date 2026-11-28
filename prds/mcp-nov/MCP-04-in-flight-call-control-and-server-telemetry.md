---
title: "MCP-04: In-flight MCP calls can be cancelled, bounded and observed"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P1
related_prds:
  - MCP-00
  - MCP-02
  - MCP-03
  - MCP-13
  - COMM-01
  - TOOLR-06
---

# MCP-04 — In-flight MCP calls can be cancelled, bounded and observed

When an MCP tool call runs long, the operator can neither stop it properly nor see
that it is working, and the only limit is one number for the whole server. The
incident behind MCP-01 was a game-generation flow: exactly the workload this
affects.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | A call past `tool_timeout_s` is abandoned with `asyncio.wait_for`. The server is **not told**. | `tools/mcp.py:540-545` |
| 2 | The library's `_request` awaits a future with no timeout and no `finally`; cancelling the awaiting task leaves the pending entry behind and sends nothing. It never emits `notifications/cancelled`. Upstream records the same and plans `cancel()` in its client-parity PRD (draft, P2). | `lauren-mcp` `_client/_base_remote.py:154-169`; `prds/2026-11-landscape/prd-client-parity.md` §1.1 |
| 3 | An operator interrupt cancels the awaiting task, so by reading the two rows above the server keeps running the tool and the host forgets it. (Read from source, not reproduced.) | `tools/mcp.py:540`; `_base_remote.py:154-169` |
| 4 | One timeout covers every tool of a server: `tool_timeout_s = 60.0`. There is no per-tool value and no way to let a call that is making progress run longer. | `tools/mcp_server_config.py:53`, `tools/mcp.py:540` |
| 5 | Progress and log handlers are never installed (MCP-02 row 7), and no progress token is sent (U5), so a server that reports progress has nothing to report to. | `tools/mcp.py:332-362` |
| 6 | The TUI has live progress for compaction only; a running tool row shows its command and elapsed time. | `tui/conversation_store.py:347`, `tui/workspace/running_rows.py:107,129` |
| 7 | The stdio client exposes a public, bounded `stderr_tail` (observed on released 1.2.1) and agenthicc appends it to a failed start. It appears nowhere else: there is no way to read a running server's stderr. | `lauren-mcp` `_client/_stdio.py:205`; `tools/mcp_manager.py:1205-1210` |
| 8 | There is no keep-alive. `DEGRADED` is set only when a catalog refresh fails, and `AUTHENTICATING` is declared and never set. | `tools/mcp_manager.py:635`, `:101` |
| 9 | Concurrent calls to one server are unbounded; the circuit breaker counts transport failures only (5 in a row, 30 s open). | `tools/mcp.py:28-30` |
| 10 | In the current revision (2026-07-28) `ping` and `logging/setLevel` are **removed**, Logging is Deprecated, request-scoped `notifications/progress` stay on the response stream of the request, and a broken response stream loses the request, which clients **MUST** re-issue as a new request. | spec 2026-07-28, Changelog items 4, 5, 9 and Deprecated item 1 |

## Goals

1. Stopping a call (timeout or operator interrupt) tells the server to stop, and
   leaves no pending state behind.
2. A long call that is making progress is allowed to finish; a hung one is not. The
   operator can set limits per tool.
3. Progress is visible, and a stdio server's stderr is available, bounded and treated as
   untrusted.
4. A silent server is noticed before the next call fails.
5. A server cannot be flooded by concurrent calls.

## Non-goals

- Running a call in the background after the turn ends (MCP-13, which depends on
  the protocol's task support).
- Showing server output to the model. Progress and stderr are for the operator.
- Server log notifications and `logging/setLevel`. Logging is **Deprecated** in MCP
  2026-07-28 ("new implementations should not add support for them") and `setLevel` is
  removed there; the specification's migration is stderr for stdio servers and
  OpenTelemetry. agenthicc adopts neither handler.
- Retrying a failed call. A retry could repeat a side effect; the model decides.

## Design

### D1 — Cancel at the adapter

`McpClient.call_tool` (MCP-02 A1) is cancel-safe. On timeout or task cancellation
it:

1. sends `notifications/cancelled {requestId, reason}` (U6);
2. removes the pending entry in a `finally`;
3. discards a response that arrives afterwards;
4. returns the existing envelope with `error_code` `timeout` or `cancelled`
   (`retryable: false` for `cancelled`).

The reason is `"timeout after 60s"` or `"user interrupt"`. A server that ignores
the notification is acceptable; the host has still stopped waiting and said so.
Until U6 exists, `mcp doctor` reports `cancellation: unsupported by lauren-mcp X`
and the adapter does steps 2–4 only.

### D2 — Timeouts per tool, extended by progress

| Setting (per server) | Meaning | Default |
|---|---|---|
| `tool_timeout_s` | the limit for any tool without its own | `60` (unchanged) |
| `tool_timeouts = { generate_level = 900 }` | per-tool limit, keyed by the server's tool name | none |
| `timeout_resets_on_progress` | a progress notification restarts the clock | `false` |
| `tool_max_total_s` | hard ceiling when the clock resets | `900` |

These follow the specification's guidance: implementations SHOULD set timeouts on
all sent requests, SHOULD send a cancellation notification and stop waiting when one
expires, SHOULD let timeouts be configured per request, MAY reset the clock on a
progress notification, and SHOULD always enforce a maximum (MCP specification
2025-06-18, Lifecycle → Timeouts, read 2026-11-28). The defaults for the last two
settings are a proposal for the maintainer to confirm.

### D3 — Progress and logs

- The adapter sends `_meta.progressToken = <tool_call_id>` (U5) and routes
  `notifications/progress` to a new presentation signal
  `conversation.tool_progress[tool_call_id] = (progress, total, message)`.
  `running_rows._row` appends `· 40% · <message>` (the message cut to 60 columns,
  control sequences removed).
- A stdio server's stderr tail (D6) is the diagnostic channel for servers. `/mcp logs
  NAME` prints the most recent 50 lines of it, redacted with the manager's existing
  `_redact`. Log notifications are not handled (see Non-goals).
- Neither progress nor stderr is placed in the model's context.

### D4 — Health

For a session-bound (legacy-era) connection, when `keepalive_s` is set (default off for
stdio, 60 for remote transports) the manager sends `ping` while the server is idle. Two
consecutive misses move the server to `DEGRADED`, start one reconnect under a new
generation id (the existing machinery in `McpSessionManager`), and emit the notice from
MCP-01 D3. `ping` is removed in the current revision, so for modern-era connections health
is the process (stdio exit) and the outcome of real requests; no keep-alive is sent
(MCP-13).

### D5 — Bounded concurrency

`max_concurrent_calls` per server (default `8`) with a FIFO queue. Queue time does
not count against a call's timeout; a call cancelled while queued never reaches the
server.

### D6 — stderr

The stdio tail (`stderr_tail`) is read through the adapter, shown in failure
messages (already built in `McpSessionManager._bridge_error`) and in `/mcp logs`,
bounded to 4 KiB and redacted.

## Failure handling

| Case | Result |
|---|---|
| Response arrives after the cancel | discarded; counted in `/mcp status` as `late_responses` |
| Connection lost with calls pending | every pending call fails `unavailable`, `retryable: true`; entries cleared |
| Server ignores `cancelled` and keeps running | the host reports `cancelled`; `mcp doctor` flags a server whose `slow_tool` run kept going |
| Progress for an unknown token | dropped, counted |

## Security

- Progress `message` and stderr text are server-controlled. They are stripped of
  control sequences, length-bounded, redacted, and rendered as plain text (no
  markup, no hyperlinks).
- A cancel carries a reason string the host builds; it never includes tool
  arguments.
- `tool_max_total_s` prevents a server from extending its own deadline forever by
  emitting progress.

## Rollout and migration

1. Needs MCP-03 (`slow_tool`, `progress`) and MCP-02 (adapter).
2. D1 first (correctness), then D2, D3, D5, then D4 and D6.
3. U5 and U6 are `lauren-mcp` changes; each item that needs one says so and
   degrades to the behaviour above without it.
4. Document the new keys in `docs/guides/configuration.md` and `docs/guides/mcp.md`;
   update `llms-full.txt` and `CHANGELOG.md`.

## Acceptance criteria

Each is asserted against the MCP-03 `slow_tool` / `progress` scenarios.

1. On timeout the peer's frame log shows exactly one `notifications/cancelled` with
   the call's `requestId`; the result is `timeout`; the client has no pending
   requests.
2. Interrupting a turn during a call produces the same frame and a `cancelled`
   result.
3. With `tool_timeouts = { slow = 5 }` and `tool_timeout_s = 1`, `slow` completes at
   3 s and another tool times out at 1 s.
4. With `timeout_resets_on_progress = true`, a call that reports progress every
   second survives past `tool_timeout_s` and stops at `tool_max_total_s`.
5. A progress notification changes the running row within one frame, and a message
   containing an ANSI escape or newline renders as inert text no wider than the cap.
6. `/mcp logs NAME` prints the last stderr lines of a stdio peer, redacted.
7. On a legacy-era connection two missed pings set `DEGRADED` and cause one reconnect; on
   a modern-era connection no `ping` is sent.
8. With `max_concurrent_calls = 2`, five simultaneous calls never put more than two
   in flight at the peer.

## Verification

Peer frame logs (MCP-03) are the oracle. Mutation checks: remove the `finally`, the
reset-on-progress clamp, the redaction, and the queue; each must fail a test.

## Risks

- A cancelled or timed-out call is never re-issued by the host (MCP-13 D8): the
  specification requires a new request after a broken stream, and re-running a tool that
  may have had side effects is the model's decision, not the transport's.
- The timeout guidance above is from the 2025-06-18 Lifecycle page; the 2026-07-28
  revision removed the handshake, so the implementer re-reads where that guidance now
  lives before coding D2.
- A server that does not honour `notifications/cancelled` leaves work running; the
  host cannot fix that, only report it.
- Per-tool timeouts are a new configuration surface. They default to the current
  behaviour.
