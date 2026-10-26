"""A readable table of the effective key bindings (AGX-13)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agenthicc.tui.keymap import CONTEXTS, KeymapLoad

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["render_bindings"]


def render_bindings(console: Console, loaded: KeymapLoad) -> None:
    """Print each context's actions: keys, description, when it applies and source."""
    from rich.markup import escape
    from rich.table import Table

    console.print(f"Key bindings file: {loaded.path}", markup=False, highlight=False)
    if loaded.errors:
        console.print(
            "[red]The file is not applied — the defaults are in effect:[/red]", highlight=False
        )
        for error in loaded.errors:
            console.print(f"  [red]•[/red] {escape(error)}", highlight=False)
    keymap = loaded.keymap
    for context in CONTEXTS:
        actions = [action for action in keymap.actions if action.context == context]
        if not actions:
            continue
        table = Table(title=context, title_justify="left", show_edge=False, pad_edge=False)
        table.add_column("Keys", no_wrap=True)
        table.add_column("Action", no_wrap=True)
        table.add_column("Does")
        table.add_column("When")
        table.add_column("From", no_wrap=True)
        for entry in keymap.help(context):
            if keymap.action(entry.action).context != context:
                continue
            source = "default" if entry.source == "default" else "your file"
            table.add_row(
                escape(entry.keys),
                escape(entry.action),
                escape(entry.description),
                escape(entry.conditions or "always"),
                source,
            )
        console.print(table)
