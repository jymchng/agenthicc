"""End-to-end smoke coverage for the parallel worker control plane."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_worktrees_cli_is_available_without_a_provider(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path / "home")
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "agenthicc", "worktrees", "list"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "No parallel orchestrations." in result.stdout
