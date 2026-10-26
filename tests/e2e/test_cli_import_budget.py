"""Fresh-interpreter import budgets for CLI commands (AGX-01).

Module counts are exact on every host, unlike timings, so they are the
regression signal for "a command imports code it does not use".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = pytest.mark.e2e

_PROBE = """
import atexit, io, json, runpy, sys
argv = json.loads(sys.argv[1])
def report():
    sys.__stderr__.write("MODULES " + json.dumps(sorted(sys.modules)) + "\\n")
atexit.register(report)
sys.argv = ["agenthicc", *argv]
sys.stdout = io.StringIO()
try:
    runpy.run_module("agenthicc", run_name="__main__", alter_sys=True)
except SystemExit:
    pass
"""

# Listing commands must not pay for transports, providers or the TUI runtime.
_DENIED = ("aiohttp", "httpx", "lauren_ai", "agenthicc.runners.tui_session", "rich.live")


def _modules(argv: list[str], home: Path) -> list[str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(argv)],
        cwd=home,
        env=environment,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    line = next(item for item in result.stderr.splitlines() if item.startswith("MODULES "))
    modules: list[str] = json.loads(line.removeprefix("MODULES "))
    return modules


@cli_test_timeout(1)
@pytest.mark.parametrize(
    "argv",
    [
        ["agents", "--json"],
        ["agents", "list", "--json"],
        ["runs", "--json"],
        ["sessions", "list"],
    ],
)
def test_listing_commands_stay_within_their_import_budget(argv: list[str], tmp_path: Path) -> None:
    modules = _modules(argv, tmp_path)
    assert len(modules) <= 200, len(modules)
    denied = [name for name in modules if name.startswith(_DENIED)]
    assert not denied, denied


@cli_test_timeout(1)
@pytest.mark.parametrize("argv", [["--help"], ["config", "--help"], ["no-such-command"]])
def test_help_and_errors_import_no_command_module(argv: list[str], tmp_path: Path) -> None:
    modules = _modules(argv, tmp_path)
    commands = [name for name in modules if name.startswith("agenthicc.cli.commands.")]
    assert not commands, commands


@cli_test_timeout(1)
def test_version_reads_metadata_without_the_parser(tmp_path: Path) -> None:
    modules = _modules(["--version"], tmp_path)
    assert "agenthicc.cli.parser" not in modules
    assert len(modules) <= 160, len(modules)
