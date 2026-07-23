"""Behavioural coverage for portable TUI primitives and overlays."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.diff_renderer import _build_hunks, _word_spans, render_file_create, render_file_diff
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.capabilities import (
    _CONSUMED,
    _EXIT,
    BackspaceCapability,
    ClearCapability,
    CtrlCCapability,
    CtrlDCapability,
    CursorCapability,
    HistoryCapability,
    InsertCapability,
    InterruptCapability,
    ModeCycleCapability,
    NewlineCapability,
    OverlayCapability,
    PasteCapability,
    SubmitCapability,
    TriggerCapability,
)
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
from agenthicc.tui.reactive import ReactiveProperty, _Observable
from agenthicc.tui.runtime.replay import ConversationReplayer, load_for_replay
from agenthicc.tui.runtime import replay as replay_module
from agenthicc.tui.runtime.session_log import SessionEventLog, register_session
from agenthicc.tui.welcome import render_welcome

pytestmark = pytest.mark.unit


def test_reactive_property_and_welcome_and_diff_renderers(monkeypatch: pytest.MonkeyPatch) -> None:
    class State(_Observable):
        value = ReactiveProperty(0)
        values = ReactiveProperty(default_factory=list)

        def __init__(self) -> None:
            super().__init__()

    state = State()
    changes: list[str] = []
    state.on_change(lambda: changes.append("changed"))
    state.value = 1
    state.value = 1
    state.values = []
    state.off_change(changes.append)  # absent callback is intentionally harmless
    assert len(changes) == 2
    assert state.values is not State.values
    state._notify()
    assert len(changes) == 3

    assert render_welcome("model", "/a/very/long/project/path")
    assert _word_spans("old value", "new value")[0]
    opcodes = [
        ("equal", 0, 5, 0, 5),
        ("replace", 5, 6, 5, 6),
        ("equal", 6, 20, 6, 20),
        ("replace", 20, 21, 20, 21),
    ]
    assert len(_build_hunks(opcodes, 2)) == 2
    assert render_file_diff("x.py", ["a = 1", "same"], ["a = 2", "same"])
    assert render_file_diff("same.py", ["same"], ["same"])
    assert render_file_create("new.py", ["x"] * 12, max_lines=2)


def test_cbreak_reader_maps_control_escape_and_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    import agenthicc.tui.cbreak_reader as reader

    def read_one(value: bytes) -> tuple[Key, str]:
        monkeypatch.setattr(reader.os, "read", lambda _fd, _size: value)
        return reader.read_key(3)

    assert read_one(b"\x03")[0] is Key.CTRL_C
    assert read_one(b"\x04")[0] is Key.CTRL_D
    assert read_one(b"\r")[0] is Key.ENTER
    assert read_one(b"\n")[0] is Key.CTRL_ENTER
    assert read_one(b"\t")[0] is Key.TAB
    assert read_one(b"\x7f")[0] is Key.BACKSPACE
    assert read_one(b"\x15")[0] is Key.CTRL_U
    assert read_one(b"\x16")[0] is Key.CTRL_V
    assert read_one(b"@")[0] is Key.AT
    utf8 = iter([b"\xc3", b"\xa9"])
    monkeypatch.setattr(reader.os, "read", lambda _fd, _size: next(utf8))
    monkeypatch.setattr(reader.select, "select", lambda *_args: ([3], [], []))
    assert reader.read_key(3)[1] == "é"
    assert read_one(b"\xff")[0] is Key.ESC

    sequences = iter([b"\x1b", b"[", b"A"])
    monkeypatch.setattr(reader.os, "read", lambda _fd, _size: next(sequences))
    monkeypatch.setattr(reader.select, "select", lambda *_args: ([3], [], []))
    assert reader.read_key(3)[0] is Key.UP
    sequences = iter([b"\x1b", b"[", b"200~", b"hello\x1b[201~"])
    monkeypatch.setattr(reader.os, "read", lambda _fd, _size: next(sequences))
    assert reader.read_key(3) == (Key.PASTE, "hello")
    sequences = iter([b"\x1b", b"x"])
    monkeypatch.setattr(reader.os, "read", lambda _fd, _size: next(sequences))
    assert reader.read_key(3)[0] is Key.ESC
    monkeypatch.setattr(reader.select, "select", lambda *_args: ([], [], []))
    sequences = iter([b"\x1b"])
    monkeypatch.setattr(reader.os, "read", lambda _fd, _size: next(sequences))
    assert reader.read_key(3)[0] is Key.ESC
    with reader.raw_mode(-1) as fd:
        assert fd == -1


class _FakeSession:
    def __init__(self) -> None:
        self._state = AppState()
        self._buf = InputBuffer()
        self._paste = PasteState()
        self._hist = HistoryNavigator(["first", "second"])
        self._ctrl_c_count = 0
        self._overlay = None
        self._registry = None
        self._modes = SimpleNamespace(cycle=lambda: SimpleNamespace(name="Plan"))
        self._bus = SimpleNamespace(dispatch_async=self._dispatch)
        self.sent: list[object] = []
        self.opened: list[str] = []

    async def _dispatch(self, command: object) -> None:
        self.sent.append(command)

    async def _submit(self, text: str) -> None:
        self.sent.append(text)

    def _push(self) -> None:
        self._state.input.update(self._buf.buf, self._buf.cursor)

    def _paste_exit(self) -> None:
        self._paste.expand()

    def _ctrl_c_sequence(self) -> object:
        self._ctrl_c_count += 1
        return _EXIT if self._ctrl_c_count > 1 else None

    def _find_trigger_tail(self) -> None:
        return None

    async def _open_trigger_overlay(self, value: str) -> None:
        self.opened.append(value)

    async def _open_trigger_overlay_with_initial(self, value: list[str]) -> None:
        self.opened.append("".join(value))


@pytest.mark.asyncio
async def test_input_capabilities_cover_idle_streaming_and_editing_paths() -> None:
    session = _FakeSession()
    assert await OverlayCapability().handle(Key.CHAR, "x", session) is False
    overlay = SimpleNamespace(active=True, handle_key=lambda key, ch: None)
    session._overlay = overlay
    assert await OverlayCapability().handle(Key.CHAR, "x", session) is _CONSUMED
    session._overlay = None
    assert await CtrlCCapability().handle(Key.CHAR, "x", session) is False
    assert await CtrlCCapability().handle(Key.CTRL_C, "", session) is _CONSUMED
    assert await CtrlCCapability().handle(Key.CTRL_C, "", session) is _EXIT
    session._buf.insert("x")
    assert await CtrlDCapability().handle(Key.CTRL_D, "", session) is _CONSUMED
    session._buf.clear()
    assert await CtrlDCapability().handle(Key.CTRL_D, "", session) is _EXIT
    assert await CtrlDCapability().handle(Key.CHAR, "", session) is False
    session._buf.set(list("send"))
    assert await SubmitCapability(commit_history=True).handle(Key.ENTER, "", session) is _CONSUMED
    assert session.sent
    assert await SubmitCapability().handle(Key.CHAR, "", session) is False
    assert await InterruptCapability().handle(Key.CHAR, "", session) is False
    assert await NewlineCapability().handle(Key.CTRL_ENTER, "", session) is _CONSUMED
    await InsertCapability().handle(Key.CHAR, "z", session)
    await CursorCapability().handle(Key.HOME, "", session)
    await CursorCapability().handle(Key.END, "", session)
    await BackspaceCapability().handle(Key.BACKSPACE, "", session)
    await ClearCapability().handle(Key.CTRL_U, "", session)
    session._buf.set(list("line1\nline2"), cursor=8)
    await CursorCapability().handle(Key.LEFT, "", session)
    await HistoryCapability().handle(Key.UP, "", session)
    await HistoryCapability().handle(Key.DOWN, "", session)
    assert await HistoryCapability().handle(Key.CHAR, "", session) is False
    session._paste.apply(session._buf, "a\nb\nc\nd", 10)
    assert await PasteCapability().handle(Key.CTRL_V, "", session) is _CONSUMED
    assert await PasteCapability().handle(Key.PASTE, "pasted", session) is _CONSUMED
    assert await ModeCycleCapability().handle(Key.SHIFT_TAB, "", session) is _CONSUMED
    assert await ModeCycleCapability().handle(Key.CHAR, "", session) is False
    assert await InterruptCapability().handle(Key.ESC, "", session) is _CONSUMED

    session._paste.apply(session._buf, "a\nb\nc\nd", 10)
    await BackspaceCapability().handle(Key.BACKSPACE, "", session)
    session._buf.set(list("@frag"))
    session._find_trigger_tail = lambda: ("@", [], "frag")  # type: ignore[method-assign]
    session._registry = SimpleNamespace(get=lambda _char: SimpleNamespace())
    await BackspaceCapability().handle(Key.BACKSPACE, "", session)
    assert await BackspaceCapability().handle(Key.CHAR, "", session) is False


@pytest.mark.asyncio
async def test_trigger_capability_passes_or_opens_registered_trigger() -> None:
    session = _FakeSession()
    handler = SimpleNamespace(can_activate=lambda _pre: True)
    session._registry = SimpleNamespace(
        resolve=lambda key, ch: "/" if key is Key.CHAR and ch == "/" else None,
        get=lambda _char: handler,
    )
    assert await TriggerCapability().handle(Key.CHAR, "/", session) is _CONSUMED
    assert session.opened == ["/"]
    handler.can_activate = lambda _pre: False
    session._buf.set(list("text"))
    assert await TriggerCapability().handle(Key.CHAR, "/", session) is _CONSUMED
    session._registry = None
    assert await TriggerCapability().handle(Key.CHAR, "/", session) is False

    session = _FakeSession()
    session._buf.set(list("@frag"))
    session._find_trigger_tail = lambda: ("@", [], "frag")  # type: ignore[method-assign]
    session._registry = SimpleNamespace(get=lambda _char: SimpleNamespace())
    assert await InsertCapability().handle(Key.CHAR, "x", session) is _CONSUMED


@pytest.mark.asyncio
async def test_unified_input_session_dispatch_and_noninteractive_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.tui.input.unified_session import InputMode, UnifiedInputSession
    from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand

    state = AppState()
    bus = CommandBus()
    sent: list[str] = []
    bus.register(SendMessageCommand, lambda command: sent.append(command.text))
    session = UnifiedInputSession(state, bus, cwd=Path("."))
    await session._dispatch(Key.CHAR, "h")
    await session._dispatch(Key.CHAR, "i")
    assert state.input.buf() == ["h", "i"]
    await session._dispatch(Key.ENTER, "")
    assert sent == ["hi"]
    session.set_mode(InputMode.STREAMING)
    await session._dispatch(Key.CHAR, "x")
    await session._dispatch(Key.CTRL_ENTER, "")
    assert "\n" in session._buf.text
    session._ctrl_c_sequence()
    session._ctrl_c_sequence()
    assert session._find_trigger_tail() is None
    session._prepare_submission()
    assert session._buf.text == ""
    backend = SimpleNamespace(is_interactive=lambda: False)
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: backend)
    await session.run()


def test_session_log_and_replay_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = "replay-session"
    root = tmp_path / session_id
    root.mkdir()
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log._SESSION_INDEX", tmp_path / "sessions" / "index.json"
    )
    monkeypatch.setattr(replay_module, "get_session_log_path", lambda _sid: root / "conversation.jsonl")
    monkeypatch.setattr("agenthicc.tui.runtime.session_log.get_session_log_path", lambda _sid: root / "conversation.jsonl")
    register_session(session_id, str(tmp_path), "model")
    log = SessionEventLog(session_id)
    from agenthicc.tui.conversation_store import ConversationEvent

    log.append(ConversationEvent("e1", "turn_start", {"turn_id": "t1"}, 1.0))
    log.append(ConversationEvent("e2", "text", {"text": "hello"}, 2.0))
    log.close()
    pairs = load_for_replay(session_id)
    assert pairs == [("turn_start", {"turn_id": "t1"}), ("text", {"text": "hello"})]

    class Signal:
        def __init__(self) -> None:
            self.value = None

        def set(self, value: object) -> None:
            self.value = value

    class Store:
        def __init__(self) -> None:
            self.notification = Signal()
            self.events: list[tuple[str, dict[str, object]]] = []

        def append_event(self, kind: str, payload: dict[str, object]) -> None:
            self.events.append((kind, payload))

    async def run_replay() -> Store:
        store = Store()
        await ConversationReplayer(session_id, store, object()).run()
        return store

    store = asyncio.run(run_replay())
    assert store.events == pairs
    assert "Replay complete" in str(store.notification.value)
    monkeypatch.setattr(replay_module, "get_session_log_path", lambda _sid: tmp_path / "missing")
    empty = Store()
    asyncio.run(ConversationReplayer("missing", empty, object()).run())
    assert "No conversation log" in str(empty.notification.value)


def test_scroll_buffer_appender_renders_event_families(capsys: pytest.CaptureFixture[str]) -> None:
    from rich.console import Console

    from agenthicc.tui.conversation_store import ConversationEvent
    from agenthicc.tui.workspace.appender import ScrollBufferAppender

    state = AppState()
    state.conversation.session_id.set("session")
    state.conversation.model_name.set("model")
    console = Console(record=True, force_terminal=False)
    appender = ScrollBufferAppender(state, console, max_live_tool_calls=1)
    appender.mount()
    events = [
        ConversationEvent("1", "turn_start", {"agent_name": "assistant"}, 1),
        ConversationEvent("2", "user_message", {"text": "hello"}, 2),
        ConversationEvent(
            "3", "tool_complete", {"name": "read_file", "args_str": "(x)", "output_lines": ["a"]}, 3
        ),
        ConversationEvent(
            "4", "tool_complete", {"name": "run_command", "success": False, "output_lines": ["bad"]}, 4
        ),
        ConversationEvent("5", "text", {"text": "answer"}, 5),
        ConversationEvent("6", "thinking_step", {"step": "thinking", "done": True}, 6),
        ConversationEvent(
            "7", "file_modified", {"path": "x.py", "old_lines": [], "new_lines": ["x = 1"]}, 7
        ),
        ConversationEvent("8", "file_modified", {"path": "x.unknown", "tool": "delete_file"}, 8),
        ConversationEvent("9", "error", {"message": "boom", "detail": "details"}, 9),
        ConversationEvent("10", "turn_complete", {"elapsed_s": 61}, 10),
        ConversationEvent(
            "11",
            "mention_chips",
            {"chips": [{"raw": "@file.py", "kind": "file"}, {"raw": "@dir", "kind": "directory", "ok": False}]},
            11,
        ),
        ConversationEvent("12", "system", {"text": "system"}, 12),
        ConversationEvent("13", "subagent_pool_started", {"total": 2, "workers": [{"label": "a", "task": "task"}]}, 13),
        ConversationEvent("14", "subagent_worker_done", {"ok": True, "label": "a", "done": 1, "total": 2, "duration_ms": 10}, 14),
        ConversationEvent("15", "subagent_worker_done", {"ok": False, "label": "b", "done": 2, "total": 2, "error": "nope"}, 15),
        ConversationEvent("16", "subagent_pool_done", {"succeeded": 1, "total": 2, "failed": 1}, 16),
    ]
    for event in events:
        appender._render_one(event)
    appender._flush_group_summary()
    appender.print_idle_header()
    appender.unmount()
    assert "Completed" in console.export_text()
    assert "ERROR" in capsys.readouterr().out
