"""Generate and measure a deterministic large background-session registry.

Run the default 10k-session/1M-event profile explicitly when recording release
performance evidence. The fixture is intentionally opt-in because its JSONL
history is large and a cold authoritative replay uses memory proportional to
the number of events. Example:

    uv run python scripts/benchmark_agents_manager.py --sessions 10000 --events 1000000
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import io
import json
import os
import platform
import queue
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console

from agenthicc.background import (
    AgentsService,
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionFilter,
    SessionStatus,
)
from agenthicc.background.maintenance import MaintenanceTrigger
from agenthicc.background.settings import AgentsHubSettings
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.agents_hub import AgentsHub, Tab
from agenthicc.tui.workspace.agents_hub.overlay import AgentsHubOverlay
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs


def _write_fixture(root: Path, sessions: int, events: int, active_workers: int) -> None:
    if sessions < 1 or events < sessions or active_workers < 0 or active_workers > sessions:
        raise ValueError(
            "require sessions >= 1, events >= sessions, and 0 <= active-workers <= sessions"
        )
    root.mkdir(parents=True, exist_ok=True)
    artifact_root = root / "artifacts"
    registry_root = root / "registry"
    registry_root.mkdir()
    event_path = registry_root / "events.jsonl"
    sequence = 0
    buffer: list[str] = []
    with event_path.open("w", encoding="utf-8") as handle:
        for index in range(sessions):
            session_id = f"bench-{index:06d}"
            artifact_dir = artifact_root / session_id
            if index < min(active_workers, 32):
                artifact_dir.mkdir(parents=True, exist_ok=True)
                (artifact_dir / "conversation.jsonl").write_text(
                    '{"kind":"text","payload":{"text":"benchmark activity"}}\n',
                    encoding="utf-8",
                )
            session = BackgroundSession.create(
                session_id,
                title=f"Synthetic benchmark session {index}",
                cwd=str(root / "workspace"),
                workflow_name="benchmark",
                intent="synthetic benchmark record",
                artifact_dir=str(artifact_dir),
                now=float(index + 1),
            )
            # Most of a large registry is finished work; the active workers
            # carry a PID whose process is not a worker.
            if index < active_workers:
                session = session.evolve(status=SessionStatus.RUNNING, worker_pid=1)
            else:
                session = session.evolve(status=SessionStatus.COMPLETED)
            sequence += 1
            buffer.append(
                json.dumps(
                    {
                        "seq": sequence,
                        "event_type": "created",
                        "timestamp": float(sequence),
                        "payload": session.to_dict(),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            if len(buffer) >= 4_096:
                handle.writelines(buffer)
                buffer.clear()
        for index in range(events - sessions):
            sequence += 1
            session_id = f"bench-{index % sessions:06d}"
            buffer.append(
                json.dumps(
                    {
                        "seq": sequence,
                        "event_type": "updated",
                        "timestamp": float(sequence),
                        "payload": {
                            "session_id": session_id,
                            "changes": {"last_active": float(sequence)},
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            if len(buffer) >= 4_096:
                handle.writelines(buffer)
                buffer.clear()
        if buffer:
            handle.writelines(buffer)
        handle.flush()
        os.fsync(handle.fileno())


def _append_heartbeats(event_path: Path, session_id: str, count: int) -> None:
    """Append *count* heartbeat events, as other processes would."""
    last = event_path.read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1]
    sequence = int(json.loads(last)["seq"])
    lines = []
    for offset in range(1, count + 1):
        lines.append(
            json.dumps(
                {
                    "seq": sequence + offset,
                    "event_type": "updated",
                    "timestamp": float(sequence + offset),
                    "payload": {
                        "session_id": session_id,
                        "changes": {"last_active": float(sequence + offset)},
                    },
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    with event_path.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    if not ordered:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def percentile(value: float) -> float:
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * value + 0.5)))
        return round(ordered[index], 3)

    return {
        f"p{percentile_value}_ms": percentile(percentile_value / 100)
        for percentile_value in (50, 95, 99)
    }


def _fresh_process_first_page(registry_root: Path, samples: int) -> dict[str, float]:
    """Time opening the store and reading one page in new interpreters.

    Imports are excluded: the probe starts its clock after importing
    ``agenthicc.background``, matching the "after imports" budget (AGX-03).
    """
    probe = (
        "import json, sys, time\n"
        "from agenthicc.background import BackgroundStore\n"
        "started = time.perf_counter()\n"
        "BackgroundStore(sys.argv[1]).query_window(limit=25)\n"
        "print(json.dumps((time.perf_counter() - started) * 1000))\n"
    )
    timings: list[float] = []
    for _ in range(max(1, min(samples, 20))):
        output = subprocess.run(
            [sys.executable, "-c", probe, str(registry_root)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        timings.append(float(json.loads(output)))
    return _percentiles(timings)


def _timed(samples: int, operation: Callable[[], object]) -> dict[str, float]:
    timings: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        timings.append((time.perf_counter() - started) * 1_000)
    return _percentiles(timings)


async def _measure(registry_root: Path, samples: int) -> dict[str, object]:
    store = BackgroundStore(registry_root)
    rebuild_started = time.perf_counter()
    store.query_window(limit=25)
    index_rebuild_ms = (time.perf_counter() - rebuild_started) * 1_000
    store.close()
    reopened_started = time.perf_counter()
    store = BackgroundStore(registry_root)
    first = store.query_window(limit=25)
    reopened_first_page_ms = (time.perf_counter() - reopened_started) * 1_000
    refresh = _timed(samples, lambda: store.query_window(limit=25))
    search = _timed(
        samples, lambda: store.query_window(SessionFilter(query="session 99"), limit=25)
    )
    counts = _timed(samples, store.status_counts)
    fresh_process = _fresh_process_first_page(registry_root, samples)

    incremental_samples: list[float] = []
    if first.sessions:
        writer = BackgroundStore(registry_root)
        session_id = first.sessions[0].session_id
        for index in range(min(samples, 25)):
            writer.update(session_id, last_active=float(time.time() + index))
            started = time.perf_counter()
            store.query_window(limit=25)
            incremental_samples.append((time.perf_counter() - started) * 1_000)
        # A reader that falls 1,000 events behind catches up in one pass.
        reader = BackgroundStore(registry_root)
        reader.query_window(limit=25)
        catch_up_samples: list[float] = []
        for _ in range(5):
            _append_heartbeats(registry_root / "events.jsonl", session_id, 1_000)
            catch_up_started = time.perf_counter()
            reader.query_window(limit=25)
            catch_up_samples.append((time.perf_counter() - catch_up_started) * 1_000)
        catch_up = _percentiles(catch_up_samples)
    else:
        catch_up = _percentiles([])

    prefs = tempfile.TemporaryDirectory(prefix="agenthicc-hub-prefs-")
    manager = AgentsHub(
        Console(width=120, height=25, record=True),
        store=store,
        supervisor=BackgroundSupervisor(store),
        prefs=HubPrefs(path=Path(prefs.name) / "hub.json"),
    )
    first_frame_started = time.perf_counter()
    manager.render(120, 25)
    first_frame_ms = (time.perf_counter() - first_frame_started) * 1_000
    await manager.snapshot()
    manager._loop = asyncio.get_running_loop()
    key_samples: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        manager.handle_key(Key.DOWN, "")
        manager.render(120, 25)
        key_samples.append((time.perf_counter() - started) * 1_000)
    maintenance_started = time.perf_counter()
    await manager.maintain_async(force=True)
    maintenance_ms = (time.perf_counter() - maintenance_started) * 1_000
    # AGX-04: once the first pass has reconciled the fixture, a pass prompted
    # by a registry change with nothing due probes nothing and appends nothing.
    # As on the screen, workers the first pass verified are watched for exit.
    scheduler = manager._service.scheduler
    watched = frozenset(scheduler.run(MaintenanceTrigger()).workers)
    log_bytes = (registry_root / "events.jsonl").stat().st_size
    idle_samples: list[float] = []
    idle_probes = 0
    idle_changes = 0
    for _ in range(samples):
        started = time.perf_counter()
        outcome = scheduler.run(MaintenanceTrigger(registry_changed=True, exit_watched=watched))
        idle_samples.append((time.perf_counter() - started) * 1_000)
        idle_probes += outcome.probes
        idle_changes += len(outcome.changed)
    idle_appended_bytes = (registry_root / "events.jsonl").stat().st_size - log_bytes
    activity_ms = 0.0
    selected_id = manager.selected_session_id
    if selected_id is not None:
        activity_session = store.get("bench-000000")
        # The first read imports the redactor; time a warm read, as the hub
        # does on every journal change.
        await manager._service.run_blocking(manager._read_detail, activity_session, False)
        activity_started = time.perf_counter()
        await manager._service.run_blocking(manager._read_detail, activity_session, False)
        activity_ms = (time.perf_counter() - activity_started) * 1_000
        dispatch_started = time.perf_counter()
        manager._start_operation(
            selected_id,
            "pin",
            functools.partial(manager._service.pin_async, selected_id, True),
        )
        action_dispatch_ms = (time.perf_counter() - dispatch_started) * 1_000
        pending = tuple(manager._operation_tasks.values())
        if pending:
            await asyncio.gather(*pending)
        full_record_bytes = len(
            json.dumps(store.get(selected_id).to_dict(), separators=(",", ":")).encode()
        )
        store.update(selected_id, pinned=False)
        last_line = (registry_root / "events.jsonl").read_bytes().rstrip(b"\n").rsplit(b"\n", 1)
        single_field_update_bytes = len(last_line[-1])
    else:
        action_dispatch_ms = 0.0
        full_record_bytes = single_field_update_bytes = 0
    await manager.settle()
    await manager._service.close()
    prefs.cleanup()
    return {
        "first_frame_ms": round(first_frame_ms, 3),
        "index_rebuild_ms": round(index_rebuild_ms, 3),
        "reopened_first_page_ms": round(reopened_first_page_ms, 3),
        "fresh_process_first_page": fresh_process,
        "page_refresh": refresh,
        "search": search,
        "status_counts": counts,
        "catch_up_1000_events": catch_up,
        "incremental_refresh": _percentiles(incremental_samples),
        "key_to_render": _percentiles(key_samples),
        "maintenance_ms": round(maintenance_ms, 3),
        "maintenance_nothing_due": {
            **_percentiles(idle_samples),
            "probes": idle_probes,
            "changed": idle_changes,
            "appended_bytes": idle_appended_bytes,
        },
        "update_event_bytes": {
            "full_record": full_record_bytes,
            "single_field": single_field_update_bytes,
        },
        "activity_read_ms": round(activity_ms, 3),
        "action_dispatch_ms": round(action_dispatch_ms, 3),
    }


class _Terminal:
    """A scripted terminal: keys are typed by the benchmark; restore unblocks a read."""

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


async def _wait_for(condition: Callable[[], bool], timeout: float) -> float:
    started = time.perf_counter()
    while not condition():
        if time.perf_counter() - started > timeout:
            raise TimeoutError("benchmark condition not reached")
        await asyncio.sleep(0.002)
    return (time.perf_counter() - started) * 1_000


def _metric_count(manager: AgentsHub, name: str) -> int:
    metrics = manager.diagnostics["metrics"]
    entry = metrics.get(name) if isinstance(metrics, dict) else None
    return int(entry["count"]) if isinstance(entry, dict) else 0


async def _measure_live(registry_root: Path, idle_s: float, samples: int) -> dict[str, object]:
    """Run the real screen loop: idle CPU, then another writer's change latency (AGX-04)."""

    import agenthicc.tui.terminal.backend as terminal_backend

    terminal = _Terminal()
    original_backend = terminal_backend.get_backend
    terminal_backend.get_backend = lambda: terminal
    try:
        store = BackgroundStore(registry_root)
        prefs = tempfile.TemporaryDirectory(prefix="agenthicc-hub-prefs-")
        manager = AgentsHub(
            Console(width=120, height=25, file=io.StringIO()),
            store=store,
            supervisor=BackgroundSupervisor(store),
            settings=AgentsHubSettings(metrics=True),
            prefs=HubPrefs(path=Path(prefs.name) / "hub.json"),
        )
        run = asyncio.create_task(manager.run())
        await _wait_for(lambda: manager.loaded, 600.0)
        await _wait_for(lambda: _metric_count(manager, "maintenance_finished") >= 1, 600.0)
        await asyncio.sleep(1.0)
        counts = {
            name: _metric_count(manager, name)
            for name in ("load_requested", "maintenance_started", "frame_requested")
        }
        cpu_started = time.process_time()
        wall_started = time.perf_counter()
        await asyncio.sleep(idle_s)
        idle_cpu = (time.process_time() - cpu_started) / (time.perf_counter() - wall_started)
        idle_work = {name: _metric_count(manager, name) - value for name, value in counts.items()}

        writer = BackgroundStore(registry_root)
        target = next(row.session_id for row in manager.rows if row.session_id is not None)
        latencies: list[float] = []
        append_ms: list[float] = []
        for index in range(samples):
            title = f"latency probe {index}"
            append_started = time.perf_counter()
            writer.rename(target, title)
            # Latency runs from the durable append, not from the writer's call.
            started = time.perf_counter()
            append_ms.append((started - append_started) * 1_000)
            await _wait_for(
                lambda title=title: any(  # type: ignore[misc]
                    row.session is not None and row.session.title == title for row in manager.rows
                ),
                30.0,
            )
            latencies.append((time.perf_counter() - started) * 1_000)
            # One change at a time: let the screen finish the frame and any
            # coalesced refresh before the next append.
            await asyncio.sleep(0.1)
        terminal.keys.put((Key.CHAR, "q"))
        await run
        prefs.cleanup()
    finally:
        terminal_backend.get_backend = original_backend
    return {
        "idle_seconds": idle_s,
        "idle_cpu_fraction": round(idle_cpu, 4),
        "idle_work": idle_work,
        "change_to_visible": _percentiles(latencies),
        "writer_append": _percentiles(append_ms),
    }


