"""TUIR-05: the attention badges say they count sessions, at every width."""

from __future__ import annotations

import pytest
from rich.text import Text

from agenthicc.tui.attention_labels import sessions_need_you, sessions_running
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.rendering import visible_len
from agenthicc.tui.workspace.components import FooterComponent

pytestmark = pytest.mark.unit


def _footer(needs_you: int, running: int) -> FooterComponent:
    app = AppState.create()
    app.conversation.background_needs_you.set(needs_you)
    app.conversation.background_running.set(running)
    return FooterComponent(app)


def _attention_row(footer: FooterComponent, cols: int) -> str:
    rows = footer.markup_lines(cols)
    assert all(visible_len(row) <= cols for row in rows)
    (row,) = [Text.from_markup(row).plain for row in rows if "◆" in row or "●" in row]
    return row


@pytest.mark.parametrize("cols", [80, 120])
def test_seven_waiting_sessions_read_as_sessions(cols: int) -> None:
    row = _attention_row(_footer(7, 1), cols)

    assert "◆ 7 sessions need you (Ctrl+G)" in row
    assert "● 1 session running" in row


def test_narrow_widths_drop_words_before_the_unit() -> None:
    footer = _footer(7, 1)

    assert _attention_row(footer, 40).strip() == "◆ 7 sessions need you  |  ● 1 sess."
    assert _attention_row(footer, 26).strip() == "◆ 7 sess.  |  ● 1 sess."
    assert _attention_row(footer, 14).strip() == "◆ 7  |  ● 1"


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
