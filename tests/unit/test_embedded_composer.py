"""AGI-05: the ask_user answer input works like the main input panel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agenthicc.tools.approval import ApprovalRequest
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.embedded import ComposerContext, EmbeddedComposer
from agenthicc.tui.workspace.overlays.questions import AnswerContext, QuestionsOverlay

pytestmark = pytest.mark.unit


class _Approvals:
    def __init__(self) -> None:
        self.responses: list[tuple[bool, str, str]] = []

    def respond_for_request(
        self, request_id: str, allowed: bool, *, message: str = "", outcome: str = ""
    ) -> None:
        self.responses.append((allowed, message, outcome))


def _request(questions: list[dict[str, object]]) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="Questions",
        tool_use_id="ask-1",
        tool_input={"questions": questions},
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="questions",
    )


class _Overlay(QuestionsOverlay):
    unmounts = 0

    def on_unmount(self) -> None:
        type(self).unmounts += 1
        super().on_unmount()


async def _keys(overlay: QuestionsOverlay, keys: list[tuple[Key, str]]) -> None:
    for key, ch in keys:
        overlay.handle_key(key, ch)
        composer = overlay._composer
        if composer is not None:
            await composer.settle()


def _typed(text: str) -> list[tuple[Key, str]]:
    return [(Key.AT, "") if char == "@" else (Key.CHAR, char) for char in text]


async def _expand(text: str) -> str:
    return f"<context for {text.count('@')} mention(s)>\n{text}"


async def test_paste_newline_and_mentions_reach_the_answer(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    approvals = _Approvals()
    overlay = _Overlay(
        _request([{"id": "file", "text": "Which file?", "options": ["none"]}]),
        approvals,  # type: ignore[arg-type]
        lambda: None,
        answers=AnswerContext(composer=ComposerContext(cwd=tmp_path), expand=_expand),
    )
    overlay.on_mount()
    _Overlay.unmounts = 0

    await _keys(overlay, [(Key.DOWN, ""), (Key.ENTER, "")])  # Other
    await _keys(overlay, _typed("see @"))
    assert overlay._composer is not None and overlay._composer.picker_open
    await _keys(overlay, _typed("src/main") + [(Key.ESC, "")])  # close the picker, keep text
    assert overlay._composer is not None and not overlay._composer.picker_open
    await _keys(
        overlay,
        [
            (Key.PASTE, ".py and more"),
            (Key.CTRL_ENTER, ""),
            *_typed("line two"),
            (Key.ENTER, ""),
        ],
    )

    assert _Overlay.unmounts == 0  # the picker never unmounted the questions
    (allowed, message, outcome) = approvals.responses[-1]
    answer = json.loads(message)["file"]
    assert allowed and outcome == "answered"
    assert answer.endswith("see @src/main.py and more\nline two")
    assert answer.startswith("<context for 1 mention(s)>")
    assert overlay._form.states[0].label == "see @src/main.py and more\nline two"


async def test_a_command_runs_in_place_of_an_answer(tmp_path: Path) -> None:
    ran: list[str] = []
    approvals = _Approvals()

    def run_command(text: str) -> bool:
        if text.startswith("/usage"):
            ran.append(text)
            return True
        return False

    overlay = QuestionsOverlay(
        _request([{"id": "q", "text": "Path?", "options": ["a"]}]),
        approvals,  # type: ignore[arg-type]
        lambda: None,
        answers=AnswerContext(composer=ComposerContext(cwd=tmp_path), run_command=run_command),
    )
    overlay.on_mount()
    await _keys(overlay, [(Key.DOWN, ""), (Key.ENTER, "")])
    await _keys(overlay, [(Key.PASTE, "/usage"), (Key.ENTER, "")])

    assert ran == ["/usage"] and approvals.responses == []
    assert overlay._composer is not None and overlay._composer.text == ""

    await _keys(overlay, [(Key.PASTE, "/etc/hosts"), (Key.ENTER, "")])  # not a command
    assert json.loads(approvals.responses[-1][1]) == {"q": "/etc/hosts"}


async def test_esc_keeps_the_draft_for_the_next_visit(tmp_path: Path) -> None:
    overlay = QuestionsOverlay(
        _request([{"id": "q", "text": "Name?", "options": ["a"]}]),
        _Approvals(),  # type: ignore[arg-type]
        lambda: None,
        answers=AnswerContext(composer=ComposerContext(cwd=tmp_path)),
    )
    overlay.on_mount()
    await _keys(overlay, [(Key.DOWN, ""), (Key.ENTER, ""), (Key.PASTE, "half typed")])
    await _keys(overlay, [(Key.ESC, ""), (Key.ENTER, "")])

    assert overlay._composer is not None and overlay._composer.text == "half typed"


async def test_keys_are_dispatched_in_order_and_none_are_dropped(tmp_path: Path) -> None:
    submitted: list[str] = []

    async def submit(text: str) -> bool:
        submitted.append(text)
        return True

    composer = EmbeddedComposer(ComposerContext(cwd=tmp_path), on_submit=submit)
    for char in "abcdefghij" * 10:
        composer.feed(Key.CHAR, char)
    composer.feed(Key.ENTER, "")
    await composer.settle()

    assert submitted == ["abcdefghij" * 10]


def test_a_synchronous_caller_still_gets_every_key(tmp_path: Path) -> None:
    async def submit(_text: str) -> bool:
        return False

    composer = EmbeddedComposer(ComposerContext(cwd=tmp_path), on_submit=submit)
    composer.feed(Key.CHAR, "[")
    composer.feed(Key.PASTE, "x]")

    assert composer.text == "[x]"
