#!/usr/bin/env python3
"""Measure agenthicc's fast paths and lazy session-service startup.

The benchmark intentionally uses child processes and an isolated HOME. It
reports process-spawn-inclusive timings for user-visible commands separately
from the in-process SessionService constructor/list operation. It does not
contact a provider, changelog endpoint, MCP server, or browser.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import TypedDict


class TimingSummary(TypedDict):
    samples: int
    p50_ms: float
    p95_ms: float
    values_ms: list[float]


_SESSION_PROBE = """
import asyncio
import json
import os
import sys
import time

from agenthicc.session_service import SessionService


async def main() -> None:
    started = time.perf_counter()
    service = SessionService()
    init_ms = (time.perf_counter() - started) * 1000
    init_metadata_bytes_scanned = service.store.metadata_bytes_scanned
    started = time.perf_counter()
    sessions = await service.list_sessions(capabilities=frozenset({"read"}))
    list_ms = (time.perf_counter() - started) * 1000
    source_bytes = 0
    for module in sys.modules.values():
        filename = getattr(module, "__file__", None)
        if isinstance(filename, str):
            try:
                source_bytes += os.stat(filename).st_size
            except OSError:
                pass
    try:
        import resource
        max_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, AttributeError):
        max_rss_kb = 0
    await service.close()
    print(json.dumps({
        "init_ms": init_ms,
        "list_ms": list_ms,
        "sessions": len(sessions),
        "metadata_bytes_scanned": service.store.metadata_bytes_scanned,
        "init_metadata_bytes_scanned": init_metadata_bytes_scanned,
        "module_count": len(sys.modules),
        "module_source_bytes": source_bytes,
        "max_rss_kb": max_rss_kb,
    }))


asyncio.run(main())
"""


_FAST_PATH_PROBE = """
import json
import os
import runpy
import sys

sys.argv = ["agenthicc", __AGENTHICC_ARGUMENT__]
try:
    runpy.run_module("agenthicc", run_name="__main__")
except SystemExit:
    pass
source_bytes = 0
for module in sys.modules.values():
    filename = getattr(module, "__file__", None)
    if isinstance(filename, str):
        try:
            source_bytes += os.stat(filename).st_size
        except OSError:
            pass
try:
    import resource
    max_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
except (ImportError, AttributeError):
    max_rss_kb = 0
print(json.dumps({
    "module_count": len(sys.modules),
    "module_source_bytes": source_bytes,
    "max_rss_kb": max_rss_kb,
}))
"""


def _summary(values: list[float]) -> TimingSummary:
    if not values:
        raise ValueError("benchmark needs at least one sample")
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95 + 0.5) - 1))
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "values_ms": [round(value, 3) for value in values],
    }


def _child_environment(home: Path, root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    source_path = str(root / "src")
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source_path + (os.pathsep + existing if existing else "")
    for key in ("AGENTHICC_CONFIG", "AGENTHICC_CHANGELOG_CACHE"):
        environment.pop(key, None)
    return environment


def _measure_command(
    arguments: list[str],
    *,
    environment: dict[str, str],
) -> float:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "agenthicc", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = (time.perf_counter() - started) * 1000
    if result.returncode != 0:
        raise RuntimeError(
            f"command {' '.join(arguments)!r} failed with {result.returncode}: "
            f"{result.stderr[-1000:]}"
        )
    return elapsed


def _measure_process_spawn(*, environment: dict[str, str]) -> float:
    """Measure a matching child-process baseline for timing interpretation."""
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-c", "pass"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"process baseline failed with {result.returncode}")
    return (time.perf_counter() - started) * 1000


def _measure_fast_path_probe(
    argument: str,
    *,
    environment: dict[str, str],
) -> dict[str, object]:
    """Collect import/RSS metrics without changing the real fast-path command."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _FAST_PATH_PROBE.replace("__AGENTHICC_ARGUMENT__", repr(argument)),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{argument} probe failed: {result.stderr[-1000:]}")
    output = result.stdout.strip().splitlines()
    if not output:
        raise RuntimeError(f"{argument} probe produced no JSON")
    data = json.loads(output[-1])
    if not isinstance(data, dict):
        raise RuntimeError(f"{argument} probe returned a non-object")
    return {str(key): value for key, value in data.items()}


