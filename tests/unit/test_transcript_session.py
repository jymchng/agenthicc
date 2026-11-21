"""TUX-09: how the session opens the view, forks for ``/rewind`` and registers the keys."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, build_builtin_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.runners import tui_session
from agenthicc.runners.tui_session import SessionSwitch
from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.runtime import CommandBus, OpenTranscriptCommand
from agenthicc.tui.transcript.fork import take_session_draft
from agenthicc.tui.cbreak_reader import Key
from tests.transcript_support import scheduler_session, use_sessions_dir, write_session
from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.unit


async def test_a_fork_switches_to_the_new_session_without_returning_to_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str | None]] = []
    script = [
        SessionSwitch("fork", "forked", origin="origin", origin_cwd="/origin"),
        None,  # the fork ends normally; the original does not reopen
    ]

    async def run_in(cwd: str | None, *, resume_id: str | None, **_kwargs: object) -> object:
        calls.append((resume_id, cwd))
        return script.pop(0)

    monkeypatch.setattr(tui_session, "_run_tui_session_in", run_in)
    await tui_session._run_tui_session(resume_id="origin")
    assert calls == [("origin", None), ("forked", "/origin")]


async def test_rewind_forks_in_a_worker_and_ends_the_session_with_a_fork_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    use_sessions_dir(monkeypatch, sessions)
    monkeypatch.setattr("agenthicc.memory.journal._SESSIONS_DIR", sessions)
    write_session(sessions, "tui-coverage", scheduler_session(4))
    session, ctx, _workspace, input_session = _make_session()
    ctx.console = Console(file=None, record=True)

    started: list[asyncio.Future[None]] = []
    session._tasks = SimpleNamespace(  # type: ignore[attr-defined]
        start=lambda coroutine, name: started.append(asyncio.ensure_future(coroutine))
    )
    session._rewind_to("turn 3: look at the scheduler", 3)  # type: ignore[attr-defined]
    assert len(started) == 1  # the fork runs in a task, off the key handler
    await started[0]

    switch = session.switch  # type: ignore[attr-defined]
    assert switch is not None and switch.action == "fork"
    assert switch.origin == "tui-coverage"
    assert input_session.exit_requested is True
    assert take_session_draft(switch.target) == "turn 3: look at the scheduler"
    # No journal in this fixture: the fork says the model starts without memory.
    notice = ctx.app_state.conversation.notification()
    assert notice is not None and "does not remember" in str(notice)


async def test_a_turn_that_cannot_be_forked_is_reported_and_the_session_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sessions_dir(monkeypatch, tmp_path / "sessions")
    session, ctx, _workspace, input_session = _make_session()
    await session._fork_and_switch(9)  # type: ignore[attr-defined]
    assert session.switch is None  # type: ignore[attr-defined]
    assert input_session.exit_requested is False
    notice = ctx.app_state.conversation.transient_notice()
    assert notice is not None and notice.severity == "error" and "no turn 9" in notice.title


async def test_run_registers_the_transcript_and_pane_commands() -> None:
    from agenthicc.tui.runtime import ScrollPaneCommand

    session, ctx, _workspace, input_session = _make_session()

    async def input_run() -> None:
        return None

    input_session.run = input_run  # type: ignore[attr-defined]
    await session.run()  # type: ignore[attr-defined]
    assert ctx.command_bus.handles(OpenTranscriptCommand)
    assert ctx.command_bus.handles(ScrollPaneCommand)


async def test_a_draft_left_by_rewind_lands_in_the_new_sessions_composer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.tui.transcript.fork import write_session_draft

    use_sessions_dir(monkeypatch, tmp_path / "sessions")
    session, ctx, _workspace, input_session = _make_session()
    write_session_draft("tui-coverage", "resend this prompt")
    texts: list[str] = []
    input_session.set_text = texts.append  # type: ignore[attr-defined]

    async def input_run() -> None:
        return None

    input_session.run = input_run  # type: ignore[attr-defined]
    await session.run()  # type: ignore[attr-defined]
    assert texts == ["resend this prompt"]
    assert take_session_draft("tui-coverage") == ""  # taken once


def _context(**fields: object) -> CommandContext:
    return CommandContext(
        text="/x",
        args="",
        model="m",
        console=Console(record=True, width=80, force_terminal=False),
        config=AgenthiccConfig(),
        session_id="s",
        **fields,  # type: ignore[arg-type]
    )


def test_the_new_commands_are_registered_and_reach_the_session() -> None:
    registry = build_builtin_registry()
    opened: list[str] = []
    for name, expected in (("/history", "history"), ("/logs", "logs"), ("/rewind", "rewind")):
        command = registry.get(name)
        assert command is not None and command.handler is not None
        assert command.handler(_context(open_transcript=opened.append))
        assert opened[-1] == expected
    fullscreen = registry.get("/fullscreen")
    assert fullscreen is not None and fullscreen.handler is not None
    toggled: list[bool] = []
    assert fullscreen.handler(_context(toggle_fullscreen=lambda: toggled.append(True) or True))
    assert toggled == [True]
    # Outside an interactive session each one says so instead of failing.
    for name in ("/history", "/logs", "/rewind", "/fullscreen"):
        command = registry.get(name)
        assert command is not None and command.handler is not None
        assert command.handler(_context())


def test_the_keys_are_in_the_keymap_and_documented_for_help() -> None:
    keymap = Keymap.build(DEFAULT_ACTIONS)
    assert keymap.resolve("composer", "ctrl+t").name == "composer.transcript"  # type: ignore[union-attr]
    names = {entry.action for entry in keymap.help("transcript_view")}
    assert {
        "transcript_view.search",
        "transcript_view.turn_next",
        "transcript_view.attention_next",
        "transcript_view.expand",
        "transcript_view.copy",
        "transcript_view.editor",
        "transcript_view.close",
    } <= names
    scrolled = frozenset({"fullscreen", "scrolled"})
    assert keymap.resolve("composer", "end", scrolled).name == "composer.pane_follow"  # type: ignore[union-attr]
    assert keymap.resolve("composer", "end", frozenset({"fullscreen"})).name == "composer.end"  # type: ignore[union-attr]
    assert keymap.resolve("composer", "pageup", frozenset({"fullscreen"})) is not None
    assert keymap.resolve("composer", "pageup") is None  # inline: PageUp is the terminal's


async def test_ctrl_t_in_the_composer_opens_the_transcript_even_during_a_turn() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.input.unified_session import InputMode

    for mode in (InputMode.IDLE, InputMode.STREAMING):
        state = AppState.create()
        bus = CommandBus()
        opened: list[OpenTranscriptCommand] = []
        bus.register(OpenTranscriptCommand, opened.append)
        session = UnifiedInputSession(state, bus)
        session.set_mode(mode)
        await session._dispatch(Key.CTRL_T, "")
        assert len(opened) == 1
        await asyncio.sleep(0)
