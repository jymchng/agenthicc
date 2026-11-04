"""Concurrent writers and a reader share one session index (AGX-03)."""

from __future__ import annotations

import json
import multiprocessing
import multiprocessing.resource_tracker
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SESSIONS_PER_WRITER = 20


def _write_sessions(root: str, prefix: str) -> None:
    from agenthicc.background import BackgroundSession, BackgroundStore

    store = BackgroundStore(root)
    for index in range(_SESSIONS_PER_WRITER):
        session_id = f"{prefix}-{index:02d}"
        store.create(
            BackgroundSession.create(
                session_id,
                title=f"{prefix} {index}",
                cwd=root,
                workflow_name="demo",
                intent="concurrency",
                now=float(index),
            )
        )
        store.rename(session_id, f"{prefix} renamed {index}")


def _read_pages(root: str, result: str) -> None:
    from agenthicc.background import BackgroundStore

    store = BackgroundStore(root)
    problems: list[str] = []
    for _ in range(60):
        page = store.query_window(limit=100)
        ids = [item.session_id for item in page.sessions]
        if len(ids) != len(set(ids)):
            problems.append("duplicate rows")
        if page.total != len(ids):
            problems.append(f"total {page.total} != {len(ids)} rows")
    Path(result).write_text(json.dumps(problems), encoding="utf-8")


@pytest.mark.timeout(300)
def test_two_writers_and_a_reader_keep_one_consistent_index(tmp_path: Path) -> None:
    from agenthicc.background import BackgroundStore

    root = str(tmp_path / "background")
    BackgroundStore(root).list()  # create the registry and its index
    context = multiprocessing.get_context("spawn")
    results = tmp_path / "reader-problems.json"
    writers = [context.Process(target=_write_sessions, args=(root, name)) for name in ("a", "b")]
    reader = context.Process(target=_read_pages, args=(root, str(results)))
    try:
        for process in (*writers, reader):
            process.start()
        for process in (*writers, reader):
            process.join(240)
            assert process.exitcode == 0
    finally:
        # Spawning starts multiprocessing's resource tracker, which otherwise
        # lives until the test process exits. Under `nox -s coverage` it then
        # writes its data file while pytest-cov is combining them, and the
        # combine fails on the half-written file. Stop it with the test; a
        # result file instead of a Queue leaves it no semaphore to clean up.
        multiprocessing.resource_tracker._resource_tracker._stop()

    assert json.loads(results.read_text(encoding="utf-8")) == []
    store = BackgroundStore(root)
    sessions = store.list()
    assert len(sessions) == 2 * _SESSIONS_PER_WRITER
    assert all(" renamed " in item.title for item in sessions)
    sequences = [
        json.loads(line)["seq"]
        for line in (tmp_path / "background" / "events.jsonl").read_text().splitlines()
    ]
    assert sequences == list(range(1, 4 * _SESSIONS_PER_WRITER + 1))
    state = store._index.state()
    assert state is not None and state.sequence == sequences[-1]
