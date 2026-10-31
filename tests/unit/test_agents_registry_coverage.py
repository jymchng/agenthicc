"""Coverage for named agent discovery and per-turn instance construction."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import agenthicc.agents.registry as registry_module

from agenthicc.agents.definitions import AgentSpec
from agenthicc.agents.registry import BUILTIN_NAMES, AgentsRegistry, build_agents_registry

pytestmark = pytest.mark.unit


def test_agents_registry_prompt_instance_and_plugin_discovery(tmp_path: Path) -> None:
    registry = AgentsRegistry()
    registry.register(
        AgentSpec("custom", "A custom agent", prompt="Project prompt.", source="project")
    )
    registry.register(AgentSpec("custom", "A custom agent", prompt="User prompt.", source="user"))
    assert registry.get("custom") is not None
    assert registry.get("custom").source == "user"
    assert registry.get("missing") is None
    assert len(registry.all()) == 1
    assert registry.get_role_system_prompt("custom") == "User prompt."
    assert registry.get_role_system_prompt("missing") == ""
    cls, instance = registry.make_instance("custom", [], "model", base_system_prompt="base")
    assert cls and instance

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "plugin.py").write_text(
        "from agenthicc.agents.plugin import AgentPlugin\n"
        "class ProjectAgent(AgentPlugin):\n"
        "    name = 'project_agent'\n",
        encoding="utf-8",
    )
    (agents_dir / "broken.py").write_text("raise RuntimeError('broken')\n", encoding="utf-8")
    (agents_dir / "_private.py").write_text("raise RuntimeError('ignored')\n", encoding="utf-8")
    built = build_agents_registry(project_dir=tmp_path, user_dir=tmp_path / "missing")
    project_agent = built.get("project_agent")
    assert project_agent is not None
    assert project_agent.source == "python-project"
    assert built.get("broken") is None
    assert any(
        entry.path == agents_dir / "broken.py" and entry.state == "invalid"
        for entry in built.catalog
    )
    assert not any(entry.path == agents_dir / "_private.py" for entry in built.catalog)


def test_markdown_definition_precedence_reports_shadowed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user_root = tmp_path / "user"
    project_root = tmp_path / "project"
    user_agents = user_root / "agents"
    project_agents = project_root / "agents"
    user_agents.mkdir(parents=True)
    project_agents.mkdir(parents=True)
    definition = "---\nname: shared\ndescription: Shared role\nmode: subagent\n---\n"
    (user_agents / "shared.md").write_text(definition + "User prompt.", encoding="utf-8")
    (project_agents / "shared.md").write_text(definition + "Project prompt.", encoding="utf-8")
    monkeypatch.setattr(registry_module, "_trusted", lambda _path: True)

    with caplog.at_level(logging.WARNING, logger="agenthicc.agents.registry"):
        registry = build_agents_registry(project_dir=project_root, user_dir=user_root)

    winner = registry.get("shared")
    assert winner is not None
    assert winner.source == "project"
    assert winner.prompt == "Project prompt."
    entries = [entry for entry in registry.catalog if entry.name == "shared"]
    assert [(entry.source, entry.state, entry.shadowed_by) for entry in entries] == [
        ("user", "shadowed", "project"),
        ("project", "active", ""),
    ]
    reports = [record.message for record in caplog.records if "'shared'" in record.message]
    assert len(reports) == 1
    assert "user" in reports[0] and "project" in reports[0]


def test_duplicate_markdown_names_are_invalid_and_name_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    agents_dir = project_root / "agents"
    agents_dir.mkdir(parents=True)
    definition = "---\nname: duplicate\ndescription: Duplicate role\nmode: subagent\n---\n"
    first = agents_dir / "first.md"
    second = agents_dir / "second.md"
    first.write_text(definition, encoding="utf-8")
    second.write_text(definition, encoding="utf-8")
    monkeypatch.setattr(registry_module, "_trusted", lambda _path: True)

    registry = build_agents_registry(project_dir=project_root, user_dir=tmp_path / "missing")

    entries = [entry for entry in registry.catalog if entry.name == "duplicate"]
    assert len(entries) == 2
    assert all(entry.state == "invalid" for entry in entries)
    assert all(entry.error is not None for entry in entries)
    assert str(first) in str(entries[0].error)
    assert str(second) in str(entries[0].error)
    assert registry.get("duplicate") is None


def test_project_definition_must_opt_in_to_override_builtin_subagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    agents_dir = project_root / "agents"
    agents_dir.mkdir(parents=True)
    path = agents_dir / "explorer.md"
    path.write_text(
        "---\nname: explorer\ndescription: Project explorer\nmode: subagent\n---\nProject prompt.",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_module, "_trusted", lambda _path: True)

    registry = build_agents_registry(project_dir=project_root, user_dir=tmp_path / "missing")

    entry = next(
        item for item in registry.catalog if item.source == "project" and item.name == "explorer"
    )
    winner = registry.get("explorer")
    assert entry.state == "invalid"
    assert entry.error is not None
    assert "built-in subagent type 'explorer'" in str(entry.error)
    assert "override: true" in str(entry.error)
    assert winner is not None and winner.source == "builtin"


def test_project_definition_with_override_key_shadows_builtin_subagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    agents_dir = project_root / "agents"
    agents_dir.mkdir(parents=True)
    path = agents_dir / "explorer.md"
    path.write_text(
        "---\nname: explorer\ndescription: Project explorer\nmode: subagent\n"
        "override: true\n---\nProject prompt.",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_module, "_trusted", lambda _path: True)

    registry = build_agents_registry(project_dir=project_root, user_dir=tmp_path / "missing")

    entry = next(
        item for item in registry.catalog if item.source == "project" and item.name == "explorer"
    )
    winner = registry.get("explorer")
    assert entry.state == "active"
    assert winner is not None and winner.source == "project"
    assert winner.prompt == "Project prompt."


def test_builtin_agent_descriptors_materialize_only_when_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenthicc.agents.registry as registry_module

    original_load_builtin = registry_module._load_builtin
    loaded: list[str] = []

    def record_load(name: str) -> AgentSpec:
        loaded.append(name)
        return original_load_builtin(name)

    monkeypatch.setattr(registry_module, "_load_builtin", record_load)
    registry = build_agents_registry(
        project_dir=Path("/nonexistent"),
        user_dir=Path("/nonexistent"),
        load_external=False,
    )

    assert registry.names() == list(BUILTIN_NAMES)
    assert loaded == []
    assert registry.get("auto") is not None
    assert loaded == ["auto"]
    assert registry.get("auto") is not None
    assert loaded == ["auto"]


def test_progressive_registry_skips_external_python_until_requested(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    marker = tmp_path / "imported"
    (agents_dir / "external.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('yes')\n"
        "from agenthicc.agents.plugin import AgentPlugin\n"
        "class ExternalAgent(AgentPlugin):\n"
        "    name = 'external'\n",
        encoding="utf-8",
    )

    lazy = build_agents_registry(
        project_dir=tmp_path,
        user_dir=tmp_path / "missing",
        load_external=False,
    )
    assert "external" not in lazy.names()
    assert not marker.exists()

    eager = build_agents_registry(
        project_dir=tmp_path,
        user_dir=tmp_path / "missing",
        load_external=True,
    )
    assert eager.get("external") is not None
    assert marker.read_text(encoding="utf-8") == "yes"


def test_registry_replacement_preserves_existing_reference() -> None:
    current = build_agents_registry(
        project_dir=Path("/nonexistent"),
        user_dir=Path("/nonexistent"),
        load_external=False,
    )
    replacement = AgentsRegistry()
    replacement.register(AgentSpec("custom", "A custom agent", source="project"))

    current.replace_with(replacement)

    assert current.names() == ["custom"]
    assert current.get("custom") is not None
