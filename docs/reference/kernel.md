# Kernel reference

The kernel is the durable domain core in `src/agenthicc/kernel/`. It is the only
package with a declared public export list, and it is deliberately small: an
event envelope, a pure reducer, an async processor, an immutable state object,
and an effect vocabulary.

## Public surface

`kernel.__all__` (`src/agenthicc/kernel/__init__.py:25-49`) is the authoritative
export list and contains **23** names:

```python
from agenthicc.kernel import (
    PROJECTION_EVENT_TYPES,  # declared state-neutral event types
    REDUCED_EVENT_TYPES,     # event types with a reducer handler
    AppState,                # frozen root state
    Effect,                  # side-effect request
    EffectExecutor,          # effect runner protocol
    EffectType,              # effect vocabulary
    Event,                   # event envelope
    EventProcessor,          # async applier
    EventSink,               # synchronous observer of published events
    Intent,                  # intent record
    IntentStatus,            # intent lifecycle enum
    NodeStatus,              # workflow-node lifecycle enum
    NoOpEffectExecutor,      # discards effects
    PermissionRule,          # security rule
    ReducerFn,               # reducer type alias
    SecurityPolicy,          # settings: policy
    SystemSettings,          # settings: paths and limits
    ToolRegistration,        # tool record
    UnknownEventError,       # raised for an undeclared event type
    Workflow,                # workflow record
    WorkflowNode,            # workflow graph node
    restore_from_log,        # event-log replay
    root_reducer,            # the dispatcher
)
```

Import the subset you need; the import above is exhaustive only to serve as an
inventory. `agenthicc.__all__` is `None` — the top-level package re-exports
nothing — so `nox -s llms_check` enforces `llms-full.txt` symbol coverage
against exactly these 23 names.

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
        await task


asyncio.run(main())
```

`Event.create(event_type, payload, source_agent_id=None, tool_call_id=None)`
(`src/agenthicc/kernel/events.py:136`) fills in `event_id` (a `uuid4` hex
string) and `timestamp`. The payload is a plain `dict[str, object]`; reducers
read it through `_payload_*` helpers that raise `ValueError` for a missing or
mistyped field instead of guessing a default.

`EventProcessor.__init__(initial_state, reducer=root_reducer,
effect_executor=None, persist=True)` (`src/agenthicc/kernel/processor.py:109`)
accepts many producers — `emit()` from coroutines, `emit_nowait()` from
synchronous code, in one order — and applies events in a single consumer loop.
For each event it:

- applies the reducer, then hands the event to the append-only JSONL log at
  `SystemSettings.event_log_path` when `persist=True`. A background thread
  writes the log, so file I/O never runs on the event loop;
- calls every **sink** registered with `add_sink()` synchronously, in order.
  Sinks are how tests and bridges collect events: the processor keeps no
  in-memory event history, so its memory does not grow with the event count;
- notifies **two** kinds of bounded subscriber queue — `subscribe()` yields
  state snapshots (`maxsize=100`), `subscribe_events()` yields events
  (`maxsize=1024`). A full queue drops the notification rather than blocking
  the loop; every drop is counted in `dropped_count` and logged on the first
  drop and every 100th after it (`_record_drop`,
  `src/agenthicc/kernel/processor.py:289`);
- starts each effect as a task owned by the processor's `SupervisedTasks`,
  through the `EffectExecutor`. The default `NoOpEffectExecutor` means a bare
  processor never performs side effects.

Effect failures are isolated: the supervisor logs an exception with the
processor and task names. A failing effect does **not** stop the consumer loop
or corrupt state — but it is not retried, so the log is where it shows.

## Reducers and the event contract

`root_reducer(state, event)` (`src/agenthicc/kernel/reducer.py:34`) returns
`(new_state, effects)` and must be pure. Every event type is declared exactly
once:

- `REDUCED_EVENT_TYPES` — the keys of `_HANDLERS`
  (`src/agenthicc/kernel/reducer.py:184`); the reducer applies them;
- `PROJECTION_EVENT_TYPES` (`src/agenthicc/kernel/events.py:23`) — events that
  are persisted and published to subscribers (MCP status, retry notices,
  subagent lifecycle, UI advertisement updates, `KernelEventRejected`) but
  leave kernel state unchanged.

An event in neither set raises `UnknownEventError`, and a handler raises
`ValueError` for an invalid payload. The processor catches any reducer
exception (`_process`, `src/agenthicc/kernel/processor.py:231`): the event is
not applied, `rejected_count` increments, the failure is logged, and a
projection-only `KernelEventRejected` event carrying the `event_id`,
`event_type` and `error` is persisted and published in its place. A typo in an
event-type string is therefore loud, never a silent no-op.

`tests/unit/test_kernel_event_contract.py` scans every `Event.create(...)` call
in `src/` and fails if an emitted type is undeclared, if a declared type is
never emitted, or if the two sets overlap. Adding an event means adding its
handler (or projection declaration) in the same change.

The 9 reducer handlers are:

```text
IntentCreated, IntentStatusChanged, ToolRegistered, WorkflowPhaseCompleted,
WorkflowRunCompleted, WorkflowRunDiscarded, WorkflowRunPaused,
WorkflowRunResumed, WorkflowRunStarted
```

Re-derive this list from source rather than from this page:

```bash
uv run python -c \
  "from agenthicc.kernel import REDUCED_EVENT_TYPES; print(len(REDUCED_EVENT_TYPES), sorted(REDUCED_EVENT_TYPES))"
