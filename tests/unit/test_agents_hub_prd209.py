"""Low-latency projection regressions (PRD-209), for the store and the agents hub."""

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
    SessionFilter,
)
from agenthicc.background.settings import AgentsHubSettings, load_background_settings
from agenthicc.tui.workspace.agents_hub import AgentsHub
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

pytestmark = pytest.mark.unit


def _hub(store: BackgroundStore, tmp_path: Path, **settings: object) -> AgentsHub:
    return AgentsHub(
        Console(width=110, height=25, record=True),
        store=store,
        supervisor=BackgroundSupervisor(store),
        settings=AgentsHubSettings(**settings),  # type: ignore[arg-type]
        prefs=HubPrefs(path=tmp_path / "hub.json"),
    )


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


@pytest.mark.asyncio
async def test_a_long_multiline_message_stays_one_line_under_a_pinned_title(tmp_path: Path) -> None:
    import time as clock

    session = _session(tmp_path, "long-activity", clock.time() - 60)
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
    hub = _hub(store, tmp_path)
    await hub.start()
    try:
        await hub.settle()
        hub._open_detail(session.session_id)
        await hub.settle()
        console = Console(width=100, height=25, record=True)
        console.print(hub.render(100, 25))
        text = console.export_text()
        assert text.splitlines()[1].strip().endswith(session.title)  # pinned title
        assert "first line" in text
        assert "agent response" not in text  # only the first line of a message
        assert all(len(line) <= 100 for line in text.splitlines())
    finally:
        await hub.stop()


def test_index_reopens_without_replaying_and_applies_external_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    for index in range(3):
        store.create(_session(tmp_path, f"s-{index}", float(index)))
    assert len(store.list()) == 3
    index_path = root / "index.sqlite3"
    assert index_path.exists()
    assert os.stat(index_path).st_mode & 0o777 == 0o600

    reopened = BackgroundStore(root)

    def reject_full_replay() -> Iterator[tuple[dict[str, object], int]]:
        raise AssertionError("a current index should avoid a full event replay")

    monkeypatch.setattr(reopened, "_iter_events_with_offsets", reject_full_replay)
    assert len(reopened.list()) == 3

    another_process = BackgroundStore(root)
    another_process.create(_session(tmp_path, "external", 10.0))
    assert {item.session_id for item in reopened.list()} == {
        "s-0",
        "s-1",
        "s-2",
        "external",
    }


def test_page_marks_index_stale_if_writer_appends_during_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "initial", 1.0))
    store.list()
    writer = BackgroundStore(root)
    writer.create(_session(tmp_path, "first-writer", 1.5))
    page_query = store._index.page
    appended = False

    def append_during_query(*args: object, **kwargs: object):
        nonlocal appended
        result = page_query(*args, **kwargs)
        if not appended:
            appended = True
            writer.create(_session(tmp_path, "racing-writer", 2.0))
        return result

    monkeypatch.setattr(store._index, "page", append_during_query)

    page = store.query_window(limit=10)

    assert page.stale is True
    assert page.total == 2
    assert {item.session_id for item in page.sessions} == {"initial", "first-writer"}


def test_replay_last_active_fast_path_preserves_evolve_normalization(
    tmp_path: Path,
) -> None:
    original = _session(tmp_path, "heartbeat", 12.0)
    created = BackgroundStore._apply_event(
        None,
        {"seq": 1, "event_type": "created", "payload": original.to_dict()},
    )
    assert created is not None

    zeroed = BackgroundStore._apply_event(
        created,
        {
            "seq": 2,
            "event_type": "updated",
            "payload": {"session_id": original.session_id, "changes": {"last_active": 0}},
        },
    )
    assert zeroed is not None and zeroed.last_active == 0.0

    # A malformed heartbeat value changes nothing, exactly as ``evolve`` would.
    assert (
        BackgroundStore._apply_event(
            zeroed,
            {
                "seq": 3,
                "event_type": "updated",
                "payload": {
                    "session_id": original.session_id,
                    "changes": {"last_active": True},
                },
            },
        )
        is None
    )


def test_corrupt_index_is_rebuilt_from_the_authoritative_event_log(tmp_path: Path) -> None:
    from agenthicc.background.index import INDEX_SCHEMA_VERSION

    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "survives", 1.0))
    assert len(store.list()) == 1
    store.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{root / 'index.sqlite3'}{suffix}").unlink(missing_ok=True)
    (root / "index.sqlite3").write_bytes(b"this is not a database" * 64)

    recovered = BackgroundStore(root)

    assert [item.session_id for item in recovered.list()] == ["survives"]
    state = recovered._index.state()
    assert state is not None and state.sequence == 1
    import sqlite3

    with sqlite3.connect(root / "index.sqlite3") as connection:
        (version,) = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    assert int(version) == INDEX_SCHEMA_VERSION


def test_sequence_mismatch_rebuilds_the_index_instead_of_recursing(
    tmp_path: Path,
) -> None:
    import asyncio
    import sqlite3

    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "sequence-recovery", 1.0))
    assert store.get("sequence-recovery").latest_activity == "Accepted"
    store.close()

    # A syntactically valid but stale index whose sequence is ahead of the
    # log: the tail guard must rebuild rather than trust or loop on it.
    with sqlite3.connect(root / "index.sqlite3") as connection:
        connection.execute("UPDATE position SET applied_sequence = 10")
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
    hub = _hub(recovered, tmp_path)
    asyncio.run(hub.snapshot())

    assert hub.loaded
    assert hub._load_error == ""
    assert [row.session_id for row in hub.rows if row.selectable] == ["sequence-recovery"]
    session = hub.session("sequence-recovery")
    assert session is not None and session.latest_activity == "Recovered from event log"
    state = recovered._index.state()
    assert state is not None and state.sequence == 2


