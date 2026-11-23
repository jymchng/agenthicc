"""Unit coverage for live TUI skill discovery refresh."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agenthicc.commands import Command, UnifiedCommandRegistry
from agenthicc.runners.reload import ReloadService
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.workflows.registry import WorkflowRegistry
from agenthicc.skills.loader import SkillDiagnostic, SkillDef, SkillDiscoveryResult

if TYPE_CHECKING:
    from agenthicc.kernel.processor import EventProcessor

pytestmark = pytest.mark.unit


def _skills_service(
    tmp_path: Path,
    skills: dict[str, SkillDef],
    registry: UnifiedCommandRegistry,
    processor: EventProcessor,
) -> ReloadService:
    return ReloadService(
        processor=processor,
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "user",
        workflow_registry=WorkflowRegistry(),
        command_registry=registry,
        subagent_registry=DEFAULT_REGISTRY.copy(),
        skills=skills,
    )


async def test_reload_replaces_only_skill_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running_processor: EventProcessor
) -> None:
    from agenthicc.skills import loader

    old = SkillDef(name="Old", slug="old", path=tmp_path / "old", aliases=("legacy",))
    new = SkillDef(name="New", slug="new", path=tmp_path / "new", aliases=("fresh",))
    registry = UnifiedCommandRegistry()
    registry.register(
        Command(
            "$old",
            "Old",
            aliases=("$legacy",),
            group="Skills",
            source_id="skill:old",
        )
    )
    registry.register(Command("/keep", "Keep", source_id="builtin"))
    service = _skills_service(tmp_path, {"old": old}, registry, running_processor)

    discovery = SkillDiscoveryResult(
        skills={"new": new},
        diagnostics=(
            SkillDiagnostic(
                path=tmp_path / "broken" / "SKILL.md",
                code="missing-file",
                message="SKILL.md is missing",
            ),
        ),
    )
    monkeypatch.setattr(loader, "discover_skills_with_diagnostics", lambda **_: discovery)

    report = await service.reload(frozenset({"skills"}), reason="command")

    assert report.ok
    assert report.diagnostics[0].code == "invalid_manifest"
    assert service.skills.current().value == {"new": new}
    assert registry.get("$old") is None
    assert registry.get("$legacy") is None
    assert registry.get("/keep") is not None
    assert registry.get("$new") is not None
    assert registry.get("$fresh") is registry.get("$new")


async def test_reload_keeps_command_and_skill_namespaces_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running_processor: EventProcessor
) -> None:
    from agenthicc.skills import loader

    skill = SkillDef(name="Commands", slug="commands", path=tmp_path / "commands")
    registry = UnifiedCommandRegistry()
    builtin = Command("/commands", "Built-in commands", source_id="builtin")
    registry.register(builtin)
    service = _skills_service(tmp_path, {}, registry, running_processor)
    discovery = SkillDiscoveryResult(skills={"commands": skill})
    monkeypatch.setattr(loader, "discover_skills_with_diagnostics", lambda **_: discovery)

    report = await service.reload(frozenset({"skills"}), reason="command")

    assert registry.get("/commands") is builtin
    assert registry.get("$commands") is not None
    assert report.diagnostics == ()


async def test_reload_failure_leaves_existing_session_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running_processor: EventProcessor
) -> None:
    from agenthicc.skills import loader

    old = SkillDef(name="Old", slug="old", path=tmp_path / "old")
    registry = UnifiedCommandRegistry()
    old_command = Command(
        "$old",
        "Old",
        group="Skills",
        source_id="skill:old",
    )
    registry.register(old_command)
    service = _skills_service(tmp_path, {"old": old}, registry, running_processor)

    def fail(**_kwargs: object) -> SkillDiscoveryResult:
        raise OSError("skill directory unavailable")

    monkeypatch.setattr(loader, "discover_skills_with_diagnostics", fail)

    report = await service.reload(frozenset({"skills"}), reason="command")
    assert not report.ok and report.generation == 0
    assert report.diagnostics[0].code == "import_error"
    assert service.skills.current().value == {"old": old}
    assert registry.get("$old") is old_command
