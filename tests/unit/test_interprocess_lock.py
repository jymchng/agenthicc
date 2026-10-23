"""The shared cross-process lock excludes other processes and fails closed (PRD-250 D-04)."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.background.store import BackgroundStore
from agenthicc.background.terminals import TerminalStore
from agenthicc.runners.process_lease import (
    InterProcessLock,
    InterProcessLockBusy,
    InterProcessLockError,
)
from agenthicc.runs.store import RunStore
from agenthicc.worktrees.store import ManifestStore

pytestmark = pytest.mark.unit

_TRY_LOCK = """
import sys
from pathlib import Path
from agenthicc.runners.process_lease import InterProcessLock, InterProcessLockBusy
try:
    with InterProcessLock(Path(sys.argv[1]), blocking=False):
        pass
except InterProcessLockBusy:
    sys.exit(3)
"""


def _try_lock_in_another_process(path: Path) -> int:
    return subprocess.run(
        [sys.executable, "-c", _TRY_LOCK, str(path)], check=False, timeout=60
    ).returncode


def test_a_held_lock_excludes_another_process_until_released(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "store.lock"

    with InterProcessLock(path):
        assert _try_lock_in_another_process(path) == 3

    assert _try_lock_in_another_process(path) == 0


def test_a_non_blocking_request_reports_a_busy_lock(tmp_path: Path) -> None:
    path = tmp_path / "store.lock"
    with InterProcessLock(path):
        with pytest.raises(InterProcessLockBusy):
            with InterProcessLock(path, blocking=False):
                pass


def test_an_unopenable_lock_file_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "is-a-directory"
    directory.mkdir()

    with pytest.raises(InterProcessLockError, match="cannot open lock file"):
        with InterProcessLock(directory):
            pass


@pytest.mark.skipif(os.name == "nt", reason="exercises the fcntl backend")
def test_a_failing_lock_backend_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr("agenthicc.runners.process_lease.fcntl.flock", no_locks)
    entered: list[bool] = []

    with pytest.raises(InterProcessLockError, match="cannot lock"):
        with InterProcessLock(tmp_path / "store.lock"):
            entered.append(True)
    assert entered == []


@pytest.mark.parametrize(
    "make_store",
    [BackgroundStore, TerminalStore, RunStore, ManifestStore],
    ids=["background", "terminals", "runs", "worktrees"],
)
def test_every_store_locks_with_the_shared_primitive(
    tmp_path: Path, make_store: type[BackgroundStore | TerminalStore | RunStore | ManifestStore]
) -> None:
    store = make_store(tmp_path / "store")
    lock = store._lock()

    assert isinstance(lock, InterProcessLock)
    assert lock.path == store.lock_path


@pytest.mark.skipif(os.name == "nt", reason="exercises the fcntl backend")
def test_a_store_refuses_to_write_without_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.runs.model import GoalRun

    def no_locks(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr("agenthicc.runners.process_lease.fcntl.flock", no_locks)

    with pytest.raises(InterProcessLockError):
        store.create(GoalRun.create("ship it", repository=str(tmp_path)))
    assert not store.events_path.exists()
