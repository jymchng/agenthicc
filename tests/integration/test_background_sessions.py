"""Integration coverage for PRD-141 control and worker contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
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

    async def emit(self, *_args: object, **_kwargs: object) -> None:
        return None


class _Conversation:
    def __init__(self) -> None:
        self.cli_flags = None
        self.events: list[tuple[str, dict[str, object], str | None]] = []

    def append_event(
        self,
        kind: str,
        payload: dict[str, object],
        event_id: str | None = None,
    ) -> None:
        self.events.append((kind, payload, event_id))


class _AppState:
    def __init__(self) -> None:
        self.conversation = _Conversation()
        self.cli_flags = None


class _Session:
    def __init__(self) -> None:
        from agenthicc.commands import build_builtin_registry

        self.session_id = "worker-session"
        self.processor = _Processor()
        self.app_state = _AppState()
        self.agent_runner = object()
        self.cfg = SimpleNamespace(
            execution=SimpleNamespace(max_agent_turns=5, profile=""),
            agents=SimpleNamespace(skill_permissions_for=lambda name: object()),
            workflows={},
        )
        self.session_memory = object()
        self.session_conversation = None
        self.workspace_scope = None
        self.skills = {}
        self.cmd_registry = build_builtin_registry()
        self.workflow_registry = object()
        self.terminal_manager = None
        self.mention_cache = object()
        self.project_plugins = SimpleNamespace(all_tools=[])
        self.mcp_registry = None
        self.approval_svc = object()
        self.memory_router = None
        self.semantic_index = None

        def resolve_name(name: str) -> str:
            if name.casefold() == "yolo":
                return "Yolo"
            raise ValueError(f"Unknown mode {name!r}")

        self.mode_manager = SimpleNamespace(active_name="Yolo", resolve_name=resolve_name)


@pytest.mark.asyncio
async def test_worker_uses_canonical_direct_turn_and_finalizes(monkeypatch, tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        assert kwargs["mode_name"] == "Yolo"
        return fake_session

    async def direct(session: object, request: WorkerRequest) -> None:
        assert request.intent == "do deterministic work"
        current = store.get(request.session_id)
        assert current.mode_application_status.value == "applied"
        assert current.mode_name == "Yolo"
        assert current.mode_application_attempt == current.attempt
        assert getattr(getattr(session, "mode_manager"), "active_name") == current.mode_name

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
        mode_name="Yolo",
    )
    exit_code = await run_worker(request, store)
    assert exit_code == 0, store.get("worker-session").error
    completed = store.get("worker-session")
    assert completed.status is SessionStatus.COMPLETED
    assert completed.mode_name == "Yolo"
    assert completed.requested_mode_name == "Yolo"


@pytest.mark.asyncio
async def test_recovered_worker_processes_deferred_input_without_replaying_original_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        "worker-session",
        "continue from the last saved point",
        expected_attempt=0,
        expected_lease_token="",
        message_id="deferred-direct-recovery",
    )
    fake_session = _Session()
    observed_intents: list[str] = []

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(_session: object, request: WorkerRequest) -> None:
        observed_intents.append(request.intent)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="original request must not replay first",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    exit_code = await run_worker(request, store)
    assert exit_code == 0, store.get("worker-session").error
    assert observed_intents == ["continue from the last saved point"]
    receipt = inbox.receipts("worker-session")[0]
    assert receipt.state == "completed"
    assert store.get("worker-session").status is SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_completed_session_reactivation_delivers_only_the_new_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.input_inbox import BackgroundInputInbox
    from agenthicc.background.supervisor import BackgroundRequest

    store = BackgroundStore(tmp_path / "background")
    queued = _queued(tmp_path, store)
    starting = store.transition(queued.session_id, SessionStatus.STARTING)
    running = store.claim(
        starting.session_id,
        pid=4321,
        lease_token="previous-attempt-lease",
    )
    completed = store.transition(
        running.session_id,
        SessionStatus.COMPLETED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=running.attempt,
        expected_lease_token=running.lease_token,
    )
    inbox = BackgroundInputInbox(store)
    user_text = "  /workflow goal_flow  \nexact target text"
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "artifacts")
    launched: list[tuple[BackgroundRequest, BackgroundSession]] = []

    def capture_launch(request: BackgroundRequest, record: BackgroundSession) -> BackgroundSession:
        launched.append((request, record))
        return record

    monkeypatch.setattr(supervisor, "_launch", capture_launch)
    accepted = supervisor.enqueue_input_or_recover(
        completed.session_id,
        user_text,
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        message_id="completed-turn-input",
    )
    assert accepted.deferred and not accepted.recovery_error
    assert len(launched) == 1
    prepared, prepared_session = launched[0]
    assert prepared_session.status is SessionStatus.STARTING
    assert prepared.run_id == ""
    assert not prepared.detached_goal

    fake_session = _Session()
    observed_intents: list[str] = []

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(_session: object, request: WorkerRequest) -> None:
        observed_intents.append(request.intent)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    worker_request = WorkerRequest(
        session_id=prepared.session_id,
        workflow_name=prepared.workflow_name,
        intent=prepared.intent,
        cwd=prepared.cwd,
        config_path=prepared.config_path,
        set_overrides=prepared.set_overrides,
        dangerously_skip_permissions=prepared.dangerously_skip_permissions,
        wall_timeout_s=prepared.wall_timeout_s,
        max_activity_bytes=prepared.max_activity_bytes,
        source=prepared.source,
        set_secret_overrides=prepared.set_secret_overrides,
        detached_goal=prepared.detached_goal,
        run_id=prepared.run_id,
        mode_name=prepared.mode_name,
    )

    exit_code = await run_worker(worker_request, store)

    assert exit_code == 0, store.get(completed.session_id).error
    assert observed_intents == [user_text]
    final = store.get(completed.session_id)
    assert final.session_id == completed.session_id
    assert final.status is SessionStatus.COMPLETED
    assert final.attempt == completed.attempt + 1
    assert final.attempt_history[-1].status is SessionStatus.COMPLETED
    receipt = inbox.receipts(completed.session_id)
    assert len(receipt) == 1
    assert receipt[0].text == user_text
    assert receipt[0].state == "completed"

    # A retry carrying the same stable message ID is idempotent even after the
    # target has completed the new turn; it cannot launch an empty worker that
    # would fall through and replay the previous request intent.
    duplicate = supervisor.enqueue_input_or_recover(
        completed.session_id,
        user_text,
        expected_attempt=final.attempt,
        expected_lease_token=final.lease_token,
        message_id="completed-turn-input",
    )
    assert duplicate.state == "completed"
    assert len(launched) == 1
    assert store.get(completed.session_id).attempt == final.attempt


@pytest.mark.asyncio
async def test_forwarded_workflow_command_and_following_text_reach_target_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The command is handled by the target; following prose stays a normal turn."""
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        "worker-session",
        "/workflow goal_flow",
        expected_attempt=0,
        expected_lease_token="",
        message_id="select-workflow",
    )
    inbox.enqueue_deferred(
        "worker-session",
        "implement the requested goal",
        expected_attempt=0,
        expected_lease_token="",
        message_id="workflow-intent",
    )
    fake_session = _Session()
    fake_session.workflow_registry = SimpleNamespace(
        get=lambda name: SimpleNamespace(name=name) if name == "goal_flow" else None,
        names=lambda: ["goal_flow"],
    )
    observed: list[tuple[str, str, str | None]] = []
    delivered: list[str] = []

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def execute(
        _session: object,
        workflow_name: str,
        intent: str,
        *,
        resume_run_id: str | None = None,
    ) -> object:
        observed.append((workflow_name, intent, resume_run_id))
        return SimpleNamespace(status="complete", error=None, phases=("intake",))

    async def direct(_session: object, direct_request: WorkerRequest) -> None:
        delivered.append(direct_request.intent)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="old launch intent must not replay",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
        mode_name="Yolo",
    )

    assert await run_worker(request, store) == 0, store.get("worker-session").error
    saved = store.get("worker-session")
    assert saved.status is SessionStatus.COMPLETED
    assert saved.workflow_name == "goal_flow"
    assert observed == []
    assert delivered == ["implement the requested goal"]
    assert any(
        kind == "user_message" and payload.get("text") == "/workflow goal_flow"
        for kind, payload, _id in fake_session.app_state.conversation.events
    )
    receipts = inbox.receipts("worker-session")
    assert [(receipt.message_id, receipt.state) for receipt in receipts] == [
        ("select-workflow", "completed"),
        ("workflow-intent", "completed"),
    ]


