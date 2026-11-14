"""One picker component for every list the user chooses from (TUX-05 §2.1).

The ``/`` and ``@`` trigger pickers, the Ctrl+P command palette and the
``/model``, ``/mode`` and ``/agent`` pickers all draw their rows with
:class:`Picker`. Every colour comes from the active theme's tokens and every
non-ASCII character from its glyph set::

     ─ Suggested ──────────────────────────────────────────────
   ▌ /compact        Summarise the conversation to free context      ctrl+x c
     /config         Show or change configuration
                                                         1 of 2 ·  ↑↓ tab ⏎

- The selected row has an ``accent`` bar and an ``accent_bg`` background.
- The characters the query matched are bold in ``accent``.
- Key bindings are right-aligned in ``muted``.
- A muted rule heads each group.
- Descriptions wrap with a hanging indent, at most two rows; below 60
  columns they go on their own row.
- A row that cannot run now is dimmed, with the reason on its second row.
- The footer shows the position (``N of M``) and the keys.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from rich.cells import cell_len
from rich.text import Text

from agenthicc.mentions.matcher import match_positions
from agenthicc.tui.theme import glyphs, palette

if TYPE_CHECKING:
    from agenthicc.tui.trigger import MatchItem

__all__ = ["NARROW_WIDTH", "Picker", "PickerRow", "picker_height", "rows_from_matches"]

T = TypeVar("T")

#: Below this width a description goes on its own row.
NARROW_WIDTH = 60
#: The most rows a picker's list uses.
MAX_ROWS = 12
_LABEL_MAX = 28
_DETAIL_ROWS = 2


@dataclass(frozen=True)
class PickerRow(Generic[T]):
    """One choosable row; *value* is what the caller gets back."""

    value: T
    label: str
    detail: str = ""
    group: str = ""
    glyph: str = ""
    keys: str = ""
    #: Why the row cannot be chosen now; the row is dimmed and shows it.
    disabled: str = ""
    #: The label is a path: its directory is muted, its filename plain.
    path: bool = False


def rows_from_matches(items: Sequence[MatchItem]) -> list[PickerRow[MatchItem]]:
    """Trigger matches as picker rows (the trigger picker's adapter)."""
    return [
        PickerRow(
            value=item,
            label=item.label,
            detail=item.detail,
            group=item.group,
            glyph=item.glyph,
            keys=item.keys,
            disabled=item.disabled,
            path=item.path,
        )
        for item in items
    ]


def picker_height(terminal_rows: int) -> int:
    """List rows for a terminal *terminal_rows* tall: ``min(12, rows / 3)``, at least 3."""
    return max(3, min(MAX_ROWS, terminal_rows // 3))


def _clip(text: str, width: int) -> str:
    """*text* cut to *width* cells, ending in an ellipsis when cut."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    ellipsis = glyphs().ellipsis
    room = max(0, width - cell_len(ellipsis))
    out = ""
    for ch in text:
        if cell_len(out + ch) > room:
            break
        out += ch
    return out + ellipsis if cell_len(out + ellipsis) <= width else out


def _wrap(text: str, width: int, rows: int) -> list[str]:
    """*text* wrapped to *width*, at most *rows* lines (the last one clipped)."""
    if not text:
        return []
    if width < 4:
        return [_clip(text, max(width, 0))]
    lines = textwrap.wrap(text, width=width, break_long_words=True) or [""]
    if len(lines) > rows:
        rest = " ".join(lines[rows - 1 :])
        lines = [*lines[: rows - 1], _clip(rest, width)]
    return lines


class Picker(Generic[T]):
    """Rows, the selection, the scroll position and the query being matched."""

    def __init__(
        self,
        rows: Sequence[PickerRow[T]] = (),
        *,
        query: str = "",
        footer_keys: str = "",
        empty: str = "No matches",
    ) -> None:
        self.rows: list[PickerRow[T]] = list(rows)
        self.query = query
        self.selected = 0
        self.scroll = 0
        #: Rows the last :meth:`render` showed (a page, for PageUp/PageDown).
        self.visible = 0
        self.footer_keys = footer_keys
        self.empty = empty

    # ── state ─────────────────────────────────────────────────────────────────

    def set_rows(self, rows: Sequence[PickerRow[T]], *, query: str = "") -> None:
        """Replace the rows (a new query): the first row is selected."""
        self.rows = list(rows)
        self.query = query
        self.selected = 0
        self.scroll = 0

    def move(self, step: int) -> None:
        """Move the selection by *step* rows, wrapping around."""
        if self.rows:
            self.selected = (self.selected + step) % len(self.rows)

    @property
    def current(self) -> PickerRow[T] | None:
        """The selected row, or ``None`` when there are no rows."""
        if not self.rows:
            return None
        return self.rows[min(self.selected, len(self.rows) - 1)]

    # ── layout ────────────────────────────────────────────────────────────────

    def render(self, width: int, height: int = MAX_ROWS) -> list[Text]:
        """The list (at most *height* rows, headings included) and the footer.

        No returned line is wider than *width* cells.
        """
        width = max(width, 20)
        if not self.rows:
            return [Text(_clip(f"   {self.empty}", width), style=palette().muted)]
        self.selected = min(max(self.selected, 0), len(self.rows) - 1)
        label_w = self._label_width(width)
        glyph_w = max((cell_len(row.glyph) for row in self.rows), default=0)
        glyph_w = glyph_w + 1 if glyph_w else 0
        keys_w = max((cell_len(row.keys) for row in self.rows), default=0)
        # Only the rows near the window are laid out: a listing can hold every
        # file of a project, and a keystroke redraws it.
        cache: dict[int, list[Text]] = {}

        def block(index: int) -> list[Text]:
            lines = cache.get(index)
            if lines is None:
                lines = self._row_lines(
                    self.rows[index], index == self.selected, width, label_w, glyph_w, keys_w
                )
                cache[index] = lines
            return lines

        self._clamp_scroll(block, height)
        lines: list[Text] = []
        previous_group: str | None = None
        last_shown = self.scroll - 1
        for index in range(self.scroll, len(self.rows)):
            row = self.rows[index]
            heading = (
                [self._heading(row.group, width)]
                if row.group and row.group != previous_group
                else []
            )
            rows_block = [*heading, *block(index)]
            if lines and len(lines) + len(rows_block) > height:
                break
            lines.extend(rows_block)
            previous_group = row.group
            last_shown = index
        self.visible = last_shown - self.scroll + 1
        lines.append(
            self._footer(
                width,
                more_above=self.scroll > 0,
                more_below=last_shown < len(self.rows) - 1,
            )
        )
        return lines

    def _label_width(self, width: int) -> int:
        longest = max((cell_len(row.label) for row in self.rows), default=0)
        if width < NARROW_WIDTH:
            return min(longest, width - 8)
        return min(longest, _LABEL_MAX, width // 2)

    def _clamp_scroll(self, block: Callable[[int], list[Text]], height: int) -> None:
        """Move the window so the selected row (and its heading) is fully visible.

        Walks back from the selected row, so the cost is one window, however
        far the selection jumped.
        """
        selected = self.selected
        if selected < self.scroll:
            self.scroll = selected
            return
        used = 0
        changes = 0
        first = selected
        for index in range(selected, self.scroll - 1, -1):
            used += len(block(index))
            group = self.rows[index].group
            if index < selected:
                following = self.rows[index + 1].group
                changes += 1 if following and following != group else 0
            if used + changes + (1 if group else 0) > height:
                break
            first = index
        self.scroll = first

    def _heading(self, group: str, width: int) -> Text:
        rule = glyphs().rule
        head = f"   {rule} {group} "
        return Text(
            _clip(head + rule * max(0, width - cell_len(head) - 1), width), style=palette().muted
        )

    def _footer(self, width: int, *, more_above: bool, more_below: bool) -> Text:
        g = glyphs()
        marks = (g.up if more_above else "") + (g.down if more_below else "")
        position = f"{self.selected + 1} of {len(self.rows)}"
        parts = [part for part in (marks, position, self.footer_keys) if part]
        footer = f" {g.sep} ".join(parts)
        footer = _clip(footer, width - 1)
        return Text(" " * max(0, width - 1 - cell_len(footer)) + footer, style=palette().muted)

    def _label_text(self, row: PickerRow[T], width: int, muted: bool) -> Text:
        p = palette()
        label = _clip(row.label, width)
        text = Text(label)
        if row.path:
            cut = label.rstrip("/").rfind("/") + 1
            if cut:
                text.stylize(p.muted, 0, cut)
        elif muted:
            text.stylize(p.muted)
        if self.query and not row.disabled:
            for position in match_positions(self.query, label):
                text.stylize(f"bold {p.accent}", position, position + 1)
        return text

    def _row_lines(
        self,
        row: PickerRow[T],
        selected: bool,
        width: int,
        label_w: int,
        glyph_w: int,
        keys_w: int,
    ) -> list[Text]:
        p, g = palette(), glyphs()
        dim = bool(row.disabled)
        prefix = Text(" ")
        prefix.append(g.bar if selected else " ", style=p.accent if selected else "none")
        prefix.append(" ")
        if glyph_w:
            prefix.append(row.glyph.ljust(glyph_w - 1) + " ", style=p.muted)
        indent = cell_len(prefix.plain)
        keys_room = keys_w + 2 if keys_w else 0
        narrow = width < NARROW_WIDTH
        detail_col = indent + label_w + 2
        detail_w = width - detail_col - keys_room - 1
        if detail_w < 12:
            narrow = True
        label_room = (width - indent - keys_room - 1) if narrow else label_w
        first = prefix.copy()
        first.append_text(self._label_text(row, max(1, label_room), dim))
        details: list[str]
        if narrow:
            hang = indent
            details = _wrap(row.detail, width - hang - 1, 1 if dim else _DETAIL_ROWS)
            lines = [first]
            lines.extend(Text(" " * hang + chunk, style=p.muted) for chunk in details)
        else:
            hang = detail_col
            details = _wrap(row.detail, detail_w, 1 if dim else _DETAIL_ROWS)
            if details:
                first.append(" " * max(0, detail_col - cell_len(first.plain)))
                first.append(details[0], style=p.muted if dim else "none")
            lines = [first]
            lines.extend(
                Text(" " * hang + chunk, style=p.muted if dim else "none") for chunk in details[1:]
            )
        if row.keys:
            pad = width - 1 - cell_len(first.plain) - cell_len(row.keys)
            if pad >= 2:
                first.append(" " * pad)
                first.append(row.keys, style=p.muted)
        if dim:
            reason = _clip(row.disabled, width - hang - 1)
            lines.append(Text(" " * hang + reason, style=f"italic {p.muted}"))
            for line in lines:
                line.stylize("dim")
        for line in lines:
            line.no_wrap = True
            line.overflow = "crop"
            line.truncate(width)
            if selected:
                line.pad_right(max(0, width - cell_len(line.plain)))
                line.stylize(p.on("accent_bg"))
        return lines
