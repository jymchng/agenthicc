"""Integration coverage for the public PRD-139 bootstrap entry points."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


def test_agenthicc_init_creates_the_project_scaffold(tmp_path, monkeypatch, capsys):
    from agenthicc.__main__ import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "integration-app"\n')

    with patch("sys.argv", ["agenthicc", "init"]):
        main()
    output = capsys.readouterr().out
    assert "Initialized" in output
    assert (tmp_path / "AGENTS.md").read_text() == ""
    config = tmp_path / ".agenthicc" / ".agenthicc.toml"
    assert config.exists()
    assert all(
        not line.strip() or line.lstrip().startswith("#")
        for line in config.read_text().splitlines()
    )

    with patch("sys.argv", ["agenthicc", "init"]):
        main()
    repeated = capsys.readouterr().out
    assert "Preserved AGENTS.md" in repeated
    assert "Preserved .agenthicc/.agenthicc.toml" in repeated


def test_agenthicc_init_force_replaces_existing_scaffold_files(tmp_path, monkeypatch, capsys):
    from agenthicc.__main__ import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Team-owned guidance\n")

    with patch("sys.argv", ["agenthicc", "init"]):
        main()

    output = capsys.readouterr().out
    assert "Preserved AGENTS.md" in output
    assert (tmp_path / "AGENTS.md").read_text() == "# Team-owned guidance\n"

    with patch("sys.argv", ["agenthicc", "init", "--force"]):
        main()
    assert (tmp_path / "AGENTS.md").read_text() == ""
    assert "Overwrote AGENTS.md" in capsys.readouterr().out
