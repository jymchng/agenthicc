"""TUX-10 §2.3-2.4 / AC 4-5: notifications, the window title and the taskbar progress.

Everything the TUI says to the terminal goes through ``write_control`` on the
backend, which never writes to a stream that is not a terminal.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from agenthicc.config import UISettings
from agenthicc.tools.approval import ApprovalRequest
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.notifications import (
    NotificationSink,
    TerminalPresence,
    TurnNotifier,
    session_label,
)
from agenthicc.tui.terminal import osc
from agenthicc.tui.terminal.osc import ProgressState

pytestmark = pytest.mark.unit

_PROGRESS_OFF = "\x1b]9;4;0;0\x07"


class FakeBackend:
    """A backend that records what the TUI writes to the terminal."""

    def __init__(self, *, progress: bool = True, links: bool = True) -> None:
        self.written: list[str] = []
        self._progress = progress
        self._links = links

    def write_control(self, sequence: str) -> None:
        self.written.append(sequence)

    def supports_progress(self) -> bool:
        return self._progress

    def supports_hyperlinks(self) -> bool:
        return self._links

    def notifications(self) -> list[str]:
        """Notifications only: ``OSC 9;4`` is progress, ``OSC 9`` and ``OSC 777`` are notices."""
        return [
            seq
            for seq in self.written
            if (seq.startswith(("\x1b]9;", "\x1b]777;")) and not seq.startswith("\x1b]9;4;"))
            or seq == "\x07"
        ]

    def progress(self) -> list[str]:
        return [seq for seq in self.written if seq.startswith("\x1b]9;4;")]

    def titles(self) -> list[str]:
        return [seq for seq in self.written if seq.startswith("\x1b]2;")]


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """One fake monotonic clock for the conversation store (turn and activity timing)."""
    fake = Clock()
    monkeypatch.setattr("agenthicc.tui.conversation_store.time.monotonic", fake)
    return fake


def _state(*, ui: UISettings | None = None, title: str = "fix flaky test") -> AppState:
    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    state.conversation.session_title.set(title)
    return state


def _notifier(
    state: AppState, backend: FakeBackend, sink_clock: Clock | None = None
) -> TurnNotifier:
    sink = NotificationSink(
        backend,  # type: ignore[arg-type]
        state,
        env={"TERM": "xterm-256color"},
        clock=sink_clock or Clock(),
    )
    notifier = TurnNotifier(state, sink)
    notifier.start()
    return notifier


def _run_turn(state: AppState, clock: Clock, seconds: float, *, interrupted: bool = False) -> None:
    conv = state.conversation
    conv.begin_turn("agent", "t1")
    clock.now += seconds
    conv.close_turn(interrupted=interrupted)


def _approval(kind: str = "tool") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="run_bash",
        tool_use_id="x",
        tool_input={"command": "npm test"},
        capabilities=frozenset({"execute"}),
        event=asyncio.Event(),
        kind=kind,
    )


# ── the sequences ─────────────────────────────────────────────────────────────


def test_a_notification_cannot_close_its_own_sequence() -> None:
    assert (
        osc.notification("osc9", "ready\n\x1b]52;clipboard\x07") == "\x1b]9;ready]52;clipboard\x07"
    )
    assert osc.title("a\x1bb\x07c") == "\x1b]2;abc\x07"
    assert osc.sanitize("x" * 500) == "x" * 240


def test_osc_777_separates_its_fields_with_semicolons_the_text_may_not_contain() -> None:
    assert osc.notification("osc777", "a;b", "agenthicc") == "\x1b]777;notify;agenthicc;a,b\x07"


@pytest.mark.parametrize(
    ("setting", "env", "mode"),
    [
        ("auto", {"TERM": "xterm-256color"}, "osc9"),
        ("auto", {"TERM": "foot"}, "osc777"),
        ("osc", {"TERM": "foot"}, "osc9"),
        ("bell", {"TERM": "xterm"}, "bell"),
        ("off", {"TERM": "xterm"}, "off"),
        ("auto", {"TERM": "dumb"}, "off"),
        ("auto", {}, "off"),
    ],
)
def test_the_notify_setting_and_the_terminal_choose_the_sequence(
    setting: str, env: dict[str, str], mode: str
) -> None:
    assert osc.notify_mode(setting, env) == mode


def test_the_bell_is_the_bell_and_off_is_nothing() -> None:
    assert osc.notification("bell", "anything") == "\x07"
    assert osc.notification("off", "anything") == ""
    assert osc.notification("osc9", "") == ""


def test_progress_is_osc_9_4_with_a_state_and_a_percentage() -> None:
    assert osc.progress(ProgressState.INDETERMINATE) == "\x1b]9;4;3;0\x07"
    assert osc.progress(ProgressState.DETERMINATE, 40) == "\x1b]9;4;1;40\x07"
    assert osc.progress(ProgressState.DETERMINATE, 400) == "\x1b]9;4;1;100\x07"
    assert osc.progress(ProgressState.CLEAR) == _PROGRESS_OFF


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"TERM": "xterm-256color", "WT_SESSION": "abc"}, True),
        ({"TERM": "xterm-256color", "ConEmuPID": "12"}, True),
        ({"TERM": "xterm-ghostty", "TERM_PROGRAM": "ghostty"}, True),
        ({"TERM": "xterm-256color"}, False),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "iTerm.app"}, False),
        ({"TERM": "xterm-256color", "WT_SESSION": "abc", "TMUX": "/tmp/t"}, False),
        ({"TERM": "dumb", "WT_SESSION": "abc"}, False),
    ],
)
def test_progress_is_only_for_terminals_that_draw_it(env: dict[str, str], expected: bool) -> None:
    assert osc.progress_supported(env) is expected


# ── the backend writes only to a terminal ─────────────────────────────────────


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_a_backend_never_writes_a_control_sequence_to_a_pipe(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    from agenthicc.tui.terminal.posix_backend import PosixBackend
    from agenthicc.tui.terminal.windows_backend import WindowsBackend

    backend = PosixBackend() if platform == "posix" else WindowsBackend()
    pipe, tty = _Stream(tty=False), _Stream(tty=True)
    monkeypatch.setattr("sys.stdout", pipe)
    backend.write_control(osc.title("x"))
    assert pipe.getvalue() == ""
    monkeypatch.setattr("sys.stdout", tty)
    backend.write_control(osc.title("x"))
    assert tty.getvalue() == "\x1b]2;x\x07"


def test_progress_is_written_only_where_wt_session_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 5: ``OSC 9;4`` only with ``WT_SESSION`` in the (fake) environment."""
    from agenthicc.tui.terminal.posix_backend import PosixBackend

    monkeypatch.setattr("sys.stdout", _Stream(tty=True))
    for name in ("WT_SESSION", "ConEmuPID", "TERM_PROGRAM", "TMUX", "STY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    backend = PosixBackend()
    assert backend.supports_progress() is False
    monkeypatch.setenv("WT_SESSION", "{guid}")
    assert backend.supports_progress() is True
    monkeypatch.setattr("sys.stdout", _Stream(tty=False))
    assert backend.supports_progress() is False  # a pipe, whatever the environment says


# ── AC 4: notifications for an away user ──────────────────────────────────────


def test_a_twelve_second_turn_while_unfocused_emits_exactly_one_osc_9(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    state.conversation.terminal_focus_known.set(True)
    state.conversation.terminal_focused.set(False)
    _notifier(state, backend)
    _run_turn(state, clock, 12.0)
    assert backend.notifications() == ["\x1b]9;fix flaky test: done in 12s\x07"]


def test_a_focused_terminal_gets_none(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    state.conversation.terminal_focus_known.set(True)
    state.conversation.terminal_focused.set(True)
    _notifier(state, backend)
    _run_turn(state, clock, 12.0)
    state.pending_approval.set(_approval())
    assert backend.notifications() == []


def test_a_short_turn_does_not_notify(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    state.conversation.terminal_focus_known.set(True)
    state.conversation.terminal_focused.set(False)
    _notifier(state, backend)
    _run_turn(state, clock, 9.0)
    assert backend.notifications() == []


def test_an_approval_while_unfocused_emits_one(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    state.conversation.terminal_focus_known.set(True)
    state.conversation.terminal_focused.set(False)
    _notifier(state, backend)
    state.conversation.begin_turn("agent", "t1")
    state.pending_approval.set(_approval())
    assert backend.notifications() == ["\x1b]9;fix flaky test: approve a command\x07"]


def test_a_failed_turn_notifies_even_when_short(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    state.conversation.terminal_focus_known.set(True)
    state.conversation.terminal_focused.set(False)
    _notifier(state, backend)
    state.conversation.begin_turn("agent", "t1")
    clock.now += 3.0
    state.conversation.close_turn(error="boom")
    assert backend.notifications() == ["\x1b]9;fix flaky test: failed after 3s\x07"]


def test_a_turn_the_user_interrupted_does_not_notify(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    state.conversation.terminal_focus_known.set(True)
    state.conversation.terminal_focused.set(False)
    _notifier(state, backend)
    _run_turn(state, clock, 40.0, interrupted=True)
    assert backend.notifications() == []


def test_unknown_focus_notifies_only_after_thirty_seconds(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    _notifier(state, backend)  # the terminal never reported focus
    _run_turn(state, clock, 25.0)
    assert backend.notifications() == []
    _run_turn(state, clock, 31.0)
    assert backend.notifications() == ["\x1b]9;fix flaky test: done in 31s\x07"]


def test_the_notify_setting_picks_the_sequence_or_silence(clock: Clock) -> None:
    for setting, expected in (("bell", ["\x07"]), ("off", [])):
        state, backend = _state(ui=UISettings(notify=setting)), FakeBackend()
        state.conversation.terminal_focus_known.set(True)
        state.conversation.terminal_focused.set(False)
        _notifier(state, backend)
        _run_turn(state, clock, 12.0)
        assert backend.notifications() == expected


def test_a_background_session_and_a_foreground_approval_are_one_notification() -> None:
    """The routing table: the same moment is never notified twice."""
    state, backend, clock = _state(), FakeBackend(), Clock()
    sink = NotificationSink(
        backend,  # type: ignore[arg-type]
        state,
        env={"TERM": "xterm-256color"},
        clock=clock,
    )
    assert sink.send("approval", "fix flaky test: approve a command") is True
    clock.now += 0.5
    assert sink.background("needs", "deploy needs your input") is None
    assert len(backend.notifications()) == 1
    # Later, or outranking: both are sent.
    clock.now += 5.0
    sink.background("needs", "deploy needs your input")
    assert len(backend.notifications()) == 2


def test_the_same_text_is_not_sent_twice_in_a_row() -> None:
    state, backend, clock = _state(), FakeBackend(), Clock()
    sink = NotificationSink(backend, state, env={"TERM": "xterm"}, clock=clock)  # type: ignore[arg-type]
    assert sink.send("done", "x: done in 12s") is True
    clock.now += 3.0
    assert sink.send("done", "x: done in 12s") is False
    clock.now += 20.0
    assert sink.send("done", "x: done in 12s") is True


def test_a_session_with_no_title_is_named_agenthicc() -> None:
    assert session_label(_state(title="")) == "agenthicc"


# ── AC 5: the title and the progress ──────────────────────────────────────────


def _presence(state: AppState, backend: FakeBackend, clock: Clock) -> TerminalPresence:
    presence = TerminalPresence(state, backend, clock=clock)  # type: ignore[arg-type]
    presence.start()
    return presence


def test_the_title_changes_to_the_working_glyph_and_is_restored_on_exit(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    presence = _presence(state, backend, clock)
    assert backend.written[0] == osc.TITLE_PUSH
    assert backend.titles()[-1] == "\x1b]2;● fix flaky test · agenthicc\x07"
    state.conversation.begin_turn("agent", "t1")
    state.conversation.frame.set(3)
    clock.now += 2.0
    presence.update()
    working = backend.titles()[-1]
    assert working.startswith("\x1b]2;❃ fix flaky test · agenthicc")
    state.pending_approval.set(_approval())
    clock.now += 2.0
    presence.update()
    assert backend.titles()[-1] == "\x1b]2;◆ fix flaky test · agenthicc\x07"
    presence.stop()
    assert backend.written[-1] == osc.TITLE_POP


def test_the_title_is_written_at_most_once_a_second(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    presence = _presence(state, backend, clock)
    state.conversation.begin_turn("agent", "t1")
    written = len(backend.titles())
    for frame in range(1, 6):
        state.conversation.frame.set(frame)
        clock.now += 0.1
        presence.update()
    assert len(backend.titles()) == written  # five ticks in half a second: none
    clock.now += 1.0
    presence.update()
    assert len(backend.titles()) == written + 1


def test_other_sessions_that_need_you_add_a_count_to_the_title(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    state.conversation.background_needs_you.set(2)
    _presence(state, backend, clock)
    assert backend.titles()[-1] == "\x1b]2;● fix flaky test · agenthicc (2)\x07"


def test_window_title_false_never_writes_the_title(clock: Clock) -> None:
    state, backend = _state(ui=UISettings(window_title=False)), FakeBackend()
    presence = _presence(state, backend, clock)
    state.conversation.begin_turn("agent", "t1")
    clock.now += 2.0
    presence.update()
    presence.stop()
    assert backend.titles() == []
    assert osc.TITLE_PUSH not in backend.written and osc.TITLE_POP not in backend.written


def test_progress_is_indeterminate_while_working_and_cleared_at_the_end(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    presence = _presence(state, backend, clock)
    assert backend.progress() == []
    state.conversation.begin_turn("agent", "t1")
    presence.update()
    assert backend.progress() == ["\x1b]9;4;3;0\x07"]
    clock.now += 1.0
    state.conversation.close_turn()
    presence.update()
    assert backend.progress() == ["\x1b]9;4;3;0\x07", _PROGRESS_OFF]


def test_a_failed_turn_shows_the_error_state_then_clears(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    presence = _presence(state, backend, clock)
    state.conversation.begin_turn("agent", "t1")
    presence.update()
    clock.now += 1.0
    state.conversation.close_turn(error="boom")
    presence.update()
    assert backend.progress()[-1] == "\x1b]9;4;2;100\x07"
    clock.now += 5.0
    presence.update()
    assert backend.progress()[-1] == _PROGRESS_OFF


def test_progress_is_not_written_where_the_terminal_does_not_draw_it(clock: Clock) -> None:
    state, backend = _state(), FakeBackend(progress=False)
    presence = _presence(state, backend, clock)
    state.conversation.begin_turn("agent", "t1")
    presence.update()
    presence.stop()
    assert backend.progress() == []


def test_stopping_clears_the_progress_a_running_turn_left(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    presence = _presence(state, backend, clock)
    state.conversation.begin_turn("agent", "t1")
    presence.update()
    presence.stop()
    assert backend.progress()[-1] == _PROGRESS_OFF
    assert backend.written[-1] == osc.TITLE_POP


def test_compaction_progress_is_determinate_only_when_it_is_measured(clock: Clock) -> None:
    """Never invented: no ``compaction_progress`` means the indeterminate state."""
    state, backend = _state(), FakeBackend()
    presence = _presence(state, backend, clock)
    conv = state.conversation
    conv.begin_compaction()
    presence.update()
    assert backend.progress() == ["\x1b]9;4;3;0\x07"]
    conv.report_compaction_progress(1, 4)
    presence.update()
    assert backend.progress()[-1] == "\x1b]9;4;1;25\x07"
    conv.report_compaction_progress(3, 4)
    presence.update()
    assert backend.progress()[-1] == "\x1b]9;4;1;75\x07"
    conv.end_compaction()
    presence.update()
    assert backend.progress()[-1] == _PROGRESS_OFF
    # A compaction that cannot say how far it is stays indeterminate throughout.
    conv.begin_compaction()
    presence.update()
    clock.now += 30.0
    presence.update()
    assert backend.progress()[-1] == "\x1b]9;4;3;0\x07"
    assert "\x1b]9;4;1;" not in "".join(backend.progress()[-2:])


def test_nothing_is_written_before_the_presence_starts_or_after_it_stops(clock: Clock) -> None:
    state, backend = _state(), FakeBackend()
    presence = TerminalPresence(state, backend, clock=clock)  # type: ignore[arg-type]
    presence.update()
    presence.stop()
    assert backend.written == []
    presence.start()
    presence.stop()
    after = list(backend.written)
    presence.update()
    presence.stop()
    assert backend.written == after
