---
skill: headless-api
version: 1.0.0
tags: [api, rest, websocket, fastapi, headless]
summary: Guide to the Agenthicc headless REST and WebSocket API — create_app, all endpoints, auth, Python client examples.
---

# Skill: Headless API

## When to use this skill

Use this skill when you need to:
- Control Agenthicc from a remote process, script, or CI pipeline without a TUI
- Submit intents and poll their status via HTTP
- Stream live kernel state changes via WebSocket
- Add API key authentication to the server
- Write a Python HTTP or WebSocket client against the API

---

## create_app

```python
from agenthicc.api.server import create_app
from agenthicc.kernel import AppState, EventProcessor

processor = EventProcessor(
    initial_state=AppState.create(),
    persist=True,       # write events to .agenthicc/events.jsonl
)

# No auth (open to localhost only)
app = create_app(processor)

# With Bearer auth
app = create_app(processor, api_key="my-secret-key")
```

The `FastAPI` lifespan starts `processor.run()` at startup and calls
`processor.stop()` at shutdown. Use `uvicorn.run(app)` or `fastapi.testclient.TestClient(app)`
for tests (TestClient drives the lifespan).

---

## Running the server

```python
# server.py
import uvicorn
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.api.server import create_app
from agenthicc.config import load_config

cfg = load_config()
processor = EventProcessor(
    initial_state=AppState.create(
        settings=cfg.to_system_settings(),
        policy=cfg.to_security_policy(),
    ),
    persist=True,
)
app = create_app(processor, api_key="secret")

if __name__ == "__main__":
    uvicorn.run(app, host=cfg.api.host, port=cfg.api.port)
```

```bash
python server.py
# or
uvicorn server:app --host 127.0.0.1 --port 8000
```

---

## Endpoints

### POST /v1/intents

Submit a new intent. The kernel emits an `IntentCreated` event and returns the
new `intent_id` immediately.

**Request body**: `{"text": "<intent text>"}`

**Response** (200):

```json
{"intent_id": "a1b2c3d4e5f6..."}
```

**Auth**: required when `api_key` is set.

**curl example**:

```bash
curl -X POST http://localhost:8000/v1/intents \
  -H "Authorization: Bearer my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"text": "refactor the auth module to use JWT"}'
```

**Python example**:

```python
import httpx

resp = httpx.post(
    "http://localhost:8000/v1/intents",
    headers={"Authorization": "Bearer my-secret-key"},
    json={"text": "refactor the auth module to use JWT"},
)
resp.raise_for_status()
intent_id = resp.json()["intent_id"]
print("Intent:", intent_id)
```

---

### GET /v1/intents/{intent_id}

Poll the status of a previously submitted intent.

**Response** (200):

```json
{
  "intent_id": "a1b2c3d4e5f6",
  "status": "running",
  "raw_text": "refactor the auth module to use JWT",
  "workflow_id": "wf-abc123",
  "created_at": 1735689600.123,
  "error": null
}
```

`status` values: `pending`, `validating`, `planning`, `running`, `complete`,
`failed`, `rejected`.

**Response** (404):

```json
{"detail": "intent_not_found"}
```

**curl example**:

```bash
curl http://localhost:8000/v1/intents/a1b2c3d4e5f6 \
  -H "Authorization: Bearer my-secret-key"
```

**Python polling example**:

```python
import time
import httpx

def poll_intent(intent_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = httpx.get(
            f"http://localhost:8000/v1/intents/{intent_id}",
            headers={"Authorization": "Bearer my-secret-key"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] in ("complete", "failed", "rejected"):
            return data
        time.sleep(1.0)
    raise TimeoutError(f"Intent {intent_id} did not complete within {timeout}s")

result = poll_intent(intent_id)
print("Final status:", result["status"])
```

---

### GET /v1/state/summary

Get a lightweight summary of current kernel state. Does not block or drain;
reflects the most recently applied event.

**Response** (200):

```json
{
  "intents": 3,
  "workflows": 2,
  "agents": 5,
  "last_event": "TaskStatusUpdated"
}
```

`last_event` is `null` if no events have been processed yet.

**curl example**:

```bash
curl http://localhost:8000/v1/state/summary \
  -H "Authorization: Bearer my-secret-key"
```

**Python example**:

```python
summary = httpx.get(
    "http://localhost:8000/v1/state/summary",
    headers={"Authorization": "Bearer my-secret-key"},
).json()
print(f"{summary['agents']} agents running, last event: {summary['last_event']}")
```