def test_same_size_event_log_rewrite_rebuilds_the_index(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "rewrite", 1.0))
    store.list()
    original = store.events_path.read_text(encoding="utf-8")
    rewritten = original.replace("session rewrite", "session changed")
    assert len(rewritten) == len(original)
    time.sleep(0.01)  # a distinct modification time for the rewrite
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
    assert reopened._partial_tail is True
    reopened.create(_session(tmp_path, "after", 2.0))

    assert {item.session_id for item in reopened.list()} == {"before", "after"}
    assert reopened._partial_tail is False
    lines = reopened.events_path.read_bytes().splitlines(keepends=True)
    assert all(line.endswith(b"\n") for line in lines)
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]


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

    first = store.query_window(SessionFilter(workflow_name="alpha"), limit=2)
    second = store.query_window(SessionFilter(workflow_name="alpha"), offset=2, limit=2)

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
        ("activity_tail_bytes", 4_000_001),
        ("frame_debounce_ms", 1_001),
        ("max_in_flight_operations", 65),
    ],
)
def test_manager_settings_reject_unbounded_values(name: str, value: object) -> None:
    with pytest.raises(ValueError, match="background.manager"):
        AgentsHubSettings.from_mapping({name: value})


def test_manager_settings_accept_nested_cli_overrides(tmp_path: Path) -> None:
    settings = load_background_settings(
        cwd=tmp_path,
        overrides=("background.manager.refresh_interval_s=0.5",),
    )

    assert settings.manager.refresh_interval_s == 0.5


def test_hub_metrics_are_opt_in_redacted_and_bounded(tmp_path: Path) -> None:
    manager = _hub(BackgroundStore(tmp_path / "background"), tmp_path, metrics=True)
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

    class UnavailableStore(BackgroundStore):
        def active_sessions(self) -> list[BackgroundSession]:
            nonlocal calls
            calls += 1
            raise OSError("temporary registry failure")

    manager = _hub(UnavailableStore(tmp_path / "background"), tmp_path, maintenance_interval_s=0.5)

    assert await manager.maintain_async(force=True) == []
    retry_at = manager._maintenance_retry_at
    assert retry_at > time.monotonic()
    assert "temporary registry failure" in manager.notice
    assert await manager.maintain_async() == []
    assert calls == 1
    # The failed pass's work stays requested for the retry.
    assert manager._maintenance_full
    assert await manager.maintain_async(force=True) == []
    assert calls == 2
    await manager._service.close()


@pytest.mark.asyncio
async def test_manager_shutdown_does_not_wait_for_slow_delete(tmp_path: Path) -> None:
    import asyncio

    manager = _hub(BackgroundStore(tmp_path / "background"), tmp_path)
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


def test_cold_index_rebuild_is_single_flight_across_manager_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh and stale-worker maintenance may share one store concurrently."""

    store = BackgroundStore(tmp_path / "background")
    for index in range(12):
        store.create(_session(tmp_path, f"session-{index:02d}", float(index)))
    store._index.discard()
    store.invalidate_index()

    replay_count = 0
    original_events = store._iter_events_with_offsets
    count_lock = threading.Lock()

    def slow_events() -> Iterator[tuple[dict[str, object], int]]:
        nonlocal replay_count
        with count_lock:
            replay_count += 1
        # Let an unsynchronized second reader pass the empty-index check.
        time.sleep(0.05)
        yield from original_events()

    monkeypatch.setattr(store, "_iter_events_with_offsets", slow_events)
    start = threading.Barrier(2)

    def read_window():
        start.wait(timeout=2.0)
        return store.query_window(limit=20)

    def maintenance_page():
        start.wait(timeout=2.0)
        return store.active_sessions()

    with ThreadPoolExecutor(max_workers=2) as executor:
        page_future = executor.submit(read_window)
        maintenance_future = executor.submit(maintenance_page)
        page = page_future.result(timeout=5.0)
        sessions = maintenance_future.result(timeout=5.0)

    assert replay_count == 1
    assert page.total == 12
    assert len(page.sessions) == 12
    assert len(sessions) == 12


@pytest.mark.asyncio
async def test_load_failure_is_visible_once_and_retries_back_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")

    def broken_select(*_args: object, **_kwargs: object):
        raise OSError("projection unavailable")

    monkeypatch.setattr(store, "select", broken_select)
    hub = _hub(store, tmp_path)
    await hub.start()
    try:
        await hub.settle()
        assert not hub.loaded
        assert hub._load_error == "OSError: projection unavailable"
        assert hub._load_retry_at > time.monotonic()

        # A change notification during the backoff does not retry early.
        hub._request_load("registry_changed")
        assert hub._load_task is None
        hub._request_load("manual", force=True)
        await hub.settle()
        assert hub._load_failures == 2

        console = Console(width=110, height=25, record=True)
        console.print(hub.render(110, 25))
        rendered = console.export_text()
        assert "Agents failed to load" in rendered
        assert rendered.count("projection unavailable") == 1
        assert "press Ctrl+R to retry" in rendered
    finally:
        await hub.stop()
