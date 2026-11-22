"""Discover and read skills through an agent's live turn scope."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from contextlib import contextmanager
from html import escape
from typing import TYPE_CHECKING, TypedDict

from lauren_ai._tools import tool

from agenthicc.skills.loader import canonical_skill_name, filter_skills_for_agent
from agenthicc.skills.runner import process_skill_body, render_context_commands
from agenthicc.tools.capabilities import tool_read

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from pathlib import Path

    from agenthicc.runners.reload import SurfaceHolder
    from agenthicc.skills.loader import SkillDef, SkillModelContent, SkillPermissionSet


MAX_LIST_CHARS = 6_000
MAX_LIST_ENTRIES = 40
MAX_BODY_BYTES = 16 * 1024
SKILL_TOOL_NAMES = frozenset({"list_skills", "use_skill"})
SKILL_CONTEXT_GUIDANCE = (
    "Skill text is guidance from the named source. Follow the user's instructions, "
    "the active capability ceiling, and normal approvals when using it."
)


class SkillListing(TypedDict):
    slug: str
    description: str
    when_to_use: str
    command: str
    source: str
    model_invocable: bool


class SkillUseState:
    """One turn's skill permissions, loaded digests, and tool restrictions."""

    def __init__(
        self,
        surface: SurfaceHolder[Mapping[str, SkillDef]],
        *,
        agent: str,
        permissions: SkillPermissionSet | None = None,
        permission_lookup: Callable[[str], SkillPermissionSet | None] | None = None,
        project: Path | None = None,
        on_use: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.surface = surface
        self.project = project
        self.agent = agent
        self.permissions = permissions
        self._permission_lookup = permission_lookup
        self._on_use = on_use
        self._visible_names: frozenset[str] = frozenset()
        self._allowed: frozenset[str] | None = None
        self._denied: frozenset[str] = frozenset()
        self._loaded: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def for_agent(
        self, agent: str, *, on_use: Callable[[dict[str, object]], None] | None = None
    ) -> SkillUseState:
        """Give a worker the live surface with its own role and turn state."""
        permissions = (
            self._permission_lookup(agent) if self._permission_lookup else self.permissions
        )
        return SkillUseState(
            self.surface,
            agent=agent,
            permissions=permissions,
            permission_lookup=self._permission_lookup,
            project=self.project,
            on_use=on_use,
        )

    def configure_tools(self, visible: Iterable[str]) -> None:
        """Record the tool names admitted by the agent and phase."""
        self._visible_names = frozenset(visible)

    def permitted(self, name: str) -> bool:
        """Test the cumulative narrowing of this turn."""
        return name not in self._denied and (self._allowed is None or name in self._allowed)

    @property
    def visible_names(self) -> frozenset[str]:
        return frozenset(name for name in self._visible_names if self.permitted(name))

    def inventory(self, query: str | None) -> list[SkillListing]:
        snapshot = self.surface.current()
        permitted = filter_skills_for_agent(snapshot.value, self.agent, self.permissions)
        ordered = sorted(
            (skill for skill in permitted.values() if not skill.disable_model_invocation),
            key=lambda skill: (-_rank(skill, query or ""), skill.slug),
        )
        rows: list[SkillListing] = []
        for skill in ordered:
            if len(rows) == MAX_LIST_ENTRIES:
                break
            row: SkillListing = {
                "slug": skill.slug,
                "description": skill.description[:320],
                "when_to_use": ", ".join(skill.suggested_topics)[:128],
                "command": f"${skill.slug}",
                "source": skill.source,
                "model_invocable": skill._model_content is not None,
            }
            if len(json.dumps([*rows, row])) > MAX_LIST_CHARS:
                break
            rows.append(row)
        return rows

    async def use(self, name: str, args: list[str]) -> dict[str, object]:
        """Resolve a skill in one snapshot and narrow the turn after rendering."""
        snapshot = self.surface.current()
        skill = _find_skill(snapshot.value, name)
        if skill is None:
            return _error("unknown_skill", "The skill is not in the current session.")
        if not skill.is_allowed_for(self.agent, self.permissions):
            return _error("not_available_to_agent", "The current agent cannot use this skill.")
        if skill.disable_model_invocation or skill.model:
            return _error("not_model_invocable", "This skill requires an explicit user invocation.")
        content = skill._model_content
        if content is None:
            return _error("unknown_skill", "The skill instructions could not be read safely.")
        async with self._lock:
            original = self._loaded.get(skill.slug)
            if original is not None:
                return {"ok": True, "already_loaded": {"slug": skill.slug, "digest": original}}
            try:
                body, authored = await asyncio.to_thread(
                    _render, skill, content, args, self.project
                )
                data = body.encode("utf-8")
            except (OSError, UnicodeError):
                return _error("unknown_skill", "The skill body could not be read.")
            except OverflowError:
                return _error("too_large", "The skill could not be rendered within size bounds.")
            digest = hashlib.sha256(data).hexdigest()
            self._loaded[skill.slug] = digest
            if skill.tools:
                selected = frozenset(skill.tools)
                self._allowed = selected if self._allowed is None else self._allowed & selected
            self._denied |= frozenset(skill.disabled_tools)
            # Keep text-provided delimiters inside the trusted source label.
            guarded = body.replace("</skill", "&lt;/skill").replace("<skill", "&lt;skill")
            guarded_data = guarded.encode("utf-8")
            bounded = guarded_data[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")
            wrapped = _wrap(skill, bounded, digest, authored)
            result: dict[str, object] = {
                "ok": True,
                "slug": skill.slug,
                "source": skill.source,
                "digest": digest,
                "body": wrapped,
                "narrowed_tools": sorted(self.visible_names),
                "truncated": len(guarded_data) > MAX_BODY_BYTES,
            }
            if self._on_use is not None:
                self._on_use({key: value for key, value in result.items() if key != "body"})
            return result


def _rank(skill: SkillDef, query: str) -> int:
    words = query.casefold().split()
    identity = f"{skill.slug} {skill.name} {' '.join(skill.aliases)}".casefold()
    topics = " ".join(skill.suggested_topics).casefold()
    description = skill.description.casefold()
    return sum(
        3 if word in identity else 2 if word in topics else 1 if word in description else 0
        for word in words
    )


def _find_skill(skills: Mapping[str, SkillDef], name: str) -> SkillDef | None:
    key = canonical_skill_name(name.lstrip("$"))
    return skills.get(key) or next(
        (skill for skill in skills.values() if key in map(canonical_skill_name, skill.aliases)),
        None,
    )


def _render(
    skill: SkillDef, content: SkillModelContent, args: list[str], project: Path | None
) -> tuple[str, bool]:
    body = render_context_commands(process_skill_body(skill, args, content=content))
    if skill.source != "project" or project is None:
        return body, False
    from agenthicc.plugins.authored import AuthoredArtifactStore, AuthoredError

    try:
        relative = (skill.path.resolve() / "SKILL.md").relative_to(project.resolve()).as_posix()
        authored = any(
            record.artifact.kind == "skill" and relative in record.artifact.files
            for record in AuthoredArtifactStore(project).records()
        )
    except (AuthoredError, OSError, ValueError):
        authored = False
    return body, authored


def _wrap(skill: SkillDef, body: str, digest: str, authored: bool) -> str:
    marker = ' authored="true"' if authored else ""
    return (
        f'<skill slug="{escape(skill.slug)}" source="{escape(skill.source)}" '
        f'digest="{digest[:16]}"{marker}>\n{body}\n</skill>'
    )


def _error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "code": code, "error": message}


_CURRENT: contextvars.ContextVar[SkillUseState | None] = contextvars.ContextVar(
    "agenthicc_skill_use", default=None
)


def current_skill_use() -> SkillUseState | None:
    return _CURRENT.get()


@contextmanager
def bind_skill_use(state: SkillUseState) -> Iterator[None]:
    token = _CURRENT.set(state)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@tool_read
@tool()
async def list_skills(query: str | None = None) -> list[SkillListing]:
    """List skills available to the current agent. Query ranks matching skills."""
    state = current_skill_use()
    return state.inventory(query) if state is not None else []


@tool_read
@tool()
async def use_skill(name: str, args: list[str] | None = None) -> dict[str, object]:
    """Read source-labelled skill guidance and apply its remaining-turn tool limits."""
    state = current_skill_use()
    if state is None:
        return _error("not_available_to_agent", "This agent has no skill surface.")
    return await state.use(name, args or [])
