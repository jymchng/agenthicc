"""Startup coordinator and fast CLI regression tests (PRD-176)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from agenthicc.runners.startup import (
    ReadinessState,
    StartupCoordinator,
    StartupDependencyError,
)

pytestmark = pytest.mark.unit


def test_startup_report_uses_monotonic_clock_and_redacts_error_length() -> None:
    ticks = iter([10.0, 12.5])
    coordinator = StartupCoordinator(clock=lambda: next(ticks))

    coordinator.begin("config")
    report = coordinator.finish("config", ReadinessState.FAILED, error="x" * 800)

    assert report.elapsed_s == 2.5
    assert report.to_dict()["state"] == "failed"
    assert len(report.error or "") == 500


def test_startup_report_redacts_credentials_in_failure_diagnostics() -> None:
    coordinator = StartupCoordinator()

    coordinator.begin("mcp")
    report = coordinator.finish(
        "mcp",
        ReadinessState.DEGRADED,
        error="Authorization: Bearer super-secret api_key=another-secret",
    )

    assert report.error is not None
    assert "super-secret" not in report.error
    assert "another-secret" not in report.error
    assert "<redacted>" in report.error


def test_startup_subscribers_are_best_effort_and_unsubscribe_idempotent() -> None:
    coordinator = StartupCoordinator()
    calls: list[str] = []

    def observe() -> None:
        calls.append("called")

    unsubscribe = coordinator.subscribe(observe)
    coordinator.begin("local")
    unsubscribe()
    unsubscribe()
    coordinator.finish("local")

    assert calls == ["called"]


@pytest.mark.asyncio
async def test_background_readiness_waits_and_publishes_result() -> None:
    coordinator = StartupCoordinator()
    release = asyncio.Event()

    async def load() -> object:
        await release.wait()
        return {"ready": True}

    task = coordinator.start_background("plugins", load)
    assert coordinator.report("plugins").state is ReadinessState.LOADING
    release.set()
    assert await task == {"ready": True}
    await coordinator.wait_for("plugins")
    assert coordinator.report("plugins").state is ReadinessState.READY
    await coordinator.close()


@pytest.mark.asyncio
async def test_failed_required_readiness_is_actionable_and_shutdown_is_idempotent() -> None:
    coordinator = StartupCoordinator()

    async def fail() -> object:
        raise RuntimeError("provider unavailable")

    task = coordinator.start_background("provider", fail, degrade_on_error=True)
    assert await task is None
    with pytest.raises(StartupDependencyError, match="provider unavailable"):
        await coordinator.wait_for("provider")
    await coordinator.close()
    await coordinator.close()
    assert coordinator.report("provider").state is ReadinessState.DEGRADED


def test_fast_help_does_not_discover_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.cli import parser

    monkeypatch.setattr(sys, "argv", ["agenthicc", "--help"])

    def fail_discovery(*args: object, **kwargs: object) -> None:
        raise AssertionError("dynamic command discovery must not run for --help")

    monkeypatch.setattr("agenthicc.cli.registry._discover", fail_discovery)
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_cli()
    assert exc_info.value.code == 0


def test_fast_help_does_not_touch_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.cli import parser

    monkeypatch.setattr(sys, "argv", ["agenthicc", "--version"])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_cli()
    assert exc_info.value.code == 0
    assert list(tmp_path.rglob("*")) == []
