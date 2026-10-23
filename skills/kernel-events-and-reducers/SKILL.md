---
name: kernel-events-and-reducers
version: 1.0.0
tags: [kernel, events, reducers, effects, state, appstate]
description: >-
  Work with the agenthicc kernel: the immutable AppState, the Event and Effect
  records, pure reducers and the event-type dispatch table, EffectType values,
  the EventProcessor loop and its subscribers, and event-log replay.
---

# Skill: Kernel Events and Reducers

The kernel is agenthicc's state core. Everything the session knows lives in one
immutable `AppState`; everything that changes it is an `Event`; every side effect
is an `Effect` requested by a reducer and performed outside it.

## When to use this skill

Use this skill when you need to:

- Emit an event and know which reducer will handle it
- Add a reducer for a new event type
- Subscribe to state snapshots or to individual events
- Understand why a side effect happens outside the reducer
- Rebuild state from a persisted event log

Do **not** use this skill for the TUI's reactive state, which is a different
type also called `AppState` — see `security-modes-and-approvals` for that
distinction.

---

## `AppState`

`AppState` (`src/agenthicc/kernel/state.py:109`) is an immutable dataclass.
Every `with_*` helper returns an updated copy that shares unchanged sub-dicts by
reference — copy-on-write, so updates are cheap.

| Field | Type | Holds |
|---|---|---|
| `session_id` | `str` | Session identity |
| `run_id` | `str` | Current run identity |
| `intents` | `dict[str, Intent]` | User intents |
| `workflows` | `dict[str, Workflow]` | Live workflow runs |
| `tools` | `dict[str, ToolRegistration]` | Registered tools |
| `snapshot_index` | `int` | Snapshot position |
| `settings` | `SystemSettings` | Session settings |
| `policy` | `SecurityPolicy` | Security policy |
| `agent_types` | `dict[str, type]` | Agent type registry |

Build one with `AppState.create(settings=..., policy=...)` (`kernel/state.py:124`),
which generates fresh `session_id` and `run_id` values and empty mappings.

Two status enums partition the lifecycle (`kernel/state.py:23-40`):

- `IntentStatus`: `pending`, `validating`, `planning`, `running`, `complete`,
  `failed`, `rejected`
- `NodeStatus`: `pending`, `running`, `paused`, `complete`, `failed`, `skipped`,
  `discarded`

---

## `Event`

An `Event` (`src/agenthicc/kernel/events.py:127`) is a frozen record:

| Field | Default | Meaning |
|---|---|---|
| `event_id` | — | Unique id |
| `event_type` | — | **Plain string** used as the dispatch key |
| `timestamp` | — | Epoch seconds |
| `payload` | — | `dict[str, object]` |
| `source_agent_id` | `None` | Emitting agent, when applicable |
| `tool_call_id` | `None` | Correlating tool call, when applicable |

Construct one with `Event.create(event_type, payload, source_agent_id=None,
tool_call_id=None)` (`kernel/events.py:136`), which fills `event_id` and `timestamp` for
you. `to_dict()` (`kernel/events.py:152`) and `from_dict()` (`kernel/events.py:163`) round-trip
it for the JSON-lines log; `from_dict()` validates every field and raises
`ValueError` for a malformed record.

`event_type` is a plain `str`, not an enum, so `Event.create` accepts any
string — but the kernel does not: every type must be declared, either as a
reducer handler or in `PROJECTION_EVENT_TYPES` (`kernel/events.py:23`). An
undeclared type is rejected when it is processed (see below), and
`tests/unit/test_kernel_event_contract.py` fails if any `Event.create` call in
`src/` emits an undeclared type.

Payload fields are read through strict helpers — `_payload_str`
(`kernel/events.py:55`), `_payload_optional_str` (`:67`), `_payload_float` (`:79`),
`_payload_bool` (`:86`), `_payload_mapping` (`:98`), and `_payload_string_list`
(`:112`) — which raise `ValueError` for a missing or mistyped field.