def _write_fixture(home: Path, sessions: int, events: int) -> int:
    service_root = home / ".agenthicc" / "session-service"
    service_root.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    now = time.time()
    for session_number in range(sessions):
        session_id = f"sess_benchmark_{session_number}_{uuid.uuid4().hex[:8]}"
        path = service_root / f"{session_id}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for sequence in range(1, events + 1):
                if sequence == 1:
                    kind = "session_created"
                    payload: dict[str, object] = {
                        "project_root": str(home / "workspace"),
                        "capabilities": ["read"],
                    }
                else:
                    kind = "turn_completed"
                    payload = {"synthetic": True}
                record = {
                    "schema_version": 1,
                    "event_id": f"evt_{uuid.uuid4().hex}",
                    "sequence": sequence,
                    "session_id": session_id,
                    "turn_id": None,
                    "source": "benchmark",
                    "kind": kind,
                    "occurred_at": now + sequence,
                    "durability": "durable",
                    "visibility": "session",
                    "payload": payload,
                }
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        total_bytes += path.stat().st_size
    return total_bytes


def _measure_session_probe(*, home: Path, root: Path) -> dict[str, object]:
    environment = _child_environment(home, root)
    result = subprocess.run(
        [sys.executable, "-c", _SESSION_PROBE],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"session probe failed: {result.stderr[-1000:]}")
    output = result.stdout.strip().splitlines()
    if not output:
        raise RuntimeError("session probe produced no JSON")
    data = json.loads(output[-1])
    if not isinstance(data, dict):
        raise RuntimeError("session probe returned a non-object")
    return {str(key): value for key, value in data.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--events", type=int, default=20)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Document that no remote dependency is allowed (the benchmark is offline by design).",
    )
    args = parser.parse_args(argv)
    if args.samples <= 0 or args.sessions < 0 or args.events <= 0:
        parser.error(
            "--samples must be positive; --sessions must be non-negative; --events must be positive"
        )

    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agenthicc-startup-") as temporary:
        home = Path(temporary)
        environment = _child_environment(home, root)
        process_spawn = [
            _measure_process_spawn(environment=environment) for _ in range(args.samples)
        ]
        version = [
            _measure_command(["--version"], environment=environment) for _ in range(args.samples)
        ]
        help_timings = [
            _measure_command(["--help"], environment=environment) for _ in range(args.samples)
        ]
        fast_metrics = {
            argument: [
                _measure_fast_path_probe(argument, environment=environment)
                for _ in range(args.samples)
            ]
            for argument in ("--version", "--help")
        }
        bytes_written = _write_fixture(home, args.sessions, args.events)
        cold_probe = _measure_session_probe(home=home, root=root)
        warm_probes = [_measure_session_probe(home=home, root=root) for _ in range(args.samples)]

    output = {
        "offline": True,
        "python": sys.version.split()[0],
        "sessions": args.sessions,
        "events_per_session": args.events,
        "event_bytes": bytes_written,
        "process_spawn_ms": _summary(process_spawn),
        "version_process_ms": _summary(version),
        "version_application_ms": _summary(
            [max(0.0, elapsed - baseline) for elapsed, baseline in zip(version, process_spawn)]
        ),
        "help_process_ms": _summary(help_timings),
        "help_application_ms": _summary(
            [max(0.0, elapsed - baseline) for elapsed, baseline in zip(help_timings, process_spawn)]
        ),
        "fast_path_metrics": fast_metrics,
        "session_service_cold": cold_probe,
        "session_service_warm_init_ms": _summary(
            [
                float(probe["init_ms"])
                for probe in warm_probes
                if isinstance(probe.get("init_ms"), (int, float))
            ]
        ),
        "session_service_warm_list_ms": _summary(
            [
                float(probe["list_ms"])
                for probe in warm_probes
                if isinstance(probe.get("list_ms"), (int, float))
            ]
        ),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
