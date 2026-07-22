"""Agent-scoped tool plugin configuration (PRD-26)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["validate_agent_name", "AgentDef", "discover_agents", "load_agent_system_prompt"]

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_MAX_LEN = 64


def validate_agent_name(name: str) -> str:
    """Return the normalised name or raise ValueError."""
    name = name.strip().lower()
    if not _SLUG_RE.match(name) or len(name) > _MAX_LEN:
        raise ValueError(
            f"Invalid agent name {name!r}. Use lowercase letters, digits, and hyphens only."
        )
    return name


@dataclass
class AgentDef:
    """Configuration for a named agent."""

    name: str  # validated slug
    system_prompt: str = ""  # custom system prompt (may be empty)
    tool_plugin_paths: list[Path] = field(default_factory=list)  # discovered tool files

    @classmethod
    def from_directory(
        cls,
        agent_dir: Path,
        *,
        user_agent_dir: Path | None = None,
    ) -> "AgentDef":
        """Build an AgentDef by scanning the agent directory tree."""
        name = validate_agent_name(agent_dir.name)

        # System prompt: project-local wins over user-global.
        # Iterate [user_agent_dir, agent_dir] and overwrite with last found.
        prompt = ""
        for base in filter(None, [user_agent_dir, agent_dir]):
            sp = base / "system_prompt.md"
            if sp.exists():
                content = sp.read_text(encoding="utf-8").strip()
                if content:
                    prompt = content

        return cls(name=name, system_prompt=prompt)


def discover_agents(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> dict[str, AgentDef]:
    """Scan for all named agent directories; return dict keyed by slug."""
    user_agents_root = (user_dir or Path.home() / ".agenthicc") / "agents"
    project_agents_root = (project_dir or Path(".agenthicc")) / "agents"

    agents: dict[str, AgentDef] = {}

    for root in (user_agents_root, project_agents_root):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                slug = validate_agent_name(entry.name)
            except ValueError:
                continue
            user_entry = user_agents_root / entry.name if root == project_agents_root else None
            agents[slug] = AgentDef.from_directory(entry, user_agent_dir=user_entry)

    return agents


def load_agent_system_prompt(
    agent_name: str,
    base_prompt: str,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> str:
    """Return the effective system prompt for a named agent.

    Checks project directory first (higher precedence), then user directory.
    Returns ``base_prompt`` if neither location has a non-empty system_prompt.md.
    """
    project_root = (project_dir or Path(".agenthicc")) / "agents" / agent_name
    user_root = (user_dir or Path.home() / ".agenthicc") / "agents" / agent_name

    for root in (project_root, user_root):  # project wins
        sp = root / "system_prompt.md"
        if sp.exists():
            content = sp.read_text(encoding="utf-8").strip()
            if content:
                return content

    return base_prompt
