"""Additional command and projection coverage for the session service."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agenthicc.session_service import (
    SessionCommand,
    SessionError,
    SessionService,
    SessionState,
)

pytestmark = pytest.mark.unit


async def test_session_command_matrix_projection_and_adoption(tmp_path: Path) -> None:
    service = SessionService(store_root=tmp_path / "service")
    caps = frozenset({"read", "control", "workspace", "private"})
    snapshot = await service.create_session(
        project_root=tmp_path,
        capabilities=caps,
        agent={"name": "builder", "secret": "hidden"},
        workflow={"name": "code_plan", "secret": "hidden"},
    )
    adopted = await service.ensure_session("sess-adopted", project_root=tmp_path, capabilities=caps)
    assert adopted.session_id == "sess-adopted"
    assert (
        await service.ensure_session("sess-adopted", capabilities=caps)
    ).session_id == "sess-adopted"
    assert len(await service.list_sessions(project_root=tmp_path, capabilities=caps)) == 2
    assert (
        len(await service.list_sessions(project_root=tmp_path / "elsewhere", capabilities=caps))
        == 0
    )

    async def submit(kind: str, *, payload: dict[str, object] | None = None) -> object:
        return await service.submit(
            SessionCommand(
                kind=kind,
                session_id=snapshot.session_id,
                payload=payload or {},
                capabilities=caps,
            )
        )

    attached = await submit("attach")
    assert attached.data["snapshot"]["session_id"] == snapshot.session_id
    await service.publish(
        snapshot.session_id,
        source="test",
        kind="workflow_phase_changed",
        payload={"phase": "plan", "secret": "not-projected"},
    )
    await service.publish(
        snapshot.session_id,
        source="test",
        kind="agent_changed",
        payload={"name": "reviewer", "secret": "not-projected"},
    )
    await service.publish(
        snapshot.session_id, source="test", kind="job_changed", payload={"running_count": 2}
    )
    await service.publish(
        snapshot.session_id, source="test", kind="terminal_changed", payload={"running_count": 3}
    )
    await service.publish(snapshot.session_id, source="test", kind="waiting_approval")
    await service.publish(snapshot.session_id, source="test", kind="waiting_question")
    projected = await service.snapshot(snapshot.session_id, capabilities=caps)
    assert projected.state is SessionState.WAITING_QUESTION
    assert projected.workflow["phase"] == "plan"
    assert projected.workflow["secret"] == "hidden"
    assert projected.background_jobs_running == 2
    assert projected.terminals_running == 3

    assert (await submit("approve", payload={"answer": "yes"})).data["resolved"] is True
    assert (await submit("reject", payload={"answer": "no"})).data["resolved"] is True
    assert (await submit("answer", payload={"answer": "42"})).data["resolved"] is True
    assert (await submit("resume")).data["state"] == SessionState.RUNNING.value
    assert (await submit("retry")).data["state"] == SessionState.RUNNING.value
    assert (await submit("invoke_command", payload={"name": "status"})).data["accepted"] is True
    forked = await submit("fork")
    child_id = forked.data["session_id"]
    assert isinstance(child_id, str)
    assert (
        await service.snapshot(child_id, capabilities=caps)
    ).parent_session_id == snapshot.session_id

    public = await service.snapshot(
        snapshot.session_id, capabilities=frozenset({"read", "control"})
    )
    assert public.project_root == "<workspace>"
    assert public.agent["secret"] == "<redacted>"
    await service.publish(
        snapshot.session_id,
        source="test",
        kind="private_event",
        payload={"value": "x"},
        visibility="private",
    )
    with pytest.raises(SessionError, match="private"):
        await service.events(snapshot.session_id, capabilities=frozenset({"read", "control"}))
    await service.close()


async def test_session_service_rejects_invalid_commands_and_subscription_close(
    tmp_path: Path,
) -> None:
    service = SessionService(store_root=tmp_path / "service")
    snapshot = await service.create_session(capabilities=frozenset({"read", "control"}))
    with pytest.raises(SessionError, match="non-empty text"):
        await service.submit(
            SessionCommand(
                kind="submit_message",
                session_id=snapshot.session_id,
                payload={"text": " "},
                capabilities=frozenset({"read", "control"}),
            )
        )
    with pytest.raises(SessionError, match="unknown session command"):
        await service.submit(
            SessionCommand(
                kind="unknown",
                session_id=snapshot.session_id,
                capabilities=frozenset({"read", "control"}),
            )
        )
    await service.publish(
        snapshot.session_id,
        source="test",
        kind="private_event",
        payload={"value": "x"},
        visibility="private",
    )
    with pytest.raises(SessionError, match="private"):
        await service.events(snapshot.session_id, capabilities=frozenset({"read", "control"}))
    with pytest.raises(SessionError) as invalid_event:
        await service.publish_kernel_event(snapshot.session_id, object())
    assert invalid_event.value.code == "invalid_event"
    assert await service.import_kernel_log(snapshot.session_id, tmp_path / "missing.jsonl") == 0

    subscription = await service.subscribe(
        snapshot.session_id, capabilities=frozenset({"read", "control", "private"})
    )
    while True:
        try:
            await subscription.__anext__()
        except asyncio.CancelledError:
            raise
        except StopAsyncIteration:
            break
        if subscription.queue.empty():
            break
    await subscription.close()
    await subscription.close()
    with pytest.raises(StopAsyncIteration):
        await subscription.__anext__()
    await service.close()
