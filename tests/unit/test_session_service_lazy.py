"""Lazy session materialization and metadata-index tests (PRD-176)."""

from __future__ import annotations

import json
import threading

import pytest

from agenthicc.session_service import SessionEventStore, SessionService
from agenthicc.session_service.models import SessionEvent

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_service_constructor_does_not_replay_existing_sessions(tmp_path) -> None:
    store_root = tmp_path / "service"
    service = SessionService(store_root=store_root)
    created = []
    for index in range(4):
        created.append(
            await service.create_session(
                project_root=tmp_path / f"project-{index}",
                capabilities=frozenset({"read", "control", "workspace"}),
            )
        )
    await service.close()

    restored = SessionService(store_root=store_root)
    assert restored._runtimes == {}
    listed = await restored.list_sessions(capabilities=frozenset({"read", "control", "workspace"}))
    assert {item.session_id for item in listed} == {item.session_id for item in created}
    assert restored._runtimes == {}
    selected = await restored.snapshot(
        created[0].session_id,
        capabilities=frozenset({"read", "control", "workspace"}),
    )
    assert selected.session_id == created[0].session_id
    assert set(restored._runtimes) == {created[0].session_id}
    await restored.close()


def test_legacy_metadata_reads_only_first_event(tmp_path) -> None:
    root = tmp_path / "service"
    root.mkdir()
    path = root / "legacy.jsonl"
    path.write_text(
        '{"schema_version":1,"event_id":"1","sequence":1,'
        '"session_id":"legacy","turn_id":null,"source":"test",'
        '"kind":"session_created","occurred_at":1.0,"durability":"durable",'
        '"visibility":"session","payload":{"project_root":"/workspace"}}\n'
        + "not-json\n"
        + "x" * 2_000_000,
        encoding="utf-8",
    )
    store = SessionEventStore(root)

    # The implementation's metadata probe inspects only the first valid
    # record; all-events replay remains available separately.
    metadata = store.session_metadata()
    assert metadata["legacy"]["project_root"] == "/workspace"
    assert metadata["legacy"]["state"] == "idle"
    assert 0 < store.metadata_bytes_scanned < path.stat().st_size
    assert len(store.all_events("legacy")) == 1


def test_stale_index_is_repaired_from_authoritative_files(tmp_path) -> None:
    root = tmp_path / "service"
    store = SessionEventStore(root)
    event = SessionEvent.create(
        session_id="sess_stale",
        sequence=1,
        source="test",
        kind="session_created",
        payload={"project_root": str(tmp_path)},
    )
    store.append(event)
    store.update_session_metadata(
        event.session_id,
        {
            "project_root": str(tmp_path),
            "created_at": event.occurred_at,
            "updated_at": event.occurred_at,
            "state": "idle",
            "last_event_sequence": 1,
        },
    )
    path = store.path_for(event.session_id)
    path.unlink()

    assert store.session_metadata() == {}
    assert json.loads(store.index_path.read_text(encoding="utf-8"))["sessions"] == {}


def test_index_write_failure_is_retried_without_losing_events(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "service"
    store = SessionEventStore(root)
    event = SessionEvent.create(
        session_id="sess_retry",
        sequence=1,
        source="test",
        kind="session_created",
    )
    store.append(event)
    original_write = store._write_index

    def fail_once(_records) -> None:
        raise OSError("read-only index")

    monkeypatch.setattr(store, "_write_index", fail_once)
    store.update_session_metadata(event.session_id, {"state": "idle"})
    assert store.index_dirty is True
    assert [item.sequence for item in store.all_events(event.session_id)] == [1]

    monkeypatch.setattr(store, "_write_index", original_write)
    metadata = store.session_metadata()
    assert metadata[event.session_id]["session_id"] == event.session_id
    assert store.index_dirty is False


def test_index_metadata_rejects_non_finite_timestamps_and_path_names(tmp_path) -> None:
    store = SessionEventStore(tmp_path / "service")
    event = SessionEvent.create(
        session_id="sess_safe",
        sequence=1,
        source="test",
        kind="session_created",
    )
    store.append(event)
    assert store._safe_metadata("sess_safe", {"created_at": float("nan")})["created_at"] == 0.0
    with pytest.raises(ValueError):
        store.update_session_metadata("../escape", {})


def test_metadata_update_records_current_event_log_fingerprint(tmp_path) -> None:
    store = SessionEventStore(tmp_path / "service")
    event = SessionEvent.create(
        session_id="sess_fingerprint",
        sequence=1,
        source="test",
        kind="session_created",
    )
    store.append(event)
    store.update_session_metadata(event.session_id, {"state": "idle"})

    indexed = store.session_metadata()[event.session_id]
    stat = store.path_for(event.session_id).stat()
    assert indexed["file_size"] == stat.st_size
    assert indexed["file_mtime_ns"] == stat.st_mtime_ns


def test_concurrent_index_updates_merge_records(tmp_path) -> None:
    root = tmp_path / "service"
    first = SessionEventStore(root)
    second = SessionEventStore(root)
    barrier = threading.Barrier(2)

    def update(store: SessionEventStore, session_id: str) -> None:
        barrier.wait()
        store.update_session_metadata(
            session_id,
            {"project_root": f"/workspace/{session_id}", "state": "idle"},
        )

    threads = [
        threading.Thread(target=update, args=(first, "one")),
        threading.Thread(target=update, args=(second, "two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    data = json.loads(first.index_path.read_text(encoding="utf-8"))
    assert set(data["sessions"]) == {"one", "two"}
