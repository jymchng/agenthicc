"""PRD-77 / TUX-04 §2.2: the trigger picker follows the composer's buffer.

The picker has no text of its own: the trigger is the text from the trigger
character to the cursor, every key it does not claim edits the prompt, and
Enter with no match still submits in one press (PRD-77).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandlerBase, TriggerManager
from agenthicc.tui.trigger import TriggerResult
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.workspace.overlay import OverlayHost
from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

pytestmark = pytest.mark.unit


class _Commands(TriggerHandlerBase):
    """A ``/`` handler with a fixed match list (no registry needed)."""

    char = "/"
    label = "Command"

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        return [MatchItem(display="/build", value="/build")] if fragment == "b" else []

    def on_select(self, item: MatchItem | None, fragment: str, buf: list[str]) -> TriggerResult:
        if item is None:
            return TriggerResult(buffer=buf + ["/"] + list(fragment))
        return TriggerResult(buffer=buf + list(item.value))

    def can_activate(self, buf: list[str]) -> bool:
        return not buf or buf[-1] == "\n"


def _session(tmp_path: Path) -> tuple[UnifiedInputSession, AppState, list[str]]:
    state = AppState.create()
    bus = CommandBus()
    sent: list[str] = []
    bus.register(SendMessageCommand, lambda command: sent.append(command.text))
    triggers = TriggerManager()
    triggers.register(AtMentionTrigger())
    triggers.register(_Commands())
    session = UnifiedInputSession(
        state, bus, trigger_registry=triggers, overlay_host=OverlayHost(state), cwd=tmp_path
    )
    return session, state, sent


async def _type(session: UnifiedInputSession, text: str) -> None:
    for char in text:
        await session._dispatch(Key.AT if char == "@" else Key.CHAR, "" if char == "@" else char)


def _picker(session: UnifiedInputSession) -> TriggerPickerOverlay:
    picker = session.picker
    assert picker is not None
    assert session._overlay is not None and session._overlay.widget is picker
    return picker


async def test_typing_the_trigger_opens_the_picker_on_the_buffer(tmp_path: Path) -> None:
    session, state, _ = _session(tmp_path)
    await _type(session, "hello @do")

    picker = _picker(session)
    assert picker.anchor == 6
    assert picker.fragment == "do"
    # The prompt holds the text: the picker has no private copy.
    assert "".join(state.input.buf()) == "hello @do"


async def test_a_second_at_is_fragment_text_and_enter_without_match_submits(
    tmp_path: Path,
) -> None:
    session, _state, sent = _session(tmp_path)
    await _type(session, "@@")

    assert _picker(session).fragment == "@"
    await session._dispatch(Key.ENTER, "")
    assert sent == ["@@"]
    assert session.picker is None


async def test_a_slash_inside_a_mention_stays_in_the_mention(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    session, _state, _ = _session(tmp_path)
    await _type(session, "@docs/")

    picker = _picker(session)
    assert picker.handler.char == "@"
    assert picker.fragment == "docs/"


async def test_typing_after_a_committed_mention_reopens_its_picker(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    session, _state, sent = _session(tmp_path)
    await _type(session, "@docs/")
    await session._dispatch(Key.ESC, "")
    assert session.picker is None

    await _type(session, ".")
    picker = _picker(session)
    assert picker.anchor == 0 and picker.fragment == "docs/."
    # "@docs/." has no match, so one Enter sends it (PRD-77 bug 1 and 2).
    await session._dispatch(Key.ENTER, "")
    assert sent == ["@docs/."]


async def test_space_is_text_and_never_selects(tmp_path: Path) -> None:
    session, state, sent = _session(tmp_path)
    await _type(session, "/b")
    assert [item.value for item in _picker(session).matches] == ["/build"]

    await _type(session, " ")
    picker = _picker(session)
    assert picker.fragment == "b "
    assert "".join(state.input.buf()) == "/b "
    assert sent == []


async def test_enter_on_a_mention_inserts_it_and_does_not_submit(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("x", encoding="utf-8")
    session, state, sent = _session(tmp_path)
    await _type(session, "see @docs/i")

    await session._dispatch(Key.ENTER, "")
    assert "".join(state.input.buf()) == "see @docs/index.md"
    assert session.picker is None
    assert sent == []
    await session._dispatch(Key.ENTER, "")
    assert sent == ["see @docs/index.md"]


async def test_tab_completes_and_never_submits(tmp_path: Path) -> None:
    session, state, sent = _session(tmp_path)
    await _type(session, "/b")
    await session._dispatch(Key.TAB, "")
    assert "".join(state.input.buf()) == "/build"
    assert session.picker is None
    assert sent == []

    await _type(session, "@nothing")
    await session._dispatch(Key.TAB, "")
    assert sent == []


async def test_esc_closes_the_picker_and_keeps_the_text(tmp_path: Path) -> None:
    session, state, sent = _session(tmp_path)
    await _type(session, "see @src")
    await session._dispatch(Key.ESC, "")
    assert session.picker is None
    assert "".join(state.input.buf()) == "see @src"
    assert sent == []


async def test_deleting_the_trigger_character_closes_the_picker(tmp_path: Path) -> None:
    session, state, _ = _session(tmp_path)
    await _type(session, "see @s")
    await session._dispatch(Key.BACKSPACE, "")
    assert _picker(session).fragment == ""
    await session._dispatch(Key.BACKSPACE, "")
    assert session.picker is None
    assert "".join(state.input.buf()) == "see "


async def test_moving_the_cursor_out_of_the_trigger_closes_the_picker(tmp_path: Path) -> None:
    session, _state, _ = _session(tmp_path)
    await _type(session, "see @src")
    await session._dispatch(Key.LEFT, "")
    assert _picker(session).fragment == "sr"
    await session._dispatch(Key.CTRL_A, "")
    assert session.picker is None


async def test_a_trigger_typed_mid_prompt_is_plain_text(tmp_path: Path) -> None:
    session, state, _ = _session(tmp_path)
    await _type(session, "foo")
    await session._dispatch(Key.HOME, "")
    await _type(session, "/")
    assert session.picker is None
    assert "".join(state.input.buf()) == "/foo"
