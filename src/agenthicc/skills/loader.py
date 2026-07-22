"""Skill discovery and lazy loading (PRD-22)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["SkillDef", "_parse_skill", "discover_skills"]


@dataclass
class SkillDef:
    """Parsed representation of a single skill."""

    name: str
    slug: str
    path: Path
    description: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    suggested_topics: list[str] = field(default_factory=list)
    disallow_auto_triggering: bool = False
    tools: list[str] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)
    max_turn_depth: int = 200
    model: str = ""
    _body: str | None = field(default=None, repr=False)

    @property
    def body(self) -> str:
        """Lazy-load SKILL.md body on first access."""
        if self._body is None:
            raw = (self.path / "SKILL.md").read_text(encoding="utf-8")
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                self._body = parts[2].strip() if len(parts) >= 3 else raw
            else:
                self._body = raw
        return self._body


def _parse_skill(skill_dir: Path) -> SkillDef | None:
    """Parse a skill directory; return None if SKILL.md missing or malformed."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        import yaml

        raw = skill_md.read_text(encoding="utf-8")
        meta: dict[str, object] = {}
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1]) or {}
        return SkillDef(
            name=str(meta.get("name", skill_dir.name)),
            slug=skill_dir.name,
            path=skill_dir,
            description=str(meta.get("description", "")),
            author=str(meta.get("author", "")),
            tags=list(meta.get("tags", [])),
            suggested_topics=list(meta.get("suggestedTopics", [])),
            disallow_auto_triggering=bool(meta.get("disallowAutoTriggering", False)),
            tools=list(meta.get("tools", [])),
            disabled_tools=list(meta.get("disabledTools", [])),
            max_turn_depth=int(meta.get("maxTurnDepth", 200)),
            model=str(meta.get("model", "")),
        )
    except Exception as exc:
        log.warning("Failed to parse skill %s: %s", skill_dir, exc)
        return None


def discover_skills(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> dict[str, SkillDef]:
    """Discover all skills; project overrides personal."""
    personal_root = (user_dir or Path.home() / ".agenthicc") / "skills"
    project_root = (project_dir or Path(".agenthicc")) / "skills"
    skills: dict[str, SkillDef] = {}
    for root in (personal_root, project_root):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                skill = _parse_skill(entry)
                if skill is not None:
                    skills[skill.slug] = skill
    return skills
