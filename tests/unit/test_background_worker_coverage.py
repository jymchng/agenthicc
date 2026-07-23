"""Worker process orchestration coverage with fully local fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.worker import WorkerRequest

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str) -> BackgroundSession:
    artifact = tmp_path / "sessions" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(tmp_path),
        workflow_name="",
        intent="work",
        artifact_dir=str(artifact),
    )


def _fake_context() -> SimpleNamespace:
    stop = asyncio.Event()

    class Processor:
        async def run(self) -> None:
            await stop.wait()

        async def drain(self) -> None:
            return None

    return SimpleNamespace(
        processor=Processor(),
        app_state=SimpleNamespace(cli_flags=None),
        cfg=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_worker_success_workflow_failure_and_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.background.worker as worker
    import agenthicc.runners.headless as headless
    import agenthicc.runners.tui_session as tui_session
    import agenthicc.tui.runtime.session_log as session_log

    contexts: list[SimpleNamespace] = []

    async def build(*args: object, **kwargs: object) -> SimpleNamespace:
        context = _fake_context()
        contexts.append(context)
        return context

    async def close(session: object, processor_task: object, registry: object) -> None:
        return None

    monkeypatch.setattr(tui_session, "_build_session_context", build)
    monkeypatch.setattr(headless, "_close_headless_session", close)
    monkeypatch.setattr(session_log, "register_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_run_direct_turn", lambda *args, **kwargs: asyncio.sleep(0))

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "worker-success"))
    request = WorkerRequest("worker-success", "", "work", str(tmp_path), None, (), False)
    assert await worker.run_worker(request, store) == 0
    assert store.get("worker-success").status is SessionStatus.COMPLETED

    store.create(_session(tmp_path, "worker-workflow"))
    monkeypatch.setattr(
        headless,
        "execute_workflow",
        lambda *args, **kwargs: asyncio.sleep(
            0, result=SimpleNamespace(status="complete", error=None)
        ),
    )
    workflow_request = WorkerRequest(
        "worker-workflow", "demo", "work", str(tmp_path), None, (), False
    )
    assert await worker.run_worker(workflow_request, store) == 0

    store.create(_session(tmp_path, "worker-failed"))

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("worker failure")

    monkeypatch.setattr(worker, "_run_direct_turn", fail)
    failed_request = WorkerRequest("worker-failed", "", "work", str(tmp_path), None, (), False)
    assert await worker.run_worker(failed_request, store) == 1
    assert store.get("worker-failed").status is SessionStatus.FAILED

    async def wait_forever(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_run_direct_turn", wait_forever)
    store.create(_session(tmp_path, "worker-timeout"))
    timeout_request = WorkerRequest(
        "worker-timeout", "", "work", str(tmp_path), None, (), False, wall_timeout_s=0.01
    )
    assert await worker.run_worker(timeout_request, store) == 1
    assert store.get("worker-timeout").status is SessionStatus.FAILED
