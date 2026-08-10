"""Registry list overlays for commands and skills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import time
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.commands.command import Command
    from agenthicc.skills.loader import SkillDef
    from agenthicc.tools.base import ToolLike
    from agenthicc.workflows.plugin import WorkflowPlugin
    from agenthicc.workflows.registry import WorkflowRegistry
    from agenthicc.runners.workflow_recovery import WorkflowRecoveryRecord


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

    def __init__(
        self,
        tools: list["ToolLike"],
        on_close: Callable[[], None],
        source_by_name: Mapping[str, str] | None = None,
    ) -> None:
        from agenthicc.tools.base import Tool

        source_by_name = source_by_name or {}
        rows: list[_ListRow] = []
        for tool in tools:
            if isinstance(tool, Tool):
                name = tool.name or type(tool).__name__
                description = tool.description
                capabilities: object = tool.capabilities
            else:
                # Callable plugin tools expose their user-facing metadata on
                # the function object. getattr_static() returns the function
                # type's descriptors here instead of the actual metadata.
                name_value: object = getattr(tool, "__name__", type(tool).__name__)
                description_value: object = getattr(tool, "__doc__", "")
                name = str(name_value)
                description = str(description_value)
                capabilities = ()
            if isinstance(capabilities, (set, frozenset, list, tuple)):
                capability_text = ", ".join(sorted(str(item) for item in capabilities)) or "(none)"
            else:
                capability_text = str(capabilities or "(none)")
            source = "builtin" if source_by_name.get(name) == "builtin" else "plugin"
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
        super().__init__("Available Tools", sorted(rows, key=lambda row: row.label), on_close)


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
        from agenthicc.tui.runtime.mode_manager import canonical_mode_name  # noqa: PLC0415

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
                        (
                            "Modes",
                            ", ".join(canonical_mode_name(mode) for mode in workflow.mode_bindings)
                            or "(none)",
                        ),
                    ),
                    selection=f"/workflow {workflow.name}",
                )
            )
        super().__init__("Registered Workflows", rows, on_close, on_select)


class WorkflowRunsOverlay(Overlay):
    """Paginated selector for durable workflow runs that can be resumed.

    Selecting a row and pressing Enter invokes the session's guarded resume
    callback immediately. The callback owns rehydration, validation, and the
    live-owner claim; this overlay is deliberately only a presentation and
    selection layer.
    """

    name = "workflow-runs"
    _PAGE_SIZE = 8

    def __init__(
        self,
        records: list[WorkflowRecoveryRecord],
        on_close: Callable[[], None],
        on_resume: Callable[[str], bool],
    ) -> None:
        self._records = sorted(records, key=self._sort_key)
        self._on_close = on_close
        self._on_resume = on_resume
        self._selected = 0

    @staticmethod
    def _sort_key(record: WorkflowRecoveryRecord) -> tuple[float, str]:
        return (
            -WorkflowRunsOverlay._created_at(record),
            record.run_id,
        )

    @staticmethod
    def _created_at(record: WorkflowRecoveryRecord) -> float:
        checkpoint = record.checkpoint
        value: object = checkpoint.created_at if checkpoint is not None else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        try:
            timestamp = float(value)
        except OverflowError:
            return 0.0
        return timestamp if math.isfinite(timestamp) else 0.0

    @property
    def page_count(self) -> int:
        return max(1, (len(self._records) + self._PAGE_SIZE - 1) // self._PAGE_SIZE)

    @property
    def selected_record(self) -> WorkflowRecoveryRecord | None:
        if not self._records:
            return None
        self._selected = max(0, min(self._selected, len(self._records) - 1))
        return self._records[self._selected]

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        separator = Text("─" * 84, style="dim")
        page = self._selected // self._PAGE_SIZE + 1
        table = Table(
            title=f"Paused Workflow Runs (page {page}/{self.page_count}; {len(self._records)})",
            expand=True,
        )
        table.add_column("", width=2)
        table.add_column("Workflow", style="bold cyan", no_wrap=True)
        table.add_column("Phase", no_wrap=True)
        table.add_column("Status", style="yellow", no_wrap=True)
        table.add_column("Saved", no_wrap=True)
        table.add_column("Run ID", no_wrap=True)

        start = (page - 1) * self._PAGE_SIZE
        end = min(len(self._records), start + self._PAGE_SIZE)
        for index in range(start, end):
            record = self._records[index]
            status = record.status
            if status in {"running", "resuming"}:
                status = "interrupted"
            saved = self._format_time(record)
            run_id = record.run_id
            table.add_row(
                "▸" if index == self._selected else "",
                record.workflow_name or "—",
                record.current_phase or "saved state",
                status,
                saved,
                _shorten(run_id, 28),
            )
        if not self._records:
            table.add_row("", "—", "—", "—", "—", "(no paused workflow runs)")

        selected = self.selected_record
        if selected is None:
            detail_text = "No paused or interrupted workflow runs are available."
        else:
            status = selected.status
            if status in {"running", "resuming"}:
                status = "interrupted"
            detail_text = (
                f"[bold]Run ID[/bold] {selected.run_id}\n"
                f"[bold]Workflow[/bold] {selected.workflow_name or '—'}\n"
                f"[bold]Phase[/bold] {selected.current_phase or 'saved state'}\n"
                f"[bold]Status[/bold] {status}\n"
                f"[bold]Intent[/bold] {_shorten(selected.intent or '—', 180)}\n"
                "Press Enter to resume this run and return to the conversation."
            )
        detail = Panel(detail_text, title="Selected Run", border_style="cyan")
        footer = Text(
            "↑↓/j/k select   PgUp/PgDn page   Home/End   Enter resume   Esc close",
            style="dim",
        )
        return Group(separator, table, detail, separator, footer)

    @staticmethod
    def _format_time(record: WorkflowRecoveryRecord) -> str:
        timestamp = WorkflowRunsOverlay._created_at(record)
        if timestamp == 0.0:
            return "unknown"
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
        except (OverflowError, OSError, ValueError):
            return "unknown"

    def _move(self, delta: int) -> None:
        if not self._records:
            return
        self._selected = max(0, min(len(self._records) - 1, self._selected + delta))

    def handle_key(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ESC:
                self._on_close()
            case Key.UP:
                self._move(-1)
            case Key.DOWN:
                self._move(1)
            case Key.PAGE_UP:
                self._move(-self._PAGE_SIZE)
            case Key.PAGE_DOWN:
                self._move(self._PAGE_SIZE)
            case Key.HOME:
                self._selected = 0
            case Key.END:
                self._selected = max(0, len(self._records) - 1)
            case Key.CHAR if ch.lower() == "k":
                self._move(-1)
            case Key.CHAR if ch.lower() == "j":
                self._move(1)
            case Key.ENTER:
                selected = self.selected_record
                if selected is not None:
                    self._on_resume(str(selected.run_id))
                    self._on_close()
        return True
