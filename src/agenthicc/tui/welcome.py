"""The start-up header (TUX-03).

Printed once into the scroll buffer, at most four rows. Normally three:

```text
 ◆ agenthicc 0.42.0                                   what's new: /release-notes
   opus-4.8 · anthropic · Safe mode
   ~/py…/agenthicc  (main)
```

- **Line 1** names the product and the installed version. ``what's new:
  /release-notes`` is right-aligned on it, and only right after an upgrade.
- **Line 2** is the model, the provider and the mode.
- **Line 3** is the working directory, home-abbreviated and cut in the middle
  to fit, with the git branch.

The first run on a machine also shows the mascot and the tagline beside them
(and ``[ui] banner = "full"`` always does). Under 60 columns lines 2 and 3 merge
into one paragraph of at most two rows. ``[ui] banner = "off"`` prints nothing:
the caller does not call :func:`print_welcome`.

Every colour is a theme token and every non-ASCII character a glyph from
:mod:`agenthicc.tui.theme`; the mascot is plain ASCII. Nothing here loads
anything from the network or the disk: the caller gathers the facts
(:mod:`agenthicc.runners.first_run`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Group
from rich.text import Text

from agenthicc.tui.theme import glyphs, style

if TYPE_CHECKING:
    from rich.console import Console

__all__ = [
    "MASCOT",
    "TAGLINE",
    "WelcomeInfo",
    "home_relative",
    "middle_truncate",
    "print_welcome",
    "render_welcome",
]

#: Plain ASCII, so it draws the same under every glyph set and font.
MASCOT = (
    r" /\_/\ ",
    r"( o.o )",
    r" > ^ < ",
)
TAGLINE = "state-driven agent operating system"

#: Below this many columns lines 2 and 3 merge and wrap, and the mascot goes.
NARROW_BELOW = 60
_INDENT = 3
_MASCOT_GAP = 2
#: The most rows the merged paragraph of a narrow terminal may use.
_NARROW_ROWS = 2
_HINT = ("what's new: ", "/release-notes")
_MODE_TOKENS = {"safe": "mode_safe", "plan": "mode_plan", "yolo": "mode_yolo"}


@dataclass(frozen=True, slots=True)
class WelcomeInfo:
    """What the header says."""

    version: str
    #: The model's short name (``opus-4.8``), without its provider.
    model: str
    provider: str
    #: The active mode's name (``Safe``).
    mode: str
    directory: Path
    home: Path
    branch: str = ""
    #: The user upgraded: point at ``/release-notes``.
    whats_new: bool = False
    #: Show the mascot and the tagline (the first run, or ``banner = "full"``).
    mascot: bool = False


def home_relative(path: Path, home: Path) -> str:
    """*path* with the home directory written ``~``."""
    try:
        relative = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def middle_truncate(text: str, room: int) -> str:
    """*text* cut to *room* cells, keeping its start and its last path segment.

    ``~/python_projects/agenthicc`` in 15 cells is ``~/py…/agenthicc``.
    """
    ellipsis = glyphs().ellipsis
    if cell_len(text) <= room:
        return text
    room = max(room, cell_len(ellipsis) + 1)
    head, separator, tail = text.rpartition("/")
    last = f"{separator}{tail}" if separator else text
    if cell_len(last) + cell_len(ellipsis) >= room:
        return ellipsis + last[-(room - cell_len(ellipsis)) :]
    keep = room - cell_len(last) - cell_len(ellipsis)
    return head[:keep] + ellipsis + last if head else last[:room]


def _mode_style(mode: str) -> str:
    return _MODE_TOKENS.get(mode.casefold(), "muted")


def _identity(info: WelcomeInfo) -> Text:
    """Line 1 without the hint: ``◆ agenthicc 0.42.0``."""
    line = Text()
    line.append(f"{glyphs().diamond} ", style("accent"))
    line.append("agenthicc", style("accent", bold=True))
    line.append(f" {info.version}", style("muted"))
    return line


def _model_line(info: WelcomeInfo) -> Text:
    sep = f" {glyphs().sep} "
    line = Text()
    line.append(info.model)
    line.append(sep, style("muted"))
    line.append(info.provider, style("muted"))
    line.append(sep, style("muted"))
    line.append(f"{info.mode} mode", style(_mode_style(info.mode)))
    return line


def _directory_line(info: WelcomeInfo, room: int) -> Text:
    branch = f"  ({info.branch})" if info.branch else ""
    line = Text()
    line.append(
        middle_truncate(home_relative(info.directory, info.home), max(8, room - len(branch)))
    )
    if branch:
        line.append(branch, style("muted"))
    return line


def _with_hint(line: Text, width: int, show: bool) -> Text:
    """*line* with ``what's new: /release-notes`` right-aligned, if it fits."""
    if not show:
        return line
    hint_width = cell_len("".join(_HINT))
    gap = width - line.cell_len - hint_width - 1
    if gap < 2:
        return line
    row = line.copy()
    row.append(" " * gap)
    row.append(_HINT[0], style("muted"))
    row.append(_HINT[1], style("accent"))
    return row


