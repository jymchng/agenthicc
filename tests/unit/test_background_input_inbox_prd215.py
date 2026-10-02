"""Durability and owner-fencing tests for PRD-215 input delivery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agenthicc.background.input_inbox import BackgroundInputInbox, MAX_INPUT_CHARS
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.store import BackgroundStore, InvalidSessionTransition
from agenthicc.background.supervisor import BackgroundSupervisor
from agenthicc.runners.agent_turn_context import (
    AgentTurnContext,
    QueuedMessageSource,
    bind_queued_message_source,
)


def _running_store(tmp_path: Path) -> tuple[BackgroundStore, BackgroundSession]:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "input-target",
        title="Input target",
        cwd=str(tmp_path),
        workflow_name="code_plan",
        intent="test input delivery",
    ).evolve(
        status=SessionStatus.RUNNING,
        worker_pid=os.getpid(),
        lease_token="current-owner-lease",
        attempt=3,
    )
    store.create(session)
    return store, session


def test_inbox_accepts_fifo_idempotent_messages_for_exact_owner(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    inbox = BackgroundInputInbox(store)

    first = inbox.enqueue(
        session.session_id,
        "first",
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="command-1",
    )
    replay = inbox.enqueue(
        session.session_id,
        "first",
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="command-1",
    )
    second = inbox.enqueue(
        session.session_id,
        "second",
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="command-2",
    )

    assert first == replay
    assert second.state == "accepted"
    claimed_first = inbox.claim_next(
        session.session_id,
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
    )
    assert claimed_first is not None
    assert (claimed_first.message_id, claimed_first.text, claimed_first.state) == (
        "command-1",
        "first",
        "delivered",
    )
    claimed_second = inbox.claim_next(
        session.session_id,
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
    )
    assert claimed_second is not None and claimed_second.message_id == "command-2"
    assert (
        inbox.claim_next(
            session.session_id,
            owner_attempt=session.attempt,
            lease_token=session.lease_token,
        )
        is None
    )
    assert [item.state for item in inbox.receipts(session.session_id)] == [
        "delivered",
        "delivered",
    ]


def test_live_and_deferred_inbox_preserve_input_text_exactly(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    inbox = BackgroundInputInbox(store)
    submitted = "  /workflow resume run-1  \nkeep this line\t"

    live = inbox.enqueue(
        session.session_id,
        submitted,
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="exact-live-input",
    )
    assert live.text == submitted
    assert (
        inbox.enqueue(
            session.session_id,
            submitted,
            owner_attempt=session.attempt,
            lease_token=session.lease_token,
            message_id="exact-live-input",
        ).text
        == submitted
    )

    deferred = inbox.enqueue_deferred(
        session.session_id,
        submitted,
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
        message_id="exact-deferred-input",
    )
    assert deferred.text == submitted
    assert {item.message_id: item.text for item in inbox.receipts(session.session_id)} == {
        "exact-live-input": submitted,
        "exact-deferred-input": submitted,
    }


def test_inbox_rejects_stale_attempt_and_changed_owner(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    inbox = BackgroundInputInbox(store)

    with pytest.raises(InvalidSessionTransition, match="owner changed"):
        inbox.enqueue(
            session.session_id,
            "stale attempt",
            owner_attempt=session.attempt - 1,
            lease_token=session.lease_token,
        )

    with pytest.raises(InvalidSessionTransition, match="owner changed"):
        inbox.enqueue(
            session.session_id,
            "stale lease",
            owner_attempt=session.attempt,
            lease_token="another-owner",
        )

    assert inbox.receipts(session.session_id) == ()


def test_completed_sessions_accept_only_deferred_input(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    inbox = BackgroundInputInbox(store)
    store.update(
        session.session_id,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
        status=SessionStatus.COMPLETED,
    )

    with pytest.raises(InvalidSessionTransition, match="completed"):
        inbox.enqueue(
            session.session_id,
            "live input cannot target a completed attempt",
            owner_attempt=session.attempt,
            lease_token=session.lease_token,
        )
    deferred = inbox.enqueue_deferred(
        session.session_id,
        "  new turn, unchanged  \n",
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
        message_id="completed-session-input",
    )
    assert deferred.text == "  new turn, unchanged  \n"
    assert deferred.deferred

    with pytest.raises(ValueError, match="empty"):
        inbox.enqueue(session.session_id, " ", owner_attempt=3, lease_token="lease")
    with pytest.raises(ValueError, match="characters"):
        inbox.enqueue(
            session.session_id,
            "x" * (MAX_INPUT_CHARS + 1),
            owner_attempt=3,
            lease_token="lease",
        )


def test_accepted_live_input_can_be_deferred_once_after_owner_completion(
    tmp_path: Path,
) -> None:
    store, session = _running_store(tmp_path)
    inbox = BackgroundInputInbox(store)
    original = inbox.enqueue(
        session.session_id,
        "not yet delivered",
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="pending-live-input",
    )
    completed = store.transition(
        session.session_id,
        SessionStatus.COMPLETED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
    )

    deferred = inbox.enqueue_deferred(
        session.session_id,
        original.text,
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        message_id=original.message_id,
    )

    assert deferred.state == "accepted"
    assert deferred.deferred
    starting = store.transition(
        session.session_id,
        SessionStatus.STARTING,
        expected_status=SessionStatus.COMPLETED,
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
    )
    claimed = store.claim(starting.session_id, pid=os.getpid(), lease_token="new-owner")
    delivered = inbox.claim_next(
        session.session_id,
        owner_attempt=claimed.attempt,
        lease_token=claimed.lease_token,
    )
    assert delivered is not None
    assert delivered.message_id == original.message_id
    assert delivered.text == original.text


def test_completed_session_input_starts_one_new_attempt_without_replaying_goal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.background.supervisor import BackgroundRequest

    store, running = _running_store(tmp_path)
    completed = store.transition(
        running.session_id,
        SessionStatus.COMPLETED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=running.attempt,
        expected_lease_token=running.lease_token,
        error="previous attempt's terminal metadata",
        worker_exit_reason="goal_complete",
        worker_exit_code=0,
        worker_started_at=123.0,
        worker_finished_at=456.0,
    )
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "artifacts")
    supervisor._write_request(
        BackgroundRequest(
            session_id=completed.session_id,
            workflow_name=completed.workflow_name,
            intent=completed.intent,
            cwd=completed.cwd,
            config_path=str(tmp_path / "agenthicc.toml"),
            set_overrides=("execution.provider=openai",),
            set_secret_overrides=("providers.openai.api_key=secret-ref",),
            dangerously_skip_permissions=True,
            run_id="old-goal-run",
            detached_goal=True,
            mode_name="Yolo",
        )
    )
    launched: list[tuple[BackgroundRequest, BackgroundSession]] = []

    def capture_launch(request: BackgroundRequest, record: BackgroundSession) -> BackgroundSession:
        launched.append((request, record))
        return record

    monkeypatch.setattr(supervisor, "_launch", capture_launch)

    receipt = supervisor.enqueue_input_or_recover(
        completed.session_id,
        "  /workflow goal_flow  \n",
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        message_id="new-session-turn",
    )
    assert receipt.deferred and receipt.recovery_error == ""
    assert len(launched) == 1
    request, starting = launched[0]
    assert request.session_id == completed.session_id
    assert request.intent == completed.intent
    assert request.config_path == str(tmp_path / "agenthicc.toml")
    assert request.set_overrides == ("execution.provider=openai",)
    assert request.set_secret_overrides == ("providers.openai.api_key=secret-ref",)
    assert request.dangerously_skip_permissions
    assert request.run_id == ""
    assert not request.detached_goal
    assert starting.status is SessionStatus.STARTING
    assert starting.resume_marker == "input:new-session-turn"
    assert starting.completed_at is None
    assert starting.error is None
    assert starting.worker_finished_at is None
    assert starting.attempt_history[-1].status is SessionStatus.COMPLETED
    assert starting.attempt_history[-1].error == "previous attempt's terminal metadata"

    # An idempotent retry and a second input during startup join the same
    # deferred inbox; neither launches another worker or targets the old lease.
    same = supervisor.enqueue_input_or_recover(
        completed.session_id,
        "  /workflow goal_flow  \n",
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        message_id="new-session-turn",
    )
    second = supervisor.enqueue_input_or_recover(
        completed.session_id,
        "follow-up",
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        message_id="second-session-turn",
    )
    assert same.message_id == receipt.message_id
    assert second.deferred
    assert len(launched) == 1
    assert [item.text for item in BackgroundInputInbox(store).receipts(completed.session_id)] == [
        "  /workflow goal_flow  \n",
        "follow-up",
    ]


def test_maintenance_recovers_pending_input_left_on_completed_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, running = _running_store(tmp_path)
    completed = store.transition(
        running.session_id,
        SessionStatus.COMPLETED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=running.attempt,
        expected_lease_token=running.lease_token,
    )
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        completed.session_id,
        "accepted before manager shutdown",
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        message_id="crash-window-input",
    )
    supervisor = BackgroundSupervisor(store)
    launched: list[BackgroundSession] = []

    def capture_launch(_request: object, record: BackgroundSession) -> BackgroundSession:
        launched.append(record)
        return record

    monkeypatch.setattr(supervisor, "_launch", capture_launch)

    changed = supervisor.recover_stale_batch(max_sessions=100)

    assert len(launched) == 1
    assert launched[0].status is SessionStatus.STARTING
    assert any(item.session_id == completed.session_id for item in changed)
    assert store.get(completed.session_id).status is SessionStatus.STARTING
    assert inbox.receipts(completed.session_id)[0].state == "accepted"


def test_maintenance_recovers_crash_after_completed_input_start_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, running = _running_store(tmp_path)
    completed = store.transition(
        running.session_id,
        SessionStatus.COMPLETED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=running.attempt,
        expected_lease_token=running.lease_token,
    )
    inbox = BackgroundInputInbox(store)
    inbox.enqueue_deferred(
        completed.session_id,
        "durable input survives a manager crash",
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        message_id="crash-after-start-transition",
    )
    # Simulate the manager stopping after the CAS to STARTING but before
    # Popen/PID persistence. Maintenance first marks this record orphaned,
    # then recovers the input-driven attempt using its durable marker.
    store.transition(
        completed.session_id,
        SessionStatus.STARTING,
        expected_status=SessionStatus.COMPLETED,
        expected_attempt=completed.attempt,
        expected_lease_token=completed.lease_token,
        resume_marker="input:crash-after-start-transition",
        worker_pid=None,
        completed_at=None,
    )
    supervisor = BackgroundSupervisor(store)
    launched: list[BackgroundSession] = []

    def capture_launch(_request: object, record: BackgroundSession) -> BackgroundSession:
        launched.append(record)
        return record

    monkeypatch.setattr(supervisor, "_launch", capture_launch)

    supervisor.recover_stale(stale_after_s=0)

    assert len(launched) == 1
    assert launched[0].status is SessionStatus.STARTING
    assert launched[0].resume_marker == "input:crash-after-start-transition"
    assert inbox.receipts(completed.session_id)[0].state == "accepted"


def test_worker_exit_settles_consumed_and_unconsumed_receipts(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    inbox = BackgroundInputInbox(store)
    first = inbox.enqueue(
        session.session_id,
        "consumed before worker exit",
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="consumed",
    )
    inbox.enqueue(
        session.session_id,
        "arrived after the last safe boundary",
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="pending",
    )
    claimed = inbox.claim_next(
        session.session_id,
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
    )
    assert claimed is not None and claimed.message_id == first.message_id

    receipts = inbox.settle_attempt(
        session.session_id,
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        delivered_ids={first.message_id},
        completed_ids=set(),
        succeeded=True,
    )

    assert [(item.message_id, item.state) for item in receipts] == [
        ("consumed", "completed"),
        ("pending", "rejected"),
    ]
    assert "resend" in receipts[1].error


def test_receipts_reject_input_from_a_stale_worker_attempt(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    inbox = BackgroundInputInbox(store)
    inbox.enqueue(
        session.session_id,
        "do not replay to a new attempt",
        owner_attempt=session.attempt,
        lease_token=session.lease_token,
        message_id="old-attempt",
    )
    store.update(
        session.session_id,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
        attempt=session.attempt + 1,
        lease_token="replacement-owner",
    )

    receipt = inbox.receipts(session.session_id)[0]
    assert receipt.state == "rejected"
    assert "changed" in receipt.error


def test_deferred_input_rebinds_to_recovered_worker_attempt(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    orphaned = store.mark_orphaned(
        session.session_id,
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
        expected_status=SessionStatus.RUNNING,
    )
    inbox = BackgroundInputInbox(store)
    queued = inbox.enqueue_deferred(
        session.session_id,
        "continue from the saved state",
        expected_attempt=orphaned.attempt,
        expected_lease_token=orphaned.lease_token,
        message_id="recover-me",
    )

    assert queued.deferred
    assert inbox.receipts(session.session_id)[0].state == "accepted"

    store.transition(
        session.session_id,
        SessionStatus.STARTING,
        expected_status=SessionStatus.ORPHANED,
        expected_attempt=orphaned.attempt,
    )
    recovered = store.claim(
        session.session_id,
        pid=os.getpid(),
        lease_token="recovered-owner-lease",
    )
    delivered = inbox.claim_next(
        session.session_id,
        owner_attempt=recovered.attempt,
        lease_token=recovered.lease_token,
    )

    assert delivered is not None
    assert delivered.text == "continue from the saved state"
    assert delivered.owner_attempt == recovered.attempt
    assert delivered.state == "delivered"
    assert not delivered.deferred


def test_supervisor_queues_recoverable_input_before_resuming_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _running_store(tmp_path)
    orphaned = store.mark_orphaned(
        session.session_id,
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
        expected_status=SessionStatus.RUNNING,
    )
    supervisor = BackgroundSupervisor(store)
    resumed: list[str] = []

    def resume(session_id: str) -> BackgroundSession:
        resumed.append(session_id)
        return store.get(session_id)

    monkeypatch.setattr(supervisor, "resume", resume)
    receipt = supervisor.enqueue_input_or_recover(
        orphaned.session_id,
        "recover this session",
        expected_attempt=orphaned.attempt,
        expected_lease_token=orphaned.lease_token,
        message_id="recover-input",
    )

    assert resumed == [orphaned.session_id]
    assert receipt.deferred
    assert receipt.recovery_error == ""
    assert BackgroundInputInbox(store).receipts(orphaned.session_id)[0].deferred
    assert store.get(orphaned.session_id).status is SessionStatus.ORPHANED


def test_deferred_input_remains_durable_if_background_recovery_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _running_store(tmp_path)
    failed = store.transition(
        session.session_id,
        SessionStatus.FAILED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
    )
    supervisor = BackgroundSupervisor(store)

    def fail_resume(_session_id: str) -> BackgroundSession:
        raise RuntimeError("worker capacity reached")

    monkeypatch.setattr(supervisor, "resume", fail_resume)
    receipt = supervisor.enqueue_input_or_recover(
        failed.session_id,
        "do not lose this recovery input",
        expected_attempt=failed.attempt,
        expected_lease_token=failed.lease_token,
        message_id="persist-on-launch-error",
    )

    assert receipt.deferred
    assert "background recovery did not start" in receipt.recovery_error
    persisted = BackgroundInputInbox(store).receipts(failed.session_id)[0]
    assert persisted.state == "accepted"
    assert persisted.deferred


def test_deleted_session_rejects_deferred_input(tmp_path: Path) -> None:
    store, session = _running_store(tmp_path)
    deleted = session.evolve(status=SessionStatus.DELETED)
    deleted_store = BackgroundStore(tmp_path / "deleted-background")
    deleted_store.create(deleted)
    with pytest.raises(InvalidSessionTransition, match="Deleted"):
        BackgroundInputInbox(deleted_store).enqueue_deferred(
            session.session_id,
            "must not resurrect",
            expected_attempt=session.attempt,
            expected_lease_token=session.lease_token,
        )


def test_agent_turn_context_inherits_worker_input_source() -> None:
    messages = iter(["follow-up", None])
    source = QueuedMessageSource(claim=lambda: next(messages))
    with bind_queued_message_source(source):
        context = AgentTurnContext(text="initial", runner=object(), processor=object())  # type: ignore[arg-type]

    assert context.next_queued_message is source.claim
    assert context.next_queued_message is not None
    assert context.next_queued_message() == "follow-up"


def test_background_command_router_uses_target_registry_and_rejects_ui_only_commands() -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _dispatch_background_command
    from agenthicc.commands import build_builtin_registry

    events: list[tuple[str, dict[str, object], str | None]] = []

    class Conversation:
        def append_event(
            self, kind: str, payload: dict[str, object], event_id: str | None = None
        ) -> None:
            events.append((kind, payload, event_id))

    session = SimpleNamespace(
        cmd_registry=build_builtin_registry(),
        model_label="openai/test-model",
        session_id="target-session",
        cfg=object(),
        skills={},
        workflow_registry=object(),
        mode_manager=object(),
        terminal_manager=None,
        app_state=SimpleNamespace(conversation=Conversation()),
    )

    error, skill_body = _dispatch_background_command(
        session,
        "/status",
        message_id="status-command",
    )
    assert error == ""
    assert skill_body is None
    assert events[0][0] == "assistant_message"
    assert "target-session" in str(events[0][1]["text"])
    assert events[0][2] == "background-command-output:status-command"

    error, _ = _dispatch_background_command(
        session,
        "/not-a-command",
        message_id="unknown-command",
    )
    assert "Unknown command" in error

    error, _ = _dispatch_background_command(
        session,
        "/workflow resume",
        message_id="workflow-command",
    )
    assert "background workflow controller" in error


@pytest.mark.asyncio
async def test_background_workflow_selection_is_persisted_under_current_owner(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _handle_background_workflow_command

    store, session_record = _running_store(tmp_path)

    class Workflows:
        def get(self, name: str) -> object | None:
            return SimpleNamespace(name="goal_flow") if name == "goal_flow" else None

        def names(self) -> list[str]:
            return ["goal_flow"]

    session = SimpleNamespace(
        session_id=session_record.session_id,
        workflow_registry=Workflows(),
        session_conversation=None,
        workspace_scope=None,
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="")),
        app_state=SimpleNamespace(active_mode=lambda: SimpleNamespace(default_workflow=None)),
    )

    result = await _handle_background_workflow_command(
        session,
        "goal_flow",
        store=store,
        attempt=session_record.attempt,
        lease_token=session_record.lease_token,
    )

    assert result.error == ""
    assert result.workflow_name == "goal_flow"
    assert result.selection_only
    saved = store.get(session_record.session_id)
    assert saved.workflow_name == "goal_flow"
    assert saved.latest_activity == "Workflow selected · goal_flow"


@pytest.mark.asyncio
async def test_background_workflow_selector_fails_closed_for_stale_owner(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _handle_background_workflow_command

    store, session_record = _running_store(tmp_path)

    class Workflows:
        def get(self, name: str) -> object | None:
            return SimpleNamespace(name=name) if name == "goal_flow" else None

        def names(self) -> list[str]:
            return ["goal_flow"]

    session = SimpleNamespace(
        session_id=session_record.session_id,
        workflow_registry=Workflows(),
        session_conversation=None,
        workspace_scope=None,
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="")),
        app_state=SimpleNamespace(active_mode=lambda: SimpleNamespace(default_workflow=None)),
    )
    result = await _handle_background_workflow_command(
        session,
        "goal_flow",
        store=store,
        attempt=session_record.attempt - 1,
        lease_token=session_record.lease_token,
    )

    assert "attempt is stale" in result.error
    assert store.get(session_record.session_id).workflow_name == session_record.workflow_name


@pytest.mark.asyncio
async def test_background_workflow_reset_restores_mode_default(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _handle_background_workflow_command

    store, session_record = _running_store(tmp_path)
    store.update(
        session_record.session_id,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=session_record.attempt,
        expected_lease_token=session_record.lease_token,
        workflow_name="other_flow",
    )

    class Workflows:
        def get(self, name: str) -> object | None:
            return SimpleNamespace(name=name) if name == "goal_flow" else None

        def names(self) -> list[str]:
            return ["goal_flow"]

    session = SimpleNamespace(
        session_id=session_record.session_id,
        workflow_registry=Workflows(),
        session_conversation=None,
        workspace_scope=None,
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="")),
        app_state=SimpleNamespace(
            active_mode=lambda: SimpleNamespace(default_workflow="goal_flow")
        ),
    )
    result = await _handle_background_workflow_command(
        session,
        "reset",
        store=store,
        attempt=session_record.attempt,
        lease_token=session_record.lease_token,
    )

    assert result.error == ""
    assert result.workflow_name == "goal_flow"
    assert result.selection_only
    assert store.get(session_record.session_id).workflow_name == "goal_flow"


@pytest.mark.asyncio
async def test_background_workflow_resume_uses_recovery_coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _handle_background_workflow_command

    store, session_record = _running_store(tmp_path)
    checkpoint = SimpleNamespace(
        run_id="recoverable-run",
        workflow_name="goal_flow",
        intent="continue the saved task",
        recoverable=True,
        display_error="",
    )

    class Workflows:
        def get(self, name: str) -> object | None:
            return SimpleNamespace(name=name) if name == "goal_flow" else None

        def names(self) -> list[str]:
            return ["goal_flow"]

    class RecoveryCoordinator:
        def __init__(self, _session_id: str) -> None:
            pass

        def select_latest_for_resume(self, **kwargs: object) -> object:
            assert kwargs["workflow_registry"] is registry
            return checkpoint

    async def execute(
        _session: object,
        workflow_name: str,
        intent: str,
        *,
        resume_run_id: str | None = None,
    ) -> object:
        assert (workflow_name, intent, resume_run_id) == (
            "goal_flow",
            "continue the saved task",
            "recoverable-run",
        )
        return SimpleNamespace(status="complete", error=None)

    registry = Workflows()
    session = SimpleNamespace(
        session_id=session_record.session_id,
        workflow_registry=registry,
        session_conversation=object(),
        workspace_scope=SimpleNamespace(primary_root=str(tmp_path)),
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="test-profile")),
        app_state=SimpleNamespace(active_mode=lambda: SimpleNamespace(default_workflow=None)),
    )
    monkeypatch.setattr(
        "agenthicc.runners.workflow_recovery.WorkflowRecoveryCoordinator",
        RecoveryCoordinator,
    )
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)

    result = await _handle_background_workflow_command(
        session,
        "resume",
        store=store,
        attempt=session_record.attempt,
        lease_token=session_record.lease_token,
    )

    assert result.error == ""
    assert result.workflow_name == "goal_flow"
    assert not result.selection_only
    assert result.message == "Workflow goal_flow resumed and completed."
    assert store.get(session_record.session_id).workflow_name == "goal_flow"


@pytest.mark.asyncio
async def test_background_workflow_resume_accepts_an_explicit_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _handle_background_workflow_command

    store, session_record = _running_store(tmp_path)
    selected = SimpleNamespace(
        run_id="explicit-run-id",
        workflow_name="goal_flow",
        intent="checkpoint intent",
        recoverable=True,
        display_error="",
    )

    class Workflows:
        def get(self, name: str) -> object | None:
            return SimpleNamespace(name=name) if name == "goal_flow" else None

        def names(self) -> list[str]:
            return ["goal_flow"]

    class RecoveryCoordinator:
        def __init__(self, _session_id: str) -> None:
            pass

        def inspect(self, **kwargs: object) -> list[object]:
            assert kwargs["include_terminal"] is False
            return [selected]

        def select_latest_for_resume(self, **kwargs: object) -> object:
            raise AssertionError("an explicit run ID must not select the latest run")

    async def execute(
        _session: object,
        workflow_name: str,
        intent: str,
        *,
        resume_run_id: str | None = None,
        continuation: str | None = None,
    ) -> object:
        assert (workflow_name, intent, resume_run_id, continuation) == (
            "goal_flow",
            "checkpoint intent",
            "explicit-run-id",
            None,
        )
        return SimpleNamespace(status="complete", error=None)

    registry = Workflows()
    session = SimpleNamespace(
        session_id=session_record.session_id,
        workflow_registry=registry,
        session_conversation=object(),
        workspace_scope=SimpleNamespace(primary_root=str(tmp_path)),
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="test-profile")),
        app_state=SimpleNamespace(active_mode=lambda: SimpleNamespace(default_workflow=None)),
    )
    monkeypatch.setattr(
        "agenthicc.runners.workflow_recovery.WorkflowRecoveryCoordinator",
        RecoveryCoordinator,
    )
    monkeypatch.setattr("agenthicc.runners.headless.execute_workflow", execute)

    result = await _handle_background_workflow_command(
        session,
        "resume explicit-run-id",
        store=store,
        attempt=session_record.attempt,
        lease_token=session_record.lease_token,
    )

    assert result.error == ""
    assert result.workflow_name == "goal_flow"
    assert not result.selection_only
    assert result.message == "Workflow goal_flow resumed and completed."


@pytest.mark.asyncio
async def test_background_workflow_resume_without_checkpoint_is_a_handled_warning(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _handle_background_workflow_command

    store, session_record = _running_store(tmp_path)
    session = SimpleNamespace(
        session_id=session_record.session_id,
        workflow_registry=SimpleNamespace(get=lambda _name: None, names=lambda: []),
        session_conversation=None,
        workspace_scope=None,
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="")),
        app_state=SimpleNamespace(active_mode=lambda: SimpleNamespace(default_workflow=None)),
    )

    result = await _handle_background_workflow_command(
        session,
        "resume",
        store=store,
        attempt=session_record.attempt,
        lease_token=session_record.lease_token,
    )

    assert "durable session conversation" in result.error
    assert result.selection_only
    assert not result.fatal


@pytest.mark.asyncio
async def test_background_workflow_reset_run_discards_checkpoint_and_resets_selector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import _handle_background_workflow_command

    store, session_record = _running_store(tmp_path)
    record = SimpleNamespace(
        run_id="saved-run-123",
        checkpoint=object(),
        display_error="",
    )
    discarded: list[str] = []
    emitted: list[object] = []

    class RecoveryCoordinator:
        def __init__(self, _session_id: str) -> None:
            pass

        def inspect(self, **kwargs: object) -> list[object]:
            assert kwargs["include_terminal"] is True
            return [record]

        def discard(self, selected: object, *, owner_id: str) -> object:
            assert selected is record
            assert owner_id.endswith(":saved-run-123")
            discarded.append(owner_id)
            return SimpleNamespace(run_id="saved-run-123", workflow_name="goal_flow")

    class Processor:
        async def emit(self, event: object) -> None:
            emitted.append(event)

    class Workflows:
        def get(self, name: str) -> object | None:
            return None

        def names(self) -> list[str]:
            return []

    session = SimpleNamespace(
        session_id=session_record.session_id,
        workflow_registry=Workflows(),
        session_conversation=object(),
        workspace_scope=SimpleNamespace(primary_root=str(tmp_path)),
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="")),
        app_state=SimpleNamespace(active_mode=lambda: SimpleNamespace(default_workflow=None)),
        processor=Processor(),
    )
    monkeypatch.setattr(
        "agenthicc.runners.workflow_recovery.WorkflowRecoveryCoordinator",
        RecoveryCoordinator,
    )

    result = await _handle_background_workflow_command(
        session,
        "reset saved-run-123",
        store=store,
        attempt=session_record.attempt,
        lease_token=session_record.lease_token,
    )

    assert result.error == ""
    assert result.workflow_name == ""
    assert result.selection_only
    assert discarded
    assert emitted
    assert store.get(session_record.session_id).workflow_name == ""


@pytest.mark.asyncio
async def test_target_trigger_submission_retains_draft_when_owner_rejects() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
    from agenthicc.tui.trigger import TriggerManager, TriggerResult
    from agenthicc.tui.workspace.overlay import OverlayHost
    from agenthicc.background.manager_service import ManagerOperationResult

    state = AppState.create()
    bus = CommandBus()
    history: list[str] = []

    async def reject(command: SendMessageCommand) -> ManagerOperationResult:
        return ManagerOperationResult(
            command.command_id,
            "target",
            "failed",
            False,
            message="owner changed",
        )

    bus.register(SendMessageCommand, reject)
    editor = UnifiedInputSession(
        app_state=state,
        command_bus=bus,
        trigger_registry=TriggerManager(),
        overlay_host=OverlayHost(state),
        history=history,
        clear_after_acceptance=True,
    )
    await editor._open_trigger_overlay_with_initial(["/", "status"])
    overlay = editor._overlay
    assert overlay is not None and overlay.widget is not None
    overlay.widget._complete(TriggerResult(buffer=list("/status"), submit=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert "".join(state.input.buf()) == "/status"
    assert history == []
