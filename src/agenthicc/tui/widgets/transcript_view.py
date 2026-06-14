"""TranscriptView — chronological event stream with typed event blocks."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.containers import ScrollableContainer
from textual.widgets import RichLog

from agenthicc.tui.messages import (
    AgentRunFinished,
    AgentRunStarted,
    ConsolePrint,
    ErrorOccurred,
    FileModified,
    ThinkingStep,
    ToolCallComplete,
    ToolCallStarted,
    TranscriptAppend,
    TranscriptUpdated,
    UserMessagePosted,
)
from agenthicc.tui.transcript import TranscriptModel

__all__ = ["TranscriptView"]

_SEP = "[dim]" + "─" * 72 + "[/dim]"


def _fmt_user(text: str) -> str:
    return f"[bold cyan]You[/bold cyan]\n{_SEP}\n{text}\n"


def _fmt_assistant_header(model_short: str) -> str:
    return f"[bold green]agenthicc[/bold green] [dim]({model_short})[/dim]\n{_SEP}"


def _fmt_thinking_step(step: str, done: bool) -> str:
    icon = "[green]✓[/green]" if done else "[yellow]→[/yellow]"
    return f"  {icon} [dim]{step}[/dim]"


def _fmt_tool_start(name: str, args: dict) -> str:
    items = list(args.items())
    if len(items) == 1:
        args_str = repr(items[0][1])[:60]
    elif items:
        args_str = ", ".join(f"{k}={repr(v)[:25]}" for k, v in items[:3])
    else:
        args_str = ""
    return f"[dim]Tool:[/dim] [bold]{name}[/bold][dim]({args_str})[/dim]  [yellow]…[/yellow]"


def _fmt_tool_complete(name: str, success: bool, duration_ms: float | None, diff: str | None) -> str:
    icon = "[green]✓[/green]" if success else "[red]✗[/red]"
    dur = f" [dim]{duration_ms:.0f}ms[/dim]" if duration_ms else ""
    header = f"[dim]Tool:[/dim] [bold]{name}[/bold]  {icon}{dur}"
    if diff:
        lines = diff.splitlines()[:8]
        diff_lines: list[str] = []
        for ln in lines:
            if ln.startswith("+++") or ln.startswith("---"):
                diff_lines.append(f"  [dim]{ln}[/dim]")
            elif ln.startswith("@@"):
                diff_lines.append(f"  [dim cyan]{ln}[/dim cyan]")
            elif ln.startswith("+"):
                diff_lines.append(f"  [green]{ln}[/green]")
            elif ln.startswith("-"):
                diff_lines.append(f"  [red]{ln}[/red]")
            else:
                diff_lines.append(f"  [dim]{ln}[/dim]")
        extra = len(diff.splitlines()) - len(lines)
        if extra > 0:
            diff_lines.append(f"  [dim]… {extra} more lines[/dim]")
        return header + "\n" + "\n".join(diff_lines)
    return header


def _fmt_file_modified(path: str) -> str:
    return f"[dim]Modified:[/dim] [cyan]{path}[/cyan]"


def _fmt_error(message: str, detail: str = "") -> str:
    body = f"\n[dim]{detail}[/dim]" if detail else ""
    return f"[red bold]ERROR[/red bold]\n{_SEP}\n[red]{message}[/red]{body}\n"


def _fmt_task_complete() -> str:
    return f"\n[green bold]✓ Task Complete[/green bold]\n{_SEP}\n"


class TranscriptView(ScrollableContainer):
    """Scrollable event-stream viewport backed by a RichLog."""

    DEFAULT_CSS = """
    TranscriptView {
        height: 1fr;
        min-height: 8;
    }
    RichLog {
        height: 1fr;
        background: transparent;
        scrollbar-size: 1 1;
    }
    """

    auto_scroll: reactive[bool] = reactive(True)

    def __init__(self, model: TranscriptModel, *, name=None, id=None, classes=None):
        super().__init__(name=name, id=id, classes=classes)
        self.model = model
        self._tool_names: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield RichLog(markup=True, wrap=True, id="richlog")

    @property
    def _log(self) -> RichLog:
        return self.query_one("#richlog", RichLog)

    def on_mount(self) -> None:
        self._log.auto_scroll = True
        self.refresh_transcript()

    def on_scroll_end(self, _: object) -> None:
        self.auto_scroll = True

    # Sentinel used by agent_turn.py to mark Markdown-formatted lines.
    _MD_SENTINEL = "\x00md\x00"

    # ── full re-render ────────────────────────────────────────────────────────

    def refresh_transcript(self) -> None:
        rl = self._log
        rl.clear()
        for line in self.model.render():
            self._write_line(rl, line)
        rl.scroll_end(animate=False)

    # ── append helpers ────────────────────────────────────────────────────────

    def _write_line(self, rl: RichLog, line: str) -> None:
        """Write one transcript line, handling the Markdown sentinel prefix."""
        if line.startswith(self._MD_SENTINEL):
            from rich.markdown import Markdown  # noqa: PLC0415
            md_text = line[len(self._MD_SENTINEL):]
            rl.write(Markdown(md_text))
        else:
            rl.write(line)

    def _append(self, markup: str) -> None:
        self._write_line(self._log, markup)
        if self.auto_scroll:
            self._log.scroll_end(animate=False)

    # ── message handlers ──────────────────────────────────────────────────────

    def on_transcript_updated(self, _: TranscriptUpdated) -> None:
        self.refresh_transcript()

    def on_console_print(self, event: ConsolePrint) -> None:
        self._append(event.markup)

    def on_transcript_append(self, event: TranscriptAppend) -> None:
        self._append(event.markup)

    def on_user_message_posted(self, event: UserMessagePosted) -> None:
        self._append(_fmt_user(event.text))

    def on_agent_run_started(self, event: AgentRunStarted) -> None:
        self._append(_fmt_assistant_header(event.model_short))

    def on_agent_run_finished(self, _: AgentRunFinished) -> None:
        self._append("")

    def on_tool_call_started(self, event: ToolCallStarted) -> None:
        self._tool_names[event.tool_use_id] = event.name
        self._append(_fmt_tool_start(event.name, event.args))

    def on_tool_call_complete(self, event: ToolCallComplete) -> None:
        name = self._tool_names.pop(event.tool_use_id, event.tool_use_id)
        self._append(_fmt_tool_complete(name, event.success, event.duration_ms, event.diff))

    def on_file_modified(self, event: FileModified) -> None:
        self._append(_fmt_file_modified(event.path))

    def on_error_occurred(self, event: ErrorOccurred) -> None:
        self._append(_fmt_error(event.message, event.detail))

    def on_thinking_step(self, event: ThinkingStep) -> None:
        self._append(_fmt_thinking_step(event.step, event.done))
