"""Console Go-like gateway regression journey for PRD-190."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from lauren_ai._config import LLMConfig
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import Message
from lauren_ai._transport._openai import OpenAITransport

pytestmark = pytest.mark.integration


class _GatewayCompletions:
    """Fake Chat Completions gateway requiring reasoning replay."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(
                id="first",
                model=kwargs["model"],
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        message=SimpleNamespace(
                            content="",
                            reasoning_content="REASONING_FIXTURE",
                            tool_calls=[
                                SimpleNamespace(
                                    id="call-1",
                                    function=SimpleNamespace(
                                        name="goal_implemented",
                                        arguments='{"summary":"done"}',
                                    ),
                                )
                            ],
                        ),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

        assistant = next(
            message for message in kwargs["messages"] if message.get("role") == "assistant"
        )
        if assistant.get("reasoning_content") != "REASONING_FIXTURE":
            raise ValueError("gateway rejected request: missing reasoning_content")
        if assistant["tool_calls"][0]["id"] != "call-1":
            raise ValueError("gateway rejected request: tool call identity changed")
        tool_result = next(
            message for message in kwargs["messages"] if message.get("role") == "tool"
        )
        if tool_result.get("tool_call_id") != "call-1":
            raise ValueError("gateway rejected request: tool result identity changed")
        return SimpleNamespace(
            id="second",
            model=kwargs["model"],
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="verified", tool_calls=[]),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


@pytest.mark.asyncio
async def test_tool_result_followup_replays_reasoning_content() -> None:
    gateway = _GatewayCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=gateway))
    config, _ = LLMConfig.for_testing()
    transport = OpenAITransport(config, client=client)
    memory = ShortTermMemory(max_tokens=100_000)

    first = await transport.complete(
        [Message.user("implement it")],
        model="gateway-model",
        stream=False,
    )
    memory.add_assistant(first)
    memory.add_tool_result(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "recorded"}],
        }
    )

    second = cast(
        Any, await transport.complete(memory.messages(), model="gateway-model", stream=False)
    )

    assert second.content == "verified"
    assert len(gateway.calls) == 2
    assistant = next(
        message for message in gateway.calls[1]["messages"] if message["role"] == "assistant"
    )
    assert assistant["reasoning_content"] == "REASONING_FIXTURE"
