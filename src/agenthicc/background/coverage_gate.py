"""Run the PRD-141 maintained-surface coverage gate."""

from __future__ import annotations

import subprocess
import sys


TARGETS = (
    "agenthicc.background",
    "agenthicc.cli.commands.background",
    "agenthicc.tui.workspace.background_manager",
)


def main() -> int:
    command = [sys.executable, "-m", "pytest", "tests", "-q"]
    command.extend(f"--cov={target}" for target in TARGETS)
    command.extend(("--cov-report=term-missing", "--cov-fail-under=90"))
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":  # pragma: no cover - exercised by the command itself
    raise SystemExit(main())