@pytest.mark.asyncio
async def test_workflow_selection_alone_leaves_session_recoverable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Selecting a workflow is configuration, not a completed user task."""
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        "worker-session",
        "/workflow goal_flow",
        expected_attempt=0,
        expected_lease_token="",
        message_id="select-only",
    )
    fake_session = _Session()
    fake_session.workflow_registry = SimpleNamespace(
        get=lambda name: SimpleNamespace(name=name) if name == "goal_flow" else None,
        names=lambda: ["goal_flow"],
    )

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="old launch intent must not replay",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
        mode_name="Yolo",
    )

    assert await run_worker(request, store) == 0
    saved = store.get("worker-session")
    assert saved.status is SessionStatus.ORPHANED
    assert saved.workflow_name == "goal_flow"
    assert saved.worker_exit_reason == "target_input_handled"
    assert inbox.receipts("worker-session")[0].state == "completed"


@pytest.mark.asyncio
async def test_recovered_workflow_input_is_forwarded_as_a_normal_session_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "worker-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "worker-session",
            title="Recover workflow",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="original workflow intent must not replay",
            artifact_dir=str(artifact),
        ).evolve(status=SessionStatus.STARTING, attempt=2)
    )
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        "worker-session",
        "  continue at the saved phase \ninclude this too\t",
        expected_attempt=2,
        expected_lease_token="",
        message_id="deferred-workflow-recovery",
    )
    fake_session = _Session()
    delivered: list[str] = []

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    def select(*args: object, **kwargs: object) -> str | None:
        raise AssertionError("ordinary input must not inspect workflow checkpoints")

    async def execute(
        _session: object,
        _workflow_name: str,
        intent: str,
        **kwargs: object,
    ) -> object:
        raise AssertionError("ordinary input must not start or resume a workflow")

    async def direct(_session: object, direct_request: WorkerRequest) -> None:
        delivered.append(direct_request.intent)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless._select_headless_workflow_resume", select)
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="demo",
        intent="original workflow intent must not replay",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    exit_code = await run_worker(request, store)
    assert exit_code == 0, store.get("worker-session").error
    assert delivered == ["  continue at the saved phase \ninclude this too\t"]
    assert inbox.receipts("worker-session")[0].state == "completed"
    assert store.get("worker-session").status is SessionStatus.COMPLETED
    assert any(
        kind == "user_message"
        and payload.get("text") == "  continue at the saved phase \ninclude this too\t"
        for kind, payload, _id in fake_session.app_state.conversation.events
    )


@pytest.mark.asyncio
async def test_recovered_workflow_input_never_starts_a_new_workflow_implicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "worker-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "worker-session",
            title="No checkpoint",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="never restart from INIT",
            artifact_dir=str(artifact),
        ).evolve(status=SessionStatus.STARTING, attempt=1)
    )
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        "worker-session",
        "resume where I left off",
        expected_attempt=1,
        expected_lease_token="",
        message_id="deferred-without-checkpoint",
    )
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    def select(*args: object, **kwargs: object) -> str | None:
        raise AssertionError("ordinary input must not inspect workflow checkpoints")

    async def execute(
        _session: object,
        workflow_name: str,
        intent: str,
        **kwargs: object,
    ) -> object:
        raise AssertionError("ordinary input must not start or resume a workflow")

    delivered: list[str] = []

    async def direct(_session: object, direct_request: WorkerRequest) -> None:
        delivered.append(direct_request.intent)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless._select_headless_workflow_resume", select)
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="demo",
        intent="never restart from INIT",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    assert await run_worker(request, store) == 0, store.get("worker-session").error
    receipt = inbox.receipts("worker-session")[0]
    assert receipt.state == "completed"
    assert delivered == ["resume where I left off"]
    saved = store.get("worker-session")
    assert saved.status is SessionStatus.COMPLETED
    assert saved.error is None


@pytest.mark.asyncio
async def test_invalid_workflow_checkpoint_does_not_interfere_with_forwarded_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ordinary input bypasses checkpoint selection and reaches the target turn."""
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "worker-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "worker-session",
            title="Unsafe workflow checkpoint",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="do not replay this launch intent",
            artifact_dir=str(artifact),
        ).evolve(status=SessionStatus.STARTING, attempt=1)
    )
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        "worker-session",
        "continue from the current state",
        expected_attempt=1,
        expected_lease_token="",
        message_id="unsafe-workflow-input",
    )
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    def select(*args: object, **kwargs: object) -> str | None:
        raise ValueError("checkpoint_phase_mismatch")

    async def execute(*args: object, **kwargs: object) -> object:
        raise AssertionError("an unsafe checkpoint must not be executed")

    delivered: list[str] = []

    async def direct(_session: object, direct_request: WorkerRequest) -> None:
        delivered.append(direct_request.intent)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless._select_headless_workflow_resume", select)
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)

    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="demo",
        intent="do not replay this launch intent",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
        mode_name="Yolo",
    )
    assert await run_worker(request, store) == 0

    receipt = inbox.receipts("worker-session")[0]
    saved = store.get("worker-session")
    assert receipt.state == "completed"
    assert saved.status is SessionStatus.COMPLETED
    assert delivered == ["continue from the current state"]
    assert any(
        kind == "user_message" for kind, _payload, _id in fake_session.app_state.conversation.events
    )
    assert not any(
        kind == "assistant_message"
        and "Workflow continuation was not safe" in str(payload.get("text"))
        for kind, payload, _id in fake_session.app_state.conversation.events
    )


