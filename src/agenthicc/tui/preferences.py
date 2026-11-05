"""Read the [ui] accessibility preferences from TUI state (AGX-13)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.config import UISettings

__all__ = ["save_ui_preference", "ui_settings", "user_config_path"]


def ui_settings(state: object) -> UISettings | None:
    """The state's [ui] preferences, or ``None`` for state without them (test doubles)."""
    from agenthicc.config import UISettings

    try:
        value = object.__getattribute__(state, "ui")
    except AttributeError:
        return None
    current = value() if callable(value) else value
    return current if isinstance(current, UISettings) else None


def user_config_path() -> Path:
    """The user configuration file a saved preference goes to.

    The first existing user candidate wins (the loader's order); otherwise
    ``~/.agenthicc/agenthicc.toml``.
    """
    from agenthicc.config import USER_CONFIG_CANDIDATES

    for candidate in USER_CONFIG_CANDIDATES:
        if candidate.is_file():
            return candidate
    return USER_CONFIG_CANDIDATES[0]


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def save_ui_preference(key: str, value: str, path: Path | None = None) -> Path:
    """Set ``[ui] key = "value"`` in the user configuration; return the file.

    Only the one line changes: comments, ordering and every other table stay
    as written.  A missing ``[ui]`` table is appended.
    """
    if not _KEY.match(key):
        raise ValueError(f"invalid [ui] key {key!r}")
    target = path or user_config_path()
    encoded = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    assignment = f"{key} = {encoded}"
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    section: str | None = None
    ui_start: int | None = None
    ui_end = len(lines)
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "ui":
                ui_end = index
                break
            section = stripped.strip("[]").strip()
            if section == "ui":
                ui_start = index
            continue
        if section == "ui" and re.match(rf"^\s*{re.escape(key)}\s*=", line):
            lines[index] = assignment
            replaced = True
            break
    if not replaced:
        if ui_start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["[ui]", assignment])
        else:
            insert_at = ui_end
            while insert_at - 1 > ui_start and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, assignment)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
