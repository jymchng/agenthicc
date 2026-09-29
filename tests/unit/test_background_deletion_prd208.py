"""Unit coverage for the durable, confirmation-free PRD-208 delete path."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str) -> BackgroundSession:
    artifact = tmp_path / "sessions" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "conversation.jsonl").write_text("{}\n", encoding="utf-8")
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="delete test",
        artifact_dir=str(artifact),
    )


@pytest.mark.asyncio
async def test_delete_async_claims_exact_targets_and_is_idempotent(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "one").evolve(status=SessionStatus.COMPLETED))
    store.create(_session(tmp_path, "two").evolve(status=SessionStatus.COMPLETED))
    supervisor = BackgroundSupervisor(store)
    phases: list[tuple[str, str]] = []

    result = await supervisor.delete_async(
        ("one", "one"),
        operation_id="delete-operation-1",
        progress=lambda session_id, phase: _record_phase(phases, session_id, phase),
    )

    assert result.deleted == ("one",)
    assert result.failures == ()
    assert store.get("one", include_deleted=True).status is SessionStatus.DELETED
    assert store.get("two").status is not SessionStatus.DELETED
    assert store.get("one", include_deleted=True).delete_operation_id == "delete-operation-1"
    assert [phase for _, phase in phases] == ["requested", "moving_artifacts", "completed"]

    repeated = await supervisor.delete_async(("one",), operation_id="delete-operation-1")
    assert repeated.deleted == ("one",)
    assert repeated.failures == ()

    restored = store.restore_deleted("one")
    assert restored.status is SessionStatus.COMPLETED
    again = await supervisor.delete_async(("one",), operation_id="delete-operation-3")
    assert again.deleted == ("one",)
    assert again.failures == ()


@pytest.mark.asyncio
async def test_delete_async_preflights_all_targets_before_mutating(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "existing").evolve(status=SessionStatus.COMPLETED))
    supervisor = BackgroundSupervisor(store)

    result = await supervisor.delete_async(
        ("existing", "missing"), operation_id="delete-operation-preflight"
    )

    assert result.deleted == ()
    assert len(result.failures) == 1
    assert result.failures[0].session_id == "missing"
    assert result.failures[0].code == "target_not_found"
    assert store.get("existing").status is SessionStatus.COMPLETED


async def _record_phase(phases: list[tuple[str, str]], session_id: str, phase: str) -> None:
    phases.append((session_id, phase))


@pytest.mark.asyncio
async def test_delete_async_keeps_event_loop_responsive_while_filesystem_work_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "slow"))
    supervisor = BackgroundSupervisor(store)
    original_delete = store.delete
    started = threading.Event()

    def slow_delete(*args: object, **kwargs: object) -> BackgroundSession:
        started.set()
        time.sleep(0.08)
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(store, "delete", slow_delete)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not started.is_set():
            ticks += 1
            await asyncio.sleep(0)

    ticker_task = asyncio.create_task(ticker())
    result = await supervisor.delete_async(("slow",), operation_id="delete-operation-2")
    ticker_task.cancel()
    await asyncio.gather(ticker_task, return_exceptions=True)

    assert result.ok
    assert ticks > 0