async def _measure_overlay_open(registry_root: Path, samples: int) -> dict[str, object]:
    """``/agents`` in a session: the keypress to the first frame with rows (AGX-05 §6).

    As in a session, every opening shares one :class:`AgentsService`, whose
    store and executor live as long as the session. CPU time is the whole
    process's, so a busy host inflates wall time far more than CPU time.
    """
    store = BackgroundStore(registry_root)
    service = AgentsService(store, BackgroundSupervisor(store))
    state = AppState.create()
    console = Console(file=io.StringIO(), width=120, height=40, force_terminal=True)
    cpu_samples: list[float] = []
    wall_samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="agenthicc-hub-prefs-") as prefs:
        for index in range(samples + 2):
            wall_started = time.perf_counter()
            cpu_started = time.process_time()
            hub = AgentsHub(
                console,
                store=store,
                supervisor=service.supervisor,
                service=service,
                app_state=state,
                prefs=HubPrefs(path=Path(prefs) / "hub.json"),
                tab=Tab.RUNNING,
                in_session=True,
            )
            dirty = asyncio.Event()
            overlay = AgentsHubOverlay(
                hub, on_close=lambda: None, on_result=lambda _result: None, request_redraw=dirty.set
            )
            overlay.set_viewport(120, 30)
            overlay.on_mount()
            console.print(overlay.render())
            while not hub.loaded:
                dirty.clear()
                await asyncio.wait_for(dirty.wait(), 30.0)
            console.print(overlay.render())
            if index >= 2:  # the first openings import modules and warm the store
                wall_samples.append((time.perf_counter() - wall_started) * 1_000)
                cpu_samples.append((time.process_time() - cpu_started) * 1_000)
            overlay.on_unmount()
            await asyncio.sleep(0)
            await hub.settle()
    await service.close()
    return {"cpu": _percentiles(cpu_samples), "wall": _percentiles(wall_samples)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=10_000)
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--active-workers", type=int, default=32)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--directory", type=Path)
    parser.add_argument(
        "--idle-seconds", type=float, default=10.0, help="idle screen CPU window (AGX-04)"
    )
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be at least one")

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.directory is None:
        temporary = tempfile.TemporaryDirectory(prefix="agenthicc-agents-benchmark-")
        root = Path(temporary.name)
    else:
        root = args.directory.expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            parser.error("--directory must be absent or empty; benchmark data is destructive")
    _write_fixture(root, args.sessions, args.events, args.active_workers)
    event_path = root / "registry" / "events.jsonl"
    fixture_bytes = event_path.stat().st_size
    result = asyncio.run(_measure(root / "registry", args.samples))
    result["live_screen"] = asyncio.run(
        _measure_live(root / "registry", args.idle_seconds, min(args.samples, 50))
    )
    result["overlay_open"] = asyncio.run(
        _measure_overlay_open(root / "registry", min(args.samples, 50))
    )
    print(
        json.dumps(
            {
                "sessions": args.sessions,
                "events": args.events,
                "active_workers": args.active_workers,
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "terminal": {"width": 120, "height": 25},
                "fixture_bytes": fixture_bytes,
                "measurements": result,
                "directory": str(root),
            },
            indent=2,
            sort_keys=True,
        )
    )
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