---

## Reducers

A reducer is a pure function with the signature declared by `ReducerFn`
(`src/agenthicc/kernel/reducer.py:31`):

```python
ReducerFn = Callable[[AppState, Event], tuple[AppState, list[Effect]]]
```

It takes the current state and one event and returns the next state plus a list
of effects to perform. It performs no I/O itself.

`root_reducer` (`kernel/reducer.py:34`) is the dispatcher:

```python
def root_reducer(state, event):
    handler = _HANDLERS.get(event.event_type)
    if handler is not None:
        return handler(state, event)
    if event.event_type in PROJECTION_EVENT_TYPES:
        return state, []
    raise UnknownEventError(event.event_type)
```

A projection-only event leaves state unchanged; anything else without a
handler raises `UnknownEventError`. Registering a reducer means adding an entry
to `_HANDLERS` (`kernel/reducer.py:184`); `REDUCED_EVENT_TYPES` is derived from
it. It currently maps nine event types:

| Group | Event types |
|---|---|
| Intent | `IntentCreated`, `IntentStatusChanged` |
| Workflow run | `WorkflowRunStarted`, `WorkflowPhaseCompleted`, `WorkflowRunCompleted`, `WorkflowRunPaused`, `WorkflowRunResumed`, `WorkflowRunDiscarded` |
| Registration | `ToolRegistered` |

An event that should only reach subscribers — a status notice, a client
stream update — belongs in `PROJECTION_EVENT_TYPES` instead of getting a
no-op handler. The contract test also fails on a declared type that nothing
emits, so delete a handler when its last producer goes.

Handlers are one-per-event and named `_intent_created`, `_workflow_run_completed`,
and so on. Passing your own `reducer=` to
`EventProcessor` replaces the dispatch table wholesale, which is the supported
way to run an isolated or test-specific kernel.

### Why effects exist

A reducer must stay pure, so anything with a side effect is returned as data.
`EffectType` (`kernel/events.py:174`) enumerates the seven effect kinds:

| `EffectType` | Value |
|---|---|
| `spawn_agent` | `"spawn_agent"` |
| `execute_tool` | `"execute_tool"` |
| `update_tui` | `"update_tui"` |
| `persist_snapshot` | `"persist_snapshot"` |
| `emit_signal` | `"emit_signal"` |
| `start_workflow_node` | `"start_workflow_node"` |
| `assign_task` | `"assign_task"` |

`Effect` is a frozen dataclass of `effect_type` plus a `payload` mapping
(`kernel/events.py:185`). A reducer that mutates state *and* does I/O is a bug: move the
I/O into an effect executor.

---

## The `EventProcessor`

`EventProcessor` (`src/agenthicc/kernel/processor.py:96`) is a multi-producer,
single-consumer event loop. Producers call `emit` (`:191`) or, from synchronous
code, `emit_nowait` (`:194`); one `run` task (`:208`) dequeues events, applies
the reducer, hands the event to the append-only log (written by a background
thread), calls the sinks, notifies subscribers, and starts effects as
supervised tasks.

When the reducer raises — an undeclared type or an invalid payload — the event
is not applied: the processor logs it, increments `rejected_count`, and
publishes a projection-only `KernelEventRejected` event (`event_id`,
`event_type`, `error`) in its place, so the refusal is persisted and visible.

```python
processor = EventProcessor(
    initial_state=AppState.create(),
    reducer=root_reducer,          # default
    effect_executor=None,          # default NoOpEffectExecutor
    persist=True,                  # write the append-only log
)
```

`EffectExecutor` is a `Protocol` with one async `execute(effect, state)` method
(`kernel/processor.py:41`). `NoOpEffectExecutor` (`kernel/processor.py:45`) implements it as a
no-op, which is the default — so an effect is only performed if you supply an
executor.

### Reading state and subscribing

