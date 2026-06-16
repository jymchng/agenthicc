# PRD-82 — Live Token Usage Display

## Background

The status bar already shows `↑ N,NNN ↓ N,NNN` for input and output
token counts (§1.4 of PRD-68).  However, those counts are only updated
**after** a complete LLM sub-turn has finished streaming — not while it
is in progress.

The token update path today:

```
LLM stream ends (final chunk carries TokenUsage)
  → caller's async-for loop asks for the next chunk
  → _stream_loop generator resumes
  → ModelCallComplete signal emitted (carries turn_usage)
  → _on_model_complete handler fires
  → conv_store.add_tokens(inp, out, cost)
  → signals fire → _redraw() → status bar updates
```

The delay between the final chunk arriving and the signal firing is
one extra `__anext__()` call — a full generator-resume cycle.  During a
typical single-response turn this means the status bar shows `↑ 0 ↓ 0`
for the entire duration of streaming and only updates after the response
is fully printed.

The fix is small and self-contained: read `chunk.usage` directly inside
the streaming loop in `_run_agent_turn`, and guard the existing
`_on_model_complete` handler so it does not double-count.

---

## Goals

- Token counts (`↑ in ↓ out`, `$cost`) update on the **final chunk of
  each LLM sub-turn** — while the caller is still inside the
  `async for _chunk in _stream:` loop — rather than after the generator
  resumes for the next sub-turn.
- For a single-turn response the status bar is correct by the time the
  text event is published to the scroll buffer.
- For a multi-turn agentic response (think → tools → think again) the
  counts are cumulative and update after each sub-turn completes.
- No provider-specific code.  The fix relies only on
  `CompletionChunk.usage`, which is part of the public lauren-ai
  transport contract.
- Providers that do not populate `chunk.usage` continue to work via the
  existing `ModelCallComplete` signal fallback.

---

## Non-Goals

- Per-chunk token-delta updates during text generation.  No provider
  exposes per-chunk deltas; this cannot be done without estimation.
- Streaming cost estimation (character-count heuristics).  Stale
  estimates are worse than a single accurate update.
- Changes to `ConversationStore`, `AppState`, or any TUI component.
  The existing signals (`tokens_in`, `tokens_out`, `cost_usd`) are
  sufficient.
- Changes to lauren-ai internals.

---

## The `chunk.usage` Contract

`CompletionChunk` (lauren-ai transport layer) is defined as:

```python
@dataclass
class CompletionChunk:
    delta:            str           = ""
    thinking_delta:   str | None    = None
    tool_call_delta:  ToolCallDelta | None = None
    stop_reason:      str | None    = None
    usage:            TokenUsage | None = None
    pending_approval: PendingApproval | None = None
    guardrail_override: str | None  = None
```

Inside `_stream_loop`, the inner streaming `async for chunk in stream:`
accumulates `usage` as follows:

```python
if chunk.usage is not None:
    accumulated_usage = chunk.usage
yield chunk
```

**Key invariant**: `chunk.usage` is non-`None` on exactly one chunk per
sub-turn — the same chunk that carries `stop_reason`.  All other chunks
have `chunk.usage = None`.  This is true for every transport backend
(Anthropic, OpenAI, Ollama).

`accumulated_usage` is then passed to `ModelCallComplete`:

```python
turn_usage = accumulated_usage or TokenUsage(0, 0)
await self._emit("ModelCallComplete", ..., usage=turn_usage,
                 cost_usd=turn_usage.cost_usd(model))
```

Since the caller's `async for _chunk in _stream:` receives the same
`CompletionChunk` objects that `_stream_loop` yields, the caller sees
`chunk.usage` at the same moment `_stream_loop` accumulates it — but
**one generator-resume cycle earlier** than `ModelCallComplete` fires.

---

## Timing Analysis

For a multi-turn agent run (sub-turn 1 → tools → sub-turn 2):

### Current flow
```
Caller iterates _stream

Sub-turn 1:
  chunk.delta="I'll "  → caller loop body runs
  chunk.delta="help"   → caller loop body runs
  chunk.stop_reason="tool_use", chunk.usage=T1  → caller loop body runs
  ↑ add_tokens NOT called yet

  caller calls __anext__() ← generator resumes here
  → _stream_loop: ModelCallComplete emitted
  → _on_model_complete fires → add_tokens(T1.in, T1.out, cost)
  → status bar updates ← first visible update
  → tools executed silently

Sub-turn 2:
  chunk.delta="Based"  → caller loop body runs
  ...
  chunk.stop_reason="end_turn", chunk.usage=T2  → caller loop body runs
  ↑ add_tokens NOT called yet

  caller calls __anext__() ← generator resumes, raises StopAsyncIteration
  → _stream_loop exits, ModelCallComplete emitted for T2
  → _on_model_complete fires → add_tokens(T2.in, T2.out, cost)
  → status bar updates ← second visible update, AFTER streaming loop exits
```

### After this PRD
```
Sub-turn 1:
  chunk.delta="I'll "  → caller loop body runs
  chunk.delta="help"   → caller loop body runs
  chunk.stop_reason="tool_use", chunk.usage=T1
    → caller: add_tokens(T1.in, T1.out, cost) called HERE
    → status bar updates ← first visible update, still inside chunk loop
    → caller: text event published

  caller calls __anext__()
  → _stream_loop: ModelCallComplete emitted
  → _on_model_complete: _got_usage_from_chunk[0] is True → skip, reset flag
  → tools executed silently

Sub-turn 2:
  ...
  chunk.stop_reason="end_turn", chunk.usage=T2
    → caller: add_tokens(T2.in, T2.out, cost) called HERE
    → status bar updates ← second visible update, still inside chunk loop
    → caller: text event published
```

