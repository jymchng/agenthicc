from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore
from agenthicc.worktrees import (
    CoordinatorError,
    ManifestStore,
    ParallelCoordinator,
    WorktreeManager,
)
from agenthicc.worktrees.environment import EnvironmentTrustRequired, EnvironmentTrustStore
from agenthicc.worktrees.environment_actions import (
    available_session_actions,
    run_session_action,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, *, setup: bool = True) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Integration tests")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    (root / ".agenthicc").mkdir()
    command = json.dumps("mkdir -p generated && printf ready > generated/ready")
    manifest = (
        "version = 1\n"
        + (
            "[[setup]]\nid = 'bootstrap'\n"
            + f"command = {command}\n"
            + "expected_outputs = ['generated/ready']\n"
            if setup
            else ""
        )
        + "[actions.test]\ncommand = 'printf test-ok'\nnetwork = 'none_declared'\n"
    )
    (root / ".agenthicc" / "environment.toml").write_text(manifest, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


class _Launcher:
    def __init__(self, store: BackgroundStore) -> None:
        self.store = store
        self.request: dict[str, object] = {}

    def submit(self, **kwargs: object) -> BackgroundSession:
        self.request = kwargs
        session_id = kwargs.get("session_id")
        cwd = kwargs.get("cwd")
        receipts = kwargs.get("environment_receipts", ())
        assert isinstance(session_id, str) and isinstance(cwd, str)
        assert isinstance(receipts, tuple)
        session = BackgroundSession.create(
            session_id,
            title=str(kwargs.get("title", "worker")),
            cwd=cwd,
            workflow_name="",
            intent="worker task",
            role="worker",
            task_id=str(kwargs.get("task_id", "")),
            worktree_id=str(kwargs.get("worktree_id", "")),
            environment_receipts=receipts,
        )
        return self.store.create(session)


def _coordinator(root: Path, tmp_path: Path, launcher: _Launcher, trust: EnvironmentTrustStore):
    return ParallelCoordinator(
        root,
        parent_session_id="parent-session",
        store=ManifestStore(tmp_path / "orchestrations"),
        manager=WorktreeManager(root, root=tmp_path / "worktrees"),
        supervisor=launcher,
        environment_trust_store=trust,
    )


def test_worker_dispatch_runs_trusted_setup_and_records_provenance(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    session_store = BackgroundStore(tmp_path / "sessions")
    launcher = _Launcher(session_store)
    trust = EnvironmentTrustStore(tmp_path / "trust.json")
    from agenthicc.worktrees.environment import load_environment_manifest

    project_manifest = load_environment_manifest(root)
    assert project_manifest is not None
    trust.trust(project_manifest)
    coordinator = _coordinator(root, tmp_path, launcher, trust)
    plan = coordinator.create()
    coordinator.add_task(plan.orchestration_id, task_id="test", description="implement tests")

    task = coordinator.dispatch(plan.orchestration_id, "test")

    assert task.status.value == "running"
    worktree = Path(str(launcher.request["cwd"]))
    assert (worktree / "generated" / "ready").read_text() == "ready"
    session_id = str(launcher.request["session_id"])
    worker = session_store.get(session_id)
    assert len(worker.environment_receipts) == 1
    receipt = json.loads(worker.environment_receipts[0])
    assert receipt["manifest_digest"] == project_manifest.digest
    assert receipt["commands"][0]["command_id"] == "bootstrap"
    assert "mkdir -p generated" not in json.dumps(receipt)
    assert "generated/ready" not in worker.title


@pytest.mark.asyncio
async def test_workflow_tool_returns_recovery_ids_when_setup_needs_approval(
    tmp_path: Path,
) -> None:
    from agenthicc.worktrees.agent_tool import make_spawn_worker_agents_tool

    root = _repo(tmp_path)
    session_store = BackgroundStore(tmp_path / "sessions")
    launcher = _Launcher(session_store)
    orchestrations = ManifestStore(tmp_path / "orchestrations")
    tool = make_spawn_worker_agents_tool(
        repository=root,
        parent_session_id="parent-session",
        store=orchestrations,
        supervisor=launcher,
    )

    result = await tool(tasks=[{"task_id": "test", "description": "run setup", "dependencies": []}])

    assert result["ok"] is False
    orchestration_id = str(result["orchestration_id"])
    assert result["pending_task_ids"] == ["test"]
    assert result["recovery_commands"] == [
        f"agenthicc worktrees dispatch {orchestration_id} test --approve"
    ]
    assert orchestrations.get(orchestration_id).task("test").status.value == "pending"
    assert not launcher.request


def test_untrusted_setup_blocks_before_worktree_allocation_and_can_be_approved_once(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    session_store = BackgroundStore(tmp_path / "sessions")
    launcher = _Launcher(session_store)
    trust = EnvironmentTrustStore(tmp_path / "trust.json")
    coordinator = _coordinator(root, tmp_path, launcher, trust)
    plan = coordinator.create()
    coordinator.add_task(plan.orchestration_id, task_id="test", description="run setup")

    with pytest.raises(EnvironmentTrustRequired, match="command"):
        coordinator.dispatch(plan.orchestration_id, "test")
    pending = coordinator.manifest(plan.orchestration_id).task("test")
    assert pending.status.value == "pending"
    assert not pending.worktree_id
    assert not list((tmp_path / "worktrees").iterdir())

    dispatched = coordinator.dispatch(plan.orchestration_id, "test", environment_approved=True)
    assert dispatched.status.value == "running"
    session = session_store.get(dispatched.worker_session_id)
    assert len(session.environment_receipts) == 1


def test_setup_fails_closed_if_worker_manifest_differs_from_reviewed_manifest(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    session_store = BackgroundStore(tmp_path / "sessions")
    launcher = _Launcher(session_store)
    trust = EnvironmentTrustStore(tmp_path / "trust.json")
    from agenthicc.worktrees.environment import load_environment_manifest

    reviewed = load_environment_manifest(root)
    assert reviewed is not None
    trust.trust(reviewed)
    manager = WorktreeManager(root, root=tmp_path / "worktrees")
    create_worktree = manager.create

    def create_without_environment_manifest(**kwargs: object):
        record = create_worktree(**kwargs)  # type: ignore[arg-type]
        (Path(record.path) / ".agenthicc" / "environment.toml").unlink()
        return record

    manager.create = create_without_environment_manifest  # type: ignore[method-assign]
    coordinator = ParallelCoordinator(
        root,
        parent_session_id="parent-session",
        store=ManifestStore(tmp_path / "orchestrations"),
        manager=manager,
        supervisor=launcher,
        environment_trust_store=trust,
    )
    plan = coordinator.create()
    coordinator.add_task(plan.orchestration_id, task_id="test", description="run setup")

    with pytest.raises(CoordinatorError, match="manifest differs"):
        coordinator.dispatch(plan.orchestration_id, "test")

    task = coordinator.manifest(plan.orchestration_id).task("test")
    assert task.status.value == "pending"
    assert task.worktree_id
    assert not launcher.request


def test_failed_setup_receipt_is_durable_and_task_remains_retryable(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest_path = root / ".agenthicc" / "environment.toml"
    manifest_path.write_text(
        "version = 1\n[[setup]]\nid = 'bootstrap'\ncommand = 'exit 7'\n",
        encoding="utf-8",
    )
    _git(root, "add", ".agenthicc/environment.toml")
    _git(root, "commit", "--amend", "-qm", "failing environment setup")
    session_store = BackgroundStore(tmp_path / "sessions")
    launcher = _Launcher(session_store)
    trust = EnvironmentTrustStore(tmp_path / "trust.json")
    from agenthicc.worktrees.environment import load_environment_manifest

    parsed = load_environment_manifest(root)
    assert parsed is not None
    trust.trust(parsed)
    coordinator = _coordinator(root, tmp_path, launcher, trust)
    plan = coordinator.create()
    coordinator.add_task(plan.orchestration_id, task_id="test", description="run setup")

    with pytest.raises(CoordinatorError, match="command exited with status 7"):
        coordinator.dispatch(plan.orchestration_id, "test")

    manifest = coordinator.manifest(plan.orchestration_id)
    task = manifest.task("test")
    worktree = manifest.worktree(task.worktree_id)
    receipt = json.loads(worktree.environment_receipts[0])
    assert task.status.value == "pending"
    assert receipt["commands"][0]["exit_code"] == 7
    assert receipt["commands"][0]["command_id"] == "bootstrap"
    assert not launcher.request


def test_worker_actions_run_in_its_worktree_and_receipt_survives_store_reopen(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, setup=False)
    session_store = BackgroundStore(tmp_path / "sessions")
    launcher = _Launcher(session_store)
    trust = EnvironmentTrustStore(tmp_path / "trust.json")
    coordinator = _coordinator(root, tmp_path, launcher, trust)
    plan = coordinator.create()
    coordinator.add_task(plan.orchestration_id, task_id="test", description="run action")
    task = coordinator.dispatch(plan.orchestration_id, "test")
    worker = session_store.get(task.worker_session_id)

    actions = available_session_actions(
        worker, manifests=ManifestStore(tmp_path / "orchestrations")
    )
    assert [action.command_id for action in actions] == ["test"]
    receipt = run_session_action(
        worker.session_id,
        "test",
        approved=True,
        manifests=ManifestStore(tmp_path / "orchestrations"),
        sessions=session_store,
    )

    assert receipt.result == "succeeded"
    assert receipt.commands[0].stdout == "test-ok"
    reopened = BackgroundStore(tmp_path / "sessions").get(worker.session_id)
    assert len(reopened.environment_receipts) == 1
    persisted = json.loads(reopened.environment_receipts[0])
    assert persisted["kind"] == "action:test"
    assert "printf test-ok" not in json.dumps(persisted)