| Method | Returns |
|---|---|
| `get_state()` (`:133`) | Current `AppState` |
| `add_sink(sink)` (`:173`) | Calls `sink(event)` for every published event; returns a remover |
| `subscribe()` (`:151`) | `Queue[AppState]`, `maxsize=100` |
| `unsubscribe(q)` (`:156`) | Removes a snapshot subscriber |
| `subscribe_events(maxsize=1024)` (`:160`) | `Queue[Event]` of published events |
| `unsubscribe_events(q)` (`:167`) | Removes an event subscriber |
| `applied_count` / `rejected_count` / `dropped_count` | Applied events, rejected events, and notifications lost to full queues |

Note the two queues have different purposes and different bounds.
`subscribe()` yields whole states for a view; `subscribe_events()` yields
individual events for a projection that wants to react without owning reducer
state. Both bounds are finite, so a slow subscriber can fill its queue: the
notification is dropped, counted in `dropped_count` and logged. Drain the queue
or unsubscribe.

The processor keeps no in-memory event history. To collect events — in a test,
or to bridge them into another log — register a sink: sinks run synchronously
in order and see every event, including `KernelEventRejected`.

---

## Replaying the log

`restore_from_log(log_path, initial_state, reducer=root_reducer)`
(`kernel/processor.py:322`) rebuilds `AppState` by replaying the JSON-lines event log.

Its documented tolerance matters in practice: corrupt lines, which is what a
crash mid-write leaves behind, and events the current reducer refuses are
skipped with a warning rather than aborting the replay. A truncated final line
therefore costs you the last event, not the whole history.

This is the mechanism behind durable session resume, so a reducer that has
changed shape since the log was written can make an old log replay into a
different state. Treat reducer semantics as a compatibility surface.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| An emitted event changes nothing and a `KernelEventRejected` event appears | The type is undeclared (often a typo) or the payload is invalid | Read the rejection's `error`; fix the spelling, or declare the type with a handler or in `PROJECTION_EVENT_TYPES` |
| State changes but nothing happens in the UI | Effects are data until an executor runs them | Supply an `EffectExecutor` instead of relying on the default no-op |
| A subscriber misses updates | Its bounded queue filled; `dropped_count` grows and the drop is logged | Drain the queue in the consumer loop, or use a sink |
| `restore_from_log` drops the last event with a warning | The final log line was truncated by a crash | Expected; re-emit the event if it matters |
| A custom reducer loses built-in behaviour | Passing `reducer=` replaces the whole dispatch table | Delegate to `root_reducer` for the types you do not handle |
| Reducer tests interfere with each other | Shared `AppState` instance | `AppState` helpers are copy-on-write; build a fresh state per test |
| Confusion between two types named `AppState` | Kernel state (`kernel/state.py:109`) vs the TUI reactive state | Import from `agenthicc.kernel` for kernel work |

---

## Key points

- `AppState` (`kernel/state.py:109`) is immutable and copy-on-write; `with_*`
  helpers return new copies sharing unchanged sub-dicts.
- A reducer is pure: `(AppState, Event) -> (AppState, list[Effect])`.
- `root_reducer` (`kernel/reducer.py:34`) dispatches on `event_type`; a
  projection-only type leaves state unchanged, and an undeclared type raises
  `UnknownEventError`, which the processor turns into `KernelEventRejected`.
- Event types are **strings**, not an enum. Only `EffectType` is an enum.
- `_HANDLERS` (`kernel/reducer.py:184`) covers nine event types across intent,
  workflow run, and tool registration; `PROJECTION_EVENT_TYPES` declares the
  state-neutral rest.
- Side effects are returned as `Effect` records and performed by an
  `EffectExecutor`; the default executor does nothing.
- `subscribe()` gives whole states; `subscribe_events()` gives events. Both are
  bounded and count their drops; a sink sees every event.
- `restore_from_log` tolerates corrupt trailing lines so a crash costs one event,
  not the history.
