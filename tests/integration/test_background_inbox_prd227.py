"""Client-neutral service and HTTP contract for PRD-227."""

from __future__ import annotations

import httpx
import pytest

from agenthicc.background.inbox import BackgroundInteractionInbox
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.store import BackgroundStore
from agenthicc.session_service import SessionError, SessionService
from agenthicc.session_service.transport import LocalSessionServer

pytestmark = pytest.mark.integration


def _pending_inbox(tmp_path):
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "service-inbox-session",
        title="service inbox",
        cwd=str(tmp_path / "workspace"),
        workflow_name="goal_flow",
        intent="private",
    ).evolve(status=SessionStatus.RUNNING, attempt=2, lease_token="lease-integration")
    store.create(session)
    inbox = BackgroundInteractionInbox(store)
    item = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id="tool-call-202",
        kind="question",
        target={
            "tool_name": "ask_user",
            "question_ids": ["runtime"],
            "questions": [{"id": "runtime", "text": "Which runtime?", "options": ["Py"]}],
        },
        expires_at=None,
    )
    store.transition(
        session.session_id,
        SessionStatus.WAITING_INPUT,
        expected_status=SessionStatus.RUNNING,
        input_item_id=item.item_id,
    )
    item = inbox.activate(item.item_id)
    return store, inbox, item


async def test_session_service_filters_scope_and_returns_stale_current_state(tmp_path) -> None:
    _store, inbox, item = _pending_inbox(tmp_path)
    service = SessionService(store_root=tmp_path / "session-service", background_inbox=inbox)
    items = await service.inbox_items(capabilities=frozenset({"read"}))
    assert items[0]["item_id"] == item.item_id
    assert items[0]["project_root"] == "<workspace>"
    assert items[0]["workspace"] == "<workspace>"
    with pytest.raises(SessionError) as denied:
        await service.respond_to_inbox(
            item.item_id,
            {"answers": {"runtime": "Py"}},
            expected_version=item.version,
            idempotency_key="read-only-client",
            capabilities=frozenset({"read"}),
        )
    assert denied.value.status == 403

    accepted = await service.respond_to_inbox(
        item.item_id,
        {"answers": {"runtime": "Py"}},
        expected_version=item.version,
        idempotency_key="control-client",
        capabilities=frozenset({"control"}),
    )
    assert accepted["item"]["status"] == "accepted"
    assert accepted["item"]["project_root"] == "<workspace>"
    assert accepted["item"]["workspace"] == "<workspace>"
    assert "response" not in accepted["item"]
    with pytest.raises(SessionError) as stale:
        await service.respond_to_inbox(
            item.item_id,
            {"answers": {"runtime": "Other"}},
            expected_version=item.version,
            idempotency_key="late-client",
            capabilities=frozenset({"control"}),
        )
    assert stale.value.status == 409
    assert stale.value.details["current"]["status"] == "accepted"
    assert stale.value.details["current"]["project_root"] == "<workspace>"
    await service.close()


async def test_http_inbox_routes_require_bearer_and_return_safe_receipts(tmp_path) -> None:
    _store, inbox, item = _pending_inbox(tmp_path)
    service = SessionService(store_root=tmp_path / "service", background_inbox=inbox)
    server = LocalSessionServer(service, auth_token="inbox-test-token")
    base_url = await server.start()
    try:
        async with httpx.AsyncClient() as client:
            unauthorized = await client.get(f"{base_url}/v1/inbox")
            assert unauthorized.status_code == 401
            headers = {"Authorization": "Bearer inbox-test-token"}
            listed = await client.get(f"{base_url}/v1/inbox?type=question", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["items"][0]["item_id"] == item.item_id
            answered = await client.post(
                f"{base_url}/v1/inbox/{item.item_id}/respond",
                headers=headers,
                json={
                    "expected_version": item.version,
                    "idempotency_key": "remote-http-answer",
                    "response": {"answers": {"runtime": "Py"}},
                },
            )
            assert answered.status_code == 200
            payload = answered.json()
            assert payload["item"]["status"] == "accepted"
            assert "response" not in payload["item"]
            conflict = await client.post(
                f"{base_url}/v1/inbox/{item.item_id}/respond",
                headers=headers,
                json={
                    "expected_version": item.version,
                    "idempotency_key": "remote-http-late",
                    "response": {"answers": {"runtime": "Other"}},
                },
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["details"]["current"]["status"] == "accepted"
    finally:
        await server.stop()
        await service.close()


async def test_service_expiry_uses_compare_and_set(tmp_path) -> None:
    _store, inbox, item = _pending_inbox(tmp_path)
    service = SessionService(store_root=tmp_path / "service", background_inbox=inbox)
    expired = await service.expire_inbox_item(
        item.item_id,
        expected_version=item.version,
        idempotency_key="expire-question",
        capabilities=frozenset({"control"}),
    )
    assert expired["status"] == "expired"
    with pytest.raises(SessionError) as conflict:
        await service.expire_inbox_item(
            item.item_id,
            expected_version=item.version,
            idempotency_key="expire-again",
            capabilities=frozenset({"control"}),
        )
    assert conflict.value.status == 409
    await service.close()
