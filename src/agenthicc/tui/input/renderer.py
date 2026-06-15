"""Input rendering helpers used by the reactive TUI.

``build_prompt`` — produce the ANSI prompt string for the workspace composer.
``show_exit_hint`` — print the --resume / --continue hint on clean exit.
"""
from __future__ import annotations

import sys
from typing import Any

PROMPT_CHAR = "❯"
CURSOR_CHAR = "▌"
_INDENT = "  "


def _truncate(text: str, max_cols: int) -> str:
    """Truncate plain-text *text* to *max_cols* visible columns."""
    visible = 0
    in_esc = False
    for i, ch in enumerate(text):
        if ch == "\x1b":
            in_esc = True
        elif in_esc and ch == "m":
            in_esc = False
        elif not in_esc:
            visible += 1
            if visible > max_cols:
                return text[:i] + "\x1b[0m"
    return text


def build_prompt(
    buf: list[str],
    cursor: int,
    mention_suffix: str = "",
    in_trigger: bool = False,
) -> str:
    """Return the ANSI prompt string (no trailing newline).

    Lines are joined with ``\\n\\r`` so each line starts at column 0 when
    written to the terminal.  The ``▌`` cursor is placed at ``cursor``.
    """
    if in_trigger:
        content = "".join(buf) + mention_suffix + f"\x1b[1m{CURSOR_CHAR}\x1b[0m"
        return f"\x1b[1;32m{PROMPT_CHAR}\x1b[0m {content}"

    raw_lines: list[list[str]] = []
    current: list[str] = []
    for ch in buf:
        if ch == "\n":
            raw_lines.append(current)
            current = []
        else:
            current.append(ch)
    raw_lines.append(current)

    cumulative = 0
    cursor_line = len(raw_lines) - 1
    cursor_col = len(raw_lines[-1])
    for i, ln in enumerate(raw_lines):
        if cumulative + len(ln) >= cursor:
            cursor_line = i
            cursor_col = cursor - cumulative
            break
        cumulative += len(ln) + 1

    rendered: list[str] = []
    for i, ln in enumerate(raw_lines):
        if i == cursor_line:
            col = cursor_col
            line_text = (
                "".join(ln[:col])
                + f"\x1b[1m{CURSOR_CHAR}\x1b[0m"
                + "".join(ln[col:])
            )
        else:
            line_text = "".join(ln)
        prefix = f"\x1b[1;32m{PROMPT_CHAR}\x1b[0m " if i == 0 else _INDENT
        rendered.append(prefix + line_text)

    return "\n\r".join(rendered)


def show_exit_hint(resume_id: str = "", out: Any = None) -> None:
    """Print the --resume / --continue hint below the input bar on exit."""
    import shutil  # noqa: PLC0415
    out = out or sys.stdout
    cols = shutil.get_terminal_size((80, 24)).columns
    border = "\x1b[2m" + "─" * cols + "\x1b[0m"
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
