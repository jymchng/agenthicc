"""Integration coverage for validated skill discovery and slash activation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agenthicc.commands import Command, CommandContext, CommandDispatcher, UnifiedCommandRegistry
from agenthicc.commands.builtins import _make_skill_handler
from agenthicc.config import load_config
from agenthicc.runners.reload import ReloadService
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.workflows.registry import WorkflowRegistry
from agenthicc.skills.loader import discover_skills_with_diagnostics

if TYPE_CHECKING:
    from agenthicc.kernel.processor import EventProcessor

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
            "$review-skill",
            skill.description,
            aliases=("$inspect",),
            group="Skills",
            handler=_make_skill_handler(skill.slug, skill),
        )
    )

    context = CommandContext(
        text="$inspect src/app.py",
        args="",
        model="",
        console=console,
        config=config,
        active_agent="planner",
        command_registry=registry,
        set_pending_skill=pending,
    )

    assert CommandDispatcher(registry).dispatch("$inspect src/app.py", context)
    assert not CommandDispatcher(registry).dispatch("/inspect src/app.py", context)
    assert pending.call_count == 1
    assert "Review src/app.py" in pending.call_args.args[0]
    assert not any(diagnostic.severity == "error" for diagnostic in discovery.diagnostics)


async def test_live_reload_discovers_new_skill_and_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_processor: EventProcessor,
) -> None:
    monkeypatch.chdir(tmp_path)
    project_root = tmp_path / ".agenthicc"
    initial_dir = project_root / "skills" / "initial"
    initial_dir.mkdir(parents=True)
    (initial_dir / "SKILL.md").write_text("---\nname: Initial\n---\nInitial\n")

    user_root = tmp_path / "user-agenthicc"
    config_file = project_root / "agenthicc.toml"
    config_file.write_text(f'[skills]\ndefault_skill_directory = "{user_root.as_posix()}"\n')
    config = load_config(project_path=config_file, env_overrides=False)
    initial = discover_skills_with_diagnostics(project_dir=project_root, user_dir=user_root)
    registry = UnifiedCommandRegistry()
    registry.register(
        Command(
            "$initial",
            "Initial",
            group="Skills",
            source_id="skill:initial",
        )
    )
    service = ReloadService(
        processor=running_processor,
        project_dir=project_root,
        user_dir=tmp_path / "user",
        skill_user_dir=Path(config.skills.default_skill_directory),
        workflow_registry=WorkflowRegistry(),
        command_registry=registry,
        subagent_registry=DEFAULT_REGISTRY.copy(),
        skills=initial.skills,
    )

    added_dir = project_root / "skills" / "new_skill"
    added_dir.mkdir()
    (added_dir / "SKILL.md").write_text("---\nname: New Skill\naliases: [fresh]\n---\nNew\n")

    report = await service.reload(frozenset({"skills"}), reason="command")

    assert report.ok
    assert "new-skill" in service.skills.current().value
    assert registry.get("$new-skill") is not None
    assert registry.get("$fresh") is registry.get("$new-skill")
