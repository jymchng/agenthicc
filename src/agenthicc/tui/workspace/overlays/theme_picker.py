"""``/theme``: choose a theme with a live preview; no restart (TUX-01 §2.6)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.theme import Theme
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType

__all__ = ["ThemePickerOverlay", "theme_preview"]


def theme_preview(theme: Theme) -> list[RenderableType]:
    """A user message, a tool row, a diff and a status line drawn with *theme*."""
    from rich.text import Text

    p, g = theme.palette, theme.glyphs
    user = Text.assemble(
        (f" {g.prompt} ", p.accent), ("fix the flaky scheduler test", "none"), style=p.on("user_bg")
    )
    tool = Text.assemble(
        (f" {g.bullet} ", p.muted), ("Read ", "none"), ("schedules/recurrence.py", p.muted)
    )
    failed = Text.assemble(
        (f" {g.fail} ", p.danger), ("$ uv run pytest -q", "none"), ("   exit 1", p.muted)
    )
    removed = Text.assemble(
        ("   12 - ", p.diff_del), ("return start", "none"), style=p.on("diff_del_bg")
    )
    added = Text.assemble(
        ("   12 + ", p.diff_add),
        ("return start.astimezone(zone)", "none"),
        style=p.on("diff_add_bg"),
    )
    status = Text.assemble(
        (f" {g.bullet} ", p.success),
        ("Idle", "none"),
        (f" {g.sep} opus-4.8 {g.sep} ", p.muted),
        ("ctx 41% ", p.muted),
        (g.meter_full * 2, p.success),
        (g.meter_empty * 3, p.subtle),
        ("   Safe", p.mode_safe),
    )
    return [user, tool, failed, removed, added, status]


class ThemePickerOverlay(Overlay):
    """Theme names with a preview of the highlighted one; Enter applies it."""

    name = "theme-picker"

    def __init__(
        self,
        names: Sequence[str],
        *,
        current: str,
        load: Callable[[str], Theme],
        apply: Callable[[Theme], None],
        save: Callable[[str], str],
        close: Callable[[], None],
    ) -> None:
        self._names = tuple(names)
        self._current = current
        self._load = load
        self._apply = apply
        self._save = save
        self._close = close
        self._index = self._names.index(current) if current in self._names else 0
        self._applied: str | None = None
        self._status = ""

    def _highlighted(self) -> Theme | str:
        name = self._names[self._index]
        try:
            return self._load(name)
        except (ValueError, OSError) as exc:
            return str(exc)

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.markup import escape
        from rich.text import Text

        from agenthicc.tui.theme import glyphs, palette

        p, g = palette(), glyphs()
        lines: list[RenderableType] = [
            Text.from_markup(f"  [bold]Theme[/bold] [{p.muted}]· applies now, no restart[/]")
        ]
        for index, name in enumerate(self._names):
            selected = index == self._index
            mark = " (current)" if name == self._current else ""
            bar = f"[{p.accent}]{g.bar}[/]" if selected else " "
            row = f" {bar} {escape(name)}[{p.muted}]{escape(mark)}[/]"
            lines.append(Text.from_markup(row, style=p.on("accent_bg") if selected else "none"))
        lines.append(Text(""))
        preview = self._highlighted()
        if isinstance(preview, str):
            lines.append(Text(f"  {preview}", style=p.danger))
        else:
            lines.extend(theme_preview(preview))
        lines.append(Text(""))
        if self._status:
            lines.append(Text(f"  {self._status}", style=p.muted))
        lines.append(
            Text(
                f"  {g.up}{g.down} choose {g.sep} enter apply {g.sep} s save as default "
                f"{g.sep} esc close",
                style=p.muted,
            )
        )
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if key in (Key.UP, Key.DOWN) and self._names:
            self._index = (self._index + (-1 if key is Key.UP else 1)) % len(self._names)
            self._status = ""
        elif key is Key.ENTER and self._names:
            chosen = self._highlighted()
            if isinstance(chosen, str):
                self._status = chosen
            else:
                self._apply(chosen)
                self._applied = chosen.name
                self._current = chosen.name
                self._status = f"Using {chosen.name}. Press s to keep it as your default."
        elif key is Key.CHAR and ch == "s" and self._names:
            name = self._applied or self._names[self._index]
            self._status = self._save(name)
        elif key is Key.ESC:
            self._close()
        return True
