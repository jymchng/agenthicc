"""PRD-206's bounded projection, for the agents hub: frames fit, targets are exact."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub import AgentsHub, HubResult
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str, ago: float) -> BackgroundSession:
    artifact = tmp_path / "sessions" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="test",
        artifact_dir=str(artifact),
        now=time.time() - ago,
    )


async def _hub(
    store: BackgroundStore, tmp_path: Path, supervisor: BackgroundSupervisor | None = None
) -> AgentsHub:
    hub = AgentsHub(
        Console(width=140, height=25, record=True),
        store=store,
        supervisor=supervisor or BackgroundSupervisor(store),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
    )
    await hub.start()
    await hub.settle()
    return hub


def _text(hub: AgentsHub, width: int, height: int) -> list[str]:
    console = Console(width=width, height=height, record=True)
    console.print(hub.render(width, height))
    return console.export_text().splitlines()


async def test_every_frame_fits_its_height_and_marks_the_selection(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(20):
        store.create(_session(tmp_path, f"session-{index:02d}", index))
    hub = await _hub(store, tmp_path)
    try:
        for height in (6, 8, 10, 15, 25):
            lines = _text(hub, 140, height)
            assert len(lines) == height
            assert any(line.startswith("▸") for line in lines)
            assert lines[-1].rstrip().endswith("q quit")
    finally:
        await hub.stop()


async def test_selection_stays_on_the_agent_when_the_order_changes(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(3):
        store.create(_session(tmp_path, f"session-{index}", 10 - index))
    hub = await _hub(store, tmp_path)
    try:
        hub._selected_key = "session:session-1"
        store.update("session-0", last_active=time.time())
        hub._request_load("test", force=True)
        await hub.settle()
        assert hub.selected_session_id == "session-1"

        store.delete("session-1", force=True)
        hub._request_load("test", force=True)
        await hub.settle()
        assert hub.selected_session_id not in (None, "session-1")
    finally:
        await hub.stop()


async def test_enter_never_retargets_attach_after_the_agent_disappears(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "session-1", 2))
    store.create(_session(tmp_path, "session-2", 1))
    hub = await _hub(store, tmp_path)
    try:
        selected = hub.selected_session_id
        assert selected is not None
        assert hub.handle_key(Key.ENTER, "") is None  # opens the detail view
        store.delete(selected, force=True)
        hub._request_load("test", force=True)
        await hub.settle()
        assert hub.handle_key(Key.ENTER, "") is None
        await hub.settle()
        assert hub.attach_result is None
    finally:
        await hub.stop()


async def test_enter_opens_the_detail_then_attaches_on_the_second_enter(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = _session(tmp_path, "inspect-me", 1).evolve(
        attempt=1,
        provider="openai",
        model="test-model",
        mode_name="Yolo",
        requested_mode_name="Yolo",
        mode_application_status="applied",
        mode_application_attempt=1,
        latest_activity="Phase completed",
        phase_history=("design", "implement"),
    )
    store.create(session)
    hub = await _hub(store, tmp_path)
    try:
        assert hub.handle_key(Key.ENTER, "") is None
        assert hub.detail_session_id == session.session_id
        details = "\n".join(_text(hub, 120, 40))
        assert "Agent ID          inspect-me" in details
        assert f"Directory         {tmp_path}" in details
        assert "Provider          openai" in details
        assert "Mode              Yolo" in details
        assert "Enter attach" in details
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        assert hub.attach_result == HubResult("attach", session.session_id)
    finally:
        await hub.stop()


async def test_escape_returns_from_the_detail_to_the_list(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "back-to-list", 1))
    hub = await _hub(store, tmp_path)
    try:
        hub.handle_key(Key.ENTER, "")
        assert hub.detail_session_id == "back-to-list"
        assert hub.handle_key(Key.ESC, "") is None
        assert hub.detail_session_id is None
    finally:
        await hub.stop()


async def test_an_unchanged_journal_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, "activity", 1)
    path = Path(session.artifact_dir) / "conversation.jsonl"
    path.write_text(
        json.dumps({"kind": "text", "payload": {"text": "old text"}})
        + "\n"
        + json.dumps({"kind": "user_message", "payload": {"text": "new text"}})
        + "\n",
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    hub = await _hub(store, tmp_path)
    reads = 0
    original_open = Path.open

    def counted_open(target: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal reads
        if target == path and args and args[0] == "rb":
            reads += 1
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    try:
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        hub._schedule_detail_read()
        await hub.settle()
        assert reads == 1
        data = hub._details[session.session_id]
        assert [line.role for line in data.transcript] == ["assistant", "you"]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "text", "payload": {"text": "newer"}}) + "\n")
        hub._schedule_detail_read()
        await hub.settle()
        assert reads == 2
    finally:
        await hub.stop()


async def test_drawing_never_reads_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "one", 1))
    hub = await _hub(store, tmp_path)
    calls: list[str] = []
    for name in ("select", "count", "query_window", "get"):
        original = getattr(store, name)

        def counted(*args: object, _name: str = name, _original=original, **kwargs: object):  # type: ignore[no-untyped-def]
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, counted)
    try:
        for _ in range(3):
            _text(hub, 120, 30)
        assert calls == []
    finally:
        await hub.stop()


async def test_delete_does_not_block_keys_on_worker_cleanup(tmp_path: Path) -> None:
    session = _session(tmp_path, "slow-delete", 1)
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    supervisor = BackgroundSupervisor(store)
    started = threading.Event()
    release = threading.Event()
    delete = supervisor.delete

    def slow_delete(session_id: str, **kwargs: object) -> BackgroundSession:
        started.set()
        release.wait(timeout=2.0)
        return delete(session_id)

    supervisor.delete = slow_delete  # type: ignore[method-assign]
    supervisor.delete_async = None  # type: ignore[assignment]
    hub = await _hub(store, tmp_path, supervisor)
    try:
        started_at = time.monotonic()
        assert hub.handle_key(Key.CTRL_X, "") is None
        assert time.monotonic() - started_at < 0.5
        import asyncio

        assert await asyncio.to_thread(started.wait, 1.0)
        assert store.get(session.session_id).status is SessionStatus.QUEUED
        release.set()
        await hub.settle()
        assert store.get(session.session_id, include_deleted=True).status is SessionStatus.DELETED
        assert hub.rows == [] or all(row.session_id != session.session_id for row in hub.rows)
    finally:
        release.set()
        await hub.stop()


def test_store_index_avoids_full_replays_and_sees_external_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "one", 1))
    assert len(store.list()) == 1

    full_replays = 0
    original_events = store._iter_events_with_offsets

    def counted_events() -> Iterator[tuple[dict[str, object], int]]:
        nonlocal full_replays
        full_replays += 1
        yield from original_events()

    monkeypatch.setattr(store, "_iter_events_with_offsets", counted_events)
    assert len(store.list()) == 1
    assert full_replays == 0

    # The other writer indexes its own append under the registry lock, so this
    # reader sees it without replaying (or even re-reading) the log.
    other = BackgroundStore(root)
    other.create(_session(tmp_path, "two", 2))
    assert {item.session_id for item in store.list()} == {"one", "two"}
    assert full_replays == 0