@pytest.mark.asyncio
async def test_forwarded_workflow_resume_without_checkpoint_does_not_fail_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The target router handles a command warning; the manager did deliver it."""
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "worker-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "worker-session",
            title="Workflow command",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="old launch intent",
            artifact_dir=str(artifact),
        ).evolve(status=SessionStatus.STARTING, attempt=1)
    )
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        "worker-session",
        "/workflow resume",
        expected_attempt=1,
        expected_lease_token="",
        message_id="workflow-resume-no-checkpoint",
    )
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="demo",
        intent="old launch intent",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
        mode_name="Yolo",
    )

    assert await run_worker(request, store) == 0
    receipt = inbox.receipts("worker-session")[0]
    saved = store.get("worker-session")
    assert receipt.state == "completed"
    assert saved.status is SessionStatus.ORPHANED
    assert saved.worker_exit_reason == "target_input_handled"
    assert any(
        kind == "assistant_message" and "durable session conversation" in str(payload.get("text"))
        for kind, payload, _id in fake_session.app_state.conversation.events
    )


@pytest.mark.asyncio
async def test_worker_binds_live_input_to_shared_agent_turn_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.input_inbox import BackgroundInputInbox
    from agenthicc.runners.agent_turn_context import AgentTurnContext

    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(session: object, request: WorkerRequest) -> None:
        current = store.get(request.session_id)
        inbox = BackgroundInputInbox(store)
        inbox.enqueue(
            request.session_id,
            "continue with the next workflow step",
            owner_attempt=current.attempt,
            lease_token=current.lease_token,
            message_id="manager-command-1",
        )
        context = AgentTurnContext(
            text=request.intent,
            runner=object(),  # type: ignore[arg-type]
            processor=object(),  # type: ignore[arg-type]
        )
        assert context.next_queued_message is not None
        assert context.next_queued_message() == "continue with the next workflow step"
        assert fake_session.app_state.conversation.events == [
            (
                "user_message",
                {
                    "text": "continue with the next workflow step",
                    "source": "agents-manager",
                },
                "manager-command-1",
            )
        ]

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
    receipt = BackgroundInputInbox(store).receipts(request.session_id)[0]
    assert receipt.state == "completed"
    assert store.get(request.session_id).status is SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_live_slash_input_is_handled_by_target_router_and_session_stays_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The worker forwards command text to its own registry, not the manager."""
    from agenthicc.background.input_inbox import BackgroundInputInbox
    from agenthicc.runners.agent_turn_context import AgentTurnContext

    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    fake_session = _Session()
    fake_session.model_label = "test-model"

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(_session: object, request: WorkerRequest) -> None:
        current = store.get(request.session_id)
        BackgroundInputInbox(store).enqueue(
            request.session_id,
            "/status",
            owner_attempt=current.attempt,
            lease_token=current.lease_token,
            message_id="forwarded-status-command",
        )
        context = AgentTurnContext(
            text=request.intent,
            runner=object(),  # type: ignore[arg-type]
            processor=object(),  # type: ignore[arg-type]
        )
        assert context.next_queued_message is not None
        assert context.next_queued_message() is None

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

    exit_code = await run_worker(request, store)
    assert exit_code == 0, store.get(request.session_id).error
    saved = store.get(request.session_id)
    receipt = BackgroundInputInbox(store).receipts(request.session_id)[0]
    assert saved.status is SessionStatus.ORPHANED
    assert saved.worker_exit_reason == "target_input_handled"
    assert receipt.state == "completed"
    assert any(
        kind == "assistant_message" and "worker-session" in str(payload.get("text"))
        for kind, payload, _id in fake_session.app_state.conversation.events
    )