def _merged(info: WelcomeInfo, width: int) -> list[Text]:
    """Lines 2 and 3 as one paragraph of at most two rows, for a narrow terminal."""
    room = max(8, width - _INDENT)
    sep = glyphs().sep
    paragraph = _model_line(info)
    paragraph.append(f" {sep} ", style("muted"))
    paragraph.append_text(_directory_line(info, room))
    rows = list(paragraph.wrap(_measure(room), room))
    if len(rows) > _NARROW_ROWS:
        rows = rows[:_NARROW_ROWS]
        rows[-1].truncate(room - 1)
        rows[-1].append(glyphs().ellipsis)
    for row in rows:
        row.rstrip()
        if row.plain.endswith(sep):  # a wrap right after a separator leaves it dangling
            row.right_crop(len(sep))
            row.rstrip()
    return rows


_measuring: Console | None = None


def _measure(width: int) -> Console:
    """A console :meth:`Text.wrap` measures with (it needs one only for its options)."""
    global _measuring
    if _measuring is None:
        import io

        from rich.console import Console

        _measuring = Console(file=io.StringIO(), width=width, highlight=False)
    return _measuring


def render_welcome(info: WelcomeInfo, width: int) -> Group:
    """The header at *width* columns: one :class:`Text` per row, none wrapped."""
    narrow = width < NARROW_BELOW
    pad = Text(" " * _INDENT)
    rows: list[Text] = []
    if info.mascot and not narrow:
        # The mascot's rows carry the name, tagline and model; the directory
        # continues under its last row.
        lead = 1 + len(MASCOT[0]) + _MASCOT_GAP
        room = width - lead - 1
        texts = [
            _with_hint(_identity(info), room + 1, info.whats_new),
            Text(TAGLINE, style=style("muted")),
            _model_line(info),
            _directory_line(info, room),
        ]
        for index, text in enumerate(texts):
            art = (
                Text(f" {MASCOT[index]}", style("frame", bold=True))
                if index < 3
                else Text(" " * (1 + len(MASCOT[0])))
            )
            row = Text.assemble(art, " " * _MASCOT_GAP)
            row.append_text(text)
            rows.append(row)
    else:
        rows.append(Text(" ").append_text(_with_hint(_identity(info), width - 1, info.whats_new)))
        if info.mascot:
            rows.append(Text.assemble(pad, Text(TAGLINE, style=style("muted"))))
        if narrow:
            rows.extend(Text.assemble(pad, piece) for piece in _merged(info, width))
        else:
            rows.append(Text.assemble(pad, _model_line(info)))
            rows.append(Text.assemble(pad, _directory_line(info, width - _INDENT - 1)))
    for row in rows:
        row.no_wrap = True
        row.overflow = "ellipsis"
    return Group(*rows)


def print_welcome(console: Console, info: WelcomeInfo) -> None:
    """Print the header to *console* (a blank row follows, as after any block)."""
    console.print(render_welcome(info, console.width), highlight=False)
    console.print()
