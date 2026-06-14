"""TUI application — InlineRenderer + AgenthiccApp (Textual inline) (PRDs)."""
from __future__ import annotations

import asyncio
import io
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from .transcript import TranscriptModel, ToolCallState
from .symbols import SPINNER_FRAMES

__all__ = [
    "INPUT_PROMPT",
    "InlineRenderer",
    "MENU_COMMANDS",
    "PROMPT_TOOLKIT_AVAILABLE",
    "RICH_AVAILABLE",
    "SlashCommandHandler",
    "StatusState",
    "build_app",
    "detect_slash_command",
    "render_frame_ansi",
    "run_headless",
    "run_inline",
]

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

PROMPT_TOOLKIT_AVAILABLE = True

INPUT_PROMPT = "> "

MENU_COMMANDS = {
    "/status": "status",
    "/history": "history",
}

SLASH_HELP = {
    "/status": "Show active agent turn table",
    "/history": "Print the last 20 lines of the transcript",
    "/help": "Show this help table",
}


def detect_slash_command(text: str) -> str | None:
    return MENU_COMMANDS.get(text.strip())


@dataclass
class StatusState:
    """Mutable state for the Status Bar (PRD-20)."""
    active: bool = False
    spinner_frame: int = 0
    intent_started_at: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    session_cost_usd: float = 0.0
    completed_agents: int = 0
    session_id: str = ""


def _thinking_wave(frame_num: int) -> str:
    text = "Thinking..."
    length = len(text)
    cycle = 2 * (length - 1)
    pos = frame_num % cycle
    if pos >= length:
        pos = cycle - pos
    result = []
    for i, ch in enumerate(text):
        if i == pos:
            result.append(f"\x1b[1m{ch}\x1b[22m")
        else:
            result.append(ch)
    return "".join(result)


