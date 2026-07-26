"""Real loopback transport coverage for PRD-150."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agenthicc.session_service import (
    HttpSessionClient,
    LocalSessionServer,
    SessionCommand,
    SessionService,
)

pytestmark = pytest.mark.e2e


async def test_loopback_http_client_can_create_snapshot_command_and_replay(tmp_path) -> None:
    service = SessionService(store_root=tmp_path / "service")
    server = LocalSessionServer(service, port=0)
    base_url = await server.start()
    client = HttpSessionClient(base_url)
    try:
        async with httpx.AsyncClient() as raw:
            created = await raw.post(
                f"{base_url}/v1/sessions",
                json={"project_root": str(tmp_path), "agent": {"name": "build"}},
            )
            assert created.status_code == 201
            session_id = created.json()["snapshot"]["session_id"]

        snapshot = await client.snapshot(session_id)
        assert snapshot.session_id == session_id
        result = await client.submit(
            SessionCommand(
                kind="submit_message",
                session_id=session_id,
                idempotency_key="http-turn",
                payload={"text": "hello from http"},
            )
        )
        assert result.ok
        events = await client.events(session_id)
        assert [event.kind for event in events].count("turn_queued") == 1
        assert (await client.snapshot(session_id)).state.value == "running"
    finally:
        await client.close()
        await server.stop()
        await service.close()


async def test_non_loopback_transport_requires_explicit_authentication(tmp_path) -> None:
    service = SessionService(store_root=tmp_path / "service")
    with pytest.raises(ValueError, match="auth_token"):
        LocalSessionServer(service, host="0.0.0.0")
    await service.close()


async def test_authenticated_transport_rejects_missing_bearer_token(tmp_path) -> None:
    service = SessionService(store_root=tmp_path / "service")
    server = LocalSessionServer(service, port=0, auth_token="test-token")
    base_url = await server.start()
    try:
        async with httpx.AsyncClient() as raw:
            denied = await raw.get(f"{base_url}/health")
            allowed = await raw.get(
                f"{base_url}/health",
                headers={"Authorization": "Bearer test-token"},
            )
        assert denied.status_code == 401
        assert allowed.status_code == 200
    finally:
        await server.stop()
        await service.close()


async def test_http_sse_stream_receives_new_event(tmp_path) -> None:
    service = SessionService(store_root=tmp_path / "service")
    server = LocalSessionServer(service, port=0)
    base_url = await server.start()
    client = HttpSessionClient(base_url)
    stream = None
    try:
        snapshot = await client.create_session(project_root=str(tmp_path))
        stream = client.stream(snapshot.session_id, after_sequence=snapshot.last_event_sequence)
        pending = stream.__anext__()
        task = asyncio.create_task(pending)
        await asyncio.sleep(0.1)
        await service.publish(
            snapshot.session_id,
            source="test",
            kind="assistant_delta",
            payload={"text": "hi"},
        )
        event = await asyncio.wait_for(task, timeout=2)
        assert event.kind == "assistant_delta"
    finally:
        await server.stop()
        if stream is not None:
            await stream.aclose()
        await client.close()
        await service.close()
