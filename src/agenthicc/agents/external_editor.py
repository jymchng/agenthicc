"""Safe launching of the user's editor for Markdown agent definitions."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from agenthicc.agents.library import DefinitionScope, agent_directory

__all__ = ["editor_is_configured", "open_definition_in_editor"]


def editor_is_configured() -> bool:
    """Whether the user has configured an external editor command."""
    return bool(os.environ.get("VISUAL", "").strip() or os.environ.get("EDITOR", "").strip())


def open_definition_in_editor(
    path: Path,
    *,
    scope: DefinitionScope,
    project_root: Path,
) -> None:
    """Open a trusted-scope Markdown definition without invoking a shell."""
    directory = agent_directory(scope, project_root=project_root).resolve(strict=True)
    if path.suffix != ".md" or path.is_symlink() or path.parent.resolve(strict=True) != directory:
        raise ValueError("the definition path is not a regular Markdown file in its scope")
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError("the definition path is not a regular Markdown file in its scope")

    configured = os.environ.get("VISUAL", "").strip() or os.environ.get("EDITOR", "").strip()
    if not configured:
        raise RuntimeError("Set $EDITOR to edit this definition externally.")
    try:
        command = shlex.split(configured)
    except ValueError as exc:
        raise ValueError(f"invalid editor command: {exc}") from exc
    if not command:
        raise ValueError("the configured editor command is empty")

    result = subprocess.run([*command, str(target)], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"the editor exited with status {result.returncode}")