class InlineRenderer:
    """Renders agenthicc output directly into the terminal scroll buffer."""

    # Sentinel prefix used by __main__.py to tag Markdown content lines
    # (also exported from transcript._MD_SENTINEL — keep in sync).
    _MD_SENTINEL: str = "\x00MD\x00"

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
            self.console = console or Console(
                highlight=False, markup=False, force_terminal=True
            )
        else:  # pragma: no cover
            self.console = console
        self._printed_count: int = 0
        self._live: Any | None = None
        self._base_path = base_path
        self._history_file = history_file
        self._processor = getattr(adapter, "_processor", None) if adapter else None
        self._status = StatusState()

    # ── Status bar lifecycle ──────────────────────────────────────────────

    def on_intent_submitted(self) -> None:
        self._status.active = True
        self._status.intent_started_at = time.monotonic()
        self._status.input_tokens = 0
        self._status.output_tokens = 0

    def on_model_call_complete(self, input_tokens: int, output_tokens: int, cost_usd: float = 0.0) -> None:
        self._status.input_tokens += input_tokens
        self._status.output_tokens += output_tokens
        self._status.session_cost_usd += cost_usd

    def on_agent_run_complete(self) -> None:
        self._status.completed_agents += 1
        if not self.has_running_tools():
            self._status.active = False

    def _render_status_panel(self) -> Any:
        if not RICH_AVAILABLE:  # pragma: no cover
            return None
        s = self._status
        if s.active:
            elapsed = time.monotonic() - s.intent_started_at if s.intent_started_at else 0.0
            frame = SPINNER_FRAMES[s.spinner_frame % len(SPINNER_FRAMES)]
            thinking = _thinking_wave(s.spinner_frame)
            text = Text.assemble(
                (frame + " ", "bold"),
                (thinking + "  ", ""),
                (f"{elapsed:.1f}s", "dim"),
                ("  │  ", "dim"),
                ("↑ ", "dim"),
                (f"{s.input_tokens:,} tok", "cyan"),
                ("  ↓ ", "dim"),
                (f"{s.output_tokens:,} tok", "green"),
            )
        else:
            cost = f"${s.session_cost_usd:.3f}"
            sid = s.session_id[:12] if s.session_id else "session"
            completed = s.completed_agents
            ss = "s" if completed != 1 else ""
            text = Text.assemble(
                (f" {sid}", "dim"),
                ("  │  ", "dim"),
                (f"{completed} agent{ss} completed", "dim"),
                ("  │  ", "dim"),
                (cost, "dim"),
            )
        return Panel(text, box=rich_box.DOUBLE, padding=(0, 1), style="dim")

    def _render_input_panel(self, input_text: str = "") -> Any:
        if not RICH_AVAILABLE:  # pragma: no cover
            return None
        content = Text(INPUT_PROMPT + input_text + "▌", style="bold white")
        return Panel(content, box=rich_box.DOUBLE, padding=(0, 1))

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self, on_input: Callable[[str], None]) -> None:
        from prompt_toolkit.patch_stdout import patch_stdout  # noqa: PLC0415
        try:
            from agenthicc.tui.input_bar import CommandRegistry  # noqa: PLC0415
            from prompt_toolkit import PromptSession  # noqa: PLC0415
            session = PromptSession(INPUT_PROMPT)
        except ImportError:
            from prompt_toolkit import PromptSession  # noqa: PLC0415
            session = PromptSession(INPUT_PROMPT)

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
                        if _running_intent[0] and self._processor is not None:
                            from agenthicc.kernel import Event  # noqa: PLC0415
                            await self._processor.emit(Event.create("IntentCancelled", {}))
                            if self.console:
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
                        self.on_intent_submitted()
                        result = on_input(text)
                        if asyncio.iscoroutine(result):
                            await result
                        _running_intent[0] = False
            finally:
                if render_task is not None:
                    render_task.cancel()
                    await asyncio.gather(render_task, return_exceptions=True)
                if self._live is not None:
                    self._live.stop()
                    self._live = None

    async def _render_loop(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            if self.adapter is not None:
                self.adapter.sync()
            self._flush_new_lines()
            self._update_spinner()
            self.model.advance_spinner()
            self._status.spinner_frame += 1

    def _flush_new_lines(self) -> None:
        lines = self.model.render()
        new = lines[self._printed_count:]
        for line in new:
            if self.console:
                self.console.print(line, markup=False, highlight=False)
        if new:
            self._printed_count = len(lines)

    def _update_spinner(self) -> None:
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
                    panel, console=self.console,
                    refresh_per_second=12, transient=True,
                )
                self._live.start()
            else:
                self._live.update(panel)

    def _build_spinner_panel(self) -> Any | None:
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
        return self._build_spinner_panel() is not None


# ── SlashCommandHandler ───────────────────────────────────────────────────

