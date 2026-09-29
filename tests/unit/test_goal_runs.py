from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession
from agenthicc.runs.manager import GoalRunManager, RepositoryValidationError
from agenthicc.runs.model import (
    GoalRun,
    GoalRunStatus,
    RunAgentRecord,
    RunTaskRecord,
    RunWorktreeRecord,
)
from agenthicc.runs.store import RunNotFound, RunStore


def test_goal_run_identity_is_separate_and_round_trips() -> None:
    run = GoalRun.create(
        "  Build an API  ",
        repository="/repo",
        workflow_name="goal_flow",
        run_id="run_test",
    )
    updated = run.evolve(status=GoalRunStatus.RUNNING, main_session_id="session-1")
    updated = updated.with_agent(
        RunAgentRecord(
            agent_id="session-1",
            run_id="run_test",
            role="main",
            process_id=4242,
            exit_code=0,
            exit_reason="workflow_complete",
        )
    )

    restored = GoalRun.from_mapping(updated.to_dict())

    assert restored.run_id == "run_test"
    assert restored.goal == "Build an API"
    assert restored.main_session_id == "session-1"
    assert restored.agents[0].role == "main"
    assert restored.agents[0].process_id == 4242
    assert restored.agents[0].exit_reason == "workflow_complete"


def test_goal_run_projection_records_tasks_and_worktrees() -> None:
    run = GoalRun.create("Build an API", repository="/repo", run_id="run_projection")
    updated = run.evolve(
        tasks=(
            RunTaskRecord(
                task_id="api",
                description="Implement the API",
                status="completed",
                worker_session_id="worker-1",
                worktree_id="wt-1",
                result_status="complete",
                result_summary="Tests passed",
            ),
        ),
        worktrees=(
            RunWorktreeRecord(
                worktree_id="wt-1",
                path="/tmp/wt-1",
                branch="agenthicc/api",
                base_commit="a" * 40,
                head_commit="b" * 40,
                status="integrated",
            ),
        ),
    )

    restored = GoalRun.from_mapping(updated.to_dict())

    assert restored.tasks[0].result_status == "complete"
    assert restored.worktrees[0].branch == "agenthicc/api"
    assert restored.worktrees[0].base_commit == "a" * 40


def test_run_store_folds_updates_and_ignores_partial_tail(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    run = store.create(GoalRun.create("Implement feature", repository="/repo", run_id="run_one"))
    store.update(run.run_id, status=GoalRunStatus.WAITING, attention_reasons=("input",))
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq":999,"event_type":"run_updated"')

    restored = store.get("run_one")
    assert restored.status == GoalRunStatus.WAITING
    assert restored.attention_reasons == ("input",)
    assert store.list(status="waiting")[0].run_id == "run_one"


def test_run_store_uses_private_registry_permissions(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.create(GoalRun.create("Implement feature", repository="/repo", run_id="run_one"))

    assert (store.root.stat().st_mode & 0o777) == 0o700
    assert (store.events_path.stat().st_mode & 0o777) == 0o600
    assert (store.lock_path.stat().st_mode & 0o777) == 0o600


def test_run_store_links_agent_without_losing_lifecycle_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    run = store.create(GoalRun.create("Implement feature", repository="/repo", run_id="run_one"))
    store.update(run.run_id, status=GoalRunStatus.RUNNING)
    store.link_agent(
        run.run_id,
        RunAgentRecord(agent_id="worker-1", run_id=run.run_id, task_id="api"),
    )

    restored = store.get(run.run_id)
    assert restored.status == GoalRunStatus.RUNNING
    assert [item.task_id for item in restored.agents] == ["api"]


def test_run_store_rejects_duplicates_and_filters_repository_and_status(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    first = store.create(
        GoalRun.create("First", repository="/repo-a", run_id="run_first").evolve(
            status=GoalRunStatus.COMPLETED
        )
    )
    second = store.create(
        GoalRun.create("Second", repository="/repo-b", run_id="run_second").evolve(
            status=GoalRunStatus.RUNNING
        )
    )
    with pytest.raises(ValueError, match="already exists"):
        store.create(first)
    with pytest.raises(RunNotFound):
        store.get("missing")
    assert [item.run_id for item in store.list(status="completed")] == ["run_first"]
    assert [item.run_id for item in store.list(repository="/repo-b")] == [second.run_id]
    with pytest.raises(ValueError):
        store.list(status="not-a-status")


def test_goal_run_creation_and_mapping_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="goal must not be empty"):
        GoalRun.create(" \n ", repository="/repo")
    with pytest.raises(ValueError, match="workflow_name"):
        GoalRun.create("goal", repository="/repo", workflow_name=" ")
    with pytest.raises(ValueError, match="run_id"):
        GoalRun.create("goal", repository="/repo", run_id="invalid")
    restored = GoalRun.from_mapping(
        {
            "run_id": "run_mapping",
            "status": "unknown",
            "attention": ["one", 2],
            "agents": [{"agent_id": ""}, {"agent_id": "agent"}],
            "tasks": [{"task_id": ""}],
            "worktrees": [{"worktree_id": ""}],
            "exit_code": True,
        }
    )
    assert restored.status is GoalRunStatus.NEEDS_ATTENTION
    assert restored.attention_reasons == ("one",)
    assert [agent.agent_id for agent in restored.agents] == ["agent"]
    assert restored.tasks == ()
    assert restored.worktrees == ()
    assert restored.exit_code is None


class _FakeSupervisor:
    def __init__(self, root: Path) -> None:
        from agenthicc.background import BackgroundStore

        self.store = BackgroundStore(root / "background")

    def submit(self, **kwargs: object) -> BackgroundSession:
        session = BackgroundSession.create(
            str(kwargs["session_id"]),
            title=str(kwargs["title"]),
            cwd=str(kwargs["cwd"]),
            workflow_name=str(kwargs["workflow_name"]),
            intent=str(kwargs["intent"]),
            run_id=str(kwargs.get("run_id", "")),
            role=str(kwargs.get("role", "")),
        )
        return self.store.create(session)


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)


def test_manager_starts_detached_run_through_supervisor(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git_repo(repository)
    supervisor = _FakeSupervisor(tmp_path)
    manager = GoalRunManager(
        store=RunStore(tmp_path / "runs"), supervisor=supervisor, require_git=True
    )

    run = manager.start_detached("Implement the API", repository=repository)

    assert run.run_id.startswith("run_")
    assert run.status == GoalRunStatus.RUNNING
    assert run.workflow_name == "goal_flow"
    assert run.main_session_id
    assert supervisor.store.get(run.main_session_id).run_id == run.run_id


def test_manager_rejects_non_repository(tmp_path: Path) -> None:
    manager = GoalRunManager(
        store=RunStore(tmp_path / "runs"), supervisor=_FakeSupervisor(tmp_path)
    )
    with pytest.raises(RepositoryValidationError):
        manager.create("Implement", repository=tmp_path)
