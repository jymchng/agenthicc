---
name: testing-agenthicc
version: 1.0.0
tags: [testing, pytest, cassettes, fixtures, replay]
description: >-
  Test agenthicc with its own fixtures and cassette replay: SessionCassette,
  CassetteEntry and ApprovalEntry, MockApprovalService, run_headless_replay and
  ReplayResult, plus recording a cassette from a live session with --record-cassette.
---

# Skill: Testing Agenthicc

There are two ways to test agenthicc, and they solve different problems:

1. **Unit and integration tests** drive real kernel objects — an
   `EventProcessor` and a reducer — with fixtures from `tests/conftest.py`.
2. **Cassette replay** replays a recorded provider session through the real
   workflow runner with no TUI, no network, and no API key.

Reach for the first for logic you wrote, the second for workflow behaviour you
do not want to re-drive against a live model.

## When to use this skill

Use this skill when you need to:

- Assert on kernel state and on the events that produced it
- Avoid the classic `emit()`-then-read race
- Feed a deterministic provider response to an agent runner
- Replay a recorded session as a regression test
- Record a new cassette from a live session

---

## The shared fixtures

All of these live in `tests/conftest.py`.

| Fixture | Line | Signature | Use when |
|---|---|---|---|
| `tmp_settings` | `:74` | → `SystemSettings` | You need temp event-log/snapshot paths |
| `fresh_appstate` | `:85` | (`tmp_settings`) → `AppState` | Base for every kernel test |
| `harness` | `:118` | (`fresh_appstate`) → harness | You need to assert *which* events were emitted |
| `running_processor` | `:127` | (`fresh_appstate`) → `EventProcessor` | You only need a running processor |
| `mock_transport_factory` | `:177` | → `make_mock_transport` | You want a pre-loaded provider transport |

```python
@pytest.fixture
def tmp_settings(tmp_path) -> SystemSettings:
    return SystemSettings(
        event_log_path=str(tmp_path / ".agenthicc" / "events.jsonl"),
        snapshot_path=str(tmp_path / ".agenthicc" / "snapshot.json"),
        max_parallel_tasks=5,
        agent_pool_size=5,
        snapshot_every_n_events=1000,
    )
```

`tmp_settings` exists so tests never touch the project `.agenthicc/` directory.
Prefer it over constructing `SystemSettings` by hand.

### `harness` vs `running_processor`

The `harness` fixture wraps an `EventProcessor` with a capturing reducer that
records every event before delegating to `root_reducer`. It exposes:

| Member | Purpose |
|---|---|
| `captured` | Every event the reducer saw, in order |
| `events_of_type(event_type)` | Filter `captured` by event type |
| `wait_for_event(event_type, timeout=2.0)` | Poll every 5 ms until the event appears |

`EventProcessor` itself has no `events_of_type`; that is a harness affordance.
The harness helper is defined inside the test suite and is **not** exported from
`agenthicc.testing.__all__` — it is test infrastructure, not a public API.

Use `harness` when you must assert on emitted events; use `running_processor`
when you only need state after processing. `running_processor` has no capturing
reducer, so it is lighter.

Both fixtures cancel their run task in teardown. Do not cancel it yourself.

---

## `drain()` — the single most common test bug

`EventProcessor` processes events asynchronously in its `run()` task. `emit()`
returns as soon as the event is queued; it does not wait for the reducer to
apply it. Reading state without `drain()` is a race.

```python
# WRONG — state may not reflect the event yet
async def test_intent_bad(running_processor):
    await running_processor.emit(Event.create("IntentCreated", {
        "intent_id": "i1", "raw_text": "hello",
    }))
    state = running_processor.get_state()   # may still be empty
    assert "i1" in state.intents            # flaky

# CORRECT — drain() waits for the queue to empty
async def test_intent_good(running_processor):
    await running_processor.emit(Event.create("IntentCreated", {
        "intent_id": "i1", "raw_text": "hello",
    }))
    await running_processor.drain()         # deterministic
    state = running_processor.get_state()
    assert "i1" in state.intents
```

