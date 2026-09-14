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

`AppState` (`src/agenthicc/kernel/state.py:143`) is an immutable dataclass.
Every `with_*` helper returns an updated copy that shares unchanged sub-dicts by
reference — copy-on-write, so updates are cheap.

| Field | Type | Holds |
|---|---|---|
| `session_id` | `str` | Session identity |
| `run_id` | `str` | Current run identity |
| `intents` | `dict[str, Intent]` | User intents |
| `workflows` | `dict[str, Workflow]` | Workflow graphs |
| `tasks` | `dict[str, Task]` | Assigned tasks |
| `agents` | `dict[str, AgentInstance]` | Agent instances |
| `tools` | `dict[str, ToolRegistration]` | Registered tools |
| `hooks` | `dict[str, dict[str, object]]` | Registered hooks |
| `snapshot_index` | `int` | Snapshot position |
| `settings` | `SystemSettings` | Session settings |
| `policy` | `SecurityPolicy` | Security policy |
| `agent_types` | `dict[str, type]` | Agent type registry |

Build one with `AppState.create(settings=..., policy=...)` (`kernel/state.py:161`),
which generates fresh `session_id` and `run_id` values and empty mappings.

Three status enums partition the lifecycle (`kernel/state.py:26-46`):

- `IntentStatus`: `pending`, `validating`, `planning`, `running`, `complete`,
  `failed`, `rejected`
- `NodeStatus`: `pending`, `running`, `paused`, `complete`, `failed`, `skipped`,
  `discarded`
- `AgentStatus`: `idle`, `busy`, `terminated`

---

## `Event`

An `Event` (`src/agenthicc/kernel/events.py:86`) is a frozen-shaped record:

| Field | Default | Meaning |
|---|---|---|
| `event_id` | — | Unique id |
| `event_type` | — | **Plain string** used as the dispatch key |
| `timestamp` | — | Epoch seconds |
| `payload` | — | `dict[str, object]` |
| `source_agent_id` | `None` | Emitting agent, when applicable |
| `tool_call_id` | `None` | Correlating tool call, when applicable |

Construct one with `Event.create(event_type, payload, source_agent_id=None,
tool_call_id=None)` (`kernel/events.py:95`), which fills `event_id` and `timestamp` for
you. `to_dict()` (`kernel/events.py:111`) and `from_dict()` (`kernel/events.py:122`) round-trip
it for the JSON-lines log.

Because `event_type` is a plain `str` and not an enum, a misspelled type is
accepted by `Event.create` and simply has no handler. See the troubleshooting
table — this is the kernel's quietest failure mode.

Payload field extraction goes through tolerant helpers — `_payload_str`
(`kernel/events.py:14`), `_payload_optional_str` (`:26`), `_payload_float` (`:38`),
`_payload_bool` (`:45`), `_payload_mapping` (`:57`), and `_payload_string_list`
(`:71`) — which is why a log line written by an older build often still loads.

---

## Reducers

A reducer is a pure function with the signature declared by `ReducerFn`
(`src/agenthicc/kernel/reducer.py:33`):

```python
ReducerFn = Callable[[AppState, Event], tuple[AppState, list[Effect]]]
```

It takes the current state and one event and returns the next state plus a list
of effects to perform. It performs no I/O itself.

`root_reducer` (`kernel/reducer.py:36`) is the dispatcher:

```python
def root_reducer(state, event):
    handler = _HANDLERS.get(event.event_type)
    if handler is None:
        return state, []
    return handler(state, event)
```

An unknown event type returns the state unchanged with no effects — a silent
no-op rather than an error. Registering a reducer means adding an entry to
`_HANDLERS` (`kernel/reducer.py:378`), which currently maps twenty event types:

| Group | Event types |
|---|---|
| Intent | `IntentCreated`, `IntentStatusChanged`, `IntentCancelled` |
| Agent | `AgentSpawnRequest`, `AgentStatusChanged` |
| Workflow graph | `WorkflowCreated`, `WorkflowNodeAdded`, `WorkflowNodeRemoved`, `WorkflowNodeStatusChanged` |
| Workflow run | `WorkflowRunStarted`, `WorkflowPhaseCompleted`, `WorkflowRunCompleted`, `WorkflowRunPaused`, `WorkflowRunResumed`, `WorkflowRunDiscarded`, `WorkflowCheckpointSaved` |
| Task and registration | `TaskCreated`, `TaskAssigned`, `ToolRegistered`, `HookRegistered` |

