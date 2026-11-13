"""The Ctrl+P command palette (TUX-05 §2.4).

A centred frame (60 % of the width, clamped to 60–100 columns) with a search
field over every command, every composer key action that has a binding, and
every theme, grouped as in the ``/`` picker. Enter runs the item; a command
whose arguments have completions (``/workflow``) lists them in the same frame.

The frame is drawn here with the theme's box glyphs until the shared
``Dialog`` component (TUX-07) is available.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.cells import cell_len

from agenthicc.commands.registry import command_rank
from agenthicc.mentions.matcher import fuzzy_positions
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.trigger import MatchItem, TriggerContext
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.workspace.picker import Picker, PickerRow, picker_height

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import RenderableType
    from rich.text import Text

    from agenthicc.tui.keymap import Keymap
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger

__all__ = ["CommandPaletteOverlay", "PaletteItem", "palette_width"]

PaletteKind = Literal["command", "action", "theme", "insert"]

KEYS_GROUP = "Keys"
THEMES_GROUP = "Themes"


@dataclass(frozen=True)
class PaletteItem:
    """What a palette row runs.

    ``command``: submit *text* (``/agents``, ``/workflow resume``) as typed;
    ``action``: run the key action named *text*; ``theme``: apply the theme
    *text*; ``insert``: put *text* in the prompt for its required argument.
    """

    kind: PaletteKind
    text: str


def palette_width(columns: int) -> int:
    """60 % of *columns*, clamped to 60–100 and never wider than the terminal."""
    return min(columns, max(60, min(100, columns * 60 // 100)))


def _text_rank(query: str, label: str) -> int | None:
    """Tier of a non-command row: ``2`` word prefix, ``4`` substring or fuzzy."""
    q = query.casefold()
    words = "".join(ch if ch.isalnum() else " " for ch in label.casefold()).split()
    if any(word.startswith(q) for word in words):
        return 2
    if q in label.casefold() or fuzzy_positions(q, label) is not None:
        return 4
    return None


class CommandPaletteOverlay(Overlay):
    """Search, choose and run any command, key action or theme."""

    name = "command-palette"

    def __init__(
        self,
        *,
        commands: SlashCommandTrigger,
        cwd: Path,
        keymap: Keymap,
        actions: Mapping[str, str],
        themes: Sequence[str],
        run: Callable[[PaletteItem], None],
        close: Callable[[], None],
        busy: bool = False,
    ) -> None:
        from agenthicc.tui.theme import glyphs

        g = glyphs()
        self._commands = commands
        self._ctx = TriggerContext(cwd=cwd, busy=busy)
        self._keymap = keymap
        self._actions = actions
        self._themes = tuple(themes)
        self._run = run
        self._close = close
        self.query = ""
        #: The command whose arguments are listed, or ``""``.
        self.command = ""
        self.status = ""
        self.picker: Picker[PaletteItem] = Picker(
            footer_keys=f"{g.up}{g.down} {g.sep} enter run {g.sep} esc close"
        )
        size = shutil.get_terminal_size((80, 24))
        self._columns = size.columns
        self._height = picker_height(size.lines) + 2
        self._refresh()

    # ── rows ──────────────────────────────────────────────────────────────────

    def _command_rows(self, matches: list[MatchItem]) -> list[PickerRow[PaletteItem]]:
        return [
            PickerRow(
                value=PaletteItem("command", item.value),
                label=item.label,
                detail=item.detail,
                group=item.group,
                keys=item.keys,
                disabled=item.disabled,
            )
            for item in matches
        ]

    def _action_rows(self) -> list[PickerRow[PaletteItem]]:
        rows: list[PickerRow[PaletteItem]] = []
        for name, label in self._actions.items():
            keys = self._keymap.action(name).keys
            if keys:
                rows.append(
                    PickerRow(
                        value=PaletteItem("action", name),
                        label=label,
                        group=KEYS_GROUP,
                        keys=keys[0],
                    )
                )
        return rows

    def _theme_rows(self) -> list[PickerRow[PaletteItem]]:
        return [
            PickerRow(value=PaletteItem("theme", name), label=f"Theme: {name}", group=THEMES_GROUP)
            for name in self._themes
        ]

    def _refresh(self) -> None:
        if self.command:
            fragment = f"{self.command[1:]} {self.query}"
            rows = self._command_rows(self._commands.get_matches(fragment, self._ctx))
            self.picker.set_rows(rows, query=self.query)
            return
        query = self.query.strip().removeprefix("/")
        if not query:
            rows = [
                *self._command_rows(self._commands.get_matches("", self._ctx)),
                *self._action_rows(),
                *self._theme_rows(),
            ]
            self.picker.set_rows(rows)
            return
        ranked: list[tuple[int, int, PickerRow[PaletteItem]]] = []
        for order, row in enumerate(
            self._command_rows(self._commands.get_matches(query, self._ctx))
        ):
            command = self._commands.registry_command(row.value.text)
            tier = command_rank(command, "/" + query) if command is not None else None
            ranked.append((4 if tier is None else tier, order, row))
        others = [*self._action_rows(), *self._theme_rows()]
        for order, row in enumerate(others, len(ranked)):
            tier = _text_rank(query, row.label)
            if tier is not None:
                ranked.append((tier, order, row))
        ranked.sort(key=lambda entry: entry[:2])
        self.picker.set_rows([replace_group(row) for _, _, row in ranked], query=query)

    # ── Overlay ───────────────────────────────────────────────────────────────

    def set_viewport(self, width: int, height: int) -> None:
        self._columns = max(20, width)
        self._height = max(3, min(self._height, height - 4))

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        from agenthicc.tui.input.renderer import build_prompt
        from agenthicc.tui.theme import glyphs, palette

        p, g = palette(), glyphs()
        tl, tr, bl, br, horizontal, vertical = g.box
        width = palette_width(self._columns)
        inner = width - 4
        margin = " " * max(0, (self._columns - width) // 2)
        title = f" {self.command} " if self.command else " Commands "
        top = (
            f"{margin}{tl}{horizontal}{title}{horizontal * max(0, width - 3 - cell_len(title))}{tr}"
        )
        lines: list[Text] = [Text(top, style=p.subtle)]

        def framed(body: Text) -> Text:
            body.truncate(inner)
            body.pad_right(inner - cell_len(body.plain))
            line = Text(margin)
            line.append(f"{vertical} ", style=p.subtle)
            line.append_text(body)
            line.append(f" {vertical}", style=p.subtle)
            return line

        # The composer's own prompt line: chevron, text and the block cursor.
        search = build_prompt(list(self.query), len(self.query))
        if not self.query:
            search.append(
                " type to search" if not self.command else " choose an argument", style=p.muted
            )
        lines.append(framed(search))
        lines.append(framed(Text(horizontal * inner, style=p.subtle)))
        for row in self.picker.render(inner, self._height):
            lines.append(framed(row))
        if self.status:
            lines.append(framed(Text(self.status, style=p.warning)))
        lines.append(Text(f"{margin}{bl}{horizontal * (width - 2)}{br}", style=p.subtle))
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if key in (Key.UP, Key.DOWN):
            self.picker.move(-1 if key is Key.UP else 1)
        elif key is Key.ESC:
            if self.command:
                self.command, self.query, self.status = "", "", ""
                self._refresh()
            else:
                self._close()
        elif key is Key.BACKSPACE:
            if self.query:
                self.query = self.query[:-1]
                self._refresh()
        elif key is Key.ENTER:
            self._enter()
        elif (key is Key.CHAR and ch) or key is Key.AT:
            self.query += "@" if key is Key.AT else ch
            self.status = ""
            self._refresh()
        return True

    def _enter(self) -> None:
        row = self.picker.current
        if row is None:
            return
        if row.disabled:
            self.status = row.disabled
            return
        item = row.value
        if item.kind == "command" and not self.command:
            needs = self._commands.argument_need(item.text)
            if needs == "completions":
                self.command, self.query, self.status = item.text, "", ""
                self._refresh()
                return
            if needs == "required":
                item = PaletteItem("insert", f"{item.text} ")
        self._close()
        self._run(item)


def replace_group(row: PickerRow[PaletteItem]) -> PickerRow[PaletteItem]:
    """*row* without its heading: a searched palette is one ranked list."""
    from dataclasses import replace

    return replace(row, group="")
