from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.agents.definitions import AgentSpec, parse_definition
from agenthicc.agents.library import delete_definition, write_definition
from agenthicc.plugins.registry import ToolRegistry
from agenthicc.runners.agent_turn import _effective_agent_tool_names
from agenthicc.tools.capabilities import tool_read, tool_write
from agenthicc.tui.conversation_store import AppState


def _spec(name: str = "reviewer") -> AgentSpec:
    return AgentSpec(
        name=name, description="Reviews code", prompt="Review carefully.", mode="subagent"
    )


def test_effective_agent_tool_names_apply_name_and_capability_intersections() -> None:
    @tool_read
    def read_file() -> str:
        return "read"

    @tool_write
    def write_file() -> str:
        return "written"

    registry = ToolRegistry()
    registry.register_many([read_file, write_file], group="fs")
    spec = AgentSpec(
        name="reader",
        description="Reads files",
        tools=("fs.*",),
        capabilities=("read",),
    )

    names = _effective_agent_tool_names(spec, registry, AppState.create())

    assert names == ("read_file",)


def test_write_definition_is_atomic_markdown_and_trusted_for_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "project"

    path = write_definition(_spec(), scope="project", project_root=project)

    assert path == project / ".agenthicc" / "agents" / "reviewer.md"
    assert parse_definition(
        path.read_text(encoding="utf-8"), path=path, source="project"
    ).prompt == ("Review carefully.")
    from agenthicc.plugins.trust import current_extension_trust

    assert current_extension_trust().check(path, kind="agent") is None


def test_write_definition_refuses_existing_target_unless_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust

    trust = ExtensionTrust(store_path=tmp_path / "trust.json", trusted_roots=())
    project = tmp_path / "project"
    with override_extension_trust(trust):
        path = write_definition(_spec(), scope="project", project_root=project)

        with pytest.raises(FileExistsError):
            write_definition(_spec(), scope="project", project_root=project)

        changed = AgentSpec(name="reviewer", description="New description", mode="subagent")
        write_definition(changed, scope="project", project_root=project, overwrite=True)
        assert "description: New description" in path.read_text(encoding="utf-8")
        assert trust.check(path, kind="agent") is not None


def test_write_definition_refuses_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "project"
    directory = project / ".agenthicc" / "agents"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("do not replace", encoding="utf-8")
    (directory / "reviewer.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        write_definition(_spec(), scope="project", project_root=project, overwrite=True)
    assert outside.read_text(encoding="utf-8") == "do not replace"


def test_delete_definition_only_removes_project_or_user_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "project"
    path = write_definition(_spec(), scope="project", project_root=project)

    delete_definition(path, project_root=project)

    assert not path.exists()
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        delete_definition(outside, project_root=project)
    assert outside.exists()
