"""E2E: TUX-09's transcript view, opened from a real composer and a real session log.

A ``MockTransport`` scripts a turn whose command fails; the agent-turn runner
executes the real ``run_bash`` tool and the conversation store persists to a real
session log. The composer is a real :class:`UnifiedInputSession`; the terminal
behind it is a fake that feeds keys and records every byte written, which
``pyte`` turns back into the screen the user would see.

NOTE: no ``from __future__ import annotations`` — the ``@tool()`` decorator
inspects annotations at decoration time.
"""

import asyncio
import base64
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime import CommandBus, OpenTranscriptCommand
from agenthicc.tui.runtime.mode_manager import build_default_registry
from agenthicc.tui.runtime.session_log import SessionEventLog
from agenthicc.tui.transcript.controller import TranscriptController
from agenthicc.tui.transcript.text import plain_text
from tests.transcript_support import (
    FakeBackend,
    FakeTerminal,
    keys,
    scheduler_session,
    use_sessions_dir,
    write_session,
)

pytestmark = pytest.mark.e2e

_OSC52 = re.compile(r"\x1b\]52;c;([A-Za-z0-9+/=]*)\x07")


def _completion(index, content="", calls=()):
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content=content,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    use_sessions_dir(monkeypatch, root)
    return root


@pytest.fixture
def app_state():
    state = TUIAppState.create()
    yolo = build_default_registry().get("Yolo")
    if yolo is not None:
        state.active_mode.set(yolo)
    return state


@pytest.fixture
async def processor(tmp_path):
    kernel_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=kernel_state, persist=False)
    task = asyncio.create_task(proc.run())
    yield proc
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _failing_turn(app_state, processor, session_id: str, command: str) -> None:
    """A scripted turn: the model runs *command*, which fails, then answers."""
    mock = MockTransport()
    mock.queue_response(
        _completion(
            0, calls=[ToolCall(tool_use_id="b1", name="run_bash", input={"command": command})]
        )
    )
    mock.queue_response(_completion(1, "The command failed."))
    log = SessionEventLog(session_id)
    app_state.conversation.on_event(log.append)
    app_state.conversation.append_event("user_message", {"text": "run the checks"})
    cfg = AgenthiccConfig()
    try:
        await _run_agent_turn(
            "run the checks",
            runner=AgentRunnerBase(transport=mock, signals=SignalBus()),
            processor=processor,
            session_memory=ShortTermMemory(max_tokens=cfg.execution.effective_usable_budget()),
            max_agent_turns=8,
            conv_store=app_state.conversation,
            app_state=app_state,
            exec_cfg=cfg.execution,
            skills=None,
            skill_permissions=cfg.agents.skill_permissions_for("auto"),
            mention_cache=MagicMock(),
            extra_tools=[],
            mcp_registry=None,
            active_agent="auto",
            completed_turns=0,
            approval_svc=None,
            output_collector=[],
            system_prompt_suffix="",
        )
    finally:
        log.close()


async def _until(
    condition: Callable[[], bool],
    what: str,
    timeout: float = 8.0,
    screen: Callable[[], str] = lambda: "",
) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}\n{screen()}"
        await asyncio.sleep(0.02)


class _Rig:
    """A composer, a fake terminal and a controller wired the way a session wires them."""

    def __init__(self, state: TUIAppState, session_id: str, monkeypatch, *, height: int = 70):
        self.state = state
        self.backend = FakeBackend()
        self.terminal = FakeTerminal(110, height)
        monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: self.backend)
        self.bus = CommandBus()
        self.input = UnifiedInputSession(state, self.bus)
        self.suspended = 0
        self.resumed = 0
        self.held = 0
        self.released = 0
        self.notices: list[tuple[str, str]] = []
        self.rewound: list[tuple[str, int]] = []
        self.tasks: list[asyncio.Task] = []
        self.log_path = Path("/nonexistent")
        self.controller = TranscriptController(
            session_id=session_id,
            console=self.terminal.console,
            state=state,
            handoff=self.input.run_terminal_handoff,
            spawn=lambda coroutine, name: self.tasks.append(asyncio.create_task(coroutine)),
            suspend=lambda: setattr(self, "suspended", self.suspended + 1),
            resume=lambda: setattr(self, "resumed", self.resumed + 1),
            hold=lambda: setattr(self, "held", self.held + 1),
            release=lambda: setattr(self, "released", self.released + 1),
            notify=lambda severity, text: self.notices.append((severity, text)),
            rewind=lambda text, turn: self.rewound.append((text, turn)),
            busy=lambda: False,
            backend=lambda: self.backend,
            log_path=lambda: self.log_path,
        )
        self.bus.register(OpenTranscriptCommand, lambda command: self.controller.open("tool"))
        self.run_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.run_task = asyncio.create_task(self.input.run())
        await _until(lambda: self.backend.raw_entered >= 1, "the composer to start")

    async def stop(self) -> None:
        self.input.request_exit()
        assert self.run_task is not None
        await asyncio.wait_for(self.run_task, 10)
        for task in self.tasks:
            await asyncio.wait_for(task, 10)

    def screen(self) -> str:
        return self.terminal.text()

    async def show(self, text: str) -> None:
        await _until(lambda: text in self.screen(), f"{text!r} on the screen", screen=self.screen)


