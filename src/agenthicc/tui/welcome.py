"""Startup welcome screen for the agenthicc TUI.

Call ``print_welcome(console, model, cwd)`` once before the Live block starts
so the panel lands in the normal scroll buffer.
"""
from __future__ import annotations

from typing import Any

from rich import box
from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

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

def _left_column(model: str, cwd: str) -> Any:
    # Mascot + title: line-for-line alignment.
    # Fixed width on the mascot column prevents Rich from squishing it.
    hero = Table.grid(padding=(0, 2, 0, 0))
    hero.add_column(no_wrap=True, width=9)   # mascot — widest line is 8 chars
    hero.add_column(no_wrap=True)            # title / subtitle

    hero.add_row(
        Text(_MASCOT_LINES[0], style="bold bright_white"),
        Text("AGENTHICC", style="bold white"),
    )
    hero.add_row(
        Text(_MASCOT_LINES[1], style="bold bright_white"),
        Text("state-driven agent operating system", style="dim"),
    )
    hero.add_row(
        Text(_MASCOT_LINES[2], style="bold bright_white"),
        Text(""),
    )

    parts: list[Any] = [
        hero,
        Text(""),
        Text("Welcome back!", style="bold white"),
        Text(""),
    ]

    if model:
        meta = Text()
        meta.append("Model  ", style="dim")
        meta.append(model, style="dim cyan")
        parts.append(meta)

    if cwd:
        wd = Text()
        wd.append("Dir    ", style="dim")
        wd.append(str(cwd), style="dim")
        parts.append(wd)

    return Group(*parts)


# ── right column ──────────────────────────────────────────────────────────────

def _right_column() -> Any:
    parts: list[Any] = [
        Text("Tips for getting started", style="bold white"),
        Text(""),
        Text.assemble(
            ("Run ", "dim"),
            ("/init", "cyan"),
            (" to create a ", "dim"),
            ("AGENTS.md", "cyan"),
            (" file with instructions for agenthicc", "dim"),
        ),
        Text(""),
        Rule(style="dim"),
        Text(""),
        Text("What's new", style="bold white"),
        Text(""),
    ]

    for entry in _CHANGELOG:
        line = Text()
        line.append("• ", style="cyan")
        line.append(entry, style="dim")
        parts.append(line)

    return Group(*parts)


# ── public API ────────────────────────────────────────────────────────────────

def render_welcome(model: str = "", cwd: str = "") -> Align:
    """Return a Rich renderable for the startup welcome screen."""
    body = Table.grid(padding=(0, 4, 0, 0))
    body.add_column(min_width=46)   # left — mascot + metadata
    body.add_column()               # right — tips + changelog (takes remainder)
    body.add_row(_left_column(model, cwd), _right_column())

    panel = Panel(
        body,
        box=box.ROUNDED,
        border_style="steel_blue1",
        padding=(1, 3),
        expand=True,
    )
    return Align.center(panel)


def print_welcome(console: Any, model: str = "", cwd: str = "") -> None:
    """Print the welcome panel to *console* (call before the Live block starts)."""
    console.print()
    console.print(render_welcome(model=model, cwd=cwd))
    console.print()
