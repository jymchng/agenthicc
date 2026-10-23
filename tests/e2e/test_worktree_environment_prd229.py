from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


@cli_test_timeout(3)
def test_cli_trust_is_explicit_digest_bound_and_prints_execution_preview(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "E2E")
    (repository / ".agenthicc").mkdir()
    (repository / ".agenthicc" / "environment.toml").write_text(
        "version = 1\n"
        "[[setup]]\n"
        'id = "bootstrap"\n'
        'command = "python -m pip install -r requirements.txt"\n'
        'cwd = "."\n'
        "timeout_s = 90\n"
        'network = "requires_network"\n',
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial")
    home = tmp_path / "home"
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")

    preview = subprocess.run(
        [sys.executable, "-m", "agenthicc", "worktrees", "trust", str(repository)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert preview.returncode == 0
    assert "python -m pip install -r requirements.txt" in preview.stdout
    assert "requires_network" in preview.stdout
    assert "No trust was recorded" in preview.stdout
    trust_path = home / ".agenthicc" / "environment-trust.json"
    assert not trust_path.exists()

    confirmed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenthicc",
            "worktrees",
            "trust",
            str(repository),
            "--confirm",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert "Trusted this exact manifest digest" in confirmed.stdout
    saved = json.loads(trust_path.read_text(encoding="utf-8"))
    assert len(saved["trusted"]) == 1

    (repository / ".agenthicc" / "environment.toml").write_text(
        'version = 1\n[actions.test]\ncommand = "pytest -q"\n', encoding="utf-8"
    )
    changed = subprocess.run(
        [sys.executable, "-m", "agenthicc", "worktrees", "trust", str(repository)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert changed.returncode == 0
    assert "No trust was recorded" in changed.stdout
