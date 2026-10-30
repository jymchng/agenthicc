"""Generation of Agent Library drafts uses the active provider runner."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from agenthicc.agents.generation import generate_definition_draft

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Chunk:
    delta: str
    stop_reason: str | None = None


class _Runner:
    def __init__(self, chunks: tuple[_Chunk, ...]) -> None:
        self.chunks = chunks
        self.prompt = ""
        self.conversation_id = ""

    async def run_stream(
        self, agent: object, prompt: str, *, conversation_id: str
    ) -> AsyncIterator[_Chunk]:
        del agent
        self.prompt = prompt
        self.conversation_id = conversation_id

        async def stream() -> AsyncIterator[_Chunk]:
            for chunk in self.chunks:
                yield chunk

        return stream()


def _patch_dynamic_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lauren_ai._agents

    def decorator(**kwargs: object) -> Callable[[type[object]], type[object]]:
        del kwargs
        return lambda agent: agent

    def use_tools(*tools: object) -> Callable[[type[object]], type[object]]:
        del tools
        return lambda agent: agent

    monkeypatch.setattr(lauren_ai._agents, "agent", decorator)
    monkeypatch.setattr(lauren_ai._agents, "use_tools", use_tools)


async def test_generation_collects_stream_and_uses_requested_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dynamic_agent(monkeypatch)
    runner = _Runner((_Chunk("---\nname: reviewer\n"), _Chunk("description: Review\n---\nBody")))

    result = await generate_definition_draft(
        cast("AgentRunnerBase", runner),
        model_id="test-model",
        name="reviewer",
        description="Review code",
    )

    assert result == "---\nname: reviewer\ndescription: Review\n---\nBody"
    assert "reviewer" in runner.prompt
    assert "Review code" in runner.prompt
    assert runner.conversation_id.startswith("agent-definition-")


async def test_generation_unwraps_a_complete_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dynamic_agent(monkeypatch)
    runner = _Runner((_Chunk("```markdown\n---\nname: reviewer\n---\nBody\n```"),))

    result = await generate_definition_draft(
        cast("AgentRunnerBase", runner),
        model_id="test-model",
        name="reviewer",
        description="Review code",
    )

    assert result == "---\nname: reviewer\n---\nBody"


async def test_generation_rejects_empty_and_truncated_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dynamic_agent(monkeypatch)
    empty = _Runner((_Chunk("  "),))
    truncated = _Runner((_Chunk("draft", stop_reason="max_tokens"),))

    with pytest.raises(ValueError, match="empty"):
        await generate_definition_draft(
            cast("AgentRunnerBase", empty),
            model_id="test-model",
            name="reviewer",
            description="Review",
        )
    with pytest.raises(ValueError, match="truncated"):
        await generate_definition_draft(
            cast("AgentRunnerBase", truncated),
            model_id="test-model",
            name="reviewer",
            description="Review",
        )
