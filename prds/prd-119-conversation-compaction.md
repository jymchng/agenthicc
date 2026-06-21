# PRD-119 — Conversation Compaction

## Problem

When an agent turn produces very large tool results (e.g. `list_directory(recursive=True)` on a large repo), the accumulated conversation history can exceed the model's context-window limit, producing an unrecoverable 400 error:

```
_plan permanent error on attempt 1: Error code: 400 —
"This model's maximum context length is 1048565 tokens. However, you requested
1589712 tokens (1585616 in the messages, 4096 in the completion)."
```

`ShortTermMemory` trim guards correctly refuse to drop the user intent message, so the full oversized payload is sent with no further mitigation.

## Goal

Introduce conversation **compaction**: summarise the accumulated history into a single dense message via an LLM call, replacing the full history and bringing the token count back within limits. Compaction must be:

- **Automatic** (configurable, default on) — fires silently before every turn when the estimate crosses a threshold.
- **Manual** — invoked explicitly via `/compact`.
- **Visible** — a spinner in the status bar indicates that a compaction LLM call is in flight.

## Solution

### 1. `src/agenthicc/memory/compactor.py` (new)

Two public functions:

```python
def should_compact(memory: ShortTermMemory, exec_cfg: ExecutionSettings | None) -> bool
async def compact_memory(
    memory: ShortTermMemory,
    transport: Any,
    *,
    model: str,
    conv_store: ConversationStore | None = None,
) -> int  # returns new token estimate
```

`compact_memory` lifecycle:
1. Set `conv_store.compaction_active.set(True)` and append a `"system"` scroll-buffer event.
2. Format the current `memory._messages` into a plain-text transcript.
3. Call the transport with a summarisation prompt (non-streaming, max_tokens=2048).
4. Replace `memory._messages` with exactly two messages:
   - `{"role": "user", "content": "[COMPACT SUMMARY]\n{summary}"}`
   - `{"role": "assistant", "content": "Understood. Continuing from the summary."}`
5. Append a completion `"system"` event and set `conv_store.compaction_active.set(False)` in a `finally` block.

### 2. Config — `ExecutionSettings` in `config.py`

| Field | Type | Default | TOML key |
|---|---|---|---|
| `auto_compact` | `bool` | `True` | `[execution] auto_compact` |
| `compact_threshold_tokens` | `int` | `1_000_000` | `[execution] compact_threshold_tokens` |

`_dict_to_config()` reads both fields with `ex.get(...)`.

### 3. `ConversationStore` — new signal

```python
compaction_active: Signal[bool]   # initialised to False
```

### 4. `StatusComponent.render()` — spinner while compacting

When `conv_store.compaction_active()` is `True`, render a spinner frame (cycling `SPINNER_FRAMES`) followed by `" Compacting…"` in place of the normal model/workflow label. The existing `prompt_toolkit` invalidation loop drives the animation.

### 5. `AgentTurnRunner._stream()` — auto-compact hook

Immediately after `ensure_valid()`, before `run_stream()`:

```python
if ctx.session_memory is not None and should_compact(ctx.session_memory, ctx.exec_cfg):
    transport = getattr(ctx.runner, "_transport", None)
    if transport is not None:
        await compact_memory(
            ctx.session_memory, transport,
            model=self._model_id, conv_store=ctx.conv_store,
        )
```

At this call site the user's current message has **not yet been added** to memory (that happens inside `run_stream()`), so only the prior history is compacted — exactly the specified behaviour.

### 6. `/compact` command — `builtins.py`

Add a catalogue entry to `BUILTIN_COMMANDS` with `handler=None`; the command is intercepted in `TUISession.route()` before `dispatch_slash()` (same pattern as `/workflow`):

```python
Command(
    name="/compact",
    description="Summarise conversation history to free context space",
    group="Built-in",
    handler=None,
)
```

### 7. `TUISession` — `/compact` route handler

```python
if parts[0] == "/compact":
    asyncio.create_task(self._handle_compact_command())
    return True
```

`_handle_compact_command()` is an `async` method that:
1. Reads `ctx.session_memory` and `ctx.agent_runner._transport`.
2. Calls `compact_memory(...)`.
3. Shows `notify_transient("⎋ Compacted")` on completion.

## Acceptance criteria

- [ ] `should_compact` returns `False` when `auto_compact=False` or when `token_estimate < compact_threshold_tokens`.
- [ ] `compact_memory` replaces `memory._messages` with exactly two messages starting with `role:"user"`.
- [ ] `compaction_active` is `False` after a successful compaction and after a failed compaction.
- [ ] The status bar shows a spinner with `" Compacting…"` while the LLM call is in flight.
- [ ] `/compact` triggers compaction on the current session memory.
- [ ] `auto_compact` and `compact_threshold_tokens` are readable from `[execution]` TOML.
