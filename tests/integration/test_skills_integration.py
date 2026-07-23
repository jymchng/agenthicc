"""Integration coverage for validated skill discovery and slash activation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agenthicc.commands import Command, CommandContext, CommandDispatcher, UnifiedCommandRegistry
from agenthicc.commands.builtins import _make_skill_handler
from agenthicc.config import load_config
from agenthicc.skills.loader import discover_skills_with_diagnostics

pytestmark = pytest.mark.integration


def test_discovered_skill_alias_and_agent_policy_reach_command_dispatch(tmp_path):
    project_root = tmp_path / ".agenthicc"
    skill_dir = project_root / "skills" / "Review_Skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: Review\n"
        "description: Review code\n"
        "aliases: [inspect]\n"
        "allowedAgents: [planner]\n"
        "---\nReview {args}\n"
    )
    config_file = project_root / "agenthicc.toml"
    config_file.write_text('[agents.planner]\nallowed_skills = ["review-skill"]\n')

    discovery = discover_skills_with_diagnostics(
        project_dir=project_root,
        user_dir=tmp_path / "missing-user",
    )
    config = load_config(project_path=config_file, env_overrides=False)
    skill = discovery.skills["review-skill"]
    registry = UnifiedCommandRegistry()
    pending = MagicMock()
    console = MagicMock()
    registry.register(
        Command(
            "/review-skill",
            skill.description,
            aliases=("/inspect",),
            group="Skills",
            handler=_make_skill_handler(skill.slug, skill),
        )
    )

    context = CommandContext(
        text="/inspect src/app.py",
        args="",
        model="",
        console=console,
        config=config,
        active_agent="planner",
        command_registry=registry,
        set_pending_skill=pending,
    )

    assert CommandDispatcher(registry).dispatch("/inspect src/app.py", context)
    assert pending.call_count == 1
    assert "Review src/app.py" in pending.call_args.args[0]
    assert not any(diagnostic.severity == "error" for diagnostic in discovery.diagnostics)