Signatures: `emit(event)` and `drain(timeout=5.0)`. `drain` raises
`asyncio.TimeoutError` when the queue does not empty in time, which surfaces a
deadlock instead of hanging the suite.

---

## Asserting on events with the harness

```python
async def test_intent_created_event(harness):
    await harness.processor.emit(
        Event.create("IntentCreated", {"intent_id": "i-001", "raw_text": "write tests"})
    )
    await harness.processor.drain()

    events = harness.events_of_type("IntentCreated")
    assert len(events) == 1
    assert events[0].payload["intent_id"] == "i-001"
```

Or wait for an event that another task emits:

```python
async def test_intent_status_event(harness):
    await harness.processor.emit(
        Event.create("IntentCreated", {"intent_id": "i-002", "raw_text": "ship it"})
    )
    event = await harness.wait_for_event("IntentCreated", timeout=2.0)
    assert event.payload["intent_id"] == "i-002"
```

---

## Testing the reducer directly (pure, no async)

`root_reducer(state, event)` returns a `(AppState, list[Effect])` tuple. It is
synchronous, so it needs no fixtures at all.

```python
from agenthicc.kernel import AppState, Event, root_reducer

def test_intent_created_reducer():
    state = AppState.create()
    new_state, effects = root_reducer(
        state,
        Event.create("IntentCreated", {"intent_id": "i1", "raw_text": "hello world"}),
    )

    assert "i1" in new_state.intents
    assert new_state.intents["i1"].raw_text == "hello world"
    assert new_state.intents["i1"].status.value == "pending"
    # Effects are NOT empty: the reducer schedules a signal and a TUI update.
    assert [e.effect_type.value for e in effects] == ["emit_signal", "update_tui"]
```

> **Common error:** assuming a reducer returns no effects. `IntentCreated`
> produces two `Effect` descriptors (`emit_signal`, `update_tui`), so
> `assert effects == []` fails. Assert on `effect_type` instead.

`Event.create` accepts `(event_type, payload, source_agent_id=None,
tool_call_id=None)`.

---

## Feeding deterministic provider responses

`make_mock_transport(responses)` builds a lauren-ai `MockTransport` pre-loaded
with completions (`tests/conftest.py:135`). Each entry is either:

- a **plain string** → an `end_turn` completion with that content; or
- a **dict** `{"tool_calls": [{"name": ..., "input": {...}}], "content": "..."}`
  → a `tool_use` completion.

```python
def test_agent_runner_tool_call(mock_transport_factory):
    transport = mock_transport_factory([
        {
            "tool_calls": [{
                "name": "application_log",
                "input": {"level": "INFO", "message": "starting task"},
            }],
            "content": "",
        },
        "Task complete",     # second completion ends the turn
    ])
    # Pass `transport` to the agent runner under test.
```

Provide one entry per provider round trip; running out raises `IndexError`, so a
short list is usually a sign the test needs one more response.

---

## Cassette replay

`agenthicc.testing.__all__` is exactly:

```text
SessionCassette, CassetteEntry, ApprovalEntry,
MockApprovalService, ReplayResult, run_headless_replay
```

### Loading a cassette

```python
from agenthicc.testing import SessionCassette

cassette = SessionCassette.from_path(
    cassette_path="tests/fixtures/plan_mode/cassette.jsonl",
    approvals_path="tests/fixtures/plan_mode/approvals.jsonl",
    intent="enhance this repo",
)
```

| Factory | Use |
|---|---|
| `SessionCassette.from_path(cassette_path, approvals_path=None, intent="")` | Explicit paths — preferred for tests |
| `SessionCassette.from_session(session_id)` | Auto-discover files recorded by `--record-cassette` |

`from_path` falls back to an adjacent `meta.json` for the intent when you do not
pass one. `from_session` looks under:

```text
~/.agenthicc/sessions/<session_id>/cassette/cassette.jsonl
~/.agenthicc/sessions/<session_id>/cassette/approvals.jsonl
~/.agenthicc/sessions/<session_id>/cassette/meta.json
```

It raises `FileNotFoundError` naming the expected path when no cassette exists.