Handlers are one-per-event and named `_intent_created`, `_workflow_run_completed`,
and so on (`kernel/reducer.py:46-359`). Passing your own `reducer=` to
`EventProcessor` replaces the dispatch table wholesale, which is the supported
way to run an isolated or test-specific kernel.

### Why effects exist

A reducer must stay pure, so anything with a side effect is returned as data.
`EffectType` (`kernel/events.py:133`) enumerates the seven effect kinds:

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
(`kernel/events.py:144`). A reducer that mutates state *and* does I/O is a bug: move the
I/O into an effect executor.

---

## The `EventProcessor`

`EventProcessor` (`src/agenthicc/kernel/processor.py:35`) is a multi-producer,
single-consumer event loop. Producers call `emit` (`:100`); one `run` task
(`:112`) dequeues events, applies the reducer, appends to the log, notifies
snapshot subscribers, and schedules effects.

```python
processor = EventProcessor(
    initial_state=AppState.create(),
    reducer=root_reducer,          # default
    effect_executor=None,          # default NoOpEffectExecutor
    persist=True,                  # write the append-only log
)
```

`EffectExecutor` is a `Protocol` with one async `execute(effect, state)` method
(`kernel/processor.py:26`). `NoOpEffectExecutor` (`kernel/processor.py:30`) implements it as a
no-op, which is the default — so an effect is only performed if you supply an
executor.

### Reading state and subscribing

| Method | Returns |
|---|---|
| `get_state()` (`:65`) | Current `AppState` |
| `event_log` (`:69`) | Copy of the applied event list |
| `subscribe()` (`:72`) | `Queue[AppState]`, `maxsize=100` |
| `unsubscribe(q)` (`:77`) | Removes a snapshot subscriber |
| `subscribe_events(maxsize=1024)` (`:83`) | `Queue[Event]` of applied events |
| `unsubscribe_events(q)` (`:90`) | Removes an event subscriber |

Note the two queues have different purposes and different bounds.
`subscribe()` yields whole states for a view; `subscribe_events()` yields
individual events for a projection that wants to react without owning reducer
state. Both bounds are finite, so a slow subscriber can fill its queue — drain
it or unsubscribe.

`event_log` returns a copy, so mutating your copy cannot corrupt the kernel.

---

## Replaying the log

`restore_from_log(log_path, initial_state, reducer=root_reducer)`
(`kernel/processor.py:191`) rebuilds `AppState` by replaying the JSON-lines event log.

Its documented tolerance matters in practice: corrupt trailing lines, which are
what a crash mid-write leaves behind, are skipped with a warning rather than
aborting the replay (`kernel/processor.py:207-211`). A truncated final line therefore
costs you the last event, not the whole history.

This is the mechanism behind durable session resume, so a reducer that has
changed shape since the log was written can make an old log replay into a
different state. Treat reducer semantics as a compatibility surface.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| An emitted event changes nothing, with no error | `event_type` is a plain string; unknown types no-op | Check the exact spelling against the `_HANDLERS` table |
| State changes but nothing happens in the UI | Effects are data until an executor runs them | Supply an `EffectExecutor` instead of relying on the default no-op |
| A subscriber stops receiving updates | Its bounded queue filled | Drain the queue in the consumer loop |
| `restore_from_log` silently drops the last event | The final log line was truncated by a crash | Expected; re-emit the event if it matters |
| A custom reducer loses built-in behaviour | Passing `reducer=` replaces the whole dispatch table | Delegate to `root_reducer` for the types you do not handle |
| Reducer tests interfere with each other | Shared `AppState` instance | `AppState` helpers are copy-on-write; build a fresh state per test |
| Confusion between two types named `AppState` | Kernel state (`kernel/state.py:143`) vs the TUI reactive state | Import from `agenthicc.kernel` for kernel work |

---

## Key points

- `AppState` (`kernel/state.py:143`) is immutable and copy-on-write; `with_*`
  helpers return new copies sharing unchanged sub-dicts.
- A reducer is pure: `(AppState, Event) -> (AppState, list[Effect])`.
- `root_reducer` (`kernel/reducer.py:36`) dispatches on `event_type`; an unhandled type
  returns the state unchanged with no effects, silently.
- Event types are **strings**, not an enum. Only `EffectType` is an enum.
- `_HANDLERS` (`kernel/reducer.py:378`) covers twenty event types across intent, agent,
  workflow graph, workflow run, and registration groups.
- Side effects are returned as `Effect` records and performed by an
  `EffectExecutor`; the default executor does nothing.
- `subscribe()` gives whole states; `subscribe_events()` gives events. Both are
  bounded.
- `restore_from_log` tolerates corrupt trailing lines so a crash costs one event,
  not the history.
