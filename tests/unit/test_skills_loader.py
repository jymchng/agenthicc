"""Unit tests for SkillDef discovery and lazy loading (PRD-22)."""

from __future__ import annotations

import pytest

from agenthicc.skills.loader import discover_skills, _parse_skill

pytestmark = pytest.mark.unit


def test_parse_skill_with_frontmatter(tmp_path):
    d = tmp_path / "my-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: My Skill\n"
        "description: Does things\n"
        "tags:\n  - deploy\n  - production\n"
        "suggestedTopics:\n  - ship\n"
        "maxTurnDepth: 50\n"
        "---\n"
        "# Instructions\n"
    )
    skill = _parse_skill(d)
    assert skill is not None
    assert skill.name == "My Skill"
    assert skill.slug == "my-skill"
    assert skill.description == "Does things"
    assert skill.tags == ["deploy", "production"]
    assert skill.suggested_topics == ["ship"]
    assert skill.max_turn_depth == 50


def test_parse_skill_without_frontmatter(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    (d / "SKILL.md").write_text("# Just instructions\n")
    skill = _parse_skill(d)
    assert skill is not None
    assert skill.slug == "bare"
    assert skill.name == "bare"  # falls back to dir name


def test_parse_skill_missing_skill_md(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert _parse_skill(d) is None


def test_lazy_body_not_loaded_at_parse(tmp_path):
    d = tmp_path / "lazy"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Lazy\n---\n# Do things\n")
    skill = _parse_skill(d)
    assert skill is not None
    assert skill._body is None  # not loaded yet


def test_lazy_body_loaded_on_access(tmp_path):
    d = tmp_path / "lazy"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Lazy\n---\n# Do things\n")
    skill = _parse_skill(d)
    assert skill is not None
    body = skill.body
    assert "Do things" in body
    assert skill._body is not None  # now cached


def test_discover_project_overrides_personal(tmp_path):
    personal = tmp_path / "personal" / "skills" / "deploy"
    project = tmp_path / "project" / "skills" / "deploy"
    personal.mkdir(parents=True)
    project.mkdir(parents=True)
    (personal / "SKILL.md").write_text("---\nname: Personal Deploy\n---\n")
    (project / "SKILL.md").write_text("---\nname: Project Deploy\n---\n")

    skills = discover_skills(
        project_dir=tmp_path / "project",
        user_dir=tmp_path / "personal",
    )
    assert "deploy" in skills
    assert skills["deploy"].name == "Project Deploy"


def test_discover_returns_empty_when_no_dirs(tmp_path):
    skills = discover_skills(
        project_dir=tmp_path / "nonexistent-project",
        user_dir=tmp_path / "nonexistent-user",
    )
    assert skills == {}


def test_discover_skips_hidden_dirs(tmp_path):
    skills_root = tmp_path / "project" / "skills"
    hidden = skills_root / ".hidden-skill"
    visible = skills_root / "visible-skill"
    hidden.mkdir(parents=True)
    visible.mkdir(parents=True)
    (hidden / "SKILL.md").write_text("---\nname: Hidden\n---\n")
    (visible / "SKILL.md").write_text("---\nname: Visible\n---\n")

    skills = discover_skills(
        project_dir=tmp_path / "project",
        user_dir=tmp_path / "nonexistent-user",
    )
    assert "visible-skill" in skills
    assert ".hidden-skill" not in skills
