"""Regression coverage for PRD-151 command and service lifecycle contracts."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from agenthicc.background.terminals import TerminalManager
from agenthicc.tools.exec import (
    RunBashTool,
    RunCommandTool,
    WaitTerminalReadinessTool,
    WaitTerminalTool,
)
from agenthicc.tools.exec.agent_tools import run_command
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.executor import normalize_result
from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.plugin import PhaseOutput, PhaseSpec

pytestmark = pytest.mark.unit


async def test_nonzero_foreground_command_is_authoritative_failure(tmp_path: Path) -> None:
    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "-c", "print('diagnostic'); raise SystemExit(7)"], "timeout": 2},
        {"workspace_root": str(tmp_path)},
    )

    assert result["ok"] is False
    assert result["state"] == "failed"
    assert result["returncode"] == 7
    assert result["stdout"] == "diagnostic\n"


async def test_timeout_is_seconds_and_preserves_diagnostic_tail(tmp_path: Path) -> None:
    result = await RunCommandTool().execute(
        {
            "argv": [
                sys.executable,
                "-c",
                "import sys,time; print('before-timeout', flush=True); time.sleep(30)",
            ],
            "timeout": 0.2,
        },
        {"workspace_root": str(tmp_path)},
    )

    assert result["ok"] is False
    assert result["state"] == "timed_out"
    assert result["timed_out"] is True
    assert result["deadline"]["owner"] == "command"  # type: ignore[index]
    assert "before-timeout" in result["stdout"]
    assert result["cleanup_result"] in {"graceful_timeout", "force_timeout", "cleanup_unproven"}


@pytest.mark.parametrize("timeout", [-1, math.inf, math.nan, "500ms"])
async def test_invalid_timeout_is_rejected_before_spawn(tmp_path: Path, timeout: object) -> None:
    result = await RunBashTool().execute(
        {"command": "echo should-not-run", "timeout": timeout},
        {"workspace_root": str(tmp_path)},
    )

    assert result["ok"] is False
    assert result["state"] == "rejected"
    assert result["cleanup_result"] == "not_spawned"


async def test_zero_timeout_means_no_operation_deadline(tmp_path: Path) -> None:
    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "-c", "print('no deadline')"], "timeout": 0},
        {"workspace_root": str(tmp_path)},
    )
    assert result["ok"] is True
    assert result["state"] == "exited"
    assert result["deadline"] is None


async def test_wait_timeout_validation_is_in_seconds_and_pre_spawn(tmp_path: Path) -> None:
    manager = TerminalManager(
        session_id="prd151-wait",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
    )
    try:
        result = await WaitTerminalTool().execute(
            {"terminal_id": "term-missing", "timeout": math.inf},
            {"terminal_manager": manager},
        )
        assert result["ok"] is False
        assert result["state"] == "rejected"
    finally:
        await manager.close()


async def test_cwd_and_environment_are_passed_without_shell_prefix(tmp_path: Path) -> None:
    result = await RunCommandTool().execute(
        {
            "argv": [
                sys.executable,
                "-c",
                "import os; print(os.getcwd()); print(os.getenv('PRD151'))",
            ],
            "cwd": str(tmp_path),
            "env": {"PRD151": "yes"},
        },
        {"workspace_root": "/unrelated"},
    )
    assert result["ok"] is True
    assert str(tmp_path) in str(result["stdout"])
    assert "yes" in str(result["stdout"])
    assert result["cwd"] == str(tmp_path)
    assert result["argv"] == [
        sys.executable,
        "-c",
        "import os; print(os.getcwd()); print(os.getenv('PRD151'))",
    ]


def test_legacy_process_mapping_cannot_normalize_to_success() -> None:
    normalized = normalize_result(
        {"stdout": "bad\n", "stderr": "", "returncode": 7, "timed_out": False}
    )
    assert normalized.ok is False
    assert normalized.error_kind == "execution"
    assert normalized.value["returncode"] == 7  # type: ignore[index]


async def test_lauren_wrapper_and_agenthicc_executor_propagate_command_failure(
    tmp_path: Path,
) -> None:
    wrapped = await run_command(
        [sys.executable, "-c", "raise SystemExit(7)"], cwd=str(tmp_path), timeout=2
    )
    assert wrapped["ok"] is False
    assert wrapped["state"] == "failed"

    executor = AgenthiccToolExecutor([RunCommandTool()])
    adapted = await executor.execute(
        "run_command",
        {"argv": [sys.executable, "-c", "raise SystemExit(7)"], "cwd": str(tmp_path)},
        "prd151-executor",
    )
    assert adapted.ok is False
    assert adapted.error_kind == "execution"


async def test_service_marker_readiness_keeps_process_owned_and_blocks_duplicates(
    tmp_path: Path,
) -> None:
    manager = TerminalManager(
        session_id="prd151",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
        cancel_grace_s=0.1,
    )
    try:
        command = f"{sys.executable} -c \"import time; print('READY', flush=True); time.sleep(30)\""
        started = await RunBashTool().execute(
            {
                "command": command,
                "background": True,
                "lifecycle": "service",
                "readiness": {"marker": "READY", "timeout": 2},
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        assert started["terminal_id"]
        assert started["ready"] is True
        assert started["state"] == "running"
        assert manager.running_count() == 1

        duplicate = await RunBashTool().execute(
            {"command": command, "background": True, "lifecycle": "service"},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        assert duplicate["ok"] is False
        assert duplicate["state"] == "rejected"
        assert "already owned" in str(duplicate["error"])

        ready = await WaitTerminalReadinessTool().execute(
            {"terminal_id": str(started["terminal_id"]), "readiness": {"marker": "READY"}},
            {"terminal_manager": manager},
        )
        assert ready["ready"] is True
    finally:
        await manager.close()


async def test_service_readiness_timeout_does_not_stop_process(tmp_path: Path) -> None:
    manager = TerminalManager(
        session_id="prd151",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
        cancel_grace_s=0.1,
    )
    started = await RunCommandTool().execute(
        {
            "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
            "background": True,
            "lifecycle": "service",
            "readiness": {"marker": "NEVER", "timeout": 0.05},
        },
        {"workspace_root": str(tmp_path), "terminal_manager": manager},
    )
    assert started["state"] == "starting_timeout"
    assert started["readiness_timeout"] is True
    assert manager.running_count() == 1
    await manager.stop(str(started["terminal_id"]), reason="test cleanup")
    await manager.close()


async def test_service_record_survives_restart_without_duplicate_start(tmp_path: Path) -> None:
    store_root = tmp_path / "registry"
    first = TerminalManager(
        session_id="prd151-resume",
        cwd=tmp_path,
        store_root=store_root,
        cancel_grace_s=0.1,
    )
    command = f"{sys.executable} -c \"import time; print('READY', flush=True); time.sleep(30)\""
    started = await RunBashTool().execute(
        {
            "command": command,
            "background": True,
            "lifecycle": "service",
            "readiness": {"marker": "READY", "timeout": 2},
        },
        {"workspace_root": str(tmp_path), "terminal_manager": first},
    )
    terminal_id = str(started["terminal_id"])
    second = TerminalManager(
        session_id="prd151-resume",
        cwd=tmp_path,
        store_root=store_root,
        cancel_grace_s=0.1,
    )
    try:
        record = second.get(terminal_id)
        assert record is not None
        assert record.state.value == "orphaned"
        assert record.ready is True
        duplicate = await RunBashTool().execute(
            {"command": command, "background": True, "lifecycle": "service"},
            {"workspace_root": str(tmp_path), "terminal_manager": second},
        )
        assert duplicate["state"] == "rejected"
        assert "already owned" in str(duplicate["error"])
    finally:
        await first.close()
        await second.close()


def test_workflow_command_gate_rejects_failed_and_accepts_successful_outcomes() -> None:
    required = PhaseSpec(name="build", require_successful_commands=True)
    failed = PhaseOutput(
        phase_name="build",
        role="executor",
        metadata={
            "command_outcomes": [
                {"state": "failed", "ok": False, "returncode": 7, "stderr": "bad build"}
            ]
        },
    )
    successful = PhaseOutput(
        phase_name="build",
        role="executor",
        metadata={"command_outcomes": [{"state": "exited", "ok": True, "returncode": 0}]},
    )
    assert WorkflowRunner._command_gate_error(required, failed)
    assert WorkflowRunner._command_gate_error(required, successful) is None


def test_service_phase_requires_background_and_readiness_is_typed() -> None:
    with pytest.raises(ValueError, match="requires terminal_wait_policy"):
        PhaseSpec(name="preview", command_lifecycle="service")
    with pytest.raises(ValueError, match="requires command_lifecycle"):
        PhaseSpec(name="build", require_readiness=True)
