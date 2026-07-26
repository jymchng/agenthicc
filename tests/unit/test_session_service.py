"""Unit coverage for the client-neutral session service (PRD-150)."""

from __future__ import annotations

import asyncio
import json

import pytest

from agenthicc.kernel import Event
from agenthicc.session_service import (
    EventDurability,
    SessionCommand,
    SessionError,
    SessionEvent,
    SessionEventStore,
    SessionService,
    SessionState,
)

pytestmark = pytest.mark.unit


async def _service(tmp_path) -> SessionService:
    return SessionService(store_root=tmp_path / "session-service")


async def test_session_contract_round_trips_and_redacts(tmp_path) -> None:
    service = await _service(tmp_path)
    snapshot = await service.create_session(
        project_root=tmp_path,
        agent={"name": "build", "api_key": "do-not-project"},
        workflow={"name": "code_plan"},
        capabilities=frozenset({"read", "control", "workspace"}),
    )

    assert snapshot.state is SessionState.IDLE
    assert snapshot.project_root == str(tmp_path.resolve())
    assert snapshot.agent["api_key"] == "<redacted>"

    event = await service.publish(
        snapshot.session_id,
        source="test",
        kind="tool_result",
        payload={"token": "secret", "nested": {"password": "hidden", "ok": True}},
        durability=EventDurability.DURABLE,
    )
    projected = (await service.events(snapshot.session_id))[1]
    assert event.event_id == projected.event_id
    assert projected.payload == {
        "token": "<redacted>",
        "nested": {"password": "<redacted>", "ok": True},
    }

    restored = SessionService(store_root=tmp_path / "session-service")
    restored_snapshot = await restored.snapshot(
        snapshot.session_id, capabilities=frozenset({"read"})
    )
    assert restored_snapshot.project_root == "<workspace>"
    assert restored_snapshot.agent["api_key"] == "<redacted>"
    assert SessionEvent.from_mapping(event.to_dict()) == event
    await service.close()
    await restored.close()


async def test_kernel_event_projection_uses_neutral_snake_case(tmp_path) -> None:
    service = await _service(tmp_path)
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    projected = await service.publish_kernel_event(
        snapshot.session_id,
        Event.create("WorkflowPhaseCompleted", {"phase_name": "plan"}),
    )
    assert projected.source == "kernel"
    assert projected.kind == "workflow_phase_completed"
    assert projected.payload["phase_name"] == "plan"
    await service.close()


async def test_idempotency_stale_sequence_and_replay_gap(tmp_path) -> None:
    service = await _service(tmp_path)
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    command = SessionCommand(
        kind="submit_message",
        session_id=snapshot.session_id,
        client_id="test",
        idempotency_key="turn-1",
        payload={"text": "Run tests"},
        capabilities=frozenset({"read", "control"}),
    )
    first = await service.submit(command)
    duplicate = await service.submit(command)
    assert first.data["turn_id"] == duplicate.data["turn_id"]
    assert duplicate.replayed is True

    with pytest.raises(SessionError, match="current is") as stale:
        await service.submit(
            SessionCommand(
                kind="archive",
                session_id=snapshot.session_id,
                expected_sequence=1,
                idempotency_key="archive-1",
                capabilities=frozenset({"read", "control"}),
            )
        )
    assert stale.value.code == "stale_sequence"

    earliest = await service.compact(snapshot.session_id, before_sequence=3)
    assert earliest == 3
    with pytest.raises(SessionError) as gap:
        await service.events(snapshot.session_id, after_sequence=0)
    assert gap.value.code == "replay_gap"
    assert gap.value.status == 409
    await service.close()


async def test_subscription_replay_ephemeral_events_and_backpressure(tmp_path) -> None:
    service = await _service(tmp_path)
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    await service.publish(
        snapshot.session_id,
        source="test",
        kind="status_tick",
        durability=EventDurability.EPHEMERAL,
    )
    replay = await service.subscribe(snapshot.session_id, max_queue=8)
    assert (await replay.__anext__()).kind == "session_created"
    await replay.close()

    slow = await service.subscribe(snapshot.session_id, max_queue=8)
    assert (await slow.__anext__()).kind == "session_created"
    for index in range(9):
        await service.publish(snapshot.session_id, source="test", kind=f"event_{index}")
    with pytest.raises(SessionError) as overflow:
        await slow.__anext__()
    assert overflow.value.code == "backpressure"
    await slow.close()
    await service.close()


