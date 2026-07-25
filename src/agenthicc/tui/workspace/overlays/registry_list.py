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
    from agenthicc.tools.base import ToolLike
    from agenthicc.workflows.plugin import WorkflowPlugin
    from agenthicc.workflows.registry import WorkflowRegistry


@dataclass(frozen=True)
class _ListRow:
    """One selectable registry entry and its detail fields."""

    label: str
    metadata: str
    description: str
    details: tuple[tuple[str, str], ...]
    selection: str | None = None


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
        on_select: Callable[[str], None] | None = None,
    ) -> None:
        self._title = title
        self._rows = rows
        self._on_close = on_close
        self._on_select = on_select
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
                Text(
                    "  ↑↓ navigate   Enter detail   Esc close"
                    if self._on_select is None
                    else "  ↑↓ navigate   Enter detail/use   Esc close",
                    style="dim",
                ),
            ]
        )
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._detail is not None:
            if key == Key.ENTER and self._on_select is not None:
                selection = self._detail.selection
                if selection:
                    self._on_select(selection)
                    self._on_close()
                return True
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
        detail_hint = (
            "Enter use   Esc/← back   Esc again close"
            if self._on_select is not None and row.selection
            else "Esc/← back   Esc again close"
        )
        lines.extend([Text(""), separator, Text(f"  {detail_hint}", style="dim")])
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
        on_select: Callable[[str], None] | None = None,
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
                selection=(command.name if command.name.startswith("/") else f"/{command.name}"),
            )
            for command in commands
        ]
        super().__init__("Registered Commands", rows, on_close, on_select)


class SkillListOverlay(_RegistryListOverlay):
    """Interactive listing of skills available to the active agent."""

    name = "skills"

    def __init__(
        self,
        skills: list["SkillDef"],
        on_close: Callable[[], None],
        on_select: Callable[[str], None] | None = None,
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
                selection=f"${skill.slug}",
            )
            for skill in skills
        ]
        super().__init__("Available Skills", rows, on_close, on_select)


class ToolListOverlay(_RegistryListOverlay):
    """Interactive listing of tools available to the active session."""

    name = "tools"

    def __init__(self, tools: list["ToolLike"], on_close: Callable[[], None]) -> None:
        from inspect import getattr_static

        from agenthicc.tools.base import Tool

        rows: list[_ListRow] = []
        for tool in tools:
            if isinstance(tool, Tool):
                name = tool.name or type(tool).__name__
                description = tool.description
                capabilities: object = tool.capabilities
            else:
                name_value: object = getattr_static(tool, "__name__", type(tool).__name__)
                description_value: object = getattr_static(tool, "__doc__", "")
                name = str(name_value)
                description = str(description_value)
                capabilities = ()
            if isinstance(capabilities, (set, frozenset, list, tuple)):
                capability_text = ", ".join(sorted(str(item) for item in capabilities)) or "(none)"
            else:
                capability_text = str(capabilities or "(none)")
            source = "MCP" if name.startswith("mcp:") else "registered"
            rows.append(
                _ListRow(
                    label=name,
                    metadata=source,
                    description=description.splitlines()[0] if description else "",
                    details=(
                        ("Source", source),
                        ("Capabilities", capability_text),
                        ("Type", type(tool).__name__),
                    ),
                )
            )
        super().__init__("Registered Tools", sorted(rows, key=lambda row: row.label), on_close)


class WorkflowListOverlay(_RegistryListOverlay):
    """Interactive listing of loaded workflow plugins and their phase graphs."""

    name = "workflows"

    def __init__(
        self,
        workflows: list["type[WorkflowPlugin]"],
        registry: "WorkflowRegistry | None",
        on_close: Callable[[], None],
        on_select: Callable[[str], None] | None = None,
    ) -> None:
        rows: list[_ListRow] = []
        for workflow in sorted(workflows, key=lambda item: item.name):
            entry = registry.get_entry(workflow.name) if registry is not None else None
            source = entry.source if entry is not None else "registered"
            phases = list(workflow.phase_names())
            runner_kind = "custom" if "build_runner" in workflow.__dict__ else "default"
            rows.append(
                _ListRow(
                    label=workflow.name,
                    metadata=f"{source} · {len(phases)} phase(s)",
                    description=workflow.description,
                    details=(
                        ("Source", source),
                        ("Phases", ", ".join(phases) or "(none)"),
                        ("Runner", runner_kind),
                        ("Modes", ", ".join(workflow.mode_bindings) or "(none)"),
                    ),
                    selection=f"/workflow {workflow.name}",
                )
            )
        super().__init__("Registered Workflows", rows, on_close, on_select)
