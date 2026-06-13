"""Unit tests for agent-scoped tool plugin configuration (PRD-26)."""
from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.plugins.agent_config import (
    AgentDef,
    discover_agents,
    load_agent_system_prompt,
    validate_agent_name,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# validate_agent_name
# ---------------------------------------------------------------------------


def test_validate_agent_name_ok():
    assert validate_agent_name("researcher") == "researcher"
    assert validate_agent_name("sql-analyst") == "sql-analyst"
    assert validate_agent_name("r2d2") == "r2d2"


def test_validate_agent_name_strips_and_lowercases():
    assert validate_agent_name("  Researcher  ") == "researcher"


def test_validate_agent_name_normalises_uppercase():
    # validate_agent_name lowercases the input before validation,
    # so "Researcher" -> "researcher" is valid (no error).
    assert validate_agent_name("Researcher") == "researcher"


def test_validate_agent_name_rejects_leading_hyphen():
    with pytest.raises(ValueError):
        validate_agent_name("-bad")


def test_validate_agent_name_rejects_trailing_hyphen():
    with pytest.raises(ValueError):
        validate_agent_name("bad-")


def test_validate_agent_name_rejects_spaces():
    with pytest.raises(ValueError):
        validate_agent_name("my agent")


def test_validate_agent_name_rejects_empty():
    with pytest.raises(ValueError):
        validate_agent_name("")


def test_validate_agent_name_rejects_too_long():
    with pytest.raises(ValueError):
        validate_agent_name("a" * 65)


def test_validate_agent_name_accepts_max_length():
    assert validate_agent_name("a" * 64) == "a" * 64


def test_validate_agent_name_rejects_special_chars():
    with pytest.raises(ValueError):
        validate_agent_name("my_agent")


def test_validate_agent_name_single_char():
    assert validate_agent_name("a") == "a"


# ---------------------------------------------------------------------------
# AgentDef.from_directory
# ---------------------------------------------------------------------------


def test_agent_def_from_directory_minimal(tmp_path):
    agent_dir = tmp_path / "researcher"
    agent_dir.mkdir()
    agent = AgentDef.from_directory(agent_dir)
    assert agent.name == "researcher"
    assert agent.system_prompt == ""
    assert agent.tool_plugin_paths == []


def test_agent_def_from_directory_reads_system_prompt(tmp_path):
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "system_prompt.md").write_text("You are a writer.")
    agent = AgentDef.from_directory(agent_dir)
    assert agent.system_prompt == "You are a writer."


def test_agent_def_from_directory_strips_system_prompt(tmp_path):
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "system_prompt.md").write_text("  You are a writer.  \n")
    agent = AgentDef.from_directory(agent_dir)
    assert agent.system_prompt == "You are a writer."


def test_agent_def_from_directory_project_wins_over_user(tmp_path):
    user_dir = tmp_path / "user" / "researcher"
    proj_dir = tmp_path / "proj" / "researcher"
    user_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)
    (user_dir / "system_prompt.md").write_text("User prompt.")
    (proj_dir / "system_prompt.md").write_text("Project prompt.")
    agent = AgentDef.from_directory(proj_dir, user_agent_dir=user_dir)
    assert agent.system_prompt == "Project prompt."


def test_agent_def_from_directory_falls_back_to_user_prompt(tmp_path):
    user_dir = tmp_path / "user" / "researcher"
    proj_dir = tmp_path / "proj" / "researcher"
    user_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)
    (user_dir / "system_prompt.md").write_text("User prompt.")
    # no system_prompt.md in proj_dir
    agent = AgentDef.from_directory(proj_dir, user_agent_dir=user_dir)
    assert agent.system_prompt == "User prompt."


def test_agent_def_from_directory_empty_project_prompt_falls_back(tmp_path):
    user_dir = tmp_path / "user" / "researcher"
    proj_dir = tmp_path / "proj" / "researcher"
    user_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)
    (user_dir / "system_prompt.md").write_text("User prompt.")
    (proj_dir / "system_prompt.md").write_text("   ")  # whitespace-only
    agent = AgentDef.from_directory(proj_dir, user_agent_dir=user_dir)
    # whitespace-only project prompt — falls back to user
    assert agent.system_prompt == "User prompt."


def test_agent_def_from_directory_invalid_name_raises(tmp_path):
    agent_dir = tmp_path / "Bad Name"
    agent_dir.mkdir()
    with pytest.raises(ValueError):
        AgentDef.from_directory(agent_dir)


# ---------------------------------------------------------------------------
# discover_agents
# ---------------------------------------------------------------------------


def test_discover_agents_finds_directories(tmp_path):
    (tmp_path / "agents" / "researcher" / "tools").mkdir(parents=True)
    (tmp_path / "agents" / "writer" / "tools").mkdir(parents=True)
    agents = discover_agents(project_dir=tmp_path)
    assert "researcher" in agents
    assert "writer" in agents


