"""Regression tests for the low-latency background manager PRD."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.background.settings import BackgroundManagerSettings, load_background_settings
from agenthicc.tui.workspace.background_manager import BackgroundManager

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str, last_active: float) -> BackgroundSession:
    artifact = tmp_path / "artifacts" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title=f"session {session_id}",
        cwd=str(tmp_path),
        workflow_name="test",
        intent="test background projection",
        artifact_dir=str(artifact),
        now=last_active,
    )


def test_updated_remains_visible_before_long_multiline_latest_text(tmp_path: Path) -> None:
    session = _session(tmp_path, "long-activity", 1_780_000_000.0)
    journal = Path(session.artifact_dir) / "conversation.jsonl"
    journal.write_text(
        json.dumps(
            {
                "kind": "text",
                "payload": {"text": "first line\n" + ("agent response " * 400)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    console = Console(width=100, height=25, record=True)
    manager = BackgroundManager(console, store=store)

    manager.refresh(force=True)
    console.print(manager.render())

    text = console.export_text()
    assert "Updated" in text
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.last_active)) in text
    assert "Latest text" in text
    assert "first line" in text
    # Agent text is normalized/cropped to one line, not allowed to push the
    # timestamp out of the fixed-height details panel.
    assert "Updated" in text.split("Latest text", maxsplit=1)[0]


def test_snapshot_reopens_and_incrementally_applies_external_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    for index in range(3):
        store.create(_session(tmp_path, f"s-{index}", float(index)))
    assert len(store.list()) == 3
    snapshot = root / "projection-v1.json"
    assert snapshot.exists()
    assert os.stat(snapshot).st_mode & 0o777 == 0o600

    reopened = BackgroundStore(root)

    def reject_full_replay() -> Iterator[dict[str, object]]:
        raise AssertionError("compatible snapshot should avoid a full event replay")

    monkeypatch.setattr(reopened, "_iter_events", reject_full_replay)
    assert len(reopened.list()) == 3

    another_process = BackgroundStore(root)
    another_process.create(_session(tmp_path, "external", 10.0))
    assert {item.session_id for item in reopened.list()} == {
        "s-0",
        "s-1",
        "s-2",
        "external",
    }


def test_page_marks_projection_stale_if_writer_appends_during_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "initial", 1.0))
    store.list()
    writer = BackgroundStore(root)
    read_tail = store._read_event_tail
    appended = False

    def append_during_tail(offset: int):
        nonlocal appended
        result = read_tail(offset)
        if not appended:
            appended = True
            writer.create(_session(tmp_path, "racing-writer", 2.0))
        return result

    monkeypatch.setattr(store, "_read_event_tail", append_during_tail)
    writer.create(_session(tmp_path, "first-writer", 1.5))

    page = store.query_page(page=1, page_size=10)

    assert page.stale is True
    assert {item.session_id for item in page.sessions} == {"initial", "first-writer"}


def test_replay_last_active_fast_path_preserves_evolve_normalization(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    original = _session(tmp_path, "heartbeat", 12.0)
    records: dict[str, BackgroundSession] = {}
    store._apply_event(
        records,
        {
            "seq": 1,
            "event_type": "created",
            "payload": original.to_dict(),
        },
    )

    store._apply_event(
        records,
        {
            "seq": 2,
            "event_type": "updated",
            "payload": {"session_id": original.session_id, "changes": {"last_active": 0}},
        },
    )
    assert records[original.session_id].last_active == 0.0

    store._apply_event(
        records,
        {
            "seq": 3,
            "event_type": "updated",
            "payload": {
                "session_id": original.session_id,
                "changes": {"last_active": True},
            },
        },
    )
    assert records[original.session_id].last_active == 0.0


def test_corrupt_snapshot_falls_back_to_authoritative_event_log(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "survives", 1.0))
    assert len(store.list()) == 1
    (root / "projection-v1.json").write_text("{corrupt", encoding="utf-8")

    recovered = BackgroundStore(root)

    assert [item.session_id for item in recovered.list()] == ["survives"]
    assert (
        json.loads((root / "projection-v1.json").read_text(encoding="utf-8"))["schema_version"]
        == recovered.projection_schema_version
    )


def test_sequence_mismatch_bypasses_stale_snapshot_instead_of_recursing(
    tmp_path: Path,
) -> None:
    import asyncio

    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "sequence-recovery", 1.0))
    assert store.get("sequence-recovery").latest_activity == "Accepted"

    # Simulate a syntactically valid but stale projection checkpoint. On the
    # current code path, the incremental sequence guard invalidates memory,
    # reloads this same checkpoint, and recurses indefinitely.
    snapshot_path = root / "projection-v1.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["sequence"] = 10
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    with store.events_path.open("a", encoding="utf-8") as events:
        events.write(
            json.dumps(
                {
                    "seq": 2,
                    "event_type": "updated",
                    "timestamp": 2.0,
                    "payload": {
                        "session_id": "sequence-recovery",
                        "changes": {"latest_activity": "Recovered from event log"},
                    },
                }
            )
            + "\n"
        )

    recovered = BackgroundStore(root)
    from agenthicc.tui.workspace.background_manager import BackgroundManager

    manager = BackgroundManager(Console(), store=recovered)

    async def load_manager_index() -> None:
        manager._async_mode = True
        manager._request_async_refresh("manager_open", force=True)
        task = manager._refresh_task
        assert task is not None
        await task
        await manager._service.close()

    asyncio.run(load_manager_index())

    assert manager._projection_ready
    assert manager._projection_error == ""
    assert manager._total_count == 1
    session = manager._visible_sessions[0]
    assert session.latest_activity == "Recovered from event log"
    assert recovered._next_sequence == 2
    repaired_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert repaired_snapshot["sequence"] == 2


def test_same_size_event_log_rewrite_invalidates_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "rewrite", 1.0))
    store.list()
    original = store.events_path.read_text(encoding="utf-8")
    rewritten = original.replace("session rewrite", "session changed")
    assert len(rewritten) == len(original)
    store.events_path.write_text(rewritten, encoding="utf-8")

    reopened = BackgroundStore(root)

    assert reopened.get("rewrite").title == "session changed"


def test_partial_tail_is_preserved_for_read_then_removed_before_next_append(
    tmp_path: Path,
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "before", 1.0))
    with store.events_path.open("ab") as handle:
        handle.write(b'{"seq":2,"event_type":"updated"')

    reopened = BackgroundStore(root)
    assert [item.session_id for item in reopened.list()] == ["before"]
    reopened.create(_session(tmp_path, "after", 2.0))

    assert {item.session_id for item in reopened.list()} == {"before", "after"}
    assert reopened._projection_tail == b""


def test_stale_recovery_walks_stable_bounded_slices(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(5):
        session = _session(tmp_path, f"stale-{index}", float(index)).evolve(
            status=SessionStatus.RUNNING,
            worker_pid=None,
            last_active=1.0,
        )
        store.create(session)
    supervisor = BackgroundSupervisor(store)

    first = supervisor.recover_stale_batch(stale_after_s=10.0, max_sessions=2)
    second = supervisor.recover_stale_batch(stale_after_s=10.0, max_sessions=2)
    third = supervisor.recover_stale_batch(stale_after_s=10.0, max_sessions=2)

    assert [item.session_id for item in first] == ["stale-0", "stale-1"]
    assert [item.session_id for item in second] == ["stale-2", "stale-3"]
    assert [item.session_id for item in third] == ["stale-4"]


def test_filtered_page_matches_full_list_without_caching_all_query_results(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(12):
        store.create(
            _session(
                tmp_path,
                f"job-{index:02d}",
                float(index),
            ).evolve(workflow_name="alpha" if index % 2 == 0 else "beta")
        )
    expected = store.list(workflow_name="alpha")

    first = store.query_page(page=1, page_size=2, workflow_name="alpha")
    second = store.query_page(page=2, page_size=2, workflow_name="alpha")

    assert first.total == len(expected) == 6
    assert [item.session_id for item in first.sessions] == [
        item.session_id for item in expected[:2]
    ]
    assert [item.session_id for item in second.sessions] == [
        item.session_id for item in expected[2:4]
    ]
    assert not hasattr(store, "_query_cache")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("refresh_interval_s", 0.01),
        ("refresh_interval_s", 61.0),
        ("maintenance_interval_s", 3_601.0),
        ("projection_batch_size", 10_001),
        ("activity_tail_bytes", 4_000_001),
        ("frame_debounce_ms", 1_001),
        ("max_in_flight_operations", 65),
    ],
)
def test_manager_settings_reject_unbounded_values(name: str, value: object) -> None:
    with pytest.raises(ValueError, match="background.manager"):
        BackgroundManagerSettings.from_mapping({name: value})


def test_manager_settings_accept_nested_cli_overrides(tmp_path: Path) -> None:
    settings = load_background_settings(
        cwd=tmp_path,
        overrides=("background.manager.refresh_interval_s=0.5",),
    )

    assert settings.manager.refresh_interval_s == 0.5


def test_manager_metrics_are_opt_in_redacted_and_bounded(tmp_path: Path) -> None:
    settings = BackgroundManagerSettings(metrics=True)
    manager = BackgroundManager(Console(), manager_settings=settings)
    for sample in range(600):
        manager._record_metric("frame_rendered", sample / 1_000.0)

    diagnostics = manager.diagnostics
    metric = diagnostics["metrics"]["frame_rendered"]  # type: ignore[index]
    assert metric["count"] == 600  # type: ignore[index]
    assert metric["p50_ms"] > 0  # type: ignore[index]
    assert len(manager._metric_samples["frame_rendered"]) == 512
    assert str(tmp_path) not in repr(diagnostics)


@pytest.mark.asyncio
async def test_maintenance_failures_back_off_until_forced_retry(tmp_path: Path) -> None:
    calls = 0

    class UnavailableSupervisor:
        def recover_stale(self) -> list[BackgroundSession]:
            nonlocal calls
            calls += 1
            raise OSError("temporary process-table failure")

    manager = BackgroundManager(
        Console(),
        store=BackgroundStore(tmp_path / "background"),
        supervisor=UnavailableSupervisor(),  # type: ignore[arg-type]
        manager_settings=BackgroundManagerSettings(maintenance_interval_s=0.5),
    )

    assert await manager.maintain_async(force=True) == []
    retry_at = manager._maintenance_retry_at
    assert retry_at > time.monotonic()
    assert await manager.maintain_async() == []
    assert calls == 1
    assert await manager.maintain_async(force=True) == []
    assert calls == 2
    await manager._service.close()


@pytest.mark.asyncio
async def test_manager_shutdown_does_not_wait_for_slow_delete(tmp_path: Path) -> None:
    import asyncio

    manager = BackgroundManager(Console(), store=BackgroundStore(tmp_path / "background"))
    deletion_started = asyncio.Event()

    async def slow_delete() -> None:
        deletion_started.set()
        await asyncio.Future()

    manager._deleting_ids = ("slow-delete",)
    manager._deletion_operation_id = "shutdown-test"
    manager._deletion_task = asyncio.create_task(slow_delete())
    delete_task = manager._deletion_task
    await deletion_started.wait()

    started_at = time.monotonic()
    await manager._shutdown_delete()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.8
    assert delete_task.cancelling() > 0
    await asyncio.gather(delete_task, return_exceptions=True)


def test_cold_projection_fold_is_single_flight_across_manager_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh and stale-worker maintenance may share one store concurrently."""

    store = BackgroundStore(tmp_path / "background")
    for index in range(12):
        store.create(_session(tmp_path, f"session-{index:02d}", float(index)))
    store.projection_path.unlink()
    store.invalidate_projection()

    replay_count = 0
    original_events = store._iter_events
    count_lock = threading.Lock()

    def slow_events() -> Iterator[dict[str, object]]:
        nonlocal replay_count
        with count_lock:
            replay_count += 1
        # Let an unsynchronized second reader pass the empty-cache check.
        time.sleep(0.05)
        yield from original_events()

    monkeypatch.setattr(store, "_iter_events", slow_events)
    start = threading.Barrier(2)

    def query_page():
        start.wait(timeout=2.0)
        return store.query_page(page_size=20)

    def maintenance_page():
        start.wait(timeout=2.0)
        return store._lifecycle_page_after("", 20)

    with ThreadPoolExecutor(max_workers=2) as executor:
        page_future = executor.submit(query_page)
        maintenance_future = executor.submit(maintenance_page)
        page = page_future.result(timeout=5.0)
        sessions, _cursor = maintenance_future.result(timeout=5.0)

    assert replay_count == 1
    assert page.total == 12
    assert len(page.sessions) == 12
    assert len(sessions) == 12


def test_index_load_failure_is_visible_and_retries_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")

    def broken_query(**_kwargs: object):
        raise OSError("projection unavailable")

    monkeypatch.setattr(store, "query_page", broken_query)
    console = Console(width=110, height=25, record=True)
    manager = BackgroundManager(console, store=store)

    async def exercise() -> None:
        manager._async_mode = True
        manager._projection_started_at = time.monotonic() - 4.0
        manager._request_async_refresh("manager_open", force=True)
        task = manager._refresh_task
        assert task is not None
        await task
        assert not manager._projection_ready
        assert manager._projection_error == "OSError: projection unavailable"
        assert manager._refresh_retry_at > time.monotonic()

        manager._request_async_refresh("poll")
        assert manager._refresh_task is None
        manager._request_async_refresh("manual", force=True)
        retry = manager._refresh_task
        assert retry is not None
        await retry
        assert manager._refresh_failure_count == 2
        await manager._service.close()

    import asyncio

    asyncio.run(exercise())
    console.print(manager.render())
    rendered = console.export_text()
    assert "session index failed" in rendered.lower()
    assert "projection unavailable" in rendered
    assert rendered.count("projection unavailable") == 1
    assert "press r to retry" in rendered.lower()
