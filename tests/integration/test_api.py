"""Integration tests for the headless FastAPI server (PRD-07)."""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agenthicc.kernel import AppState, SecurityPolicy, SystemSettings
from agenthicc.kernel.processor import EventProcessor
from agenthicc.api.server import create_app

pytestmark = pytest.mark.integration

@pytest.fixture
def proc(tmp_path):
    state = AppState.create(
        settings=SystemSettings(event_log_path=str(tmp_path/"ev.jsonl"), snapshot_path=str(tmp_path/"s.json")),
        policy=SecurityPolicy(),
    )
    return EventProcessor(initial_state=state, persist=False)

@pytest.fixture
def client(proc):
    app = create_app(proc)
    with TestClient(app) as c:
        yield c, proc

@pytest.fixture
def authed_client(proc):
    app = create_app(proc, api_key="secret-key")
    with TestClient(app) as c:
        yield c, proc

class TestIntentEndpoints:
    def test_post_intent_returns_intent_id(self, client):
        c, proc = client
        r = c.post("/v1/intents", json={"text": "refactor auth"})
        assert r.status_code == 200
        assert "intent_id" in r.json()

    def test_get_intent_returns_status(self, client):
        c, proc = client
        r1 = c.post("/v1/intents", json={"text": "fix bug"})
        intent_id = r1.json()["intent_id"]
        r2 = c.get(f"/v1/intents/{intent_id}")
        assert r2.status_code == 200
        data = r2.json()
        assert data["intent_id"] == intent_id
        assert "status" in data

    def test_get_unknown_intent_404(self, client):
        c, _ = client
        r = c.get("/v1/intents/nonexistent-xyz")
        assert r.status_code == 404

    def test_state_summary_returns_counts(self, client):
        c, proc = client
        c.post("/v1/intents", json={"text": "first intent"})
        r = c.get("/v1/state/summary")
        assert r.status_code == 200
        assert "intents" in r.json()
        assert r.json()["intents"] >= 1

    def test_multiple_intents_accumulate(self, client):
        c, _ = client
        for i in range(3):
            c.post("/v1/intents", json={"text": f"intent {i}"})
        r = c.get("/v1/state/summary")
        assert r.json()["intents"] >= 3

class TestAuthentication:
    def test_no_bearer_returns_401(self, authed_client):
        c, _ = authed_client
        r = c.post("/v1/intents", json={"text": "x"})
        assert r.status_code == 401

    def test_wrong_bearer_returns_401(self, authed_client):
        c, _ = authed_client
        r = c.post("/v1/intents", json={"text": "x"}, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_correct_bearer_returns_200(self, authed_client):
        c, _ = authed_client
        r = c.post("/v1/intents", json={"text": "x"}, headers={"Authorization": "Bearer secret-key"})
        assert r.status_code == 200

    def test_summary_requires_auth(self, authed_client):
        c, _ = authed_client
        r = c.get("/v1/state/summary")
        assert r.status_code == 401
        r2 = c.get("/v1/state/summary", headers={"Authorization": "Bearer secret-key"})
        assert r2.status_code == 200
