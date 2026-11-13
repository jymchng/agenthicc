"""TUIR-05 in the TUX-02 status line: attention segments say they count sessions."""

from __future__ import annotations

import pytest
from rich.text import Text

from agenthicc.tui.attention_labels import sessions_need_you, sessions_running
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.rendering import visible_len
from agenthicc.tui.workspace.components import StatusLine

pytestmark = pytest.mark.unit


def _line(needs_you: int, running: int, cols: int) -> str:
    app = AppState.create()
    app.conversation.background_needs_you.set(needs_you)
    app.conversation.background_running.set(running)
    markup = StatusLine(app).markup(cols)
    assert visible_len(markup) <= cols
    return Text.from_markup(markup).plain


@pytest.mark.parametrize("cols", [80, 120])
def test_seven_waiting_sessions_read_as_sessions(cols: int) -> None:
    line = _line(7, 1, cols)

    assert "◆ 7 sessions need you ←" in line
    assert "● 1 session running" in line


def test_the_needs_you_segment_outlasts_the_running_one() -> None:
    assert "● 1 session running" not in _line(7, 1, 60)
    assert _line(7, 1, 50) == " ● Idle · ◆ 7 sessions need you ←"


def test_narrow_widths_drop_words_before_the_unit() -> None:
    app = AppState.create()
    app.conversation.background_needs_you.set(7)
    app.conversation.context_window.set(100)
    app.conversation.context_tokens.set(42)
    line = Text.from_markup(StatusLine(app).markup(40)).plain
    assert line == " ● Idle · ◆ 7 sess. ← · ctx 42%"


@pytest.mark.parametrize(
    ("count", "needs", "running"),
    [
        (1, "1 session needs you", "1 session running"),
        (2, "2 sessions need you", "2 sessions running"),
    ],
)
def test_singular_and_plural(count: int, needs: str, running: str) -> None:
    assert sessions_need_you(count) == needs
    assert sessions_running(count) == running
