"""Interactive end-to-end coverage for confirmation-free PRD-208 deletion."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    DeleteResult,
    SessionStatus,
)
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.background_manager import BackgroundManager, ManagerResult

pytestmark = pytest.mark.e2e


def _session(tmp_path: Path) -> BackgroundSession:
    artifact = tmp_path / "sessions" / "interactive-delete"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "conversation.jsonl").write_text("{}\n", encoding="utf-8")
    return BackgroundSession.create(
        "interactive-delete",
        title="Interactive delete",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="delete from the TUI",
        artifact_dir=str(artifact),
    ).evolve(status=SessionStatus.COMPLETED)


@pytest.mark.asyncio
async def test_ctrl_x_dispatches_without_confirmation_and_keeps_tui_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = _session(tmp_path)
    store.create(session)
    deletion_started = asyncio.Event()
    deletion_finished = asyncio.Event()

    class SlowSupervisor(BackgroundSupervisor):
        async def delete_async(
            self,
            session_ids: Sequence[str],
            *,
            operation_id: str,
            requested_by: str = "agents",
            progress: Callable[[str, str], Awaitable[None]] | None = None,
        ) -> DeleteResult:
            deletion_started.set()
            await asyncio.sleep(0.05)
            try:
                return await super().delete_async(
                    session_ids,
                    operation_id=operation_id,
                    requested_by=requested_by,
                    progress=progress,
                )
            finally:
                deletion_finished.set()

    class ScriptedBackend:
        def __init__(self) -> None:
            self.keys = [(Key.CTRL_X, ""), (Key.CHAR, "q")]

        def is_interactive(self) -> bool:
            return True

        @contextmanager
        def enter_raw_mode(self):
            yield

        def read_key(self) -> tuple[Key, str]:
            return self.keys.pop(0)

        def restore(self) -> None:
            return None

    backend = ScriptedBackend()
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: backend)
    supervisor = SlowSupervisor(store)
    console = Console(width=120, height=25, record=True)
    manager = BackgroundManager(
        console,
        store=store,
        supervisor=supervisor,
        refresh_s=0.01,
    )
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not deletion_finished.is_set():
            ticks += 1
            await asyncio.sleep(0)

    tick_task = asyncio.create_task(ticker())
    result = await manager.run()
    tick_task.cancel()
    await asyncio.gather(tick_task, return_exceptions=True)

    assert result == ManagerResult("exit")
    assert deletion_started.is_set()
    assert deletion_finished.is_set()
    assert ticks > 0
    assert store.get(session.session_id, include_deleted=True).status is SessionStatus.DELETED
    assert "confirm" not in console.export_text().lower()
