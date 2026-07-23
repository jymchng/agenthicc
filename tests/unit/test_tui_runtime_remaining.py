"""Additional coverage for durable TUI runtime state and trigger UX."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandlerBase, TriggerManager
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

pytestmark = pytest.mark.unit


def test_session_log_index_restore_and_corruption_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as log_module
    from agenthicc.tui.runtime.session_log import (
        SessionEventLog,
        find_latest_session_for_cwd,
        register_session,
        restore_session,
        touch_session,
    )

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(log_module, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(log_module, "_SESSION_INDEX", sessions / "index.json")
    assert find_latest_session_for_cwd(str(tmp_path)) is None
    register_session("one", str(tmp_path), "model")
    register_session("two", str(tmp_path), "model-2")
    touch_session("one")
    assert find_latest_session_for_cwd(str(tmp_path)) in {"one", "two"}
    touch_session("missing")
    (sessions / "index.json").write_text("not-json", encoding="utf-8")
    assert find_latest_session_for_cwd(str(tmp_path)) is None
    register_session("restore", str(tmp_path), "model")

    event_log = SessionEventLog("restore")
    event_log.append(
        ConversationEvent("start", "turn_start", {"turn_id": "t", "agent_name": "a"}, 1.0)
    )
    event_log.append(
        ConversationEvent(
            "tokens", "tokens", {"input_tokens": 3, "output_tokens": 4, "cost_usd": 0.5}, 2.0
        )
    )
    event_log.append(ConversationEvent("text", "text", {"text": "hello"}, 3.0))
    event_log.close()
    event_log.close()
    path = sessions / "restore" / "conversation.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("bad-json\n")
        fh.write(json.dumps({"kind": "text"}) + "\n")
    loaded = SessionEventLog.load("restore")
    assert [event.kind for event in loaded] == ["turn_start", "tokens", "text"]
    state = AppState.create()
    asyncio.run(restore_session("restore", state))
    assert state.conversation.tokens_in() == 3
    assert state.conversation.tokens_out() == 4
    assert state.conversation.turn_count() == 1


@pytest.mark.asyncio
async def test_unified_input_trigger_overlay_and_interactive_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
    from agenthicc.tui.workspace.overlay import OverlayHost

    state = AppState.create()
    bus = CommandBus()
    sent: list[str] = []
    bus.register(SendMessageCommand, lambda command: sent.append(command.text))
    triggers = TriggerManager()
    handler = SlashCommandTrigger()
    triggers.register(handler)
    session = UnifiedInputSession(
        state, bus, triggers, overlay_host=OverlayHost(state), cwd=Path(".")
    )
    session._buf.set(list("/he"))
    assert session._find_trigger_tail() == ("/", [], "he")
    session._find_trigger_tail = lambda: None  # type: ignore[method-assign]
    await session._open_trigger_overlay("/")
    assert session._overlay is not None and session._overlay.active
    await session._dispatch(Key.ESC, "")
    assert not session._overlay.active
    session._buf.clear()
    del session._find_trigger_tail
    await session._dispatch(Key.CHAR, "/")
    await session._dispatch(Key.CHAR, "x")
    await session._dispatch(Key.ENTER, "")
    await asyncio.sleep(0)
    assert sent == ["/x"]

    class Backend:
        def is_interactive(self) -> bool:
            return True

        def enter_raw_mode(self) -> object:
            class Raw:
                def __enter__(self) -> None:
                    return None

                def __exit__(self, *args: object) -> None:
                    return None

            return Raw()

        def read_key(self) -> tuple[Key, str]:
            return Key.CTRL_C, ""

    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: Backend())
    await session.run()


def test_trigger_handlers_and_picker_navigation(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("read", encoding="utf-8")
    (tmp_path / "docs" / ".hidden").write_text("hidden", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("notes", encoding="utf-8")
    mention = AtMentionTrigger()
    assert mention.can_activate([])
    assert not mention.can_activate(list("email"))
    matches = mention.get_matches("read", TriggerContext(tmp_path))
    assert any(item.value == "docs/README.md" for item in matches)
    assert mention.get_matches("missing/", TriggerContext(tmp_path)) == []
    assert mention.get_matches("docs/R", TriggerContext(tmp_path))
    assert mention.on_select(None, "x", []).buffer == list("@x")
    assert mention.on_cancel("x", ["text"])[-2:] == ["@", "x"]

    from agenthicc.commands.command import Command
    from agenthicc.commands.registry import UnifiedCommandRegistry

    commands = UnifiedCommandRegistry()
    commands.register(Command("/very-long", "A very long description " * 5, argument_hint="[x]"))
    slash = SlashCommandTrigger(commands)
    item = slash.get_matches("very", TriggerContext(tmp_path))[0]
    assert slash.get_lines(item, 30)
    assert len(slash.get_lines(item, 200)) == 1
    assert slash.can_activate([]) and not slash.can_activate(["x"])

    class Handler(TriggerHandlerBase):
        char = "/"
        label = "test"

        def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
            return [MatchItem("one", "one", hint="hint"), MatchItem("two", "two")]

        def on_select(self, item: MatchItem | None, fragment: str, buf: list[str]):
            from agenthicc.tui.trigger import TriggerResult

            return TriggerResult(buf + list(item.value if item else fragment))

        def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
            return buf + list(fragment)

    manager = TriggerManager()
    manager.register(Handler())
    completed: list[object] = []
    picker = TriggerPickerOverlay(["/", "a"], manager, tmp_path, completed.append)
    picker.render()
    picker.handle_key(Key.DOWN, "")
    picker.handle_key(Key.UP, "")
    picker.handle_key(Key.TAB, "")
    picker.handle_key(Key.CHAR, "x")
    picker.handle_key(Key.BACKSPACE, "")
    picker.handle_key(Key.ENTER, "")
    picker.handle_key(Key.ESC, "")
    assert completed


def test_posix_backend_non_tty_and_prompt_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.tui.input.renderer import build_prompt, show_exit_hint
    from agenthicc.tui.terminal.posix_backend import PosixBackend

    backend = PosixBackend()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert not backend.is_interactive()
    with backend.enter_raw_mode():
        pass
    assert build_prompt(list("a\nb"), 2)
    out = io.StringIO()
    show_exit_hint("session", out)
    show_exit_hint("", out)
    assert "agenthicc" in out.getvalue()
