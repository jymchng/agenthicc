"""Failure-path and control-surface coverage for PRD-141."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    InvalidSessionTransition,
    SessionStatus,
)
from agenthicc.background.integration import _handoff, _last_user_text
from agenthicc.background.settings import (
    BackgroundSettings,
    background_enabled,
    load_background_settings,
)
from agenthicc.background.worker import (
    BackgroundApprovalService,
    WorkerRequest,
    _load_request,
    _run_direct_turn,
    run_worker,
)

pytestmark = pytest.mark.unit


def _record(tmp_path: Path, sid: str = "edge-session") -> BackgroundSession:
    artifact = tmp_path / "sessions" / sid
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        sid,
        title="Edge",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="do work",
        artifact_dir=str(artifact),
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled", "yes", "enabled"),
        ("store_path", 4, "store_path"),
        ("max_workers", True, "max_workers"),
        ("max_workers", -1, "max_workers"),
        ("cancel_grace_s", "slow", "cancel_grace_s"),
        ("cancel_grace_s", float("inf"), "cancel_grace_s"),
    ],
)
def test_background_settings_reject_unsafe_values(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        BackgroundSettings.from_mapping({field: value})


def test_background_settings_ignore_bad_toml_and_support_disable_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background.settings import _override_value

    assert _override_value("[") == "["
    bad = tmp_path / "bad.toml"
    bad.write_text("[background\n", encoding="utf-8")
    settings = load_background_settings(config_path=str(bad), overrides=("unrelated=true",))
    assert settings.max_workers == 2
    disabled = BackgroundSettings(enabled=False)
    assert not background_enabled(disabled)
    monkeypatch.setenv("AGENTHICC_DISABLE_BACKGROUND", "1")
    assert not background_enabled(BackgroundSettings())


def test_worker_request_validation_and_json_loading(tmp_path: Path) -> None:
    request = WorkerRequest.from_mapping(
        {
            "session_id": "s",
            "intent": "go",
            "cwd": str(tmp_path),
            "workflow_name": "demo",
            "set_overrides": ["execution.model=x", 3],
            "wall_timeout_s": 2,
            "max_activity_bytes": 100,
        }
    )
    assert request.set_overrides == ("execution.model=x",)
    assert request.wall_timeout_s == 2.0
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request.__dict__), encoding="utf-8")
    assert _load_request(path).session_id == "s"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        _load_request(path)
    with pytest.raises(ValueError, match="requires"):
        WorkerRequest.from_mapping({})


def test_supervisor_validates_limits_and_worker_launch_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="max_workers"):
        BackgroundSupervisor(max_workers=0)
    with pytest.raises(ValueError, match="max_workers_per_project"):
        BackgroundSupervisor(max_workers_per_project=0)
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    with pytest.raises(ValueError, match="invalid"):
        supervisor._request_path("../escape")
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no process")),
    )
    failed = supervisor.submit(intent="launch failure", cwd=str(tmp_path))
    assert failed.status is SessionStatus.FAILED
    assert "Worker launch failed" in (failed.error or "")


def test_supervisor_handoff_cancel_and_approval_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=4321),
    )
    created = supervisor.handoff(
        session_id="handoff", intent="continue", cwd=str(tmp_path), workflow_name="demo"
    )
    assert created.status is SessionStatus.QUEUED
    with pytest.raises(InvalidSessionTransition, match="already managed"):
        supervisor.handoff(session_id="handoff", intent="duplicate", cwd=str(tmp_path))
    assert supervisor.cancel("handoff").status is SessionStatus.CANCELLED
    assert supervisor.cancel("handoff").status is SessionStatus.CANCELLED
    with pytest.raises(InvalidSessionTransition, match="not waiting"):
        supervisor.approve("handoff", True)
    monkeypatch.setattr(supervisor, "_alive", lambda pid: False)
    monkeypatch.setattr(supervisor, "_terminate", lambda pid: None)
    supervisor._terminate(99999)


def test_store_rejects_corrupt_events_and_applies_filters(tmp_path: Path) -> None:
    from agenthicc.background.store import _json_object

    assert _json_object("not an object") == {}
    store = BackgroundStore(tmp_path / "background")
    store.events_path.parent.mkdir(parents=True, exist_ok=True)
    store.events_path.write_text(
        json.dumps({"event_type": "created", "payload": {"session_id": ""}})
        + "\n"
        + json.dumps({"event_type": "updated", "payload": {"session_id": "unknown"}})
        + "\n",
        encoding="utf-8",
    )
    assert store.list() == []
    store.create(_record(tmp_path, "filter-session"))
    assert store.list(cwd=str(tmp_path), workflow_name="demo", status=SessionStatus.QUEUED)
    assert store.list(cwd=str(tmp_path / "other")) == []
    with pytest.raises(KeyError):
        store.get("missing")


def test_store_same_state_and_trash_manifest_edge_cases(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path, "empty-artifact").evolve(artifact_dir=""))
    store.transition("empty-artifact", SessionStatus.QUEUED, latest_activity="still queued")
    deleted = store.delete("empty-artifact", force=True)
    assert deleted.status is SessionStatus.DELETED
    with pytest.raises(InvalidSessionTransition, match="Deleted"):
        store.transition("empty-artifact", SessionStatus.RUNNING)
    assert store.restore_deleted("empty-artifact").status is SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_manager_noninteractive_fallback_and_action_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from rich.console import Console

    from agenthicc.tui.workspace.background_manager import BackgroundManager, ManagerResult

    store = BackgroundStore(tmp_path / "background")
    session = _record(tmp_path, "manager-edge")
    store.create(session)
    store.transition("manager-edge", SessionStatus.STARTING)
    store.transition("manager-edge", SessionStatus.RUNNING)
    store.transition("manager-edge", SessionStatus.COMPLETED)

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("controlled action failure")

    bad = SimpleNamespace(
        delete=fail,
        cancel=fail,
        archive=fail,
        restore_deleted=fail,
        approve=fail,
    )
    console = Console(record=True)
    manager = BackgroundManager(console, store=store, supervisor=bad, refresh_s=0.1)
    manager.set_query("manager")
    manager.render()
    manager.help_visible = True
    manager.render()
    manager.help_visible = False
    manager.handle_key("CHAR", "c")
    manager.handle_key("CHAR", "a")
    manager.handle_key("CHAR", "u")
    manager.handle_key("CHAR", "p")
    manager.handle_key("CHAR", "\x18")
    result = manager.handle_key("CHAR", "y")
    assert isinstance(result, ManagerResult) and result.action == "error"
    assert "controlled action failure" in console.export_text()

    backend = SimpleNamespace(is_interactive=lambda: False)
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: backend)
    assert await manager.run() == ManagerResult("exit")


def test_manager_empty_navigation_and_unavailable_activity(tmp_path: Path) -> None:
    from rich.console import Console

    from agenthicc.tui.workspace.background_manager import BackgroundManager, ManagerResult

    store = BackgroundStore(tmp_path / "background")
    console = Console(record=True)
    manager = BackgroundManager(console, store=store)
    assert manager.selected_session is None
    manager.render()
    manager.handle_key("UP")
    manager.handle_key("DOWN")
    manager.handle_key("CHAR", "r")
    assert manager.handle_key("ENTER") is None
    assert manager.handle_key("CHAR", "q") == ManagerResult("exit")

    session = _record(tmp_path, "unavailable-activity").evolve(error="error summary")
    Path(session.artifact_dir, "conversation.jsonl").write_text("bad\n{}\n", encoding="utf-8")
    store.create(session)
    manager.refresh(force=True)
    assert manager.handle_key("ENTER") is not None
    manager.pending_delete = True
    console.print(manager.render())
    assert "error summary" in console.export_text()


def test_prd141_coverage_gate_builds_the_feature_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.background import coverage_gate

    calls: list[list[str]] = []
    monkeypatch.setattr(
        coverage_gate.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )
    assert coverage_gate.main() == 0
    assert "--cov-fail-under=90" in calls[0]


@pytest.mark.asyncio
async def test_approval_service_denies_completed_or_removed_sessions(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path))
    store.transition("edge-session", SessionStatus.STARTING)
    store.transition("edge-session", SessionStatus.RUNNING)
    store.transition("edge-session", SessionStatus.COMPLETED)
    denied = await BackgroundApprovalService(store, "edge-session").request_approval(
        SimpleNamespace(tool_name="write")
    )
    assert denied.allowed is False


@pytest.mark.asyncio
async def test_approval_service_handles_rejection_and_cancellation(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path))
    store.claim("edge-session", pid=1, lease_token="lease")
    service = BackgroundApprovalService(store, "edge-session")
    task = asyncio.create_task(service.request_approval(SimpleNamespace(tool_name="write")))
    for _ in range(100):
        if store.get("edge-session").status is SessionStatus.WAITING_APPROVAL:
            break
        await asyncio.sleep(0.01)
    store.update("edge-session", approval_decision=False)
    response = await asyncio.wait_for(task, timeout=2)
    assert response.allowed is False
    assert store.get("edge-session").status is SessionStatus.RUNNING
    service.respond(False)
    service.reset_turn_memory()

    task = asyncio.create_task(service.request_approval(SimpleNamespace(tool_name="delete")))
    for _ in range(100):
        if store.get("edge-session").status is SessionStatus.WAITING_APPROVAL:
            break
        await asyncio.sleep(0.01)
    store.transition("edge-session", SessionStatus.CANCELLING)
    response = await asyncio.wait_for(task, timeout=2)
    assert response.allowed is False


@pytest.mark.asyncio
async def test_approval_service_handles_missing_session(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path, "approval-edge"))
    store.claim("approval-edge", pid=1, lease_token="lease")
    original_get = store.get
    calls = 0

    def missing_after_claim(session_id: str, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls > 3:
            raise KeyError(session_id)
        return original_get(session_id, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(store, "get", missing_after_claim)
    response = await BackgroundApprovalService(store, "approval-edge").request_approval(
        SimpleNamespace(tool_name="write")
    )
    assert response.allowed is False and "removed" in response.message
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_background_input_round_trip_and_explicit_supervisor_route(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path, "input-session"))
    store.claim("input-session", pid=1, lease_token="input-lease")
    service = BackgroundApprovalService(store, "input-session")
    request = SimpleNamespace(tool_name="Questions", kind="questions")
    task = asyncio.create_task(service.request_approval(request))
    for _ in range(100):
        if store.get("input-session").status is SessionStatus.WAITING_INPUT:
            break
        await asyncio.sleep(0.01)
    assert store.get("input-session").input_request == "Questions"
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    waiting = supervisor.provide_input("input-session", '{"choice":"yes"}')
    assert waiting.status is SessionStatus.WAITING_INPUT
    response = await asyncio.wait_for(task, timeout=2)
    assert response.allowed is True
    assert response.message == '{"choice":"yes"}'
    assert store.get("input-session").status is SessionStatus.RUNNING


def test_metadata_labels_and_expired_trash_cleanup(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = _record(tmp_path, "metadata-session").evolve(
        provider="anthropic", model="test-model", source="foreground", resume_marker="r1"
    )
    store.create(session)
    renamed = store.rename("metadata-session", "  A useful title  ")
    labeled = store.set_labels("metadata-session", ("one", "one", " two "))
    assert renamed.title == "A useful title"
    assert labeled.labels == ("one", "two")
    store.transition("metadata-session", SessionStatus.STARTING)
    store.transition("metadata-session", SessionStatus.RUNNING)
    completed = store.transition("metadata-session", SessionStatus.COMPLETED)
    assert completed.state_changed_at >= completed.created_at
    assert completed.provider == "anthropic"
    deleted = store.delete("metadata-session")
    trash = Path(deleted.trash_dir)
    assert trash.exists()
    old = time.time() - 10_000
    import os

    os.utime(trash, (old, old))
    assert store.purge_trash(older_than_s=1) == ["metadata-session"]
    assert not trash.exists()


@pytest.mark.asyncio
async def test_direct_turn_requires_runner_and_delegates_to_canonical_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = WorkerRequest("s", "", "intent", "/tmp", None, (), False)
    missing = SimpleNamespace(agent_runner=None)
    with pytest.raises(RuntimeError, match="No LLM"):
        await _run_direct_turn(missing, request)
    calls: list[str] = []

    async def runner(*args: object, **kwargs: object) -> None:
        calls.append(str(args[0]))

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", runner)
    session = SimpleNamespace(
        agent_runner=object(),
        app_state=SimpleNamespace(conversation=object()),
        cfg=SimpleNamespace(
            execution=SimpleNamespace(max_agent_turns=3),
            agents=SimpleNamespace(skill_permissions_for=lambda name: object()),
        ),
        processor=object(),
        session_memory=object(),
        skills={},
        mention_cache=object(),
        project_plugins=SimpleNamespace(all_tools=[]),
        mcp_registry=None,
        approval_svc=object(),
        memory_router=None,
        semantic_index=None,
    )
    await _run_direct_turn(session, request)
    assert calls == ["intent"]


@pytest.mark.asyncio
async def test_worker_build_failure_is_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path))

    async def build(*args: object, **kwargs: object) -> object:
        raise RuntimeError("bad config")

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    request = WorkerRequest("edge-session", "", "do work", str(tmp_path), None, (), False)
    assert await run_worker(request, store) == 1
    assert "bad config" in (store.get("edge-session").error or "")


@pytest.mark.asyncio
async def test_worker_cannot_claim_terminal_session(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_record(tmp_path, "terminal-worker"))
    store.transition("terminal-worker", SessionStatus.STARTING)
    store.transition("terminal-worker", SessionStatus.RUNNING)
    store.transition("terminal-worker", SessionStatus.COMPLETED)
    request = WorkerRequest("terminal-worker", "", "do work", str(tmp_path), None, (), False)
    assert await run_worker(request, store) == 1


@pytest.mark.asyncio
async def test_background_cli_aliases_and_run_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext

    opened: list[str] = []

    async def open_manager(ctx: CLIContext) -> None:
        opened.append("manager")

    monkeypatch.setattr(background, "_open_manager", open_manager)
    await background.agents(CLIContext())
    await background.jobs(CLIContext())
    assert opened == ["manager", "manager"]

    store = BackgroundStore(tmp_path / "background")
    session = _record(tmp_path, "cli-edge")
    store.create(session)
    supervisor = SimpleNamespace(submit=lambda **kwargs: session)
    monkeypatch.setattr(background, "_store_and_supervisor", lambda ctx: (store, supervisor))
    await background.run(CLIContext(), background=False)
    await background.run(CLIContext(), background=True, intent="do it")
    assert "Use --background" in capsys.readouterr().out


def test_background_cli_list_status_and_mutations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext

    store = BackgroundStore(tmp_path / "background")
    session = _record(tmp_path, "cli-edge")
    store.create(session)
    supervisor = SimpleNamespace(
        cancel=lambda sid: session,
        resume=lambda sid: session,
        retry=lambda sid: session,
        archive=lambda sid: session,
        delete=lambda sid: session,
        restore_deleted=lambda sid: session,
        approve=lambda sid, allowed: session,
    )
    monkeypatch.setattr(background, "_store_and_supervisor", lambda ctx: (store, supervisor))
    ctx = CLIContext()
    background.jobs_list(ctx, False, False, False)
    background.jobs_list(ctx, True, True, True)
    background.jobs_status(ctx, "missing", False)
    background.jobs_status(ctx, "missing", True)
    for action in ("cancel", "resume", "retry", "archive", "delete", "restore"):
        background._mutate(ctx, "cli-edge", action)
    background.jobs_approve(ctx, "cli-edge")
    background.jobs_reject(ctx, "cli-edge")
    output = capsys.readouterr().out
    assert "cli-edge" in output
    assert "not_found" in output


@pytest.mark.asyncio
async def test_background_cli_manager_attach_and_handler_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext
    from agenthicc.tui.workspace.background_manager import ManagerResult

    store = BackgroundStore(tmp_path / "background")
    session = _record(tmp_path, "attach-edge")
    store.create(session)
    supervisor = SimpleNamespace(
        cancel=lambda sid: session,
        resume=lambda sid: session,
        retry=lambda sid: session,
        archive=lambda sid: session,
        delete=lambda sid: session,
        restore_deleted=lambda sid: session,
        approve=lambda sid, allowed: session,
    )
    monkeypatch.setattr(background, "_store_and_supervisor", lambda ctx: (store, supervisor))
    monkeypatch.setattr(
        "agenthicc.tui.workspace.background_manager.run_background_manager",
        lambda *args, **kwargs: asyncio.sleep(0, result=ManagerResult("attach", "attach-edge")),
    )
    attached: list[str] = []

    async def resume(**kwargs: object) -> None:
        attached.append(str(kwargs["resume_id"]))

    monkeypatch.setattr("agenthicc.runners.tui_session._run_tui_session", resume)
    await background._open_manager(CLIContext())
    assert attached == ["attach-edge"]

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("handler failure")

    failing = SimpleNamespace(
        cancel=fail,
        resume=fail,
        retry=fail,
        archive=fail,
        delete=fail,
        restore_deleted=fail,
        approve=fail,
    )
    monkeypatch.setattr(background, "_store_and_supervisor", lambda ctx: (store, failing))
    ctx = CLIContext()
    for handler in (
        background.jobs_cancel,
        background.jobs_resume,
        background.jobs_retry,
        background.jobs_archive,
        background.jobs_delete,
        background.jobs_restore,
        background.jobs_approve,
        background.jobs_reject,
    ):
        with pytest.raises(SystemExit):
            handler(ctx, "attach-edge")
    assert "Unable to" in capsys.readouterr().out


def test_foreground_handoff_extracts_latest_user_request_and_handles_missing_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    user_event = SimpleNamespace(kind="user_message", payload={"text": "latest request"})
    ctx = SimpleNamespace(
        session_id="foreground",
        app_state=SimpleNamespace(
            conversation=SimpleNamespace(
                turns=lambda: [SimpleNamespace(events=[user_event])],
                notify_transient=lambda text: None,
            ),
            active_mode=lambda: SimpleNamespace(default_workflow="demo"),
            cli_flags=SimpleNamespace(dangerously_skip_permissions=False),
        ),
        cfg=SimpleNamespace(background=None),
        console=SimpleNamespace(print=lambda text: None),
    )
    session = SimpleNamespace(
        _ctx=ctx,
        _workflow_override="",
        _input_session=SimpleNamespace(),
        _agent_task=None,
    )
    assert _last_user_text(session) == "latest request"
    monkeypatch.setattr(
        "agenthicc.background.integration.BackgroundSupervisor.handoff",
        lambda self, **kwargs: BackgroundSession.create(
            "foreground",
            title="x",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="latest request",
        ).evolve(status=SessionStatus.QUEUED),
    )
    assert _handoff(session) is True
    assert session._input_session._background_exit_requested is True
    empty = SimpleNamespace(
        _ctx=SimpleNamespace(
            app_state=SimpleNamespace(conversation=SimpleNamespace(turns=lambda: [])),
            console=SimpleNamespace(print=lambda text: print(text)),
        )
    )
    _handoff(empty)
    assert "Cannot background" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_foreground_handoff_reports_disabled_and_launch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    event = SimpleNamespace(kind="user_message", payload={"text": "background me"})
    console = Console(record=True)
    ctx = SimpleNamespace(
        session_id="foreground-edge",
        app_state=SimpleNamespace(
            conversation=SimpleNamespace(
                turns=lambda: [SimpleNamespace(events=[event])],
                notify_transient=lambda text: None,
            ),
            active_mode=lambda: SimpleNamespace(default_workflow=""),
            cli_flags=SimpleNamespace(dangerously_skip_permissions=False),
        ),
        cfg=SimpleNamespace(background=BackgroundSettings(enabled=False)),
        console=console,
    )
    session = SimpleNamespace(_ctx=ctx, _workflow_override="", _input_session=SimpleNamespace())
    assert _handoff(session) is True
    assert "disabled" in console.export_text()

    ctx.cfg = SimpleNamespace(background=None)
    task = asyncio.create_task(asyncio.Event().wait())
    session._agent_task = task
    monkeypatch.setattr(
        "agenthicc.background.integration.BackgroundSupervisor.handoff",
        lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("launch failed")),
    )
    assert _handoff(session) is True
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert "launch failed" in console.export_text()


@pytest.mark.asyncio
async def test_installed_foreground_wrappers_route_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.background import integration
    from agenthicc.runners.tui_session import TUISession

    monkeypatch.setattr(integration, "_handoff", lambda session: True)
    fake = SimpleNamespace(dispatch_slash=lambda text: False)
    assert TUISession.dispatch_slash(fake, "/bg") is True
    await TUISession.handle_send(fake, SimpleNamespace(text="/background"))