# ── acceptance criterion 1: expand ────────────────────────────────────────────


async def test_ctrl_t_shows_the_failed_commands_full_output_and_q_returns_with_the_draft(
    tmp_path, monkeypatch, sessions, app_state, processor
):
    monkeypatch.chdir(tmp_path)
    await _failing_turn(
        app_state,
        processor,
        "s-e2e",
        'for i in $(seq 1 30); do echo "stored line $i"; done; exit 3',
    )
    app_state.conversation.session_id.set("s-e2e")
    rig = _Rig(app_state, "s-e2e", monkeypatch)
    await rig.start()
    try:
        rig.backend.feed(keys("a draft"), (Key.CTRL_T, ""))
        # The view opens on the failed command, selected and expanded: every stored
        # line is on the screen, and the call's arguments after them.
        await rig.show("Transcript")
        await rig.show("stored line 30")
        screen = rig.screen()
        assert all(f"stored line {i}" in screen for i in range(1, 31)), screen
        assert "exit 3" in screen
        assert "[expanded]" in screen
        assert rig.suspended == 1 and rig.held == 1  # the live block gave the screen up

        # Enter collapses the call to its failure tail, and expands it again.
        rig.backend.feed((Key.ENTER, ""))
        await rig.show("enter to expand")
        collapsed = rig.screen()
        assert "stored line 30" in collapsed
        assert not re.search(r"stored line 1\b", collapsed)
        rig.backend.feed((Key.ENTER, ""))
        await _until(lambda: re.search(r"stored line 1\b", rig.screen()) is not None, "expansion")

        # q returns to the composer exactly as it was left.
        rig.backend.feed((Key.CHAR, "q"))
        await _until(lambda: rig.resumed == 1 and rig.released == 1, "the view to close")
        assert "".join(app_state.input.buf()) == "a draft"
        assert not rig.terminal.console.is_alt_screen
        # The composer reads keys again: the draft continues where it stopped.
        rig.backend.feed(keys("!"))
        await _until(lambda: "".join(app_state.input.buf()) == "a draft!", "typing to resume")
    finally:
        await rig.stop()


async def test_the_failed_command_is_expanded_with_enter_from_the_history_view(
    tmp_path, monkeypatch, sessions, app_state, processor
):
    """``/history`` opens the same view with nothing expanded; Enter on the command expands it."""
    monkeypatch.chdir(tmp_path)
    await _failing_turn(
        app_state, processor, "s-hist", 'for i in $(seq 1 30); do echo "hist line $i"; done; exit 2'
    )
    app_state.conversation.session_id.set("s-hist")
    rig = _Rig(app_state, "s-hist", monkeypatch)
    await rig.start()
    try:
        rig.controller.open("history")
        await rig.show("Transcript")
        await rig.show("enter to expand")
        assert not re.search(r"hist line 1\b", rig.screen())
        rig.backend.feed((Key.CHAR, "{"), (Key.ENTER, ""))
        await _until(lambda: re.search(r"hist line 1\b", rig.screen()) is not None, "expansion")
        screen = rig.screen()
        assert all(f"hist line {i}" in screen for i in range(1, 31))
        rig.backend.feed((Key.ESC, ""))
        await _until(lambda: rig.resumed == 1, "the view to close")
    finally:
        await rig.stop()


# ── acceptance criterion 4: copy ──────────────────────────────────────────────