def test_discover_agents_reads_system_prompt(tmp_path):
    agent_dir = tmp_path / "agents" / "researcher"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system_prompt.md").write_text("You are a deep researcher.")
    agents = discover_agents(project_dir=tmp_path)
    assert agents["researcher"].system_prompt == "You are a deep researcher."


def test_discover_agents_skips_invalid_names(tmp_path):
    (tmp_path / "agents" / "Bad Agent").mkdir(parents=True)
    agents = discover_agents(project_dir=tmp_path)
    assert "Bad Agent" not in agents


def test_discover_agents_skips_dot_directories(tmp_path):
    (tmp_path / "agents" / ".hidden").mkdir(parents=True)
    (tmp_path / "agents" / "visible").mkdir(parents=True)
    agents = discover_agents(project_dir=tmp_path)
    assert ".hidden" not in agents
    assert "visible" in agents


def test_discover_agents_project_overrides_user(tmp_path):
    user_dir = tmp_path / "user"
    proj_dir = tmp_path / "proj"
    (user_dir / "agents" / "bot").mkdir(parents=True)
    (proj_dir / "agents" / "bot").mkdir(parents=True)
    (user_dir / "agents" / "bot" / "system_prompt.md").write_text("User prompt.")
    (proj_dir / "agents" / "bot" / "system_prompt.md").write_text("Project prompt.")
    agents = discover_agents(project_dir=proj_dir, user_dir=user_dir)
    assert agents["bot"].system_prompt == "Project prompt."


def test_discover_agents_merges_user_and_project(tmp_path):
    user_dir = tmp_path / "user"
    proj_dir = tmp_path / "proj"
    (user_dir / "agents" / "user-only").mkdir(parents=True)
    (proj_dir / "agents" / "proj-only").mkdir(parents=True)
    agents = discover_agents(project_dir=proj_dir, user_dir=user_dir)
    assert "user-only" in agents
    assert "proj-only" in agents


def test_discover_agents_empty_when_no_dirs(tmp_path):
    agents = discover_agents(project_dir=tmp_path, user_dir=tmp_path)
    assert agents == {}


def test_discover_agents_skips_files_not_dirs(tmp_path):
    (tmp_path / "agents").mkdir(parents=True)
    (tmp_path / "agents" / "notadir.txt").write_text("file")
    agents = discover_agents(project_dir=tmp_path)
    assert agents == {}


def test_discover_agents_returns_agent_def_instances(tmp_path):
    (tmp_path / "agents" / "analyst").mkdir(parents=True)
    agents = discover_agents(project_dir=tmp_path)
    assert isinstance(agents["analyst"], AgentDef)
    assert agents["analyst"].name == "analyst"


# ---------------------------------------------------------------------------
# load_agent_system_prompt
# ---------------------------------------------------------------------------


def test_load_agent_system_prompt_returns_base_when_no_file(tmp_path):
    result = load_agent_system_prompt(
        "researcher", "Base prompt.", project_dir=tmp_path, user_dir=tmp_path
    )
    assert result == "Base prompt."


def test_load_agent_system_prompt_project_wins(tmp_path):
    proj = tmp_path / "proj"
    user = tmp_path / "user"
    (proj / "agents" / "researcher").mkdir(parents=True)
    (user / "agents" / "researcher").mkdir(parents=True)
    (proj / "agents" / "researcher" / "system_prompt.md").write_text("Project sys.")
    (user / "agents" / "researcher" / "system_prompt.md").write_text("User sys.")
    result = load_agent_system_prompt(
        "researcher", "Base prompt.", project_dir=proj, user_dir=user
    )
    assert result == "Project sys."


def test_load_agent_system_prompt_falls_back_to_user(tmp_path):
    proj = tmp_path / "proj"
    user = tmp_path / "user"
    (proj / "agents" / "researcher").mkdir(parents=True)
    (user / "agents" / "researcher").mkdir(parents=True)
    (user / "agents" / "researcher" / "system_prompt.md").write_text("User sys.")
    result = load_agent_system_prompt(
        "researcher", "Base prompt.", project_dir=proj, user_dir=user
    )
    assert result == "User sys."


def test_load_agent_system_prompt_ignores_empty_file(tmp_path):
    proj = tmp_path / "proj"
    user = tmp_path / "user"
    (proj / "agents" / "researcher").mkdir(parents=True)
    (user / "agents" / "researcher").mkdir(parents=True)
    (proj / "agents" / "researcher" / "system_prompt.md").write_text("   \n")
    (user / "agents" / "researcher" / "system_prompt.md").write_text("User sys.")
    result = load_agent_system_prompt(
        "researcher", "Base prompt.", project_dir=proj, user_dir=user
    )
    # project file is whitespace-only; should move on to user
    assert result == "User sys."


def test_load_agent_system_prompt_strips_whitespace(tmp_path):
    proj = tmp_path / "proj"
    (proj / "agents" / "writer").mkdir(parents=True)
    (proj / "agents" / "writer" / "system_prompt.md").write_text("  You write.\n\n")
    result = load_agent_system_prompt(
        "writer", "Base prompt.", project_dir=proj, user_dir=tmp_path
    )
    assert result == "You write."