@pytest.mark.asyncio
async def test_worker_drains_input_arriving_after_last_agent_safe_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    fake_session = _Session()
    observed_intents: list[str] = []

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(session: object, request: WorkerRequest) -> None:
        observed_intents.append(request.intent)
        if len(observed_intents) == 1:
            current = store.get(request.session_id)
            BackgroundInputInbox(store).enqueue(
                request.session_id,
                "arrived just after the final tool boundary",
                owner_attempt=current.attempt,
                lease_token=current.lease_token,
                message_id="late-manager-command",
            )

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
    assert observed_intents == [
        "do deterministic work",
        "arrived just after the final tool boundary",
    ]
    receipt = BackgroundInputInbox(store).receipts(request.session_id)[0]
    assert receipt.state == "completed"
    assert fake_session.app_state.conversation.events[0][2] == "late-manager-command"


@pytest.mark.asyncio
async def test_late_manager_input_during_workflow_is_forwarded_as_normal_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.input_inbox import BackgroundInputInbox

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "worker-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "worker-session",
            title="Workflow worker",
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="start the workflow",
            artifact_dir=str(artifact),
        )
    )
    fake_session = _Session()
    workflow_calls: list[str] = []
    delivered: list[str] = []

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    def select(*args: object, **kwargs: object) -> str | None:
        return None

    async def execute(
        session: object,
        workflow_name: str,
        intent: str,
        **kwargs: object,
    ) -> object:
        workflow_calls.append(intent)
        if len(workflow_calls) == 1:
            current = store.get("worker-session")
            BackgroundInputInbox(store).enqueue(
                "worker-session",
                "continue from the saved phase",
                owner_attempt=current.attempt,
                lease_token=current.lease_token,
                message_id="late-workflow-input",
            )
        return SimpleNamespace(status="complete", error=None)

    async def direct(_session: object, direct_request: WorkerRequest) -> None:
        delivered.append(direct_request.intent)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.runners.headless._select_headless_workflow_resume", select)
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="demo",
        intent="start the workflow",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    assert await run_worker(request, store) == 0
    assert workflow_calls == ["start the workflow"]
    assert delivered == ["continue from the saved phase"]
    receipt = BackgroundInputInbox(store).receipts(request.session_id)[0]
    assert receipt.state == "completed"


