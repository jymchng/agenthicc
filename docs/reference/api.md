# API Reference

Agenthicc exposes a headless REST and WebSocket API via `create_app`. This page
documents every endpoint with request/response schemas, error codes, and examples.

---

## Server setup

```python
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.api.server import create_app
import uvicorn, os

proc = EventProcessor(AppState.create(), persist=True)
app = create_app(proc, api_key=os.environ.get("AGENTHICC_API_KEY"))
uvicorn.run(app, host="127.0.0.1", port=8000)
```

The FastAPI lifespan starts `proc.run()` at startup and stops it at shutdown.

---

## Authentication

When `api_key` is provided to `create_app`:

- **HTTP**: every endpoint requires `Authorization: Bearer <api_key>`.
- **WebSocket**: the `Authorization` header is checked at connect time before the
  handshake is accepted.

Failure responses:

| Transport | Failure | Code |
|---|---|---|
| HTTP | Missing or wrong key | `401 {"detail": "unauthorized"}` |
| WebSocket | Missing or wrong key | Connection closed with code `1008` |

To disable auth (local development only):

```python
app = create_app(proc, api_key=None)
```

---

## POST /v1/intents

Submit a new intent. Returns immediately with a fresh `intent_id`; processing
is asynchronous.

**Method**: `POST`
**Path**: `/v1/intents`
**Auth**: required when `api_key` is set

### Request body

```json
{
  "text": "refactor the auth module to use JWT"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes | The raw intent text |

### Response (200 OK)

```json
{
  "intent_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
}
```

### Error responses

| Code | Body | Cause |
|---|---|---|
| `401` | `{"detail": "unauthorized"}` | Missing/wrong Authorization header |
| `422` | Pydantic error detail | Missing or invalid `text` field |

### curl example

```bash
curl -X POST http://localhost:8000/v1/intents \
  -H "Authorization: Bearer my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"text": "refactor the auth module to use JWT"}'
```

### Python example

```python
import httpx

resp = httpx.post(
    "http://localhost:8000/v1/intents",
    headers={"Authorization": "Bearer my-secret-key"},
    json={"text": "refactor the auth module to use JWT"},
)
resp.raise_for_status()
intent_id = resp.json()["intent_id"]
```

---

## GET /v1/intents/{intent_id}

Poll the current status and metadata of an intent.

**Method**: `GET`
**Path**: `/v1/intents/{intent_id}`
**Auth**: required when `api_key` is set

### Path parameters

| Parameter | Type | Description |
|---|---|---|
| `intent_id` | string | The `intent_id` returned by `POST /v1/intents` |

### Response (200 OK)

```json
{
  "intent_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "status": "running",
  "raw_text": "refactor the auth module to use JWT",
  "workflow_id": "wfabc123def456abc123def456abc123",
  "created_at": 1735689600.123,
  "error": null
}
```

| Field | Type | Description |
|---|---|---|
| `intent_id` | string | Same as path parameter |
| `status` | string | One of: `pending`, `validating`, `planning`, `running`, `complete`, `failed`, `rejected` |
| `raw_text` | string | Original submitted text |
| `workflow_id` | string or null | Associated workflow ID (null until planning completes) |
| `created_at` | float | Unix timestamp of creation |
| `error` | string or null | Error message if status is `failed` or `rejected` |

### Error responses

| Code | Body | Cause |
|---|---|---|
| `401` | `{"detail": "unauthorized"}` | Missing/wrong Authorization header |
| `404` | `{"detail": "intent_not_found"}` | No intent with this ID in current state |

### curl example

```bash
curl http://localhost:8000/v1/intents/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 \
  -H "Authorization: Bearer my-secret-key"
```

### Python polling example

```python
import time, httpx

def wait_for_completion(intent_id: str, timeout: float = 120.0) -> dict:
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
        time.sleep(2.0)
    raise TimeoutError(f"Intent {intent_id} did not complete in {timeout}s")
