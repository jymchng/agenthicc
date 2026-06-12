# Kernel Reference

The kernel (`agenthicc.kernel`) is the event-sourced core of Agenthicc.
It exposes the immutable `AppState`, the `Event` record, the `EventProcessor`
MPSC queue, and the `root_reducer` pure function.

See [llms-full.txt](../../llms-full.txt) for the complete API reference with
signatures and common errors for every exported symbol.

## Quick imports

```python
from agenthicc.kernel import (
    AppState,
    AgentInstance,
    AgentStatus,
    Effect,
    EffectExecutor,
    EffectType,
    Event,
    EventProcessor,
    Intent,
    IntentStatus,
    NodeStatus,
    NoOpEffectExecutor,
    PermissionRule,
    ReducerFn,
    SecurityPolicy,
    SystemSettings,
    Task,
    ToolRegistration,
    Workflow,
    WorkflowNode,
    restore_from_log,
    root_reducer,
)
```

## Key invariants

- `AppState` is **frozen** — never assign to its fields directly.
- Use `with_*` helpers (`with_intent`, `with_workflow`, etc.) for updates;
  they return new copies and share unchanged sub-dicts by reference.
- `root_reducer(state, event)` is a **pure function** — no IO, no async.
- `EventProcessor.emit` is the only entry point for writes.
- Always call `await processor.drain()` before reading state after emitting.

For detailed documentation see:
- [Architecture guide](../guides/architecture.md) — event-sourcing explanation
- [llms-full.txt](../../llms-full.txt) — full symbol reference
