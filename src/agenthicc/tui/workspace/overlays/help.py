"""HelpOverlay — interactive /help overlay (PRD-70)."""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from rich.console import RenderableType

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.commands.command import Command
from agenthicc.commands.registry import UnifiedCommandRegistry


class _View(Enum):
    LIST = auto()
    DETAIL = auto()


class HelpOverlay(Overlay):
    """Scrollable grouped command list with per-command detail view."""

    name = "help"
    _MAX_VISIBLE = 14

    def __init__(
        self,
        registry: UnifiedCommandRegistry,
        on_close: Callable[[], None],
        initial_query: str = "",
    ) -> None:
        self._registry: UnifiedCommandRegistry = registry
        self._on_close = on_close
        self._view = _View.LIST
        self._detail_cmd: Command | None = None  # Command currently shown in DETAIL_VIEW

        # Build flat row list: str = group header, Command = selectable row.
        self._rows: list[str | Command] = []
        if registry is not None:
            for group in registry.groups():
                cmds = registry.commands_for_group(group)
                if cmds:
                    self._rows.append(group)  # header (str)
                    self._rows.extend(cmds)  # commands

        # Indices into _rows that point at selectable Command objects.
        self._cmd_indices: list[int] = [
            i for i, r in enumerate(self._rows) if not isinstance(r, str)
        ]
        self._cursor_pos: int = 0  # index into _cmd_indices
        self._scroll: int = 0

        # Route initial_query.
        query = initial_query.strip()
        if query and registry is not None:
            exact = registry.get(query)
            if exact is not None:
                # Exact match → open DETAIL_VIEW immediately.
                self._view = _View.DETAIL
                self._detail_cmd = exact
            else:
                # Partial match → navigate LIST_VIEW cursor to best match.
                matches = registry.matches(query) if query.startswith("/") else []
                if matches:
                    target = matches[0].name
                    for pos, idx in enumerate(self._cmd_indices):
                        row = self._rows[idx]
                        if isinstance(row, Command) and row.name == target:
                            self._cursor_pos = pos
                            break
        self._clamp_scroll()

    # ── Overlay interface ─────────────────────────────────────────────────────

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> "RenderableType":
        if self._view == _View.DETAIL:
            return self._render_detail()
        return self._render_list()

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._view == _View.DETAIL:
            if key == Key.ESC:
                self._view = _View.LIST
                self._detail_cmd = None
        else:
            match key:
                case Key.ESC:
                    self._on_close()
                case Key.UP:
                    self._cursor_pos = max(0, self._cursor_pos - 1)
                    self._clamp_scroll()
                case Key.DOWN:
                    self._cursor_pos = min(len(self._cmd_indices) - 1, self._cursor_pos + 1)
                    self._clamp_scroll()
                case Key.ENTER:
                    if self._cmd_indices:
                        cmd = self._rows[self._cmd_indices[self._cursor_pos]]
                        if isinstance(cmd, Command):
                            self._detail_cmd = cmd
                        self._view = _View.DETAIL
        return True

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_list(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        if not self._rows:
            return Group(
                Text("─" * 60, style="dim"),
                Text("  (no commands registered)", style="dim"),
                Text("─" * 60, style="dim"),
            )

        # Determine which rows are visible based on scroll offset.
        selected_row = self._cmd_indices[self._cursor_pos] if self._cmd_indices else -1
        visible = self._rows[self._scroll : self._scroll + self._MAX_VISIBLE]
        sep = Text("─" * 60, style="dim")
        lines: list[RenderableType] = [sep]

        for local_i, row in enumerate(visible):
            row_idx = self._scroll + local_i
            if isinstance(row, str):
                # Group header.
                lines.append(Text(f"  {row}", style="bold"))
            else:
                # Command row.
                selected = row_idx == selected_row
                indicator = "▶" if selected else " "
                name_col = f"{row.name:<24}"
                desc_col = (
                    row.description[:36] + "…" if len(row.description) > 36 else row.description
                )
                markup = (
                    f"  [bold cyan]{indicator} {name_col}[/bold cyan] {desc_col}"
                    if selected
                    else f"  [dim]{indicator}[/dim] [cyan]{name_col}[/cyan] {desc_col}"
                )
                lines.append(Text.from_markup(markup))

        lines += [
            sep,
            Text.from_markup("  [dim]↑↓ navigate   Enter detail   Esc close[/dim]"),
        ]
        return Group(*lines)

    def _render_detail(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        cmd = self._detail_cmd
        if cmd is None:
            return Group(Text("  (no command selected)", style="dim"))

        aliases = ", ".join(cmd.aliases) if cmd.aliases else "(none)"
        arg_hint = cmd.argument_hint or "(none)"
        sep = Text("─" * 60, style="dim")

        lines: list[RenderableType] = [
            sep,
            Text.from_markup(f"  [bold cyan]{cmd.name}[/bold cyan]"),
            Text(""),
            Text.from_markup(f"  {cmd.description}"),
            Text(""),
            Text.from_markup(f"  [dim]Group:[/dim]   {cmd.group}"),
            Text.from_markup(f"  [dim]Args:[/dim]    {arg_hint}"),
            Text.from_markup(f"  [dim]Aliases:[/dim] {aliases}"),
            Text.from_markup(f"  [dim]Source:[/dim]  {cmd.source_id}"),
            Text(""),
            sep,
            Text.from_markup("  [dim]Esc  back to list[/dim]"),
        ]
        return Group(*lines)

    # ── Scroll management ─────────────────────────────────────────────────────

    def _clamp_scroll(self) -> None:
        """Keep the selected command row inside the visible window."""
        if not self._cmd_indices:
            return
        selected_row = self._cmd_indices[self._cursor_pos]
        # Scroll up if cursor is above the window.
        if selected_row < self._scroll:
            self._scroll = selected_row
        # Scroll down if cursor is below the window.
        elif selected_row >= self._scroll + self._MAX_VISIBLE:
            self._scroll = selected_row - self._MAX_VISIBLE + 1
        # Never scroll past the end of rows.
        self._scroll = max(0, min(self._scroll, max(0, len(self._rows) - self._MAX_VISIBLE)))
