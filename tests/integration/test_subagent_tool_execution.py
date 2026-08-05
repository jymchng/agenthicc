"""Mock-provider integration tests for real subagent tool execution."""

from __future__ import annotations

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec

pytestmark = pytest.mark.integration


def _registry(tool_name: str) -> SubagentTypeRegistry:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="researcher",
            allowed_tools=frozenset({tool_name}),
            max_turns=3,
            system_prompt="Use the research tool and report its result.",
            max_turn_time_s=10.0,
        )
    )
    return registry


def _final(text: str, identifier: str) -> Completion:
    return Completion(
        id=identifier,
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=3, output_tokens=5),
    )


def _tool_names(call: object) -> list[str]:
    tools = getattr(call, "tools", None) or []
    return [
        str(schema.get("name", "") if isinstance(schema, dict) else getattr(schema, "name", ""))
        for schema in tools
    ]


def _message_content(message: object) -> object:
    return message.get("content") if isinstance(message, dict) else getattr(message, "content", "")


async def test_real_subagent_executes_mock_requested_tool_and_continues() -> None:
    """A provider tool call reaches the worker's actual callable executor."""
    invoked: list[str] = []

    @tool()
    async def lookup_fact(topic: str) -> str:
        """Look up a deterministic fact for a topic."""
        invoked.append(topic)
        return f"fact:{topic}"

    transport = MockTransport()
    transport.queue_tool_use("lookup_fact", {"topic": "chapter-7"}, tool_use_id="lookup-7")
    transport.queue_response(_final("The fact was incorporated.", "final-7"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [lookup_fact],
        registry=_registry("lookup_fact"),
    )

    result = await spawn(
        tasks=[{"type": "researcher", "task": "Find the chapter-7 fact."}],
        timeout_s=30,
    )

    assert result["ok"] is True
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert "The fact was incorporated." in result["results"]
    assert invoked == ["chapter-7"]
    assert len(transport.calls) == 2
    assert _tool_names(transport.calls[0]) == ["lookup_fact"]
    assert any(
        "fact:chapter-7" in str(_message_content(message))
        for message in transport.calls[1].messages
    )


async def test_real_pool_runs_two_tool_workers_and_filters_unallowed_tools() -> None:
    """Each real worker can execute tools while the pool preserves both results."""
    invoked: list[str] = []

    @tool()
    async def lookup_fact(topic: str) -> str:
        """Look up a deterministic fact for a topic."""
        invoked.append(topic)
        return f"fact:{topic}"

    @tool()
    async def write_forbidden(topic: str) -> str:
        """A tool that must not be exposed to this researcher type."""
        raise AssertionError(f"unallowed tool was executed: {topic}")

    transport = MockTransport()
    transport.queue_tool_use("lookup_fact", {"topic": "alpha"}, tool_use_id="lookup-alpha")
    transport.queue_tool_use("lookup_fact", {"topic": "beta"}, tool_use_id="lookup-beta")
    transport.queue_response(_final("alpha complete", "final-alpha"))
    transport.queue_response(_final("beta complete", "final-beta"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [lookup_fact, write_forbidden],
        registry=_registry("lookup_fact"),
    )

    result = await spawn(
        tasks=[
            {"type": "researcher", "task": "Research alpha."},
            {"type": "researcher", "task": "Research beta."},
        ],
        max_concurrent=2,
        timeout_s=30,
    )

    assert result["ok"] is True
    assert result["total"] == 2
    assert result["succeeded"] == 2
    assert result["failed"] == 0
    assert "alpha complete" in result["results"]
    assert "beta complete" in result["results"]
    assert sorted(invoked) == ["alpha", "beta"]
    assert len(transport.calls) == 4
    assert all(_tool_names(call) == ["lookup_fact"] for call in transport.calls)
