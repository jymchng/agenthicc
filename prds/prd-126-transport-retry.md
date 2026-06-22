# PRD-126 — Transport Retry with Memory Rollback

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

### 1. New `ExecutionSettings` fields

```python
transport_max_retries:        int   = 3    # 0 = disabled
transport_retry_base_delay_s: float = 1.0  # first backoff; doubles each attempt
```

Exposed in `[execution]` TOML section.

### 2. `_is_transient_network_error()` — `runners/agent_turn.py`

Complements the existing `_is_permanent_error()`:

```python
def _is_transient_network_error(exc: BaseException) -> bool:
    """True for ReadTimeout, ConnectError, and other retriable network errors."""
```

Checks for `TransientTransportError` from lauren-ai and common timeout type
names in the exception chain.

### 3. `_run_turn_with_retry()` — `workflows/code_plan/runner.py`

Wraps `_run_turn()` with snapshot-rollback retry:

```
for attempt in range(max_retries + 1):
    snapshot = ctx.shared_memory.snapshot()       ← checkpoint

    try:
        await self._run_turn(text, ...)
        return                                    ← success

    except TransientNetworkError:
        ctx.shared_memory.restore(snapshot)       ← rollback
        await asyncio.sleep(base_delay * 2**attempt)
        # retry
```

Memory is restored to the exact pre-turn state so `run_stream()` adds the user
message on a clean history every time.  `CancelledError` and permanent errors
are never retried.

A scroll-buffer notification (`⟳ Network error — retrying N/M…`) is emitted
on each retry so the user sees progress.

### 4. Phase methods use `_run_turn_with_retry`

`_plan`, `_execute`, `_review`, `_summarize` in `CodePlanRunner` replace their
`_run_turn(...)` calls with `_run_turn_with_retry(...)`.

### 5. TUI session — direct agent turn retry

For non-workflow (Auto / Ask / Review) turns, `run_turn()` in `TUISession`
wraps `_run_agent_turn` the same way: snapshot before, restore on transient
error, retry up to `transport_max_retries` times.

### 6. `build_llm_config()` — forward `max_retries` to transport

All four provider branches (`anthropic`, `openai`, `ollama`, `litellm`) receive
`max_retries=execution.transport_max_retries` so the transport-level retry also
uses the configured value.

## Error taxonomy

| Exception | `_is_permanent_error` | `_is_transient_network_error` | Action |
|---|---|---|---|
| HTTP 400–499 (not 429) | True | False | Fail immediately — never retry |
| HTTP 429, 5xx | False | False | Swallow — phase loop retries whole turn |
| `TransientTransportError` | False | True | Snapshot-rollback retry |
| `ReadTimeout`, `ConnectError` | False | True | Snapshot-rollback retry |

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
