# PRD-83 — Token Tracking Architecture Revamp

## Background

PRD-82 identified that token counts update one generator-resume cycle too
late and added `chunk.usage` handling inside the streaming loop.  That fix
is correct but incomplete: it was grafted onto an existing design that has
two deeper structural problems which PRD-82 did not address.

### Problem 1 — Handler accumulation (memory leak)

`runner` (a `lauren_ai.AgentRunnerBase`) is created once in
`tui_session.py` and reused for every agent turn in the session.  Its
`_signals` (`SignalBus`) is therefore also session-scoped.

Every call to `_run_agent_turn` registers a new `_on_model_complete`
handler on that same bus:

```python
@_signals.on(_MCC)
async def _on_model_complete(sig: Any) -> None: ...
```

There is no deregistration.  After N turns the bus looks like:

```
bus._handlers[ModelCallComplete] = [
    handler_turn_1,
    handler_turn_2,
    ...
    handler_turn_N,
]
```

On every subsequent `ModelCallComplete` emission, `asyncio.gather` fans
out to all N coroutines.  N-1 of them short-circuit via the `_turn_active`
guard, but they are still scheduled, their closures stay alive, and the
list grows without bound.  This is a session-length memory leak and a
steadily worsening performance drag.

The same accumulation affects the `ToolCallStarted` and
`ToolCallComplete` handlers registered in the same block.

### Problem 2 — Fragile multi-frame state co-ordination

PRD-82 added `_got_usage_from_chunk: list[bool]` to prevent the
`_on_model_complete` fallback from double-counting when `chunk.usage`
had already been consumed in the streaming loop.  This requires that:

1. The streaming loop sets the flag to `True` while the generator is
   suspended at `yield chunk`.
2. The signal handler reads the flag when the generator resumes on the
   next `__anext__()` call.
3. The flag is then reset by the handler.

This is correct but relies on implicit asyncio scheduling guarantees.
More importantly, it co-ordinates two separate code paths — the chunk
loop and the signal handler — through a shared mutable cell, which is
exactly the kind of hidden coupling that makes token tracking a source
of subtle bugs.

### Problem 3 — Zero-usage fallback is silent

When a provider does not return usage in streaming chunks (e.g.
OpenAI-compatible endpoints without `stream_options={"include_usage": true}`
support), `accumulated_usage` in `_stream_loop` stays `None`, so
`turn_usage = TokenUsage(0, 0)`.  The `_on_model_complete` fallback then
calls `add_tokens(0, 0, 0.0)`, which is a no-op because the signals do
not fire for unchanged values.  The status bar shows zero forever with
no indication of why.

The `AgentRunnerBase` always populates `AgentRunComplete.total_usage`
from its own internal `total_usage` accumulator (which is `turn_usage +
turn_usage + …`).  For the zero-usage case this is also zero — but
`AgentRunComplete` is the authoritative final record and is emitted
unconditionally, making it the right hook for reconciliation.

---

## Goals

- Eliminate the per-turn handler registration from `_run_agent_turn`.
  All token-related signal subscriptions must be registered exactly once
  per session, not once per turn.
- Remove `_got_usage_from_chunk` and the double-counting guard entirely.
  Token counting must not require co-ordination between two asyncio frames.
- Make the "live" update path (`chunk.usage`) completely self-contained
  inside the streaming loop — no signal bus involvement.
- Make the "reconciliation" path (`AgentRunComplete`) completely
  self-contained in `tui_session.py` — no per-turn knowledge required.
- Token counts shown in the status bar must be correct and non-zero for
  any provider that returns usage, either in chunks or in the run-complete
  signal.
- For providers that return zero usage everywhere, the status bar shows
  zero — which is correct and honest, not a bug.

## Non-Goals

- Per-chunk streaming token estimation (character-count heuristics).
- Changes to `lauren_ai` internals beyond the already-applied
  `stream_options` fix.
- Removing the `ToolCallStarted` / `ToolCallComplete` handlers from
  `_run_agent_turn` — those are tool-tracking, not token-tracking, and
  are out of scope here.

---

## Architecture

### Two-source model

Token counts flow from exactly two places, with no shared state between them:

