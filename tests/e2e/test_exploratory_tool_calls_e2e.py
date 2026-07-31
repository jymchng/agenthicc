"""Headless TUI journey for consolidated exploratory calls."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.e2e


def test_source_discovery_journey_groups_exploration_and_preserves_events() -> None:
    state = AppState.create()
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    appender = ScrollBufferAppender(state, console, group_exploratory_calls=True)
    appender.mount()

    state.conversation.begin_turn("assistant", turn_id="journey")
    state.conversation.append_event("turn_start", {"agent_name": "assistant"})
    state.conversation.append_event(
        "tool_complete",
        {
            "name": "read_file",
            "success": True,
            "presentation": {"exploratory": True, "target": "command.py"},
        },
    )
    state.conversation.append_event(
        "tool_complete",
        {
            "name": "search_files",
            "success": True,
            "presentation": {"exploratory": True, "target": "def _emit in _runner.py"},
        },
    )
    state.conversation.append_event(
        "tool_complete",
        {"name": "write_file", "success": True},
    )
    state.conversation.append_event("text", {"text": "Inspection complete."})
    appender._flush_batch()
    appender.unmount()

    rendered = output.getvalue()
    assert "● Explored" in rendered
    assert "└ Read command.py" in rendered
    assert "Read command.py" in rendered
    assert "Search def _emit in _runner.py" in rendered
    assert "Search def _emit in _runner.py\n\n" in rendered
    assert "Update" in rendered
    assert "Inspection complete." in rendered

    turns = state.conversation.turns()
    assert len(turns) == 1
    assert [event.kind for event in turns[0].events] == [
        "turn_start",
        "tool_complete",
        "tool_complete",
        "tool_complete",
        "text",
    ]
    assert sum(event.kind == "tool_complete" for event in turns[0].events) == 3


def test_resume_journey_reconstructs_the_same_explored_block() -> None:
    def render() -> str:
        state = AppState.create()
        output = StringIO()
        appender = ScrollBufferAppender(
            state,
            Console(file=output, force_terminal=False, color_system=None),
            group_exploratory_calls=True,
        )
        appender.replay(
            [
                ConversationEvent(
                    "one",
                    "tool_complete",
                    {
                        "name": "read_file",
                        "success": True,
                        "presentation": {"exploratory": True, "target": "one.py"},
                    },
                    1.0,
                ),
                ConversationEvent(
                    "two",
                    "tool_complete",
                    {
                        "name": "search_files",
                        "success": True,
                        "presentation": {"exploratory": True, "target": "needle in src"},
                    },
                    2.0,
                ),
            ]
        )
        appender._flush_batch()
        return output.getvalue()

    first = render()
    second = render()
    assert first == second
    assert first.count("● Explored") == 1
    assert "└ Read one.py" in first
    assert "Read one.py" in first
    assert "Search needle in src" in first
