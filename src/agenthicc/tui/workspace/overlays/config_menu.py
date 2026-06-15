"""ConfigMenuOverlay — /config configuration editor (PRD-65 §3)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay


class _State(Enum):
    NAVIGATE = auto()
    EDIT     = auto()


@dataclass
class _Field:
    section_name: str
    field_name:   str
    label:        str
    value:        Any
    default:      Any
    field_type:   type
    editable:     bool
    changed:      bool = False


@dataclass
class _Section:
    name:     str
    fields:   list["_Field"]
    expanded: bool = True


def _build_sections(cfg: Any) -> list[_Section]:
    if cfg is None:
        return []
    ATTRS = ["execution", "memory", "security", "api", "plugins"]
    sections: list[_Section] = []
    for attr in ATTRS:
        obj = getattr(cfg, attr, None)
        if obj is None:
            continue
        cls = type(obj)
        try:
            default = cls()
        except TypeError:
            default = obj
        fields: list[_Field] = []
        if dataclasses.is_dataclass(obj):
            for f in dataclasses.fields(obj):
                val      = getattr(obj, f.name)
                editable = isinstance(val, (int, str, bool, float))
                fields.append(_Field(
                    section_name=attr,
                    field_name=f.name,
                    label=f.name,
                    value=val,
                    default=getattr(default, f.name, val),
                    field_type=type(val),
                    editable=editable,
                ))
        if fields:
            sections.append(_Section(name=attr, fields=fields))
    return sections


class ConfigMenuOverlay(Overlay):
    """Interactive configuration editor."""

    name = "config"
    _MAX_VISIBLE = 12

    def __init__(self, cfg: Any, on_close: Callable[[], None]) -> None:
        self._cfg      = cfg
        self._on_close = on_close
        self._sections = _build_sections(cfg)
        self._cursor   = (0, -1)   # (section_idx, field_idx; -1 = header)
        self._state    = _State.NAVIGATE
        self._edit_buf = ""
        self._scroll   = 0
        self._status   = "↑↓ navigate   Enter edit/expand   s save   Esc close"

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> Any:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415

        rows    = self._build_rows()
        visible = rows[self._scroll : self._scroll + self._MAX_VISIBLE]
        sep     = Text("─" * 60, style="dim")
        lines   = [sep]
        for row in visible:
            lines.append(Text.from_markup(row))
        lines += [sep, Text.from_markup(f"  [dim]{self._status}[/dim]")]
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._state == _State.EDIT:
            self._handle_edit(key, ch)
            return True

        match key:
            case Key.ESC:
                self._on_close()
            case Key.UP:
                self._move(-1)
            case Key.DOWN:
                self._move(1)
            case Key.ENTER | Key.RIGHT:
                self._activate()
            case Key.LEFT:
                self._collapse()
            case Key.CHAR if ch == "s":
                self._save()
        return True

    def _handle_edit(self, key: Key, ch: str) -> None:
        match key:
            case Key.ESC:
                self._state    = _State.NAVIGATE
                self._edit_buf = ""
                self._status   = "↑↓ navigate   Enter edit/expand   s save   Esc close"
            case Key.ENTER:
                self._commit_edit()
                self._state    = _State.NAVIGATE
                self._edit_buf = ""
            case Key.BACKSPACE:
                self._edit_buf = self._edit_buf[:-1]
            case Key.CHAR if ch and ch.isprintable():
                self._edit_buf += ch

    def _build_rows(self) -> list[str]:
        if not self._sections:
            return [
                "  [dim](no configuration loaded)[/dim]",
                "  [dim]Start from a project directory with an agenthicc.toml[/dim]",
                "  [dim]Press Esc to close.[/dim]",
            ]
        from rich.markup import escape as _e  # noqa: PLC0415
        rows: list[str] = []
        si_cur, fi_cur = self._cursor
        for si, section in enumerate(self._sections):
            icon    = "▼" if section.expanded else "▶"
            focused = si == si_cur and fi_cur == -1
            row     = (
                f"  {'[reverse]' if focused else ''}[bold]{icon} {section.name}[/bold]"
                f"{'[/reverse]' if focused else ''}"
            )
            rows.append(row)
            if section.expanded:
                for fi, field in enumerate(section.fields):
                    foc  = si == si_cur and fi == fi_cur
                    ind  = "▶" if foc else " "
                    chg  = "[yellow]●[/yellow] " if field.changed else "  "
                    val  = str(field.value)[:30]
                    if foc and self._state == _State.EDIT:
                        val = self._edit_buf + "█"
                    rows.append(
                        f"  {'[reverse]' if foc else ''}{ind} {chg}"
                        f"{_e(field.label):<24}{_e(val)}"
                        f"{'[/reverse]' if foc else ''}"
                    )
        return rows

    def _focused_field(self) -> "_Field | None":
        si, fi = self._cursor
        if fi < 0 or si >= len(self._sections):
            return None
        section = self._sections[si]
        if not section.expanded or fi >= len(section.fields):
            return None
        return section.fields[fi]

    def _all_positions(self) -> list[tuple[int, int]]:
        pos: list[tuple[int, int]] = []
        for si, section in enumerate(self._sections):
            pos.append((si, -1))
            if section.expanded:
                for fi in range(len(section.fields)):
                    pos.append((si, fi))
        return pos

    def _move(self, delta: int) -> None:
        positions = self._all_positions()
        if not positions:
            return
        try:
            idx = positions.index(self._cursor)
        except ValueError:
            idx = 0
        idx = max(0, min(len(positions) - 1, idx + delta))
        self._cursor = positions[idx]
        if idx < self._scroll:
            self._scroll = idx
        elif idx >= self._scroll + self._MAX_VISIBLE:
            self._scroll = idx - self._MAX_VISIBLE + 1

    def _activate(self) -> None:
        si, fi = self._cursor
        if fi == -1:
            self._sections[si].expanded = not self._sections[si].expanded
            return
        field = self._focused_field()
        if field and field.editable:
            self._edit_buf = str(field.value)
            self._state    = _State.EDIT
            self._status   = "Type new value  Enter confirm  Esc cancel"

    def _collapse(self) -> None:
        si, fi = self._cursor
        if fi >= 0:
            self._sections[si].expanded = False
            self._cursor = (si, -1)
        elif si > 0:
            self._sections[si].expanded = False

    def _commit_edit(self) -> None:
        field = self._focused_field()
        if not field:
            return
        try:
            new_val = field.field_type(self._edit_buf)
            object.__setattr__(field, "value",   new_val)
            object.__setattr__(field, "changed", True)
            self._status = f"[green]✓[/green] {field.label} updated (press s to save)"
        except (ValueError, TypeError):
            self._status = f"[red]Invalid value for {field.field_type.__name__}[/red]"

    def _save(self) -> None:
        if self._cfg is None:
            self._status = "[red]No config loaded[/red]"
            return
        saved = 0
        for section in self._sections:
            for field in section.fields:
                if field.changed:
                    obj = getattr(self._cfg, field.section_name, None)
                    if obj is not None and dataclasses.is_dataclass(obj):
                        try:
                            object.__setattr__(obj, field.field_name, field.value)
                            saved += 1
                        except Exception:  # noqa: BLE001
                            pass
        self._status = f"[green]✓ Saved {saved} field(s)[/green]"