```
┌─────────────────────────────────────────────────┐
│ _run_agent_turn  (per turn, self-contained)      │
│                                                  │
│  async for _chunk in _stream:                    │
│      if _chunk.usage is not None:                │
│          conv_store.add_tokens(...)   ◄──── (1)  │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ tui_session.py  (once per session)               │
│                                                  │
│  @bus.on(AgentRunComplete)                       │
│  async def _on_run_complete(sig):                │
│      conv_store.set_tokens(...)       ◄──── (2)  │
└─────────────────────────────────────────────────┘
```

**(1) Live path** — `chunk.usage` inside the streaming loop.
Fires on the final chunk of each LLM sub-turn.  Updates `add_tokens`
incrementally.  Self-contained: no signal bus, no flags, no shared state.

**(2) Reconciliation path** — `AgentRunComplete` subscribed once at
session startup.  Fires after the entire multi-sub-turn agentic run
completes.  Calls `conv_store.set_tokens(...)` (see §Data model below)
to **set** the absolute cumulative total.  Because it sets absolutely
rather than adding, it is idempotent and cannot double-count regardless
of what the live path did.

### Why `AgentRunComplete` instead of `ModelCallComplete`

`ModelCallComplete` fires once per LLM sub-turn.  Subscribing to it in
`tui_session.py` would re-introduce the N-handler accumulation problem
(one per turn, never cleaned up) at a different call site.

`AgentRunComplete` fires once per call to `_run_agent_turn` and carries
`total_usage` — the authoritative sum of all sub-turns.  A single
session-scoped handler on this signal covers every agent turn cleanly.

### Signal handler lifetime

```
tui_session.py startup:

    bus = runner._signals          # SignalBus, session-scoped

    @bus.on(AgentRunComplete)
    async def _on_run_complete(sig): ...   # registered ONCE

_run_agent_turn (per turn):

    # No signal subscriptions for tokens at all.
    # chunk.usage handling only.
```

The `ToolCallStarted` / `ToolCallComplete` handlers remain in
`_run_agent_turn` because they are correctly guarded by `_turn_active`
and their state (`_tool_args`, `_tool_names`) is turn-local by design.

---

## Data model

### `ConversationStore.set_tokens(inp, out, cost)`

New method alongside `add_tokens`:

```python
def set_tokens(self, inp: int, out: int, cost: float) -> None:
    """Set token counts to absolute values (reconciliation path)."""
    self.tokens_in.set(inp)
    self.tokens_out.set(out)
    self.cost_usd.set(cost)
```

`Signal.set` is a no-op when the value is unchanged, so calling this
with the same values as a prior `add_tokens` call produces no spurious
redraws.

`add_tokens` remains for the live incremental path and is unchanged.

---

## `AgentRunComplete` signal fields

From lauren-ai:

```python
@dataclass
class AgentRunComplete(LifecycleEvent):
    agent_id:    str | None = None
    agent_class: type | None = None
    agent_name:  str = ""
    turns:       int = 1
    total_usage: Any = None   # TokenUsage — input_tokens, output_tokens, cost_usd(model)
    total_cost_usd: float = 0.0
    stop_reason: str = "unknown"
```

`total_usage` is `TokenUsage(input_tokens, output_tokens)`.
`total_cost_usd` is the pre-computed cost for the run's model.

The reconciliation handler uses `total_cost_usd` directly (already
computed by the runner against the correct model string) rather than
calling `total_usage.cost_usd(model_id)` itself, which avoids any
model-string mismatch risk.

---

## Detailed changes

### `tui/conversation_store.py`

Add `set_tokens`:

```python
def set_tokens(self, inp: int, out: int, cost: float) -> None:
    """Overwrite token counts with authoritative absolute values."""
    self.tokens_in.set(inp)
    self.tokens_out.set(out)
    self.cost_usd.set(cost)
```

### `runners/agent_turn.py`

1. Remove `_got_usage_from_chunk: list[bool] = [False]`.

2. Remove the entire `if _signals is not None:` block that registers
   `_on_model_complete`.  The `ToolCallStarted` and `ToolCallComplete`
   handlers stay — they are NOT token tracking.

3. The `chunk.usage` handling in the streaming loop becomes:

   ```python
   if _chunk.usage is not None and conv_store:
       _u   = _chunk.usage
       _cst = (
           _u.cost_usd(model_id)
           if callable(getattr(_u, "cost_usd", None))
           else 0.0
       )
       conv_store.add_tokens(_u.input_tokens, _u.output_tokens, _cst)
   ```

   No flag.  No guard.  Self-contained.

