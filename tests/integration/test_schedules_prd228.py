from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agenthicc.background.store import BackgroundStore
from agenthicc.background.supervisor import BackgroundSupervisor
from agenthicc.schedules import (
    CoalescePolicy,
    RecurrenceKind,
    ScheduleManager,
    ScheduleStore,
    build_background_dispatcher,
)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "schedule-test@example.invalid")
    _git(path, "config", "user.name", "Schedule Test")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "baseline", "-q")
    return path, _git(path, "rev-parse", "HEAD")


def test_mutating_schedule_dispatch_uses_recorded_isolated_worktree_and_exact_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base_commit = _repository(tmp_path / "repo")
    schedule_store = ScheduleStore(tmp_path / "schedule-state")
    manager = ScheduleManager(schedule_store, clock=lambda: 1_700_000_000.0)
    schedule = manager.create(
        repository_root=repository,
        workflow_name="test_workflow",
        prompt="  Exact prompt\nwith trailing newline\n",
        profile_name="test-profile",
        permission_ceiling="Safe",
        recurrence_kind=RecurrenceKind.INTERVAL,
        recurrence_spec="1h",
        timezone="UTC",
        coalesce_policy=CoalescePolicy.SKIP,
    )
    occurrence = manager.run_now(schedule.schedule_id)

    background_store = BackgroundStore(tmp_path / "background-state")
    supervisor = BackgroundSupervisor(background_store, max_workers=2)
    monkeypatch.setattr(
        supervisor,
        "_launch",
        lambda _request, session: background_store.get(session.session_id),
    )
    dispatcher = build_background_dispatcher(
        store=schedule_store,
        supervisor=supervisor,
        background_store=background_store,
    )

    receipt = dispatcher(schedule, occurrence)
    persisted = schedule_store.get_occurrence(occurrence.occurrence_id)
    assert persisted.base_commit == base_commit
    assert persisted.worktree is not None
    worktree_path = Path(str(persisted.worktree["path"]))
    assert worktree_path != repository
    assert _git(repository, "rev-parse", "HEAD") == base_commit
    assert _git(worktree_path, "rev-parse", "HEAD") == base_commit
    assert receipt["working_directory"] == str(worktree_path)
    assert receipt["permission_ceiling"] == "Safe"

    launched = background_store.get(occurrence.session_id)
    assert launched.cwd == str(worktree_path)
    assert launched.workflow_name == "test_workflow"
    assert launched.intent == "  Exact prompt\nwith trailing newline\n"
    assert launched.requested_mode_name == "Safe"
    assert launched.requested_profile_name == "test-profile"
    assert launched.worktree_id == persisted.worktree["worktree_id"]

    # Simulate a crash after Git and background admission but before the
    # scheduler records its final receipt. Stable IDs re-adopt both resources.
    second_receipt = dispatcher(schedule, occurrence)
    assert second_receipt["session_id"] == receipt["session_id"]
    assert len(background_store.list(include_archived=True)) == 1
    assert _git(repository, "worktree", "list", "--porcelain").count("worktree ") == 2


def test_read_only_plan_schedule_runs_in_repo_without_creating_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base_commit = _repository(tmp_path / "repo")
    schedule_store = ScheduleStore(tmp_path / "schedule-state")
    manager = ScheduleManager(schedule_store, clock=lambda: 1_700_000_000.0)
    schedule = manager.create(
        repository_root=repository,
        workflow_name="test_workflow",
        prompt="Inspect only",
        profile_name="test-profile",
        permission_ceiling="Plan",
        recurrence_kind=RecurrenceKind.INTERVAL,
        recurrence_spec="1h",
        timezone="UTC",
    )
    occurrence = manager.run_now(schedule.schedule_id)
    background_store = BackgroundStore(tmp_path / "background-state")
    supervisor = BackgroundSupervisor(background_store, max_workers=2)
    monkeypatch.setattr(
        supervisor,
        "_launch",
        lambda _request, session: background_store.get(session.session_id),
    )
    receipt = build_background_dispatcher(
        store=schedule_store,
        supervisor=supervisor,
        background_store=background_store,
    )(schedule, occurrence)
    persisted = schedule_store.get_occurrence(occurrence.occurrence_id)
    assert persisted.base_commit == base_commit
    assert persisted.worktree is None
    assert receipt["working_directory"] == str(repository)
    assert receipt["worktree_id"] == ""
    assert _git(repository, "worktree", "list", "--porcelain").count("worktree ") == 1
