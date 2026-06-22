---
skill: testing-agenthicc
version: 1.0.0
tags: [testing, pytest, fixtures, harness, mock-transport, drain]
summary: Complete testing guide — conftest fixtures, EventBusTestHarness, MockTransport, drain() timing, and reducer/TUI/API test patterns.
---

# Skill: Testing Agenthicc

## When to use this skill

Use this skill when you need to:
- Write unit, integration, or e2e tests for Agenthicc components
- Use the shared `conftest.py` fixtures correctly
- Understand `EventBusTestHarness` and when to use it over `running_processor`
- Avoid timing bugs from not calling `drain()` after `emit()`
- Spin up `AgentRunnerBase` in tests with `MockTransport`
- Test the FastAPI endpoints with `TestClient`

---

## Fixtures reference (`tests/conftest.py`)

### `tmp_settings`

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

**Use when**: you need a `SystemSettings` that writes to a temp directory so tests
don't pollute the project `.agenthicc/` folder. Always prefer this over
constructing `SystemSettings` directly in tests.

---

### `fresh_appstate`

```python
@pytest.fixture
def fresh_appstate(tmp_settings) -> AppState:
    return AppState.create(settings=tmp_settings, policy=SecurityPolicy())
```

**Use when**: you need a clean `AppState` with temporary paths. Depends on
`tmp_settings`. Use this as the base for all kernel tests.

---

### `harness`

```python
@pytest.fixture
async def harness(fresh_appstate):
    h = EventBusTestHarness(fresh_appstate)
    task = asyncio.create_task(h.processor.run())
    yield h
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
```

**Use when**: you need to assert which events were processed and need to wait for
specific event types asynchronously. The harness captures every event for inspection.

**Do NOT use** for pure state-read tests — `running_processor` is lighter.

---

### `running_processor`

```python
@pytest.fixture
async def running_processor(fresh_appstate):
    processor = EventProcessor(initial_state=fresh_appstate, persist=False)
    task = asyncio.create_task(processor.run())
    yield processor
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
```

**Use when**: you need a running `EventProcessor` but don't need event capture.
Lighter than `harness` — no capturing reducer overhead.

---

### `mock_transport_factory`

```python
@pytest.fixture
def mock_transport_factory():
    return make_mock_transport
```

**Use when**: you want to build `MockTransport` instances pre-loaded with
responses in your test. Call `mock_transport_factory([...])` with a list of
responses.

---

## EventBusTestHarness

```python
class EventBusTestHarness:
    captured: list[Event]
    processor: EventProcessor

    def events_of_type(self, event_type: str) -> list[Event]
    async def wait_for_event(self, event_type: str, timeout: float = 2.0) -> Event
```

Wraps `EventProcessor` with a capturing reducer that appends every event to
`harness.captured` while still applying `root_reducer` normally.

### Example: asserting events were emitted

```python
async def test_intent_created_event(harness):
    from agenthicc.kernel import Event

    await harness.processor.emit(
        Event.create("IntentCreated", {"intent_id": "i-001", "raw_text": "write tests"})
    )
    await harness.processor.drain()

    # Assert the event was captured
    events = harness.events_of_type("IntentCreated")
    assert len(events) == 1
    assert events[0].payload["intent_id"] == "i-001"
    assert events[0].payload["raw_text"] == "write tests"
```

### Example: waiting for a specific event

```python
async def test_intent_status_event(harness):
    from agenthicc.kernel import Event

    await harness.processor.emit(
        Event.create("IntentCreated", {"intent_id": "i-002", "raw_text": "ship it"})
    )

    # wait_for_event polls until the event appears or timeout
    event = await harness.wait_for_event("IntentCreated", timeout=2.0)
    assert event.payload["intent_id"] == "i-002"
```

---

## drain() timing — the most common test bug

**Always call `await processor.drain()` before reading state after emitting events.**

The `EventProcessor` processes events asynchronously in its `run()` task. Emitting
puts an event on the queue but does not wait for it to be applied. Without `drain()`,
`get_state()` may return the pre-event state.

```python
# WRONG — race condition
async def test_intent_bad(running_processor):
    await running_processor.emit(Event.create("IntentCreated", {
        "intent_id": "i1", "raw_text": "hello",
    }))
    state = running_processor.get_state()           # may still be empty!
    assert "i1" in state.intents                    # flaky

# CORRECT
async def test_intent_good(running_processor):
    await running_processor.emit(Event.create("IntentCreated", {
        "intent_id": "i1", "raw_text": "hello",
    }))
    await running_processor.drain()                 # wait for processing
    state = running_processor.get_state()
    assert "i1" in state.intents                    # deterministic
```