```

`AppState` is frozen. Use the `with_*` copy-on-write helpers inside reducers
instead of mutating dictionaries in place: `with_intent`, `with_workflow` and
`with_tool` (`src/agenthicc/kernel/state.py:142-149`). Each returns a new
`AppState` that shares unchanged sub-dicts by reference, so an unchanged branch
costs nothing.

## Persistence

`EventProcessor` writes serialized events — including `KernelEventRejected` — to
`SystemSettings.event_log_path` and periodically writes a snapshot marker to
`SystemSettings.snapshot_path`.

`restore_from_log(log_path, initial_state, reducer=root_reducer)`
(`src/agenthicc/kernel/processor.py:322`) rebuilds state by replaying the log
line by line. It skips blank lines, skips lines that fail to parse or validate
(logging `skipping corrupt event log line`), skips events the current reducer
refuses (with a warning naming the event), and returns `initial_state` when the
file does not exist. Only the reducer's output is restored — subscribers and
in-flight effects are not.

!!! warning "The snapshot is a marker, not a resume path"
    `_persist_snapshot` writes only `{"snapshot_index": ..., "session_id": ...}`
    (`src/agenthicc/kernel/processor.py:303`). It contains no domain state, and
    `restore_from_log` never reads it. Recovery always replays the JSONL log in
    full; the snapshot exists so tooling can see how far the log had advanced.
    Do not build a fast-resume path on it.

Session paths, the separate conversation journal, and the exact defaults for
`event_log_path` (`.agenthicc/events.jsonl`) and `snapshot_path`
(`.agenthicc/snapshot.json`) are documented in [Storage](storage.md).

## Errors, timeouts and shutdown

`drain(timeout=5.0)` (`src/agenthicc/kernel/processor.py:201`) waits until every
event emitted so far has been processed — it awaits `asyncio.Queue.join()`, so
it returns only after the last event's reducer, log hand-off, sinks and
subscriber notifications have run. A processor whose `run()` task was never
scheduled raises `TimeoutError` after five seconds rather than hanging. This is
the most common kernel integration-test failure, and the fix is almost always a
missing `asyncio.create_task(processor.run())`. Tune the wait with
`await processor.drain(timeout=30.0)` when replaying a large log.

`stop()` enqueues a stop marker behind any pending events, so `run()` returns
after processing every event emitted before the call; repeated calls before
the marker is consumed are no-ops. Await the `run()` task afterwards, as the
example above does: on exit the loop gives running effects five seconds to
finish, cancels the rest, and flushes and closes the event log. Cancelling the
`run()` task also stops it at once — the loop blocks on `asyncio.Queue.get()`
rather than polling with `asyncio.wait_for`, which on Python 3.11 can drop a
cancellation that races an arriving event.
