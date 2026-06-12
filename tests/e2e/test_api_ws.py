"""E2E: WebSocket event stream from headless API (PRD-07)."""
from __future__ import annotations
import json
import pytest
from starlette.websockets import WebSocketDisconnect
from fastapi.testclient import TestClient
from agenthicc.kernel import AppState, SecurityPolicy, SystemSettings
from agenthicc.kernel.processor import EventProcessor
from agenthicc.api.server import create_app

pytestmark = pytest.mark.e2e

@pytest.fixture
def proc(tmp_path):
    state = AppState.create(
        settings=SystemSettings(event_log_path=str(tmp_path/"ev.jsonl"), snapshot_path=str(tmp_path/"s.json")),
        policy=SecurityPolicy(),
    )
    return EventProcessor(initial_state=state, persist=False)

def test_websocket_receives_state_on_intent_submit(proc):
    app = create_app(proc)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/ws") as ws:
            r = client.post("/v1/intents", json={"text": "ws test intent"})
            assert r.status_code == 200
            msg = ws.receive_text()
            data = json.loads(msg)
            assert "intents" in data
            assert data["intents"] >= 1

def test_websocket_reflects_multiple_intents(proc):
    app = create_app(proc)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/ws") as ws:
            for text in ["intent-A", "intent-B", "intent-C"]:
                client.post("/v1/intents", json={"text": text})
            for _ in range(10):
                msg = ws.receive_text()
                data = json.loads(msg)
                if data["intents"] >= 3:
                    break
            else:
                pytest.fail("Never saw 3 intents in WebSocket messages")

def test_websocket_api_key_rejected_without_bearer(proc):
    app = create_app(proc, api_key="ws-secret")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/ws"):
                pass

def test_websocket_api_key_accepted_with_bearer(proc):
    app = create_app(proc, api_key="ws-secret")
    with TestClient(app) as client:
        with client.websocket_connect("/v1/ws", headers={"authorization": "Bearer ws-secret"}) as ws:
            client.post("/v1/intents", json={"text": "authed intent"}, headers={"Authorization": "Bearer ws-secret"})
            msg = ws.receive_text()
            data = json.loads(msg)
            assert "intents" in data
