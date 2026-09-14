"""Unit coverage for long-question rendering and scrolling (PRD-189)."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from rich.console import Console, Group

from agenthicc.tools.approval import ApprovalRequest
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

pytestmark = pytest.mark.unit


def _request(
    text: str,
    *,
    options: list[str] | None = None,
    question_id: str = "decision",
) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="ask_user",
        tool_use_id="ask-1",
        tool_input={
            "questions": [
                {
                    "id": question_id,
                    "text": text,
                    "options": options or ["Use the default"],
                }
            ]
        },
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="questions",
    )


def _overlay(
    text: str,
    *,
    options: list[str] | None = None,
) -> tuple[QuestionsOverlay, list[dict[str, object]], list[bool]]:
    responses: list[dict[str, object]] = []
    closed: list[bool] = []
    service = SimpleNamespace(respond=lambda **kwargs: responses.append(kwargs))
    overlay = QuestionsOverlay(
        _request(text, options=options),
        service,
        lambda: closed.append(True),
    )
    overlay.on_mount()
    return overlay, responses, closed


def _terminal(monkeypatch: pytest.MonkeyPatch, width: int = 40, height: int = 24) -> None:
    monkeypatch.setattr(
        "agenthicc.tui.workspace.overlays.questions.shutil.get_terminal_size",
        lambda _fallback=(80, 24): os.terminal_size((width, height)),
    )


def _render(overlay: QuestionsOverlay, width: int = 40) -> str:
    console = Console(record=True, width=width, color_system=None, highlight=False)
    console.print(overlay.render())
    return console.export_text()


def test_long_question_wraps_as_literal_text_and_bracket_scrolls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch)
    text = (
        "[red]Question content must remain literal. "
        "Read every constraint before deciding. "
        "final-question-marker"
    )
    overlay, responses, _closed = _overlay(text)

    initial = _render(overlay)
    assert "[red]" in initial
    assert "[/] scroll" in initial
    assert len(overlay._question_lines[0]) > overlay._question_rows

    cursor_before = overlay._states[0].cursor
    overlay.handle_key(Key.CHAR, "]")
    assert overlay._states[0].question_scroll == 1
    assert overlay._states[0].cursor == cursor_before
    assert responses == []

    for _ in range(100):
        overlay.handle_key(Key.CHAR, "]")
    assert overlay._states[0].question_scroll == (
        len(overlay._question_lines[0]) - overlay._question_rows
    )
    assert "final-question-marker" in _render(overlay)

    for _ in range(100):
        overlay.handle_key(Key.CHAR, "[")
    assert overlay._states[0].question_scroll == 0
    assert responses == []


def test_wrapping_preserves_unicode_and_explicit_blank_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, width=30, height=24)
    overlay, _responses, _closed = _overlay(
        "第一段落 αβγ\n\n第二段落 — final-marker",
    )

    overlay.render()
    lines = [line.plain for line in overlay._question_lines[0]]
    assert "" in lines
    assert any("第一段落" in line for line in lines)
    assert any("第二段落" in line and "final-marker" in line for line in lines)


def test_question_viewport_height_is_stable_across_scroll_and_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, width=44, height=28)
    responses: list[dict[str, object]] = []
    service = SimpleNamespace(respond=lambda **kwargs: responses.append(kwargs))
    request = ApprovalRequest(
        tool_name="ask_user",
        tool_use_id="ask-2",
        tool_input={
            "questions": [
                {"id": "short", "text": "Short?", "options": ["Yes"]},
                {
                    "id": "long",
                    "text": " ".join(f"constraint-{i}" for i in range(80)),
                    "options": ["Yes", "No", "Other"],
                },
            ]
        },
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="questions",
    )
    overlay = QuestionsOverlay(request, service, lambda: None)
    overlay.on_mount()

    first = overlay.render()
    assert isinstance(first, Group)
    initial_height = len(first.renderables)
    overlay.handle_key(Key.CHAR, "]")
    scrolled = overlay.render()
    assert isinstance(scrolled, Group)
    assert len(scrolled.renderables) == initial_height

    overlay._states[0].answer = "Yes"
    overlay._states[0].answered = True
    overlay.handle_key(Key.RIGHT, "")
    second_question = overlay.render()
    assert isinstance(second_question, Group)
    assert len(second_question.renderables) == initial_height
    assert overlay._states[0].answer == "Yes"
    assert responses == []


def test_resize_rewraps_and_clamps_question_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    size = [(32, 24)]
    monkeypatch.setattr(
        "agenthicc.tui.workspace.overlays.questions.shutil.get_terminal_size",
        lambda _fallback=(80, 24): os.terminal_size(size[0]),
    )
    overlay, _responses, _closed = _overlay(" ".join(f"word-{i}" for i in range(100)))
    overlay.render()
    for _ in range(100):
        overlay.handle_key(Key.CHAR, "]")
    narrow_offset = overlay._states[0].question_scroll
    assert narrow_offset > 0

    size[0] = (100, 24)
    overlay.render()
    assert overlay._question_width == 96
    assert overlay._states[0].question_scroll <= max(
        0,
        len(overlay._question_lines[0]) - overlay._question_rows,
    )


def test_typing_mode_keeps_brackets_in_answer_and_uses_page_scroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, width=40, height=24)
    overlay, responses, closed = _overlay(
        " ".join(f"type-answer-constraint-{i}" for i in range(80)),
        options=["Preset"],
    )
    overlay.render()
    overlay.handle_key(Key.DOWN, "")  # Other
    overlay.handle_key(Key.ENTER, "")
    assert overlay._mode.name == "TYPING"

    overlay.render()
    before = overlay._states[0].question_scroll
    overlay.handle_key(Key.PAGE_DOWN, "")
    assert overlay._states[0].question_scroll >= before
    overlay.handle_key(Key.CHAR, "[")
    overlay.handle_key(Key.CHAR, "]")
    assert overlay._prompt_text == "[]"

    overlay.handle_key(Key.ENTER, "")
    assert closed
    assert json.loads(str(responses[-1]["message"])) == {"decision": "[]"}
