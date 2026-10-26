"""SQLite session index for the background registry (AGX-03)."""

from __future__ import annotations

import json
import os
import random
import sqlite3
from pathlib import Path

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    SessionFilter,
    SessionOrder,
    SessionStatus,
)
from agenthicc.background.store import BackgroundEventLogError
from agenthicc.background.index import INDEX_SCHEMA_VERSION, SearchMode, SessionIndex
from agenthicc.background.model import QueuePriority

pytestmark = pytest.mark.unit


def _session(
    tmp_path: Path, session_id: str, last_active: float, **changes: object
) -> BackgroundSession:
    session = BackgroundSession.create(
        session_id,
        title=f"title {session_id}",
        cwd=str(tmp_path / "project"),
        workflow_name="demo",
        intent=f"intent for {session_id}",
        now=last_active,
    )
    return session.evolve(**changes) if changes else session


# ── search ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["fts5", "like"])
def test_search_covers_title_intent_labels_activity_id_and_workspace(
    tmp_path: Path, mode: SearchMode
) -> None:
    store = BackgroundStore(tmp_path / "background", search_mode=mode)
    store.create(
        _session(
            tmp_path,
            "alpha-1",
            1.0,
            title="Refactor billing",
            intent="Split the invoice module",
            labels=("Payments",),
            latest_activity="Running pytest suite",
            cwd="/work/shop",
            workflow_name="code_plan",
        )
    )
    store.create(_session(tmp_path, "beta-2", 2.0, title="Docs", cwd="/work/site"))
    assert store.search_mode == mode

    def found(query: str) -> list[str]:
        return [item.session_id for item in store.list(query=query)]

    assert found("billing") == ["alpha-1"]
    assert found("INVOICE") == ["alpha-1"]  # case-insensitive
    assert found("payments") == ["alpha-1"]
    assert found("pytest") == ["alpha-1"]
    assert found("alpha-") == ["alpha-1"]
    assert found("/work/") == ["beta-2", "alpha-1"]  # registry order: newest first
    assert found("code_pl") == ["alpha-1"]
    assert found("lph") == ["alpha-1"]  # substring, not word, matching
    assert found("al") == ["alpha-1"]  # shorter than a trigram
    assert found("absent") == []


@pytest.mark.parametrize("mode", ["fts5", "like"])
def test_search_treats_like_wildcards_literally(tmp_path: Path, mode: SearchMode) -> None:
    store = BackgroundStore(tmp_path / "background", search_mode=mode)
    store.create(_session(tmp_path, "percent", 1.0, title="100% done"))
    store.create(_session(tmp_path, "plain", 2.0, title="1000 done"))
    store.create(_session(tmp_path, "under", 3.0, title="snake_case"))
    store.create(_session(tmp_path, "glob", 4.0, title="a*b?c[d]"))

    assert [item.session_id for item in store.list(query="0% d")] == ["percent"]
    assert [item.session_id for item in store.list(query="e_c")] == ["under"]
    assert store.list(query="_x") == []
    assert [item.session_id for item in store.list(query="*b?")] == ["glob"]
    assert [item.session_id for item in store.list(query="c[d]")] == ["glob"]
    assert store.list(query="a?b") == []


@pytest.mark.parametrize("mode", ["fts5", "like"])
def test_every_word_of_a_search_must_match_in_any_order(tmp_path: Path, mode: SearchMode) -> None:
    store = BackgroundStore(tmp_path / "background", search_mode=mode)
    # A fixed directory: pytest's tmp_path contains the test's own name.
    store.create(_session(tmp_path, "one", 1.0, title="Fix flaky scheduler test", cwd="/w"))
    store.create(_session(tmp_path, "two", 2.0, title="Scheduler docs", cwd="/w"))

    def found(query: str) -> list[str]:
        return [item.session_id for item in store.select(SessionFilter(query=query))]

    assert found("scheduler") == ["two", "one"]
    assert found("test scheduler") == ["one"]  # any order
    assert found("  flaky   SCHED ") == ["one"]
    assert found("docs flaky") == []


