"""Startup welcome screen for the agenthicc TUI.

Call ``print_welcome(console, model, cwd)`` once before the Live block starts
so the panel lands in the normal scroll buffer.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
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

CHANGELOG_URL = "https://agenthicc.dev/changelog.json"
_CHANGELOG_TIMEOUT_S = 5.0
_CHANGELOG_LIST_KEYS = (
    "items",
    "entries",
    "changes",
    "changelog",
    "releases",
    "whats_new",
    "updates",
)
_CHANGELOG_TEXT_KEYS = ("title", "text", "message", "description", "name")

_MASCOT_LINES = (
    r" /\_/\ ",
    r"( ◕.◕ )",
    r" > ^ < ",
)


async def fetch_changelog(url: str = CHANGELOG_URL) -> list[str]:
    """Fetch and normalize the remote changelog for the welcome panel.

    The welcome screen is non-essential startup UI.  Any transport, HTTP,
    JSON, or schema error therefore degrades to an empty list instead of
    delaying or failing session startup.

    Accepted payloads are either a JSON list or an object containing a list
    under one of ``items``, ``entries``, ``changes``, ``changelog``,
    ``releases``, or ``whats_new``.  List entries may be strings or objects
    with a useful text field such as ``title``, ``text``, ``message``, or
    ``description``.
    """
    try:
        from agenthicc.tools.http import agenthicc_http_client  # noqa: PLC0415

        async with asyncio.timeout(_CHANGELOG_TIMEOUT_S):
            async with agenthicc_http_client(
                timeout=_CHANGELOG_TIMEOUT_S,
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        return _normalize_changelog(payload)
    except Exception:  # noqa: BLE001
        return []


def _normalize_changelog(payload: object) -> list[str]:
    """Convert supported changelog JSON payloads into display lines."""
    if isinstance(payload, dict):
        for key in _CHANGELOG_LIST_KEYS:
            entries = payload.get(key)
            if isinstance(entries, list):
                return _normalize_changelog_entries(entries)
        return []
    if isinstance(payload, list):
        return _normalize_changelog_entries(payload)
    return []


def _normalize_changelog_entries(entries: list[object]) -> list[str]:
    """Recursively normalize strings and nested release/change objects."""
    result: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, dict):
            nested = next(
                (
                    value
                    for key in _CHANGELOG_LIST_KEYS
                    if isinstance(value := entry.get(key), list)
                ),
                None,
            )
            if nested is not None:
                result.extend(_normalize_changelog_entries(nested))
                continue
            text = next(
                (
                    value.strip()
                    for key in _CHANGELOG_TEXT_KEYS
                    if isinstance(value := entry.get(key), str) and value.strip()
                ),
                "",
            )
        elif isinstance(entry, list):
            result.extend(_normalize_changelog_entries(entry))
            continue
        else:
            text = ""
        if text:
            result.append(text)
    return result


# ── left column ───────────────────────────────────────────────────────────────


def _left_column(model: str, cwd: str, left_w: int = 46) -> "RenderableType":
    # Mascot + title: line-for-line alignment.
    # Fixed width on the mascot column prevents Rich from squishing it.
    hero = Table.grid(padding=(0, 2, 0, 0))
    hero.add_column(no_wrap=True, width=9)  # mascot — widest line is 8 chars
    hero.add_column()  # title / subtitle — wraps on narrow terminals

    mascot = Text("\n".join(_MASCOT_LINES), style="bold bright_yellow")
    title = Text()
    title.append("AGENTHICC", style="bold yellow")
    title.append("\n")
    title.append("state-driven agent operating system", style="dim")
    # Keep the mascot as one multiline cell.  If the subtitle wraps while it
    # is represented as a separate table row, Rich increases that row's height
    # and pushes the feet below the subtitle instead of directly below the
    # face.
    hero.add_row(mascot, title)

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
        _label = "Dir    "  # 7 chars
        _path = str(cwd)
        _avail = left_w - len(_label)
        if len(_path) > _avail:
            _path = _path[: max(4, _avail - 1)] + "…"
        wd = Text()
        wd.append(_label, style="dim")
        wd.append(_path, style="dim")
        parts.append(wd)

    return Group(*parts)


# ── right column ──────────────────────────────────────────────────────────────


def _right_column(changelog: Sequence[str] = ()) -> "RenderableType":
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

    if changelog:
        for entry in changelog:
            line = Text()
            line.append("• ", style="yellow")
            line.append(entry, style="dim")
            parts.append(line)
    else:
        parts.append(Text("No list", style="dim"))

    return Group(*parts)


# ── public API ────────────────────────────────────────────────────────────────


def render_welcome(
    model: str = "",
    cwd: str = "",
    changelog: Sequence[str] = (),
) -> Align:
    """Return a Rich renderable for the startup welcome screen."""
    # Compute column widths from the live terminal size.
    # Panel overhead = border(2) + padding(3+3) = 8 cols.
    # Body separator padding = 4 cols between the two columns.
    # Left column gets ~40 % of the usable space, clamped to [32, 48].
    term_cols = shutil.get_terminal_size((80, 24)).columns
    usable = max(60, term_cols - 8)
    left_w = min(48, max(32, (usable - 4) * 2 // 5))

    body = Table.grid(padding=(0, 4, 0, 0))
    body.add_column(width=left_w)  # left — exact computed width
    body.add_column()  # right — takes remainder
    body.add_row(_left_column(model, cwd, left_w), _right_column(changelog))

    panel = Panel(
        body,
        box=box.ROUNDED,
        border_style="yellow",
        padding=(1, 3),
        expand=True,
    )
    return Align.center(panel)


def print_welcome(
    console: "Console",
    model: str = "",
    cwd: str = "",
    changelog: Sequence[str] = (),
) -> None:
    """Print the welcome panel to *console* (call before the Live block starts)."""
    console.print(render_welcome(model=model, cwd=cwd, changelog=changelog))
