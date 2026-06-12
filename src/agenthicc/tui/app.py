"""TUI application — InlineRenderer (PRD-09) + legacy headless/ANSI renderer.

The preferred entry point is :func:`run_inline`, which renders agent output
directly into the normal terminal scroll buffer using :mod:`rich` and reads
user input via a :class:`~prompt_toolkit.shortcuts.PromptSession`.

:func:`render_frame_ansi` and :func:`run_headless` are unchanged for
backward-compatibility with pyte E2E tests and headless CI pipelines.
:func:`build_app` is deprecated and now raises :class:`RuntimeError`.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, TextIO

from .transcript import TranscriptModel, ToolCallState

__all__ = [
    "INPUT_PROMPT",
    "InlineRenderer",
    "MENU_COMMANDS",
    "PROMPT_TOOLKIT_AVAILABLE",
    "RICH_AVAILABLE",
    "SlashCommandHandler",
    "build_app",
    "detect_slash_command",
    "render_frame_ansi",
    "run_headless",
    "run_inline",
]

# ── optional-dependency guards ────────────────────────────────────────────

try:  # pragma: no cover - exercised implicitly by import
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import (
        ConditionalContainer,
        Float,
        FloatContainer,
        HSplit,
        Layout,
        Window,
    )
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

    PROMPT_TOOLKIT_AVAILABLE = True
except Exception:  # pragma: no cover
    PROMPT_TOOLKIT_AVAILABLE = False

try:
    from rich.console import Console, Group as RichGroup
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    from rich import box as rich_box

    RICH_AVAILABLE = True
except Exception:  # pragma: no cover
    RICH_AVAILABLE = False

# ── constants ─────────────────────────────────────────────────────────────

INPUT_PROMPT = "> "

#: Slash commands that open a menu overlay.
MENU_COMMANDS = {
    "/status": "status",
    "/history": "history",
}

#: Help text for the /help slash command.
SLASH_HELP = {
    "/status": "Show active agent turn table (agent_id, name, cost, tokens)",
    "/history": "Print the last 20 lines of the transcript scroll buffer",
    "/help": "Show this help table",
}


def detect_slash_command(text: str) -> str | None:
    """Return the menu name for *text* if it is a menu slash command."""
    return MENU_COMMANDS.get(text.strip())


# ── InlineRenderer ────────────────────────────────────────────────────────


class InlineRenderer:
    """Renders agenthicc session output directly into the terminal scroll buffer.

    Uses rich for styled output and a prompt_toolkit PromptSession for the
    input bar so readline editing (history, Ctrl-A/E, arrow keys) works.
    """

    def __init__(
        self,
        model: TranscriptModel,
        adapter: Any | None = None,
        console: Any | None = None,
        base_path: str = ".",
        history_file: str | None = None,
    ) -> None:
        self.model = model
        self.adapter = adapter
        if RICH_AVAILABLE:
            # force_terminal=True: Rich detects patch_stdout()'s wrapped stdout as
            # non-terminal and strips ANSI codes; this forces proper escape sequences.
            self.console = console or Console(
                highlight=False, markup=False, force_terminal=True
            )
        else:  # pragma: no cover
            self.console = console
        self._printed_count: int = 0
        self._live: Any | None = None
        self._base_path = base_path
        self._history_file = history_file
        # processor ref for interrupt/cancel; set externally or via adapter
        self._processor = getattr(adapter, "_processor", None) if adapter else None

    # ── main loop ─────────────────────────────────────────────────────────

    async def run(self, on_input: Callable[[str], None]) -> None:
        """Start the render loop and prompt until Ctrl-C / EOF."""
        from prompt_toolkit.patch_stdout import patch_stdout
        # raw=True: pass bytes through the stdout proxy unchanged so Rich's
        # ANSI escape sequences (\x1b[…) are not mangled into "?[…".

        # Use InputBarSession (slash-command + @-mention completers, Meta+Enter multi-line)
        # when available; fall back to plain PromptSession otherwise.
        try:
            from agenthicc.tui.input_bar import InputBarSession
            session = InputBarSession(
                base_path=self._base_path,
                history_file=self._history_file,
            )
        except ImportError:
            from prompt_toolkit import PromptSession
            session = PromptSession(INPUT_PROMPT)  # type: ignore[assignment]

        render_task: asyncio.Task | None = None
        _running_intent: list[bool] = [False]

        with patch_stdout(raw=True):
            render_task = asyncio.create_task(self._render_loop())
            try:
                while True:
                    try:
                        text = await session.prompt_async()
                    except EOFError:
                        break
                    except KeyboardInterrupt:
                        # Cancel running intent rather than exiting when work is in flight
                        if _running_intent[0] and self._processor is not None:
                            from agenthicc.kernel import Event
                            await self._processor.emit(Event.create("IntentCancelled", {}))
                            self.console.print("[dim]intent cancelled[/dim]", markup=True)
                            _running_intent[0] = False
                            continue
                        break
                    text = text.strip()
                    if not text:
                        continue
                    handled = SlashCommandHandler().handle(text, self.model, self.console)
                    if not handled:
                        _running_intent[0] = True
                        on_input(text)
                        _running_intent[0] = False
            finally:
                if render_task is not None:
                    render_task.cancel()
                    await asyncio.gather(render_task, return_exceptions=True)
                if self._live is not None:
                    self._live.stop()
                    self._live = None

    # ── render loop ───────────────────────────────────────────────────────

    async def _render_loop(self) -> None:
        """Background task: print new lines and update spinner every 50 ms."""
        while True:
            await asyncio.sleep(0.05)
            if self.adapter is not None:
                self.adapter.sync()
            self._flush_new_lines()
            self._update_spinner()
            self.model.advance_spinner()

    def _flush_new_lines(self) -> None:
        """Print lines from model.render() not yet printed."""
        lines = self.model.render()
        new = lines[self._printed_count:]
        for line in new:
            self.console.print(line, markup=False, highlight=False)
        if new:
            self._printed_count = len(lines)

    def _update_spinner(self) -> None:
        """Start / update / stop the spinner Live block."""
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        panel = self._build_spinner_panel()
        if panel is None:
            if self._live is not None:
                self._live.stop()
                self._live = None
        else:
            if self._live is None:
                self._live = Live(
                    panel,
                    console=self.console,
                    refresh_per_second=12,
                    transient=True,
                )
                self._live.start()
            else:
                self._live.update(panel)

    def _build_spinner_panel(self) -> Any | None:
        """Return a Panel containing one Spinner per running tool, or None."""
        if not RICH_AVAILABLE:  # pragma: no cover
            return None
        running = [
            tc
            for turn in self.model.turns
            for tc in turn.tool_calls
            if tc.state == ToolCallState.RUNNING
        ]
        if not running:
            return None
        rows = [Spinner("dots", text=f"  [tool] {tc.name}") for tc in running]
        return Panel(RichGroup(*rows), border_style="dim", padding=(0, 1))

    def has_running_tools(self) -> bool:
        """Return True if any tool call is currently in the RUNNING state."""
        return self._build_spinner_panel() is not None


# ── SlashCommandHandler ───────────────────────────────────────────────────


class SlashCommandHandler:
    """Renders slash-command output as Rich Panels/Tables inline."""

    def handle(self, text: str, model: TranscriptModel, console: Any) -> bool:
        """Dispatch *text* to a slash-command renderer.  Returns True if handled."""
        cmd = text.strip()
        if cmd == "/status":
            self._status(model, console)
            return True
        if cmd == "/history":
            self._history(model, console)
            return True
        if cmd == "/help":
            self._help(console)
            return True
        return False

    def _status(self, model: TranscriptModel, console: Any) -> None:
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        table = Table(title="Agent Status", box=rich_box.SIMPLE)
        table.add_column("Agent ID", style="cyan")
        table.add_column("Name")
        table.add_column("Cost")
        table.add_column("Tokens", justify="right")
        for turn in model.turns:
            table.add_row(
                turn.agent_id[:8],
                turn.agent_name,
                f"${turn.cost_usd:.4f}" if turn.cost_usd is not None else "$0.0000",
                str(turn.tokens) if turn.tokens is not None else "0",
            )
        if not model.turns:
            table.add_row("—", "(no active agents)", "", "")
        console.print(table)

    def _history(self, model: TranscriptModel, console: Any) -> None:
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        lines = model.render()[-20:]
        console.print(
            Panel(
                "\n".join(lines) or "(empty)",
                title="/history — last 20 lines",
            )
        )

    def _help(self, console: Any) -> None:
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        table = Table(title="Slash Commands", box=rich_box.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Description")
        for cmd, desc in SLASH_HELP.items():
            table.add_row(cmd, desc)
        console.print(table)


# ── run_inline ────────────────────────────────────────────────────────────


async def run_inline(
    model: TranscriptModel,
    adapter: Any | None = None,
    on_input: Callable[[str], None] | None = None,
) -> None:
    """Convenience wrapper: create InlineRenderer and run.

    Renders agent output directly into the terminal scroll buffer (no alternate
    screen), with a prompt_toolkit PromptSession for the input bar.
    """
    renderer = InlineRenderer(model, adapter)
    await renderer.run(on_input or (lambda _: None))


# ── deprecated build_app ──────────────────────────────────────────────────


def build_app(model: TranscriptModel, on_input: Callable[[str], None]) -> Any:
    """Deprecated. Use :func:`run_inline` instead.

    .. deprecated::
        ``build_app()`` returned a full-screen prompt_toolkit Application.
        The new entry point is the inline scroll-buffer renderer.
    """
    raise RuntimeError(
        "build_app() is deprecated. Use run_inline() instead.\n"
        "\n"
        "Migration:\n"
        "  Before: app = build_app(model, on_input); await app.run_async()\n"
        "  After:  await run_inline(model, adapter, on_input=on_input)\n"
    )


# ── headless mode ────────────────────────────────────────────────────────


async def run_headless(event_queue: asyncio.Queue, output_stream: TextIO) -> None:
    """Emit one JSON line per kernel event instead of rendering a TUI.

    Stops when a ``None`` sentinel is read from *event_queue*.
    """
    while True:
        event = await event_queue.get()
        if event is None:
            break
        record = {
            "ts": getattr(event, "timestamp", time.time()),
            "event_type": getattr(event, "event_type", type(event).__name__),
            "event_id": getattr(event, "event_id", None),
            "payload": getattr(event, "payload", {}),
            "source_agent_id": getattr(event, "source_agent_id", None),
        }
        output_stream.write(json.dumps(record, default=str) + "\n")
        output_stream.flush()


# ── offline ANSI frame renderer (used by the pyte e2e tests) ─────────────


def render_frame_ansi(
    model: TranscriptModel,
    cols: int,
    rows: int,
    input_text: str = "",
    menu_lines: list[str] | None = None,
) -> str:
    """Compose a full ANSI frame the way the prompt_toolkit layout does.

    Row layout (1-indexed ANSI rows):
      rows 1 .. rows-2   transcript (auto-scrolled to the tail)
      row  rows-1        status line
      row  rows          input bar  (ALWAYS the last row)

    A menu overlay, when present, is painted over the transcript region
    anchored 2 rows above the terminal bottom — i.e. its last row sits just
    above the status line and it never touches the input bar.
    """
    transcript_rows = max(rows - 2, 0)
    lines = model.render()
    visible = lines[-transcript_rows:] if transcript_rows else []

    def clip(text: str) -> str:
        return text[:cols]

    buf: list[str] = ["\x1b[2J\x1b[H"]  # clear screen, home cursor
    for i, line in enumerate(visible):
        buf.append(f"\x1b[{i + 1};1H{clip(line)}")

    # menu overlay floats above the status line (bottom=2 anchor)
    if menu_lines:
        overlay_end = rows - 2  # last overlay row (1-indexed)
        overlay_start = max(overlay_end - len(menu_lines) + 1, 1)
        for i, line in enumerate(menu_lines[: overlay_end - overlay_start + 1]):
            buf.append(f"\x1b[{overlay_start + i};1H{clip(line)}")

    agents = len({t.agent_id for t in model.turns})
    status = (
        f" {agents} agents | ${model.total_cost_usd:.3f}"
        f" | {model.total_tokens:,} tok"
    )
    buf.append(f"\x1b[{rows - 1};1H{clip(status)}")
    buf.append(f"\x1b[{rows};1H{clip(INPUT_PROMPT + input_text)}")
    return "".join(buf)
