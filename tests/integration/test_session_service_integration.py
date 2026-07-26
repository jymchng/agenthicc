"""Multi-client and restart integration tests for PRD-150."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.session_service import (
    InProcessSessionClient,
    SessionCommand,
    SessionService,
    SessionState,
)

pytestmark = pytest.mark.integration


async def test_two_clients_share_one_turn_projection_and_reconnect(tmp_path) -> None:
    service = SessionService(store_root=tmp_path / "service")
    snapshot = await service.create_session(
        project_root=tmp_path,
        capabilities=frozenset({"read", "control", "workspace"}),
    )
    tui = InProcessSessionClient(service, client_id="tui")
    ide = InProcessSessionClient(service, client_id="ide")
    tui_events = await tui.subscribe(snapshot.session_id)
    ide_events = await ide.subscribe(snapshot.session_id)

    command = SessionCommand(
        kind="submit_message",
        session_id=snapshot.session_id,
        idempotency_key="shared-turn",
        payload={"text": "same session"},
        capabilities=frozenset({"read", "control"}),
    )
    result = await tui.submit(command)
    assert result.ok
    assert (await ide.snapshot(snapshot.session_id)).state is SessionState.RUNNING
    assert (await tui.snapshot(snapshot.session_id)).active_turn_id == result.data["turn_id"]

    tui_replay = await tui.events(snapshot.session_id)
    ide_replay = await ide.events(snapshot.session_id)
    assert [event.event_id for event in tui_replay] == [event.event_id for event in ide_replay]
    assert [event.kind for event in tui_replay].count("turn_queued") == 1
    assert [event.kind for event in tui_replay].count("command_accepted") == 1

    await tui_events.close()
    await ide_events.close()
    restarted = SessionService(store_root=tmp_path / "service")
    resumed = await restarted.snapshot(snapshot.session_id)
    assert resumed.active_turn_id == result.data["turn_id"]
    assert resumed.last_event_sequence == (await restarted.events(snapshot.session_id))[-1].sequence
    await service.close()
    await restarted.close()


async def test_handler_failure_is_projected_without_leaking_to_other_sessions(tmp_path) -> None:
    service = SessionService(store_root=tmp_path / "service")
    first = await service.create_session(capabilities=frozenset({"read", "control"}))
    second = await service.create_session(capabilities=frozenset({"read", "control"}))

    async def failing_handler(
        command: SessionCommand, turn_id: str, coordinator: SessionService
    ) -> None:
        del command, turn_id, coordinator
        raise RuntimeError("controlled failure")

    service.register_turn_handler(first.session_id, failing_handler)
    await service.submit(
        SessionCommand(
            kind="submit_message",
            session_id=first.session_id,
            payload={"text": "fail"},
            capabilities=frozenset({"read", "control"}),
        )
    )
    for _ in range(20):
        if (await service.snapshot(first.session_id)).state is SessionState.FAILED:
            break
        await asyncio.sleep(0.01)
    assert (await service.snapshot(first.session_id)).state is SessionState.FAILED
    assert (await service.snapshot(second.session_id)).state is SessionState.IDLE
    failure = [
        event for event in await service.events(first.session_id) if event.kind == "turn_failed"
    ]
    assert failure and failure[0].payload["error"] == "controlled failure"
    await service.close()
