"""Unit and integration-style coverage for PRD-203 worktree orchestration."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from agenthicc.worktrees import (
    CoordinatorError,
    ManifestStore,
    ParallelCoordinator,
    TaskStatus,
    WorktreeDirtyError,
    WorktreeManager,
    WorktreeStatus,
)
from agenthicc.worktrees.agent_tool import make_spawn_worker_agents_tool
from agenthicc.background.model import BackgroundSession


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "agenthicc-tests@example.invalid")
    _git(repo, "config", "user.name", "agenthicc tests")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, tmp_path / "durable"


def test_manifest_store_round_trips_tasks_and_worktrees(repository: tuple[Path, Path]) -> None:
    repo, durable = repository
    manager = WorktreeManager(repo, root=durable / "worktrees")
    coordinator = ParallelCoordinator(
        repo,
        parent_session_id="parent",
        manager=manager,
        store=ManifestStore(durable / "manifests"),
    )
    coordinator.create(orchestration_id="orchestration")
    coordinator.add_task("orchestration", task_id="api", description="Implement the API")
    coordinator.add_task(
        "orchestration",
        task_id="ui",
        description="Implement the UI after the API",
        dependencies=("api",),
    )

    restored = ManifestStore(durable / "manifests").get("orchestration")
    assert [task.task_id for task in restored.tasks] == ["api", "ui"]
    assert restored.tasks[1].dependencies == ("api",)
    assert restored.base_commit == _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_spawn_worker_agents_schema_exposes_resolved_task_fields() -> None:
    from lauren_ai._tools._schema import generate_tool_schema

    tool = make_spawn_worker_agents_tool(repository=".", parent_session_id="parent")
    schema = generate_tool_schema(tool)[2]["input_schema"]
    task_schema = schema["properties"]["tasks"]["items"]

    assert task_schema["properties"]["task_id"] == {"type": "string"}
    assert task_schema["properties"]["description"] == {"type": "string"}
    assert task_schema["properties"]["dependencies"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert schema["properties"]["max_concurrent"]["default"] == 4


def test_background_worker_metadata_round_trips_without_affecting_normal_jobs() -> None:
    worker = BackgroundSession.create(
        "worker-session",
        title="Worker",
        cwd="/tmp/worker",
        workflow_name="code_plan",
        intent="Implement task",
        parent_session_id="parent-session",
        role="worker",
        task_id="task-a",
        worktree_id="worktree-a",
        branch="agenthicc/parent/task-a",
        base_commit="0123456789abcdef",
    )
    restored = BackgroundSession.from_mapping(worker.to_dict())
    assert restored.parent_session_id == "parent-session"
    assert restored.role == "worker"
    assert restored.task_id == "task-a"
    assert restored.worktree_id == "worktree-a"
    assert restored.branch == "agenthicc/parent/task-a"
    assert restored.base_commit == "0123456789abcdef"


def test_dirty_coordinator_is_rejected(repository: tuple[Path, Path]) -> None:
    repo, durable = repository
    (repo / "uncommitted.txt").write_text("do not lose me\n", encoding="utf-8")
    manager = WorktreeManager(repo, root=durable / "worktrees")

    with pytest.raises(WorktreeDirtyError):
        manager.create(parent_session_id="parent", task_id="task")


def test_worker_isolated_and_integrates_without_mutating_coordinator(
    repository: tuple[Path, Path],
) -> None:
    repo, durable = repository
    manager = WorktreeManager(repo, root=durable / "worktrees")
    store = ManifestStore(durable / "manifests")
    coordinator = ParallelCoordinator(
        repo, parent_session_id="parent", manager=manager, store=store
    )
    coordinator.create(orchestration_id="orchestration")
    coordinator.add_task("orchestration", task_id="task", description="Change the shared file")
    task = coordinator.dispatch("orchestration", "task")
    record = store.get("orchestration").worktree(task.worktree_id)

    assert store.get("orchestration").task("task").status == TaskStatus.RUNNING
    assert Path(record.path).is_dir()
    assert record.branch.startswith("agenthicc/parent/task-")
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert (Path(record.path) / "shared.txt").read_text(encoding="utf-8") == "base\n"

    (Path(record.path) / "shared.txt").write_text("worker\n", encoding="utf-8")
    _git(Path(record.path), "add", ".")
    _git(Path(record.path), "commit", "-qm", "worker change")
    result = coordinator.inspect_task("orchestration", "task")
    assert result.clean is True
    assert result.status == "complete"
    assert result.base_commit == record.base_commit
    assert result.changed_files == ("shared.txt",)

    integrated = coordinator.integrate_task("orchestration", "task")
    assert integrated.ok is True
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "worker\n"
    saved = store.get("orchestration")
    assert saved.task("task").status == TaskStatus.INTEGRATED
    assert saved.worktree(record.worktree_id).status == WorktreeStatus.INTEGRATED

    coordinator.cleanup_task("orchestration", "task")
    assert not Path(record.path).exists()
    assert store.get("orchestration").task("task").status == TaskStatus.CLEANED


def test_merge_conflict_preserves_worker_and_aborts_coordinator_merge(
    repository: tuple[Path, Path],
) -> None:
    repo, durable = repository
    manager = WorktreeManager(repo, root=durable / "worktrees")
    store = ManifestStore(durable / "manifests")
    coordinator = ParallelCoordinator(
        repo, parent_session_id="parent", manager=manager, store=store
    )
    coordinator.create(orchestration_id="orchestration")
    coordinator.add_task("orchestration", task_id="task", description="Conflict intentionally")
    task = coordinator.dispatch("orchestration", "task")
    record = store.get("orchestration").worktree(task.worktree_id)

    (Path(record.path) / "shared.txt").write_text("worker\n", encoding="utf-8")
    _git(Path(record.path), "add", ".")
    _git(Path(record.path), "commit", "-qm", "worker")
    (repo / "shared.txt").write_text("coordinator\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "coordinator")
    result = coordinator.inspect_task("orchestration", "task")
    assert result.status == "complete"

    integrated = coordinator.integrate_task("orchestration", "task")
    assert integrated.ok is False
    assert integrated.conflict_paths == ("shared.txt",)
    assert Path(record.path).is_dir()
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert store.get("orchestration").worktree(record.worktree_id).status == WorktreeStatus.CONFLICT


def test_integration_rejects_a_changed_coordinator_branch(
    repository: tuple[Path, Path],
) -> None:
    repo, durable = repository
    manager = WorktreeManager(repo, root=durable / "worktrees")
    store = ManifestStore(durable / "manifests")
    coordinator = ParallelCoordinator(
        repo, parent_session_id="parent", manager=manager, store=store
    )
    coordinator.create(orchestration_id="orchestration")
    coordinator.add_task("orchestration", task_id="task", description="Change a file")
    task = coordinator.dispatch("orchestration", "task")
    record = store.get("orchestration").worktree(task.worktree_id)
    (Path(record.path) / "shared.txt").write_text("worker\n", encoding="utf-8")
    _git(Path(record.path), "add", ".")
    _git(Path(record.path), "commit", "-qm", "worker")
    _git(repo, "checkout", "-qb", "unexpected")
    coordinator.inspect_task("orchestration", "task")

    with pytest.raises(CoordinatorError, match="parent branch"):
        coordinator.integrate_task("orchestration", "task")


def test_dependency_graph_rejects_cycles(repository: tuple[Path, Path]) -> None:
    repo, durable = repository
    manager = WorktreeManager(repo, root=durable / "worktrees")
    store = ManifestStore(durable / "manifests")
    coordinator = ParallelCoordinator(
        repo, parent_session_id="parent", manager=manager, store=store
    )
    coordinator.create(orchestration_id="orchestration")
    coordinator.add_task("orchestration", task_id="a", description="A")

    with pytest.raises(Exception, match="unknown task dependencies"):
        coordinator.add_task(
            "orchestration",
            task_id="b",
            description="B",
            dependencies=("missing",),
        )


@pytest.mark.asyncio
async def test_workflow_wide_worker_tool_dispatches_durable_agents(
    repository: tuple[Path, Path],
) -> None:
    repo, durable = repository
    store = ManifestStore(durable / "manifests")

    class FakeSupervisor:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def submit(self, **kwargs: object) -> BackgroundSession:
            self.calls.append(kwargs)
            return BackgroundSession.create(
                str(kwargs["session_id"]),
                title=str(kwargs.get("title", "worker")),
                cwd=str(kwargs["cwd"]),
                workflow_name=str(kwargs.get("workflow_name", "")),
                intent=str(kwargs["intent"]),
            )

    supervisor = FakeSupervisor()
    tool = make_spawn_worker_agents_tool(
        repository=repo,
        parent_session_id="parent",
        max_parallel_tasks=2,
        store=store,
        supervisor=supervisor,
        workspace_access=SimpleNamespace(
            scope=SimpleNamespace(primary_root=repo),
        ),
    )

    result = await tool(
        tasks=[
            {"task_id": "api", "description": "Implement the API", "dependencies": None},
            {"task_id": "ui", "description": "Implement the UI", "dependencies": None},
        ]
    )

    assert result["ok"] is True
    assert result["dispatched"] == ["api", "ui"]
    assert len(supervisor.calls) == 2
    assert all(call["workflow_name"] == "" for call in supervisor.calls)
    manifest = store.get(str(result["orchestration_id"]))
    assert {task.status for task in manifest.tasks} == {TaskStatus.RUNNING}
    assert all(Path(str(call["cwd"])).is_dir() for call in supervisor.calls)