```

---

## GET /v1/state/summary

Return a lightweight summary of current kernel state. Non-blocking — reflects the
most recently applied event without draining the queue.

**Method**: `GET`
**Path**: `/v1/state/summary`
**Auth**: required when `api_key` is set

### Response (200 OK)

```json
{
  "intents": 3,
  "workflows": 2,
  "agents": 5,
  "last_event": "TaskStatusUpdated"
}
```

| Field | Type | Description |
|---|---|---|
| `intents` | int | Total number of intents in the current state |
| `workflows` | int | Total number of workflows |
| `agents` | int | Total number of registered agents |
| `last_event` | string or null | `event_type` of the most recently processed event; null if no events yet |

### Error responses

| Code | Body | Cause |
|---|---|---|
| `401` | `{"detail": "unauthorized"}` | Missing/wrong Authorization header |

### curl example

```bash
curl http://localhost:8000/v1/state/summary \
  -H "Authorization: Bearer my-secret-key"
```

### Python example

```python
summary = httpx.get(
    "http://localhost:8000/v1/state/summary",
    headers={"Authorization": "Bearer my-secret-key"},
).json()
print(f"{summary['agents']} agents | last: {summary['last_event']}")
```

---

## WS /v1/ws

WebSocket stream of state summaries. The server pushes one JSON message after
each kernel event is applied. The message format is identical to the
`GET /v1/state/summary` response body.

**Method**: WebSocket upgrade (`GET`)
**Path**: `/v1/ws`
**Auth**: `Authorization: Bearer <api_key>` header at connect time

### Message format (server → client)

Each message is a JSON object with the same schema as `GET /v1/state/summary`:

```json
{
  "intents": 3,
  "workflows": 2,
  "agents": 5,
  "last_event": "TaskStatusUpdated"
}
```

Messages are sent non-blocking. If a client's receive buffer is full
(`maxsize=100`), messages are silently dropped. High-frequency workloads should
use `GET /v1/state/summary` for polling instead.

### Connection close codes

| Code | Meaning |
|---|---|
| `1008` | Auth failure (policy violation) — wrong or missing `Authorization` |
| `1000` | Normal close (client disconnected) |

### curl example (requires websocat)

```bash
websocat ws://localhost:8000/v1/ws \
  -H "Authorization: Bearer my-secret-key"
```

### Python example (requires websockets)

```python
import asyncio, json, websockets

async def stream():
    uri = "ws://localhost:8000/v1/ws"
    extra = {"Authorization": "Bearer my-secret-key"}
    async with websockets.connect(uri, additional_headers=extra) as ws:
        async for message in ws:
            data = json.loads(message)
            print(data["agents"], "agents |", data["last_event"])

asyncio.run(stream())
```

### Python example: wait until intent completes

```python
import asyncio, json, websockets

async def wait_via_ws(intent_id: str) -> None:
    uri = "ws://localhost:8000/v1/ws"
    headers = {"Authorization": "Bearer my-secret-key"}
    async with websockets.connect(uri, additional_headers=headers) as ws:
        async for raw in ws:
            summary = json.loads(raw)
            # Check the REST endpoint for this specific intent
            if summary.get("last_event") in (
                "IntentCompleted", "IntentFailed", "IntentStatusUpdated"
            ):
                break   # check intent status separately
    print("Intent may be done — poll GET /v1/intents/<id> to confirm")

asyncio.run(wait_via_ws("a1b2c3d4e5f6"))
```

---

## Testing with TestClient

`fastapi.testclient.TestClient` drives the FastAPI lifespan (starts the processor):

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

def test_full_workflow(client):
    # Submit
    resp = client.post("/v1/intents", json={"text": "run tests"})
    assert resp.status_code == 200
    intent_id = resp.json()["intent_id"]

    # Summary
    summary = client.get("/v1/state/summary").json()
    assert summary["intents"] >= 1

    # Poll
    resp = client.get(f"/v1/intents/{intent_id}")
    assert resp.status_code == 200
    assert resp.json()["raw_text"] == "run tests"

def test_404(client):
    assert client.get("/v1/intents/missing").status_code == 404

def test_auth_blocks_without_key():
    proc = EventProcessor(AppState.create(), persist=False)
    app = create_app(proc, api_key="secret")
    with TestClient(app) as c:
        assert c.post("/v1/intents", json={"text": "hi"}).status_code == 401
        assert c.post(
            "/v1/intents",
            json={"text": "hi"},
            headers={"Authorization": "Bearer secret"},
        ).status_code == 200
```
