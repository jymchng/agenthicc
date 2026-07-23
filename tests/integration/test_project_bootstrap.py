"""Integration coverage for the public PRD-139 bootstrap entry points."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


def test_agenthicc_init_preview_then_write(tmp_path, monkeypatch, capsys):
    from agenthicc.__main__ import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "integration-app"\n')

    with patch("sys.argv", ["agenthicc", "init"]):
        main()
    preview = capsys.readouterr().out
    assert "integration-app" in preview
    assert "Preview only" in preview
    assert not (tmp_path / "AGENTS.md").exists()

    with patch("sys.argv", ["agenthicc", "init", "--write"]):
        main()
    written = capsys.readouterr().out
    assert "Updated" in written
    assert "integration-app" in (tmp_path / "AGENTS.md").read_text()


def test_agenthicc_init_existing_file_requires_explicit_force(tmp_path, monkeypatch, capsys):
    from agenthicc.__main__ import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Team-owned guidance\n")

    with patch("sys.argv", ["agenthicc", "init", "--write"]):
        main()

    output = capsys.readouterr().out
    assert "Refusing to overwrite" in output
    assert (tmp_path / "AGENTS.md").read_text() == "# Team-owned guidance\n"