@pytest.mark.parametrize("mode", ["fts5", "like"])
def test_token_filters_narrow_by_project_agent_run_label_and_time(
    tmp_path: Path, mode: SearchMode
) -> None:
    store = BackgroundStore(tmp_path / "background", search_mode=mode)
    store.create(
        _session(
            tmp_path,
            "a",
            10.0,
            cwd="/work/app-0",
            workflow_name="code_plan",
            labels=("release", "ui"),
            run_id="run-1",
        )
    )
    store.create(
        _session(tmp_path, "b", 20.0, cwd="/work/app-1", role="reviewer", labels=("release",))
    )
    store.create(_session(tmp_path, "c", 30.0, cwd="C:\\work\\app-0", workflow_name="direct"))
    store.create(_session(tmp_path, "d", 40.0, cwd="/work/app-00"))
    store.update("d", pinned=True)

    def ids(**fields: object) -> set[str]:
        return {item.session_id for item in store.select(SessionFilter(**fields))}  # type: ignore[arg-type]

    assert ids(project="app-0") == {"a", "c"}  # last path component, either separator
    assert ids(project="/work/app-1") == {"b"}  # or the full directory
    assert ids(agent="code_plan") == {"a"}
    assert ids(agent="reviewer") == {"b"}
    assert ids(run_id="run-1") == {"a"}
    assert ids(labels=frozenset({"release"})) == {"a", "b"}
    assert ids(labels=frozenset({"release", "ui"})) == {"a"}
    assert ids(active_since=25.0) >= {"c", "d"}
    assert "a" not in ids(active_since=25.0)
    assert ids(pinned=True) == {"d"}
    assert store.count(SessionFilter(project="app-0")) == 2


def test_listings_can_be_ordered(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "old", 1.0, title="beta", cwd="/b"))
    store.create(_session(tmp_path, "new", 3.0, title="Alpha", cwd="/a"))
    store.create(_session(tmp_path, "mid", 2.0, title="gamma", cwd="/a"))
    store.update("old", pinned=True)  # also the most recent activity now
    store.update("new", last_active=5.0)

    def ordered(order: SessionOrder) -> list[str]:
        return [item.session_id for item in store.select(SessionFilter(), order=order)]

    assert ordered(SessionOrder.REGISTRY)[0] == "old"  # pinned first
    assert ordered(SessionOrder.CREATED) == ["new", "mid", "old"]
    assert ordered(SessionOrder.TITLE) == ["new", "old", "mid"]  # case-insensitive
    assert ordered(SessionOrder.PROJECT)[-1] == "old"  # /a before /b
    window = store.query_window(SessionFilter(), order=SessionOrder.CREATED, offset=1, limit=1)
    assert [item.session_id for item in window.sessions] == ["mid"]
    assert window.total == 3


def test_search_mode_is_recorded_and_kept_by_later_opens(tmp_path: Path) -> None:
    root = tmp_path / "background"
    BackgroundStore(root, search_mode="like").create(_session(tmp_path, "one", 1.0))
    with sqlite3.connect(root / "index.sqlite3") as connection:
        (mode,) = connection.execute("SELECT value FROM meta WHERE key = 'search_mode'").fetchone()
    assert mode == "like"
    assert BackgroundStore(root).search_mode == "like"


# ── filters, counts and pages ────────────────────────────────────────────────


