"""Interaction tests for manager-facing and existing prompt overlays."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.tools.approval import ApprovalRequest
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
from agenthicc.tui.workspace.overlays.config_menu import ConfigMenuOverlay
from agenthicc.tui.workspace.overlays.help import HelpOverlay
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay
from agenthicc.commands.command import Command
from agenthicc.commands.registry import UnifiedCommandRegistry

pytestmark = pytest.mark.unit


def _request(
    *, kind: str = "tool", questions: list[dict[str, object]] | None = None
) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="ask_user" if kind == "questions" else "write_file",
        tool_use_id="tool-1",
        tool_input={"path": "/tmp/demo", "content": "secret"}
        if questions is None
        else {"questions": questions},
        capabilities=frozenset({"write"}),
        event=asyncio.Event(),
        kind=kind,
    )


def test_approval_overlay_render_and_shortcuts() -> None:
    responses: list[dict[str, object]] = []
    closed: list[bool] = []
    service = SimpleNamespace(respond=lambda **kwargs: responses.append(kwargs))
    overlay = ApprovalOverlay(_request(), service, lambda: closed.append(True))
    overlay.on_mount()
    assert overlay.render()
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")
    assert responses[-1]["allowed"] is True
    assert closed
    overlay = ApprovalOverlay(_request(), service, lambda: closed.append(True))
    overlay.handle_key(Key.CHAR, "n")
    assert responses[-1]["allowed"] is False
    overlay.handle_key(Key.ESC, "")
    assert responses[-1]["allowed"] is False


def test_questions_overlay_select_type_cancel_and_render() -> None:
    responses: list[dict[str, object]] = []
    closed: list[bool] = []
    service = SimpleNamespace(respond=lambda **kwargs: responses.append(kwargs))
    questions = [
        {"id": "lang", "text": "Language?", "options": ["Python", {"label": "Rust"}]},
        {"id": "why", "text": "Why?", "options": ["Fast"]},
    ]
    overlay = QuestionsOverlay(
        _request(kind="questions", questions=questions), service, lambda: closed.append(True)
    )
    overlay.on_mount()
    assert overlay.render()
    overlay.handle_key(Key.ENTER, "")
    assert overlay._current == 1
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")  # choose the "Other" free-text option
    assert overlay.render()
    overlay.handle_key(Key.CHAR, "b")
    overlay.handle_key(Key.CHAR, "e")
    overlay.handle_key(Key.CHAR, "t")
    overlay.handle_key(Key.ENTER, "")
    assert closed
    assert responses[-1]["allowed"] is True
    assert "bet" in str(responses[-1]["message"])

    empty = QuestionsOverlay(_request(kind="questions", questions=[]), service, lambda: None)
    assert empty.render()
    empty.handle_key(Key.ESC, "")
    assert responses[-1]["allowed"] is False


def test_help_overlay_detail_and_config_editor() -> None:
    registry = UnifiedCommandRegistry()
    registry.register_many(
        [
            Command("/one", "A command", aliases=("/1",)),
            Command("/two", "Another command", group="Plugins", source_id="plugin:test"),
        ]
    )
    closed: list[bool] = []
    help_overlay = HelpOverlay(registry, lambda: closed.append(True), initial_query="/one")
    assert help_overlay.render()
    help_overlay.handle_key(Key.ESC, "")
    assert help_overlay.render()
    help_overlay.handle_key(Key.UP, "")
    help_overlay.handle_key(Key.DOWN, "")
    help_overlay.handle_key(Key.ENTER, "")
    assert help_overlay.render()
    help_overlay.handle_key(Key.ESC, "")
    help_overlay.handle_key(Key.ESC, "")
    assert closed
    assert HelpOverlay(None, lambda: None).render()

    cfg = AgenthiccConfig()
    menu = ConfigMenuOverlay(cfg, lambda: closed.append(True))
    assert menu.render()
    menu.handle_key(Key.DOWN, "")
    menu.handle_key(Key.ENTER, "")
    menu.handle_key(Key.CHAR, "9")
    menu.handle_key(Key.ENTER, "")
    menu.handle_key(Key.CHAR, "s")
    menu.handle_key(Key.LEFT, "")
    menu.handle_key(Key.RIGHT, "")
    menu.handle_key(Key.ESC, "")
    assert menu.render()
    empty = ConfigMenuOverlay(None, lambda: None)
    assert empty.render()
    empty.handle_key(Key.CHAR, "s")
