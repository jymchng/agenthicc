"""Input rendering helpers used by the reactive TUI.

``build_prompt`` — the prompt as a Rich ``Text`` (the pickers draw it above
their matches).
``show_exit_hint`` — print the --resume / --continue hint on clean exit.

Colours come from the active theme's tokens and the chevron, cursor and rule
from its glyph set (TUX-01).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from rich.text import Text

#: The Unicode chevron and cursor; renderers use ``glyphs()`` for the active set.
PROMPT_CHAR = "❯"
CURSOR_CHAR = "▌"
_INDENT = "  "


def build_prompt(buf: list[str], cursor: int) -> Text:
    """Return the prompt: chevron, text and the cursor glyph at *cursor*.

    Lines of a multi-line prompt after the first are indented to the text.
    """
    from rich.text import Text

    from agenthicc.tui.theme import glyphs, style

    g = glyphs()
    chevron = style("accent", bold=True)
    lines = "".join(buf).split("\n")
    prompt = Text()
    offset = 0
    for index, line in enumerate(lines):
        if index:
            prompt.append("\n")
        if index == 0:
            prompt.append(g.prompt, chevron)
            prompt.append(" ")
        else:
            prompt.append(_INDENT)
        column = cursor - offset
        if 0 <= column <= len(line):
            prompt.append(line[:column])
            prompt.append(g.cursor, "bold")
            prompt.append(line[column:])
            cursor = -1  # drawn once, on the first line that holds it
        else:
            prompt.append(line)
        offset += len(line) + 1
    return prompt


def show_exit_hint(resume_id: str = "", out: TextIO | None = None) -> None:
    """Print the --resume / --continue hint below the input bar on exit."""
    import shutil

    from agenthicc.tui.theme import glyphs

    out = out or sys.stdout
    cols = shutil.get_terminal_size((80, 24)).columns
    border = "\x1b[2m" + glyphs().rule * cols + "\x1b[0m"
    if resume_id:
        hints = [
            f"  \x1b[2mTo resume:\x1b[0m \x1b[1magenthicc --resume {resume_id}\x1b[0m",
            "  \x1b[2mOr in the same directory:\x1b[0m \x1b[1magenthicc --continue\x1b[0m",
        ]
    else:
        hints = [
            "  \x1b[2mTo resume:\x1b[0m \x1b[1magenthicc --continue\x1b[0m"
            "\x1b[2m  (in the same directory)\x1b[0m",
        ]
    text = "\n\r" + border
    for h in hints:
        text += "\n\r" + h
    text += "\n\n"
    out.write(text)
    out.flush()