def test_filters_counts_and_pages_are_index_queries(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(9):
        status = [SessionStatus.QUEUED, SessionStatus.COMPLETED, SessionStatus.WAITING_INPUT][
            index % 3
        ]
        store.create(
            _session(
                tmp_path,
                f"s-{index}",
                float(index),
                status=status,
                workflow_name="even" if index % 2 == 0 else "odd",
            )
        )
    store.archive("s-1")
    store.update("s-8", pinned=True)

    counts = store.status_counts()
    assert counts[SessionStatus.QUEUED] == 3
    assert counts[SessionStatus.COMPLETED] == 2
    assert counts[SessionStatus.ARCHIVED] == 1
    assert counts[SessionStatus.WAITING_INPUT] == 3
    assert store.status_counts(include_archived=False).get(SessionStatus.ARCHIVED) is None

    everything = store.list()
    assert everything[0].session_id == "s-8"  # pinned first
    # Archiving touched s-1 last; then the rest by activity.
    assert [item.session_id for item in everything[1:4]] == ["s-1", "s-7", "s-6"]
    first = store.query_window(limit=4)
    second = store.query_window(offset=4, limit=4)
    assert first.total == second.total == 9
    assert (first.offset, second.offset) == (0, 4)
    assert [item.session_id for item in [*first.sessions, *second.sessions]] == [
        item.session_id for item in everything[:8]
    ]
    assert [
        item.session_id for item in store.list(workflow_name="odd", include_archived=False)
    ] == [
        "s-7",
        "s-5",
        "s-3",
    ]
    assert [item.session_id for item in store.list(status=SessionStatus.WAITING_INPUT)][:1] == [
        "s-8"
    ]


def test_deleted_sessions_are_hidden_unless_requested(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "kept", 1.0, status=SessionStatus.COMPLETED))
    store.create(_session(tmp_path, "gone", 2.0, status=SessionStatus.COMPLETED))
    store.delete("gone")

    assert [item.session_id for item in store.list()] == ["kept"]
    assert {item.session_id for item in store.list(include_deleted=True)} == {"kept", "gone"}
    with pytest.raises(KeyError):
        store.get("gone")
    assert store.get("gone", include_deleted=True).status is SessionStatus.DELETED


# ── recovery and rebuilds ────────────────────────────────────────────────────


def test_a_writer_that_dies_before_indexing_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    writer = BackgroundStore(root)
    writer.create(_session(tmp_path, "first", 1.0))

    def killed(*_args: object) -> bool:
        raise KeyboardInterrupt("writer killed between append and index update")

    monkeypatch.setattr(writer, "_index_own_append", killed)
    with pytest.raises(KeyboardInterrupt):
        writer.create(_session(tmp_path, "second", 2.0))
    assert b'"second"' in writer.events_path.read_bytes()  # appended, not indexed

    reader = BackgroundStore(root)
    assert {item.session_id for item in reader.list()} == {"first", "second"}
    state = reader._index.state()
    assert state is not None and state.sequence == 2


def test_an_own_append_after_a_foreign_one_falls_back_to_catch_up(tmp_path: Path) -> None:
    root = tmp_path / "background"
    first = BackgroundStore(root)
    first.create(_session(tmp_path, "one", 1.0))
    # Another process appends directly, without indexing (as if it died).
    other = BackgroundStore(root)
    other.create(_session(tmp_path, "two", 2.0))
    first.invalidate_index()

    first.create(_session(tmp_path, "three", 3.0))

    assert {item.session_id for item in BackgroundStore(root).list()} == {"one", "two", "three"}


def test_a_rotated_log_or_schema_change_rebuilds_the_index(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "one", 1.0))
    store.close()

    # Rotation: same content under a new inode.
    rotated = root / "events.rotated"
    os.replace(store.events_path, rotated)
    rotated.rename(store.events_path)
    contents = store.events_path.read_bytes()
    store.events_path.unlink()
    store.events_path.write_bytes(contents)
    assert [item.session_id for item in BackgroundStore(root).list()] == ["one"]

    with sqlite3.connect(root / "index.sqlite3") as connection:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(INDEX_SCHEMA_VERSION + 1),),
        )
    reopened = BackgroundStore(root)
    assert [item.session_id for item in reopened.list()] == ["one"]
    state = reopened._index.state()
    assert state is not None and state.sequence == 1


def test_rebuild_reports_progress_and_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    for index in range(5):
        store.create(_session(tmp_path, f"s-{index}", float(index)))
    store._index.discard()
    store.invalidate_index()
    seen: list[tuple[int, int] | None] = []
    original = store._iter_events_with_offsets

    def observed():  # type: ignore[no-untyped-def]
        for item in original():
            seen.append(store.rebuild_progress())
            yield item

    monkeypatch.setattr(store, "_iter_events_with_offsets", observed)
    assert len(store.list()) == 5
    assert seen and all(progress is not None for progress in seen)
    total = seen[0][1]  # type: ignore[index]
    assert total == store.events_path.stat().st_size
    assert store.rebuild_progress() is None


