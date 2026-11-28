"""MCP-01: the MCP client library is usable, or the failure says what is wrong and how to fix it."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from agenthicc.tools import mcp_availability
from agenthicc.tools.mcp_availability import (
    MINIMUM_VERSION,
    PACKAGE,
    REQUIREMENT,
    McpClientStatus,
    McpUnavailable,
    check_mcp_client,
    fix_command,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _hide_library(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec

    def find_spec(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        return None if name == "lauren_mcp" else real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_availability.importlib.util, "find_spec", find_spec)


def _installed_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    real = importlib.metadata.version

    def fake(name: str) -> str:
        return version if name == PACKAGE else real(name)

    monkeypatch.setattr(mcp_availability.importlib.metadata, "version", fake)


# ── the real library, as CI installs it ───────────────────────────────────────


def test_the_real_client_library_imports_and_meets_the_floor() -> None:
    """CI installs the client with the project, so a dropped dependency fails here (D5)."""
    import lauren_mcp  # noqa: F401  (the import is the assertion)

    status = check_mcp_client()
    assert status.available, status.summary()
    assert status.reason == "" and status.fix == ""
    parts = tuple(int(part) for part in status.installed.split(".")[:3])
    assert parts >= MINIMUM_VERSION


def test_the_declared_requirement_is_the_one_in_pyproject() -> None:
    """The text a user is told to install is the dependency the project declares."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = declared["project"]["dependencies"]
    assert REQUIREMENT in dependencies
    assert "mcp" not in declared["project"].get("optional-dependencies", {})


# ── the three ways it can be unusable ─────────────────────────────────────────


def test_a_missing_library_is_reported_with_its_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_library(monkeypatch)

    status = check_mcp_client()

    assert not status.available
    assert status.reason == "dependency_missing"
    assert status.summary() == "lauren-mcp is not installed"
    assert status.to_dict()["client_available"] is False
    assert status.to_dict()["cause"] == "lauren-mcp is not installed"


@pytest.mark.parametrize(
    ("version", "usable"),
    [
        ("1.1.0", False),
        ("1.1.1.dev0+g8b90d0ba8", False),
        ("1.2.0", False),
        ("1.2.1", True),
        ("1.2.2.dev0+gabc", True),
        ("1.10.0", True),
    ],
)
def test_a_release_older_than_the_floor_is_incompatible(
    monkeypatch: pytest.MonkeyPatch, version: str, usable: bool
) -> None:
    _installed_version(monkeypatch, version)

    status = check_mcp_client()

    assert status.available is usable
    if not usable:
        assert status.reason == "dependency_incompatible"
        assert version in status.summary()
        assert "1.2.1" in status.summary()


def test_an_installed_library_that_fails_to_import_is_broken_not_missing() -> None:
    status = check_mcp_client(imported=False, import_error="ImportError: No module named 'x'")

    assert not status.available
    assert status.reason == "dependency_broken"
    assert "installed but cannot be imported" in status.summary()
    assert "No module named 'x'" in status.summary()


# ── the fix names the interpreter that failed ─────────────────────────────────


def test_the_fix_targets_the_running_interpreter_and_the_declared_requirement() -> None:
    fix = fix_command()

    assert fix.startswith(shlex.quote(sys.executable) + " -m pip install ")
    assert shlex.quote(REQUIREMENT) in fix
    assert f"uv pip install --python {shlex.quote(sys.executable)}" in fix
    assert fix.count("lauren-mcp[http,ws]>=1.2.1,<2") == 2  # once per command


def test_the_fix_quotes_an_interpreter_path_with_spaces() -> None:
    fix = fix_command("/opt/my python/bin/python")

    assert fix.startswith("'/opt/my python/bin/python' -m pip install ")


# ── the error ─────────────────────────────────────────────────────────────────


def test_the_error_message_carries_both_the_cause_and_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hide_library(monkeypatch)
    status = check_mcp_client()

    error = McpUnavailable(status)

    assert error.status is status
    assert "lauren-mcp is not installed" in str(error)
    assert status.fix in str(error)


def test_an_available_status_has_no_cause_or_fix() -> None:
    status = McpClientStatus(True, installed="1.2.2")

    assert status.to_dict()["cause"] == ""
    assert status.to_dict()["fix"] == ""
    assert status.summary() == "the MCP client is available"