async def test_registered_turn_handler_lifecycle_and_cancellation(tmp_path) -> None:
    service = await _service(tmp_path)
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(command: SessionCommand, turn_id: str, coordinator: SessionService) -> None:
        del command, turn_id, coordinator
        started.set()
        await release.wait()

    service.register_turn_handler(snapshot.session_id, handler)
    submit = await service.submit(
        SessionCommand(
            kind="submit_message",
            session_id=snapshot.session_id,
            payload={"text": "wait"},
            capabilities=frozenset({"read", "control"}),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (await service.snapshot(snapshot.session_id)).state is SessionState.RUNNING
    await service.submit(
        SessionCommand(
            kind="cancel",
            session_id=snapshot.session_id,
            payload={"turn_id": submit.data["turn_id"]},
            capabilities=frozenset({"read", "control"}),
        )
    )
    await asyncio.sleep(0)
    assert (await service.snapshot(snapshot.session_id)).state is SessionState.CANCELLED
    await service.close()


async def test_fork_isolated_and_capabilities_are_enforced(tmp_path) -> None:
    service = await _service(tmp_path)
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    result = await service.submit(
        SessionCommand(
            kind="fork",
            session_id=snapshot.session_id,
            capabilities=frozenset({"read", "control"}),
        )
    )
    child_id = result.data["session_id"]
    assert isinstance(child_id, str)
    child = await service.snapshot(child_id)
    assert child.parent_session_id == snapshot.session_id
    assert child.session_id != snapshot.session_id

    with pytest.raises(SessionError) as forbidden:
        await service.snapshot(snapshot.session_id, capabilities=frozenset())
    assert forbidden.value.code == "forbidden"
    await service.close()


async def test_delete_is_a_durable_tombstone_with_replayable_retry(tmp_path) -> None:
    service = await _service(tmp_path)
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    delete = SessionCommand(
        kind="delete",
        session_id=snapshot.session_id,
        idempotency_key="delete-once",
        capabilities=frozenset({"read", "control"}),
    )
    result = await service.submit(delete)
    assert result.data["deleted"] is True
    with pytest.raises(SessionError) as missing:
        await service.snapshot(snapshot.session_id)
    assert missing.value.code == "not_found"

    restarted = SessionService(store_root=tmp_path / "session-service")
    replayed = await restarted.submit(delete)
    assert replayed.replayed is True
    assert replayed.data["deleted"] is True
    await service.close()
    await restarted.close()


def test_store_rejects_path_traversal_and_compacts(tmp_path) -> None:
    store = SessionEventStore(tmp_path / "store")
    with pytest.raises(ValueError):
        store.path_for("../outside")
    event = SessionEvent.create(session_id="sess_test", sequence=1, source="test", kind="one")
    later = SessionEvent.create(session_id="sess_test", sequence=2, source="test", kind="two")
    store.append_many([event, later])
    assert store.compact("sess_test", before_sequence=2) == 2
    assert [item.sequence for item in store.all_events("sess_test")] == [2]


async def test_legacy_kernel_log_is_imported_once(tmp_path) -> None:
    from agenthicc.kernel import Event

    service = await _service(tmp_path)
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    log_path = tmp_path / "legacy.jsonl"
    log_path.write_text(
        json.dumps(
            Event.create("IntentCreated", {"intent_id": "legacy", "raw_text": "hello"}).to_dict()
        )
        + "\n",
        encoding="utf-8",
    )
    assert await service.import_kernel_log(snapshot.session_id, log_path) == 1
    assert await service.import_kernel_log(snapshot.session_id, log_path) == 0
    assert [event.kind for event in await service.events(snapshot.session_id)].count(
        "intent_created"
    ) == 1
    await service.close()
