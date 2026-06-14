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


def get_mode_str(mode_manager: Any | None) -> str:
    """Return the plain-text mode label for the given ModeManager (no ANSI/markup).

    Both renderers call this and apply their own styling, so the return value
    is always unstyled text.
    """
    if mode_manager is None:
        return f"⏵⏵ Auto  (shift+tab to cycle)"
    m = mode_manager.active
    if m.name == "Auto":
        return "⏵⏵ Auto  (shift+tab to cycle)"
    return f"⏵⏵ {m.badge} {m.name}  (shift+tab to cycle)"


# ── Rich markup ───────────────────────────────────────────────────────────────


def prompt_markup(typed: str, cols: int) -> str:
    """Rich markup prompt line: ``❯ <text>▌``."""
    display = typed[:cols - 7] + "…" if len(typed) > cols - 4 else typed
    return (
        f"[bold green]{PROMPT_CHAR}[/bold green] "
        f"{display}[bold]{CURSOR_CHAR}[/bold]"
    )


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
    """ANSI prompt line: bold-green ❯, buffer text, bold ▌ cursor.

    In trigger mode (picker open) ▌ follows the trigger fragment so the user
    always sees the cursor at the end of the current input.
    In normal mode ▌ is placed at ``cursor`` within ``buf``; when ``cursor``
    is ``None`` it defaults to the end of the buffer.

    The real terminal cursor should be hidden (``\\x1b[?25l``) before calling
    this so the OS cursor and ▌ do not both appear at the same spot.
    """
    pos = len(buf) if cursor is None else cursor
    if in_trigger:
        content = "".join(buf) + mention_suffix + f"\x1b[1m{CURSOR_CHAR}\x1b[0m"
    else:
        pre = "".join(buf[:pos])
        post = "".join(buf[pos:])
        content = f"{pre}\x1b[1m{CURSOR_CHAR}\x1b[0m{post}"
    return f"\x1b[1;32m{PROMPT_CHAR}\x1b[0m {content}"


def footer_ansi(mode_str: str, cols: int) -> tuple[str, str]:
    """Return ``(border_line, mode_text_line)`` as raw ANSI escape strings."""
    border = "\x1b[2m" + "─" * cols + "\x1b[0m"
    mode = f"  \x1b[2m{mode_str}\x1b[0m"
    return border, mode
