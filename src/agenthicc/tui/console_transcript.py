"""TranscriptView — prints typed event blocks to the terminal scroll buffer.

Satisfies :class:`~agenthicc.tui.protocols.TranscriptPrinter`.

Content is written via ``console.print()`` and scrolls naturally in the
terminal's own scrollback history.  There is no internal viewport or widget —
old content is simply above the current cursor position in the terminal.
"""
from __future__ import annotations

from typing import Any

_SEP = "[dim]" + "─" * 72 + "[/dim]"
_MD  = "\x00md\x00"   # sentinel prefix used by agent_turn.py for Markdown lines


class TranscriptView:
    """Appends event blocks to the terminal scroll buffer.

    Implements :class:`~agenthicc.tui.protocols.TranscriptPrinter`.
    """

    def __init__(self, console: Any) -> None:
        self._console = console
        self._printed_count = 0
        self._model: Any = None

    def set_model(self, model: Any) -> None:
        self._model = model

    # ── typed event printers ──────────────────────────────────────────────────

    def print_user(self, text: str) -> None:
        self._console.print(
            f"[bold cyan]You[/bold cyan]\n{_SEP}\n{text}",
            markup=True, highlight=False,
        )

    def print_assistant_header(self, model_short: str) -> None:
        self._console.print(
            f"[bold green]agenthicc[/bold green] [dim]({model_short})[/dim]\n{_SEP}",
            markup=True, highlight=False,
        )

    def print_assistant_chunk(self, text: str) -> None:
        self._console.print(text, end="", markup=False, highlight=False)

    def print_thinking_step(self, step: str, done: bool = False) -> None:
        icon = "[green]✓[/green]" if done else "[yellow]→[/yellow]"
        self._console.print(f"  {icon} [dim]{step}[/dim]", markup=True, highlight=False)

    def print_tool_complete(
        self,
        name: str,
        success: bool,
        ms: float | None,
        diff: str | None,
    ) -> None:
        icon = "[green]✓[/green]" if success else "[red]✗[/red]"
        dur = f" [dim]{ms:.0f}ms[/dim]" if ms else ""
        self._console.print(
            f"  [dim]⎿[/dim] [bold]{name}[/bold]  {icon}{dur}",
            markup=True, highlight=False,
        )
        if diff:
            for dl in diff.splitlines()[:8]:
                if dl.startswith("+"):
                    self._console.print(f"    [green]{dl}[/green]", markup=True, highlight=False)
                elif dl.startswith("-"):
                    self._console.print(f"    [red]{dl}[/red]", markup=True, highlight=False)
                elif dl.startswith("@@"):
                    self._console.print(f"    [dim cyan]{dl}[/dim cyan]", markup=True, highlight=False)
                else:
                    self._console.print(f"    [dim]{dl}[/dim]", markup=True, highlight=False)

    def print_file_modified(self, path: str) -> None:
        self._console.print(
            f"  [dim]Modified:[/dim] [cyan]{path}[/cyan]",
            markup=True, highlight=False,
        )

    def print_error(self, message: str, detail: str = "") -> None:
        self._console.print(
            f"\n[red bold]ERROR[/red bold]\n{_SEP}\n[red]{message}[/red]",
            markup=True, highlight=False,
        )
        if detail:
            self._console.print(f"[dim]{detail}[/dim]", markup=True, highlight=False)
        self._console.print()

    def print_task_complete(self) -> None:
        self._console.print(
            f"\n[green bold]✓ Task Complete[/green bold]\n{_SEP}\n",
            markup=True, highlight=False,
        )

    def print_markup(self, markup: str) -> None:
        """Print an arbitrary Rich markup string (or Markdown sentinel)."""
        if markup.startswith(_MD):
            from rich.markdown import Markdown  # noqa: PLC0415
            # end="" lets Markdown's own newlines control spacing; console.print's
            # default end="\n" would add a second trailing newline creating a blank line.
            self._console.print(Markdown(markup[len(_MD):]), highlight=False, end="")
        else:
            self._console.print(markup, markup=True, highlight=False)

    # ── model flush ───────────────────────────────────────────────────────────

    def flush_from_model(self) -> None:
        """Print any lines in TranscriptModel that haven't been printed yet."""
        if self._model is None:
            return
        lines = self._model.render()
        new = lines[self._printed_count:]
        for line in new:
            self.print_markup(line)
        if new:
            self._printed_count = len(lines)
