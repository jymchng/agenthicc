"""CLI and process-restart journeys for repository schedules (PRD-228)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from agenthicc.schedules import RecurrenceKind, ScheduleManager, ScheduleStore, format_timestamp
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout


def _run_cli(
    repository: Path,
    home: Path,
    config_path: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (source_root, environment.get("PYTHONPATH", "")))
    )
    return subprocess.run(
        [sys.executable, "-m", "agenthicc", "--config", str(config_path), *args],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )


def _create_repo(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "project"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    project_config = repository / ".agenthicc"
    project_config.mkdir()
    config_path = project_config / "agenthicc.toml"
    config_path.write_text(
        "[execution]\nprofile = 'local'\n\n"
        "[providers.local]\nprovider = 'openai'\n"
        "model = 'test-model'\nbase_url = 'https://example.invalid/v1'\n",
        encoding="utf-8",
    )
    return repository, config_path


@cli_test_timeout(11)
def test_cli_schedule_lifecycle_persists_revisions_and_history_across_processes(
    tmp_path: Path,
) -> None:
    repository, config_path = _create_repo(tmp_path)
    home = tmp_path / "home"
    created = _run_cli(
        repository,
        home,
        config_path,
        "schedules",
        "create",
        "code_plan",
        "local",
        "Plan",
        "interval:2h",
        "UTC",
        "  Preserve this exact prompt.\n",
        "--repository-root",
        str(repository),
    )
    assert created.returncode == 0, created.stderr
    schedule_id = re.search(r"Created disabled schedule ([0-9a-f]+)", created.stdout)
    assert schedule_id is not None
    identity = schedule_id.group(1)
    assert "First execution preview:" in created.stdout

    listed = _run_cli(
        repository,
        home,
        config_path,
        "schedules",
        "list",
        "--repository-root",
        str(repository),
        "--json",
    )
    assert listed.returncode == 0, listed.stderr
    list_payload = json.loads(listed.stdout)
    assert list_payload[0]["schedule_id"] == identity
    assert list_payload[0]["status"] == "paused"

    preview = _run_cli(repository, home, config_path, "schedules", "resume", identity)
    assert preview.returncode == 0, preview.stderr
    next_run_match = re.search(r"Next execution preview: (\S+) \[UTC\]", preview.stdout)
    assert next_run_match is not None
    confirmed = _run_cli(
        repository,
        home,
        config_path,
        "schedules",
        "resume",
        identity,
        "--confirm-next-run",
        next_run_match.group(1),
    )
    assert confirmed.returncode == 0, confirmed.stderr

    shown = _run_cli(repository, home, config_path, "schedules", "show", identity, "--json")
    assert shown.returncode == 0, shown.stderr
    show_payload = json.loads(shown.stdout)
    assert show_payload["enabled"] is True
    assert show_payload["prompt"] == "  Preserve this exact prompt.\n"
    assert [entry["action"] for entry in show_payload["revisions"]] == ["created", "resumed"]
    assert show_payload["service_running"] is False

    run_now = _run_cli(repository, home, config_path, "schedules", "run-now", identity, "--json")
    assert run_now.returncode == 0, run_now.stderr
    occurrence = json.loads(run_now.stdout)
    assert occurrence["state"] == "awaiting_service"
    assert occurrence["session_id"]

    # A confirmed edit revokes the old enablement; a new exact preview must
    # be reviewed before any scheduler can dispatch the revised definition.
    rejected_update = _run_cli(
        repository,
        home,
        config_path,
        "schedules",
        "update",
        identity,
        "workflow_name",
        "not-a-registered-workflow",
        "--confirm",
    )
    assert rejected_update.returncode != 0
    assert "not registered" in rejected_update.stderr

    updated = _run_cli(
        repository,
        home,
        config_path,
        "schedules",
        "update",
        identity,
        "prompt",
        "Revised exact prompt",
        "--confirm",
    )
    assert updated.returncode == 0, updated.stderr
    assert "schedule is paused" in updated.stdout

    deleted = _run_cli(
        repository,
        home,
        config_path,
        "schedules",
        "delete",
        identity,
        "--confirm",
    )
    assert deleted.returncode == 0, deleted.stderr
    final = _run_cli(repository, home, config_path, "schedules", "show", identity, "--json")
    assert final.returncode == 0, final.stderr
    final_payload = json.loads(final.stdout)
    assert final_payload["deleted_at"] is not None
    assert len(final_payload["history"]) == 1
    assert final_payload["history"][0]["occurrence_id"] == occurrence["occurrence_id"]
    remaining = _run_cli(
        repository,
        home,
        config_path,
        "schedules",
        "list",
        "--repository-root",
        str(repository),
        "--json",
    )
    assert remaining.returncode == 0, remaining.stderr
    assert json.loads(remaining.stdout) == []


@cli_test_timeout(2)
def test_service_process_restart_recovers_claimed_occurrence_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    repository.mkdir()
    store = ScheduleStore(home / ".agenthicc" / "schedules")
    manager = ScheduleManager(store, clock=lambda: 1_700_000_000.0)
    schedule = manager.create(
        repository_root=repository,
        workflow_name="code_plan",
        prompt="Restart safely",
        profile_name="local",
        permission_ceiling="Plan",
        recurrence_kind=RecurrenceKind.INTERVAL,
        recurrence_spec="1s",
        timezone="UTC",
    )
    manager.resume(
        schedule.schedule_id,
        confirm_next_run=format_timestamp(schedule.next_run_at, schedule.timezone),
    )

    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (str(Path(__file__).parents[2] / "src"), environment.get("PYTHONPATH", "")),
        )
    )
    marker = tmp_path / "dispatch.marker"
    base_script = """
