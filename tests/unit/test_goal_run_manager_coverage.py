"""Additional branch coverage for goal-run reconciliation and control."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.runs.manager import GoalRunManager, RepositoryValidationError, inspect_repository
from agenthicc.runs.model import GoalRun, GoalRunStatus
from agenthicc.runs.store import RunStore


class _Supervisor:
    def __init__(self, root: Path) -> None:
        self.store = BackgroundStore(root / "background")
        self.cancelled: list[str] = []

    def submit(self, **kwargs: object) -> BackgroundSession:
        session = BackgroundSession.create(
            str(kwargs["session_id"]),
            title=str(kwargs.get("title", "goal")),
            cwd=str(kwargs["cwd"]),
            workflow_name=str(kwargs["workflow_name"]),
            intent=str(kwargs["intent"]),
            run_id=str(kwargs.get("run_id", "")),
            role=str(kwargs.get("role", "main")),
        )
        return self.store.create(session)

    def cancel(self, session_id: str) -> BackgroundSession:
        self.cancelled.append(session_id)
        session = self.store.get(session_id)
        updated = session.evolve(status=SessionStatus.CANCELLED)
        return self.store.update(session_id, status=updated.status)

    def resume(self, session_id: str) -> BackgroundSession:
        self.store.get(session_id)
        return self.store.update(session_id, status=SessionStatus.RUNNING)

    def attach_foreground(self, session_id: str) -> BackgroundSession:
        return self.store.get(session_id)


def _manager(tmp_path: Path) -> tuple[GoalRunManager, _Supervisor]:
    supervisor = _Supervisor(tmp_path)
    manager = GoalRunManager(
        store=RunStore(tmp_path / "runs"), supervisor=supervisor, require_git=False
    )
    manager._validate_workflow = lambda _name: None  # type: ignore[method-assign]
    return manager, supervisor


def test_repository_inspection_handles_missing_and_non_git_paths(tmp_path: Path) -> None:
    with pytest.raises(RepositoryValidationError, match="not a directory"):
        inspect_repository(tmp_path / "missing")
    root, branch, head = inspect_repository(tmp_path, require_git=False)
    assert root == tmp_path.resolve()
    assert branch == ""
    assert head == ""
    with pytest.raises(RepositoryValidationError, match="not a usable Git repository"):
        inspect_repository(tmp_path)


def test_manifest_status_precedence_is_deterministic() -> None:
    helper = GoalRunManager._status_for_manifests
    assert helper(GoalRunStatus.RUNNING, (), has_attention=False) is GoalRunStatus.RUNNING
    assert (
        helper(GoalRunStatus.CREATED, ("planning",), has_attention=False) is GoalRunStatus.PLANNING
    )
    assert (
        helper(GoalRunStatus.RUNNING, ("reviewing",), has_attention=False)
        is GoalRunStatus.NEEDS_ATTENTION
    )
    assert (
        helper(GoalRunStatus.RUNNING, ("verifying",), has_attention=False)
        is GoalRunStatus.VERIFYING
    )
    assert (
        helper(GoalRunStatus.RUNNING, ("integrating",), has_attention=False)
        is GoalRunStatus.INTEGRATING
    )
    assert (
        helper(GoalRunStatus.RUNNING, ("integrating",), has_attention=True)
        is GoalRunStatus.NEEDS_ATTENTION
    )


def test_manager_reconciles_missing_and_active_children(tmp_path: Path) -> None:
    manager, supervisor = _manager(tmp_path)
    run = manager.store.create(
        GoalRun.create("Goal", repository=str(tmp_path), run_id="run_reconcile")
    )
    manager.store.update(run.run_id, main_session_id="missing")
    missing = manager.reconcile(run.run_id)
    assert missing.status is GoalRunStatus.NEEDS_ATTENTION
    assert missing.attention_reasons == ("main background session is missing",)

    main = BackgroundSession.create(
        "main",
        title="main",
        cwd=str(tmp_path),
        workflow_name="goal_flow",
        intent="Goal",
        run_id=run.run_id,
        now=1.0,
    )
    supervisor.store.create(main)
    manager.store.update(run.run_id, main_session_id="main", status=GoalRunStatus.RUNNING)
    child = BackgroundSession.create(
        "child",
        title="child",
        cwd=str(tmp_path),
        workflow_name="code_plan",
        intent="child",
        parent_session_id="main",
        run_id=run.run_id,
    )
    supervisor.store.create(child)
    completed = supervisor.store.update("main", status=SessionStatus.COMPLETED, completed_at=2.0)
    integrating = manager.reconcile(run.run_id)
    assert completed.status is SessionStatus.COMPLETED
    assert integrating.status is GoalRunStatus.INTEGRATING

    supervisor.store.update("child", status=SessionStatus.FAILED, error="worker failed")
    needs_attention = manager.reconcile(run.run_id)
    assert needs_attention.status is GoalRunStatus.NEEDS_ATTENTION
    assert needs_attention.failure_reason == ""


def test_manager_controls_runs_and_projects_linked_agents(tmp_path: Path) -> None:
    manager, supervisor = _manager(tmp_path)
    run = manager.store.create(
        GoalRun.create("Goal", repository=str(tmp_path), run_id="run_controls")
    )
    session = supervisor.submit(
        session_id="main",
        title="main",
        cwd=str(tmp_path),
        workflow_name="goal_flow",
        intent="Goal",
        run_id=run.run_id,
        role="main",
    )
    manager.store.update(run.run_id, main_session_id=session.session_id)

    projected = manager.projection(run.run_id)
    assert projected.agents[0].role == "main"
    resumed = manager.resume(run.run_id)
    assert resumed.status is GoalRunStatus.RUNNING
    assert supervisor.store.get("main").status is SessionStatus.RUNNING
    assert manager.attach(run.run_id).session_id == "main"
    cancelled = manager.cancel(run.run_id)
    assert cancelled.status is GoalRunStatus.CANCELLED
    assert supervisor.cancelled == ["main"]

    no_session = manager.store.create(
        GoalRun.create("No session", repository=str(tmp_path), run_id="run_no_session")
    )
    with pytest.raises(ValueError, match="no main session"):
        manager.resume(no_session.run_id)
    with pytest.raises(ValueError, match="no main session"):
        manager.attach(no_session.run_id)


def test_manager_start_failure_is_recorded(tmp_path: Path) -> None:
    class _Failing(_Supervisor):
        def submit(self, **_kwargs: object) -> BackgroundSession:
            raise RuntimeError("cannot start")

    supervisor = _Failing(tmp_path)
    manager = GoalRunManager(
        store=RunStore(tmp_path / "runs"), supervisor=supervisor, require_git=False
    )
    manager._validate_workflow = lambda _name: None  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="cannot start"):
        manager.start_detached("Goal", repository=tmp_path)
    saved = manager.store.list()[0]
    assert saved.status is GoalRunStatus.FAILED
    assert "cannot start" in saved.failure_reason


def test_manifest_projection_deduplicates_and_surfaces_attention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    task = SimpleNamespace(
        task_id="task",
        description="work",
        dependencies=("dependency",),
        status=SimpleNamespace(value="failed"),
        worker_session_id="worker",
        worktree_id="worktree",
        result=SimpleNamespace(status="complete", summary="done"),
        error="failed task",
        updated_at=1.0,
    )
    worktree = SimpleNamespace(
        worktree_id="worktree",
        path=str(tmp_path / "missing-worktree"),
        branch="agenthicc/task",
        base_commit="base",
        owner_session_id="parent",
        task_id="task",
        status=SimpleNamespace(value="orphaned"),
        head_commit="head",
        dirty=True,
        conflict_paths=("file.py",),
        last_error="orphaned",
        updated_at=2.0,
    )
    manifest = SimpleNamespace(
        run_id="run_manifest",
        parent_session_id="main",
        orchestration_id="orchestration",
        tasks=(task, task),
        worktrees=(worktree, worktree),
        status=SimpleNamespace(value="reviewing"),
    )

    class _ManifestStore:
        def list(self) -> list[object]:
            return [manifest]

    monkeypatch.setattr("agenthicc.worktrees.ManifestStore", _ManifestStore)
    run = GoalRun.create("Goal", repository=str(tmp_path), run_id="run_manifest").evolve(
        main_session_id="main"
    )
    ids, tasks, worktrees, attention = GoalRunManager._manifest_projection(run)
    assert ids == ("orchestration",)
    assert len(tasks) == len(worktrees) == 1
    assert tasks[0].result_status == "complete"
    assert "task task: failed" in attention
    assert "worktree worktree: orphaned" in attention
    assert any("path is missing" in item for item in attention)
    assert "orchestration orchestration: reviewing" in attention


def test_workflow_projection_joins_latest_checkpoint_and_reports_invalid_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.workflows.checkpoint import WorkflowCheckpoint

    workflow_root = tmp_path / ".agenthicc" / "sessions" / "main" / "workflows"
    workflow_root.mkdir(parents=True)
    checkpoint = WorkflowCheckpoint(
        run_id="workflow-run",
        workflow_name="goal_flow",
        conversation_id="main",
        intent="Goal",
        status="failed",
        current_phase="architecture",
        phase_index=1,
        phase_iteration=2,
        conversation_cursor=0,
        context={},
        plugin_fingerprint="fingerprint",
        failure_message="provider failed",
    )

    class _CheckpointStore:
        def __init__(self, _session_id: str) -> None:
            pass

        def list_run_ids(self) -> list[str]:
            return ["workflow-run"]

        def load(self, _run_id: str) -> WorkflowCheckpoint:
            return checkpoint

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        _CheckpointStore,
    )
    manager, _supervisor = _manager(tmp_path)
    run = manager.store.create(
        GoalRun.create("Goal", repository=str(tmp_path), run_id="run_checkpoint").evolve(
            main_session_id="main", workflow_name="goal_flow"
        )
    )
    projected = manager.projection(run.run_id)
    assert projected.workflow_run_id == "workflow-run"
    assert projected.agents == ()
    assert any("workflow workflow-run: failed" in item for item in projected.attention_reasons)
