"""End-to-end coverage for discovered skill registration, picking, and dispatch."""

from __future__ import annotations

from pathlib import Path
import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, CommandDispatcher, UnifiedCommandRegistry
from agenthicc.config import AgenthiccConfig
from agenthicc.runners.tui_session import _build_skill_command
from agenthicc.skills.loader import discover_skills_with_diagnostics
from agenthicc.tui.trigger import TriggerContext, TriggerManager
from agenthicc.tui.triggers.slash_command import SkillTrigger, SlashCommandTrigger
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime.commands import CommandBus

pytestmark = pytest.mark.e2e


def test_discovered_skill_flows_through_dollar_picker_and_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    skills_root = tmp_path / ".agenthicc" / "skills" / "review-code"
    skills_root.mkdir(parents=True)
    (skills_root / "SKILL.md").write_text(
        "---\n"
        "name: Review Code\n"
        "description: Review implementation changes\n"
        "aliases: [review]\n"
        "---\n"
        "Review {args}\n",
        encoding="utf-8",
    )

    discovery = discover_skills_with_diagnostics(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "missing-user-skills",
    )
    skill = discovery.skills["review-code"]
    registry = UnifiedCommandRegistry()
    registry.register(_build_skill_command("review-code", skill))

    triggers = TriggerManager()
    triggers.register(SlashCommandTrigger(registry))
    triggers.register(SkillTrigger(registry))
    picker_context = TriggerContext(cwd=tmp_path, command_registry=registry)

    assert triggers.get("/") is not None
    assert triggers.get("$") is not None
    assert [item.value for item in triggers.get("$").get_matches("review", picker_context)] == [
        "$review-code",
        "$review",
    ]
    assert triggers.get("/").get_matches("", picker_context) == []

    input_session = UnifiedInputSession(
        AppState.create(), CommandBus(), trigger_registry=triggers, cwd=tmp_path
    )
    input_session._buf.set(list("$review"))
    tail = input_session._find_trigger_tail()
    assert tail is not None
    assert tail[0] == "$"
    assert tail[2] == "review"

    pending: list[str] = []
    context = CommandContext(
        text="$review src/app.py",
        args="",
        model="test/model",
        console=Console(record=True),
        config=AgenthiccConfig(),
        active_agent="default",
        command_registry=registry,
        skills=discovery.skills,
        set_pending_skill=pending.append,
    )
    dispatcher = CommandDispatcher(registry)

    assert dispatcher.dispatch("$review src/app.py", context)
    assert len(pending) == 1
    assert "[Skill $review-code" in pending[0]
    assert "Review src/app.py" in pending[0]
    assert not dispatcher.dispatch("/review src/app.py", context)
    assert len(pending) == 1