@pytest.mark.asyncio
async def test_worker_first_turn_sees_attested_mode_from_production_session_builder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real turn adapter sees attested AppState before fake provider dispatch."""
    from agenthicc.agents import registry as agents_registry
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.commands import plugin_loader
    from agenthicc.memory import journal as memory_journal
    from agenthicc.memory import layers
    from agenthicc.plugins import discovery
    from agenthicc.plugins.discovery import PluginToolSet
    from agenthicc.commands.plugin_loader import CommandPluginSet
    from agenthicc.runners import tui_session
    from agenthicc.skills import bootstrap, loader
    from agenthicc.skills.loader import SkillDiscoveryResult
    from agenthicc.session_service import SessionService
    from agenthicc.workflows import registry as workflows_registry
    from agenthicc.workflows.registry import WorkflowRegistry

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = home / ".agenthicc" / "sessions"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(tui_session, "_SESSIONS_DIR", session_root)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", session_root)
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log._SESSION_INDEX", session_root / "index.json"
    )
    monkeypatch.setattr(memory_journal, "_SESSIONS_DIR", session_root)
    monkeypatch.setattr(
        "agenthicc.session_service.SessionService",
        lambda: SessionService(store_root=tmp_path / "service"),
    )
    monkeypatch.chdir(workspace)
    fake_agent_runner = object()
    monkeypatch.setattr(
        tui_session, "_build_agent_runner", lambda *args, **kwargs: fake_agent_runner
    )
    monkeypatch.setattr(bootstrap, "bootstrap_default_skills", lambda **kwargs: 0)
    monkeypatch.setattr(
        loader, "discover_skills_with_diagnostics", lambda **kwargs: SkillDiscoveryResult({}, ())
    )
    workflow_registry = WorkflowRegistry()
    monkeypatch.setattr(
        workflows_registry, "build_workflow_registry", lambda **kwargs: workflow_registry
    )
    monkeypatch.setattr(agents_registry, "build_agents_registry", lambda **kwargs: AgentsRegistry())
    monkeypatch.setattr(discovery, "discover_project_tools", lambda **kwargs: PluginToolSet())
    monkeypatch.setattr(discovery, "warn_conflicts", lambda tools: None)
    monkeypatch.setattr(
        plugin_loader, "discover_command_plugins", lambda **kwargs: CommandPluginSet()
    )
    monkeypatch.setattr(layers, "GlobalMemoryLayer", lambda: layers.SessionMemoryLayer())

    config_file = workspace / "agenthicc.toml"
    config_file.write_text("[tools]\n", encoding="utf-8")
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    first_turn_observations: list[tuple[str, str, str, bool]] = []

    async def fake_provider_turn(*args: object, **kwargs: object) -> None:
        app_state = kwargs["app_state"]
        mode_manager = getattr(app_state, "active_mode")()
        active_mode = getattr(mode_manager, "name")
        durable = store.get("worker-session")
        assert args[0] == "deterministic first turn"
        assert args[1] is fake_agent_runner
        assert durable.mode_application_status.value == "applied"
        assert durable.mode_application_attempt == durable.attempt
        first_turn_observations.append(
            (
                active_mode,
                getattr(mode_manager, "name"),
                durable.mode_name,
                getattr(getattr(app_state, "cli_flags"), "dangerously_skip_permissions"),
            )
        )

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", fake_provider_turn)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="deterministic first turn",
        cwd=str(workspace),
        config_path=str(config_file),
        set_overrides=(),
        dangerously_skip_permissions=False,
        mode_name="YOLO",
    )

    exit_code = await run_worker(request, store)
    assert exit_code == 0, store.get("worker-session").error
    assert first_turn_observations == [("Yolo", "Yolo", "Yolo", False)]
    persisted = BackgroundStore(store.root).get("worker-session")
    assert persisted.requested_mode_name == "YOLO"
    assert persisted.mode_name == "Yolo"
    assert persisted.mode_application_status.value == "applied"


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
@pytest.mark.parametrize("requested_mode", ["DefinitelyNotAMode", ""])
async def test_invalid_requested_mode_is_a_durable_startup_failure(
    monkeypatch, tmp_path: Path, requested_mode: str
) -> None:
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    agent_invocations: list[str] = []

    async def build(*args: object, **kwargs: object) -> _Session:
        assert kwargs["mode_name"] == requested_mode
        raise ValueError(f"Unknown mode {requested_mode!r}. Choose one of: Safe, Plan, Yolo.")

    async def direct(session: object, request: WorkerRequest) -> None:
        agent_invocations.append(request.session_id)

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)

    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="do deterministic work",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
        mode_name=requested_mode,
    )

    assert await run_worker(request, store) == 1
    failed = store.get("worker-session")
    assert failed.status is SessionStatus.FAILED
    assert f"Unknown mode {requested_mode!r}" in (failed.error or "")
    assert failed.mode_application_status.value == "failed"
    assert failed.mode_name == ""
    assert agent_invocations == []


@pytest.mark.asyncio
async def test_worker_refuses_first_turn_when_mode_attestation_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = BackgroundStore(tmp_path / "background")
    _queued(tmp_path, store)
    agent_invocations: list[str] = []
    fake_session = _Session()

    async def build(*args: object, **kwargs: object) -> _Session:
        return fake_session

    async def direct(session: object, request: WorkerRequest) -> None:
        agent_invocations.append(request.session_id)

    async def close(*args: object, **kwargs: object) -> None:
        return None

    def fail_attestation(*args: object, **kwargs: object) -> object:
        raise OSError("durable store unavailable")

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", build)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct)
    monkeypatch.setattr("agenthicc.runners.headless._close_headless_session", close)
    monkeypatch.setattr(store, "record_mode_application", fail_attestation)
    request = WorkerRequest(
        session_id="worker-session",
        workflow_name="",
        intent="must not execute before attestation",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
        mode_name="Yolo",
    )

    assert await run_worker(request, store) == 1
    failed = store.get("worker-session")
    assert failed.status is SessionStatus.FAILED
    assert failed.mode_application_status.value == "failed"
    assert failed.mode_name == ""
    assert "durable store unavailable" in failed.mode_application_error
    assert agent_invocations == []


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
        assert kwargs["mode_name"] == "Yolo"
        return fake_session

    async def execute(session: object, workflow_name: str, intent: str) -> object:
        assert workflow_name == "demo"
        assert intent == "run workflow"
        assert getattr(getattr(session, "mode_manager"), "active_name") == "Yolo"
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
        mode_name="Yolo",
    )
    assert await run_worker(request, store) == 0
    completed = store.get("workflow-session")
    assert completed.status is SessionStatus.COMPLETED
    assert completed.mode_name == "Yolo"


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
async def test_project_workflow_runs_through_background_worker(monkeypatch, tmp_path: Path) -> None:
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
    fake_session.mode_manager = SimpleNamespace(active_name="Safe")

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

    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=4321),
    )
    supervisor.archive("project-session")
    resumed = supervisor.resume("project-session")
    assert resumed.status is SessionStatus.STARTING
    assert resumed.phase_history == ("plan",)
    assert resumed.resume_marker == "resume:2"


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