The key improvement: **tokens update in the same iteration as the text
event**, not one `__anext__()` later.  For single-response turns the
counts are visible before the response reaches the scroll buffer.

---

## Implementation

### `runners/agent_turn.py`

#### New local variable

```python
# Set to True when chunk.usage is consumed inside the streaming loop.
# Prevents _on_model_complete from double-counting the same sub-turn.
_got_usage_from_chunk: list[bool] = [False]
```

#### Inside `async for _chunk in _stream:`

Add after the existing `_chunk.delta` handling, before `stop_reason`:

```python
# Update token counts as soon as the provider reports them.
# chunk.usage is non-None only on the final chunk of each sub-turn.
if _chunk.usage is not None and conv_store:
    _got_usage_from_chunk[0] = True
    _u = _chunk.usage
    _cost = (
        _u.cost_usd(model_id)
        if callable(getattr(_u, "cost_usd", None))
        else 0.0
    )
    conv_store.add_tokens(_u.input_tokens, _u.output_tokens, _cost)
```

#### Guard in `_on_model_complete`

```python
@_signals.on(_MCC)
async def _on_model_complete(sig: Any) -> None:
    if not _turn_active[0]:
        return
    if _got_usage_from_chunk[0]:
        # Already counted via chunk.usage in the streaming loop.
        # Reset flag so the next sub-turn can use the fallback if needed.
        _got_usage_from_chunk[0] = False
        return
    # Fallback path: provider did not populate chunk.usage.
    usage = getattr(sig, "usage", None)
    inp   = getattr(usage, "input_tokens", 0) if usage else 0
    out   = getattr(usage, "output_tokens", 0) if usage else 0
    cost  = getattr(sig, "cost_usd", 0.0) or 0.0
    if conv_store:
        conv_store.add_tokens(inp, out, cost)
```

### Why the guard is safe

The timing guarantee is structural, not coincidental:

1. The caller's `async for _chunk in _stream:` iteration receives
   `chunk.usage` and sets `_got_usage_from_chunk[0] = True` while the
   generator is suspended at its `yield chunk` statement.
2. The caller then calls `__anext__()`.  Only at this point does the
   generator resume and reach the `await self._emit("ModelCallComplete",
   ...)` call.
3. `ModelCallComplete` fires `_on_model_complete` synchronously (via
   the signal bus) while the generator is running.
4. At that moment `_got_usage_from_chunk[0]` is guaranteed to be `True`
   (set in step 1), so the handler returns early and resets the flag.

There is no race condition because the asyncio event loop is
single-threaded and the generator cannot resume between steps 1 and 2.

### Fallback behaviour

If a provider transport does not set `chunk.usage` on the final chunk:

- `_got_usage_from_chunk[0]` remains `False`.
- `_on_model_complete` executes its existing body, calling `add_tokens`
  with the values from the signal.  Behaviour is identical to today.

---

## `ConversationStore.add_tokens` — no changes needed

The existing method is additive and signal-driven:

```python
def add_tokens(self, inp: int, out: int, cost: float) -> None:
    self.tokens_in.set(self.tokens_in() + inp)
    self.tokens_out.set(self.tokens_out() + out)
    self.cost_usd.set(self.cost_usd() + cost)
```

`Signal.set()` notifies subscribers synchronously, so `_redraw()` fires
immediately, updating the Live block before any subsequent `console.print()`
call in the same event-loop turn.

---

## PRD-68 §2 update

Feature §2.13 (tool group collapse) is unchanged.

Add §1.4 note:

> **1.4 Token counts** — counts update on the final chunk of each LLM
> sub-turn, before the corresponding text event reaches the scroll
> buffer.  During a sub-turn, the status bar shows the cumulative total
> from all previously completed sub-turns.  Per-chunk in-flight
> estimation is out of scope.

---

## File changes

| File | Change |
|---|---|
| `runners/agent_turn.py` | Add `_got_usage_from_chunk` flag; handle `_chunk.usage` in the streaming loop; guard `_on_model_complete` |
| `prds/prd-68-feature-expectations.md` | Update §1.4 with the timing guarantee |

No other files change.

---

## Acceptance criteria

- [ ] For a single-response turn (no tool calls), `↑ in ↓ out` in the
      status bar is non-zero and correct by the time the agent's text
      response appears in the scroll buffer.
- [ ] For a multi-turn agentic run, token counts update after each LLM
      sub-turn (before tool execution begins), and the cumulative total
      is correct at turn end.
- [ ] `conv_store.add_tokens` is called exactly once per sub-turn
      regardless of whether `chunk.usage` is populated or whether the
      `ModelCallComplete` signal fires.
- [ ] With a provider that does not populate `chunk.usage` (e.g. a mock
      transport returning `CompletionChunk(usage=None, stop_reason="end_turn")`),
      the `_on_model_complete` fallback fires and `add_tokens` is still
      called correctly.
- [ ] All existing unit and integration tests pass unchanged.
- [ ] The `_got_usage_from_chunk` flag resets to `False` between
      sub-turns so each sub-turn is counted independently.
