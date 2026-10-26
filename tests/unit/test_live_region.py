"""The live block is written by line diff, equal to a full redraw (AGX-11)."""

from __future__ import annotations

import asyncio
import random
import sys
from io import StringIO

import pyte
import pytest
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.terminal.capabilities import capabilities
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.live_region import Line, LiveRegion, lines_text
from tests.tui_frames import settle_frames

pytestmark = pytest.mark.unit


class _Terminal:
    """A console whose output is fed to a virtual terminal."""

    def __init__(self, columns: int, rows: int) -> None:
        self.output = StringIO()
        self.console = Console(
            file=self.output,
            force_terminal=True,
            width=columns,
            height=rows,
            color_system=None,
        )
        self.screen = pyte.Screen(columns, rows)
        self.stream = pyte.Stream(self.screen)

    def flush(self) -> str:
        data = self.output.getvalue()
        self.output.seek(0)
        self.output.truncate(0)
        # A terminal's output processing turns LF into CRLF (onlcr).
        self.stream.feed(data.replace("\n", "\r\n"))
        return data

    def block(self, height: int) -> list[str]:
        """The *height* rows ending at the cursor's row."""
        bottom = self.screen.cursor.y
        return [row.rstrip() for row in self.screen.display[bottom - height + 1 : bottom + 1]]


def _line(text: str) -> Line:
    return (Segment(text),) if text else ()


def _frames(rng: random.Random, columns: int, rows: int) -> list[list[str] | str]:
    """A random script of frames (lists of rows) and scroll-back prints (str)."""
    alphabet = "abcdefgh  ─│▌❯"
    height = rng.randint(1, rows - 2)
    current = ["".join(rng.choices(alphabet, k=rng.randint(0, columns))) for _ in range(height)]
    script: list[list[str] | str] = [list(current)]
    for step in range(rng.randint(5, 25)):
        roll = rng.random()
        if roll < 0.15:
            script.append(f"history {step} " + "x" * rng.randint(0, columns - 12))
            continue
        if roll < 0.3:
            height = rng.randint(1, rows - 2)
            current = (current + [""] * height)[:height]
        for _ in range(rng.randint(1, 3)):
            row = rng.randrange(len(current))
            current[row] = "".join(rng.choices(alphabet, k=rng.randint(0, columns)))
        script.append(list(current))
    return script


def _play(script: list[list[str] | str], columns: int, rows: int, ratio: float) -> list[str]:
    terminal = _Terminal(columns, rows)
    region = LiveRegion(terminal.console, full_redraw_ratio=ratio)
    region.start()
    try:
        for item in script:
            if isinstance(item, str):
                terminal.console.print(item)
                terminal.flush()
                continue
            region.update([_line(text) for text in item])
            terminal.flush()
            # The block always ends on the cursor's row and shows the frame.
            assert terminal.block(len(item)) == [text.rstrip() for text in item]
        return [row.rstrip() for row in terminal.screen.display]
    finally:
        region.stop()


@pytest.mark.parametrize("seed", range(60))
def test_diffed_frames_leave_the_same_screen_as_full_redraws(seed: int) -> None:
    rng = random.Random(seed)
    columns, rows = rng.randint(12, 40), rng.randint(6, 18)
    script = _frames(rng, columns, rows)
    # A ratio below zero makes every frame a full redraw.
    assert _play(script, columns, rows, 0.5) == _play(script, columns, rows, -1.0)


def test_an_unchanged_frame_writes_nothing_and_one_change_rewrites_one_row() -> None:
    terminal = _Terminal(40, 10)
    region = LiveRegion(terminal.console)
    region.start()
    try:
        rows = [f"row {index}" for index in range(6)]
        region.update([_line(text) for text in rows])
        terminal.flush()
        region.update([_line(text) for text in rows])
        assert terminal.flush() == ""

        rows[2] = "row 2 changed"
        region.update([_line(text) for text in rows])
        written = terminal.flush()
        assert "row 2 changed" in written
        assert "row 1" not in written and "row 3" not in written
        assert region.stats.frames == 2
        assert region.stats.full_redraws == 1
    finally:
        region.stop()


def test_a_transient_block_is_cleared_on_stop() -> None:
    terminal = _Terminal(30, 8)
    region = LiveRegion(terminal.console, transient=True)
    region.start()
    terminal.console.print("kept above")
    region.update([_line("LIVE A"), _line("LIVE B")])
    terminal.flush()
    region.stop()
    terminal.flush()
    display = "\n".join(terminal.screen.display)
    assert "kept above" in display
    assert "LIVE" not in display


