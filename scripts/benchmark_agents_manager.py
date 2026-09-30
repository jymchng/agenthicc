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
import json
import os
import platform
import sqlite3
import tempfile
import time
from pathlib import Path

from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.tui.workspace.background_manager import BackgroundManager


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
            if index < active_workers:
                session = session.evolve(status=SessionStatus.RUNNING, worker_pid=1)
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


def _measure_sqlite_candidate(event_path: Path, database_path: Path) -> dict[str, float | int]:
    """Prototype a transactional SQLite projection against the same fixture.

    The deterministic load fixture consists of created records followed by
    last-active heartbeats. This candidate stores a queryable materialized
    projection in SQLite and applies those heartbeats transactionally; it is
    an architecture comparison, not production storage code.
    """

    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            pinned INTEGER NOT NULL,
            last_active REAL NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    started = time.perf_counter()
    connection.execute("BEGIN")
    with event_path.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("event_type") == "created":
                session = BackgroundSession.from_mapping(payload)
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    (
                        session.session_id,
                        session.status.value,
                        int(session.pinned),
                        session.last_active,
                        json.dumps(session.to_dict(), separators=(",", ":")),
                    ),
                )
                continue
            changes = payload.get("changes")
            value = changes.get("last_active") if isinstance(changes, dict) else None
            if event.get("event_type") != "updated" or not isinstance(changes, dict):
                raise ValueError("SQLite candidate fixture contains an unsupported event")
            session_id = payload.get("session_id")
            if (
                len(changes) == 1
                and "last_active" in changes
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                connection.execute(
                    """
                    UPDATE sessions
                    SET last_active = ?, payload = json_set(payload, '$.last_active', ?)
                    WHERE session_id = ?
                    """,
                    (float(value), float(value), session_id),
                )
            else:
                row = connection.execute(
                    "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    continue
                updated = BackgroundSession.from_mapping(json.loads(row[0])).evolve(**changes)
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, pinned = ?, last_active = ?, payload = ?
                    WHERE session_id = ?
                    """,
                    (
                        updated.status.value,
                        int(updated.pinned),
                        updated.last_active,
                        json.dumps(updated.to_dict(), separators=(",", ":")),
                        updated.session_id,
                    ),
                )
    connection.execute(
        """
        CREATE INDEX sessions_page_order
        ON sessions (pinned DESC, last_active DESC, session_id)
        WHERE status != 'deleted'
        """
    )
    connection.commit()
    cold_projection_ms = (time.perf_counter() - started) * 1_000
    query_started = time.perf_counter()
    connection.execute(
        """
        SELECT payload FROM sessions WHERE status != 'deleted'
        ORDER BY pinned DESC, last_active DESC, session_id LIMIT 25 OFFSET 0
        """
    ).fetchall()
    connection.execute("SELECT count(*) FROM sessions WHERE status != 'deleted'").fetchone()
    page_query_ms = (time.perf_counter() - query_started) * 1_000
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_bytes = database_path.stat().st_size
    connection.close()
    return {
        "cold_projection_ms": round(cold_projection_ms, 3),
        "page_and_count_query_ms": round(page_query_ms, 3),
        "database_bytes": database_bytes,
    }


async def _measure(registry_root: Path, samples: int) -> dict[str, object]:
    store = BackgroundStore(registry_root)
    cold_started = time.perf_counter()
    store.query_page(page=1, page_size=25)
    cold_projection_ms = (time.perf_counter() - cold_started) * 1_000
    refresh_samples: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        store.query_page(page=1, page_size=25)
        refresh_samples.append((time.perf_counter() - started) * 1_000)

    incremental_samples: list[float] = []
    if store._ordered_ids:
        writer = BackgroundStore(registry_root)
        session_id = store._ordered_ids[0]
        for index in range(min(samples, 25)):
            writer.update(session_id, last_active=float(time.time() + index))
            started = time.perf_counter()
            store.query_page(page=1, page_size=25)
            incremental_samples.append((time.perf_counter() - started) * 1_000)

    manager = BackgroundManager(
        Console(width=120, height=25, record=True),
        store=store,
    )
    manager._async_mode = True
    manager._projection_ready = False
    first_frame_started = time.perf_counter()
    manager.render()
    first_frame_ms = (time.perf_counter() - first_frame_started) * 1_000
    page = await manager._service.refresh_page_async(page=1, page_size=25)
    manager._apply_async_page(page)
    key_samples: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        manager.handle_key("DOWN")
        manager.render()
        key_samples.append((time.perf_counter() - started) * 1_000)
    maintenance_started = time.perf_counter()
    await manager.maintain_async(force=True)
    maintenance_ms = (time.perf_counter() - maintenance_started) * 1_000
    activity_ms = 0.0
    if page.sessions:
        activity_session = store.get("bench-000000")
        activity_started = time.perf_counter()
        await manager._service.run_blocking(manager._activity_lines, activity_session)
        activity_ms = (time.perf_counter() - activity_started) * 1_000
        selected_id = page.sessions[0].session_id
        dispatch_started = time.perf_counter()
        manager._start_operation(
            selected_id,
            "pin",
            lambda: manager._service.pin_async(selected_id, True),
        )
        action_dispatch_ms = (time.perf_counter() - dispatch_started) * 1_000
        pending = tuple(manager._operation_tasks.values())
        if pending:
            await asyncio.gather(*pending)
    else:
        action_dispatch_ms = 0.0
    if manager._activity_task is not None:
        manager._activity_task.cancel()
        await asyncio.gather(manager._activity_task, return_exceptions=True)
    if manager._refresh_task is not None and not manager._refresh_task.done():
        manager._refresh_task.cancel()
        await asyncio.gather(manager._refresh_task, return_exceptions=True)
    await manager._service.close()
    return {
        "first_frame_ms": round(first_frame_ms, 3),
        "cold_projection_ms": round(cold_projection_ms, 3),
        "projection_refresh": _percentiles(refresh_samples),
        "incremental_refresh": _percentiles(incremental_samples),
        "key_to_render": _percentiles(key_samples),
        "maintenance_ms": round(maintenance_ms, 3),
        "activity_read_ms": round(activity_ms, 3),
        "action_dispatch_ms": round(action_dispatch_ms, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=10_000)
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--active-workers", type=int, default=32)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--directory", type=Path)
    parser.add_argument(
        "--compare-sqlite",
        action="store_true",
        help="also measure a transactional SQLite materialized-projection prototype",
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
    sqlite_candidate = (
        _measure_sqlite_candidate(
            event_path,
            root / "sqlite-candidate.db",
        )
        if args.compare_sqlite
        else None
    )
    result = asyncio.run(_measure(root / "registry", args.samples))
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
                "sqlite_candidate": sqlite_candidate,
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
