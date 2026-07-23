"""Remaining lifecycle, CLI, and manager branches for PRD-141."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    InvalidSessionTransition,
    SessionStatus,
)
from agenthicc.background.settings import BackgroundSettings
from agenthicc.background.worker import BackgroundApprovalService, WorkerRequest, _load_request
import agenthicc.background.worker as worker

pytestmark = pytest.mark.unit


def _record(tmp_path: Path, sid: str = "remaining") -> BackgroundSession:
    artifact = tmp_path / "sessions" / sid
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        sid,
        title="Remaining",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="private intent",
        artifact_dir=str(artifact),
    )


async def _wait_for(store: BackgroundStore, sid: str, status: SessionStatus) -> None:
    for _ in range(100):
        if store.get(sid, include_deleted=True).status is status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"session did not reach {status}")


@pytest.mark.asyncio
async def test_approval_and_input_services_accept_and_validate_values(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path, "approve"))
    store.claim("approve", pid=1, lease_token="lease")
    service = BackgroundApprovalService(store, "approve")
    task = asyncio.create_task(service.request_approval(SimpleNamespace(tool_name="write")))
    await _wait_for(store, "approve", SessionStatus.WAITING_APPROVAL)
    store.update("approve", approval_decision=True)
    response = await task
    assert response.allowed is True
    assert store.get("approve").status is SessionStatus.RUNNING

    with pytest.raises(ValueError):
        service.provide_input(" ")
    with pytest.raises(InvalidSessionTransition):
        service.provide_input("answer")

    store.create(_record(tmp_path, "input"))
    store.claim("input", pid=1, lease_token="input-lease")
    input_service = BackgroundApprovalService(store, "input")
    input_task = asyncio.create_task(
        input_service.request_input(SimpleNamespace(tool_name="question"))
    )
    await _wait_for(store, "input", SessionStatus.WAITING_INPUT)
    input_service.provide_input("answer")
    answer = await input_task
    assert answer.allowed is True and answer.message == "answer"

    store.create(_record(tmp_path, "approval-race"))
    store.claim("approval-race", pid=1, lease_token="race")
    race_service = BackgroundApprovalService(store, "approval-race")
    race_task = asyncio.create_task(
        race_service.request_approval(SimpleNamespace(tool_name="race"))
    )
    await _wait_for(store, "approval-race", SessionStatus.WAITING_APPROVAL)
    await asyncio.sleep(0.25)
    original_transition = store.transition

    def reject_transition(session_id: str, target: SessionStatus, **kwargs: object) -> object:
        if target is SessionStatus.RUNNING:
            raise InvalidSessionTransition("race")
        return original_transition(session_id, target, **kwargs)

    race_service.store.transition = reject_transition  # type: ignore[method-assign]
    store.update("approval-race", approval_decision=True)
    race_response = await race_task
    assert race_response.allowed is False and "changed" in race_response.message
    race_service.store.transition = original_transition  # type: ignore[method-assign]

    store.create(_record(tmp_path, "input-denied"))
    store.transition("input-denied", SessionStatus.STARTING)
    store.transition("input-denied", SessionStatus.RUNNING)
    store.transition("input-denied", SessionStatus.COMPLETED)
    denied_input = await BackgroundApprovalService(store, "input-denied").request_input(
        SimpleNamespace(tool_name="question")
    )
    assert denied_input.allowed is False

    store.create(_record(tmp_path, "input-removed"))
    store.claim("input-removed", pid=1, lease_token="removed")
    original_get = store.get
    get_calls = 0

    def missing_get(session_id: str, **kwargs: object) -> BackgroundSession:
        nonlocal get_calls
        get_calls += 1
        if get_calls > 4:
            raise KeyError(session_id)
        return original_get(session_id, **kwargs)

    store.get = missing_get  # type: ignore[method-assign]
    removed = await BackgroundApprovalService(store, "input-removed").request_input(
        SimpleNamespace(tool_name="question")
    )
    assert removed.allowed is False and "removed" in removed.message


def test_worker_request_defaults_and_loader_errors(tmp_path: Path) -> None:
    request = WorkerRequest.from_mapping(
        {"session_id": "x", "intent": "go", "cwd": str(tmp_path), "wall_timeout_s": True}
    )
    assert request.wall_timeout_s == 0.0 and request.max_activity_bytes == 64_000
    path = tmp_path / "bad.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _load_request(path)
    path.write_text(
        json.dumps({"session_id": "x", "intent": "go", "cwd": str(tmp_path), "config_path": 4}),
        encoding="utf-8",
    )
    assert _load_request(path).config_path is None


@pytest.mark.asyncio
async def test_worker_rejects_non_running_claim_and_main_reports_bad_request(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    record = _record(tmp_path, "finished")
    store.create(record)
    store.transition(record.session_id, SessionStatus.STARTING)
    store.transition(record.session_id, SessionStatus.RUNNING)
    store.transition(record.session_id, SessionStatus.COMPLETED)
    request = WorkerRequest(record.session_id, "", "intent", str(tmp_path), None, (), False)
    assert await worker.run_worker(request, store) == 1

    request_file = tmp_path / "missing.json"
    assert (
        worker.main(["--request-file", str(request_file), "--store-root", str(tmp_path / "store")])
        == 1
    )


@pytest.mark.asyncio
async def test_worker_cancel_path_cleans_up_without_resurrecting_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = BackgroundStore(tmp_path / "background")
    record = _record(tmp_path, "cancelled")
    store.create(record)
    stop = asyncio.Event()

    class Processor:
        async def run(self) -> None:
            await asyncio.Event().wait()

        async def drain(self) -> None:
            return None

    session = SimpleNamespace(
        processor=Processor(),
        app_state=SimpleNamespace(conversation=SimpleNamespace(cli_flags=None), cli_flags=None),
        agent_runner=object(),
        cfg=SimpleNamespace(
            execution=SimpleNamespace(max_agent_turns=1),
            agents=SimpleNamespace(skill_permissions_for=lambda _name: frozenset()),
        ),
        session_memory=None,
        skills={},
        mention_cache=None,
        project_plugins=SimpleNamespace(all_tools=[]),
        mcp_registry=None,
        approval_svc=None,
        memory_router=None,
        semantic_index=None,
    )

    async def build(*_args: object, **_kwargs: object) -> object:
        return session

    async def direct(*_args: object, **_kwargs: object) -> None:
        await stop.wait()

    async def close(_session: object, processor_task: asyncio.Task[object], _error: object) -> None:
        processor_task.cancel()
        await asyncio.gather(processor_task, return_exceptions=True)

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(record.session_id, "", "intent", str(tmp_path), None, (), False)
    task = asyncio.create_task(worker.run_worker(request, store))
    await _wait_for(store, record.session_id, SessionStatus.RUNNING)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get(record.session_id).status is SessionStatus.RUNNING


def test_store_rejects_bad_updates_and_cleans_malformed_trash(tmp_path: Path) -> None:
    from agenthicc.background.store import default_artifact_dir

    assert default_artifact_dir("sid").name == "sid"
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path))
    with pytest.raises(ValueError, match="Unknown"):
        store.update("remaining", unknown_field=True)
    with pytest.raises(InvalidSessionTransition, match="Expected"):
        store.update("remaining", expected_status=SessionStatus.RUNNING, title="x")
    with pytest.raises(InvalidSessionTransition, match="lease"):
        store.update("remaining", expected_lease_token="stale", title="x")
    with pytest.raises(ValueError, match="non-negative"):
        store.purge_trash(older_than_s=-1)
    store.trash_root.mkdir(parents=True)
    (store.trash_root / "bad").mkdir()
    (store.trash_root / "bad" / "manifest.json").write_text("{}", encoding="utf-8")
    (store.trash_root / "broken").mkdir()
    (store.trash_root / "broken" / "manifest.json").write_text("not-json", encoding="utf-8")
    assert store.purge_trash(older_than_s=0) == []

    store.claim("remaining", pid=1, lease_token="lease")
    store.transition("remaining", SessionStatus.CANCELLING)
    assert store.mark_orphaned("remaining").status is SessionStatus.ORPHANED


def test_store_recovery_and_artifact_kernel_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    store = BackgroundStore(tmp_path / "background")
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 1  # type: ignore[attr-defined]
    fcntl.LOCK_UN = 2  # type: ignore[attr-defined]
    fcntl.flock = lambda *_args: (_ for _ in ()).throw(OSError("lock unavailable"))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fcntl", fcntl)
    with store._lock():
        pass
    store.events_path.parent.mkdir(parents=True, exist_ok=True)
    store.events_path.touch()
    original_open = Path.open
    monkeypatch.setattr(
        Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read"))
    )
    assert store._read_events() == []
    monkeypatch.setattr(Path, "open", original_open)

    empty_store = BackgroundStore(tmp_path / "empty")
    assert empty_store.purge_trash(older_than_s=0) == []

    session = _record(tmp_path, "artifact")
    store.create(session)
    with pytest.raises(ValueError, match="already exists"):
        store.create(session)
    with pytest.raises(ValueError, match="empty"):
        store.rename(session.session_id, "   ")
    assert store.list(workflow_name="other", query="absent") == []
    assert store.list(query="absent") == []
    with store.events_path.open("a", encoding="utf-8") as events:
        events.write(
            json.dumps(
                {"event_type": "created", "payload": {"session_id": "bad", "status": "nope"}}
            )
            + "\n"
        )
    assert store.get(session.session_id).session_id == "artifact"

    retry = _record(tmp_path, "retry").evolve(status=SessionStatus.RETRYING)
    store.create(retry)
    assert store.claim("retry", pid=1, lease_token="retry-lease").status is SessionStatus.RUNNING
    completed = store.transition("artifact", SessionStatus.STARTING)
    completed = store.transition("artifact", SessionStatus.RUNNING)
    completed = store.transition("artifact", SessionStatus.COMPLETED)
    assert store.mark_orphaned("artifact").status is SessionStatus.COMPLETED
    with pytest.raises(InvalidSessionTransition, match="Only archived"):
        store.restore_archive("artifact")

    artifact_dir = Path(completed.artifact_dir)
    kernel = artifact_dir.parent / "artifact.jsonl"
    kernel.write_text("kernel", encoding="utf-8")
    deleted = store.delete("artifact")
    assert Path(deleted.trash_dir, "kernel.jsonl").exists()
    artifact_dir.mkdir()
    with pytest.raises(InvalidSessionTransition, match="already exists"):
        store.restore_deleted("artifact")
    artifact_dir.rmdir()
    assert store.restore_deleted("artifact").status is SessionStatus.COMPLETED

    with pytest.raises(InvalidSessionTransition, match="recoverable trash"):
        store.restore_deleted("retry")

    original_from_mapping = BackgroundSession.from_mapping

    def bad_from_mapping(_cls: type[BackgroundSession], _value: object) -> BackgroundSession:
        raise TypeError("bad record")

    monkeypatch.setattr(BackgroundSession, "from_mapping", classmethod(bad_from_mapping))
    store._fold()
    monkeypatch.setattr(BackgroundSession, "from_mapping", original_from_mapping)

    original_evolve = BackgroundSession.evolve

    def bad_evolve(_self: BackgroundSession, **_changes: object) -> BackgroundSession:
        if "trash_dir" in _changes:
            return original_evolve(_self, **_changes)
        raise ValueError("bad update")

    monkeypatch.setattr(BackgroundSession, "evolve", bad_evolve)
    store._fold()
    monkeypatch.setattr(BackgroundSession, "evolve", original_evolve)


def test_cli_background_config_mutations_and_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext

    cfg = SimpleNamespace(background=BackgroundSettings(store_path=str(tmp_path / "store")))
    monkeypatch.setattr(background, "_config", lambda _ctx: cfg)
    store, supervisor = background._store_and_supervisor(CLIContext())
    assert store.root == tmp_path / "store" and supervisor.max_workers == 2
    monkeypatch.setattr(background, "background_enabled", lambda _settings: False)
    with pytest.raises(RuntimeError, match="disabled"):
        background._store_and_supervisor(CLIContext())
    monkeypatch.undo()

    session = _record(tmp_path, "public")
    session = session.evolve(
        title="public title", latest_activity="Authorization: Bearer secret", intent="do not show"
    )
    public = background._public_session(session)
    assert "intent" not in public and public["latest_activity"] == "Authorization: <redacted>"

    class Supervisor:
        def __init__(self) -> None:
            self.session = session.evolve(status=SessionStatus.COMPLETED)

        def purge_expired_trash(self) -> list[str]:
            return []

        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: self.session

    fake_supervisor = Supervisor()
    store.create(session)
    monkeypatch.setattr(background, "_store_and_supervisor", lambda _ctx: (store, fake_supervisor))
    for operation in (
        background.jobs_cancel,
        background.jobs_resume,
        background.jobs_retry,
        background.jobs_archive,
        background.jobs_delete,
        background.jobs_restore,
    ):
        try:
            operation(CLIContext(), "public")
        except SystemExit:
            pass
    asyncio.run(background.run(CLIContext(), False, "", "", ""))
    background.jobs_approve(CLIContext(), "public")
    background.jobs_reject(CLIContext(), "public")
    background.jobs_input(CLIContext(), "public", "answer")
    background.jobs_rename(CLIContext(), "public", "new title")
    background.jobs_labels(CLIContext(), "public", "one,two")
    background.jobs_purge(CLIContext())
    assert "public" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_manager_bulk_activity_and_interactive_action_edges(tmp_path: Path) -> None:
    from rich.console import Console
    from agenthicc.tui.workspace.background_manager import BackgroundManager

    store = BackgroundStore(tmp_path / "background")
    record = _record(tmp_path, "manager-remaining")
    store.create(record)
    store.transition(record.session_id, SessionStatus.STARTING)
    store.transition(record.session_id, SessionStatus.RUNNING)
    store.transition(record.session_id, SessionStatus.COMPLETED)
    activity = Path(record.artifact_dir) / "conversation.jsonl"
    activity.write_text("not-json\n{\n", encoding="utf-8")

    class Supervisor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def cancel(self, sid: str) -> object:
            self.calls.append(sid)
            raise RuntimeError("bulk failure")

        def archive(self, sid: str) -> object:
            self.calls.append(sid)
            raise RuntimeError("archive failure")

        def delete(self, sid: str) -> object:
            self.calls.append(sid)
            raise RuntimeError("delete failure")

    supervisor = Supervisor()
    console = Console(record=True)
    manager = BackgroundManager(console, store=store, supervisor=supervisor, refresh_s=0.0)
    assert manager._activity_lines(store.get(record.session_id)) == []
    manager.toggle_mark_selected()
    manager.bulk_cancel()
    manager.marked_ids.add(record.session_id)
    manager.bulk_archive()
    assert "failure" in console.export_text()
    for status in SessionStatus:
        manager._status_style(status)
    assert manager.handle_key("CHAR", "q").action == "exit"
