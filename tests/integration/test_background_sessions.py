"""Integration coverage for PRD-141 control and worker contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.worker import BackgroundApprovalService, WorkerRequest, run_worker

pytestmark = pytest.mark.integration


def _queued(tmp_path: Path, store: BackgroundStore) -> BackgroundSession:
    artifact = tmp_path / "sessions" / "worker-session"
    artifact.mkdir(parents=True)
    session = BackgroundSession.create(
        "worker-session",
        title="Worker test",
        cwd=str(tmp_path),
        workflow_name="",
        intent="do deterministic work",
        artifact_dir=str(artifact),
    )
    store.create(session)
    return session


class _Processor:
    async def run(self) -> None:
        await asyncio.Event().wait()

    async def drain(self) -> None:
        return None


class _Conversation:
    def __init__(self) -> None:
        self.cli_flags = None


class _AppState:
    def __init__(self) -> None:
        self.conversation = _Conversation()
        self.cli_flags = None


class _Session:
    def __init__(self) -> None:
        self.session_id = "worker-session"
        self.processor = _Processor()
        self.app_state = _AppState()
        self.agent_runner = object()
        self.cfg = SimpleNamespace(
            execution=SimpleNamespace(max_agent_turns=5),
            agents=SimpleNamespace(skill_permissions_for=lambda name: object()),
            workflows={},
        )
        self.session_memory = object()
        self.skills = {}
        self.mention_cache = object()
        self.project_plugins = SimpleNamespace(all_tools=[])
        self.mcp_registry = None
        self.approval_svc = object()
        self.memory_router = None
        self.semantic_index = None


@pytest.mark.asyncio
async def test_worker_uses_canonical_direct_turn_and_finalizes(monkeypatch, tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(session: object, request: WorkerRequest) -> None:
        assert request.intent == "do deterministic work"

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="do deterministic work",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )
    assert await run_worker(request, store) == 0
    assert store.get("worker-session").status is SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_records_failure_without_resurrecting_cancelled_job(
    monkeypatch, tmp_path: Path
) -> None:
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(session: object, request: WorkerRequest) -> None:
        raise RuntimeError("controlled failure")

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="do deterministic work",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )
    assert await run_worker(request, store) == 1
    failed = store.get("worker-session")
    assert failed.status is SessionStatus.FAILED
    assert failed.error == "RuntimeError: controlled failure"


@pytest.mark.asyncio
async def test_worker_wall_timeout_is_recorded(monkeypatch, tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(session: object, request: WorkerRequest) -> None:
        await asyncio.sleep(0.05)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="do deterministic work",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
        wall_timeout_s=0.001,
    )
    assert await run_worker(request, store) == 1
    failed = store.get("worker-session")
    assert failed.status is SessionStatus.FAILED
    assert failed.error is not None and "TimeoutError" in failed.error


@pytest.mark.asyncio
async def test_worker_uses_headless_workflow_result(monkeypatch, tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "workflow-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "workflow-session",
            title="Workflow test",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="run workflow",
            artifact_dir=str(artifact),
        )
    )
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def execute(session: object, workflow_name: str, intent: str) -> object:
        assert workflow_name == "demo"
        assert intent == "run workflow"
        return SimpleNamespace(status="complete", error=None)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="workflow-session",
        workflow_name="demo",
        intent="run workflow",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )
    assert await run_worker(request, store) == 0
    assert store.get("workflow-session").status is SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_persists_workflow_phase_history(monkeypatch, tmp_path: Path) -> None:
    """A background workflow leaves phase metadata in the durable registry."""

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "phase-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "phase-session",
            title="Phase history",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="run phases",
            artifact_dir=str(artifact),
        )
    )
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def execute(session: object, workflow_name: str, intent: str) -> object:
        assert workflow_name == "demo"
        assert intent == "run phases"
        return SimpleNamespace(
            status="complete",
            error=None,
            phases=("plan", "execute"),
        )

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="phase-session",
        workflow_name="demo",
        intent="run phases",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    assert await run_worker(request, store) == 0
    completed = store.get("phase-session")
    assert completed.status is SessionStatus.COMPLETED
    assert completed.current_phase == "execute"
    assert completed.phase_history == ("plan", "execute")
    assert completed.exit_reason == "Workflow complete"


@pytest.mark.asyncio
async def test_project_workflow_runs_through_background_worker(
    monkeypatch, tmp_path: Path
) -> None:
    """A real project-local workflow plugin uses the background execution path."""

    workflow_dir = tmp_path / ".agenthicc" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "project_flow.py").write_text(
        "from agenthicc.kernel import Event\n"
        "from agenthicc.workflows.plugin import PhaseRunRecord, PhaseSpec, WorkflowPlugin, WorkflowRun\n"
        "\n"
        "class ProjectFlow(WorkflowPlugin):\n"
        "    name = 'project_flow'\n"
        "    phases = [PhaseSpec(name='plan', agent_type='human')]\n"
        "\n"
        "    @classmethod\n"
        "    def build_runner(cls, config, mode_manager):\n"
        "        class Runner:\n"
        "            async def run(self, intent):\n"
        "                run_id = 'project-run'\n"
        "                config.app_state.workflow_run.set(WorkflowRun(\n"
        "                    run_id=run_id, workflow_name=cls.name, intent=intent,\n"
        "                    current_phase=None, status='complete',\n"
        "                    phase_history=[PhaseRunRecord(\n"
        "                        phase_name='plan', role='human', approved=True,\n"
        "                        output_summary='project output', iteration=1, duration_s=0.01\n"
        "                    )]\n"
        "                ))\n"
        "                config.processor.event_log.append(Event.create(\n"
        "                    'WorkflowPhaseCompleted',\n"
        "                    {'run_id': run_id, 'phase_name': 'plan'},\n"
        "                ))\n"
        "\n"
        "            async def resume(self, context):\n"
        "                return None\n"
        "        return Runner()\n",
        encoding="utf-8",
    )

    from agenthicc.tui.conversation_store import AppState as TUIAppState
    from agenthicc.workflows.registry import build_workflow_registry

    registry = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "user" / ".agenthicc",
    )
    assert registry.get_entry("project_flow") is not None
    assert registry.get_entry("project_flow").source == "project"  # type: ignore[union-attr]

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "project-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "project-session",
            title="Project workflow",
            cwd=str(tmp_path),
            workflow_name="project_flow",
            intent="inspect project workflow",
            artifact_dir=str(artifact),
        )
    )
    fake_session = _Session()
    fake_session.app_state = TUIAppState.create()
    fake_session.workflow_registry = registry
    fake_session.agents_registry = object()
    fake_session.mode_manager = None

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="project-session",
        workflow_name="project_flow",
        intent="inspect project workflow",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    assert await run_worker(request, store) == 0
    completed = store.get("project-session")
    assert completed.status is SessionStatus.COMPLETED
    assert completed.phase_history == ("plan",)
    assert completed.latest_activity == "Workflow complete"


def test_cli_handlers_return_redacted_status(monkeypatch, tmp_path: Path, capsys) -> None:
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext

    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "cli-session",
        title="CLI session",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="private prompt",
    )
    store.create(session)
    monkeypatch.setattr(background, "_store_and_supervisor", lambda ctx: (store, object()))
    background.jobs_status(CLIContext(), "cli-session", True)
    output = capsys.readouterr().out
    assert "cli-session" in output
    assert "private prompt" not in output


def test_cli_status_redacts_secret_patterns(monkeypatch, tmp_path: Path, capsys) -> None:
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext

    store = BackgroundStore(tmp_path / "background")
    store.create(
        BackgroundSession.create(
            "secret-session",
            title="Bearer sk-ant-1234567890123456",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="ignored",
        ).evolve(error="Bearer sk-ant-1234567890123456")
    )
    monkeypatch.setattr(background, "_store_and_supervisor", lambda ctx: (store, object()))
    background.jobs_status(CLIContext(), "secret-session", True)
    output = capsys.readouterr().out
    assert "sk-ant-1234567890123456" not in output
    assert "<redacted>" in output


@pytest.mark.asyncio
async def test_background_approval_waits_for_manager_decision(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    store.claim("worker-session", pid=1, lease_token="worker")
    service = BackgroundApprovalService(store, "worker-session")
    task = asyncio.create_task(service.request_approval(SimpleNamespace(tool_name="write_file")))
    deadline = asyncio.get_running_loop().time() + 2.0
    while store.get("worker-session").status is not SessionStatus.WAITING_APPROVAL:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("approval request did not become visible")
        await asyncio.sleep(0.01)
    store.update("worker-session", approval_decision=True)
    response = await asyncio.wait_for(task, timeout=2.0)
    assert response.allowed is True
    assert store.get("worker-session").status is SessionStatus.RUNNING
