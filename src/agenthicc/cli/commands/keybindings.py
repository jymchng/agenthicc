"""``agenthicc keybindings``: show or edit the user's key bindings (AGX-13)."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

from agenthicc.cli.context import CLIContext


def keybindings_show(ctx: CLIContext) -> None:
    """Print every action's effective keys, where they come from, and file errors."""
    from rich.console import Console

    from agenthicc.tui.actions import DEFAULT_ACTIONS
    from agenthicc.tui.keymap import load_keybindings
    from agenthicc.tui.keymap_table import render_bindings

    del ctx
    loaded = load_keybindings(DEFAULT_ACTIONS)
    render_bindings(Console(), loaded)


def keybindings_edit(ctx: CLIContext) -> None:
    """Open the bindings file in $VISUAL or $EDITOR, creating it first if needed."""
    from agenthicc.tui.actions import DEFAULT_ACTIONS
    from agenthicc.tui.keymap import default_keybindings_path, load_keybindings, template

    del ctx
    path = default_keybindings_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template(DEFAULT_ACTIONS), encoding="utf-8")
        print(f"Created {path}")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if sys.platform == "win32" else "vi"
    try:
        subprocess.run([*shlex.split(editor), str(path)], check=False)
    except OSError as exc:
        print(f"error: could not start {editor!r}: {exc}")
        raise SystemExit(1) from exc
    loaded = load_keybindings(DEFAULT_ACTIONS, path)
    if loaded.errors:
        print("The file is not applied until these are fixed:")
        for error in loaded.errors:
            print(f"  {error}")
        raise SystemExit(1)
    print("Key bindings are valid; running sessions apply them at once.")
