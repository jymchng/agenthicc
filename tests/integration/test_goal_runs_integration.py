from __future__ import annotations

from pathlib import Path

from agenthicc.background import BackgroundSession
from agenthicc.runs.manager import GoalRunManager
from agenthicc.runs.model import GoalRunStatus
from agenthicc.runs.store import RunStore
from agenthicc.worktrees import (
    ManifestStore,
    OrchestrationStatus,
    ParallelManifest,
    ParallelTask,
    TaskStatus,
    WorktreeRecord,
    WorktreeStatus,
)

from tests.unit.test_goal_runs import _FakeSupervisor, _git_repo


def test_projection_joins_worker_session_by_parent_session(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git_repo(repository)
    supervisor = _FakeSupervisor(tmp_path)
    manager = GoalRunManager(
        store=RunStore(tmp_path / "runs"), supervisor=supervisor, require_git=True
    )
    run = manager.start_detached("Implement the API", repository=repository)
    worker = BackgroundSession.create(
        "worker-1",
        title="API worker",
        cwd=str(repository),
        workflow_name="",
        intent="worker",
        parent_session_id=run.main_session_id,
        role="worker",
        task_id="api",
        worktree_id="wt-api",
    )
    supervisor.store.create(worker)

    projection = manager.projection(run.run_id)

    # The fake supervisor records the session but does not start its worker;
    # startup remains visible until the worker claims and initializes it.
    assert projection.status == GoalRunStatus.STARTING
    assert {item.task_id for item in projection.agents} == {"", "api"}
    assert any(item.role == "worker" and item.worktree_id == "wt-api" for item in projection.agents)


def test_projection_reconciles_manifest_tasks_worktrees_and_conflicts(
    tmp_path: Path, monkeypatch: object
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git_repo(repository)
    supervisor = _FakeSupervisor(tmp_path)
    manager = GoalRunManager(
        store=RunStore(tmp_path / "runs"), supervisor=supervisor, require_git=True
    )
    run = manager.start_detached("Implement the API", repository=repository)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    ManifestStore().create(
        ParallelManifest(
            orchestration_id="orchestration-1",
            parent_session_id=run.main_session_id,
            run_id=run.run_id,
            repository=str(repository),
            parent_branch="main",
            base_commit="a" * 40,
            status=OrchestrationStatus.REVIEWING,
            tasks=(
                ParallelTask(
                    task_id="api",
                    description="Implement the API",
                    status=TaskStatus.CONFLICT,
                    worker_session_id="worker-1",
                    worktree_id="wt-1",
                    error="conflict",
                ),
            ),
            worktrees=(
                WorktreeRecord(
                    worktree_id="wt-1",
                    repository=str(repository),
                    path=str(tmp_path / "missing-worktree"),
                    branch="agenthicc/api",
                    base_commit="a" * 40,
                    owner_session_id=run.main_session_id,
                    task_id="api",
                    status=WorktreeStatus.CONFLICT,
                ),
            ),
        )
    )

    projection = manager.projection(run.run_id)

    assert projection.orchestration_ids == ("orchestration-1",)
    assert projection.tasks[0].status == "conflict"
    assert projection.worktrees[0].path.endswith("missing-worktree")
    assert projection.status == GoalRunStatus.NEEDS_ATTENTION
    assert projection.attention_reasons
