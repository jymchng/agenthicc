# PRD-117 — Permanent Error Early Exit for Workflow Phase Loops

## Summary

When an HTTP 4xx error occurs during a workflow phase (e.g. model name not
supported, invalid API key), the phase loop must exit immediately with a clear
diagnostic message instead of retrying up to the maximum attempt cap and
then showing a misleading "exhausted N attempts" failure.

---

## Problem

A `TransportError: 400 — model 'gpt-4o' is not supported` causes the plan
phase to retry **10 times** before failing, printing the same error to the
TUI on every attempt:

```
ERROR TransportError: Error code: 400  (×10, one per attempt)
ERROR code_plan failed: Plan phase exhausted 10 attempts without finalization.
```

**Root cause — two-layer swallow:**

1. `AgentTurnRunner._stream()` catches all `Exception` and emits a TUI error
   event but **does not re-raise** — it falls through to `finally` and returns
   normally.
2. `CodePlanRunner._plan()` calls `_run_turn()`.  Because `_stream()` swallowed
   the exception, `_run_turn()` returns normally.  Neither `plan_event` nor
   `exit_event` was set, so the loop increments the attempt counter and tries
   again.  The `except Exception` block in `_plan()` is never reached.

This repeats for every attempt cap (10 for plan, 10 for execute, 10 for
review).

**Why the swallow is correct for transient errors:**

`_stream()` intentionally swallows exceptions so a single failed LLM turn
(network hiccup, 5xx, timeout) does not crash the whole workflow — the phase
loop can decide whether to retry.  This is correct and must be preserved.

**Why it is wrong for permanent errors:**

HTTP 4xx errors (except 429 rate-limit) are **structurally permanent** — the
same request will always fail regardless of how many times it is retried.
Retrying 10 times wastes time, floods the TUI with duplicate errors, and
produces a misleading final message.

---

## Design

### Error classification

```python
def _http_status_code(exc: BaseException) -> int | None:
    """Extract an HTTP status code from *exc* or its chained causes."""

def _is_permanent_error(exc: BaseException) -> bool:
    """Return True for errors that will never succeed on retry.

    HTTP 4xx (except 429 rate-limit) are permanent.
    HTTP 5xx, timeouts, and network errors are transient.
    """
```

### `_stream()` re-raises permanent errors

```python
except Exception as exc:
    if _is_permanent_error(exc):
        if ctx.conv_store:
            ctx.conv_store.append_event("error", {"message": f"..."})
        raise   # _stream()'s finally still runs → close_turn() is called
    # Transient — swallow, let the phase loop decide
    if ctx.conv_store:
        ctx.conv_store.append_event("error", {"message": f"..."})
```

`_stream()`'s `finally` block always runs (even on re-raise), so
`close_turn()` is called and turn state is correctly cleaned up.

### Phase loops set `ctx.fail_reason` and return immediately

In `_plan()`, `_execute()`, `_review()`:

```python
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                # Permanent error propagated from _stream().
                # Transient errors are swallowed by _stream(), so this branch
                # is only reached for errors that should never be retried.
                ctx.fail_reason = f"{type(exc).__name__}: {exc}"
                log.error("Phase permanent error on attempt %d: %s", attempt, exc)
                return CodePlanState.FAILED
```

### Result

```
ERROR TransportError: Error code: 400 — model 'gpt-4o' is not supported  (×1)
ERROR code_plan failed: TransportError: Error code: 400 — ...
```

One error, immediate exit, TUI returns to idle for new input.

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `_http_status_code(exc)` returns the HTTP status integer or `None`. |
| 2 | `_is_permanent_error(exc)` returns `True` for 4xx (except 429), `False` for 5xx / transient. |
| 3 | `_stream()` re-raises permanent errors after emitting the TUI error event. |
| 4 | `_stream()`'s `finally` block still runs on re-raise (close_turn called). |
| 5 | `_plan()` returns `FAILED` on first permanent error with `ctx.fail_reason` set to the exception message. |
| 6 | `_execute()` and `_review()` do the same. |
| 7 | Transient errors (5xx, timeout, `ConnectionError`) are still swallowed by `_stream()` — phase loops continue to retry. |
| 8 | 429 rate-limit is treated as transient (retried, not permanent). |

---

## Files Changed

| File | Change |
|---|---|
| `runners/agent_turn.py` | Add `_http_status_code()`, `_is_permanent_error()`; modify `_stream()` |
| `workflows/code_plan/runner.py` | `except Exception` in `_plan`, `_execute`, `_review` sets `ctx.fail_reason` and returns `FAILED` |