def test_print_to_stdout_while_live_goes_above_the_block() -> None:
    terminal = _Terminal(30, 8)
    region = LiveRegion(terminal.console)
    region.start()
    try:
        region.update([_line("LIVE")])
        terminal.flush()
        print("from print()")
        terminal.flush()
        assert terminal.block(1) == ["LIVE"]
        display = [row.rstrip() for row in terminal.screen.display]
        assert display.index("from print()") < display.index("LIVE")
    finally:
        region.stop()
    assert not isinstance(sys.stdout, type(None))


def test_frames_are_bracketed_only_when_the_terminal_supports_it() -> None:
    capabilities.reset()
    try:
        terminal = _Terminal(30, 8)
        region = LiveRegion(
            terminal.console, synchronized_output=lambda: capabilities.synchronized_output
        )
        region.start()
        region.update([_line("one")])
        assert "\x1b[?2026" not in terminal.flush()

        capabilities.report_mode(2026, 2)
        region.update([_line("two")])
        written = terminal.flush()
        assert written.startswith("\x1b[?2026h") and written.endswith("\x1b[?2026l")
        terminal.console.print("history")
        written = terminal.flush()
        assert written.startswith("\x1b[?2026h") and written.endswith("\x1b[?2026l")
        region.stop()
    finally:
        capabilities.reset()


# ── the workspace on top of the region ───────────────────────────────────────


def _workspace(width: int = 120) -> tuple[AppState, Workspace, StringIO]:
    state = AppState.create()
    state.conversation.model_name.set("anthropic/claude-sonnet-5-5")
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=width, height=40)
    return state, Workspace(state, console), output


async def test_a_spinner_frame_rewrites_only_the_status_row() -> None:
    state, workspace, output = _workspace()
    workspace.start()
    try:
        state.conversation.begin_turn("agent", "turn-1")
        await settle_frames(workspace)
        output.seek(0)
        output.truncate(0)

        state.conversation.tick()
        await settle_frames(workspace)

        written = output.getvalue()
        assert 0 < len(written.encode()) <= 200
        # One row rewritten: a single erase-line, no other block rows.
        assert written.count("\x1b[2K") == 1
        assert "claude-sonnet-5-5" not in written
    finally:
        workspace.stop()


async def test_typing_echoes_without_waiting_for_the_frame_interval() -> None:
    state, workspace, output = _workspace()
    workspace.frames.interval_s = 10.0
    workspace.start()
    try:
        state.conversation.notification.set("a notification")  # waits for the interval
        state.input.buf.set(list("hi"))
        state.input.cursor.set(2)
        await asyncio.sleep(0)
        assert any("hi▌" in row for row in lines_text(workspace._live.lines))  # type: ignore[union-attr]
    finally:
        workspace.stop()


def test_the_footer_parses_its_markup_only_when_a_row_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, workspace, _output = _workspace()
    parsed: list[str] = []
    original = Text.from_markup

    def counting(text: str, **kwargs: object) -> Text:
        parsed.append(text)
        return original(text, **kwargs)  # type: ignore[arg-type]

    workspace._build_lines()
    monkeypatch.setattr(Text, "from_markup", staticmethod(counting))
    workspace._build_lines()
    workspace._build_lines()
    assert parsed == []

    state.conversation.notification.set("Something happened")
    workspace._build_lines()
    assert len(parsed) == 1 and "Something happened" in parsed[0]


def test_status_and_footer_rows_are_styled_segments() -> None:
    _state, workspace, _output = _workspace(width=60)
    lines = workspace._build_lines()
    assert all(isinstance(segment, Segment) for line in lines for segment in line)
    assert any(isinstance(segment.style, Style) for line in lines for segment in line)
    # Every row fits the width, so the terminal never wraps one.
    assert all(sum(len(segment.text) for segment in line) <= 60 for line in lines)


def test_the_perf_command_reports_frames_and_synchronized_output() -> None:
    from agenthicc.commands.builtins import _cmd_perf
    from agenthicc.commands.command import CommandContext
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.workspace.frames import FrameStats

    stats = FrameStats()
    stats.record_render(0.002)
    stats.record(0.001, 54, full=False)
    stats.record(0.003, 900, full=True)
    console = Console(record=True, width=120)
    context = CommandContext(
        text="/perf",
        args="",
        model="test/model",
        console=console,
        config=AgenthiccConfig(),
        frame_stats=lambda: stats,
    )
    capabilities.reset()
    try:
        assert _cmd_perf(context)
    finally:
        capabilities.reset()
    lines = console.export_text().splitlines()
    assert lines[0] == "Frames drawn: 2 (1 full redraws)"
    assert lines[1].startswith("Render: p50 2.00 ms")
    assert lines[-1] == "Synchronized output: off"

    unavailable = Console(record=True, width=120)
    assert _cmd_perf(
        CommandContext(
            text="/perf", args="", model="m", console=unavailable, config=AgenthiccConfig()
        )
    )
    assert "unavailable" in unavailable.export_text()
