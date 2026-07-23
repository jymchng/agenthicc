"""Unit coverage for live TUI skill discovery refresh."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenthicc.commands import Command, UnifiedCommandRegistry
from agenthicc.config import AgenthiccConfig
from agenthicc.runners.tui_session import TUISession
from agenthicc.skills.loader import SkillDiagnostic, SkillDef, SkillDiscoveryResult

pytestmark = pytest.mark.unit


def _session_context(tmp_path, skills, registry):
    return SimpleNamespace(
        cfg=AgenthiccConfig(),
        skills=skills,
        cmd_registry=registry,
    )


def test_reload_replaces_only_skill_commands(tmp_path, monkeypatch):
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
    context = _session_context(tmp_path, {"old": old}, registry)
    session = object.__new__(TUISession)
    session._ctx = context

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

    returned = session._reload_skills()

    assert returned is discovery
    assert context.skills == {"new": new}
    assert registry.get("$old") is None
    assert registry.get("$legacy") is None
    assert registry.get("/keep") is not None
    assert registry.get("$new") is not None
    assert registry.get("$fresh") is registry.get("$new")


def test_reload_keeps_command_and_skill_namespaces_independent(tmp_path, monkeypatch):
    from agenthicc.skills import loader

    skill = SkillDef(name="Commands", slug="commands", path=tmp_path / "commands")
    registry = UnifiedCommandRegistry()
    builtin = Command("/commands", "Built-in commands", source_id="builtin")
    registry.register(builtin)
    context = _session_context(tmp_path, {}, registry)
    session = object.__new__(TUISession)
    session._ctx = context
    discovery = SkillDiscoveryResult(skills={"commands": skill})
    monkeypatch.setattr(loader, "discover_skills_with_diagnostics", lambda **_: discovery)

    returned = session._reload_skills()

    assert registry.get("/commands") is builtin
    assert registry.get("$commands") is not None
    assert not any(item.code == "command-conflict" for item in returned.diagnostics)


def test_reload_failure_leaves_existing_session_unchanged(tmp_path, monkeypatch):
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
    context = _session_context(tmp_path, {"old": old}, registry)
    session = object.__new__(TUISession)
    session._ctx = context

    def fail(**_kwargs):
        raise OSError("skill directory unavailable")

    monkeypatch.setattr(loader, "discover_skills_with_diagnostics", fail)

    with pytest.raises(OSError, match="skill directory unavailable"):
        session._reload_skills()

    assert context.skills == {"old": old}
    assert registry.get("$old") is old_command
