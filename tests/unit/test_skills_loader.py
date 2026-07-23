"""Unit tests for SkillDef discovery and lazy loading (PRD-22)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agenthicc.commands import Command, CommandContext, CommandDispatcher, UnifiedCommandRegistry
from agenthicc.config import load_config
from agenthicc.skills.loader import (
    SkillPermissionSet,
    _parse_skill,
    discover_skills,
    discover_skills_with_diagnostics,
    filter_skills_for_agent,
)

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


def test_frontmatter_validation_and_legacy_name_aliases(tmp_path):
    skill_dir = tmp_path / "skills" / "Review_Skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Review\n"
        "description: Review changes\n"
        "suggested_topics: review\n"
        "aliases: [/old-review]\n"
        "allowed_agents: [planner]\n"
        "deniedAgents: [executor]\n"
        "---\nInstructions\n"
    )

    result = discover_skills_with_diagnostics(
        project_dir=tmp_path,
        user_dir=tmp_path / "missing-user",
    )

    skill = result.skills["review-skill"]
    assert skill.name == "Review"
    assert skill.suggested_topics == ["review"]
    assert skill.aliases == ("Review_Skill", "old-review")
    assert skill.is_allowed_for("planner")
    assert not skill.is_allowed_for("executor")
    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "legacy-directory-name",
        "compatibility-field",
    }


def test_invalid_frontmatter_is_reported_and_excluded(tmp_path):
    skill_dir = tmp_path / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: [not a string]\ndescription: [not a string]\n---\n"
    )

    result = discover_skills_with_diagnostics(
        project_dir=tmp_path,
        user_dir=tmp_path / "missing-user",
    )

    assert result.skills == {}
    assert any(diagnostic.code == "invalid-name" for diagnostic in result.diagnostics)
    assert any(diagnostic.severity == "error" for diagnostic in result.diagnostics)


def test_directory_without_a_canonical_name_is_rejected(tmp_path):
    skill_dir = tmp_path / "skills" / "!!!"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: Broken\n---\n")

    result = discover_skills_with_diagnostics(
        project_dir=tmp_path,
        user_dir=tmp_path / "missing-user",
    )

    assert result.skills == {}
    assert any(diagnostic.code == "invalid-canonical-name" for diagnostic in result.diagnostics)


def test_discovery_reports_missing_files_scope_override_and_alias_conflicts(tmp_path):
    user_skills = tmp_path / "user" / "skills"
    project_skills = tmp_path / "project" / "skills"
    (user_skills / "deploy").mkdir(parents=True)
    (project_skills / "deploy").mkdir(parents=True)
    (project_skills / "release").mkdir(parents=True)
    (project_skills / "empty").mkdir(parents=True)
    (user_skills / "deploy" / "SKILL.md").write_text("---\nname: User\n---\n")
    (project_skills / "deploy" / "SKILL.md").write_text("---\nname: Project\n---\n")
    (project_skills / "release" / "SKILL.md").write_text(
        "---\nname: Release\naliases: [deploy]\n---\n"
    )

    result = discover_skills_with_diagnostics(
        project_dir=tmp_path / "project",
        user_dir=tmp_path / "user",
    )

    assert result.skills["deploy"].name == "Project"
    assert result.skills["release"].aliases == ()
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "scope-override" in codes
    assert "missing-skill-file" in codes
    assert "alias-conflict" in codes


def test_skill_permissions_filter_canonical_names_aliases_and_wildcards(tmp_path):
    allowed = _parse_skill(tmp_path / "missing")
    assert allowed is None

    from agenthicc.skills.loader import SkillDef

    review = SkillDef(
        name="Review",
        slug="review",
        path=tmp_path,
        aliases=("inspect",),
    )
    deploy = SkillDef(name="Deploy", slug="deploy", path=tmp_path)
    skills = {"review": review, "deploy": deploy}

    filtered = filter_skills_for_agent(
        skills,
        "planner",
        SkillPermissionSet(allowed_skills=frozenset({"inspect"})),
    )
    assert filtered == {"review": review}
    assert (
        filter_skills_for_agent(
            skills,
            "planner",
            SkillPermissionSet(denied_skills=frozenset({"*"})),
        )
        == {}
    )


def test_agent_skill_permissions_are_loaded_from_toml(tmp_path):
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text(
        "[agents.planner]\n"
        'allowed_skills = ["review"]\n'
        'denied_skills = ["deploy"]\n'
        "\n[skills]\ninstall_default_skills = false\n"
    )

    config = load_config(project_path=config_path, env_overrides=False)
    permissions = config.agents.skill_permissions_for("planner")

    assert config.skills.install_default_skills is False
    assert permissions.allowed_skills == frozenset({"review"})
    assert permissions.denied_skills == frozenset({"deploy"})


def test_default_skill_bootstrap_remains_loader_compatible(tmp_path):
    from agenthicc.skills.bootstrap import bootstrap_default_skills

    installed = bootstrap_default_skills(global_dir=tmp_path)
    result = discover_skills_with_diagnostics(
        project_dir=tmp_path / "missing-project",
        user_dir=tmp_path,
    )

    assert installed >= 9
    assert len(result.skills) >= installed
    assert not any(diagnostic.severity == "error" for diagnostic in result.diagnostics)


def test_skill_slash_alias_dispatches_but_permission_denial_is_enforced(tmp_path):
    from agenthicc.commands.builtins import _make_skill_handler
    from agenthicc.config import AgenthiccConfig
    from agenthicc.skills.loader import SkillDef

    skill_dir = tmp_path / "review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: Review\n---\nReview {args}\n")
    skill = SkillDef(
        name="Review",
        slug="review",
        path=skill_dir,
        aliases=("inspect",),
        allowed_agents=("planner",),
    )
    pending = MagicMock()
    console = MagicMock()
    command = Command(
        "/review",
        "Review",
        aliases=("/inspect",),
        handler=_make_skill_handler("review", skill),
        group="Skills",
    )
    registry = UnifiedCommandRegistry()
    registry.register(command)
    base = dict(
        text="/inspect file.py",
        args="",
        model="",
        console=console,
        config=AgenthiccConfig(),
        command_registry=registry,
        set_pending_skill=pending,
    )

    denied = CommandContext(**base, active_agent="executor")
    assert CommandDispatcher(registry).dispatch("/inspect file.py", denied)
    pending.assert_not_called()
    assert "not permitted" in str(console.print.call_args)

    allowed = CommandContext(**base, active_agent="planner")
    assert CommandDispatcher(registry).dispatch("/inspect file.py", allowed)
    pending.assert_called_once()
    assert "Review file.py" in pending.call_args.args[0]
