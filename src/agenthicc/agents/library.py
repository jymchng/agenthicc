"""Safe file operations for user-authored Markdown agent definitions (AGX-09)."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from agenthicc.agents.definitions import AgentSpec, parse_definition, render_definition

__all__ = [
    "DefinitionScope",
    "agent_directory",
    "delete_definition",
    "write_definition",
]

DefinitionScope = Literal["project", "user"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def agent_directory(scope: DefinitionScope, *, project_root: Path) -> Path:
    """The definition directory at *scope* for a project."""
    if scope == "project":
        return project_root / ".agenthicc" / "agents"
    return Path.home() / ".agenthicc" / "agents"


def _target(name: str, directory: Path) -> Path:
    if not _NAME.fullmatch(name):
        raise ValueError("agent name must be lowercase letters, digits, '-' or '_' (64 at most)")
    return directory / f"{name}.md"


def write_definition(
    spec: AgentSpec,
    *,
    scope: DefinitionScope,
    project_root: Path,
    overwrite: bool = False,
) -> Path:
    """Write a validated definition atomically, refusing path escapes and symlinks."""
    directory = agent_directory(scope, project_root=project_root)
    directory.mkdir(parents=True, exist_ok=True)
    root = directory.resolve(strict=True)
    path = _target(spec.name, directory)
    if path.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {path}")
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    if path.parent.resolve(strict=True) != root:
        raise ValueError(f"definition path escapes its {scope} agent directory")

    content = render_definition(spec)
    # Catch invalid specs before changing a file, including name/frontmatter mismatches.
    parse_definition(content, path=path, source=scope)
    fd, temporary = tempfile.mkstemp(prefix=".agent-", suffix=".tmp", dir=root)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp_path, path)
        else:
            # link() is atomic and fails if another writer created the target.
            os.link(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)

    if scope == "project":
        from agenthicc.plugins.trust import current_extension_trust

        trust = current_extension_trust()
        if overwrite:
            # Editing a trusted definition changes its digest; do not silently
            # authorize the new content as part of a file-management action.
            trust.revoke(path)
        else:
            trust.trust(path, kind="agent")
    return path


def delete_definition(
    path: Path,
    *,
    project_root: Path,
    user_root: Path | None = None,
) -> None:
    """Delete one Markdown definition inside the project or user agent directory."""
    if path.suffix != ".md" or path.is_symlink():
        raise ValueError(f"only regular Markdown agent definitions can be deleted: {path}")
    candidate = path.resolve(strict=True)
    user_base = (user_root or Path.home() / ".agenthicc") / "agents"
    allowed = {
        (project_root / ".agenthicc" / "agents").resolve(),
        user_base.resolve(),
    }
    if candidate.parent not in allowed or not candidate.is_file():
        raise ValueError(f"definition is outside the project and user agent directories: {path}")
    candidate.unlink()
    if candidate.parent.name == "agents" and candidate.parent.parent.name == ".agenthicc":
        # Removing the file also removes its digest-bound project trust record.
        from agenthicc.plugins.trust import current_extension_trust

        current_extension_trust().revoke(candidate)