def test_recent_sessions_are_read_from_the_log_tail_without_the_index(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    for index in range(6):
        store.create(_session(tmp_path, f"s-{index}", float(index), status=SessionStatus.COMPLETED))
    store.rename("s-5", "renamed late")
    store.delete("s-4")
    store.update("s-0", pinned=True)

    recent = BackgroundStore(root).recent_from_log_tail(limit=3)
    assert [item.session_id for item in recent] == ["s-0", "s-5", "s-3"]
    assert recent[1].title == "renamed late"

    # A window holding only the tombstone and the pin's delta event (AGX-04)
    # cannot reconstruct a session created before it.
    size = store.events_path.stat().st_size
    last_two = store.events_path.read_bytes().splitlines(keepends=True)[-2:]
    window = sum(len(line) for line in last_two)
    assert window < size
    assert BackgroundStore(root).recent_from_log_tail(max_bytes=window) == ()


# ── equivalence with a reference fold ────────────────────────────────────────


def _reference_fold(events: list[dict[str, object]]) -> dict[str, BackgroundSession]:
    """Apply every event in order — the semantics the index must reproduce."""
    records: dict[str, BackgroundSession] = {}
    for event in events:
        payload = event["payload"]
        assert isinstance(payload, dict)
        session_id = payload["session_id"]
        assert isinstance(session_id, str)
        updated = BackgroundStore._apply_event(records.get(session_id), event)
        if updated is not None:
            records[session_id] = updated
    return records


def _random_events(rng: random.Random, tmp_path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    known: list[str] = []
    for sequence in range(1, rng.randint(1, 40) + 1):
        kind = rng.choice(["create", "heartbeat", "heartbeat", "update", "delete", "priority"])
        if kind == "create" or not known:
            session_id = f"s-{rng.randint(0, 6)}"
            known.append(session_id)
            payload: dict[str, object] = _session(tmp_path, session_id, float(sequence)).to_dict()
            events.append(
                {
                    "seq": sequence,
                    "event_type": "created",
                    "timestamp": float(sequence),
                    "payload": payload,
                }
            )
            continue
        session_id = rng.choice(known)
        if kind == "heartbeat":
            changes: dict[str, object] = {"last_active": float(sequence) + rng.random()}
            events.append(
                {
                    "seq": sequence,
                    "event_type": "updated",
                    "timestamp": float(sequence),
                    "payload": {"session_id": session_id, "changes": changes},
                }
            )
        elif kind == "update":
            changes = {
                "title": f"t{sequence}",
                "latest_activity": rng.choice(["working", "waiting on tests", "done"]),
                "pinned": rng.random() < 0.3,
                "status": rng.choice(
                    [
                        status.value
                        for status in SessionStatus
                        if status is not SessionStatus.DELETED
                    ]
                ),
                "labels": rng.sample(["red", "green", "blue"], rng.randint(0, 2)),
                "last_active": float(sequence),
            }
            events.append(
                {
                    "seq": sequence,
                    "event_type": "updated",
                    "timestamp": float(sequence),
                    "payload": {"session_id": session_id, "changes": changes},
                }
            )
        elif kind == "delete":
            events.append(
                {
                    "seq": sequence,
                    "event_type": "deleted",
                    "timestamp": float(sequence),
                    "payload": {
                        "session_id": session_id,
                        "trash_dir": "/trash",
                        "operation_id": "op",
                    },
                }
            )
        else:
            events.append(
                {
                    "seq": sequence,
                    "event_type": "priority_changed",
                    "timestamp": float(sequence),
                    "payload": {
                        "session_id": session_id,
                        "previous": QueuePriority.NORMAL.value,
                        "current": rng.choice([p.value for p in QueuePriority]),
                        "changed_by": "test",
                    },
                }
            )
    return events


def _write(path: Path, events: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def _ordered(records: dict[str, BackgroundSession]) -> list[BackgroundSession]:
    return sorted(
        records.values(), key=lambda item: (not item.pinned, -item.last_active, item.session_id)
    )


def test_rebuild_and_incremental_catch_up_match_the_reference_fold(tmp_path: Path) -> None:
    rng = random.Random(20261023)
    for case in range(120):
        root = tmp_path / f"case-{case}"
        root.mkdir()
        events = _random_events(rng, tmp_path)
        expected = _ordered(_reference_fold(events))
        split = rng.randint(0, len(events))

        # Incremental: a reader indexes a prefix, then catches up on the rest.
        _write(root / "events.jsonl", events[:split])
        incremental = BackgroundStore(root)
        incremental.list(include_deleted=True)
        _write(root / "events.jsonl", events[split:])
        assert incremental.list(include_deleted=True) == expected, case

        # Cold: a fresh process rebuilds the same rows from the whole log.
        incremental.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{root / 'index.sqlite3'}{suffix}").unlink(missing_ok=True)
        assert BackgroundStore(root).list(include_deleted=True) == expected, case


async def test_hub_shows_recent_agents_and_progress_while_the_index_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading
    import time

    from rich.console import Console

    from agenthicc.background import BackgroundSupervisor
    from agenthicc.tui.workspace.agents_hub import Tab
    from agenthicc.tui.workspace.agents_hub import AgentsHub
    from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

    root = tmp_path / "background"
    store = BackgroundStore(root)
    for index in range(4):
        store.create(_session(tmp_path, f"s-{index}", float(index)))
    store._index.discard()
    store.invalidate_index()
    release = threading.Event()
    original = store._iter_events_with_offsets

    def slow_rebuild():  # type: ignore[no-untyped-def]
        for position, item in enumerate(original()):
            if position == 2:
                store._rebuild_progress = (item[1], store.events_path.stat().st_size)
                assert release.wait(10), "test never released the rebuild"
            yield item

    monkeypatch.setattr(store, "_iter_events_with_offsets", slow_rebuild)
    console = Console(width=120, height=30, record=True)
    hub = AgentsHub(
        console,
        store=store,
        supervisor=BackgroundSupervisor(store),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
    )
    await hub.start()
    try:
        deadline = time.monotonic() + 5
        while not hub._provisional and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert hub._provisional
        assert [row.session_id for row in hub.rows if row.selectable] == [
            "s-3",
            "s-2",
            "s-1",
            "s-0",
        ]
        console.print(hub.render(120, 30))
        assert "Indexing the agent registry" in console.export_text()

        release.set()
        await hub.settle()
        assert hub.loaded and not hub._provisional
        assert hub.counts[Tab.RUNNING] == 4
    finally:
        release.set()
        await hub.stop()


def test_search_works_after_a_rebuild_and_follows_later_renames(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "rebuilt", 1.0, title="Original name"))
    store._index.discard()
    store.invalidate_index()

    assert [item.session_id for item in store.list(query="original")] == ["rebuilt"]
    store.rename("rebuilt", "Renamed later")
    store.update("rebuilt", last_active=5.0)  # a heartbeat does not re-index

    assert store.list(query="original") == []
    assert [item.session_id for item in store.list(query="renamed")] == ["rebuilt"]


# ── migration from the JSON projection ───────────────────────────────────────


def _write_legacy_snapshot(
    root: Path, sessions: list[BackgroundSession], *, inode: int | None = None
) -> None:
    """Write ``projection-v1.json`` exactly as the pre-index store did."""
    events = root / "events.jsonl"
    stat = events.stat()
    lines = events.read_bytes().splitlines(keepends=True)
    payload = {
        "schema_version": 1,
        "event_inode": stat.st_ino if inode is None else inode,
        "event_size": stat.st_size,
        "event_offset": stat.st_size,
        "event_mtime_ns": stat.st_mtime_ns,
        "sequence": len(lines),
        "records": [session.to_dict() for session in sessions],
    }
    (root / "projection-v1.json").write_text(json.dumps(payload), encoding="utf-8")


def _drop_index(root: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{root / 'index.sqlite3'}{suffix}").unlink(missing_ok=True)


def test_event_log_read_failure_rolls_back_rebuild_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    writer = BackgroundStore(root)
    writer.create(_session(tmp_path, "preserved", 1.0))
    writer.close()
    events_path = root / "events.jsonl"
    events_before = events_path.read_bytes()
    _drop_index(root)

    store = BackgroundStore(root)
    original_open = Path.open

    def deny_event_log(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path == events_path and mode == "rb":
            raise PermissionError("test event-log access denial")
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", deny_event_log)
        with pytest.raises(BackgroundEventLogError) as raised:
            store.list()

    assert isinstance(raised.value.__cause__, PermissionError)
    assert events_path.read_bytes() == events_before
    # The failed replay rolls back rather than publishing an empty projection.
    with sqlite3.connect(root / "index.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone() == (0,)

    assert [item.session_id for item in store.list()] == ["preserved"]
    assert events_path.read_bytes() == events_before


def test_missing_event_log_does_not_replace_a_nonempty_index_with_empty_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "preserved", 1.0))
    store.close()
    (root / "events.jsonl").unlink()

    reopened = BackgroundStore(root)
    with pytest.raises(BackgroundEventLogError, match="is missing"):
        reopened.list()

    with sqlite3.connect(root / "index.sqlite3") as connection:
        assert connection.execute("SELECT session_id FROM sessions").fetchall() == [("preserved",)]


def test_first_open_seeds_the_index_from_the_old_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    writer = BackgroundStore(root)
    for index in range(3):
        writer.create(_session(tmp_path, f"s-{index}", float(index)))
    snapshot = writer.list(include_deleted=True)
    writer.close()
    _write_legacy_snapshot(root, snapshot)
    # Events after the snapshot must still be applied from the log tail.
    with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "seq": 4,
                    "event_type": "updated",
                    "timestamp": 9.0,
                    "payload": {"session_id": "s-1", "changes": {"title": "after snapshot"}},
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    _drop_index(root)
    upgraded = BackgroundStore(root)

    def no_full_replay():  # type: ignore[no-untyped-def]
        raise AssertionError("a compatible projection avoids replaying the log")

    monkeypatch.setattr(upgraded, "_iter_events_with_offsets", no_full_replay)

    assert {item.session_id for item in upgraded.list()} == {"s-0", "s-1", "s-2"}
    assert upgraded.get("s-1").title == "after snapshot"
    assert not (root / "projection-v1.json").exists()
    state = upgraded._index.state()
    assert state is not None and state.sequence == 4


def test_a_stale_old_projection_is_ignored_and_removed(tmp_path: Path) -> None:
    root = tmp_path / "background"
    writer = BackgroundStore(root)
    writer.create(_session(tmp_path, "real", 1.0))
    writer.close()
    fake = _session(tmp_path, "from-another-log", 2.0)
    _write_legacy_snapshot(root, [fake], inode=1)
    _drop_index(root)

    upgraded = BackgroundStore(root)

    assert [item.session_id for item in upgraded.list()] == ["real"]
    assert not (root / "projection-v1.json").exists()


def test_an_index_from_another_schema_version_is_discarded(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "kept", 1.0))
    store.close()
    with sqlite3.connect(root / "index.sqlite3") as connection:
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        connection.execute("CREATE TABLE stray_old_layout (value TEXT)")

    reopened = BackgroundStore(root)

    assert [item.session_id for item in reopened.list()] == ["kept"]
    with sqlite3.connect(root / "index.sqlite3") as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "stray_old_layout" not in tables


def test_unknown_missing_column_layout_is_rebuilt_from_events(tmp_path: Path) -> None:
    root = tmp_path / "background"
    original = BackgroundStore(root)
    original.create(_session(tmp_path, "unknown-layout", 1.0).evolve(idempotency_key="safe-key"))
    original.close()
    event_log_before_rebuild = (root / "events.jsonl").read_bytes()
    with sqlite3.connect(root / "index.sqlite3") as connection:
        connection.execute("ALTER TABLE sessions DROP COLUMN title")

    recovered = BackgroundStore(root)

    assert recovered.get("unknown-layout").title == "title unknown-layout"
    assert recovered.with_idempotency_key("safe-key")[0].session_id == "unknown-layout"
    assert (root / "events.jsonl").read_bytes() == event_log_before_rebuild


@pytest.mark.parametrize("version_state", ["no_meta", "unversioned", "current", "previous"])
def test_missing_idempotency_column_is_migrated_and_backfilled(
    tmp_path: Path, version_state: str
) -> None:
    root = tmp_path / "background"
    original = BackgroundStore(root)
    record = _session(tmp_path, "legacy-index", 1.0).evolve(idempotency_key="detach-key")
    original.create(record)
    original.create(_session(tmp_path, "legacy-empty-key", 2.0))
    original.close()
    event_log_before_migration = (root / "events.jsonl").read_bytes()

    with sqlite3.connect(root / "index.sqlite3") as connection:
        connection.execute("DROP INDEX sessions_idempotency")
        connection.execute("ALTER TABLE sessions DROP COLUMN idempotency_key")
        if version_state == "no_meta":
            connection.execute("DROP TABLE meta")
        elif version_state == "unversioned":
            connection.execute("DELETE FROM meta WHERE key = 'schema_version'")
        elif version_state == "previous":
            connection.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")

    migrated = BackgroundStore(root)

    assert migrated.get("legacy-index").idempotency_key == "detach-key"
    assert [item.session_id for item in migrated.with_idempotency_key("detach-key")] == [
        "legacy-index"
    ]
    assert [item.session_id for item in migrated.with_idempotency_key("")] == ["legacy-empty-key"]
    with sqlite3.connect(root / "index.sqlite3") as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    assert "idempotency_key" in columns
    assert version == (str(INDEX_SCHEMA_VERSION),)
    assert (root / "events.jsonl").read_bytes() == event_log_before_migration


def test_interrupted_additive_migration_rolls_back_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    original = BackgroundStore(root)
    original.create(_session(tmp_path, "migration-retry", 1.0).evolve(idempotency_key="retry-key"))
    original.close()
    with sqlite3.connect(root / "index.sqlite3") as connection:
        connection.execute("DROP INDEX sessions_idempotency")
        connection.execute("ALTER TABLE sessions DROP COLUMN idempotency_key")

    def interrupted(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("simulated interruption during idempotency backfill")

    original_backfill = SessionIndex._backfill_idempotency_keys
    monkeypatch.setattr(SessionIndex, "_backfill_idempotency_keys", staticmethod(interrupted))
    with pytest.raises(RuntimeError, match="simulated interruption"):
        BackgroundStore(root)._index.state()

    with sqlite3.connect(root / "index.sqlite3") as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert "idempotency_key" not in columns

    monkeypatch.setattr(SessionIndex, "_backfill_idempotency_keys", staticmethod(original_backfill))
    recovered = BackgroundStore(root)
    assert [item.session_id for item in recovered.with_idempotency_key("retry-key")] == [
        "migration-retry"
    ]


def test_corrupt_derived_index_still_rebuilds_from_the_event_log(tmp_path: Path) -> None:
    root = tmp_path / "background"
    original = BackgroundStore(root)
    original.create(_session(tmp_path, "recover-corruption", 1.0))
    original.close()
    (root / "index.sqlite3").write_bytes(b"not a SQLite database")

    recovered = BackgroundStore(root)

    assert [item.session_id for item in recovered.list()] == ["recover-corruption"]
    state = recovered._index.state()
    assert state is not None and state.sequence == 1


def test_stale_store_does_not_discard_a_replacement_index(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "replacement-survives", 1.0))
    replacement = tmp_path / "replacement.sqlite3"
    with sqlite3.connect(replacement) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")

    os.replace(replacement, root / "index.sqlite3")
    store._index.discard()

    assert (root / "index.sqlite3").is_file()
    assert [item.session_id for item in store.list()] == ["replacement-survives"]


def test_missing_fts5_falls_back_but_unrelated_operational_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_create = SessionIndex._create_fts_objects

    def unavailable(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(SessionIndex, "_create_fts_objects", staticmethod(unavailable))
    assert SessionIndex(tmp_path / "no-fts.sqlite3").search_mode == "like"

    def disk_error(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SessionIndex, "_create_fts_objects", staticmethod(disk_error))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _ = SessionIndex(tmp_path / "io-error.sqlite3").search_mode
    monkeypatch.setattr(SessionIndex, "_create_fts_objects", staticmethod(original_create))


def test_a_busy_index_does_not_fail_a_durable_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    writer = BackgroundStore(root)
    writer.create(_session(tmp_path, "first", 1.0))

    def busy(*_args: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(writer, "_index_own_append", busy)
    writer.create(_session(tmp_path, "second", 2.0))

    assert {item.session_id for item in BackgroundStore(root).list()} == {"first", "second"}


def test_schema_operational_error_resets_and_replays_the_derived_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "recover-index", 1.0))
    original_state = store._index.state
    fail_once = True

    def missing_column_once():  # type: ignore[no-untyped-def]
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise sqlite3.OperationalError("no such column: idempotency_key")
        return original_state()

    monkeypatch.setattr(store._index, "state", missing_column_once)

    assert store.get("recover-index").title == "title recover-index"
    assert store.count() == 1


@pytest.mark.parametrize("message", ["database is locked", "database is busy", "disk I/O error"])
def test_operational_errors_do_not_trigger_unsafe_schema_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "busy-index", 1.0))
    with sqlite3.connect(root / "index.sqlite3") as connection:
        connection.execute("CREATE TABLE migration_guard (value TEXT)")
    store.invalidate_index()

    def busy():  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(store._index, "state", busy)
    with pytest.raises(sqlite3.OperationalError, match=message):
        store.list()
    with sqlite3.connect(root / "index.sqlite3") as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migration_guard'"
        ).fetchone()


