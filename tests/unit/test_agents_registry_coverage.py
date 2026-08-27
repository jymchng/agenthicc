"""Coverage for named agent discovery and per-turn instance construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.agents.builtin import AutoAgent
from agenthicc.agents.plugin import AgentDefinition, READ_CAPS
from agenthicc.agents.registry import AgentsRegistry, build_agents_registry

pytestmark = pytest.mark.unit


def test_agents_registry_prompt_instance_and_plugin_discovery(tmp_path: Path) -> None:
    registry = AgentsRegistry()
    definition = AgentDefinition("custom", AutoAgent, READ_CAPS, source="project")
    registry.register(definition)
    registry.register(AgentDefinition("custom", AutoAgent, None, source="user"))
    assert registry.get("custom") is not None
    assert registry.get("missing") is None
    assert len(registry.all()) == 1
    assert registry.get_role_system_prompt("custom")
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
    assert built.get("project_agent") is not None


def test_builtin_agent_descriptors_materialize_only_when_selected() -> None:
    registry = build_agents_registry(
        project_dir=Path("/nonexistent"),
        user_dir=Path("/nonexistent"),
    )
    assert registry.names() == [
        "planner",
        "executor",
        "reviewer",
        "explorer",
        "verifier",
        "human",
        "auto",
    ]
    assert registry.get("auto") is not None


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
    replacement.register(AgentDefinition("custom", AutoAgent, source="project"))

    current.replace_with(replacement)

    assert current.names() == ["custom"]
    assert current.get("custom") is not None