`drain(timeout=5.0)` raises `asyncio.TimeoutError` if the queue doesn't empty
within 5 seconds — this surfaces deadlocks quickly rather than hanging forever.

---

## MockTransport pattern

Use `make_mock_transport` (from `conftest.py`) to build a `MockTransport` with
pre-loaded responses for `AgentRunnerBase` tests.

```python
from tests.conftest import make_mock_transport

def test_agent_runner_tool_call(mock_transport_factory):
    # Response 1: tool call
    # Response 2: end_turn after tool result
    transport = mock_transport_factory([
        {
            "tool_calls": [{"name": "application_log", "input": {
                "level": "INFO",
                "message": "starting task",
            }}],
            "content": "",
        },
        "Task complete",
    ])
    # Pass transport to AgentRunnerBase directly
    # ...
```

Each entry is either:
- A plain string → `Completion(stop_reason="end_turn", content=str)`.
- A dict with `"tool_calls"` → `Completion(stop_reason="tool_use", tool_calls=[...])`.

---

## Unit test patterns

### Testing root_reducer directly (no async)

```python
from agenthicc.kernel import AppState, Event, SecurityPolicy, SystemSettings, root_reducer

def test_intent_created_reducer():
    state = AppState.create()
    event = Event.create("IntentCreated", {
        "intent_id": "i1",
        "raw_text": "hello world",
    })
    new_state, effects = root_reducer(state, event)

    assert "i1" in new_state.intents
    assert new_state.intents["i1"].raw_text == "hello world"
    assert new_state.intents["i1"].status.value == "pending"
    assert effects == []  # IntentCreated has no side effects
```

### Testing TranscriptModel (pure, no async)

```python
from agenthicc.tui.transcript import TranscriptModel, ToolCallState

def test_tool_call_spinner():
    model = TranscriptModel()
    turn = model.append_turn("agent-1", "worker")
    entry = model.add_tool_call("agent-1", "tc-001", "file_write")

    assert entry.state is ToolCallState.RUNNING
    assert "running" in entry.render()

    model.advance_spinner()
    model.advance_spinner()
    assert entry.spinner_frame == 2

    model.update_tool_call("tc-001", state=ToolCallState.SUCCESS, duration_ms=42.0)
    assert "✓" in entry.render()
    assert "42ms" in entry.render()
```

---

## E2E test patterns

### API endpoints with TestClient

```python
import pytest
from fastapi.testclient import TestClient
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.api.server import create_app

@pytest.fixture
def api_client():
    proc = EventProcessor(AppState.create(), persist=False)
    app = create_app(proc, api_key=None)
    with TestClient(app) as client:
        yield client

def test_submit_and_poll(api_client):
    # Submit
    resp = api_client.post("/v1/intents", json={"text": "run linter"})
    assert resp.status_code == 200
    intent_id = resp.json()["intent_id"]

    # Poll
    resp = api_client.get(f"/v1/intents/{intent_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent_id"] == intent_id
    assert data["raw_text"] == "run linter"

def test_404_on_missing_intent(api_client):
    resp = api_client.get("/v1/intents/does-not-exist")
    assert resp.status_code == 404
```

---

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Not calling `drain()` after `emit()` | Flaky assertions on state | Always `await processor.drain()` before `get_state()` |
| Not starting the processor run task | `drain()` never returns | `asyncio.create_task(processor.run())` before `emit()` |
| Using `harness` for state reads | Extra overhead | Use `running_processor` for simple state assertions |
| Not cancelling the run task in teardown | Task leak warning | Fixtures handle this — use `harness` or `running_processor` |
| `MockTransport` runs out of responses | `IndexError` mid-test | Add more responses to the list — one per LLM completion cycle |
| `TestClient` without lifespan | Processor never starts | Use `with TestClient(app) as c:` to trigger lifespan |

---

## Key points

- `drain(timeout=5.0)` is the synchronisation primitive — always call it before
  reading state in async tests.
- `EventBusTestHarness.wait_for_event` polls at 5ms intervals up to `timeout` —
  use it instead of `asyncio.sleep` guesses.
- `running_processor` and `harness` fixtures cancel their run task in teardown —
  do not cancel it yourself.
- `mock_transport_factory` returns the `make_mock_transport` function — call it
  with a list, not as a bare fixture.
- `persist=False` in all test processors — never write to real `.agenthicc/` in tests.
- `TestClient` drives the FastAPI lifespan; use it as a context manager (`with`).
