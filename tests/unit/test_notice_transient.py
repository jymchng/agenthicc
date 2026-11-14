"""TUX-08 §2.2 / AC 5: transient notices live on the status line, never in the scrollback."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, build_builtin_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.tui.conversation_store import TRANSIENT_NOTICE_S, AppState, ConversationEvent
from agenthicc.tui.workspace.announcer import Announcer
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.notice import Notice

pytestmark = pytest.mark.unit


def _console() -> Console:
    return Console(record=True, width=80, force_terminal=False, color_system=None)


def _dnd_context(state: AppState, console: Console, args: str = "") -> CommandContext:
    enabled = False

    def toggle() -> bool:
        nonlocal enabled
        enabled = not enabled
        return enabled

    return CommandContext(
        text=f"/dnd {args}".strip(),
        args=args,
        model="test/model",
        console=console,
        config=AgenthiccConfig(),
        toggle_dnd=toggle,
        transient_notify=state.conversation.notify,
    )


async def test_a_confirmation_shows_on_the_status_line_for_four_seconds_and_adds_no_row() -> None:
    state = AppState.create()
    events: list[ConversationEvent] = []
    state.conversation.on_event(events.append)
    console = _console()
    command = build_builtin_registry().get("/dnd")
    assert command is not None and command.handler is not None

    assert command.handler(_dnd_context(state, console)) is True

    notice = state.conversation.transient_notice()
    assert notice == Notice("success", "Do not disturb on")
    # Nothing was printed and no conversation event was appended: the
    # scrollback (and the session log, which records events) gains no row.
    assert console.export_text() == ""
    assert events == []
    handle = state.conversation._transient_notice_handle
    assert handle is not None
    remaining = handle.when() - asyncio.get_running_loop().time()
    assert TRANSIENT_NOTICE_S == 4.0
    assert TRANSIENT_NOTICE_S - 0.5 < remaining <= TRANSIENT_NOTICE_S


async def test_a_transient_notice_clears_itself_and_a_newer_one_replaces_it() -> None:
    state = AppState.create()
    first = state.conversation.notify("info", "Copied", duration=0.05)
    second = state.conversation.notify("info", "Queued", duration=0.4)
    assert state.conversation.transient_notice() is second
    await asyncio.sleep(0.15)
    # The first notice's timer was cancelled, so it cannot clear the second.
    assert state.conversation.transient_notice() is second
    assert first is not second
    await asyncio.sleep(0.5)
    assert state.conversation.transient_notice() is None


def test_an_error_is_persistent_even_where_a_status_line_exists() -> None:
    state = AppState.create()
    console = _console()
    command = build_builtin_registry().get("/dnd")
    assert command is not None and command.handler is not None

    assert command.handler(_dnd_context(state, console, args="now")) is True

    assert state.conversation.transient_notice() is None
    assert console.export_text().splitlines() == [
        " ✗ /dnd takes no arguments; run it again to toggle."
    ]


def test_without_a_status_line_a_confirmation_is_printed_as_a_notice() -> None:
    console = _console()
    context = CommandContext(
        text="/cancel", args="", model="test/model", console=console, config=AgenthiccConfig()
    )
    context.notify("info", "No active intent to cancel.")
    assert console.export_text().splitlines() == [" · No active intent to cancel."]


def test_screen_readers_hear_a_transient_notice_once_in_words() -> None:
    state = AppState.create()
    console = _console()
    appender = ScrollBufferAppender(state, console)
    rendered: list[str] = []
    appender._queue_event = lambda event: rendered.append(str(event.payload["text"]))  # type: ignore[method-assign]
    announcer = Announcer(state, appender)
    announcer.start()
    rendered.clear()  # the status line says "Idle." once as the announcer starts (TUX-02)
    try:
        state.conversation.notify("success", "Theme: dark")
        state.conversation.notify("success", "Theme: dark")
        state.conversation.notify("error", "Copy failed")
    finally:
        announcer.stop()
    assert rendered == ["Done: Theme: dark", "Error: Copy failed"]
