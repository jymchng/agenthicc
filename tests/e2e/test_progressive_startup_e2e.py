"""Executable end-to-end coverage for PRD-176 startup boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _environment(home: Path, root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    source = str(root / "src")
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
    environment.pop("AGENTHICC_CONFIG", None)
    return environment


def test_fast_help_and_version_do_not_import_runtime_or_mutate_home(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    home = tmp_path / "home"
    environment = _environment(home, root)
    probe = (
        "import sys\n"
        "from agenthicc.cli.parser import parse_cli\n"
        "try:\n"
        "    parse_cli()\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "else:\n"
        "    raise AssertionError('fast command did not exit')\n"
        "forbidden = {\n"
        "    'agenthicc.runners.tui_session', 'agenthicc.runners.agent_turn',\n"
        "    'agenthicc.memory.vector', 'agenthicc.tools.mcp_manager',\n"
        "    'agenthicc.tools.playwright', 'agenthicc.tools.cloakbrowser', 'lauren_ai',\n"
        "}\n"
        "assert forbidden.isdisjoint(sys.modules), forbidden & set(sys.modules)\n"
    )
    for argument in ("--version", "--help"):
        result = subprocess.run(
            [sys.executable, "-c", probe, argument],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
    assert not home.exists()


def test_large_legacy_store_lists_without_replaying_unselected_sessions(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    home = tmp_path / "home"
    service_root = home / ".agenthicc" / "session-service"
    service_root.mkdir(parents=True)
    selected = "sess_selected"
    for session_number in range(100):
        session_id = selected if session_number == 0 else f"sess_unrelated_{session_number}"
        path = service_root / f"{session_id}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for sequence in range(1, 11):
                handle.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "event_id": f"evt_{uuid.uuid4().hex}",
                            "sequence": sequence,
                            "session_id": session_id,
                            "turn_id": None,
                            "source": "e2e",
                            "kind": "session_created" if sequence == 1 else "turn_completed",
                            "occurred_at": float(sequence),
                            "durability": "durable",
                            "visibility": "session",
                            "payload": {
                                "project_root": str(tmp_path / "project"),
                                "synthetic": "x" * 256,
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )

    probe = (
        "import asyncio\n"
        "from agenthicc.session_service import SessionService\n"
        "async def main():\n"
        "    service = SessionService()\n"
        "    assert not service._runtimes\n"
        "    sessions = await service.list_sessions(capabilities=frozenset({'read'}))\n"
        "    assert len(sessions) == 100\n"
        "    assert not service._runtimes\n"
        "    await service.snapshot('sess_selected', capabilities=frozenset({'read'}))\n"
        "    assert set(service._runtimes) == {'sess_selected'}\n"
        "    await service.close()\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        env=_environment(home, root),
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_offline_startup_benchmark_reports_separate_milestones(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_startup.py",
            "--samples",
            "1",
            "--sessions",
            "2",
            "--events",
            "2",
            "--offline",
        ],
        cwd=root,
        env=_environment(tmp_path / "home", root),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["offline"] is True
    assert data["version_process_ms"]["samples"] == 1
    assert data["help_process_ms"]["samples"] == 1
    assert data["fast_path_metrics"]["--version"][0]["module_count"] > 0
    assert data["fast_path_metrics"]["--help"][0]["module_count"] > 0
    assert data["session_service_cold"]["metadata_bytes_scanned"] >= 0
    assert data["session_service_cold"]["init_metadata_bytes_scanned"] > 0
    assert data["session_service_warm_init_ms"]["samples"] == 1
