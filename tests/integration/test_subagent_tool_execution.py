"""Mock-provider integration tests for real subagent tool execution."""

from __future__ import annotations

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
from agenthicc.tools.capabilities import tool_write
from agenthicc.tools.fs.agent_tools import write_file

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


def _implementer_registry(*tool_names: str) -> SubagentTypeRegistry:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="implementer",
            allowed_tools=frozenset(tool_names),
            max_turns=3,
            system_prompt="Make the requested change with the available tools.",
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


async def test_real_implementer_exposes_the_filesystem_write_tool() -> None:
    """The production implementer path exposes the real write-tool schema."""
    transport = MockTransport()
    transport.queue_response(_final("Ready to write chapters/01.md.", "final-1"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [write_file],
        registry=_implementer_registry("write_file"),
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    assert len(transport.calls) == 1
    assert _tool_names(transport.calls[0]) == ["write_file"]


async def test_implementer_prose_without_a_tool_is_reported_as_failure() -> None:
    """An implementer that only claims a change is not a successful worker."""
    transport = MockTransport()
    transport.queue_response(_final("I would expand chapter 1 next.", "final-noop"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [write_file],
        registry=_implementer_registry("write_file"),
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["succeeded"] == 0
    assert result["failed"] == 1
    assert "no mutation was recorded" in str(result["results"])


async def test_empty_final_response_gets_a_tool_execution_summary() -> None:
    """Tool work remains visible even when a provider omits final prose."""
    invoked: list[str] = []

    @tool()
    async def lookup_fact(topic: str) -> str:
        """Look up a deterministic fact for a topic."""
        invoked.append(topic)
        return f"fact:{topic}"

    transport = MockTransport()
    transport.queue_tool_use("lookup_fact", {"topic": "chapter-9"}, tool_use_id="lookup-9")
    transport.queue_response(_final("", "final-empty"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [lookup_fact],
        registry=_registry("lookup_fact"),
    )

    result = await spawn(
        tasks=[{"type": "researcher", "task": "Find the chapter-9 fact."}],
        timeout_s=30,
    )

    assert result["ok"] is True
    assert invoked == ["chapter-9"]
    assert "Executed tool call(s): lookup_fact." in str(result["results"])


async def test_implementer_success_requires_a_successful_mutating_tool_call() -> None:
    """A successful implementer result records the mutation evidence."""
    changed: list[str] = []

    @tool_write
    @tool()
    async def record_change(path: str, content: str) -> dict[str, object]:
        """Record a deterministic file change for the integration test."""
        changed.append(f"{path}:{content}")
        return {"ok": True, "path": path}

    transport = MockTransport()
    transport.queue_tool_use(
        "record_change",
        {"path": "chapters/01.md", "content": "expanded"},
        tool_use_id="record-1",
    )
    transport.queue_response(_final("Changed chapters/01.md.", "final-change"))
    runner = AgentRunnerBase(transport=transport)
    registry = _implementer_registry("record_change")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [record_change],
        registry=registry,
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is True
    assert changed == ["chapters/01.md:expanded"]
    assert "Changed chapters/01.md." in str(result["results"])


async def test_implementer_rejects_a_mutating_tool_error_result() -> None:
    """A write tool returning ``ok=False`` is not mutation evidence."""

    @tool_write
    @tool()
    async def rejected_change(path: str, content: str) -> dict[str, object]:
        """Return a deterministic write failure for the integration test."""
        return {"ok": False, "error": "permission denied"}

    transport = MockTransport()
    transport.queue_tool_use(
        "rejected_change",
        {"path": "chapters/01.md", "content": "expanded"},
        tool_use_id="record-rejected",
    )
    transport.queue_response(_final("I could not write the chapter.", "final-rejected"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [rejected_change],
        registry=_implementer_registry("rejected_change"),
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    assert "no mutation was recorded" in str(result["results"])