async def test_y_writes_an_osc52_sequence_holding_the_items_plain_text(
    tmp_path, monkeypatch, sessions, app_state
):
    monkeypatch.setenv("TERM", "xterm-256color")
    write_session(sessions, "s-copy", scheduler_session(5))
    app_state.conversation.session_id.set("s-copy")
    rig = _Rig(app_state, "s-copy", monkeypatch, height=40)
    await rig.start()
    try:
        rig.controller.open("history")
        await rig.show("Transcript")
        rig.backend.feed((Key.HOME, ""), (Key.CHAR, "y"))
        await _until(lambda: _OSC52.search(rig.terminal.raw) is not None, "the OSC 52 sequence")
        sequences = _OSC52.findall(rig.terminal.raw)
        assert len(sequences) == 1
        copied = base64.b64decode(sequences[0]).decode("utf-8")
        assert copied == "turn 1: look at the parser"
        assert "\x1b[" not in copied  # plain text: no SGR codes in the payload
        assert "copied 26 characters" in rig.screen()

        # A failed command: its plain text, and still no escape codes anywhere.
        rig.backend.feed((Key.CHAR, "}"), (Key.CHAR, "y"))
        await _until(lambda: len(_OSC52.findall(rig.terminal.raw)) == 2, "the second copy")
        second = base64.b64decode(_OSC52.findall(rig.terminal.raw)[1]).decode("utf-8")
        assert second.startswith("$ pytest tests/unit/test_scheduler.py -q")
        assert "output line 059" in second and "\x1b" not in second
        rig.backend.feed((Key.CHAR, "q"))
        await _until(lambda: rig.resumed == 1, "the view to close")
    finally:
        await rig.stop()


