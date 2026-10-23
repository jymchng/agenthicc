"""Multi-process regressions for PRD-171's duplicate --continue race."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = pytest.mark.integration


_OWNER_PROGRAM = r"""
import json
import pathlib
import sys
import time

from agenthicc.runners.session_lease import SessionOpenCoordinator

root = pathlib.Path(sys.argv[1])
cwd = pathlib.Path(sys.argv[2])
start = pathlib.Path(sys.argv[3])
pathlib.Path(sys.argv[4]).touch()  # imported and waiting: ready to race
while not start.exists():
    time.sleep(0.005)
try:
    selected = SessionOpenCoordinator(root).select_latest_for_cwd(cwd, entrypoint="tui")
    if selected is None:
        raise RuntimeError("no session selected")
    session_id, lease = selected
    print(json.dumps({"result": "winner", "session_id": session_id}), flush=True)
    time.sleep(2.0)
    lease.release()
except Exception as exc:
    print(
        json.dumps(
            {
                "result": "error",
                "type": type(exc).__name__,
                "code": getattr(exc, "code", ""),
                "session_id": getattr(exc, "session_id", ""),
            }
        ),
        flush=True,
    )
    raise SystemExit(getattr(exc, "exit_code", 1))
"""


def _start_owner(root: Path, cwd: Path, start: Path, ready: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _OWNER_PROGRAM, str(root), str(cwd), str(start), str(ready)],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@cli_test_timeout(3)
def test_two_processes_racing_for_latest_session_have_one_owner(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    cwd = tmp_path / "project"
    session_id = "latest-session"
    start = tmp_path / "start"
    cwd.mkdir()
    (root / session_id).mkdir(parents=True)
    (root / "index.json").write_text(
        json.dumps({session_id: {"cwd": str(cwd), "last_active": 100.0}}),
        encoding="utf-8",
    )

    first_ready = tmp_path / "first-ready"
    second_ready = tmp_path / "second-ready"
    first = _start_owner(root, cwd, start, first_ready)
    second = _start_owner(root, cwd, start, second_ready)
    # Start the race only once both interpreters have imported and are
    # waiting; otherwise a slow start lets the first winner release its lease
    # before the second process even asks for it.
    deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S
    while not (first_ready.exists() and second_ready.exists()):
        assert time.monotonic() < deadline, "the owner processes never became ready"
        time.sleep(0.01)
    start.touch()
    first_out, first_err = first.communicate(timeout=CLI_PROCESS_TIMEOUT_S)
    second_out, second_err = second.communicate(timeout=CLI_PROCESS_TIMEOUT_S)
    assert first_err == "" or "RuntimeWarning" not in first_err
    assert second_err == "" or "RuntimeWarning" not in second_err

    records = [json.loads(line) for line in (first_out + second_out).splitlines() if line]
    assert [record["result"] for record in records].count("winner") == 1
    conflicts = [record for record in records if record["result"] == "error"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "SessionAlreadyActiveError"
    assert conflicts[0]["code"] == "session_already_active"
    assert conflicts[0]["session_id"] == session_id
    assert sorted((first.returncode, second.returncode)) == [0, 3]
    assert not (root / session_id / ".owner").exists()


@cli_test_timeout(1)
def test_sigkill_owner_is_reclaimable_without_timeout(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session_id = "crashed-session"
    session_dir = root / session_id
    session_dir.mkdir(parents=True)
    program = r"""
import pathlib
import sys
from agenthicc.runners.session_lease import SessionOwnerStore

lease = SessionOwnerStore(pathlib.Path(sys.argv[1])).acquire(sys.argv[2])
print(lease.owner_id, flush=True)
while True:
    pass
"""
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(root), session_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert child.stdout is not None
    owner_id = child.stdout.readline().strip()
    assert owner_id
    child.kill()
    child.wait(timeout=10)

    from agenthicc.runners.session_lease import SessionOwnerStore

    replacement = SessionOwnerStore(root).acquire(session_id)
    try:
        assert replacement.owner_id != owner_id
    finally:
        replacement.release()
