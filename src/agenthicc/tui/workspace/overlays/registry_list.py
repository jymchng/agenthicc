"""Registry list overlays for commands and skills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.commands.command import Command
    from agenthicc.skills.loader import SkillDef


@dataclass(frozen=True)
class _ListRow:
    """One selectable registry entry and its detail fields."""

    label: str
    metadata: str
    description: str
    details: tuple[tuple[str, str], ...]


def _shorten(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


class _RegistryListOverlay(Overlay):
    """Scrollable list with a detail view for registry-backed entries."""

    _MAX_VISIBLE = 10
    _LABEL_WIDTH = 27

    def __init__(
        self,
        title: str,
        rows: list[_ListRow],
        on_close: Callable[[], None],
    ) -> None:
        self._title = title
        self._rows = rows
        self._on_close = on_close
        self._selected = 0
        self._scroll = 0
        self._detail: _ListRow | None = None

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        if self._detail is not None:
            return self._render_detail(self._detail)

        separator = Text("─" * 72, style="dim")
        lines: list[RenderableType] = [
            separator,
            Text(f"  {self._title}  ({len(self._rows)})", style="bold cyan"),
        ]
        if not self._rows:
            lines.append(Text("  (none found)", style="dim"))
        else:
            visible = self._rows[self._scroll : self._scroll + self._MAX_VISIBLE]
            for offset, row in enumerate(visible):
                index = self._scroll + offset
                selected = index == self._selected
                line = Text("  ▶ " if selected else "    ", style="reverse" if selected else "")
                line.append(f"{row.label:<{self._LABEL_WIDTH}}", style="bold cyan")
                line.append(row.metadata, style="dim")
                line.append("  ")
                line.append(_shorten(row.description or "—", 52))
                if selected:
                    line.stylize("reverse")
                lines.append(line)
        lines.extend(
            [
                separator,
                Text("  ↑↓ navigate   Enter detail   Esc close", style="dim"),
            ]
        )
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._detail is not None:
            if key in (Key.ESC, Key.LEFT):
                self._detail = None
            return True

        match key:
            case Key.ESC:
                self._on_close()
            case Key.UP:
                self._move(-1)
            case Key.DOWN:
                self._move(1)
            case Key.ENTER if self._rows:
                self._detail = self._rows[self._selected]
        return True

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        self._selected = max(0, min(len(self._rows) - 1, self._selected + delta))
        if self._selected < self._scroll:
            self._scroll = self._selected
        elif self._selected >= self._scroll + self._MAX_VISIBLE:
            self._scroll = self._selected - self._MAX_VISIBLE + 1

    def _render_detail(self, row: _ListRow) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        separator = Text("─" * 72, style="dim")
        lines: list[RenderableType] = [
            separator,
            Text(f"  {row.label}", style="bold cyan"),
            Text(""),
            Text(row.description or "—"),
            Text(""),
        ]
        for label, value in row.details:
            line = Text(f"  {label}: ", style="dim")
            line.append(value)
            lines.append(line)
        lines.extend([Text(""), separator, Text("  Esc/← back   Esc again close", style="dim")])
        return Group(*lines)


class RegistryMessageOverlay(Overlay):
    """Display a command/skill result without appending it to the transcript."""

    name = "registry_message"

    def __init__(self, title: str, message: str, on_close: Callable[[], None]) -> None:
        self._title = title
        self._message = message
        self._on_close = on_close

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        separator = Text("─" * 72, style="dim")
        lines: list[RenderableType] = [
            separator,
            Text(f"  {self._title}", style="bold cyan"),
            Text(""),
        ]
        lines.extend(Text(line) for line in self._message.splitlines() or [""])
        lines.extend([Text(""), separator, Text("  Esc close", style="dim")])
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if key == Key.ESC:
            self._on_close()
        return True


class CommandListOverlay(_RegistryListOverlay):
    """Interactive listing of registered non-skill commands."""

    name = "commands"

    def __init__(
        self,
        commands: list["Command"],
        on_close: Callable[[], None],
    ) -> None:
        rows = [
            _ListRow(
                label=command.name,
                metadata=f"{command.group} · {command.source_id}",
                description=command.description,
                details=(
                    ("Group", command.group),
                    ("Source", command.source_id),
                    ("Arguments", command.argument_hint or "(none)"),
                    ("Aliases", ", ".join(command.aliases) or "(none)"),
                ),
            )
            for command in commands
        ]
        super().__init__("Registered Commands", rows, on_close)


class SkillListOverlay(_RegistryListOverlay):
    """Interactive listing of skills available to the active agent."""

    name = "skills"

    def __init__(
        self,
        skills: list["SkillDef"],
        on_close: Callable[[], None],
    ) -> None:
        rows = [
            _ListRow(
                label=f"${skill.slug}",
                metadata=(
                    f"{skill.source} · aliases: "
                    f"{', '.join(f'${alias}' for alias in skill.aliases) or '(none)'}"
                ),
                description=skill.description or skill.name,
                details=(
                    ("Name", skill.name),
                    ("Commands", ", ".join(f"${name}" for name in skill.command_names)),
                    ("Source", skill.source),
                    ("Tools", ", ".join(skill.tools) or "(none)"),
                ),
            )
            for skill in skills
        ]
        super().__init__("Available Skills", rows, on_close)
