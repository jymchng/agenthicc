"""The agents hub wakes on changes, not on a timer (AGX-04)."""

from __future__ import annotations

import asyncio
import io
import json
import queue
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore, BackgroundSupervisor
from agenthicc.background.model import SessionStatus
from agenthicc.background.settings import AgentsHubSettings
from agenthicc.background.supervisor import _WorkerProcessState
from agenthicc.background.watch import FileWatcher, WatchBackend
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub import AgentsHub, HubResult, Tab
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

pytestmark = pytest.mark.e2e


class _Terminal:
    """A terminal whose keys the test types; ``restore`` releases a pending read."""

    def __init__(self) -> None:
        self.keys: queue.Queue[tuple[Key, str]] = queue.Queue()

    def is_interactive(self) -> bool:
        return True

    @contextmanager
    def enter_raw_mode(self) -> Iterator[None]:
        yield

    def read_key(self) -> tuple[Key, str]:
        return self.keys.get()

    def restore(self) -> None:
        self.keys.put((Key.CHAR, ""))

    def close(self) -> None:
        return None

    def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        return None

    def press(self, ch: str) -> None:
        self.keys.put((Key.CHAR, ch))


def _session(tmp_path: Path, session_id: str, **changes: object) -> BackgroundSession:
    artifact = tmp_path / "artifacts" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title=f"title {session_id}",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="event-driven manager",
        artifact_dir=str(artifact),
        now=float(len(session_id)),
    ).evolve(**changes)


async def _until(condition: Callable[[], bool], timeout: float = 5.0) -> float:
    """Wait for *condition*; return how long it took."""
    started = time.monotonic()
    while not condition():
        if time.monotonic() - started > timeout:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)
    return time.monotonic() - started


def _manager(
    store: BackgroundStore,
    terminal: _Terminal,
    monkeypatch: pytest.MonkeyPatch,
    *,
    supervisor: BackgroundSupervisor | None = None,
    backend: WatchBackend | None = None,
    tab: Tab = Tab.RUNNING,
) -> AgentsHub:
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: terminal)
    if backend is not None:

        class _Watcher(FileWatcher):
            def __init__(self, *, fallback_interval_s: float = 0.25) -> None:
                super().__init__(fallback_interval_s=fallback_interval_s, backend=backend)

        monkeypatch.setattr("agenthicc.background.watch.FileWatcher", _Watcher)
    return AgentsHub(
        Console(width=120, height=30, file=io.StringIO()),
        store=store,
        supervisor=supervisor or BackgroundSupervisor(store),
        settings=AgentsHubSettings(metrics=True, frame_debounce_ms=0),
        prefs=HubPrefs(path=store.root.parent / "hub.json"),
        tab=tab,
    )


def _quiet(manager: AgentsHub) -> bool:
    diagnostics = manager.diagnostics
    return not (
        diagnostics["load_in_flight"]
        or diagnostics["maintenance_in_flight"]
        or diagnostics["detail_in_flight"]
        or manager.dirty
    )


def _count(manager: AgentsHub, metric: str) -> int:
    metrics = manager.diagnostics["metrics"]
    assert isinstance(metrics, dict)
    entry = metrics.get(metric)
    return int(entry["count"]) if isinstance(entry, dict) else 0


async def test_an_idle_screen_does_no_periodic_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(20):
        store.create(_session(tmp_path, f"s-{index:02d}", status=SessionStatus.COMPLETED))
    terminal = _Terminal()
    manager = _manager(store, terminal, monkeypatch)
    run = asyncio.create_task(manager.run())
    await _until(lambda: manager.loaded)
    await _until(lambda: _count(manager, "maintenance_finished") >= 1)
    # Let the one-time work of opening finish: the first page, activity read
    # and maintenance pass, and the frame they draw.
    await _until(lambda: _quiet(manager))
    await asyncio.sleep(0.3)
    await _until(lambda: _quiet(manager))

    refreshes = _count(manager, "load_requested")
    passes = _count(manager, "maintenance_started")
    frames = _count(manager, "frame_requested")
    await asyncio.sleep(2.5)

    assert _count(manager, "load_requested") == refreshes
    assert _count(manager, "maintenance_started") == passes
    assert _count(manager, "frame_requested") == frames
    terminal.press("q")
    assert await asyncio.wait_for(run, 5.0) == HubResult("exit")


@pytest.mark.parametrize("backend", [None, "stat"])
async def test_a_change_from_another_process_appears_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: WatchBackend | None
) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(5):
        store.create(_session(tmp_path, f"s-{index}", status=SessionStatus.COMPLETED))
    terminal = _Terminal()
    manager = _manager(store, terminal, monkeypatch, backend=backend, tab=Tab.HISTORY)
    run = asyncio.create_task(manager.run())
    await _until(lambda: manager.loaded)

    BackgroundStore(store.root).rename("s-3", "renamed elsewhere")
    latency = await _until(
        lambda: any(
            row.session is not None and row.session.title == "renamed elsewhere"
            for row in manager.rows
        )
    )

    # AGX-04 budgets p95 ≤ 250 ms; the margin absorbs a loaded test host.
    assert latency < 1.5
    terminal.press("q")
    assert await asyncio.wait_for(run, 5.0) == HubResult("exit")


async def test_a_worker_that_exits_without_reporting_is_orphaned_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        store = BackgroundStore(tmp_path / "background")
        store.create(
            _session(
                tmp_path,
                "worker",
                status=SessionStatus.RUNNING,
                worker_pid=worker.pid,
                attempt=1,
                lease_token="lease-worker",
                last_active=time.time(),
            )
        )
        supervisor = BackgroundSupervisor(store)

        def probe(_session: BackgroundSession) -> _WorkerProcessState:
            alive = worker.poll() is None
            return _WorkerProcessState.LIVE if alive else _WorkerProcessState.DEAD

        monkeypatch.setattr(supervisor, "_worker_process_state", probe)
        terminal = _Terminal()
        manager = _manager(store, terminal, monkeypatch, supervisor=supervisor)
        run = asyncio.create_task(manager.run())
        await _until(lambda: "worker" in manager._exit_watched)

        worker.kill()
        latency = await _until(lambda: store.get("worker").status is SessionStatus.ORPHANED)

        assert latency < 1.0
        terminal.press("q")
        assert await asyncio.wait_for(run, 5.0) == HubResult("exit")
    finally:
        worker.kill()
        worker.wait()


async def test_the_selected_session_follows_its_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = store.create(_session(tmp_path, "followed", status=SessionStatus.COMPLETED))
    journal = Path(session.artifact_dir) / "conversation.jsonl"
    journal.write_text(
        json.dumps({"kind": "text", "payload": {"text": "first line"}}) + "\n", encoding="utf-8"
    )
    terminal = _Terminal()
    manager = _manager(store, terminal, monkeypatch, tab=Tab.HISTORY)
    run = asyncio.create_task(manager.run())
    await _until(lambda: manager.loaded)

    def last_line() -> str:
        data = manager._details.get("followed")
        return data.transcript[-1].text if data is not None and data.transcript else ""

    terminal.press(" ")  # the peek panel follows the selected agent's journal
    await _until(lambda: last_line() == "first line")
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "text", "payload": {"text": "second line"}}) + "\n")
    latency = await _until(lambda: last_line() == "second line")

    assert latency < 1.5
    terminal.press("q")
    assert await asyncio.wait_for(run, 5.0) == HubResult("exit")
