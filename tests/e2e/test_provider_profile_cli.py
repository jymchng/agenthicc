"""CLI-level provider profile journeys."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run(tmp_path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        [sys.executable, "-m", "agenthicc", "--config", str(tmp_path / "agenthicc.toml"), *args],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_profiles_validate_and_show_are_safe_cli_surfaces(tmp_path) -> None:
    (tmp_path / "agenthicc.toml").write_text(
        textwrap.dedent(
            """
            [execution]
            profile = "modal"

            [providers.modal]
            provider = "openai"
            model = "moonshotai/Kimi-K3"
            base_url = "https://modal.example/v1"
            """
        ),
        encoding="utf-8",
    )

    profiles = _run(tmp_path, "config", "profiles")
    assert profiles.returncode == 0, profiles.stderr
    assert "modal\topenai\tmoonshotai/Kimi-K3\thttps://modal.example/v1" in profiles.stdout

    valid = _run(tmp_path, "config", "validate")
    assert valid.returncode == 0, valid.stderr
    assert "Configuration is valid" in valid.stdout

    shown = _run(tmp_path, "config", "show")
    assert shown.returncode == 0, shown.stderr
    assert "[providers]" in shown.stdout
    assert "https://modal.example/v1" in shown.stdout


def test_set_secret_cli_redacts_value_in_config_show(tmp_path) -> None:
    env = os.environ.copy()
    env["MODAL_KEY"] = "super-secret-header"
    env["HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenthicc",
            "--config",
            str(tmp_path / "agenthicc.toml"),
            "--set-secret",
            "execution.default_headers.Modal-Key=MODAL_KEY",
            "config",
            "show",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "MODAL_KEY" in result.stdout
    assert "super-secret-header" not in result.stdout
