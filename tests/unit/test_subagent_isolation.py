"""SUBAGENT-15: per-worker git worktrees, snapshot base, and patch integration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agenthicc.subagents.isolation import IsolationError, PoolIsolation, git_repository_for
from agenthicc.worktrees.manager import WorktreeDirtyError, WorktreeManager

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "shared.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    (root / "notes.md").write_text("notes\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _isolation(repo: Path, tmp_path: Path, pool: str = "poolpool") -> PoolIsolation:
    return PoolIsolation(repo, session_id="sess", pool_id=pool, worktree_root=tmp_path / "wt")


def _branches(repo: Path) -> list[str]:
    return [
        line.strip("* ") for line in _git(repo, "branch", "--format=%(refname:short)").splitlines()
    ]


def _worktrees(repo: Path) -> list[str]:
    return [
        line.split(" ", 1)[1]
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


# ── snapshot ──────────────────────────────────────────────────────────────────


def test_a_clean_repository_snapshots_to_head(repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(repo, root=tmp_path / "wt")
    assert manager.snapshot_commit() == _git(repo, "rev-parse", "HEAD")


def test_a_dirty_repository_snapshots_its_working_state_without_touching_it(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "notes.md").write_text("edited, uncommitted\n")
    (repo / "fresh.txt").write_text("untracked\n")
    (repo / "ignored.log").write_text("ignored\n")
    (repo / ".gitignore").write_text("*.log\n")
    head = _git(repo, "rev-parse", "HEAD")
    status_before = _git(repo, "status", "--porcelain")

    snapshot = WorktreeManager(repo, root=tmp_path / "wt").snapshot_commit()

    assert snapshot != head
    assert _git(repo, "show", f"{snapshot}:notes.md") == "edited, uncommitted"
    assert _git(repo, "show", f"{snapshot}:fresh.txt") == "untracked"
    assert "ignored.log" not in _git(repo, "ls-tree", "-r", "--name-only", snapshot)
    assert _git(repo, "rev-parse", "HEAD") == head  # branch not moved
    assert _git(repo, "status", "--porcelain") == status_before  # index and tree untouched
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_the_default_create_still_refuses_a_dirty_repository(repo: Path, tmp_path: Path) -> None:
    (repo / "notes.md").write_text("dirty\n")
    with pytest.raises(WorktreeDirtyError):
        WorktreeManager(repo, root=tmp_path / "wt").create(parent_session_id="s", task_id="t")


# ── prepare / harvest / integrate / cleanup ───────────────────────────────────


async def test_workers_start_from_the_parents_uncommitted_work(repo: Path, tmp_path: Path) -> None:
    (repo / "notes.md").write_text("edited, uncommitted\n")
    (repo / "fresh.txt").write_text("untracked\n")
    isolation = _isolation(repo, tmp_path)

    workspaces = await isolation.prepare(["task-0", "task-1"])

    for workspace in workspaces.values():
        assert (workspace.path / "notes.md").read_text() == "edited, uncommitted\n"
        assert (workspace.path / "fresh.txt").read_text() == "untracked\n"
    assert workspaces["task-0"].path != workspaces["task-1"].path
    await isolation.cleanup(frozenset())


async def test_two_workers_on_different_files_are_both_applied(repo: Path, tmp_path: Path) -> None:
    isolation = _isolation(repo, tmp_path)
    workspaces = await isolation.prepare(["task-0", "task-1"])
    (workspaces["task-0"].path / "alpha.py").write_text("ALPHA\n")
    (workspaces["task-1"].path / "beta.py").write_text("BETA\n")

    harvests = [await isolation.harvest(task) for task in ("task-0", "task-1")]
    results = await isolation.integrate([("task-0", True), ("task-1", True)])
    await isolation.cleanup(
        {task for task, r in results.items() if r.state in {"conflict", "kept"}}
    )

    assert [h.changed_paths for h in harvests] == [("alpha.py",), ("beta.py",)]
    assert {task: r.state for task, r in results.items()} == {
        "task-0": "applied",
        "task-1": "applied",
    }
    assert (repo / "alpha.py").read_text() == "ALPHA\n"
    assert (repo / "beta.py").read_text() == "BETA\n"
    assert _worktrees(repo) == [str(repo)]  # nothing left behind
    assert _branches(repo) == ["main"]


async def test_overlapping_edits_apply_the_first_and_report_the_second(
    repo: Path, tmp_path: Path
) -> None:
    isolation = _isolation(repo, tmp_path)
    workspaces = await isolation.prepare(["task-0", "task-1"])
    (workspaces["task-0"].path / "shared.py").write_text(
        "def a():\n    return 'A'\n\n\ndef b():\n    return 2\n"
    )
    (workspaces["task-1"].path / "shared.py").write_text(
        "def a():\n    return 'B'\n\n\ndef b():\n    return 2\n"
    )
    for task in ("task-0", "task-1"):
        await isolation.harvest(task)

    results = await isolation.integrate([("task-0", True), ("task-1", True)])
    preserved = {task for task, r in results.items() if r.state in {"conflict", "kept"}}
    await isolation.cleanup(preserved)

    assert results["task-0"].state == "applied"
    assert results["task-1"].state == "conflict"
    assert results["task-1"].conflict_paths == ("shared.py",)
    assert "return 'A'" in (repo / "shared.py").read_text()  # the first worker's edit only
    assert "return 'B'" not in (repo / "shared.py").read_text()
    # The conflicting worker's work is recoverable.
    kept = Path(results["task-1"].workspace)
    assert "return 'B'" in (kept / "shared.py").read_text()
    assert results["task-1"].branch in _branches(repo)
    assert str(kept) in _worktrees(repo)


async def test_edits_to_different_regions_of_one_file_compose(repo: Path, tmp_path: Path) -> None:
    isolation = _isolation(repo, tmp_path)
    workspaces = await isolation.prepare(["task-0", "task-1"])
    base = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    (workspaces["task-0"].path / "shared.py").write_text(base.replace("return 1", "return 'A'"))
    (workspaces["task-1"].path / "shared.py").write_text(base.replace("return 2", "return 'B'"))
    for task in ("task-0", "task-1"):
        await isolation.harvest(task)

    results = await isolation.integrate([("task-0", True), ("task-1", True)])
    await isolation.cleanup(frozenset())

    assert [r.state for r in results.values()] == ["applied", "applied"]
    assert (repo / "shared.py").read_text() == base.replace("return 1", "return 'A'").replace(
        "return 2", "return 'B'"
    )


async def test_the_parents_uncommitted_work_survives_integration(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "notes.md").write_text("parent edit\n")
    (repo / "scratch.txt").write_text("parent scratch\n")
    head = _git(repo, "rev-parse", "HEAD")
    isolation = _isolation(repo, tmp_path)
    workspaces = await isolation.prepare(["task-0"])
    (workspaces["task-0"].path / "alpha.py").write_text("ALPHA\n")
    await isolation.harvest("task-0")

    results = await isolation.integrate([("task-0", True)])
    await isolation.cleanup(frozenset())

    assert results["task-0"].state == "applied"
    assert (repo / "notes.md").read_text() == "parent edit\n"
    assert (repo / "scratch.txt").read_text() == "parent scratch\n"
    assert (repo / "alpha.py").read_text() == "ALPHA\n"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "diff", "--cached", "--name-only") == ""  # the index is the parent's own


async def test_a_change_made_by_a_command_is_harvested(repo: Path, tmp_path: Path) -> None:
    isolation = _isolation(repo, tmp_path)
    workspaces = await isolation.prepare(["task-0"])
    subprocess.run(
        ["sh", "-c", "echo generated > made-by-command.txt && rm notes.md"],
        cwd=workspaces["task-0"].path,
        check=True,
    )

    harvest = await isolation.harvest("task-0")
    await isolation.cleanup(frozenset())

    assert set(harvest.changed_paths) == {"made-by-command.txt", "notes.md"}


async def test_a_failed_worker_with_changes_is_kept_and_one_without_leaves_nothing(
    repo: Path, tmp_path: Path
) -> None:
    isolation = _isolation(repo, tmp_path)
    workspaces = await isolation.prepare(["task-0", "task-1"])
    (workspaces["task-0"].path / "half-done.py").write_text("WIP\n")
    for task in ("task-0", "task-1"):
        await isolation.harvest(task)

    results = await isolation.integrate([("task-0", False), ("task-1", False)])
    preserved = {task for task, r in results.items() if r.state in {"conflict", "kept"}}
    await isolation.cleanup(preserved)

    assert results["task-0"].state == "kept"
    assert results["task-1"].state == "none"
    assert not (repo / "half-done.py").exists()  # never applied
    assert len(_worktrees(repo)) == 2  # the repository and the kept worker
    assert Path(results["task-0"].workspace, "half-done.py").exists()


async def test_a_binary_file_round_trips(repo: Path, tmp_path: Path) -> None:
    isolation = _isolation(repo, tmp_path)
    workspaces = await isolation.prepare(["task-0"])
    payload = bytes(range(256)) * 4
    (workspaces["task-0"].path / "blob.bin").write_bytes(payload)
    await isolation.harvest("task-0")

    results = await isolation.integrate([("task-0", True)])
    await isolation.cleanup(frozenset())

    assert results["task-0"].state == "applied"
    assert (repo / "blob.bin").read_bytes() == payload


async def test_a_workspace_inside_a_subdirectory_gets_the_same_subdirectory(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pkg")
    isolation = PoolIsolation(
        repo / "pkg", session_id="sess", pool_id="poolpool", worktree_root=tmp_path / "wt"
    )

    workspaces = await isolation.prepare(["task-0"])
    (workspaces["task-0"].path / "mod.py").write_text("x = 2\n")
    harvest = await isolation.harvest("task-0")
    results = await isolation.integrate([("task-0", True)])
    await isolation.cleanup(frozenset())

    assert workspaces["task-0"].path.name == "pkg"
    assert harvest.changed_paths == ("pkg/mod.py",)  # repository-relative
    assert results["task-0"].state == "applied"
    assert (repo / "pkg" / "mod.py").read_text() == "x = 2\n"


async def test_cleanup_removes_every_worktree_and_branch(repo: Path, tmp_path: Path) -> None:
    isolation = _isolation(repo, tmp_path)
    await isolation.prepare(["task-0", "task-1", "task-2"])
    assert len(_worktrees(repo)) == 4

    await isolation.cleanup(frozenset())

    assert _worktrees(repo) == [str(repo)]
    assert _branches(repo) == ["main"]


def test_a_directory_outside_git_has_no_repository_and_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    assert git_repository_for(plain) is None
    with pytest.raises(IsolationError):
        PoolIsolation(plain, session_id="s", pool_id="p", worktree_root=tmp_path / "wt")
