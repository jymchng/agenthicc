"""Worker process orchestration coverage with fully local fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.worker import (
    WorkerRequest,
    _confirmed_idle_after_thinking,
    _finalize_worker,
)
from agenthicc.tui.conversation_store import ConversationStore

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


def test_confirmed_idle_requires_a_closed_turn_and_rejects_pending_work() -> None:
    conversation = ConversationStore()
    conversation.begin_turn("default", turn_id="turn-1")
    conversation.close_turn()
    session = SimpleNamespace(
        app_state=SimpleNamespace(conversation=conversation, workflow_run=lambda: None)
    )

    assert _confirmed_idle_after_thinking(session)
    conversation.active_tool.set("read_file")
    assert not _confirmed_idle_after_thinking(session)
    conversation.active_tool.set("")
    conversation.subagent_pool_state.set(SimpleNamespace(total=1, done=0, workers=[]))
    assert not _confirmed_idle_after_thinking(session)


def test_worker_request_round_trips_detached_marker() -> None:
    request = WorkerRequest.from_mapping(
        {
            "session_id": "session",
            "intent": "goal",
            "cwd": "/tmp",
            "detached_goal": True,
        }
    )
    assert request.detached_goal is True


def test_finalizer_completes_a_racing_cancellation(tmp_path: Path) -> None:
    from agenthicc.background.worker import _WorkerOutcome

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "cancel-race"))
    store.claim("cancel-race", pid=99, lease_token="lease")
    store.transition(
        "cancel-race",
        SessionStatus.CANCELLING,
        cancellation_reason="user requested cancellation",
    )
    request = WorkerRequest("cancel-race", "", "goal", str(tmp_path), None, (), False)

    _finalize_worker(
        store,
        request,
        lease_token="lease",
        outcome=_WorkerOutcome(
            status=SessionStatus.COMPLETED,
            error=None,
            activity="Workflow complete",
            exit_reason="workflow_complete",
            exit_code=0,
        ),
        worker_pid=99,
    )

    cancelled = store.get("cancel-race")
    assert cancelled.status is SessionStatus.CANCELLED
    assert cancelled.worker_exit_reason == "cancelled"
    assert cancelled.worker_exit_code == 130


def test_finalizer_projects_terminal_metadata_to_goal_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.worker import _WorkerOutcome
    from agenthicc.runs.model import GoalRun, GoalRunStatus
    from agenthicc.runs.store import RunStore

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    run_store = RunStore()
    run_store.create(GoalRun.create("detached goal", repository=str(tmp_path), run_id="run_test"))

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "goal-session"))
    request = WorkerRequest(
        "goal-session",
        "goal_flow",
        "detached goal",
        str(tmp_path),
        None,
        (),
        False,
        run_id="run_test",
        detached_goal=True,
    )
    store.claim(request.session_id, pid=123, lease_token="lease")

    _finalize_worker(
        store,
        request,
        lease_token="lease",
        outcome=_WorkerOutcome(
            status=SessionStatus.COMPLETED,
            error=None,
            activity="Workflow complete",
            exit_reason="workflow_complete",
            exit_code=0,
        ),
        worker_pid=123,
    )

    run = run_store.get("run_test")
    assert run.status is GoalRunStatus.COMPLETED
    assert run.worker_pid == 123
    assert run.worker_exit_reason == "workflow_complete"
    assert run.agents[0].process_id == 123


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

    store.create(_session(tmp_path, "worker-paused"))
    monkeypatch.setattr(
        headless,
        "execute_workflow",
        lambda *args, **kwargs: asyncio.sleep(
            0, result=SimpleNamespace(status="paused", error="provider checkpoint saved")
        ),
    )
    paused_request = WorkerRequest("worker-paused", "demo", "work", str(tmp_path), None, (), False)
    assert await worker.run_worker(paused_request, store) == 1
    paused = store.get("worker-paused")
    assert paused.status is SessionStatus.FAILED
    assert paused.worker_exit_reason == "recoverable_error"
    assert paused.failure_category == "recoverable_workflow"

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


@pytest.mark.asyncio
async def test_detached_worker_retains_pid_and_writes_one_exit_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.background.worker as worker
    import agenthicc.runners.headless as headless
    import agenthicc.runners.tui_session as tui_session
    import agenthicc.tui.runtime.session_log as session_log

    context = _fake_context()
    context.app_state.conversation = ConversationStore()
    context.app_state.workflow_run = lambda: None

    async def build(*args: object, **kwargs: object) -> SimpleNamespace:
        return context

    async def close(*args: object, **kwargs: object) -> None:
        return None

    async def direct(*args: object, **kwargs: object) -> None:
        context.app_state.conversation.begin_turn("default", turn_id="turn-1")
        context.app_state.conversation.close_turn()

    monkeypatch.setattr(tui_session, "_build_session_context", build)
    monkeypatch.setattr(headless, "_close_headless_session", close)
    monkeypatch.setattr(session_log, "register_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_run_direct_turn", direct)

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "detached-session"))
    request = WorkerRequest(
        "detached-session",
        "",
        "goal",
        str(tmp_path),
        None,
        (),
        False,
        detached_goal=True,
    )

    assert await worker.run_worker(request, store) == 0
    completed = store.get("detached-session")
    assert completed.status is SessionStatus.COMPLETED
    assert completed.worker_pid is not None
    assert completed.worker_pid > 0
    assert completed.worker_exit_code == 0
    assert completed.worker_exit_reason == "idle_after_thinking"
    assert completed.worker_finished_at is not None
    assert completed.worker_finalization_attempts == 1
    events = store._read_events()
    assert sum(event.get("event_type") == "worker_exited" for event in events) == 1

    # A repeated finalizer callback is a no-op once finalization metadata is
    # already durable.
    _finalize_worker(
        store,
        request,
        lease_token="stale",
        outcome=worker._WorkerOutcome(
            status=SessionStatus.COMPLETED,
            error=None,
            activity="Turn complete",
            exit_reason="idle_after_thinking",
            exit_code=0,
        ),
        worker_pid=completed.worker_pid,
    )
    assert sum(event.get("event_type") == "worker_exited" for event in store._read_events()) == 1
