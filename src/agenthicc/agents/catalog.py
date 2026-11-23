"""Bounded discovery for the capabilities visible to a model request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable
    from pathlib import Path

    from agenthicc.plugins.authored_types import ArtifactKind
    from agenthicc.runners.reload import ReloadService
    from agenthicc.skills.loader import SkillDef
    from agenthicc.workflows.stack_tools import WorkflowToolScope

CATALOG_LIMIT = 1_500
_Kind = Literal["workflow", "skill", "tool"]
_HEADINGS: tuple[tuple[_Kind, str], ...] = (
    ("workflow", "Workflows · enter_workflow"),
    ("skill", "Skills · use_skill"),
    ("tool", "New tools this session"),
)


def _one_line(text: str) -> str:
    return " ".join(text.split())[:160]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    kind: _Kind
    name: str
    description: str = ""
    topics: tuple[str, ...] = ()
    builtin: bool = False
    new: bool = False

    def line(self) -> str:
        description = _one_line(self.description)
        details = f" · {description}" if description else ""
        topics = _one_line(", ".join(self.topics))
        if topics:
            details += f" (use when {topics})"
        return f"- {_one_line(self.name)}{details}{' [new this session]' if self.new else ''}"


def render_capability_catalog(
    entries: Iterable[CatalogEntry],
    *,
    recent_text: str = "",
    used_workflows: Collection[str] = (),
) -> str:
    """Render eligible entries within the catalog's fixed character budget."""
    rows = sorted(entries, key=lambda entry: (entry.kind, entry.name))
    if not rows:
        return ""
    recent = recent_text.casefold().replace("-", " ")
    retained = dict(enumerate(rows))

    def removal_priority(index: int) -> tuple[int, int]:
        entry = rows[index]
        if entry.new:
            return (4, index)
        if entry.kind == "skill":
            matched = any(
                phrase.casefold().replace("-", " ") in recent
                for phrase in (entry.name, *entry.topics)
                if phrase
            )
            return (3 if matched else 0, index)
        if entry.kind == "workflow" and entry.builtin and entry.name in used_workflows:
            return (1, index)
        return (2, index)

    def render() -> str:
        lines = ["Available to you"]
        for kind, heading in _HEADINGS:
            selected = [entry.line() for entry in retained.values() if entry.kind == kind]
            if selected:
                lines.extend((heading, *selected))
        omitted = len(rows) - len(retained)
        if omitted:
            lines.append(f"… {omitted} more (list_workflows / list_skills)")
        return "\n".join(lines)

    catalog = render()
    for index in sorted(retained, key=removal_priority):
        if len(catalog) <= CATALOG_LIMIT:
            break
        del retained[index]
        catalog = render()
    return catalog


class CapabilityCatalog:
    """Cache installed provenance per reload and retain the frame's last prompt."""

    def __init__(
        self, workspace: Path, session_id: str, reload: ReloadService, opened_at: float
    ) -> None:
        self._workspace = workspace
        self._session_id = session_id
        self._reload = reload
        self._opened = datetime.fromtimestamp(opened_at, UTC)
        self._generation = -1
        self._installed: frozenset[tuple[ArtifactKind, str]] = frozenset()
        self._frame: tuple[str, str | None, str] | None = None
        self._prompt_generation = -1
        self._prompt = ""

    @property
    def generation(self) -> int:
        return self._reload.generation

    def _new_artifacts(self) -> frozenset[tuple[ArtifactKind, str]]:
        """Return installed artifacts from this session or after it opened."""
        from agenthicc.plugins.authored import AuthoredArtifactStore, AuthoredError

        generation = self._reload.generation
        if generation != self._generation:
            installed: set[tuple[ArtifactKind, str]] = set()
            try:
                records = AuthoredArtifactStore(self._workspace).records()
                for record in records:
                    authored_at = datetime.fromisoformat(record.authored_at).astimezone(UTC)
                    if record.session_id == self._session_id or authored_at >= self._opened:
                        installed.add((record.artifact.kind, record.artifact.name))
            except (AuthoredError, OSError, ValueError):
                installed.clear()
            self._installed = frozenset(installed)
            self._generation = generation
        return self._installed

    def render(
        self,
        scope: WorkflowToolScope,
        skills: Iterable[SkillDef],
        visible_names: Collection[str],
        recent_text: str,
    ) -> str:
        installed = self._new_artifacts()
        entries: list[CatalogEntry] = []
        if "enter_workflow" in visible_names:
            entries.extend(
                CatalogEntry(
                    "workflow",
                    entry.plugin_cls.name,
                    entry.plugin_cls.invocation.when_to_use,
                    builtin=entry.source == "builtin",
                    new=("workflow", entry.plugin_cls.name) in installed,
                )
                for entry in scope.callable_workflows()
            )
        if "use_skill" in visible_names:
            entries.extend(
                CatalogEntry(
                    "skill",
                    skill.slug,
                    skill.description,
                    tuple(skill.suggested_topics),
                    new=("skill", skill.slug) in installed,
                )
                for skill in skills
            )
        entries.extend(
            CatalogEntry("tool", name, new=True)
            for kind, name in installed
            if kind == "tool" and name in visible_names
        )
        text = render_capability_catalog(
            entries,
            recent_text=recent_text,
            used_workflows={returned.workflow_name for returned in scope.stack.recent_returns()},
        )
        if not scope.is_worker:
            self._frame = (scope.stack.top.frame_id, scope.stack.top.phase, scope.mode_name)
            self._prompt_generation = self._reload.generation
            self._prompt = text
        return text

    def current(self, scope: WorkflowToolScope) -> str | None:
        """Give the operator the text offered to the current model."""
        frame = (scope.stack.top.frame_id, scope.stack.top.phase, scope.mode_name)
        if frame == self._frame and self._prompt_generation == self._reload.generation:
            return self._prompt
        return None
