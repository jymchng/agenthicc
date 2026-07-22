# Kernel reference

The kernel is the durable domain core in `src/agenthicc/kernel/`.

## Public surface

```python
from agenthicc.kernel import (
    AppState,
    Effect,
    EffectType,
    Event,
    EventProcessor,
    Intent,
    SecurityPolicy,
    SystemSettings,
    Workflow,
    WorkflowNode,
    restore_from_log,
    root_reducer,
)
```

`kernel.__all__` is the authoritative public export list. `llms-full.txt`
contains symbol signatures and event payload notes.

## Event lifecycle

```python
processor = EventProcessor(AppState.create(), persist=False)
task = asyncio.create_task(processor.run())
try:
    await processor.emit(Event.create("IntentCreated", {
        "intent_id": "i1",
        "raw_text": "inspect the repository",
    }))
    await processor.drain()
    state = processor.get_state()
finally:
    await processor.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
```

`EventProcessor` accepts many producers but applies events in one consumer
loop. It stores an in-memory event list, optionally appends JSONL records,
notifies bounded subscriber queues, and schedules returned effects through an
`EffectExecutor`.

## Reducers

`root_reducer(state, event)` returns `(new_state, effects)`. It must be pure.
Unknown event types are handled according to the current reducer policy; add a
handler and a unit test for every new event.

`AppState` is frozen. Use `with_intent`, `with_workflow`, `with_task`,
`with_agent`, `with_tool`, and `with_hook` helpers inside reducers instead of
mutating dictionaries in place.

## Persistence

`EventProcessor` writes serialized events to the path in
`SystemSettings.event_log_path` and periodically writes a lightweight snapshot
to `snapshot_path`. `restore_from_log()` replays valid lines and tolerates
corrupt records according to its current implementation. Session paths and the
separate conversation journal are documented in [Storage](storage.md).

## Important invariant

`drain()` waits for the running consumer to become idle; it cannot make
progress if `run()` was never scheduled. This is the most common integration
test failure in the kernel.
