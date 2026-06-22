# PRD-126 — Transport Retry with Memory Rollback

> **Architecture revision (gap fixes):** retry lives at the single choke point
> — inside `AgentTurnRunner._stream()` — not scattered across call sites.  This
> is both the cleanest boundary (every workflow phase, `run_phase`, and direct
> TUI turn flows through it) and the *only* place the retry can fire: `_stream`
> catches transient errors and previously swallowed them (PRD-117), so a
> call-site wrapper could never observe them.  The retry is implemented by the
> shared helper `agenthicc.runners.retry.run_with_transport_retry`, also used
> by subagent workers (which call `runner.run()` directly).

## Problem

Occasional `ReadTimeout` errors from the LLM provider (e.g. Anthropic's
Anthropic-compatible endpoint) cause the entire workflow phase to fail
permanently.  The error path in `AgentTurnRunner._stream()` propagates
non-permanent transport errors up to the phase loop, which in code-plan mode
exits immediately (PRD-117 "permanent error early exit").

The underlying issue is twofold:

1. **Transport-level retries do not help** when the timeout fires mid-stream,
   because `memory.add_user(message)` has already been called inside
   `run_stream()`.  A naïve retry of `run_stream()` would double-add the user
   message, producing an invalid conversation the API immediately rejects with a
   400 error.

2. **`build_llm_config()`** constructs `LLMConfig` without passing
   `max_retries`, so the transport uses its own default (which may be 0 for
   the SDK's bare client).

## Solution

### 1. `ExecutionSettings` fields

```python
transport_max_retries:        int   = 3    # TURN-level retry (memory-safe); 0 = disabled
transport_retry_base_delay_s: float = 1.0  # first backoff; doubles each attempt
transport_retry_max_total_s:  float = 0.0  # wall-clock ceiling; 0 = no cap
llm_sdk_max_retries:          int   = 2    # SDK/transport internal retry (pre-stream 429/5xx)
```

Two independent layers (gap 4): `transport_max_retries` is the turn-level,
memory-safe primary mechanism; `llm_sdk_max_retries` is the SDK's internal retry
for clean pre-stream 429/5xx, kept low to avoid a large multiplier.  The total
wall-clock is bounded by `transport_retry_max_total_s` (gap 8).

### 2. Shared helper — `runners/retry.py`

`run_with_transport_retry(turn_fn, *, config, memory, deadline_monotonic,
on_retry, reset_fns)` is the single retry mechanism:

- snapshots `memory` before each attempt; restores on transient error (clean
  history so `run_stream`/`runner.run` re-adds the user message correctly);
- exponential backoff with **jitter** (gap 7);
- **`max_total_duration_s`** wall-clock ceiling (gap 8);
- **`deadline_monotonic`** awareness — skips a retry that cannot run before a
  turn-timeout fires (gap 11);
- **`reset_fns`** — side-effect rollback (approval-turn reset, gap 6);
- async-or-sync **`on_retry`** callback for observability (gap 9).

`CancelledError` / `KeyboardInterrupt` never retried; permanent errors propagate.

### 3. `_is_transient_network_error()` — `runners/agent_turn.py`

Matches `TransientTransportError` and library-specific timeout / connection
type names (`ReadTimeout`, `ConnectTimeout`, `PoolTimeout`, `APITimeoutError`,
`APIConnectionError`, …).  **Excludes** the bare builtin `TimeoutError` (gap 5)
because it is `asyncio.TimeoutError` in 3.11+ and would mask `wait_for` timeouts.

### 4. Retry at the single choke point — `AgentTurnRunner._stream()`

`_stream` runs `ensure_valid()` + compaction once, then wraps the
`run_stream()` + chunk-consumption block in `run_with_transport_retry` via
`_stream_with_retry()`.  Because **every** workflow phase, `run_phase`, and
direct TUI turn flows through `_stream`, all paths get retry with no call-site
wrappers.  Approval-turn state is reset between attempts via `reset_fns`
(gap 6); a `TransportRetryScheduled` kernel event + scroll-buffer notification
are emitted via `on_retry` (gap 9).

Transient errors that survive all retries are swallowed (PRD-117) so the phase
loop re-runs the whole turn and decides.

### 5. Subagent workers — `subagents/pool.py` (gap 3)

Subagent workers call `runner.run()` directly (not via `_stream`), so
`SubagentWorker._execute` wraps that call in `run_with_transport_retry` with a
fresh per-call memory.  Retry config is threaded from `ctx.exec_cfg` through
`make_spawn_subagents_tool` → `SubagentPool` → `SubagentWorker`.

### 6. TUI turn-timeout deadline — `tui_session.py` (gap 11)

`run_turn()` computes `retry_deadline_monotonic = monotonic() + turn_timeout_s`
and threads it through `_run_agent_turn`, so retries are not scheduled with no
budget left before the `asyncio.wait_for` timeout fires.

### 7. `build_llm_config()` — SDK retries only (gap 4)

All four provider branches receive `max_retries=execution.llm_sdk_max_retries`
(not `transport_max_retries`), so the SDK layer and the turn layer no longer
multiply.

## Error taxonomy

| Exception | `_is_permanent_error` | `_is_transient_network_error` | Action |
|---|---|---|---|
| HTTP 400–499 (not 429) | True | False | Fail immediately — never retry |
| HTTP 429, 5xx | False | False | Swallow — phase loop retries whole turn |
| `TransientTransportError` | False | True | Snapshot-rollback retry |
| `ReadTimeout`, `APITimeoutError`, `ConnectError`, … | False | True | Snapshot-rollback retry |
| bare builtin `TimeoutError` (= `asyncio.TimeoutError`) | False | **False** | Not retried (gap 5) |

## Acceptance criteria

| # | Criterion |
|---|---|
| 126.1 | `ExecutionSettings.transport_max_retries` and `transport_retry_base_delay_s` exist with correct defaults |
| 126.2 | `_is_transient_network_error` returns True for `TransientTransportError` and timeout-named exceptions |
| 126.3 | `_is_transient_network_error` returns False for `TransportError` with a 400 status code |
| 126.4 | On transient error, `shared_memory` is restored to its pre-turn snapshot |
| 126.5 | On transient error, a `"⟳ Network error — retrying N/M…"` system event is appended |
| 126.6 | After `transport_max_retries` exhausted, the error propagates normally |
| 126.7 | `CancelledError` and `KeyboardInterrupt` are never retried |
| 126.8 | `build_llm_config()` passes `max_retries` to all provider `LLMConfig` factories |
| 126.9 | `[execution] transport_max_retries = 0` disables retry completely |

## Files changed

| File | Change |
|---|---|
| `src/agenthicc/config.py` | Add 2 new fields to `ExecutionSettings`; read in `_dict_to_config`; pass `max_retries` in `build_llm_config` |
| `src/agenthicc/runners/agent_turn.py` | Add `_is_transient_network_error()` |
| `src/agenthicc/workflows/code_plan/runner.py` | Add `_run_turn_with_retry()`; call it from all 4 phase methods |
| `src/agenthicc/runners/tui_session.py` | Wrap `_run_agent_turn` in `run_turn()` with snapshot-rollback retry |
