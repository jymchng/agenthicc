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
            "summary": "command.py",
            "presentation": {"exploratory": True},
        },
    )
    state.conversation.append_event(
        "tool_complete",
        {
            "name": "grep_files",
            "success": True,
            "summary": '"def _emit" in _runner.py',
            "presentation": {"exploratory": True},
        },
    )
    state.conversation.append_event(
        "tool_complete",
        {"name": "write_file", "success": True, "summary": "notes.md"},
    )
    state.conversation.append_event("text", {"text": "Inspection complete."})
    appender._flush_batch()
    appender.unmount()

    # TUX-06: the group is one row; the next tool row follows without a blank.
    rows = [row.rstrip() for row in output.getvalue().splitlines()]
    assert rows[:4] == [
        '● Explored 1 file, 1 search  command.py, "def _emit" in _runner.py',
        "● Write notes.md",
        "",
        "Inspection complete.",
    ]

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
                        "summary": "one.py",
                        "presentation": {"exploratory": True},
                    },
                    1.0,
                ),
                ConversationEvent(
                    "two",
                    "tool_complete",
                    {
                        "name": "search_files",
                        "success": True,
                        "summary": '"needle" in src/',
                        "presentation": {"exploratory": True},
                    },
                    2.0,
                ),
            ]
        )
        # Resume ends with ``flush()``, which closes the open group.
        appender.flush()
        return output.getvalue()

    first = render()
    second = render()
    assert first == second
    assert first.splitlines() == ['● Explored 1 file, 1 search  one.py, "needle" in src/']
