"""Concurrent writers and a reader share one session index (AGX-03)."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
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


def _read_pages(root: str, result: "multiprocessing.Queue[str]") -> None:
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
    result.put(json.dumps(problems))


def _open_migrating_index(root: str, result: "multiprocessing.Queue[str]") -> None:
    from agenthicc.background import BackgroundStore

    store = BackgroundStore(root)
    result.put(json.dumps(sorted(item.session_id for item in store.list())))


@pytest.mark.timeout(300)
def test_two_writers_and_a_reader_keep_one_consistent_index(tmp_path: Path) -> None:
    from agenthicc.background import BackgroundStore

    root = str(tmp_path / "background")
    BackgroundStore(root).list()  # create the registry and its index
    context = multiprocessing.get_context("spawn")
    results: multiprocessing.Queue[str] = context.Queue()
    writers = [context.Process(target=_write_sessions, args=(root, name)) for name in ("a", "b")]
    reader = context.Process(target=_read_pages, args=(root, results))
    for process in (*writers, reader):
        process.start()
    for process in (*writers, reader):
        process.join(240)
        assert process.exitcode == 0

    assert json.loads(results.get(timeout=10)) == []
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


@pytest.mark.timeout(120)
def test_concurrent_openers_serialize_legacy_index_migration(tmp_path: Path) -> None:
    from agenthicc.background import BackgroundSession, BackgroundStore

    root_path = tmp_path / "background"
    store = BackgroundStore(root_path)
    for session_id in ("legacy-a", "legacy-b"):
        store.create(
            BackgroundSession.create(
                session_id,
                title=session_id,
                cwd=str(tmp_path),
                workflow_name="demo",
                intent="schema migration",
            ).evolve(idempotency_key=f"key-{session_id}")
        )
    store.close()
    with sqlite3.connect(root_path / "index.sqlite3") as connection:
        connection.execute("DROP INDEX sessions_idempotency")
        connection.execute("ALTER TABLE sessions DROP COLUMN idempotency_key")
        connection.execute("DELETE FROM meta WHERE key = 'schema_version'")

    context = multiprocessing.get_context("spawn")
    results: multiprocessing.Queue[str] = context.Queue()
    openers = [
        context.Process(target=_open_migrating_index, args=(str(root_path), results))
        for _ in range(2)
    ]
    for process in openers:
        process.start()
    for process in openers:
        process.join(60)
        assert process.exitcode == 0

    outputs = [json.loads(results.get(timeout=10)) for _ in openers]
    assert outputs == [["legacy-a", "legacy-b"], ["legacy-a", "legacy-b"]]
    upgraded = BackgroundStore(root_path)
    assert {item.idempotency_key for item in upgraded.list()} == {"key-legacy-a", "key-legacy-b"}
