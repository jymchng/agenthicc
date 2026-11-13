"""Several agenthicc instances share one background registry (operator report).

``/bg`` failed with ``OperationalError: database is locked`` while another
instance caught its index up with a long log: the catch-up held SQLite's write
lock past the readers' timeout. Catch-ups now queue on ``registry.lock``, and
the log is compacted, so instances wait for one another instead of failing.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.store import BackgroundStore

pytestmark = pytest.mark.integration


def _session(session_id: str) -> BackgroundSession:
    return BackgroundSession.create(
        session_id, title=session_id, cwd="/work", workflow_name="", intent="do it"
    )


def _append_raw_heartbeats(root: Path, session_id: str, count: int, start_seq: int) -> None:
    """Append heartbeats behind the index's back, as another writer would."""
    now = time.time()
    with (root / "events.jsonl").open("ab") as handle:
        for index in range(count):
            record = {
                "seq": start_seq + index,
                "event_type": "updated",
                "timestamp": now,
                "payload": {
                    "session_id": session_id,
                    "changes": {"last_active": now + index, "latest_activity": "x" * 400},
                },
            }
            handle.write((json.dumps(record, separators=(",", ":")) + "\n").encode())


def _slow_sync(root: str, ready: mp.synchronize.Event, release_after: float) -> None:
    """Hold a long index catch-up: the sync is slowed while it holds the lock."""
    from agenthicc.background import store as store_module

    store = BackgroundStore(Path(root))
    original = store_module.BackgroundStore._apply_tail

    def slow_apply_tail(self, connection, state, fingerprint):  # type: ignore[no-untyped-def]
        ready.set()
        time.sleep(release_after)
        return original(self, connection, state, fingerprint)

    store_module.BackgroundStore._apply_tail = slow_apply_tail  # type: ignore[method-assign]
    store.list()


def _reader(
    root: str,
    session_id: str,
    out: mp.Queue,  # type: ignore[type-arg]
    holding: mp.synchronize.Event,
    ready: mp.synchronize.Semaphore,
) -> None:
    from agenthicc.background import index as index_module

    # The readers' own SQLite wait is far shorter than the other instance's
    # catch-up: before the fix this was exactly ``database is locked``.
    original_open = index_module.SessionIndex._open

    def short_timeout(self):  # type: ignore[no-untyped-def]
        import sqlite3

        return sqlite3.connect(
            self.path, timeout=0.2, check_same_thread=False, isolation_level=None
        )

    index_module.SessionIndex._open = short_timeout  # type: ignore[method-assign]
    store = BackgroundStore(Path(root))
    ready.release()  # imported and set up: the long catch-up may start
    # Read only while the other instance is inside its long catch-up.
    holding.wait(60)
    try:
        session = store.get(session_id)
        out.put(f"ok:{session.status.value}")
    except Exception as exc:  # noqa: BLE001
        out.put(f"error:{type(exc).__name__}: {exc}")
    finally:
        index_module.SessionIndex._open = original_open  # type: ignore[method-assign]


def test_readers_wait_for_another_instances_catch_up_instead_of_failing(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path)
    store.create(_session("s1"))
    store.list()
    _append_raw_heartbeats(tmp_path, "s1", 200, start_seq=2)

    context = mp.get_context("spawn")
    holding = context.Event()
    ready = context.Semaphore(0)
    out = context.Queue()
    readers = [
        context.Process(target=_reader, args=(str(tmp_path), "s1", out, holding, ready))
        for _ in range(3)
    ]
    for reader in readers:
        reader.start()
    for _reader_process in readers:
        assert ready.acquire(timeout=60)  # every reader is waiting for ``holding``
    slow = context.Process(target=_slow_sync, args=(str(tmp_path), holding, 3.0))
    slow.start()
    for reader in readers:
        reader.join(90)
    slow.join(90)

    results = [out.get(timeout=5) for _ in readers]
    assert results == ["ok:queued"] * 3, results


def test_a_writer_holding_the_registry_lock_can_still_read() -> None:
    """The registry lock is reentrant within a store: no self-deadlock."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = BackgroundStore(Path(directory))
        store.create(_session("s1"))
        with store._lock():
            with store._lock():
                assert store.get("s1").session_id == "s1"
            store.update("s1", latest_activity="still mine")
        assert store.get("s1").latest_activity == "still mine"


def test_compaction_keeps_every_record_and_shrinks_the_log(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path)
    for session_id in ("a", "b", "c"):
        store.create(_session(session_id))
    store.transition("a", SessionStatus.STARTING)
    for index in range(300):
        store.update("b", latest_activity=f"step {index}")
    store.transition("c", SessionStatus.CANCELLED)
    store.delete("c")
    before = {s.session_id: s for s in store.list(include_deleted=True)}
    size_before = (tmp_path / "events.jsonl").stat().st_size

    assert store.compact(force=True) is True

    size_after = (tmp_path / "events.jsonl").stat().st_size
    assert size_after < size_before / 10
    after = {s.session_id: s for s in store.list(include_deleted=True)}
    assert after == before  # every record, the deleted tombstone included
    # A fresh index folds the same registry from the small log alone.
    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    (fresh_root / "events.jsonl").write_bytes((tmp_path / "events.jsonl").read_bytes())
    fresh = {s.session_id: s for s in BackgroundStore(fresh_root).list(include_deleted=True)}
    assert fresh == before
    # Appends continue with increasing sequence numbers.
    store.update("b", latest_activity="after compaction")
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    sequences = [json.loads(line)["seq"] for line in lines]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert BackgroundStore(tmp_path).get("b").latest_activity == "after compaction"


def test_compaction_runs_by_itself_past_the_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background import store as store_module

    monkeypatch.setattr(store_module, "COMPACT_THRESHOLD_BYTES", 20_000)
    store = BackgroundStore(tmp_path)
    store.create(_session("a"))
    for index in range(200):
        store.update("a", latest_activity=f"step {index} " + "x" * 100)
    assert (tmp_path / "events.jsonl").stat().st_size < 60_000
    assert store.get("a").latest_activity.startswith("step 199")


def test_another_process_follows_a_compaction(tmp_path: Path) -> None:
    first = BackgroundStore(tmp_path)
    second = BackgroundStore(tmp_path)
    first.create(_session("a"))
    for index in range(50):
        first.update("a", latest_activity=f"step {index}")
    assert second.get("a").latest_activity == "step 49"
    first.compact(force=True)
    first.update("a", latest_activity="after")
    # ``second`` still holds its old index position for the old inode.
    assert second.get("a").latest_activity == "after"
    second.update("a", latest_activity="from second")
    assert first.get("a").latest_activity == "from second"