---

### WS /v1/ws

WebSocket stream of state summaries. The server pushes one summary message after
each kernel event is applied. The message format is identical to the
`GET /v1/state/summary` response.

**Auth**: checked via the `Authorization` header at connect time. Connection is
closed with code 1008 on mismatch.

**curl example** (requires `websocat`):

```bash
websocat ws://localhost:8000/v1/ws \
  -H "Authorization: Bearer my-secret-key"
```

**Python example** (requires `websockets`):

```python
import asyncio
import json
import websockets

async def stream_state():
    uri = "ws://localhost:8000/v1/ws"
    headers = {"Authorization": "Bearer my-secret-key"}
    async with websockets.connect(uri, additional_headers=headers) as ws:
        async for message in ws:
            summary = json.loads(message)
            print(
                f"agents={summary['agents']}  "
                f"intents={summary['intents']}  "
                f"last={summary['last_event']}"
            )

asyncio.run(stream_state())
```

**asyncio client with cancellation**:

```python
import asyncio
import json
import websockets

async def watch_until_done(intent_id: str) -> None:
    uri = "ws://localhost:8000/v1/ws"
    headers = {"Authorization": "Bearer my-secret-key"}
    async with websockets.connect(uri, additional_headers=headers) as ws:
        async for raw in ws:
            summary = json.loads(raw)
            if summary.get("last_event") in ("IntentCompleted", "IntentFailed"):
                print("Intent finished, last event:", summary["last_event"])
                break

asyncio.run(watch_until_done("a1b2c3d4e5f6"))
```

---

## Authentication

When `api_key` is set in `create_app`:

- HTTP endpoints require: `Authorization: Bearer <api_key>`
- WebSocket requires: the same header passed at connect time

On failure:
- HTTP → `401 {"detail": "unauthorized"}`
- WebSocket → connection closed with code `1008` before `accept()`

To disable auth (development only):

```python
app = create_app(processor, api_key=None)
```

Or use the config:

```toml
# agenthicc.toml
[api]
api_key_env = "AGENTHICC_API_KEY"  # reads key from this env var at startup
```

---

## Error codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 401 | Missing or incorrect `Authorization` header |
| 404 | Intent ID not found |
| 422 | Invalid request body (Pydantic validation) |
| 1008 | WebSocket auth failure (policy violation) |

---

## Testing with TestClient

`fastapi.testclient.TestClient` drives the lifespan so the `EventProcessor.run()`
task starts automatically:

```python
import pytest
from fastapi.testclient import TestClient
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.api.server import create_app

@pytest.fixture
def client():
    proc = EventProcessor(AppState.create(), persist=False)
    app = create_app(proc, api_key=None)
    with TestClient(app) as c:
        yield c

def test_submit_intent(client):
    resp = client.post("/v1/intents", json={"text": "run tests"})
    assert resp.status_code == 200
    assert "intent_id" in resp.json()

def test_get_intent(client):
    intent_id = client.post("/v1/intents", json={"text": "run tests"}).json()["intent_id"]
    resp = client.get(f"/v1/intents/{intent_id}")
    assert resp.status_code == 200
    assert resp.json()["raw_text"] == "run tests"

def test_state_summary(client):
    resp = client.get("/v1/state/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "intents" in data
    assert "agents" in data

def test_auth_required():
    proc = EventProcessor(AppState.create(), persist=False)
    app = create_app(proc, api_key="secret")
    with TestClient(app) as c:
        resp = c.post("/v1/intents", json={"text": "hi"})
        assert resp.status_code == 401
        resp = c.post(
            "/v1/intents",
            json={"text": "hi"},
            headers={"Authorization": "Bearer secret"},
        )
        assert resp.status_code == 200
```

---

## Key points

- `create_app(processor)` returns a standard `FastAPI` app — deploy with any ASGI server.
- The lifespan starts `processor.run()` automatically; **do not** start it separately.
- `GET /v1/intents/{id}` reads current state without draining; the status may lag
  briefly after a burst of events.
- `WS /v1/ws` pushes after every event — high-frequency workloads may saturate
  slow clients; the queue is capped at `maxsize=100` per subscriber (drops silently).
- `api_key=None` disables auth entirely — only use in trusted local environments.
- `401` from HTTP, code `1008` from WebSocket — both indicate auth failure.
