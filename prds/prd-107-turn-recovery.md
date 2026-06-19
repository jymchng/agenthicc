# PRD-107 — TUI Turn Recovery: Always-Recoverable Agent Turns

## Summary

After any exception raised during an agent turn (network timeout, tool crash,
LLM error, cancellation), the TUI must return to a clean, interactive state.
`--resume` must restore the conversation and allow the user to continue without
manual intervention.

---

## Problem Statement

Five distinct failure modes make the TUI unrecoverable after a tool or LLM exception:

### 1. `agent_state` stuck at `ERROR`

```
_stream().except Exception  →  fail_turn()      → agent_state = ERROR
_stream().finally           →  end_turn()       → agent_state = IDLE
exception propagates to
agent_task_body.except      →  fail_turn() again → agent_state = ERROR  ← stuck
```

`InputMode` returns to `IDLE` so the user can type, but `agent_state = ERROR`
persists in the status bar indefinitely — until the next successful turn or restart.

### 2. Duplicate error events

The same exception triggers `append_event("error", ...)` in both
`_stream().except` (with the type name) and `fail_turn()` in
`agent_task_body.except` (without the type name).  The user sees two error
messages, the second one missing the exception class.

### 3. Hung turns with no timeout

A `ReadTimeout` that hangs indefinitely (rather than raising) leaves
`_agent_task` alive with `InputMode = STREAMING`.  There is no watchdog to
cancel the turn automatically.

### 4. Kernel intent never completed

`_emit_intent_complete()` is only called on the success path inside `run()`.
Any exception causes the kernel `AppState` intent to stay permanently at
`"pending"`, accumulating stale entries across sessions.

### 5. Terminal left in raw mode on hard kill

SIGTERM, SIGHUP, OOM kill — none trigger `_reset_terminal_on_exit()`.  The
shell is left in raw/no-echo mode.  Starting a new `--resume` session on a
broken terminal makes the new session appear non-functional.

---

## Root Cause Analysis

| # | Root cause | File | Lines |
|---|---|---|---|
| 1 | Double `fail_turn()` at two independent layers | `tui_session.py`, `agent_turn.py` | 608–612, 391–402 |
| 2 | `fail_turn()` sets `agent_state = ERROR` not `IDLE` | `conversation_store.py` | 153–159 |
| 3 | No `asyncio.wait_for` watchdog | `tui_session.py` | 526–600 |
| 4 | `_emit_intent_complete()` not in `finally` | `agent_turn.py` | 82–103 |
| 5 | No `atexit` / `SIGTERM` handler | `tui_session.py` | 850–873 |

---

## Design

### Layer 1 — `close_turn()`: idempotent single cleanup path

Replace the dual `end_turn()` / `fail_turn()` API with a single
`close_turn(*, error: str | None = None)`:

```python
def close_turn(self, *, error: str | None = None) -> None:
    """Idempotent. Always ends at IDLE."""
    if self._current_turn is not None:
        if error:
            self._current_turn.state = AgentState.ERROR
            self.append_event("error", {"message": error})
        else:
            self._current_turn.state = AgentState.COMPLETE
            self.append_event("turn_complete", {})
    self._current_turn = None
    self.agent_state.set(AgentState.IDLE)   # ALWAYS IDLE
    self.active_tool.set("")
    self._start_time = 0.0
```

`end_turn()` and `fail_turn()` become thin wrappers for backward
compatibility.  `close_turn()` is idempotent — calling it twice is safe.

**Invariant:** `agent_state` always ends at `IDLE` after any turn exit path.

### Layer 2 — Single cleanup site

`_stream()` handles its own exception display (scroll-buffer `error` event) but
does **not** call `fail_turn()`.  `close_turn()` in `_stream().finally` resets
state.

`agent_task_body` and `_resume_workflow_task` call `close_turn()` only when the
turn is still active (i.e., an exception occurred before `_stream()` even
started).  They never call `fail_turn()`.

### Layer 3 — Turn watchdog

`run_turn()` wraps the agent coroutine with `asyncio.wait_for`:

```toml
# agenthicc.toml
[execution]
turn_timeout_s = 300   # 5 minutes; 0 = no limit (default)
```

On `asyncio.TimeoutError`, the turn is closed with a human-readable message:

```
TimeoutError: Turn timed out after 300s — the agent may be stuck on a slow
network call. Use /resume or send a new message to continue.
```

### Layer 4 — Kernel intent always completed

`_emit_intent_complete(status)` is called in `AgentTurnRunner.run()`'s
`finally`, not only on the success path:

```python
_intent_status = "complete"
try:
    await self._stream(...)
except Exception:
    _intent_status = "failed"
    raise
finally:
    await self._emit_intent_complete(status=_intent_status)
```

### Layer 5 — Crash-safe terminal restore

`_run_tui()` installs `atexit` + `SIGTERM`/`SIGHUP` handlers that call
`_reset_terminal_on_exit()` before the process exits:

```python
atexit.register(_reset_terminal_on_exit)
signal.signal(signal.SIGTERM, lambda *_: (_reset_terminal_on_exit(), sys.exit(0)))
signal.signal(signal.SIGHUP,  lambda *_: (_reset_terminal_on_exit(), sys.exit(0)))
```

### Error message formatting

All exception display paths use `_fmt_exc(exc)` which produces:

```
ReadTimeout: HTTPSConnectionPool(host='api.anthropic.com', port=443):
Read timed out. (read timeout=60)
```

Never a bare `str(exc)` without the exception class name.

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | After any tool or LLM exception, `agent_state` returns to `IDLE`. |
| 2 | After any tool or LLM exception, `InputMode` returns to `IDLE`. |
| 3 | Exactly one error event appears in the scroll buffer per exception. |
| 4 | Error events always display `ExceptionType: message`. |
| 5 | `close_turn()` is idempotent — safe to call multiple times. |
| 6 | A hung turn is cancelled after `turn_timeout_s` seconds and the turn is closed cleanly. |
| 7 | After cancellation (Ctrl+C / ESC), `agent_state` returns to `IDLE`. |
| 8 | On SIGTERM or SIGHUP, `_reset_terminal_on_exit()` runs before the process exits. |
| 9 | On `atexit` (any exit path), `_reset_terminal_on_exit()` runs. |
| 10 | The kernel intent is marked `"complete"` or `"failed"` after every turn exit. |
| 11 | `--resume` starts a fresh session that accepts user input immediately. |

---

## Files Changed

| File | Change |
|---|---|
| `tui/conversation_store.py` | Add `close_turn()`, `is_turn_active`; fix `fail_turn()` to end IDLE |
| `runners/agent_turn.py` | Remove `fail_turn()` from `_stream()`; `close_turn()` in finally; emit intent in finally |
| `runners/tui_session.py` | `close_turn()` in `agent_task_body`/`_resume_workflow_task`; turn watchdog; atexit+SIGTERM; `_fmt_exc()` |
| `config.py` | `ExecutionSettings.turn_timeout_s: float = 0.0` |