class SlashCommandHandler:
    def __init__(self, renderer: Any = None) -> None:
        self.renderer = renderer

    def handle(self, text: str, model: TranscriptModel, console: Any) -> bool:
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
        if cmd.startswith("/expand"):
            self._expand(cmd, model, console)
            return True
        # Check renderer's menu registry for dynamic commands
        if self.renderer is not None:
            menu_registry = getattr(self.renderer, "_menu_registry", None)
            if menu_registry is not None and hasattr(menu_registry, "get"):
                factory = menu_registry.get(cmd)
                if factory is not None:
                    # Create context for factory
                    class _MenuCtx:
                        pass
                    ctx = _MenuCtx()
                    ctx.config = getattr(self.renderer, "_loaded_config", None)  # type: ignore[attr-defined]
                    ctx.console = console  # type: ignore[attr-defined]
                    ctx.renderer = self.renderer  # type: ignore[attr-defined]
                    widget = factory(ctx)
                    self.renderer._pending_menu = widget
                    return True
        return False

    def _expand(self, text: str, model: TranscriptModel, console: Any) -> None:
        """Expand an @mention chip to show its content."""
        import re  # noqa: PLC0415
        m = re.search(r"@\S+", text)
        if not m:
            if console:
                console.print("Usage: /expand @file.py")
            return
        raw_mention = m.group(0)  # e.g. "@src/auth.py"

        # Find the chip in the model
        for turn in model.turns:
            for chip in turn.mention_chips:
                if chip.raw == raw_mention:
                    chip.expanded = True
                    if console:
                        console.print(f"Expanded {raw_mention}")
                    return

        if console:
            console.print(f"No item found for {raw_mention!r}")

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
                turn.agent_id[:8], turn.agent_name,
                f"${turn.cost_usd:.4f}" if turn.cost_usd else "$0.0000",
                str(turn.tokens) if turn.tokens else "0",
            )
        if not model.turns:
            table.add_row("—", "(no active agents)", "", "")
        if console:
            console.print(table)

    def _history(self, model: TranscriptModel, console: Any) -> None:
        if not RICH_AVAILABLE or console is None:  # pragma: no cover
            return
        lines = model.render()[-20:]
        console.print(Panel("\n".join(lines) or "(empty)", title="/history — last 20 lines"))

    def _help(self, console: Any) -> None:
        if not RICH_AVAILABLE or console is None:  # pragma: no cover
            return
        table = Table(title="Slash Commands", box=rich_box.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Description")
        for cmd_name, desc in SLASH_HELP.items():
            table.add_row(cmd_name, desc)
        console.print(table)


# ── run_inline ────────────────────────────────────────────────────────────

async def run_inline(
    model: TranscriptModel,
    adapter: Any | None = None,
    on_input: Callable[[str], None] | None = None,
) -> None:
    renderer = InlineRenderer(model, adapter)
    await renderer.run(on_input or (lambda _: None))


# ── deprecated build_app ──────────────────────────────────────────────────

def build_app(model: TranscriptModel, on_input: Callable[[str], None]) -> Any:
    """Deprecated. Use run_inline() instead."""
    raise RuntimeError(
        "build_app() is deprecated. Use run_inline() instead.\n"
        "  Before: app = build_app(model, on_input); await app.run_async()\n"
        "  After:  await run_inline(model, adapter, on_input=on_input)\n"
    )


# ── headless mode ─────────────────────────────────────────────────────────

async def run_headless(event_queue: asyncio.Queue, output_stream: TextIO) -> None:
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


# ── offline ANSI frame renderer (pyte e2e tests) ──────────────────────────

def render_frame_ansi(
    model: TranscriptModel,
    cols: int,
    rows: int,
    input_text: str = "",
    menu_lines: list[str] | None = None,
    status_state: StatusState | None = None,
) -> str:
    transcript_rows = max(rows - 3, 0)
    lines = model.render()
    visible = lines[-transcript_rows:] if transcript_rows else []

    def clip(text: str) -> str:
        return text[:cols]

    buf: list[str] = ["\x1b[2J\x1b[H"]
    for i, line in enumerate(visible):
        buf.append(f"\x1b[{i + 1};1H{clip(line)}")

    if menu_lines:
        overlay_end = rows - 3
        overlay_start = max(overlay_end - len(menu_lines) + 1, 1)
        for i, line in enumerate(menu_lines[:overlay_end - overlay_start + 1]):
            buf.append(f"\x1b[{overlay_start + i};1H{clip(line)}")

    # Status bar (rows-2)
    if status_state is not None and status_state.active:
        from .symbols import SPINNER_FRAMES as SF  # noqa: PLC0415
        frame = SF[status_state.spinner_frame % len(SF)]
        elapsed = (time.monotonic() - status_state.intent_started_at
                   if status_state.intent_started_at else 0.0)
        status_bar = (
            f" {frame} {_thinking_wave(status_state.spinner_frame)}  {elapsed:.1f}s"
            f"  ↑{status_state.input_tokens:,} ↓{status_state.output_tokens:,}"
        )
    else:
        status_bar = ""
    buf.append(f"\x1b[{rows - 2};1H{clip(status_bar)}")

    agents = len({t.agent_id for t in model.turns})
    status = (
        f" {agents} agents | ${model.total_cost_usd:.3f}"
        f" | {model.total_tokens:,} tok"
    )
    buf.append(f"\x1b[{rows - 1};1H{clip(status)}")
    buf.append(f"\x1b[{rows};1H{clip(INPUT_PROMPT + input_text)}")
    return "".join(buf)