import asyncio, json, os, time
from pathlib import Path
from agenthicc.schedules import ScheduleScheduler, ScheduleStore

mode = os.environ['SCHEDULE_TEST_MODE']
now = float(os.environ['SCHEDULE_TEST_NOW'])
marker = Path(os.environ['SCHEDULE_TEST_MARKER'])
store = ScheduleStore()
stop = asyncio.Event()
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

def dispatch(schedule, occurrence):
    record = {'occurrence_id': occurrence.occurrence_id, 'session_id': occurrence.session_id}
    if mode == 'interrupt':
        marker.write_text(json.dumps(record), encoding='utf-8')
        while True:
            time.sleep(0.05)
    marker.write_text(json.dumps(record), encoding='utf-8')
    loop.call_soon_threadsafe(stop.set)
    return {'status': 'running', 'session_id': occurrence.session_id}

async def run():
    scheduler = ScheduleScheduler(store, dispatch, clock=lambda: now)
    await scheduler.run(stop=stop, poll_interval_s=0.1)

loop.run_until_complete(run())
loop.close()
"""

    def child_env(mode: str) -> dict[str, str]:
        result = dict(environment)
        result["SCHEDULE_TEST_MODE"] = mode
        result["SCHEDULE_TEST_NOW"] = str(schedule.next_run_at)
        result["SCHEDULE_TEST_MARKER"] = str(marker)
        return result

    interrupted = subprocess.Popen(
        [sys.executable, "-c", base_script],
        cwd=tmp_path,
        env=child_env("interrupt"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S
    try:
        while time.monotonic() < deadline and not marker.exists():
            if interrupted.poll() is not None:
                stdout, stderr = interrupted.communicate(timeout=2)
                raise AssertionError(f"scheduler exited before dispatch: {stdout}\n{stderr}")
            time.sleep(0.05)
        assert marker.exists(), "scheduler never reached its claimed dispatch"
    finally:
        interrupted.terminate()
        interrupted.wait(timeout=5)
        if interrupted.stdout is not None:
            interrupted.stdout.close()
        if interrupted.stderr is not None:
            interrupted.stderr.close()

    first_dispatch = json.loads(marker.read_text(encoding="utf-8"))
    claimed = store.get_occurrence(first_dispatch["occurrence_id"])
    assert claimed.session_id == first_dispatch["session_id"]
    assert claimed.state.value == "preparing"

    restarted = subprocess.run(
        [sys.executable, "-c", base_script],
        cwd=tmp_path,
        env=child_env("recover"),
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert restarted.returncode == 0, restarted.stderr
    recovered_dispatch = json.loads(marker.read_text(encoding="utf-8"))
    assert recovered_dispatch == first_dispatch
    history = store.history(schedule.schedule_id)
    assert len(history) == 1
    assert history[0].occurrence_id == first_dispatch["occurrence_id"]
    assert history[0].session_id == first_dispatch["session_id"]
    assert history[0].state.value == "running"
