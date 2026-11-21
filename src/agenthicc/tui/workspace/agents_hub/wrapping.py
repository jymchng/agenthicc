"""Wrapping for the agents hub: text is broken into lines, never cut off.

The hub draws into a fixed-width panel, and a long line used to be cropped at
the panel's edge, so a command in an approval request, an agent's reply or an
option of a question lost its end with nothing to say so. Everything that
carries content now goes through :func:`wrap_cells` and :func:`hanging`:

* the text is broken at spaces into lines of at most the panel's width, counted
  in terminal cells (wide characters take two);
* explicit line breaks stay, and blank lines between paragraphs stay;
* a word longer than a line (a path, a URL) is broken after a ``/``, ``-``,
  ``_`` or ``.`` where one is near the end of the line, and anywhere else only
  when it has to be;
* a block capped at a number of lines says how many it left out, in a line of
  its own, so a shortened block is never mistaken for a whole one.

Headings, list rows and key hints are fixed-column chrome and keep their
single line and ellipsis; this module is for content.
"""

from __future__ import annotations

from rich.cells import cell_len, set_cell_size
from rich.style import Style
from rich.text import Text

from agenthicc.tui.theme import glyphs, palette

__all__ = ["MIN_BODY_CELLS", "fold_word", "hanging", "wrap_cells"]

#: The narrowest body a wrapped block is given. Below it the hanging indent
#: gives way, so a very narrow panel still shows whole words.
MIN_BODY_CELLS = 12

#: Where an overlong word may be broken: after one of these.
_BREAK_AFTER = "/-_.,:;=&?#"


def fold_word(word: str, width: int) -> list[str]:
    """*word* in pieces of at most *width* cells, broken after ``/``, ``-``… when near the end."""
    width = max(1, width)
    pieces: list[str] = []
    rest = word
    while cell_len(rest) > width:
        head = set_cell_size(rest, width)
        # Wide characters can make set_cell_size pad; keep only whole characters.
        head = head.rstrip(" ") if cell_len(head.rstrip(" ")) <= width else head
        cut = len(head)
        # Prefer a break after a separator in the last third of the piece.
        floor = max(1, cut - max(3, cut // 3))
        for index in range(cut - 1, floor - 1, -1):
            if head[index - 1] in _BREAK_AFTER:
                cut = index
                break
        if cut <= 0:
            cut = 1
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def _wrap_line(line: str, width: int) -> list[str]:
    """One logical line broken at spaces into lines of at most *width* cells."""
    words = line.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if cell_len(word) > width:
            if current:
                lines.append(current)
                current = ""
            *full, tail = fold_word(word, width)
            lines.extend(full)
            current = tail
            continue
        candidate = f"{current} {word}" if current else word
        if cell_len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def wrap_cells(text: str, width: int) -> list[str]:
    """*text* as lines of at most *width* cells; line breaks and blank lines are kept.

    Runs of spaces inside a line collapse to one, as every hub line already did.
    Nothing is dropped: the words of *text*, in order, are the words of the
    result.
    """
    width = max(1, width)
    lines: list[str] = []
    for logical in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.extend(_wrap_line(logical, width))
    # A trailing run of blank lines carries nothing.
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines


def _more_line(hidden: int, hang: int, width: int) -> Text:
    """``… 3 more lines``: what a capped block left out."""
    g, p = glyphs(), palette()
    noun = "line" if hidden == 1 else "lines"
    text = Text(no_wrap=True, overflow="crop")
    text.append(" " * min(hang, max(0, width - 16)) + f"{g.ellipsis} {hidden} more {noun}")
    text.stylize(p.muted)
    return text


def hanging(
    prefix: Text | str,
    body: str,
    width: int,
    *,
    hang: int | None = None,
    style: str | Style = "",
    max_lines: int = 0,
) -> list[Text]:
    """*body* wrapped to *width* after *prefix*, continuation lines indented by *hang*.

    ``prefix`` is the first line's lead (a status glyph and a role, a label);
    *hang* is how far the continuation lines are indented, by default the width
    of the prefix, so the text reads as one column. On a panel too narrow for
    that, the indent shrinks until the column holds :data:`MIN_BODY_CELLS`.

    With *max_lines*, a longer block is cut to that many lines and followed by
    a ``… N more lines`` line: the count of lines cut, never a guess.
    """
    lead = prefix.copy() if isinstance(prefix, Text) else Text(prefix)
    if cell_len(lead.plain) > width:
        lead.truncate(width, overflow="crop")  # a lead wider than the panel is the one thing cut
    lead_width = cell_len(lead.plain)
    indent = lead_width if hang is None else hang
    # A narrow panel shrinks the indent before it shrinks the words.
    indent = max(0, min(indent, width - MIN_BODY_CELLS))
    room = width - max(indent, lead_width)
    lines: list[Text]
    if room >= MIN_BODY_CELLS:
        chunks = wrap_cells(body, room)
        first = lead.copy()
        first.append(chunks[0], style=style)
        lines = [first]
        rest = chunks[1:]
    else:
        # The lead leaves no room beside it: it stands alone and the body follows.
        lines = [lead.copy()]
        rest = wrap_cells(body, max(1, width - indent))
    for chunk in rest:
        shown = Text(" " * indent) if chunk else Text("")
        shown.append(chunk, style=style)
        lines.append(shown)
    for line in lines:
        line.no_wrap = True
        line.overflow = "crop"
    if max_lines and len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = [*lines[:max_lines], _more_line(hidden, indent, width)]
    return lines
