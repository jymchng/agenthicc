# Kernel reference

The kernel is the durable domain core in `src/agenthicc/kernel/`. It is the only
package with a declared public export list, and it is deliberately small: an
event envelope, a pure reducer, an async processor, an immutable state object,
and an effect vocabulary.

## Public surface

`kernel.__all__` (`src/agenthicc/kernel/__init__.py:27-49`) is the authoritative
export list and contains **22** names:

```python
from agenthicc.kernel import (
    AgentInstance,        # agent record
    AgentStatus,          # agent lifecycle enum
    AppState,             # frozen root state
    Effect,               # side-effect request
    EffectExecutor,       # abstract effect sink
    EffectType,           # effect vocabulary
    Event,                # event envelope
    EventProcessor,       # async applier
    Intent,               # intent record
    IntentStatus,         # intent lifecycle enum
    NodeStatus,           # workflow-node lifecycle enum
    NoOpEffectExecutor,   # discards effects
    PermissionRule,       # security rule
    ReducerFn,            # reducer type alias
    SecurityPolicy,       # settings: policy
    SystemSettings,       # settings: paths and limits
    Task,                 # task record
    ToolRegistration,     # tool record
    Workflow,             # workflow record
    WorkflowNode,         # workflow graph node
    restore_from_log,     # event-log replay
    root_reducer,         # the dispatcher
)
```

Import the subset you need; the import above is exhaustive only to serve as an
inventory. `agenthicc.__all__` is `None` — the top-level package re-exports
nothing — so `llms-full.txt` symbol coverage is enforced against exactly these
22 names plus any subpackage `__all__`.

## Event lifecycle

A complete producer/consumer round trip:

```python
import asyncio

from agenthicc.kernel import AppState, Event, EventProcessor


async def main() -> None:
    processor = EventProcessor(AppState.create(), persist=False)
    task = asyncio.create_task(processor.run())
    try:
        await processor.emit(Event.create("IntentCreated", {
            "intent_id": "i1",
            "raw_text": "inspect the repository",
        }))
        await processor.drain()
        intent = processor.get_state().intents["i1"]
        print(intent.status)  # IntentStatus.pending
    finally:
        await processor.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


asyncio.run(main())
```

`Event.create(event_type, payload, source_agent_id=None, tool_call_id=None)`
(`src/agenthicc/kernel/events.py:95-106`) fills in `event_id` (a `uuid4` hex
string) and `timestamp`. The payload is a plain `dict[str, object]`; the reducer
extracts fields through the `_payload_*` coercion helpers, which is why a
handler does not crash on a missing key.

`EventProcessor.__init__(initial_state, reducer=root_reducer,
effect_executor=None, persist=True)` (`src/agenthicc/kernel/processor.py:43-48`)
accepts many producers but applies events in a single consumer loop. It:

- keeps an in-memory event list (`_event_log`) and, when `persist=True`, appends
  JSONL records to `SystemSettings.event_log_path`;
- notifies **two** kinds of bounded subscriber queue — `subscribe()` yields
  state snapshots (`maxsize=100`), `subscribe_events()` yields raw events
  (`maxsize=1024`). A full queue drops rather than blocks the loop
  (`asyncio.QueueFull` is caught at `src/agenthicc/kernel/processor.py:144,152`);
- schedules effects through the `EffectExecutor`, defaulting to
  `NoOpEffectExecutor` so a bare processor never performs side effects.

Effect failures are isolated: `_safe_effect` catches every exception and logs it
with `logger.exception` (`src/agenthicc/kernel/processor.py:172-177`). A failing effect therefore
does **not** stop the consumer loop or corrupt state — but it is also not
retried, so a swallowed failure is only visible in the log.

## Reducers

`root_reducer(state, event)` returns `(new_state, effects)` and must be pure.
It dispatches through `_HANDLERS`, a dict keyed by event-type string
(`src/agenthicc/kernel/reducer.py:31-35`).

!!! important "Unknown event types are silently ignored"
    If `_HANDLERS` has no entry for `event.event_type`, `root_reducer` returns
    `(state, [])` unchanged. No exception, no warning, no log line. A typo in an
    event-type string therefore produces a silent no-op that looks exactly like
    a healthy run — which is why every new event needs both a handler and a
    test that asserts state actually changed.

The 20 built-in handlers are:

```text
AgentSpawnRequest, AgentStatusChanged, HookRegistered, IntentCancelled,
IntentCreated, IntentStatusChanged, TaskAssigned, TaskCreated,
ToolRegistered, WorkflowCheckpointSaved, WorkflowCreated,
WorkflowNodeAdded, WorkflowNodeRemoved, WorkflowNodeStatusChanged,
WorkflowPhaseCompleted, WorkflowRunCompleted, WorkflowRunDiscarded,
WorkflowRunPaused, WorkflowRunResumed, WorkflowRunStarted
```

Re-derive this list from source rather than from this page:

```bash
PYTHONPATH=src python -c \
  "from agenthicc.kernel.reducer import _HANDLERS; print(len(_HANDLERS), sorted(_HANDLERS))"
```

`AppState` is frozen. Use the `with_*` copy-on-write helpers inside reducers
instead of mutating dictionaries in place: `with_intent`, `with_workflow`,
`with_task`, `with_agent`, `with_tool` (`src/agenthicc/kernel/state.py:182-195`), and `with_hook`
(`src/agenthicc/kernel/state.py:197`). Each returns a new `AppState` that shares unchanged sub-dicts
by reference, so an unchanged branch costs nothing.

## Persistence

`EventProcessor` writes serialized events to `SystemSettings.event_log_path` and
periodically writes a snapshot to `SystemSettings.snapshot_path`.

`restore_from_log(log_path, initial_state, reducer=root_reducer)`
(`src/agenthicc/kernel/processor.py:191-215`) rebuilds state by replaying the log line by line. It
skips blank lines, skips lines that fail to parse or lack required keys
(logging `skipping corrupt event log line`), and tolerates a `FileNotFoundError`
by returning `initial_state`. Only the reducer's output is restored — the event
list, subscribers, and in-flight effects are not.

!!! warning "The snapshot is a marker, not a resume path"
    `_persist_snapshot` writes only `{"snapshot_index": ..., "session_id": ...}`
    (`src/agenthicc/kernel/processor.py:178-186`). It contains no domain state, and
    `restore_from_log` never reads it. Recovery always replays the JSONL log in
    full; the snapshot exists so tooling can see how far the log had advanced.
    Do not build a fast-resume path on it.

Session paths, the separate conversation journal, and the exact defaults for
`event_log_path` (`.agenthicc/events.jsonl`) and `snapshot_path`
(`.agenthicc/snapshot.json`) are documented in [Storage](storage.md).

## Errors and timeouts

`drain(timeout=5.0)` (`src/agenthicc/kernel/processor.py:104-108`) waits until the queue is empty and
the consumer is idle. It wraps that wait in `asyncio.timeout`, so a processor
whose `run()` task was never scheduled raises `TimeoutError` after five seconds
rather than hanging. This is the most common kernel integration-test failure,
and the fix is almost always a missing `asyncio.create_task(processor.run())`.
Tune the wait with `await processor.drain(timeout=30.0)` when replaying a large
log.

`stop()` only sets a flag; the `run()` task must still be cancelled and
gathered, as the example above does. Cancelling `run()` without awaiting it
leaves the JSONL log file handle open, because the `finally` block that closes
it lives inside the loop.
