"""Validated skill discovery, compatibility, and permission helpers (PRD-22/139)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Literal

log = logging.getLogger(__name__)

__all__ = [
    "SkillDef",
    "SkillDiagnostic",
    "SkillDiscoveryResult",
    "SkillPermissionSet",
    "_parse_skill",
    "canonical_skill_name",
    "discover_skills",
    "discover_skills_with_diagnostics",
    "filter_skills_for_agent",
]

DiagnosticSeverity = Literal["info", "warning", "error"]

MAX_SKILL_NAME_LENGTH: Final[int] = 64
MAX_SKILL_DESCRIPTION_LENGTH: Final[int] = 1_536
_CANONICAL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "description",
        "author",
        "tags",
        "suggestedTopics",
        "disallowAutoTriggering",
        "tools",
        "disabledTools",
        "maxTurnDepth",
        "model",
        "aliases",
        "allowedAgents",
        "deniedAgents",
        "permissions",
        "source",
        "version",
    }
)
_FIELD_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "suggestedTopics": ("suggested_topics", "topics"),
    "disallowAutoTriggering": ("disallow_auto_triggering",),
    "disabledTools": ("disabled_tools",),
    "maxTurnDepth": ("max_turn_depth",),
    "allowedAgents": ("allowed_agents", "allowAgents", "allow_agents"),
    "deniedAgents": ("denied_agents", "denyAgents", "deny_agents"),
    "aliases": ("alias",),
}


@dataclass(frozen=True)
class SkillDiagnostic:
    """Actionable result from one skill discovery attempt."""

    path: Path
    code: str
    message: str
    severity: DiagnosticSeverity = "warning"

    def __str__(self) -> str:
        return f"{self.path}: {self.message} ({self.code})"


@dataclass(frozen=True)
class SkillPermissionSet:
    """Per-agent allow/deny rules for skill activation.

    ``allowed_skills=None`` means no config allowlist is applied. Names may be
    canonical skill names, compatibility aliases, or ``*``.
    """

    allowed_skills: frozenset[str] | None = None
    denied_skills: frozenset[str] = frozenset()


@dataclass
class SkillDef:
    """Validated representation of one skill directory."""

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
    aliases: tuple[str, ...] = ()
    allowed_agents: tuple[str, ...] | None = None
    denied_agents: tuple[str, ...] = ()
    source: str = "unknown"
    _body: str | None = field(default=None, repr=False)

    @property
    def canonical_name(self) -> str:
        """Return the canonical explicit trigger name without its prefix."""

        return self.slug

    @property
    def command_names(self) -> tuple[str, ...]:
        """Return the canonical name followed by compatibility aliases."""

        return (self.slug, *self.aliases)

    def is_allowed_for(
        self,
        agent_type: str,
        permissions: SkillPermissionSet | None = None,
    ) -> bool:
        """Return whether *agent_type* may activate this skill."""

        agent_name = _normalise_agent_name(agent_type)
        if self.allowed_agents is not None and not _matches_name(agent_name, self.allowed_agents):
            return False
        if _matches_name(agent_name, self.denied_agents):
            return False

        if permissions is None:
            return True
        if permissions.allowed_skills is not None and not _matches_skill_name(
            self, permissions.allowed_skills
        ):
            return False
        return not _matches_skill_name(self, permissions.denied_skills)

    @property
    def body(self) -> str:
        """Lazy-load the instruction body on first access."""

        if self._body is None:
            raw = (self.path / "SKILL.md").read_text(encoding="utf-8")
            self._body = _extract_body(raw)
        return self._body


@dataclass(frozen=True)
class SkillDiscoveryResult:
    """Skills plus diagnostics collected while scanning both scopes."""

    skills: dict[str, SkillDef]
    diagnostics: tuple[SkillDiagnostic, ...] = ()


def canonical_skill_name(value: str) -> str:
    """Convert a legacy directory/name value to a safe kebab-case slug."""

    normalised = value.strip().lstrip("/").lower()
    normalised = re.sub(r"[\s_]+", "-", normalised)
    normalised = re.sub(r"[^a-z0-9-]+", "-", normalised)
    return re.sub(r"-+", "-", normalised).strip("-")


def _normalise_agent_name(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _normalise_alias(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    alias = value.strip().lstrip("/")
    if not alias or any(char.isspace() for char in alias) or "/" in alias:
        return None
    return alias


def _matches_name(value: str, candidates: tuple[str, ...]) -> bool:
    return any(
        candidate == "*" or _normalise_agent_name(candidate) == value for candidate in candidates
    )


def _matches_skill_name(skill: SkillDef, candidates: frozenset[str]) -> bool:
    names = {canonical_skill_name(name) for name in skill.command_names}
    return any(
        candidate == "*" or canonical_skill_name(candidate) in names for candidate in candidates
    )


def _split_frontmatter(raw: str) -> tuple[str | None, str]:
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, raw
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise ValueError("frontmatter starts with '---' but has no closing delimiter")


def _extract_body(raw: str) -> str:
    try:
        _, body = _split_frontmatter(raw)
    except ValueError:
        return raw
    return body.strip()


def _metadata_value(
    metadata: Mapping[str, object],
    canonical: str,
    diagnostics: list[SkillDiagnostic],
    path: Path,
) -> object | None:
    if canonical in metadata:
        for alias in _FIELD_ALIASES.get(canonical, ()):
            if alias in metadata:
                diagnostics.append(
                    SkillDiagnostic(
                        path,
                        "duplicate-field-alias",
                        f"uses both {canonical!r} and compatibility field {alias!r}; "
                        f"{canonical!r} wins",
                        "warning",
                    )
                )
        return metadata[canonical]
    for alias in _FIELD_ALIASES.get(canonical, ()):
        if alias in metadata:
            diagnostics.append(
                SkillDiagnostic(
                    path,
                    "compatibility-field",
                    f"field {alias!r} is supported for compatibility; use {canonical!r}",
                    "info",
                )
            )
            return metadata[alias]
    return None


def _string_list(
    value: object | None,
    field_name: str,
    diagnostics: list[SkillDiagnostic],
    path: Path,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Older skills commonly used a single topic/tool string.
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        diagnostics.append(
            SkillDiagnostic(
                path, "invalid-field-type", f"{field_name} must be a string list", "error"
            )
        )
        return []
    return [item.strip() for item in value if item.strip()]


def _agent_list(
    value: object | None,
    field_name: str,
    diagnostics: list[SkillDiagnostic],
    path: Path,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    values = _string_list(value, field_name, diagnostics, path)
    return tuple(_normalise_agent_name(item) for item in values)


def _parse_skill_detailed(
    skill_dir: Path,
    source: str,
) -> tuple[SkillDef | None, list[SkillDiagnostic]]:
    diagnostics: list[SkillDiagnostic] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        diagnostics.append(
            SkillDiagnostic(skill_dir, "missing-skill-file", "SKILL.md is missing", "warning")
        )
        return None, diagnostics

    canonical = canonical_skill_name(skill_dir.name)
    if (
        not canonical
        or len(canonical) > MAX_SKILL_NAME_LENGTH
        or _CANONICAL_NAME_RE.fullmatch(canonical) is None
    ):
        diagnostics.append(
            SkillDiagnostic(
                skill_dir,
                "invalid-canonical-name",
                f"directory name {skill_dir.name!r} cannot produce a canonical "
                f"kebab-case name of at most {MAX_SKILL_NAME_LENGTH} characters",
                "error",
            )
        )
        return None, diagnostics
    if skill_dir.name != canonical:
        diagnostics.append(
            SkillDiagnostic(
                skill_dir,
                "legacy-directory-name",
                f"using canonical name {canonical!r}; {skill_dir.name!r} remains a compatibility alias",
                "warning",
            )
        )

    try:
        raw = skill_md.read_text(encoding="utf-8")
        frontmatter, _ = _split_frontmatter(raw)
    except (OSError, UnicodeError) as exc:
        diagnostics.append(SkillDiagnostic(skill_md, "read-error", str(exc), "error"))
        return None, diagnostics
    except ValueError as exc:
        diagnostics.append(SkillDiagnostic(skill_md, "invalid-frontmatter", str(exc), "error"))
        return None, diagnostics

    metadata: Mapping[str, object]
    if frontmatter is None:
        metadata = {}
        diagnostics.append(
            SkillDiagnostic(
                skill_md,
                "missing-frontmatter",
                "SKILL.md has no YAML frontmatter; legacy fallback metadata is being used",
                "warning",
            )
        )
    else:
        try:
            import yaml

            loaded = yaml.safe_load(frontmatter)
        except ImportError as exc:
            diagnostics.append(
                SkillDiagnostic(skill_md, "missing-yaml-dependency", str(exc), "error")
            )
            return None, diagnostics
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(SkillDiagnostic(skill_md, "invalid-yaml", str(exc), "error"))
            return None, diagnostics
        if loaded is None:
            metadata = {}
        elif isinstance(loaded, Mapping):
            metadata = loaded
        else:
            diagnostics.append(
                SkillDiagnostic(
                    skill_md, "invalid-frontmatter", "frontmatter must be a mapping", "error"
                )
            )
            return None, diagnostics

    unknown = sorted(
        set(metadata)
        - _KNOWN_FIELDS
        - {alias for aliases in _FIELD_ALIASES.values() for alias in aliases}
    )
    if unknown:
        diagnostics.append(
            SkillDiagnostic(
                skill_md,
                "unknown-frontmatter-field",
                f"unknown field(s): {', '.join(unknown)}",
                "warning",
            )
        )

    name_value = _metadata_value(metadata, "name", diagnostics, skill_md)
    if name_value is None:
        name = skill_dir.name
        if frontmatter is not None:
            diagnostics.append(
                SkillDiagnostic(
                    skill_md, "missing-name", "name is missing; directory name is used", "warning"
                )
            )
    elif not isinstance(name_value, str) or not name_value.strip():
        diagnostics.append(
            SkillDiagnostic(skill_md, "invalid-name", "name must be a non-empty string", "error")
        )
        return None, diagnostics
    else:
        name = name_value.strip()

    description_value = _metadata_value(metadata, "description", diagnostics, skill_md)
    if description_value is None:
        description = ""
        if frontmatter is not None:
            diagnostics.append(
                SkillDiagnostic(
                    skill_md, "missing-description", "description is missing", "warning"
                )
            )
    elif not isinstance(description_value, str):
        diagnostics.append(
            SkillDiagnostic(
                skill_md, "invalid-description", "description must be a string", "error"
            )
        )
        return None, diagnostics
    else:
        description = description_value.strip()
        if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
            diagnostics.append(
                SkillDiagnostic(
                    skill_md,
                    "description-too-long",
                    f"description exceeds {MAX_SKILL_DESCRIPTION_LENGTH} characters",
                    "error",
                )
            )
            return None, diagnostics

    permissions = metadata.get("permissions")
    if permissions is not None and not isinstance(permissions, Mapping):
        diagnostics.append(
            SkillDiagnostic(
                skill_md,
                "invalid-permissions",
                "permissions must be a mapping",
                "error",
            )
        )
    permission_map = permissions if isinstance(permissions, Mapping) else {}
    permission_agents = permission_map.get("agents")
    if permission_agents is not None and not isinstance(permission_agents, Mapping):
        diagnostics.append(
            SkillDiagnostic(
                skill_md,
                "invalid-permissions",
                "permissions.agents must be a mapping",
                "error",
            )
        )
    permission_agent_map = permission_agents if isinstance(permission_agents, Mapping) else {}
    allowed_agents_value = _metadata_value(metadata, "allowedAgents", diagnostics, skill_md)
    denied_agents_value = _metadata_value(metadata, "deniedAgents", diagnostics, skill_md)
    if allowed_agents_value is None:
        allowed_agents_value = permission_agent_map.get("allow")
    if denied_agents_value is None:
        denied_agents_value = permission_agent_map.get("deny")

    aliases_value = _metadata_value(metadata, "aliases", diagnostics, skill_md)
    aliases = _string_list(aliases_value, "aliases", diagnostics, skill_md)
    if skill_dir.name != canonical:
        aliases.insert(0, skill_dir.name)
    valid_aliases: list[str] = []
    seen_aliases: set[str] = set()
    for raw_alias in aliases:
        alias = _normalise_alias(raw_alias)
        if alias is None:
            diagnostics.append(
                SkillDiagnostic(
                    skill_md,
                    "invalid-alias",
                    f"invalid compatibility alias: {raw_alias!r}",
                    "error",
                )
            )
        elif alias.lower() == canonical:
            diagnostics.append(
                SkillDiagnostic(
                    skill_md, "redundant-alias", f"alias {alias!r} is the canonical name", "warning"
                )
            )
        elif alias.lower() not in seen_aliases:
            seen_aliases.add(alias.lower())
            valid_aliases.append(alias)

    max_turn_value = _metadata_value(metadata, "maxTurnDepth", diagnostics, skill_md)
    max_turn_depth = 200
    if max_turn_value is not None:
        if (
            isinstance(max_turn_value, bool)
            or not isinstance(max_turn_value, int)
            or max_turn_value < 1
        ):
            diagnostics.append(
                SkillDiagnostic(
                    skill_md,
                    "invalid-max-turn-depth",
                    "maxTurnDepth must be a positive integer",
                    "error",
                )
            )
            return None, diagnostics
        max_turn_depth = max_turn_value

    bool_value = _metadata_value(metadata, "disallowAutoTriggering", diagnostics, skill_md)
    disallow_auto = False
    if bool_value is not None:
        if not isinstance(bool_value, bool):
            diagnostics.append(
                SkillDiagnostic(
                    skill_md,
                    "invalid-boolean",
                    "disallowAutoTriggering must be a boolean",
                    "error",
                )
            )
            return None, diagnostics
        disallow_auto = bool_value

    author_value = metadata.get("author")
    if author_value is not None and not isinstance(author_value, str):
        diagnostics.append(
            SkillDiagnostic(skill_md, "invalid-author", "author must be a string", "error")
        )
    model_value = metadata.get("model")
    if model_value is not None and not isinstance(model_value, str):
        diagnostics.append(
            SkillDiagnostic(skill_md, "invalid-model", "model must be a string", "error")
        )

    skill = SkillDef(
        name=name,
        slug=canonical,
        path=skill_dir,
        description=description,
        author=author_value.strip() if isinstance(author_value, str) else "",
        tags=_string_list(metadata.get("tags"), "tags", diagnostics, skill_md),
        suggested_topics=_string_list(
            _metadata_value(metadata, "suggestedTopics", diagnostics, skill_md),
            "suggestedTopics",
            diagnostics,
            skill_md,
        ),
        disallow_auto_triggering=disallow_auto,
        tools=_string_list(metadata.get("tools"), "tools", diagnostics, skill_md),
        disabled_tools=_string_list(
            _metadata_value(metadata, "disabledTools", diagnostics, skill_md),
            "disabledTools",
            diagnostics,
            skill_md,
        ),
        max_turn_depth=max_turn_depth,
        model=model_value.strip() if isinstance(model_value, str) else "",
        aliases=tuple(valid_aliases),
        allowed_agents=_agent_list(
            allowed_agents_value,
            "allowedAgents",
            diagnostics,
            skill_md,
        ),
        denied_agents=_agent_list(
            denied_agents_value,
            "deniedAgents",
            diagnostics,
            skill_md,
        )
        or (),
        source=source,
    )
    if any(diagnostic.severity == "error" for diagnostic in diagnostics):
        return None, diagnostics
    return skill, diagnostics


def _parse_skill(skill_dir: Path) -> SkillDef | None:
    """Parse a skill directory, retaining the legacy ``None`` failure API."""

    skill, diagnostics = _parse_skill_detailed(skill_dir, "unknown")
    for diagnostic in diagnostics:
        if diagnostic.severity == "error":
            log.warning("Skill rejected: %s", diagnostic)
        else:
            log.info("Skill diagnostic: %s", diagnostic)
    return skill


def discover_skills_with_diagnostics(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> SkillDiscoveryResult:
    """Discover skills and return diagnostics instead of silently dropping them."""

    personal_root = (user_dir or Path.home() / ".agenthicc") / "skills"
    project_root = (project_dir or Path(".agenthicc")) / "skills"
    skills: dict[str, SkillDef] = {}
    diagnostics: list[SkillDiagnostic] = []

    for root, source in ((personal_root, "user"), (project_root, "project")):
        if not root.exists():
            continue
        if not root.is_dir():
            diagnostics.append(
                SkillDiagnostic(
                    root, "invalid-skill-root", "skill root is not a directory", "error"
                )
            )
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name.lower())
        except OSError as exc:
            diagnostics.append(SkillDiagnostic(root, "scan-error", str(exc), "error"))
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if not entry.is_dir():
                diagnostics.append(
                    SkillDiagnostic(
                        entry, "ignored-entry", "skill root entry is not a directory", "info"
                    )
                )
                continue
            skill, entry_diagnostics = _parse_skill_detailed(entry, source)
            diagnostics.extend(entry_diagnostics)
            if skill is None:
                continue
            if skill.slug in skills:
                diagnostics.append(
                    SkillDiagnostic(
                        entry,
                        "scope-override",
                        f"project skill overrides the previously discovered {skill.slug!r} skill",
                        "warning",
                    )
                )
            skills[skill.slug] = skill

    canonical_names = set(skills)
    claimed_aliases: dict[str, str] = {}
    for slug, skill in list(skills.items()):
        aliases: list[str] = []
        for alias in skill.aliases:
            alias_key = canonical_skill_name(alias)
            if alias_key in canonical_names and alias_key != slug:
                diagnostics.append(
                    SkillDiagnostic(
                        skill.path / "SKILL.md",
                        "alias-conflict",
                        f"alias {alias!r} conflicts with canonical skill {alias_key!r}; alias ignored",
                        "error",
                    )
                )
                continue
            owner = claimed_aliases.get(alias_key)
            if owner is not None and owner != slug:
                diagnostics.append(
                    SkillDiagnostic(
                        skill.path / "SKILL.md",
                        "alias-conflict",
                        f"alias {alias!r} is already owned by skill {owner!r}; alias ignored",
                        "error",
                    )
                )
                continue
            claimed_aliases[alias_key] = slug
            aliases.append(alias)
        skills[slug] = replace(skill, aliases=tuple(aliases))

    return SkillDiscoveryResult(skills=skills, diagnostics=tuple(diagnostics))


def discover_skills(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> dict[str, SkillDef]:
    """Discover all skills; project scope overrides user scope.

    This retains the original dictionary return type. Call
    :func:`discover_skills_with_diagnostics` when callers need actionable
    validation and precedence diagnostics.
    """

    result = discover_skills_with_diagnostics(project_dir=project_dir, user_dir=user_dir)
    for diagnostic in result.diagnostics:
        if diagnostic.severity == "error":
            log.warning("Skill discovery: %s", diagnostic)
        else:
            log.info("Skill discovery: %s", diagnostic)
    return result.skills


def filter_skills_for_agent(
    skills: Mapping[str, SkillDef],
    agent_type: str,
    permissions: SkillPermissionSet | None = None,
) -> dict[str, SkillDef]:
    """Return only skills permitted for an agent and its configured policy."""

    return {
        slug: skill
        for slug, skill in skills.items()
        if skill.is_allowed_for(agent_type, permissions)
    }
