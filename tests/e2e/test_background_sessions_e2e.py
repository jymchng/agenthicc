"""Process and CLI end-to-end coverage for PRD-141."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.background.supervisor import BackgroundRequest
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = pytest.mark.e2e


def _wait_for_status(store: BackgroundStore, session_id: str, status: SessionStatus) -> None:
    # The worker is a fresh interpreter; the loop returns as soon as it is done.
    deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if store.get(session_id).status is status:
                return
        except KeyError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"session {session_id} did not reach {status.value}")


@cli_test_timeout(1)
def test_owned_worker_process_persists_completion(tmp_path: Path, monkeypatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "process-session"
    artifact.mkdir(parents=True)
    session = BackgroundSession.create(
        "process-session",
        title="Process worker",
        cwd=str(tmp_path),
        workflow_name="",
        intent="test",
        artifact_dir=str(artifact),
    )
    store.create(session)
    script = tmp_path / "worker.py"
    script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from agenthicc.background import BackgroundStore, SessionStatus\n"
        "store = BackgroundStore(Path(sys.argv[2]))\n"
        "sid = sys.argv[1]\n"
        "store.claim(sid, pid=os.getpid(), lease_token='e2e')\n"
        "time.sleep(0.15)\n"
        "store.transition(sid, SessionStatus.COMPLETED, expected_lease_token='e2e', lease_token='')\n",
        encoding="utf-8",
    )
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    monkeypatch.setattr(
        supervisor,
        "_worker_command",
        lambda request: [sys.executable, str(script), session.session_id, str(store.root)],
    )
    request = BackgroundRequest(
        session_id=session.session_id,
        workflow_name="",
        intent="test",
        cwd=str(tmp_path),
    )
    supervisor._launch(request, session)
    _wait_for_status(store, session.session_id, SessionStatus.COMPLETED)
    assert BackgroundStore(store.root).get(session.session_id).status is SessionStatus.COMPLETED


@cli_test_timeout(1)
def test_completed_session_detach_starts_same_session_without_replaying_intent(
    tmp_path: Path,
) -> None:
    """A terminal prior attempt is reactivated as the same resumable session."""
    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "completed-session"
    artifact.mkdir(parents=True)
    completed = BackgroundSession.create(
        "completed-session",
        title="Completed session",
        cwd=str(tmp_path),
        workflow_name="",
        intent="historical intent must not be replayed",
        artifact_dir=str(artifact),
        run_id="existing-run",
    ).evolve(status=SessionStatus.COMPLETED, attempt=2, completed_at=time.time())
    store.create(completed)
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")

    prepared = supervisor.handoff(
        session_id=completed.session_id,
        intent=completed.intent,
        cwd=str(tmp_path),
        run_id="",
        start=False,
        resume_existing_only=True,
    )
    assert prepared.status is SessionStatus.STARTING
    assert prepared.session_id == completed.session_id
    assert prepared.run_id == "existing-run"
    assert prepared.resume_marker == "resume:3"

    started = supervisor.start(completed.session_id)
    assert started.session_id == completed.session_id
    _wait_for_status(store, completed.session_id, SessionStatus.COMPLETED)

    final = store.get(completed.session_id)
    assert final.attempt == 3
    assert final.status is SessionStatus.COMPLETED
    assert final.latest_activity == "No resumable work"
    assert final.worker_exit_reason == "no_resumable_work"
    assert final.run_id == "existing-run"
    assert [attempt.attempt for attempt in final.attempt_history] == [2, 3]
    assert all(attempt.status is SessionStatus.COMPLETED for attempt in final.attempt_history)
    request = json.loads(
        (store.root / "requests" / f"{completed.session_id}.json").read_text(encoding="utf-8")
    )
    assert request["resume_existing_only"] is True
    assert request["intent"] == completed.intent


@pytest.mark.skipif(os.name == "nt", reason="process command identity recovery is POSIX-only")
@cli_test_timeout(2)
def test_killed_worker_continue_then_detach_uses_same_session_and_updated_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce kill → --continue lease → config change → /detach end to end."""
    from rich.console import Console

    from agenthicc.background.integration import _handoff
    from agenthicc.background.settings import BackgroundSettings
    from agenthicc.runners.session_lease import SessionOpenCoordinator

    store_root = tmp_path / "background"
    artifacts = tmp_path / "artifacts"
    session_id = "killed-then-continued"
    store = BackgroundStore(store_root)
    session = BackgroundSession.create(
        session_id,
        title="Killed worker recovery",
        cwd=str(tmp_path),
        workflow_name="goal_flow",
        intent="continue the existing goal",
        artifact_dir=str(artifacts / session_id),
    )
    store.create(session)

    script = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from agenthicc.background import BackgroundStore\n"
        "args = sys.argv\n"
        "request = Path(args[args.index('--request-file') + 1])\n"
        "store = BackgroundStore(args[args.index('--store-root') + 1])\n"
        "store.claim(request.stem, pid=os.getpid(), lease_token='e2e-worker-lease')\n"
        "time.sleep(120)\n"
    )

    def worker_command(supervisor: BackgroundSupervisor, request_path: Path) -> list[str]:
        return [
            sys.executable,
            "-c",
            script,
            "agenthicc.background.worker",
            "--request-file",
            str(request_path),
            "--store-root",
            str(supervisor.store.root),
        ]

    monkeypatch.setattr(BackgroundSupervisor, "_worker_command", worker_command)
    actual_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def capture_process(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = actual_popen(*args, **kwargs)  # type: ignore[arg-type]
        processes.append(process)
        return process

    monkeypatch.setattr("agenthicc.background.supervisor.subprocess.Popen", capture_process)

    old_supervisor = BackgroundSupervisor(store, artifact_root=artifacts)
    old_request = BackgroundRequest(
        session_id=session_id,
        workflow_name="goal_flow",
        intent="continue the existing goal",
        cwd=str(tmp_path),
    )
    old_supervisor._launch(old_request, session)
    _wait_for_status(store, session_id, SessionStatus.RUNNING)
    old_worker_pid = store.get(session_id).worker_pid
    assert old_worker_pid is not None
    old_process = processes[0]
    os.kill(old_worker_pid, signal.SIGKILL)
    old_process.wait(timeout=60)
    assert old_process.returncode is not None

    sessions_root = tmp_path / "sessions"
    (sessions_root / session_id).mkdir(parents=True)
    foreground_lease = SessionOpenCoordinator(sessions_root).acquire_existing(session_id)
    changed_config = tmp_path / "agenthicc-custom.toml"
    changed_config.write_text(
        "[execution]\nmodel = 'model-selected-after-continue'\nprofile = 'modal-kimi'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agenthicc.background.integration.load_background_settings",
        lambda **_kwargs: BackgroundSettings(store_path=str(store_root)),
    )

    user_event = SimpleNamespace(
        kind="user_message",
        payload={"text": "continue the existing goal"},
    )
    mode = SimpleNamespace(name="Yolo", default_workflow="goal_flow")
    ctx = SimpleNamespace(
        session_id=session_id,
        config_path=str(changed_config),
        cfg=SimpleNamespace(
            background=None,
            execution=SimpleNamespace(profile="modal-kimi"),
        ),
        cfg_overrides=(),
        cfg_secret_overrides=(),
        owner_lease=foreground_lease,
        app_state=SimpleNamespace(
            conversation=SimpleNamespace(
                turns=lambda: [SimpleNamespace(events=[user_event])],
                notify_transient=lambda _message: None,
            ),
            active_mode=lambda: mode,
            cli_flags=SimpleNamespace(dangerously_skip_permissions=False),
        ),
        console=Console(record=True),
    )
    input_session = SimpleNamespace()
    foreground = SimpleNamespace(
        _ctx=ctx,
        _workflow_override="",
        _input_session=input_session,
        _agent_task=None,
        # The /background handoff state TUISession declares.
        _background_intent="",
        _background_handoff_state="idle",
        _background_prepared_supervisor=None,
        _background_prepared_session_id=None,
        _last_submitted_text="",
    )

    assert _handoff(foreground) is True
    _wait_for_status(store, session_id, SessionStatus.RUNNING)
    resumed = store.get(session_id)
    request = json.loads(
        (store_root / "requests" / f"{session_id}.json").read_text(encoding="utf-8")
    )
    assert resumed.session_id == session_id
    assert resumed.attempt == 2
    assert resumed.worker_pid not in {None, old_worker_pid}
    assert resumed.attempt_history[-1].status is SessionStatus.ORPHANED
    assert len(processes) == 2
    assert request["config_path"] == str(changed_config)
    assert request["set_overrides"] == []
    assert request["requested_profile_name"] == "modal-kimi"
    assert request["mode_name"] == "Yolo"
    assert request["workflow_name"] == "goal_flow"
    from agenthicc.config import load_config

    worker_config = load_config(
        config_path=changed_config,
        user_path=tmp_path / "absent-user-config.toml",
        env_overrides=False,
        cli_overrides=request["set_overrides"],
    )
    assert worker_config.execution.model == "model-selected-after-continue"
    assert worker_config.execution.profile == "modal-kimi"
    assert foreground_lease.released
    assert input_session._exit_requested is True

    new_worker_pid = resumed.worker_pid
    assert new_worker_pid is not None
    os.kill(new_worker_pid, signal.SIGKILL)
    processes[1].wait(timeout=60)


@cli_test_timeout(1)
def test_foreground_takeover_stops_owned_worker_before_resume(tmp_path: Path, monkeypatch) -> None:
    """The manager handoff releases the worker before a resumed TUI can start."""

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "foreground-session"
    artifact.mkdir(parents=True)
    session = BackgroundSession.create(
        "foreground-session",
        title="Foreground takeover",
        cwd=str(tmp_path),
        workflow_name="",
        intent="continue directing",
        artifact_dir=str(artifact),
    )
    store.create(session)
    script = tmp_path / "long_worker.py"
    script.write_text(
        "import os, sys, time\n"
        "from agenthicc.background import BackgroundStore\n"
        "store = BackgroundStore(sys.argv[2])\n"
        "store.claim(sys.argv[1], pid=os.getpid(), lease_token='e2e')\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    supervisor = BackgroundSupervisor(
        store,
        cancel_grace_s=1.0,
        artifact_root=tmp_path / "sessions",
    )
    monkeypatch.setattr(
        supervisor,
        "_worker_command",
        lambda request: [sys.executable, str(script), session.session_id, str(store.root)],
    )
    request = BackgroundRequest(
        session_id=session.session_id,
        workflow_name="",
        intent=session.intent,
        cwd=str(tmp_path),
    )
    supervisor._launch(request, session)
    _wait_for_status(store, session.session_id, SessionStatus.RUNNING)
    running = store.get(session.session_id)
    assert running.worker_pid is not None
    worker_pid = running.worker_pid

    released = supervisor.attach_foreground(session.session_id)

    assert released.status is SessionStatus.CANCELLED
    assert released.worker_pid is None
    assert not supervisor._alive(worker_pid)


@cli_test_timeout(3)
def test_agents_cli_listing_and_background_acceptance(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    manager = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert manager.returncode == 0
    assert "Agents — Running 0" in manager.stdout
    assert "No agents yet" in manager.stdout

    accepted = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenthicc",
            "--mode",
            "Yolo",
            "run",
            "--background",
            "--intent",
            "no provider",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert accepted.returncode == 0
    assert "Background session" in accepted.stdout
    session_id = accepted.stdout.split("Background session ", 1)[1].split()[0]
    request_file = tmp_path / ".agenthicc" / "background" / "requests" / f"{session_id}.json"
    assert json.loads(request_file.read_text(encoding="utf-8"))["mode_name"] == "Yolo"
    status = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents", "status", session_id, "--json"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert status.returncode == 0
    assert json.loads(status.stdout)["session_id"] == session_id


@cli_test_timeout(1)
def test_agents_cli_projects_durable_queue_state_across_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    (tmp_path / ".agenthicc.toml").write_text(
        "[background]\n"
        "queue_enabled = true\n"
        "max_workers = 1\n"
        "max_workers_per_project = 1\n"
        "max_queue_length = 4\n",
        encoding="utf-8",
    )
    launches: list[object] = []

    def popen(*args: object, **kwargs: object) -> object:
        launches.append((args, kwargs))
        return type("Process", (), {"pid": 70_000 + len(launches)})()

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr("agenthicc.background.supervisor.subprocess.Popen", popen)
        store = BackgroundStore()
        supervisor = BackgroundSupervisor(
            store,
            max_workers=1,
            max_workers_per_project=1,
            queue_enabled=True,
            max_queue_length=4,
        )
        supervisor.submit(intent="active request", cwd=str(tmp_path / "project"))
        queued = supervisor.submit(
            intent="durable queued request",
            cwd=str(tmp_path / "project"),
        )
    assert queued.status is SessionStatus.QUEUED
    assert len(launches) == 1

    listed = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents", "list", "--json"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )

    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    queued_item = next(item for item in payload if item["session_id"] == queued.session_id)
    assert queued_item["status"] == "queued"
    assert queued_item["dispatch_state"] == "pending"
    assert queued_item["queue_position"] == 1
    assert queued_item["queue_position_advisory"]


@cli_test_timeout(5)
def test_detached_goal_yolo_mode_is_visible_and_attested_across_cli_projections(
    tmp_path: Path,
) -> None:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(key, None)

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True
    )
    subprocess.run(["git", "config", "user.name", "Agenthicc Test"], cwd=repository, check=True)
    (repository / "README.md").write_text("test repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)

    started = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenthicc",
            "--json",
            "--mode",
            "YOLO",
            "--goal",
            "Check detached mode initialization",
            "--detach",
        ],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert started.returncode == 0, started.stderr + started.stdout
    launch_payload = json.loads(started.stdout)
    run_id = launch_payload["run_id"]
    session_id = launch_payload["main_session_id"]
    assert launch_payload["mode"]["requested"] == "YOLO"
    if launch_payload["mode"]["status"] == "applied":
        assert launch_payload["mode"]["effective"] == "Yolo"
    else:
        assert launch_payload["mode"]["status"] == "pending"
        assert launch_payload["mode"]["effective"] is None

    store = BackgroundStore(tmp_path / ".agenthicc" / "background")
    deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S
    while time.monotonic() < deadline:
        session = store.get(session_id)
        if session.mode_application_status.value != "pending":
            break
        time.sleep(0.05)
    assert session.mode_application_status.value == "applied"
    assert session.requested_mode_name == "YOLO"
    assert session.mode_name == "Yolo"

    agents_status = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents", "status", session_id, "--json"],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert agents_status.returncode == 0, agents_status.stderr
    agents_payload = json.loads(agents_status.stdout)
    assert agents_payload["requested_mode_name"] == "YOLO"
    assert agents_payload["mode_name"] == "Yolo"
    assert agents_payload["mode_application_status"] == "applied"

    run_status = subprocess.run(
        [sys.executable, "-m", "agenthicc", "runs", "show", run_id, "--json"],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert run_status.returncode == 0, run_status.stderr
    main_agent = next(
        item for item in json.loads(run_status.stdout)["agents"] if item["role"] == "main"
    )
    assert main_agent["requested_mode_name"] == "YOLO"
    assert main_agent["mode_name"] == "Yolo"
    assert main_agent["mode_application_status"] == "applied"

    agents_status = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents", "--run", run_id, "--json"],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert agents_status.returncode == 0, agents_status.stderr
    agents_payload = json.loads(agents_status.stdout)
    assert any(
        item["role"] == "main"
        and item["mode_name"] == "Yolo"
        and item["mode_application_status"] == "applied"
        for item in agents_payload
    )
