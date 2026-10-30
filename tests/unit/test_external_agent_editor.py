from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.agents.external_editor import open_definition_in_editor
from agenthicc.agents.library import agent_directory

pytestmark = pytest.mark.unit


def test_external_editor_uses_argv_and_rejects_paths_outside_definition_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    directory = agent_directory("project", project_root=project)
    directory.mkdir(parents=True)
    path = directory / "reviewer.md"
    path.write_text("---\nname: reviewer\ndescription: Reviews code\n---\n", encoding="utf-8")
    monkeypatch.setenv("EDITOR", "editor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    invoked: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> SimpleNamespace:
        invoked.append(command)
        assert check is False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)

    open_definition_in_editor(path, scope="project", project_root=project)

    assert invoked == [["editor", "--wait", str(path)]]
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError, match="scope"):
        open_definition_in_editor(outside, scope="project", project_root=project)