async def test_a_terminal_without_osc52_gets_the_system_clipboard_command(
    tmp_path, monkeypatch, sessions, app_state
):
    monkeypatch.setenv("TERM", "linux")
    ran: list[tuple[list[str], bytes]] = []
    monkeypatch.setattr(
        "agenthicc.tui.transcript.clipboard._run_clipboard",
        lambda command, data: ran.append((command, data)) or True,
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/xclip" if name == "xclip" else None)
    write_session(sessions, "s-sys", scheduler_session(2))
    app_state.conversation.session_id.set("s-sys")
    rig = _Rig(app_state, "s-sys", monkeypatch, height=40)
    await rig.start()
    try:
        rig.controller.open("history")
        await rig.show("Transcript")
        rig.backend.feed((Key.HOME, ""), (Key.CHAR, "y"))
        await _until(lambda: bool(ran), "the clipboard command")
        assert ran[0][0][0] == "xclip" and ran[0][1] == b"turn 1: look at the parser"
        assert _OSC52.search(rig.terminal.raw) is None
        await rig.show("system clipboard")
        rig.backend.feed((Key.CHAR, "q"))
        await _until(lambda: rig.resumed == 1, "the view to close")
    finally:
        await rig.stop()


# ── the pager ─────────────────────────────────────────────────────────────────


async def test_e_leaves_the_alternate_screen_for_the_pager_and_comes_back(
    tmp_path, monkeypatch, sessions, app_state
):
    from agenthicc.tui.transcript.host import TranscriptHost
    from agenthicc.tui.transcript.sources import SessionLogSource
    from agenthicc.tui.transcript.view import TranscriptView
    from agenthicc.tui.keymap import Keymap
    from agenthicc.tui.actions import DEFAULT_ACTIONS

    write_session(sessions, "s-pager", scheduler_session(3))
    source = SessionLogSource("s-pager")
    assert source.wait_ready(5)
    backend = FakeBackend()
    terminal = FakeTerminal(100, 30)
    shown: list[tuple[str, bool]] = []

    def pager(text: str) -> str:
        shown.append((text, terminal.console.is_alt_screen))
        return ""

    view = TranscriptView(source, Keymap.build(DEFAULT_ACTIONS))
    host = TranscriptHost(view, terminal.console, backend, pager=pager)
    backend.feed((Key.HOME, ""), (Key.CHAR, "e"))
    task = asyncio.create_task(host.run())
    await _until(lambda: bool(shown), "the pager")
    backend.feed((Key.CHAR, "q"))
    result = await asyncio.wait_for(task, 10)
    assert result.kind == "closed"
    assert shown == [("turn 1: look at the parser", False)]  # the normal screen, for the pager
    assert terminal.raw.count("\x1b[?1049h") == 2  # in, back in after the pager
    assert not terminal.console.is_alt_screen


# ── acceptance criterion 7: /rewind through the dialog ───────────────────────


async def test_rewind_offers_the_dialog_and_choosing_edit_forks_at_that_turn(
    tmp_path, monkeypatch, sessions, app_state
):
    write_session(sessions, "s-rew", scheduler_session(5))
    app_state.conversation.session_id.set("s-rew")
    rig = _Rig(app_state, "s-rew", monkeypatch, height=40)
    await rig.start()
    try:
        rig.controller.open("rewind")
        await rig.show("Transcript")
        rig.backend.feed((Key.HOME, ""), (Key.CHAR, "]"), (Key.CHAR, "]"), (Key.ENTER, ""))
        await rig.show("Rewind to turn 3?")
        screen = rig.screen()
        assert "Edit and resend from here" in screen and "Copy the prompt" in screen
        assert "Files are not restored" in screen and "this session is kept" in screen
        # Esc returns to the view, not to the composer.
        rig.backend.feed((Key.ESC, ""))
        await _until(lambda: "Rewind to turn 3?" not in rig.screen(), "the dialog to close")
        assert rig.resumed == 0
        rig.backend.feed((Key.ENTER, ""))
        await rig.show("Rewind to turn 3?")
        rig.backend.feed((Key.CHAR, "1"))
        await _until(lambda: rig.resumed == 1, "the view to close")
        assert rig.rewound == [("turn 3: look at the scheduler", 3)]
    finally:
        await rig.stop()


async def test_rewind_copy_the_prompt_stays_in_the_view(tmp_path, monkeypatch, sessions, app_state):
    monkeypatch.setenv("TERM", "xterm-256color")
    write_session(sessions, "s-rew2", scheduler_session(4))
    app_state.conversation.session_id.set("s-rew2")
    rig = _Rig(app_state, "s-rew2", monkeypatch, height=40)
    await rig.start()
    try:
        rig.controller.open("rewind")
        await rig.show("Transcript")
        rig.backend.feed((Key.HOME, ""), (Key.CHAR, "]"), (Key.ENTER, ""), (Key.CHAR, "2"))
        await _until(lambda: _OSC52.search(rig.terminal.raw) is not None, "the copy")
        copied = base64.b64decode(_OSC52.findall(rig.terminal.raw)[0]).decode()
        assert copied == "turn 2: look at the parser"
        assert rig.rewound == [] and rig.resumed == 0
        rig.backend.feed((Key.CHAR, "q"))
        await _until(lambda: rig.resumed == 1, "the view to close")
    finally:
        await rig.stop()


# ── /logs and the approval's details use the same view ───────────────────────


async def test_logs_open_the_diagnostics_log_in_the_same_view(
    tmp_path, monkeypatch, sessions, app_state
):
    log = tmp_path / "diag.log"
    log.write_text("starting up\nWARNING something odd\nplain\nERROR it broke\n")
    rig = _Rig(app_state, "any", monkeypatch, height=30)
    rig.log_path = log
    await rig.start()
    try:
        rig.controller.open("logs")
        await rig.show("it broke")
        screen = rig.screen()
        assert "Log" in screen and str(log) in screen and "turns" not in screen
        rig.backend.feed((Key.CHAR, "/"), *keys("odd"), (Key.ENTER, ""))
        await rig.show("hit 1 of 1")
        rig.backend.feed((Key.CHAR, "q"))
        await _until(lambda: rig.resumed == 1, "the view to close")

        rig.log_path = tmp_path / "missing.log"
        rig.controller.open("logs")
        await _until(lambda: ("info", "The diagnostics log is empty.") in rig.notices, "a notice")
    finally:
        await rig.stop()


async def test_an_approvals_full_details_open_in_the_view_and_esc_returns(
    tmp_path, monkeypatch, sessions, app_state
):
    from rich.text import Text

    rig = _Rig(app_state, "any", monkeypatch, height=30)
    await rig.start()
    try:
        rig.controller.open_document(
            "Edit a file", [Text("--- a/app.py\n+++ b/app.py\n-    return 1\n+    return 2")]
        )
        await rig.show("+    return 2")
        assert "Edit a file" in rig.screen() and "--- a/app.py" in rig.screen()
        rig.backend.feed((Key.ESC, ""))
        await _until(lambda: rig.resumed == 1, "the view to close")
    finally:
        await rig.stop()


async def test_without_a_terminal_the_view_says_so_instead_of_opening(
    tmp_path, monkeypatch, sessions, app_state
):
    rig = _Rig(app_state, "any", monkeypatch)
    rig.backend._interactive = False
    rig.controller.open("history")
    await asyncio.gather(*rig.tasks)
    assert rig.notices == [("warning", "The transcript view needs a terminal.")]
    assert rig.suspended == 0


# ── acceptance criterion 5 ────────────────────────────────────────────────────


def test_the_old_history_overlay_and_log_pager_no_longer_exist():
    src = Path(__file__).resolve().parents[2] / "src"
    sources = [path for path in src.rglob("*.py")]
    offenders = [
        str(path.relative_to(src))
        for path in sources
        if any(
            name in path.read_text(encoding="utf-8")
            for name in ("TranscriptHistoryOverlay", "LogTailOverlay")
        )
    ]
    assert offenders == []
    assert not (src / "agenthicc/tui/workspace/overlays/transcript_history.py").exists()
    assert not (src / "agenthicc/tui/workspace/overlays/log_tail.py").exists()


def test_plain_text_matches_what_the_scrollback_prints_for_a_tool():
    payload = {"name": "run_bash", "summary": "make test", "output_lines": ["boom"]}
    assert plain_text("tool_complete", payload) == "$ make test\nboom"
    assert json.dumps(payload)  # (the payload itself stays JSON-serializable)
