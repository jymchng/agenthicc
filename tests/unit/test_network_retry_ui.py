"""Regression tests for compact transient-network notices in the TUI."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.runners.agent_turn import (
    _network_retry_detail,
    _network_retry_reason,
)
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.workspace.appender import ScrollBufferAppender


def test_rate_limit_retry_is_classified_and_provider_message_is_bounded() -> None:
    class RateLimitError(Exception):
        status_code = 429

    error = RateLimitError(
        "Error code: 429 - {'error': {'message': 'Service temporarily unavailable. "
        "All endpoints are currently overloaded. Please try again later.'}} | "
        "provider='openai' | secret=should-not-be-shown"
    )

    assert _network_retry_reason(error) == "Provider temporarily unavailable"
    assert (
        _network_retry_detail(error)
        == "Service temporarily unavailable. All endpoints are currently overloaded. "
        "Please try again later."
    )
    assert "secret" not in _network_retry_detail(error)


def test_scroll_appender_renders_structured_retry_without_exception_dump() -> None:
    state = AppState.create()
    console = Console(record=True, width=100, force_terminal=False)
    appender = ScrollBufferAppender(state, console)

    appender._render_one(
        ConversationEvent(
            "retry",
            "network_retry",
            {
                "attempt": 1,
                "max_retries": 10,
                "delay_s": 0.8,
                "reason": "Provider temporarily unavailable",
                "detail": "All endpoints are currently overloaded. Please try again later.",
                "status_code": 429,
            },
        )
    )

    rendered = console.export_text()
    assert "⟳ Network issue retry 1/10 in 0.8s" in rendered
    assert "Provider temporarily unavailable · HTTP 429" in rendered
    assert "All endpoints are currently overloaded. Please try again later." in rendered
    assert "TransientTransportError" not in rendered
    assert "provider='openai'" not in rendered


def test_scroll_appender_does_not_render_http_zero_for_unknown_status() -> None:
    state = AppState.create()
    console = Console(record=True, width=100, force_terminal=False)
    appender = ScrollBufferAppender(state, console)

    appender._render_one(
        ConversationEvent(
            "retry",
            "network_retry",
            {
                "attempt": 2,
                "max_retries": 10,
                "delay_s": 1.6,
                "reason": "Request timed out",
                "status_code": None,
            },
        )
    )

    rendered = console.export_text()
    assert "retry 2/10 in 1.6s" in rendered
    assert "Request timed out" in rendered
    assert "HTTP 0" not in rendered


@pytest.mark.asyncio
async def test_retry_callback_emits_structured_event_for_the_scroll_appender() -> None:
    from agenthicc.runners.agent_turn import AgentTurnRunner

    state = AppState.create()
    events = []
    state.conversation.on_event(events.append)
    runner = AgentTurnRunner.__new__(AgentTurnRunner)
    runner._ctx = SimpleNamespace(conv_store=state.conversation, processor=None)

    class RateLimitError(Exception):
        status_code = 429

    await runner._emit_retry(
        1,
        10,
        0.8,
        RateLimitError("rate limited"),
    )

    assert len(events) == 1
    assert events[0].kind == "network_retry"
    assert events[0].payload["attempt"] == 1
    assert events[0].payload["max_retries"] == 10
    assert events[0].payload["status_code"] == 429
    assert events[0].payload["reason"] == "Rate limit reached"
