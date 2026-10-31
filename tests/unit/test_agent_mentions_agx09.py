from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agenthicc.agents.definitions import AgentSpec
from agenthicc.agents.registry import AgentsRegistry
from agenthicc.mentions.injector import InjectionConfig, build_context_prefix
from agenthicc.mentions.parser import MentionKind, agent_mentions, parse_mentions
from agenthicc.runners.agent_turn import AgentTurnRunner
from agenthicc.tui.trigger import TriggerContext
from agenthicc.tui.triggers.at_mention import AtMentionTrigger

pytestmark = pytest.mark.unit


def test_agent_mentions_are_not_files_and_respect_markdown_and_boundaries(tmp_path) -> None:
    text = "Ask @agent:reviewer, then `@agent:literal` and contact@example.test."

    parsed = parse_mentions(text, cwd=tmp_path)
    selected = agent_mentions(text)

    assert [(mention.kind, mention.path) for mention in parsed] == [(MentionKind.AGENT, "reviewer")]
    assert [(mention.path, text[mention.start : mention.end]) for mention in selected] == [
        ("reviewer", "@agent:reviewer")
    ]
    assert selected[0].resolved is None


def test_invalid_agent_name_is_not_an_agent_mention() -> None:
    assert agent_mentions("@agent:Upper @agent:has.dot @agent:-bad") == []


@pytest.mark.asyncio
async def test_context_injection_skips_agent_invocations(tmp_path) -> None:
    prefix, injected = await build_context_prefix(
        "Ask @agent:reviewer to inspect this.",
        cwd=tmp_path,
        cfg=InjectionConfig(cwd=tmp_path),
    )

    assert prefix == ""
    assert injected == []


def test_at_picker_suggests_visible_subagents_and_inserts_exact_name() -> None:
    registry = AgentsRegistry()
    registry.register(AgentSpec(name="reviewer", description="Reviews changes", mode="subagent"))
    registry.register(
        AgentSpec(name="hidden", description="Internal agent", mode="subagent", hidden=True)
    )
    registry.register(AgentSpec(name="primary", description="Primary only", mode="primary"))
    trigger = AtMentionTrigger(agents=registry.subagents)

    matches = trigger.get_matches("agent:rev", TriggerContext(cwd=Path.cwd()))

    assert [item.value for item in matches] == ["agent:reviewer"]
    assert trigger.on_select(matches[0], "agent:rev", []).buffer == list("@agent:reviewer")
    assert trigger.get_lines(matches[0], 70)[0].startswith("@agent:reviewer")
    assert (
        trigger.get_matches("agent:", TriggerContext(cwd=Path.cwd()))[0].value == "agent:reviewer"
    )


@pytest.mark.asyncio
async def test_agent_mentions_call_spawn_tool_and_add_results_to_parent_context() -> None:
    registry = AgentsRegistry()
    registry.register(AgentSpec(name="reviewer", description="Reviews changes", mode="subagent"))
    message = "Review the change @agent:reviewer"
    runner = object.__new__(AgentTurnRunner)
    runner._ctx = SimpleNamespace(agents_registry=registry, conv_store=None, text=message)
    runner._agent_mentions = agent_mentions(message)
    runner._spawn_tool = AsyncMock(return_value={"ok": True, "results": "No issues found."})
    runner._definition_agent_errors = {}

    result = await runner._run_agent_mentions(message)

    call = runner._spawn_tool.await_args
    assert call is not None
    assert call.kwargs["tasks"] == [
        {
            "type": "reviewer",
            "task": "Review the change",
            "context": "Explicitly invoked by the user with @agent:NAME.",
        }
    ]
    assert "No issues found." in result
    assert message in result
