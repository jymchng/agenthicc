"""Change notification for files, directories and worker processes (AGX-04)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.background.watch import FileWatcher, WatchBackend

pytestmark = pytest.mark.unit

_NATIVE: list[WatchBackend] = ["inotify"] if sys.platform.startswith("linux") else ["kqueue"]
_ALL: list[WatchBackend] = [*_NATIVE, "stat"]


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


@pytest.mark.parametrize("backend", _ALL)
async def test_an_append_to_a_watched_file_is_reported(
    tmp_path: Path, backend: WatchBackend
) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text("", encoding="utf-8")
    watcher = FileWatcher(backend=backend, fallback_interval_s=0.02)
    assert watcher.backend == backend
    watcher.watch("registry", log)
    try:
        assert await watcher.wait(0.1) == {}
        _append(log, "{}\n")
        assert await watcher.wait(2.0) == {"registry": frozenset()}
        # Nothing further happened.
        assert await watcher.wait(0.1) == {}
    finally:
        watcher.close()


@pytest.mark.parametrize("backend", _ALL)
async def test_an_atomic_replacement_and_a_late_creation_are_reported(
    tmp_path: Path, backend: WatchBackend
) -> None:
    journal = tmp_path / "conversation.jsonl"
    watcher = FileWatcher(backend=backend, fallback_interval_s=0.02, safety_interval_s=0.05)
    watcher.watch("detail", journal)
    try:
        journal.write_text("created\n", encoding="utf-8")
        assert "detail" in await watcher.wait(2.0)
        replacement = tmp_path / "conversation.tmp"
        replacement.write_text("replaced and longer\n", encoding="utf-8")
        os.replace(replacement, journal)
        assert "detail" in await watcher.wait(2.0)
    finally:
        watcher.close()


async def test_other_files_in_the_directory_are_not_reported(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text("", encoding="utf-8")
    watcher = FileWatcher(backend=_NATIVE[0])
    watcher.watch("registry", log)
    try:
        (tmp_path / "index.sqlite3-wal").write_bytes(b"page")
        assert await watcher.wait(0.2) == {}
    finally:
        watcher.close()


async def test_inotify_reports_which_directory_entries_changed(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("entry names are reported by inotify only")
    inbox = tmp_path / "input-inbox"
    inbox.mkdir()
    (inbox / "s-1.jsonl").write_text("", encoding="utf-8")
    watcher = FileWatcher()
    assert watcher.reports_entry_names
    watcher.watch_directory("inbox", inbox)
    try:
        _append(inbox / "s-1.jsonl", "{}\n")
        (inbox / "s-2.jsonl").write_text("{}\n", encoding="utf-8")
        changes = await watcher.wait(2.0)
        await asyncio.sleep(0.05)
        changes = {**changes, **await watcher.wait(0.05)}
        assert changes["inbox"] is not None
        assert {"s-1.jsonl", "s-2.jsonl"} <= changes["inbox"]
    finally:
        watcher.close()


async def test_the_stat_fallback_reports_a_directory_change_without_names(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "input-inbox"
    inbox.mkdir()
    watcher = FileWatcher(backend="stat", fallback_interval_s=0.02)
    assert not watcher.reports_entry_names
    watcher.watch_directory("inbox", inbox)
    try:
        (inbox / "s-1.jsonl").write_text("{}\n", encoding="utf-8")
        assert await watcher.wait(2.0) == {"inbox": None}
    finally:
        watcher.close()


async def test_a_process_exit_is_reported(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    watcher = FileWatcher()
    try:
        assert watcher.watch_process("pid:worker", process.pid)
        assert await watcher.wait(0.05) == {}
        assert await watcher.wait(5.0) == {"pid:worker": frozenset()}
    finally:
        process.wait()
        watcher.close()


async def test_a_process_that_already_exited_is_reported_at_once(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    watcher = FileWatcher()
    try:
        assert watcher.watch_process("pid:gone", process.pid)
        assert await watcher.wait(1.0) == {"pid:gone": frozenset()}
    finally:
        watcher.close()


async def test_an_unwatched_process_is_not_reported(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    watcher = FileWatcher()
    try:
        assert watcher.watch_process("pid:worker", process.pid)
        watcher.unwatch("pid:worker")
        process.wait()
        assert await watcher.wait(0.3) == {}
    finally:
        watcher.close()


async def test_the_stat_backend_does_not_claim_process_notification(tmp_path: Path) -> None:
    watcher = FileWatcher(backend="stat")
    try:
        assert not watcher.watch_process("pid:self", os.getpid())
    finally:
        watcher.close()
