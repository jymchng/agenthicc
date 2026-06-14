"""Single source of truth for input area layout (PRD-06).

Both the streaming Live block (_spin in __main__.py) and the idle renderer
(_redraw in mention_input.py) derive their input area structure from this
module.  Changes to layout, prompt character, cursor indicator, or mode-line
format only need to happen here.

Two rendering families are provided:
  • ``*_markup`` functions  — Rich markup strings (streaming Live block)
  • ``*_ansi``    functions  — raw ANSI escape strings (idle _redraw)
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "CURSOR_CHAR",
    "PROMPT_CHAR",
    "footer_ansi",
    "footer_markup",
    "get_mode_str",
    "prompt_ansi",
    "prompt_markup",
]

PROMPT_CHAR = "❯"
CURSOR_CHAR = "▌"


_NEW_LINE_HINT = "  │  ctrl+j = ↵"


def get_mode_str(mode_manager: Any | None) -> str:
    """Return the plain-text mode label for the given ModeManager (no ANSI/markup).

    Both renderers call this and apply their own styling, so the return value
    is always unstyled text.  A new-line key hint is appended so users on any
    terminal know how to insert a line break.
    """
    if mode_manager is None:
        return f"⏵⏵ Auto  (shift+tab to cycle){_NEW_LINE_HINT}"
    m = mode_manager.active
    if m.name == "Auto":
        return f"⏵⏵ Auto  (shift+tab to cycle){_NEW_LINE_HINT}"
    return f"⏵⏵ {m.badge} {m.name}  (shift+tab to cycle){_NEW_LINE_HINT}"


# ── Rich markup ───────────────────────────────────────────────────────────────


def prompt_markup(typed: str, cols: int) -> str:
    """Rich markup prompt — handles multiline (``typed`` may contain ``\\n``).

    First line is prefixed with ``❯``.  Subsequent lines are indented by two
    spaces (aligning their content under the first-line text).  ``▌`` appears
    on the last line so it always stays at the end of what the user has typed.
    """
    _INDENT = "  "
    lines = typed.split("\n")
    parts: list[str] = []
    for i, line in enumerate(lines):
        is_last = i == len(lines) - 1
        content = line + (f"[bold]{CURSOR_CHAR}[/bold]" if is_last else "")
        if i == 0:
            parts.append(f"[bold green]{PROMPT_CHAR}[/bold green] {content}")
        else:
            parts.append(f"{_INDENT}{content}")
    return "\n".join(parts)


def footer_markup(mode_str: str, cols: int) -> tuple[str, str]:
    """Return ``(border_line, mode_text_line)`` as Rich markup strings."""
    border = "[dim]" + "─" * cols + "[/dim]"
    mode = f"  [dim]{mode_str}[/dim]"
    return border, mode


# ── Raw ANSI ──────────────────────────────────────────────────────────────────


def prompt_ansi(
    buf: list[str],
    mention_suffix: str,
    cursor: int | None,
    in_trigger: bool,
) -> str:
    """ANSI prompt — handles multiline (``buf`` may contain ``'\\n'`` chars).

    Lines are joined with ``\\n\\r`` so each is written at column 0.  The
    first line is prefixed with the bold-green ``❯``; subsequent lines are
    indented by two spaces so their text aligns under the first-line content.
    ``▌`` is placed at the cursor position (``cursor`` defaults to end of
    buffer).

    In trigger mode ``▌`` is appended after the trigger fragment and no line
    splitting is applied (fragments are always single-line).

    The real terminal cursor must be hidden (``\\x1b[?25l``) before calling
    this so the OS cursor and ``▌`` do not appear simultaneously.
    """
    _INDENT = "  "
    pos = len(buf) if cursor is None else cursor

    if in_trigger:
        content = "".join(buf) + mention_suffix + f"\x1b[1m{CURSOR_CHAR}\x1b[0m"
        return f"\x1b[1;32m{PROMPT_CHAR}\x1b[0m {content}"

    # Split buf into visual lines at each '\n' boundary.
    raw_lines: list[list[str]] = []
    current: list[str] = []
    for ch in buf:
        if ch == "\n":
            raw_lines.append(current)
            current = []
        else:
            current.append(ch)
    raw_lines.append(current)

    # Locate which line and column the cursor sits on.
    cumulative = 0
    cursor_line = len(raw_lines) - 1  # default: last line
    cursor_col = len(raw_lines[-1])
    for i, ln in enumerate(raw_lines):
        if cumulative + len(ln) >= pos:
            cursor_line = i
            cursor_col = pos - cumulative
            break
        cumulative += len(ln) + 1  # +1 for the '\n'

    # Render each visual line.
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


def footer_ansi(mode_str: str, cols: int) -> tuple[str, str]:
    """Return ``(border_line, mode_text_line)`` as raw ANSI escape strings."""
    border = "\x1b[2m" + "─" * cols + "\x1b[0m"
    mode = f"  \x1b[2m{mode_str}\x1b[0m"
    return border, mode
