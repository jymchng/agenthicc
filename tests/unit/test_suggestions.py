"""TUX-03 §2.5: the empty composer suggests what fits the context, and Tab accepts a command."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agenthicc.commands.builtins import build_builtin_registry
from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import InputMode, UnifiedInputSession
from agenthicc.tui.keymap import Keymap
from agenthicc.tui.runtime import CommandBus
from agenthicc.tui.runtime.commands import CycleAgentCommand
from agenthicc.tui.suggestions import (
    TIPS,
    Suggestion,
    agents_md_missing,
    current_suggestion,
    static_suggestion,
    tip_for,
)
from agenthicc.tui.workspace.components import ComposerComponent
from agenthicc.tui.workspace.live_region import lines_text

pytestmark = pytest.mark.unit

_KEYMAP = Keymap.build(DEFAULT_ACTIONS)


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", sessions)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSION_INDEX", sessions / "index.json")
    return home


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


def _static(directory: Path, *, tip_index: int = 0, keymap: Keymap = _KEYMAP) -> Suggestion:
    return static_suggestion(directory, session_id="current", tip_index=tip_index, keymap=keymap)


def _placeholder(app: AppState, cols: int = 100) -> str:
    first = lines_text(ComposerComponent(app).lines(cols))[0]
    return first.removeprefix("❯ ").rstrip()


def _interrupted_session(directory: Path, session_id: str, last: str) -> None:
    from agenthicc.tui.runtime.session_log import get_session_log_path, register_session

    register_session(session_id, str(directory), "m")
    log = get_session_log_path(session_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    kinds = ["user_message", "turn_start", "text", last]
    log.write_text("".join(json.dumps({"kind": kind, "payload": {}}) + "\n" for kind in kinds))


# ── AC 7: /init ───────────────────────────────────────────────────────────────


def test_a_repository_without_agents_md_suggests_init(machine: Path, repository: Path) -> None:
    suggestion = _static(repository)

    assert (suggestion.kind, suggestion.command) == ("init", "/init")
    assert suggestion.text == "try /init to write AGENTS.md for this repository"


def test_the_placeholder_names_init_without_agents_md_and_not_with_it(
    machine: Path, repository: Path
) -> None:
    app = AppState.create()
    app.conversation.suggestion.set(_static(repository))
    assert "/init" in _placeholder(app)

    (repository / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    app.conversation.suggestion.set(_static(repository))

    assert "/init" not in _placeholder(app)


def test_an_agents_md_in_a_parent_directory_of_the_repository_counts(
    machine: Path, repository: Path
) -> None:
    (repository / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    package = repository / "packages" / "app"
    package.mkdir(parents=True)

    assert not agents_md_missing(package)


def test_outside_a_repository_there_is_nothing_to_initialise(machine: Path, tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()

    assert not agents_md_missing(loose)
    assert _static(loose).kind == "tip"


# ── the order ─────────────────────────────────────────────────────────────────


def test_an_interrupted_session_suggests_resume(machine: Path, repository: Path) -> None:
    (repository / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    _interrupted_session(repository, "earlier", last="tool_complete")

    suggestion = _static(repository)

    assert (suggestion.kind, suggestion.command) == ("resume", "/resume")
    assert suggestion.text == "resume your last session: /resume"


def test_a_session_that_finished_its_last_turn_is_not_interrupted(
    machine: Path, repository: Path
) -> None:
    (repository / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    _interrupted_session(repository, "earlier", last="turn_complete")

    assert _static(repository).kind == "tip"


def test_the_current_session_is_never_the_one_to_resume(machine: Path, repository: Path) -> None:
    (repository / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    _interrupted_session(repository, "current", last="tool_complete")

    assert _static(repository).kind == "tip"


def test_init_comes_before_resume_and_resume_before_a_tip(machine: Path, repository: Path) -> None:
    _interrupted_session(repository, "earlier", last="tool_complete")
    assert _static(repository).kind == "init"

    (repository / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    assert _static(repository).kind == "resume"


def test_background_sessions_that_need_you_come_before_a_tip_but_after_init_and_resume() -> None:
    app = AppState.create()
    conv = app.conversation
    conv.suggestion.set(Suggestion("tip", "@ adds a file"))
    assert _placeholder(app) == "@ adds a file"

    conv.background_needs_you.set(2)
    assert _placeholder(app) == "2 sessions need you · press ←"

    conv.suggestion.set(
        Suggestion("init", "try /init to write AGENTS.md for this repository", "/init")
    )
    assert "/init" in _placeholder(app)

    conv.suggestion.set(Suggestion("resume", "resume your last session: /resume", "/resume"))
    assert "/resume" in _placeholder(app)

    conv.background_needs_you.set(0)
    conv.suggestion.set(None)
    assert _placeholder(app) == "ask, or type / for commands, @ for files"


def test_one_session_needing_you_is_singular() -> None:
    app = AppState.create()
    app.conversation.background_needs_you.set(1)

    assert _placeholder(app) == "1 session needs you · press ←"


def test_a_narrow_frame_falls_back_to_the_generic_forms() -> None:
    app = AppState.create()
    app.conversation.suggestion.set(
        Suggestion("tip", "/compact summarises the conversation to free up context")
    )

    assert _placeholder(app, 100).startswith("/compact summarises")
    assert _placeholder(app, 40) == "ask, or type / for commands"
    assert _placeholder(app, 12) == "ask"


def test_the_suggestion_does_not_change_while_it_is_visible() -> None:
    app = AppState.create()
    app.conversation.suggestion.set(tip_for(3, _KEYMAP))

    shown = {_placeholder(app) for _ in range(5)}

    assert len(shown) == 1


def test_a_screen_reader_gets_no_placeholder() -> None:
    from agenthicc.config import UISettings

    app = AppState.create()
    app.ui.set(UISettings(screen_reader=True))
    app.conversation.suggestion.set(tip_for(0, _KEYMAP))

    assert _placeholder(app) == ""


# ── the tips ──────────────────────────────────────────────────────────────────


def test_there_are_twenty_tips_and_each_names_a_key_or_a_command() -> None:
    assert len(TIPS) == 20
    for tip in TIPS:
        assert (tip.action != "") != (tip.command != ""), tip
        assert tip.text.startswith("{key}") if tip.action else tip.text.startswith(tip.command)


def test_every_tip_renders_with_a_real_key_or_a_real_command() -> None:
    registry = build_builtin_registry()
    for index in range(len(TIPS)):
        suggestion = tip_for(index, _KEYMAP)
        assert "{" not in suggestion.text and suggestion.text.strip()
        if suggestion.command:
            assert registry.get(suggestion.command) is not None, suggestion.text


def test_the_tip_rotation_is_deterministic_and_cycles() -> None:
    texts = [tip_for(index, _KEYMAP).text for index in range(len(TIPS))]

    assert len(set(texts)) == 20
    assert [tip_for(index + 20, _KEYMAP).text for index in range(20)] == texts
    assert tip_for(1, _KEYMAP).text == "Ctrl+R searches history"
    assert tip_for(0, _KEYMAP).text == "@ adds a file"


def test_a_tip_names_the_key_the_user_bound() -> None:
    actions = tuple(
        replace(action, keys=("ctrl+h",)) if action.name == "composer.history_search" else action
        for action in DEFAULT_ACTIONS
    )

    assert tip_for(1, Keymap.build(actions)).text == "Ctrl+H searches history"


def test_a_tip_for_an_unbound_action_is_skipped() -> None:
    actions = tuple(
        action for action in DEFAULT_ACTIONS if action.name != "composer.history_search"
    )

    assert tip_for(1, Keymap.build(actions)).text != "Ctrl+R searches history"
    assert tip_for(1, Keymap.build(actions)).text.strip()


# ── Tab ───────────────────────────────────────────────────────────────────────


def _session() -> tuple[UnifiedInputSession, AppState, list[str]]:
    app = AppState.create()
    bus = CommandBus()
    cycled: list[str] = []

    async def cycle(_command: CycleAgentCommand) -> None:
        cycled.append("cycled")

    bus.register(CycleAgentCommand, cycle)
    return UnifiedInputSession(app, bus), app, cycled


async def test_tab_puts_a_suggested_command_in_the_empty_prompt() -> None:
    session, app, cycled = _session()
    app.conversation.suggestion.set(Suggestion("init", "try /init …", "/init"))

    assert "suggestion" in session.conditions()
    await session._dispatch(Key.TAB, "")

    assert app.input.buf() == list("/init")
    assert cycled == []  # Tab was the suggestion's, not the agent's


async def test_tab_accepts_a_tip_that_is_a_command_too() -> None:
    session, app, _cycled = _session()
    app.conversation.suggestion.set(tip_for(10, _KEYMAP))  # /compact

    await session._dispatch(Key.TAB, "")

    assert "".join(app.input.buf()) == "/compact"


async def test_tab_keeps_cycling_the_agent_when_the_suggestion_is_not_a_command() -> None:
    session, app, cycled = _session()
    app.conversation.suggestion.set(tip_for(0, _KEYMAP))  # a key tip

    assert "suggestion" not in session.conditions()
    await session._dispatch(Key.TAB, "")

    assert cycled == ["cycled"]
    assert app.input.buf() == []


async def test_tab_keeps_cycling_the_agent_when_there_is_no_suggestion_or_a_draft() -> None:
    session, app, cycled = _session()
    await session._dispatch(Key.TAB, "")
    app.conversation.suggestion.set(Suggestion("init", "try /init …", "/init"))
    await session._dispatch(Key.CHAR, "h")
    await session._dispatch(Key.TAB, "")

    assert cycled == ["cycled", "cycled"]
    assert "".join(app.input.buf()) == "h"


async def test_tab_does_not_accept_a_suggestion_while_a_turn_runs() -> None:
    session, app, cycled = _session()
    app.conversation.suggestion.set(Suggestion("init", "try /init …", "/init"))
    session.set_mode(InputMode.STREAMING)

    assert "suggestion" not in session.conditions()
    await session._dispatch(Key.TAB, "")

    assert app.input.buf() == []
    assert cycled == ["cycled"]


def test_the_accept_action_is_in_the_help_with_its_condition_in_words() -> None:
    action = _KEYMAP.action("composer.accept_suggestion")

    assert action.keys == ("tab",)
    from agenthicc.tui.keymap_phrases import describe_conditions

    assert (
        describe_conditions(action.when, action.unless)
        == "when the empty prompt suggests a command"
    )


def test_current_suggestion_is_none_before_the_session_decides() -> None:
    assert current_suggestion(None, 0) is None