### Running the replay

```python
from agenthicc.testing import run_headless_replay

result = await run_headless_replay(cassette)   # intent defaults to cassette.intent
assert result.status == "complete"
assert "finalize_plan" in result.tools_called
assert result.phases == ["plan", "execute", "review", "summarize"]
```

`run_headless_replay(cassette, *, intent=None, cfg=None)` builds the minimal
service set `CodePlanRunner` needs — no Rich console, no kernel event queue, no
MCP, no TUI subscriptions — runs it, and returns a `ReplayResult`. Pass
`intent=` when the cassette has no `meta.json`, and `cfg=` to supply an
`AgenthiccConfig` instead of loading `agenthicc.toml` from disk.

### What `ReplayResult` tells you

| Field | Meaning |
|---|---|
| `status` | `"complete"`, `"failed"`, or `"unknown"` |
| `phases` | Phase names that emitted `WorkflowPhaseCompleted`, in order |
| `tools_called` | Tool names seen in `tool_complete` conversation events |
| `approvals_consumed` | Approval requests dequeued from the mock service |
| `transport_calls` | Number of provider `complete()` calls made |
| `error` | Exception text when the runner raised, else `None` |

Assert on `status`, `phases`, and `tools_called` — together they catch a removed
tool, a broken phase gate, a missing approval gate, and a max-iteration
regression.

### Mock approval

`SessionCassette.to_mock_approval_service()` returns a `MockApprovalService`
pre-loaded with the recorded `ApprovalEntry` objects. It dequeues them in order
and **auto-approves once the queue is exhausted**, so a cassette shorter than
the live session can still run to completion. Inspect `.consumed` to see how
many recorded approvals were used.

The service is a drop-in for `ApprovalService`: it implements
`request_approval(req) -> ApprovalResponse`.

### Cassette tests are skipped by default

Replay is slow, so the `cassette` marker is skipped unless targeted
(`tests/conftest.py:24-70`):

```bash
# run just the replay suite (this un-skips it)
uv run pytest tests/integration/test_cassette_replay.py

# or add the switch to any invocation
uv run pytest --run-cassette
```

---

## Recording a cassette

Record from a live session:

```bash
uv run agenthicc --record-cassette tests/fixtures/plan_mode/
```

`--record-cassette` takes an optional directory
(`src/agenthicc/cli/parser.py:58`). Recording is implemented by
`RecordingTransport` (`src/agenthicc/testing/recording_transport.py:68`), which
wraps the real transport and appends each completion, and
`RecordingApprovalService` (`src/agenthicc/testing/recording_approval.py:29`),
which records each approval decision. A `meta.json` capturing the intent is
written alongside so `from_path` can recover it.

---

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Not calling `drain()` after `emit()` | Flaky state assertions | `await processor.drain()` before `get_state()` |
| Not starting the `run()` task | `drain()` never returns | Use `harness`/`running_processor`, which start it |
| Using `harness` just to read state | Unnecessary capturing overhead | Use `running_processor` |
| Cancelling the run task in the test | Double-cancel / task leak warnings | Let the fixture teardown do it |
| Assuming a reducer has no effects | `assert effects == []` fails | Assert on `effect_type` values |
| Too few mock transport entries | `IndexError` mid-test | Add one response per provider round trip |
| Writing to the real `.agenthicc/` | Polluted project state | Pass `persist=False` (as the fixtures do) |

---

## Key points

- `drain(timeout=5.0)` is the synchronisation primitive; always await it before
  reading state.
- Prefer `tmp_settings` and `fresh_appstate` so nothing touches the real
  `.agenthicc/` directory.
- `harness` for event assertions, `running_processor` for plain state.
- `root_reducer` returns `(AppState, list[Effect])` and is synchronous.
- Cassette replay is the regression-test entry point: `SessionCassette` in,
  `ReplayResult` out.
- `MockApprovalService` auto-approves past the end of the recording.
- Cassette tests carry `@pytest.mark.cassette` and are skipped unless the replay
  file is named explicitly or `--run-cassette` is passed.
