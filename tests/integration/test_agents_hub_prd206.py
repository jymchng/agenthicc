"""Integration coverage for the hub's bounded projection of a large registry (PRD-206)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore, BackgroundSupervisor
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub import AgentsHub, Tab
from agenthicc.tui.workspace.agents_hub.hub import HISTORY_CHUNK
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

pytestmark = pytest.mark.integration


def _write_sessions(store: BackgroundStore, root: Path, count: int) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    records = []
    for index in range(count):
        session = BackgroundSession.create(
            f"session-{index:04d}",
            title=f"Session {index}",
            cwd=str(root),
            workflow_name="demo",
            intent="integration fixture",
            artifact_dir=str(root / "sessions" / f"session-{index:04d}"),
            now=float(index + 1),
        ).evolve(status="completed")
        records.append(
            json.dumps(
                {
                    "seq": index + 1,
                    "event_type": "created",
                    "timestamp": float(index + 1),
                    "payload": session.to_dict(),
                },
                separators=(",", ":"),
            )
        )
    store.events_path.write_text("\n".join(records) + "\n", encoding="utf-8")


async def _hub(store: BackgroundStore, tmp_path: Path) -> AgentsHub:
    hub = AgentsHub(
        Console(width=140, height=25),
        store=store,
        supervisor=BackgroundSupervisor(store),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
        tab=Tab.HISTORY,
    )
    await hub.start()
    await hub.settle()
    return hub


async def test_a_large_registry_loads_one_window_and_more_on_demand(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    _write_sessions(store, tmp_path, 1_000)
    hub = await _hub(store, tmp_path)
    try:
        assert hub.counts[Tab.HISTORY] == 1_000
        assert len(hub.rows) == HISTORY_CHUNK
        output = Console(width=140, height=25, record=True)
        output.print(hub.render(140, 25))
        text = output.export_text()
        assert "Session 999" in text  # newest first
        assert "Session 960" not in text  # beyond the first screen
        hub.handle_key(Key.END, "")
        await hub.settle()
        assert len(hub.rows) == 2 * HISTORY_CHUNK
    finally:
        await hub.stop()


def _count_tail_reads(store: BackgroundStore, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    reads: list[int] = []
    original_read = store._read_event_tail

    def counted_read(offset: int) -> tuple[list[dict[str, object]], int, bytes]:
        reads.append(offset)
        return original_read(offset)

    monkeypatch.setattr(store, "_read_event_tail", counted_read)
    return reads


async def test_an_external_change_reaches_the_hub_without_rereading_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    _write_sessions(store, tmp_path, 4)
    hub = await _hub(store, tmp_path)
    try:
        tail_reads = _count_tail_reads(store, monkeypatch)
        other = BackgroundStore(store.root)
        other.update("session-0000", latest_activity="changed externally")
        hub._request_load("test", force=True)
        await hub.settle()
        hub._request_load("test", force=True)
        await hub.settle()
        # The writer applied its own append to the shared index (AGX-03).
        assert tail_reads == []
        session = hub.session("session-0000")
        assert session is not None and session.latest_activity == "changed externally"
    finally:
        await hub.stop()


async def test_an_append_no_writer_indexed_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    _write_sessions(store, tmp_path, 4)
    hub = await _hub(store, tmp_path)
    try:
        tail_reads = _count_tail_reads(store, monkeypatch)
        event = {
            "seq": 5,
            "event_type": "updated",
            "timestamp": 5.0,
            "payload": {
                "session_id": "session-0000",
                "changes": {"latest_activity": "appended directly", "last_active": 5.0},
            },
        }
        with store.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        hub._request_load("test", force=True)
        await hub.settle()
        hub._request_load("test", force=True)
        await hub.settle()
        assert len(tail_reads) == 1
        session = hub.session("session-0000")
        assert session is not None and session.latest_activity == "appended directly"
    finally:
        await hub.stop()
