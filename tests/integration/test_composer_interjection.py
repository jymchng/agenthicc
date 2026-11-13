"""TUX-04 §2.6: Ctrl+Enter's interjection reaches the agent at the next tool boundary.

A scripted transport runs one tool and then answers. While the tool runs the
user types two messages into the composer: one with Ctrl+Enter (an
interjection) and one with Enter (queued for after the turn). The second
model request carries the interjection and nothing else; the queued message
is still waiting when the turn ends.

NOTE: no ``from __future__ import annotations`` here — ``@tool()`` inspects
real annotations at decoration time.
"""

import asyncio

import pytest
from lauren_ai._signals import SignalBus
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.unified_session import InputMode, UnifiedInputSession
from agenthicc.tui.runtime.commands import SendMessageCommand
from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.integration


def _user_texts(messages: list[object]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        content = (
            message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        )
        if role == "user" and isinstance(content, str):
            texts.append(content)
    return texts


async def test_an_interjection_is_delivered_at_the_next_tool_boundary(running_processor):
    session, ctx, _workspace, _input = _make_session()
    ctx.command_bus.register(SendMessageCommand, session.handle_send)
    composer = UnifiedInputSession(ctx.app_state, ctx.command_bus)
    composer.set_mode(InputMode.STREAMING)

    async def type_keys(text: str, final: tuple[Key, str]) -> None:
        for ch in text:
            await composer._dispatch(Key.CHAR, ch)
        await composer._dispatch(*final)

    @tool()
    async def inspect_item(item: str) -> dict:
        """Inspect an item before answering the user."""
        # The user types while the tool runs.
        # Queued first, so a FIFO that ignored the kind would deliver it instead.
        await type_keys("then update the changelog", (Key.ENTER, ""))
        await type_keys("check the lockfile too", (Key.MODIFIED, "ctrl+enter"))
        return {"item": item, "status": "ready"}

    mock = MockTransport()
    mock.queue_tool_use("inspect_item", {"item": "first"})
    mock.queue_response(
        Completion(
            id="c2",
            model="mock-model",
            content="Checked the lockfile.",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
    )
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())

    from agenthicc.runners.agent_turn import _run_agent_turn

    turn = asyncio.create_task(
        _run_agent_turn(
            "initial request",
            runner,
            running_processor,
            project_plugin_tools=[inspect_item],
            next_queued_message=session._next_queued_message,
        )
    )
    session._agent_task = turn  # the composer's messages arrive while it runs
    await turn

    assert len(mock.calls) == 2
    second = _user_texts(list(mock.calls[1].messages))
    assert "check the lockfile too" in second
    assert "then update the changelog" not in second
    assert session._msg_queue.texts() == ["then update the changelog"]