### `runners/tui_session.py`

After `agent_runner` is constructed, register the reconciliation handler
on its signal bus.  The handler receives `AgentRunComplete` once per
agent turn and sets the absolute cumulative total:

```python
from lauren_ai._signals import AgentRunComplete as _ARC  # noqa: PLC0415

_runner_signals = getattr(agent_runner, "_signals", None)
if _runner_signals is not None:
    @_runner_signals.on(_ARC)
    async def _on_agent_run_complete(sig: Any) -> None:
        usage = getattr(sig, "total_usage", None)
        cost  = getattr(sig, "total_cost_usd", 0.0) or 0.0
        if usage is not None and app_state:
            inp = getattr(usage, "input_tokens", 0)
            out = getattr(usage, "output_tokens", 0)
            app_state.conversation.set_tokens(inp, out, cost)
```

This handler is registered once and never accumulates.  `app_state` is
captured by closure; `app_state.conversation` is the live
`ConversationStore`.

---

## Interaction between the two paths

For a single agent turn with 2 LLM sub-turns and a provider that
populates `chunk.usage`:

```
Sub-turn 1 final chunk arrives:
  → conv_store.add_tokens(100, 20, 0.001)
  → tokens_in=100, tokens_out=20, cost=$0.001  [status bar updates]

Sub-turn 2 final chunk arrives:
  → conv_store.add_tokens(150, 30, 0.0015)
  → tokens_in=250, tokens_out=50, cost=$0.0025 [status bar updates]

AgentRunComplete fires (total_usage = TokenUsage(250, 50), total_cost=0.0025):
  → conv_store.set_tokens(250, 50, 0.0025)
  → Signal.set(250) == 250 → no-op
  → Signal.set(50)  == 50  → no-op
  → Signal.set(0.0025) == 0.0025 → no-op
  → No extra redraw.
```

For a provider that does NOT populate `chunk.usage` (e.g. OpenAI-compatible
without stream_options support):

```
Sub-turn 1 final chunk: chunk.usage is None → add_tokens skipped
Sub-turn 2 final chunk: chunk.usage is None → add_tokens skipped

AgentRunComplete fires:
  If runner accumulated usage internally:
    total_usage = TokenUsage(250, 50), total_cost = 0.0025
    → conv_store.set_tokens(250, 50, 0.0025)
    → tokens_in=250, tokens_out=50  [status bar updates here]

  If runner has zero usage (provider truly returns nothing):
    total_usage = TokenUsage(0, 0), total_cost = 0.0
    → conv_store.set_tokens(0, 0, 0.0)
    → All values already 0 → no-op
    → Status bar shows zero (correct)
```

---

## File changes

| File | Change |
|---|---|
| `tui/conversation_store.py` | Add `set_tokens(inp, out, cost)` |
| `runners/agent_turn.py` | Remove `_got_usage_from_chunk`; remove `_on_model_complete` handler; keep bare `chunk.usage` block (no flag) |
| `runners/tui_session.py` | Register one `AgentRunComplete` handler on `agent_runner._signals` at session startup |
| `tests/e2e/test_live_token_display.py` | Update tests to remove flag-related assertions; add reconciliation test |

---

## Acceptance criteria

- [ ] After N agent turns in a single session,
      `runner._signals._handlers[ModelCallComplete]` contains exactly
      zero agenthicc-registered handlers.  (Tool handlers remain but
      that is a separate concern.)
- [ ] `conv_store.add_tokens` is called at most once per LLM sub-turn
      (from `chunk.usage`), with no call from any signal handler inside
      `_run_agent_turn`.
- [ ] `conv_store.set_tokens` is called exactly once per completed agent
      turn (from `AgentRunComplete`), regardless of how many sub-turns it
      contained.
- [ ] For a provider that populates `chunk.usage`: status bar token
      counts update on the final chunk of each sub-turn and are confirmed
      correct by the `AgentRunComplete` reconciliation (which is a no-op).
- [ ] For a provider that does NOT populate `chunk.usage` but whose
      runner accumulates usage internally: status bar is zero during
      streaming, then shows the correct total after `AgentRunComplete`.
- [ ] Calling `set_tokens` with the same values as already set causes
      zero `_redraw()` calls (Signal equality short-circuit).
- [ ] All existing unit and integration tests pass.
