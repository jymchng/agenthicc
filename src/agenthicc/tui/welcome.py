"""Startup welcome screen for the agenthicc TUI.

Call ``print_welcome(console, model, cwd)`` once before the Live block starts
so the panel lands in the normal scroll buffer.
"""
from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from rich import box
from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console, RenderableType

# ── brand ─────────────────────────────────────────────────────────────────────

_MASCOT_LINES = (
    r" /\_/\ ",
    r"( ◕.◕ )",
    r" > ^ < ",
)

_CHANGELOG = [
    "Bug fixes and reliability improvements",
    "Added fallbackModel setting",
    "Added glob pattern support in deny rules",
    "/release-notes for more",
]


# ── left column ───────────────────────────────────────────────────────────────

def _left_column(model: str, cwd: str, left_w: int = 46) -> "RenderableType":
    # Mascot + title: line-for-line alignment.
    # Fixed width on the mascot column prevents Rich from squishing it.
    hero = Table.grid(padding=(0, 2, 0, 0))
    hero.add_column(no_wrap=True, width=9)   # mascot — widest line is 8 chars
    hero.add_column()                        # title / subtitle — wraps on narrow terminals

    hero.add_row(
        Text(_MASCOT_LINES[0], style="bold bright_yellow"),
        Text("AGENTHICC", style="bold yellow"),
    )
    hero.add_row(
        Text(_MASCOT_LINES[1], style="bold bright_yellow"),
        Text("state-driven agent operating system", style="dim"),
    )
    hero.add_row(
        Text(_MASCOT_LINES[2], style="bold bright_yellow"),
        Text(""),
    )

    parts: list[RenderableType] = [
        hero,
        Text(""),
        Text("Welcome back!", style="bold yellow"),
        Text(""),
    ]

    if model:
        meta = Text()
        meta.append("Model  ", style="dim")
        meta.append(model, style="dim yellow")
        parts.append(meta)

    if cwd:
        _label = "Dir    "   # 7 chars
        _path  = str(cwd)
        _avail = left_w - len(_label)
        if len(_path) > _avail:
            _path = _path[:max(4, _avail - 1)] + "…"
        wd = Text()
        wd.append(_label, style="dim")
        wd.append(_path, style="dim")
        parts.append(wd)

    return Group(*parts)


# ── right column ──────────────────────────────────────────────────────────────

def _right_column() -> "RenderableType":
    parts: list[RenderableType] = [
        Text("Tips for getting started", style="bold yellow"),
        Text(""),
        Text.assemble(
            ("Run ", "dim"),
            ("/init", "yellow"),
            (" to create a ", "dim"),
            ("AGENTS.md", "yellow"),
            (" file with instructions for agenthicc", "dim"),
        ),
        Text(""),
        Rule(style="dim"),
        Text(""),
        Text("What's new", style="bold yellow"),
        Text(""),
    ]

    for entry in _CHANGELOG:
        line = Text()
        line.append("• ", style="yellow")
        line.append(entry, style="dim")
        parts.append(line)

    return Group(*parts)


# ── public API ────────────────────────────────────────────────────────────────

def render_welcome(model: str = "", cwd: str = "") -> Align:
    """Return a Rich renderable for the startup welcome screen."""
    # Compute column widths from the live terminal size.
    # Panel overhead = border(2) + padding(3+3) = 8 cols.
    # Body separator padding = 4 cols between the two columns.
    # Left column gets ~40 % of the usable space, clamped to [32, 48].
    term_cols = shutil.get_terminal_size((80, 24)).columns
    usable    = max(60, term_cols - 8)
    left_w    = min(48, max(32, (usable - 4) * 2 // 5))

    body = Table.grid(padding=(0, 4, 0, 0))
    body.add_column(width=left_w)   # left — exact computed width
    body.add_column()               # right — takes remainder
    body.add_row(_left_column(model, cwd, left_w), _right_column())

    panel = Panel(
        body,
        box=box.ROUNDED,
        border_style="yellow",
        padding=(1, 3),
        expand=True,
    )
    return Align.center(panel)


def print_welcome(console: "Console", model: str = "", cwd: str = "") -> None:
    """Print the welcome panel to *console* (call before the Live block starts)."""
    console.print(render_welcome(model=model, cwd=cwd))