def test_the_provisional_view_uses_full_record_updates_from_older_logs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    old = store.create(_session(tmp_path, "old", 1.0, status=SessionStatus.COMPLETED))
    created_bytes = store.events_path.stat().st_size
    # Before AGX-04 every update event carried the whole record.
    changes = old.evolve(title="renamed in the tail").to_dict()
    changes.pop("session_id")
    legacy = {
        "seq": 2,
        "event_type": "updated",
        "timestamp": 2.0,
        "payload": {"session_id": "old", "changes": changes},
    }
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy, separators=(",", ":")) + "\n")

    window = store.events_path.stat().st_size - created_bytes
    recent = BackgroundStore(root).recent_from_log_tail(max_bytes=window)

    assert [item.session_id for item in recent] == ["old"]
    assert recent[0].title == "renamed in the tail"


def test_a_cancelled_rebuild_rolls_back_and_the_next_read_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from agenthicc.background.store import IndexRebuildCancelled

    root = tmp_path / "background"
    store = BackgroundStore(root)
    for index in range(5):
        store.create(_session(tmp_path, f"s-{index}", float(index)))
    store._index.discard()
    store.invalidate_index()
    original = store._iter_events_with_offsets

    def long_log():  # type: ignore[no-untyped-def]
        events = list(original())
        for repeat in range(5_000):  # enough records to pass a cancellation check
            yield events[repeat % len(events)]
            if repeat == 100:
                threading.Thread(target=store.cancel_rebuild).start()
                threading.Event().wait(0.2)

    monkeypatch.setattr(store, "_iter_events_with_offsets", long_log)
    with pytest.raises(IndexRebuildCancelled):
        store.list()
    assert store.rebuild_progress() is None

    monkeypatch.setattr(store, "_iter_events_with_offsets", original)
    assert len(store.list()) == 5


def test_scheduler_queries_read_only_the_sessions_they_need(tmp_path: Path) -> None:
    from agenthicc.background.model import ACTIVE_STATUSES

    store = BackgroundStore(tmp_path / "background")
    for index, status in enumerate(SessionStatus):
        if status is SessionStatus.DELETED:
            continue
        store.create(
            _session(
                tmp_path,
                f"s-{status.value}",
                float(index),
                status=status,
                idempotency_key="shared" if index % 2 else "",
            )
        )

    assert {item.status for item in store.active_sessions()} == set(ACTIVE_STATUSES)
    keyed = store.with_idempotency_key("shared")
    assert keyed and all(item.idempotency_key == "shared" for item in keyed)
    assert {item.session_id for item in keyed} == {
        item.session_id for item in store.list() if item.idempotency_key == "shared"
    }
    assert store.with_idempotency_key("absent") == []
