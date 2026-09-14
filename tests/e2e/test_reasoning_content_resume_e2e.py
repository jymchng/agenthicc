"""End-to-end reasoning-content replay after a session restart (PRD-190)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lauren_ai._config import LLMConfig
from lauren_ai._transport import Message
from lauren_ai._transport._openai import OpenAITransport

from agenthicc.runners.session_conversation import SessionConversation

pytestmark = pytest.mark.e2e


class _RestartGateway:
    """A Console Go-like gateway that requires exact history replay."""

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
            raise ValueError("gateway rejected resumed history without reasoning_content")
        if assistant["tool_calls"][0]["id"] != "call-1":
            raise ValueError("gateway rejected resumed history with a changed tool ID")
        tool_result = next(
            message for message in kwargs["messages"] if message.get("role") == "tool"
        )
        if tool_result.get("tool_call_id") != "call-1":
            raise ValueError("gateway rejected resumed history without its tool result")
        return SimpleNamespace(
            id="second",
            model=kwargs["model"],
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="resumed", tool_calls=[]),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


@pytest.mark.asyncio
async def test_resume_rehydrates_reasoning_before_followup_request(tmp_path: Path) -> None:
    gateway = _RestartGateway()
    client = SimpleNamespace(chat=SimpleNamespace(completions=gateway))
    config, _ = LLMConfig.for_testing()
    transport = OpenAITransport(config, client=client)
    journal_path = tmp_path / "conversation.jsonl"

    first_session = SessionConversation.open(
        "reasoning-resume-e2e",
        max_tokens=100_000,
        journal_path=journal_path,
    )
    try:
        first = await transport.complete(
            [Message.user("implement it")],
            model="gateway-model",
            stream=False,
        )
        first_session.memory.add_assistant(first)
        first_session.memory.add_tool_result(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "recorded",
                    }
                ],
            }
        )
    finally:
        first_session.close()

    resumed_session = SessionConversation.open(
        "reasoning-resume-e2e",
        max_tokens=100_000,
        journal_path=journal_path,
    )
    try:
        resumed = cast(
            Any,
            await transport.complete(
                cast(list[Message], resumed_session.messages),
                model="gateway-model",
                stream=False,
            ),
        )
        assert resumed.content == "resumed"
        assert len(gateway.calls) == 2
    finally:
        resumed_session.close()
